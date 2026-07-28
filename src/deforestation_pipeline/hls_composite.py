"""Construcción remota, explícita y auditable del primer composite anual HLS."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyproj import CRS
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.catalog import BandRole, BenchmarkSourcePlan, CatalogSource
from deforestation_pipeline.config import DataConfig, SpectralIndex
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_tiles import mgrs_tile_ids_from_hls_identifiers
from deforestation_pipeline.spectral_indices import DENOMINATOR_EPSILON, INDEX_FORMULAS

REFLECTANCE_BAND_ROLES = (
    BandRole.BLUE,
    BandRole.GREEN,
    BandRole.RED,
    BandRole.NIR,
    BandRole.SWIR1,
    BandRole.SWIR2,
)
REFLECTANCE_BAND_NAMES = tuple(role.value for role in REFLECTANCE_BAND_ROLES)
MASKED_FMASK_CLASSES = (
    "fill",
    "cloud",
    "adjacent_to_cloud_or_shadow",
    "cloud_shadow",
    "snow_or_ice",
    "high_aerosol",
)


class HlsCompositeError(RuntimeError):
    """Fallo sanitizado de preparación o consulta del composite."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Composite HLS fallido: {code}")


class StrictHlsModel(BaseModel):
    """Base inmutable para el contrato remoto del Paso 10."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HlsCompositeRequest(StrictHlsModel):
    """Solicitud anual cuya semántica temporal no admite ventanas ambiguas."""

    start_date: date
    end_date_exclusive: date
    target_crs: str = Field(min_length=1)
    scale_m: Literal[30]
    reducer: Literal["median"] = "median"

    @field_validator("target_crs")
    @classmethod
    def target_crs_is_projected_in_metres(cls, value: str) -> str:
        try:
            crs = CRS.from_user_input(value)
        except Exception:
            raise ValueError("target_crs debe ser un CRS reconocible") from None
        if not crs.is_projected:
            raise ValueError("target_crs debe ser proyectado para usar scale_m")
        axis_units = {axis.unit_name.lower() for axis in crs.axis_info if axis.unit_name}
        if not axis_units or not axis_units <= {"metre", "meter"}:
            raise ValueError("target_crs debe usar metros")
        return crs.to_string()

    @model_validator(mode="after")
    def period_is_one_complete_calendar_year(self) -> HlsCompositeRequest:
        expected_end = date(self.start_date.year + 1, 1, 1)
        if (
            self.start_date.month != 1
            or self.start_date.day != 1
            or self.end_date_exclusive != expected_end
        ):
            raise ValueError("el composite requiere un año calendario completo")
        return self

    @property
    def period_days(self) -> int:
        return (self.end_date_exclusive - self.start_date).days


class HlsCompositeInputScene(StrictHlsModel):
    """Escena efectivamente incluida en la colección anual."""

    image_id: str = Field(min_length=1)
    acquired_at: datetime
    cloud_coverage_pct: float = Field(ge=0, le=100)

    @field_validator("acquired_at")
    @classmethod
    def acquired_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acquired_at debe incluir zona horaria")
        return value


class HlsCompositeSourceInventory(StrictHlsModel):
    """Inventario completo, no truncado, de una colección constituyente."""

    source_id: str
    product: str
    collection_id: str
    spatial_filter_strategy: Literal["filterBounds", "MGRS_TILE_ID"]
    mgrs_tile_ids: tuple[str, ...]
    scene_count: int = Field(ge=0)
    scenes: tuple[HlsCompositeInputScene, ...]

    @model_validator(mode="after")
    def count_matches_scene_list(self) -> HlsCompositeSourceInventory:
        if self.scene_count != len(self.scenes):
            raise ValueError("scene_count no coincide con scenes")
        return self


class HlsIndexFormulaRecord(StrictHlsModel):
    """Fórmula versionada junto al producto que la utilizó."""

    index: SpectralIndex
    formula: str


class HlsCompositeMetadata(StrictHlsModel):
    """Procedencia científica de las imágenes calculadas en GEE."""

    generated_at: datetime
    start_date: date
    end_date_exclusive: date
    period_days: int
    reducer: Literal["median"]
    target_crs: str
    scale_m: Literal[30]
    source_native_reflectance_scale_factor: float
    source_native_reflectance_offset: float
    earth_engine_reflectance_multiplier: float
    earth_engine_reflectance_offset: float
    earth_engine_pixel_value_semantics: Literal["surface_reflectance_already_scaled"]
    reflectance_band_names: tuple[str, ...]
    requested_indices: tuple[SpectralIndex, ...]
    index_formulas: tuple[HlsIndexFormulaRecord, ...]
    indices_derived_from: Literal["annual_median_reflectance"]
    masked_fmask_classes: tuple[str, ...]
    water_preserved: Literal[True]
    complete_scene_inventory: Literal[True]
    input_scene_count: int = Field(ge=1)
    sources: tuple[HlsCompositeSourceInventory, ...]
    ndmi_lswi_equivalent: bool
    remote_data_accessed: Literal[True] = True
    metadata_only: Literal[False] = False


@dataclass(frozen=True, slots=True)
class HlsCompositeImages:
    """Imágenes perezosas de GEE más metadatos ya materializados."""

    reflectance: Any = field(repr=False, compare=False)
    indices: Any = field(repr=False, compare=False)
    valid_observation_count: Any = field(repr=False, compare=False)
    valid_observation_count_l30: Any = field(repr=False, compare=False)
    valid_observation_count_s30: Any = field(repr=False, compare=False)
    metadata: HlsCompositeMetadata


def hls_fmask_pixel_is_valid(value: int) -> bool:
    """Decodifica Fmask con la política científica explícita del Paso 10."""
    if not 0 <= value <= 255:
        raise ValueError("Fmask debe estar entre 0 y 255")
    if value == 255:
        return False
    invalid_bits = (1, 2, 3, 4)
    if any(value & (1 << bit) for bit in invalid_bits):
        return False
    aerosol_level = (value >> 6) & 0b11
    return aerosol_level != 0b11


def build_hls_annual_composite(
    *,
    session: GeeSession,
    plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    aoi_wgs84: BaseGeometry,
    request: HlsCompositeRequest,
    generated_at: datetime | None = None,
) -> HlsCompositeImages:
    """Construye mediana anual, índices y conteo usando todas las escenas coincidentes."""
    _validate_composite_inputs(aoi_wgs84, data_config, request, generated_at)
    generation_time = generated_at or datetime.now(UTC)
    module = session.module
    try:
        remote_aoi = module.Geometry(mapping(aoi_wgs84))
        source_by_product = {source.product: source for source in plan.sources}
        l30_source = source_by_product["HLSL30"]
        s30_source = source_by_product["HLSS30"]
        l30_collection = (
            module.ImageCollection(l30_source.collection_id)
            .filterBounds(remote_aoi)
            .filterDate(
                request.start_date.isoformat(),
                request.end_date_exclusive.isoformat(),
            )
            .sort("system:time_start")
        )
        l30_inventory = _complete_inventory(
            l30_collection,
            l30_source,
            spatial_filter_strategy="filterBounds",
        )
        if not l30_inventory.mgrs_tile_ids:
            raise HlsCompositeError("cannot_resolve_mgrs_tiles_without_l30_scenes")
        s30_collection = (
            module.ImageCollection(s30_source.collection_id)
            .filterDate(
                request.start_date.isoformat(),
                request.end_date_exclusive.isoformat(),
            )
            .filter(
                module.Filter.inList(
                    "MGRS_TILE_ID",
                    list(l30_inventory.mgrs_tile_ids),
                )
            )
            .sort("system:time_start")
        )
        s30_inventory = _complete_inventory(
            s30_collection,
            s30_source,
            spatial_filter_strategy="MGRS_TILE_ID",
            mgrs_tile_ids=l30_inventory.mgrs_tile_ids,
        )
        inventories = [l30_inventory, s30_inventory]
        normalized_collections = [
            collection.map(
                lambda image, source=source: _normalize_hls_image(
                    module.Image(image),
                    source=source,
                    data_config=data_config,
                )
            )
            for collection, source in (
                (l30_collection, l30_source),
                (s30_collection, s30_source),
            )
        ]

        input_scene_count = sum(item.scene_count for item in inventories)
        if input_scene_count == 0:
            raise HlsCompositeError("no_scenes_for_period")
        combined = normalized_collections[0]
        for collection in normalized_collections[1:]:
            combined = combined.merge(collection)

        reflectance = (
            combined.select(list(REFLECTANCE_BAND_NAMES))
            .median()
            .rename(list(REFLECTANCE_BAND_NAMES))
            .toFloat()
            .clip(remote_aoi)
        )
        valid_observation_count = (
            combined.select([BandRole.RED.value])
            .count()
            .rename("valid_observation_count")
            .toUint16()
            .clip(remote_aoi)
        )
        valid_observation_count_l30 = _sensor_observation_count(
            collection=normalized_collections[0],
            scene_count=l30_inventory.scene_count,
            combined_collection=combined,
            output_band="valid_observation_count_l30",
            remote_aoi=remote_aoi,
        )
        valid_observation_count_s30 = _sensor_observation_count(
            collection=normalized_collections[1],
            scene_count=s30_inventory.scene_count,
            combined_collection=combined,
            output_band="valid_observation_count_s30",
            remote_aoi=remote_aoi,
        )
        indices = _build_index_image(reflectance, data_config.indices).clip(remote_aoi)
    except HlsCompositeError:
        raise
    except Exception as error:
        raise HlsCompositeError(_remote_error_code(error)) from None

    return HlsCompositeImages(
        reflectance=reflectance,
        indices=indices,
        valid_observation_count=valid_observation_count,
        valid_observation_count_l30=valid_observation_count_l30,
        valid_observation_count_s30=valid_observation_count_s30,
        metadata=HlsCompositeMetadata(
            generated_at=generation_time,
            start_date=request.start_date,
            end_date_exclusive=request.end_date_exclusive,
            period_days=request.period_days,
            reducer=request.reducer,
            target_crs=request.target_crs,
            scale_m=request.scale_m,
            source_native_reflectance_scale_factor=(
                data_config.source_native_reflectance_scale_factor
            ),
            source_native_reflectance_offset=data_config.source_native_reflectance_offset,
            earth_engine_reflectance_multiplier=(data_config.earth_engine_reflectance_multiplier),
            earth_engine_reflectance_offset=data_config.earth_engine_reflectance_offset,
            earth_engine_pixel_value_semantics="surface_reflectance_already_scaled",
            reflectance_band_names=REFLECTANCE_BAND_NAMES,
            requested_indices=data_config.indices,
            index_formulas=tuple(
                HlsIndexFormulaRecord(index=index, formula=INDEX_FORMULAS[index])
                for index in data_config.indices
            ),
            indices_derived_from="annual_median_reflectance",
            masked_fmask_classes=MASKED_FMASK_CLASSES,
            water_preserved=True,
            complete_scene_inventory=True,
            input_scene_count=input_scene_count,
            sources=tuple(inventories),
            ndmi_lswi_equivalent=(
                SpectralIndex.NDMI in data_config.indices
                and SpectralIndex.LSWI in data_config.indices
            ),
        ),
    )


def _sensor_observation_count(
    *,
    collection: Any,
    scene_count: int,
    combined_collection: Any,
    output_band: str,
    remote_aoi: Any,
) -> Any:
    """Conserva una banda cero explícita cuando un sensor no aportó escenas."""
    source = collection if scene_count > 0 else combined_collection
    count = source.select([BandRole.RED.value]).count()
    if scene_count == 0:
        count = count.multiply(0)
    return count.rename(output_band).toUint16().clip(remote_aoi)


def _validate_composite_inputs(
    aoi_wgs84: BaseGeometry,
    data_config: DataConfig,
    request: HlsCompositeRequest,
    generated_at: datetime | None,
) -> None:
    if aoi_wgs84.geom_type not in {"Polygon", "MultiPolygon"}:
        raise HlsCompositeError("aoi_must_be_polygonal")
    if aoi_wgs84.is_empty or not aoi_wgs84.is_valid:
        raise HlsCompositeError("aoi_invalid")
    min_lon, min_lat, max_lon, max_lat = aoi_wgs84.bounds
    if not (-180 <= min_lon <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
        raise HlsCompositeError("aoi_not_wgs84")
    if data_config.composition_interval != "annual":
        raise HlsCompositeError("configuration_is_not_annual")
    if data_config.composition_reducer != request.reducer:
        raise HlsCompositeError("reducer_mismatch")
    if data_config.target_resolution_m != request.scale_m:
        raise HlsCompositeError("scale_mismatch")
    if generated_at is not None and (
        generated_at.tzinfo is None or generated_at.utcoffset() is None
    ):
        raise HlsCompositeError("generated_at_requires_timezone")


def _complete_inventory(
    collection: Any,
    source: CatalogSource,
    *,
    spatial_filter_strategy: Literal["filterBounds", "MGRS_TILE_ID"],
    mgrs_tile_ids: tuple[str, ...] | None = None,
) -> HlsCompositeSourceInventory:
    count = _non_negative_integer(collection.size().getInfo())
    identifiers = _list_value(collection.aggregate_array("system:index").getInfo())
    timestamps = _list_value(collection.aggregate_array("system:time_start").getInfo())
    cloud_coverages = _list_value(collection.aggregate_array("CLOUD_COVERAGE").getInfo())
    if not (count == len(identifiers) == len(timestamps) == len(cloud_coverages)):
        raise HlsCompositeError(f"{source.product.lower()}_inventory_misaligned")
    scenes = tuple(
        HlsCompositeInputScene(
            image_id=_non_empty_string(identifier),
            acquired_at=_timestamp_milliseconds(timestamp),
            cloud_coverage_pct=_percentage(cloud_coverage),
        )
        for identifier, timestamp, cloud_coverage in zip(
            identifiers,
            timestamps,
            cloud_coverages,
            strict=True,
        )
    )
    resolved_tile_ids = (
        mgrs_tile_ids
        if mgrs_tile_ids is not None
        else _tile_ids_or_composite_error(scene.image_id for scene in scenes)
    )
    return HlsCompositeSourceInventory(
        source_id=source.source_id,
        product=source.product,
        collection_id=source.collection_id,
        spatial_filter_strategy=spatial_filter_strategy,
        mgrs_tile_ids=resolved_tile_ids,
        scene_count=count,
        scenes=scenes,
    )


def _normalize_hls_image(
    image: Any,
    *,
    source: CatalogSource,
    data_config: DataConfig,
) -> Any:
    source_bands = [source.band_map[role] for role in REFLECTANCE_BAND_ROLES]
    reflectance = (
        image.select(source_bands, list(REFLECTANCE_BAND_NAMES))
        .multiply(data_config.earth_engine_reflectance_multiplier)
        .add(data_config.earth_engine_reflectance_offset)
        .toFloat()
    )
    qa = image.select(source.band_map[BandRole.QA])
    valid = qa.neq(255)
    for bit in (
        source.qa_mask_bits.cloud,
        source.qa_mask_bits.adjacent_to_cloud_or_shadow,
        source.qa_mask_bits.cloud_shadow,
        source.qa_mask_bits.snow_or_ice,
    ):
        valid = valid.And(qa.bitwiseAnd(1 << bit).eq(0))
    if data_config.mask_high_aerosol:
        aerosol_level = qa.rightShift(source.qa_mask_bits.aerosol_first_bit).bitwiseAnd(0b11)
        valid = valid.And(aerosol_level.neq(0b11))
    return reflectance.updateMask(valid).copyProperties(
        image,
        ["system:index", "system:time_start", "CLOUD_COVERAGE"],
    )


def _build_index_image(reflectance: Any, requested: tuple[SpectralIndex, ...]) -> Any:
    red = reflectance.select(BandRole.RED.value)
    nir = reflectance.select(BandRole.NIR.value)
    swir1 = reflectance.select(BandRole.SWIR1.value)
    swir2 = reflectance.select(BandRole.SWIR2.value)

    ndvi = _safe_ee_ratio(nir.subtract(red), nir.add(red))
    ndmi = _safe_ee_ratio(nir.subtract(swir1), nir.add(swir1))
    swir_difference = swir1.subtract(swir2)
    values = {
        SpectralIndex.NDVI: ndvi,
        SpectralIndex.EVI2: _safe_ee_ratio(
            nir.subtract(red).multiply(2.5),
            nir.add(red.multiply(2.4)).add(1),
        ),
        SpectralIndex.NBR: _safe_ee_ratio(nir.subtract(swir2), nir.add(swir2)),
        SpectralIndex.NDMI: ndmi,
        SpectralIndex.NMDI: _safe_ee_ratio(
            nir.subtract(swir_difference),
            nir.add(swir_difference),
        ),
        SpectralIndex.LSWI: ndmi,
        SpectralIndex.NIRV: ndvi.multiply(nir),
        SpectralIndex.KNDVI: ndvi.pow(2).tanh(),
    }
    selected = [values[index].rename(index.value) for index in requested]
    result = selected[0]
    for image in selected[1:]:
        result = result.addBands(image)
    return result.toFloat()


def _safe_ee_ratio(numerator: Any, denominator: Any) -> Any:
    return numerator.divide(denominator).updateMask(denominator.abs().gt(DENOMINATOR_EPSILON))


def _list_value(value: object) -> list[object]:
    if not isinstance(value, list):
        raise HlsCompositeError("inventory_not_a_list")
    return value


def _non_negative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HlsCompositeError("inventory_count_invalid")
    return value


def _non_empty_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise HlsCompositeError("scene_identifier_invalid")
    return value


def _timestamp_milliseconds(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HlsCompositeError("scene_timestamp_invalid")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _percentage(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HlsCompositeError("scene_cloud_coverage_invalid")
    percentage = float(value)
    if not 0 <= percentage <= 100:
        raise HlsCompositeError("scene_cloud_coverage_invalid")
    return percentage


def _tile_ids_or_composite_error(identifiers: Iterable[str]) -> tuple[str, ...]:
    try:
        return mgrs_tile_ids_from_hls_identifiers(identifiers)
    except ValueError:
        raise HlsCompositeError("scene_identifier_missing_mgrs_tile") from None


def _remote_error_code(error: Exception) -> str:
    message = str(error).lower()
    if "permission" in message or "forbidden" in message:
        return "permission_denied"
    if "quota" in message or "rate" in message:
        return "quota_or_rate_limit"
    if "timeout" in message or "deadline" in message:
        return "timeout"
    return "service_error"
