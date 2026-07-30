"""Construcción remota, explícita y auditable de composites temporales HLS."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from time import sleep
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
METADATA_GETINFO_MAX_ATTEMPTS = 2
METADATA_RETRY_BACKOFF_SECONDS = 0.1
_metadata_retry_sleep: Callable[[float], None] = sleep
_INVENTORY_OPERATION_CODES = {
    "size": "size",
    "system:index": "system_index",
    "system:time_start": "system_time_start",
    "CLOUD_COVERAGE": "cloud_coverage",
}
_TRANSIENT_METADATA_ERROR_CATEGORIES = frozenset(
    {
        "timeout",
        "quota_or_rate_limit",
        "service_unavailable",
        "connection_error",
    }
)
_COMPOSITE_BUILD_STAGES = frozenset(
    {
        "remote_aoi",
        "source_lookup",
        "l30_collection",
        "l30_inventory",
        "s30_collection",
        "s30_inventory",
        "normalize_l30",
        "normalize_s30",
        "merge",
        "reflectance",
        "total_count",
        "l30_count",
        "s30_count",
        "indices",
    }
)
_DENSE_COLLECTION_BUILD_STAGES = frozenset(
    {
        "remote_aoi",
        "source_collection",
        "normalize_observations",
    }
)


class HlsCompositeError(RuntimeError):
    """Fallo sanitizado de preparación o consulta del composite."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Composite HLS fallido: {code}")


class StrictHlsModel(BaseModel):
    """Base inmutable para el contrato remoto del Paso 10."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HlsTemporalCompositeRequest(StrictHlsModel):
    """Solicitud genérica con límites temporales inequívocos."""

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
    def period_is_non_empty(self) -> HlsTemporalCompositeRequest:
        if self.start_date >= self.end_date_exclusive:
            raise ValueError("start_date debe ser anterior a end_date_exclusive")
        if (self.end_date_exclusive - self.start_date).days > 366:
            raise ValueError("el intervalo no puede superar 366 días")
        return self

    @property
    def period_days(self) -> int:
        return (self.end_date_exclusive - self.start_date).days


class HlsCompositeRequest(HlsTemporalCompositeRequest):
    """Solicitud anual conservada como contrato compatible del Paso 10."""

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
    indices_derived_from: Literal[
        "annual_median_reflectance",
        "period_median_reflectance",
    ]
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


@dataclass(frozen=True, slots=True)
class HlsDenseObservationCollection:
    """Colección perezosa por escena, sin reducir el eje temporal."""

    collection: Any = field(repr=False, compare=False)
    source_product: Literal["HLSL30", "HLSS30"]
    collection_id: str
    start_date: date
    end_date_exclusive: date
    input_temporal_granularity: Literal["dense_masked_observations"]
    qa_policy: Literal["hls_fmask_preapplied"]
    reflectance_band_names: tuple[str, ...]
    index_band_names: tuple[str, ...]


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


def build_masked_hls_dense_collection(
    *,
    session: GeeSession,
    source: CatalogSource,
    data_config: DataConfig,
    aoi_wgs84: BaseGeometry,
    start_date: date,
    end_date_exclusive: date,
    requested_indices: tuple[str, ...],
) -> HlsDenseObservationCollection:
    """Prepara observaciones HLS enmascaradas sin componer ni materializar."""
    if start_date >= end_date_exclusive:
        raise HlsCompositeError("dense_period_is_empty")
    if not requested_indices or len(requested_indices) != len(set(requested_indices)):
        raise HlsCompositeError("dense_indices_invalid")
    try:
        normalized_indices = tuple(SpectralIndex(index) for index in requested_indices)
    except ValueError:
        raise HlsCompositeError("dense_indices_unknown") from None
    if not set(normalized_indices).issubset(set(data_config.indices)):
        raise HlsCompositeError("dense_indices_not_configured")
    _validate_hls_aoi(aoi_wgs84)

    module = session.module
    interval_code = f"dense_{start_date:%Y%m%d}_{end_date_exclusive:%Y%m%d}"
    remote_aoi = _run_dense_collection_stage(
        interval_code=interval_code,
        stage="remote_aoi",
        operation=lambda: module.Geometry(mapping(aoi_wgs84)),
    )
    source_collection = _run_dense_collection_stage(
        interval_code=interval_code,
        stage="source_collection",
        operation=lambda: (
            module.ImageCollection(source.collection_id)
            .filterBounds(remote_aoi)
            .filterDate(start_date.isoformat(), end_date_exclusive.isoformat())
            .sort("system:time_start")
        ),
    )
    dense_collection = _run_dense_collection_stage(
        interval_code=interval_code,
        stage="normalize_observations",
        operation=lambda: source_collection.map(
            lambda image: _normalize_hls_image_with_indices(
                image=module.Image(image),
                source=source,
                data_config=data_config,
                requested_indices=normalized_indices,
                remote_aoi=remote_aoi,
            )
        ),
    )

    return HlsDenseObservationCollection(
        collection=dense_collection,
        source_product=source.product,
        collection_id=source.collection_id,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        input_temporal_granularity="dense_masked_observations",
        qa_policy="hls_fmask_preapplied",
        reflectance_band_names=REFLECTANCE_BAND_NAMES,
        index_band_names=tuple(index.value for index in normalized_indices),
    )


def _run_dense_collection_stage(
    *,
    interval_code: str,
    stage: str,
    operation: Callable[[], Any],
) -> Any:
    """Conserva intervalo y etapa densa sin propagar mensajes remotos."""
    safe_stage = stage if stage in _DENSE_COLLECTION_BUILD_STAGES else "unknown_stage"
    prefix = f"{interval_code}_{safe_stage}_"
    try:
        return operation()
    except HlsCompositeError as error:
        if error.code.startswith(prefix):
            raise
        raise HlsCompositeError(f"{prefix}{error.code}") from None
    except Exception as error:
        raise HlsCompositeError(f"{prefix}{_remote_error_code(error)}") from None


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
    if data_config.composition_interval != "annual":
        raise HlsCompositeError("configuration_is_not_annual")
    return _build_hls_composite(
        session=session,
        plan=plan,
        data_config=data_config,
        aoi_wgs84=aoi_wgs84,
        request=request,
        indices_derived_from="annual_median_reflectance",
        period_code=str(request.start_date.year),
        generated_at=generated_at,
    )


def build_hls_composite(
    *,
    session: GeeSession,
    plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    aoi_wgs84: BaseGeometry,
    request: HlsTemporalCompositeRequest,
    period_id: str | None = None,
    generated_at: datetime | None = None,
) -> HlsCompositeImages:
    """Construye un composite para cualquier intervalo explícito no vacío."""
    return _build_hls_composite(
        session=session,
        plan=plan,
        data_config=data_config,
        aoi_wgs84=aoi_wgs84,
        request=request,
        indices_derived_from="period_median_reflectance",
        period_code=_period_error_code(request=request, period_id=period_id),
        generated_at=generated_at,
    )


def _build_hls_composite(
    *,
    session: GeeSession,
    plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    aoi_wgs84: BaseGeometry,
    request: HlsTemporalCompositeRequest,
    indices_derived_from: Literal[
        "annual_median_reflectance",
        "period_median_reflectance",
    ],
    period_code: str,
    generated_at: datetime | None,
) -> HlsCompositeImages:
    _validate_composite_inputs(aoi_wgs84, data_config, request, generated_at)
    generation_time = generated_at or datetime.now(UTC)
    module = session.module

    remote_aoi = _run_composite_stage(
        period_code=period_code,
        stage="remote_aoi",
        operation=lambda: module.Geometry(mapping(aoi_wgs84)),
    )

    def lookup_sources() -> tuple[CatalogSource, CatalogSource]:
        source_by_product = {source.product: source for source in plan.sources}
        return source_by_product["HLSL30"], source_by_product["HLSS30"]

    l30_source, s30_source = _run_composite_stage(
        period_code=period_code,
        stage="source_lookup",
        operation=lookup_sources,
    )
    l30_collection = _run_composite_stage(
        period_code=period_code,
        stage="l30_collection",
        operation=lambda: (
            module.ImageCollection(l30_source.collection_id)
            .filterBounds(remote_aoi)
            .filterDate(
                request.start_date.isoformat(),
                request.end_date_exclusive.isoformat(),
            )
            .sort("system:time_start")
        ),
    )

    def build_l30_inventory() -> HlsCompositeSourceInventory:
        inventory = _complete_inventory(
            l30_collection, l30_source, spatial_filter_strategy="filterBounds"
        )
        if not inventory.mgrs_tile_ids:
            raise HlsCompositeError("cannot_resolve_mgrs_tiles_without_l30_scenes")
        return inventory

    l30_inventory = _run_composite_stage(
        period_code=period_code,
        stage="l30_inventory",
        operation=build_l30_inventory,
    )
    s30_collection = _run_composite_stage(
        period_code=period_code,
        stage="s30_collection",
        operation=lambda: (
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
        ),
    )
    s30_inventory = _run_composite_stage(
        period_code=period_code,
        stage="s30_inventory",
        operation=lambda: _complete_inventory(
            s30_collection,
            s30_source,
            spatial_filter_strategy="MGRS_TILE_ID",
            mgrs_tile_ids=l30_inventory.mgrs_tile_ids,
        ),
    )
    inventories = [l30_inventory, s30_inventory]
    normalized_l30 = _run_composite_stage(
        period_code=period_code,
        stage="normalize_l30",
        operation=lambda: l30_collection.map(
            lambda image: _normalize_hls_image(
                module.Image(image),
                source=l30_source,
                data_config=data_config,
            )
        ),
    )
    normalized_s30 = _run_composite_stage(
        period_code=period_code,
        stage="normalize_s30",
        operation=lambda: s30_collection.map(
            lambda image: _normalize_hls_image(
                module.Image(image),
                source=s30_source,
                data_config=data_config,
            )
        ),
    )

    def merge_collections() -> tuple[int, Any]:
        input_count = sum(item.scene_count for item in inventories)
        if input_count == 0:
            raise HlsCompositeError("no_scenes_for_period")
        return input_count, normalized_l30.merge(normalized_s30)

    input_scene_count, combined = _run_composite_stage(
        period_code=period_code,
        stage="merge",
        operation=merge_collections,
    )
    reflectance = _run_composite_stage(
        period_code=period_code,
        stage="reflectance",
        operation=lambda: (
            combined.select(list(REFLECTANCE_BAND_NAMES))
            .median()
            .rename(list(REFLECTANCE_BAND_NAMES))
            .toFloat()
            .clip(remote_aoi)
        ),
    )
    valid_observation_count = _run_composite_stage(
        period_code=period_code,
        stage="total_count",
        operation=lambda: (
            combined.select([BandRole.RED.value])
            .count()
            .unmask(0, False)
            .rename("valid_observation_count")
            .toUint16()
            .clip(remote_aoi)
        ),
    )
    valid_observation_count_l30 = _run_composite_stage(
        period_code=period_code,
        stage="l30_count",
        operation=lambda: _sensor_observation_count(
            collection=normalized_l30,
            scene_count=l30_inventory.scene_count,
            combined_collection=combined,
            output_band="valid_observation_count_l30",
            remote_aoi=remote_aoi,
        ),
    )
    valid_observation_count_s30 = _run_composite_stage(
        period_code=period_code,
        stage="s30_count",
        operation=lambda: _sensor_observation_count(
            collection=normalized_s30,
            scene_count=s30_inventory.scene_count,
            combined_collection=combined,
            output_band="valid_observation_count_s30",
            remote_aoi=remote_aoi,
        ),
    )
    indices = _run_composite_stage(
        period_code=period_code,
        stage="indices",
        operation=lambda: _build_index_image(reflectance, data_config.indices).clip(remote_aoi),
    )

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
            indices_derived_from=indices_derived_from,
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


def _period_error_code(
    *,
    request: HlsTemporalCompositeRequest,
    period_id: str | None,
) -> str:
    """Acepta IDs temporales internos conocidos sin reflejar texto arbitrario."""
    if period_id is not None and re.fullmatch(
        r"\d{4}-(?:DJF|MAM|JJA|SON)",
        period_id,
        flags=re.IGNORECASE,
    ):
        return period_id.casefold().replace("-", "_")
    return f"{request.start_date:%Y%m%d}_{request.end_date_exclusive:%Y%m%d}"


def _run_composite_stage(
    *,
    period_code: str,
    stage: str,
    operation: Callable[[], Any],
) -> Any:
    """Ejecuta una etapa una vez y conserva contexto estable, nunca texto remoto."""
    safe_stage = stage if stage in _COMPOSITE_BUILD_STAGES else "unknown_stage"
    prefix = f"{period_code}_{safe_stage}_"
    try:
        return operation()
    except HlsCompositeError as error:
        if error.code.startswith(prefix):
            raise
        raise HlsCompositeError(f"{prefix}{error.code}") from None
    except Exception as error:
        raise HlsCompositeError(f"{prefix}{_metadata_error_category(error)}") from None


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
    return count.unmask(0, False).rename(output_band).toUint16().clip(remote_aoi)


def _validate_composite_inputs(
    aoi_wgs84: BaseGeometry,
    data_config: DataConfig,
    request: HlsTemporalCompositeRequest,
    generated_at: datetime | None,
) -> None:
    _validate_hls_aoi(aoi_wgs84)
    if data_config.composition_reducer != request.reducer:
        raise HlsCompositeError("reducer_mismatch")
    if data_config.target_resolution_m != request.scale_m:
        raise HlsCompositeError("scale_mismatch")
    if generated_at is not None and (
        generated_at.tzinfo is None or generated_at.utcoffset() is None
    ):
        raise HlsCompositeError("generated_at_requires_timezone")


def _validate_hls_aoi(aoi_wgs84: BaseGeometry) -> None:
    if aoi_wgs84.geom_type not in {"Polygon", "MultiPolygon"}:
        raise HlsCompositeError("aoi_must_be_polygonal")
    if aoi_wgs84.is_empty or not aoi_wgs84.is_valid:
        raise HlsCompositeError("aoi_invalid")
    min_lon, min_lat, max_lon, max_lat = aoi_wgs84.bounds
    if not (-180 <= min_lon <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
        raise HlsCompositeError("aoi_not_wgs84")


def _complete_inventory(
    collection: Any,
    source: CatalogSource,
    *,
    spatial_filter_strategy: Literal["filterBounds", "MGRS_TILE_ID"],
    mgrs_tile_ids: tuple[str, ...] | None = None,
) -> HlsCompositeSourceInventory:
    count = _non_negative_integer(
        _inventory_get_info(
            collection.size(),
            product=source.product,
            operation="size",
        )
    )
    identifiers = _list_value(
        _inventory_get_info(
            collection.aggregate_array("system:index"),
            product=source.product,
            operation="system:index",
        )
    )
    timestamps = _list_value(
        _inventory_get_info(
            collection.aggregate_array("system:time_start"),
            product=source.product,
            operation="system:time_start",
        )
    )
    cloud_coverages = _list_value(
        _inventory_get_info(
            collection.aggregate_array("CLOUD_COVERAGE"),
            product=source.product,
            operation="CLOUD_COVERAGE",
        )
    )
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


def _inventory_get_info(
    remote_value: Any,
    *,
    product: Literal["HLSL30", "HLSS30"],
    operation: Literal[
        "size",
        "system:index",
        "system:time_start",
        "CLOUD_COVERAGE",
    ],
) -> object:
    """Obtiene un campo idempotente con un único reintento transitorio seguro."""
    for attempt in range(METADATA_GETINFO_MAX_ATTEMPTS):
        try:
            return remote_value.getInfo()
        except Exception as error:
            category = _metadata_error_category(error)
            can_retry = (
                category in _TRANSIENT_METADATA_ERROR_CATEGORIES
                and attempt + 1 < METADATA_GETINFO_MAX_ATTEMPTS
            )
            if can_retry:
                _metadata_retry_sleep(METADATA_RETRY_BACKOFF_SECONDS)
                continue
            operation_code = _INVENTORY_OPERATION_CODES[operation]
            raise HlsCompositeError(
                f"{product.lower()}_inventory_{operation_code}_{category}"
            ) from None
    raise AssertionError("metadata retry loop must always return or raise")


def _metadata_error_category(error: Exception) -> str:
    """Clasifica sin propagar el texto remoto, URLs, tokens ni IDs de proyecto."""
    message = str(error).casefold()
    if any(
        marker in message
        for marker in (
            "unauthenticated",
            "authentication",
            "invalid credential",
            "invalid token",
            "expired token",
        )
    ) or re.search(r"\b401\b", message):
        return "authentication_failed"
    if any(marker in message for marker in ("permission", "forbidden", "access denied")) or (
        re.search(r"\b403\b", message)
    ):
        return "permission_denied"
    if "memory limit" in message or "out of memory" in message:
        return "memory_limit"
    if any(
        marker in message for marker in ("invalid argument", "bad request", "malformed request")
    ) or re.search(r"\b400\b", message):
        return "invalid_argument"
    if "timeout" in message or "timed out" in message or "deadline" in message:
        return "timeout"
    if any(
        marker in message for marker in ("quota", "rate limit", "rate-limit", "too many requests")
    ) or re.search(r"\b429\b", message):
        return "quota_or_rate_limit"
    if any(
        marker in message
        for marker in (
            "connection reset",
            "connection aborted",
            "connection error",
            "transport error",
            "transfer error",
            "broken pipe",
            "remote end closed",
        )
    ):
        return "connection_error"
    if any(
        marker in message
        for marker in (
            "internal error",
            "backend error",
            "backend unavailable",
            "service unavailable",
            "temporarily unavailable",
            "server error",
        )
    ) or re.search(r"\b5\d{2}\b", message):
        return "service_unavailable"
    return "service_error"


def _normalize_hls_image(
    image: Any,
    *,
    source: CatalogSource,
    data_config: DataConfig,
) -> Any:
    return _masked_hls_reflectance(
        image,
        source=source,
        data_config=data_config,
    ).copyProperties(
        image,
        ["system:index", "system:time_start", "CLOUD_COVERAGE"],
    )


def _masked_hls_reflectance(
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
    return reflectance.updateMask(valid)


def _normalize_hls_image_with_indices(
    *,
    image: Any,
    source: CatalogSource,
    data_config: DataConfig,
    requested_indices: tuple[SpectralIndex, ...],
    remote_aoi: Any,
) -> Any:
    """Reutiliza una única aplicación de Fmask para reflectancias e índices."""
    reflectance = _masked_hls_reflectance(
        image,
        source=source,
        data_config=data_config,
    )
    return (
        reflectance.addBands(_build_index_image(reflectance, requested_indices))
        .clip(remote_aoi)
        .copyProperties(
            image,
            ["system:index", "system:time_start", "CLOUD_COVERAGE"],
        )
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
