"""Orquestación de composites HLS para las ventanas estacionales del Paso 12.2."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.artifact_layout import (
    published_raster_path,
    seasonal_json,
    seasonal_tiff,
)
from deforestation_pipeline.catalog import BenchmarkSourcePlan
from deforestation_pipeline.config import DataConfig, OutputConfig, SpectralIndex
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import (
    HlsCompositeImages,
    HlsTemporalCompositeRequest,
    build_hls_composite,
)
from deforestation_pipeline.raster_products import (
    DirectDownloadEstimate,
    FetchBytes,
    HlsRasterMaterialization,
    estimate_fixed_grid_download,
    materialize_hls_raster_products,
    validate_direct_download,
)
from deforestation_pipeline.schemas import RasterGridSpec
from deforestation_pipeline.seasonal_qa import (
    SeasonalQaInput,
    SeasonalQaResult,
    analyze_seasonal_rasters,
)
from deforestation_pipeline.temporal_cube import (
    SeasonCode,
    TemporalCubeSpec,
    TemporalWindow,
    TemporalWindowPlan,
    build_meteorological_south_window_plan,
    build_temporal_cube_spec,
)

HLS_SEASONAL_SCHEMA_VERSION: Literal["2.0.0"] = "2.0.0"
HLS_SEASONAL_METADATA_SCHEMA_ID = "urn:deforestation-pipeline:hls-seasonal-metadata:2.0.0"
TEMPORAL_CUBE_INDEX_SCHEMA_ID = "urn:deforestation-pipeline:temporal-cube-index:2.0.0"
HLS_SEASONAL_MINIMUM_YEAR = 2015


class HlsSeasonalError(RuntimeError):
    """Fallo sanitizado al preparar la colección estacional."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Serie estacional HLS fallida: {code}")


class StrictSeasonalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HlsSeasonalRequest(StrictSeasonalModel):
    """Rango inclusivo de años de temporada cerrados."""

    start_year: int = Field(ge=HLS_SEASONAL_MINIMUM_YEAR)
    end_year: int = Field(ge=HLS_SEASONAL_MINIMUM_YEAR)

    @model_validator(mode="after")
    def years_are_ordered(self) -> HlsSeasonalRequest:
        if self.start_year > self.end_year:
            raise ValueError("start_year no puede ser posterior a end_year")
        return self

    def build_window_plan(self, *, generated_at: datetime) -> TemporalWindowPlan:
        return build_meteorological_south_window_plan(
            start_year=self.start_year,
            end_year=self.end_year,
            generated_at=generated_at,
        )


class HlsSeasonalPeriodProductRecord(StrictSeasonalModel):
    period_id: str = Field(pattern=r"^\d{4}-(DJF|MAM|JJA|SON)$")
    season_year: int
    season: SeasonCode
    start_date: date
    end_date_exclusive: date
    composite_metadata_path: str
    composite_input_inventory_path: str
    reflectance_raster_path: str
    reflectance_raster_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    indices_raster_path: str
    indices_raster_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    valid_observation_count_raster_path: str
    valid_observation_count_raster_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    l30_observation_count_raster_path: str
    l30_observation_count_raster_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    s30_observation_count_raster_path: str
    s30_observation_count_raster_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class HlsSeasonalMetadata(StrictSeasonalModel):
    schema_version: Literal["2.0.0"] = HLS_SEASONAL_SCHEMA_VERSION
    generated_at: datetime
    start_year: int
    end_year: int
    period_ids: tuple[str, ...]
    window_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    grid_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_plan_path: Literal["json/temporal/seasonal/window_plan.json"] = (
        "json/temporal/seasonal/window_plan.json"
    )
    cube_spec_path: Literal["json/temporal/seasonal/cube_spec.json"] = (
        "json/temporal/seasonal/cube_spec.json"
    )
    cube_index_path: Literal["json/temporal/seasonal/cube_index.json"] = (
        "json/temporal/seasonal/cube_index.json"
    )
    source_products: tuple[Literal["HLSL30"], Literal["HLSS30"]]
    composite_method: Literal["median_reflectance_then_indices"]
    total_estimated_uncompressed_bytes: int = Field(gt=0)
    period_products: tuple[HlsSeasonalPeriodProductRecord, ...]
    stage: Literal["seasonal_rasters_materialized"] = "seasonal_rasters_materialized"
    final_assessment_generated: Literal[False] = False

    @model_validator(mode="after")
    def metadata_is_consistent(self) -> HlsSeasonalMetadata:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        if tuple(item.period_id for item in self.period_products) != self.period_ids:
            raise ValueError("period_products debe coincidir con period_ids")
        expected_count = (self.end_year - self.start_year + 1) * 4
        if len(self.period_ids) != expected_count or len(set(self.period_ids)) != expected_count:
            raise ValueError("period_ids debe contener cuatro estaciones únicas por año")
        return self


class TemporalCubeIndex(StrictSeasonalModel):
    """Índice físico virtual: dimensiones lógicas sobre GeoTIFF planos."""

    schema_version: Literal["2.0.0"] = HLS_SEASONAL_SCHEMA_VERSION
    storage_model: Literal["virtual_geotiff_collection"] = "virtual_geotiff_collection"
    cube_spec_path: Literal["json/temporal/seasonal/cube_spec.json"] = (
        "json/temporal/seasonal/cube_spec.json"
    )
    cube_spec_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    grid_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    period_axis: tuple[str, ...]
    reflectance_band_axis: tuple[str, ...]
    spectral_index_axis: tuple[SpectralIndex, ...]
    sensor_axis: tuple[Literal["total"], Literal["HLSL30"], Literal["HLSS30"]]
    reflectance_dimensions: tuple[
        Literal["time"],
        Literal["reflectance_band"],
        Literal["y"],
        Literal["x"],
    ]
    spectral_index_dimensions: tuple[
        Literal["time"],
        Literal["index"],
        Literal["y"],
        Literal["x"],
    ]
    observation_count_dimensions: tuple[
        Literal["time"],
        Literal["sensor"],
        Literal["y"],
        Literal["x"],
    ]
    period_products: tuple[HlsSeasonalPeriodProductRecord, ...]
    missing_data_policy: Literal["preserve_missing_no_interpolation"]
    interpolation_allowed: Literal[False]
    cube_index_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def index_is_consistent(self) -> TemporalCubeIndex:
        if tuple(item.period_id for item in self.period_products) != self.period_axis:
            raise ValueError("period_products debe coincidir con period_axis")
        expected = _cube_index_sha256(
            cube_spec_sha256=self.cube_spec_sha256,
            grid_sha256=self.grid_sha256,
            period_axis=self.period_axis,
            period_products=self.period_products,
        )
        if self.cube_index_sha256 != expected:
            raise ValueError("cube_index_sha256 no coincide")
        return self


@dataclass(frozen=True, slots=True)
class HlsSeasonalCompositeItem:
    """Composite remoto asociado inequívocamente a una ventana."""

    window: TemporalWindow
    composite: HlsCompositeImages = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class HlsSeasonalCompositeSeries:
    """Colección perezosa previa a descargar o persistir el cubo."""

    cube_spec: TemporalCubeSpec
    items: tuple[HlsSeasonalCompositeItem, ...] = field(repr=False, compare=False)
    stage: str = "seasonal_composites_ready"
    final_assessment_generated: bool = False


@dataclass(frozen=True, slots=True)
class HlsSeasonalRasterItem:
    window: TemporalWindow
    composite: HlsCompositeImages = field(repr=False, compare=False)
    materialization: HlsRasterMaterialization = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class HlsSeasonalMaterialization:
    files: Mapping[str, bytes]
    metadata: HlsSeasonalMetadata
    cube_index: TemporalCubeIndex
    cube_spec: TemporalCubeSpec
    qa: SeasonalQaResult
    raster_items: tuple[HlsSeasonalRasterItem, ...] = field(repr=False, compare=False)
    grid_spec: RasterGridSpec
    estimates: tuple[DirectDownloadEstimate, ...]


def build_hls_seasonal_composites(
    *,
    session: GeeSession,
    source_plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    aoi_wgs84: BaseGeometry,
    grid_spec: RasterGridSpec,
    window_plan: TemporalWindowPlan,
    generated_at: datetime | None = None,
) -> HlsSeasonalCompositeSeries:
    """Construye una imagen perezosa por ventana, sin descargar ni interpolar."""
    generation_time = generated_at or datetime.now(UTC)
    _validate_inputs(
        data_config=data_config,
        grid_spec=grid_spec,
        window_plan=window_plan,
        generated_at=generation_time,
    )
    cube_spec = build_temporal_cube_spec(
        grid_spec=grid_spec,
        window_plan=window_plan,
        spectral_indices=data_config.indices,
        created_at=generation_time,
    )
    items: list[HlsSeasonalCompositeItem] = []
    for window in window_plan.windows:
        composite = build_hls_composite(
            session=session,
            plan=source_plan,
            data_config=data_config,
            aoi_wgs84=aoi_wgs84,
            request=HlsTemporalCompositeRequest(
                start_date=window.start_date,
                end_date_exclusive=window.end_date_exclusive,
                target_crs=grid_spec.target_crs,
                scale_m=data_config.target_resolution_m,
            ),
            period_id=window.period_id,
            generated_at=generation_time,
        )
        if (
            composite.metadata.start_date != window.start_date
            or composite.metadata.end_date_exclusive != window.end_date_exclusive
        ):
            raise HlsSeasonalError("composite_window_mismatch")
        items.append(HlsSeasonalCompositeItem(window=window, composite=composite))
    if tuple(item.window for item in items) != window_plan.windows:
        raise HlsSeasonalError("seasonal_window_order_mismatch")
    return HlsSeasonalCompositeSeries(cube_spec=cube_spec, items=tuple(items))


def materialize_hls_seasonal_series(
    *,
    session: GeeSession,
    source_plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    output_config: OutputConfig,
    aoi_wgs84: BaseGeometry,
    grid_spec: RasterGridSpec,
    request: HlsSeasonalRequest,
    generated_at: datetime | None = None,
    fetch_bytes: FetchBytes | None = None,
) -> HlsSeasonalMaterialization:
    """Descarga cada estación y publica un cubo virtual sin duplicar rasters."""
    generation_time = generated_at or datetime.now(UTC)
    window_plan = request.build_window_plan(generated_at=generation_time)
    _validate_inputs(
        data_config=data_config,
        grid_spec=grid_spec,
        window_plan=window_plan,
        generated_at=generation_time,
    )
    if not math.isclose(
        grid_spec.nodata,
        output_config.raster_nodata,
        abs_tol=1e-9,
    ):
        raise HlsSeasonalError("grid_nodata_mismatch")
    per_period_estimates = _period_download_estimates(
        grid_spec=grid_spec,
        output_config=output_config,
        index_count=len(data_config.indices),
    )
    total_estimated_bytes = sum(
        estimate.estimated_uncompressed_bytes for estimate in per_period_estimates
    ) * len(window_plan.windows)
    if total_estimated_bytes > output_config.maximum_series_download_bytes:
        raise HlsSeasonalError("seasonal_download_budget_exceeded")

    seasonal = build_hls_seasonal_composites(
        session=session,
        source_plan=source_plan,
        data_config=data_config,
        aoi_wgs84=aoi_wgs84,
        grid_spec=grid_spec,
        window_plan=window_plan,
        generated_at=generation_time,
    )
    files: dict[str, bytes] = {}
    raster_items: list[HlsSeasonalRasterItem] = []
    records: list[HlsSeasonalPeriodProductRecord] = []
    qa_inputs: list[SeasonalQaInput] = []
    all_estimates: list[DirectDownloadEstimate] = []
    for item in seasonal.items:
        materialization = materialize_hls_raster_products(
            product=item.composite,
            aoi_wgs84=aoi_wgs84,
            output_config=output_config,
            grid_spec=grid_spec,
            fetch_bytes=fetch_bytes,
            artifact_kind="period",
        )
        if materialization.grid_spec.grid_sha256 != grid_spec.grid_sha256:
            raise HlsSeasonalError("seasonal_grid_identity_mismatch")
        for path, content in materialization.files.items():
            files[_period_artifact_path(path, item.window.period_id)] = content
        metadata_payload, inventory_payload = _period_metadata_payload(
            item=item,
            materialization=materialization,
        )
        files[seasonal_json("composite_metadata.json", period_id=item.window.period_id)] = (
            _json_bytes(metadata_payload)
        )
        files[seasonal_json("input_inventory.json", period_id=item.window.period_id)] = _json_bytes(
            inventory_payload
        )
        records.append(_period_product_record(item.window, materialization))
        source_counts = {
            source.product: source.scene_count for source in item.composite.metadata.sources
        }
        qa_inputs.append(
            SeasonalQaInput(
                window=item.window,
                input_scene_count=item.composite.metadata.input_scene_count,
                l30_scene_count=source_counts.get("HLSL30", 0),
                s30_scene_count=source_counts.get("HLSS30", 0),
                reflectance_bytes=materialization.files["tiffs/hls_period_reflectance.tif"],
                indices_bytes=materialization.files["tiffs/hls_period_indices.tif"],
                total_count_bytes=materialization.files["tiffs/hls_valid_observation_count.tif"],
                l30_count_bytes=materialization.files["tiffs/hls_valid_observation_count_l30.tif"],
                s30_count_bytes=materialization.files["tiffs/hls_valid_observation_count_s30.tif"],
            )
        )
        raster_items.append(
            HlsSeasonalRasterItem(
                window=item.window,
                composite=item.composite,
                materialization=materialization,
            )
        )
        all_estimates.extend(materialization.estimates)

    cube_spec = seasonal.cube_spec
    period_records = tuple(records)
    cube_index = _build_cube_index(cube_spec, period_records)
    metadata = HlsSeasonalMetadata(
        generated_at=generation_time,
        start_year=request.start_year,
        end_year=request.end_year,
        period_ids=tuple(window.period_id for window in window_plan.windows),
        window_plan_sha256=window_plan.plan_sha256,
        grid_sha256=grid_spec.grid_sha256,
        source_products=("HLSL30", "HLSS30"),
        composite_method="median_reflectance_then_indices",
        total_estimated_uncompressed_bytes=total_estimated_bytes,
        period_products=period_records,
    )
    qa = analyze_seasonal_rasters(
        periods=tuple(qa_inputs),
        aoi_wgs84=aoi_wgs84,
        grid_spec=grid_spec,
        spectral_indices=data_config.indices,
        output_config=output_config,
        window_plan_sha256=window_plan.plan_sha256,
        generated_at=generation_time,
    )
    files.update(qa.files)
    files[seasonal_json("window_plan.json")] = _json_bytes(window_plan.model_dump(mode="json"))
    files[seasonal_json("cube_spec.json")] = _json_bytes(cube_spec.model_dump(mode="json"))
    files[seasonal_json("cube_index.json")] = _json_bytes(cube_index.model_dump(mode="json"))
    files[seasonal_json("series_metadata.json")] = _json_bytes(metadata.model_dump(mode="json"))
    return HlsSeasonalMaterialization(
        files=files,
        metadata=metadata,
        cube_index=cube_index,
        cube_spec=cube_spec,
        qa=qa,
        raster_items=tuple(raster_items),
        grid_spec=grid_spec,
        estimates=tuple(all_estimates),
    )


def hls_seasonal_metadata_json_schema() -> dict[str, Any]:
    schema = HlsSeasonalMetadata.model_json_schema(mode="serialization")
    schema["$id"] = HLS_SEASONAL_METADATA_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = HLS_SEASONAL_SCHEMA_VERSION
    return schema


def temporal_cube_index_json_schema() -> dict[str, Any]:
    schema = TemporalCubeIndex.model_json_schema(mode="serialization")
    schema["$id"] = TEMPORAL_CUBE_INDEX_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = HLS_SEASONAL_SCHEMA_VERSION
    return schema


def _period_download_estimates(
    *,
    grid_spec: RasterGridSpec,
    output_config: OutputConfig,
    index_count: int,
) -> tuple[DirectDownloadEstimate, ...]:
    return tuple(
        validate_direct_download(
            estimate_fixed_grid_download(
                grid_spec=grid_spec,
                band_count=band_count,
                bytes_per_sample=bytes_per_sample,
            ),
            maximum_bytes=output_config.maximum_direct_download_bytes,
            maximum_dimension=output_config.maximum_direct_download_dimension,
        )
        for band_count, bytes_per_sample in (
            (6, 4),
            (index_count, 4),
            (1, 2),
            (1, 2),
            (1, 2),
        )
    )


def _period_artifact_path(path: str, period_id: str) -> str:
    try:
        return published_raster_path(path, cadence="seasonal", period_id=period_id)
    except ValueError:
        raise HlsSeasonalError("period_artifact_path_not_flat_by_type") from None


def _period_metadata_payload(
    *,
    item: HlsSeasonalCompositeItem,
    materialization: HlsRasterMaterialization,
) -> tuple[dict[str, object], dict[str, object]]:
    metadata = item.composite.metadata.model_dump(mode="json")
    sources = metadata.pop("sources")
    inventory_path = seasonal_json("input_inventory.json", period_id=item.window.period_id)
    raster_metadata = materialization.metadata_payload()
    raster_metadata["raster_validations"] = [
        {
            **record.model_dump(mode="json"),
            "artifact_path": _period_artifact_path(
                record.artifact_path,
                item.window.period_id,
            ),
        }
        for record in materialization.validations
    ]
    raster_metadata["visualizations"] = [
        {
            **record.model_dump(mode="json"),
            "path": _period_artifact_path(record.path, item.window.period_id),
            "source_raster": _period_artifact_path(
                record.source_raster,
                item.window.period_id,
            ),
        }
        for record in materialization.visualizations
    ]
    return (
        {
            **metadata,
            "period_id": item.window.period_id,
            "season_year": item.window.season_year,
            "season": item.window.season.value,
            "input_inventory_path": inventory_path,
            "raster_materialization": raster_metadata,
        },
        {
            "complete_scene_inventory": True,
            "input_scene_count": item.composite.metadata.input_scene_count,
            "sources": sources,
        },
    )


def _period_product_record(
    window: TemporalWindow,
    materialization: HlsRasterMaterialization,
) -> HlsSeasonalPeriodProductRecord:
    prefix = window.period_id
    raster_files = materialization.files
    return HlsSeasonalPeriodProductRecord(
        period_id=prefix,
        season_year=window.season_year,
        season=window.season,
        start_date=window.start_date,
        end_date_exclusive=window.end_date_exclusive,
        composite_metadata_path=seasonal_json("composite_metadata.json", period_id=prefix),
        composite_input_inventory_path=seasonal_json("input_inventory.json", period_id=prefix),
        reflectance_raster_path=seasonal_tiff(prefix, "reflectance.tif"),
        reflectance_raster_sha256=_sha256(raster_files["tiffs/hls_period_reflectance.tif"]),
        indices_raster_path=seasonal_tiff(prefix, "indices.tif"),
        indices_raster_sha256=_sha256(raster_files["tiffs/hls_period_indices.tif"]),
        valid_observation_count_raster_path=seasonal_tiff(prefix, "observation_count_total.tif"),
        valid_observation_count_raster_sha256=_sha256(
            raster_files["tiffs/hls_valid_observation_count.tif"]
        ),
        l30_observation_count_raster_path=seasonal_tiff(prefix, "observation_count_l30.tif"),
        l30_observation_count_raster_sha256=_sha256(
            raster_files["tiffs/hls_valid_observation_count_l30.tif"]
        ),
        s30_observation_count_raster_path=seasonal_tiff(prefix, "observation_count_s30.tif"),
        s30_observation_count_raster_sha256=_sha256(
            raster_files["tiffs/hls_valid_observation_count_s30.tif"]
        ),
    )


def _build_cube_index(
    cube_spec: TemporalCubeSpec,
    records: tuple[HlsSeasonalPeriodProductRecord, ...],
) -> TemporalCubeIndex:
    spec_payload = cube_spec.model_dump(mode="json")
    spec_sha256 = hashlib.sha256(
        json.dumps(
            spec_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    period_axis = tuple(window.period_id for window in cube_spec.windows)
    index_hash = _cube_index_sha256(
        cube_spec_sha256=spec_sha256,
        grid_sha256=cube_spec.grid.grid_sha256,
        period_axis=period_axis,
        period_products=records,
    )
    return TemporalCubeIndex(
        cube_spec_sha256=spec_sha256,
        grid_sha256=cube_spec.grid.grid_sha256,
        period_axis=period_axis,
        reflectance_band_axis=cube_spec.reflectance_band_names,
        spectral_index_axis=cube_spec.spectral_indices,
        sensor_axis=cube_spec.sensor_names,
        reflectance_dimensions=cube_spec.reflectance_dimensions,
        spectral_index_dimensions=cube_spec.spectral_index_dimensions,
        observation_count_dimensions=cube_spec.observation_count_dimensions,
        period_products=records,
        missing_data_policy=cube_spec.missing_data_policy,
        interpolation_allowed=cube_spec.interpolation_allowed,
        cube_index_sha256=index_hash,
    )


def _cube_index_sha256(
    *,
    cube_spec_sha256: str,
    grid_sha256: str,
    period_axis: tuple[str, ...],
    period_products: tuple[HlsSeasonalPeriodProductRecord, ...],
) -> str:
    payload = {
        "cube_spec_sha256": cube_spec_sha256,
        "grid_sha256": grid_sha256,
        "period_axis": period_axis,
        "period_products": [product.model_dump(mode="json") for product in period_products],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_inputs(
    *,
    data_config: DataConfig,
    grid_spec: RasterGridSpec,
    window_plan: TemporalWindowPlan,
    generated_at: datetime,
) -> None:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise HlsSeasonalError("generated_at_requires_timezone")
    if not math.isclose(
        grid_spec.resolution_m,
        data_config.target_resolution_m,
        abs_tol=1e-9,
    ):
        raise HlsSeasonalError("grid_resolution_mismatch")
    if len(data_config.indices) != len(set(data_config.indices)):
        raise HlsSeasonalError("spectral_indices_not_unique")
    if window_plan.plan_sha256 == "":
        raise HlsSeasonalError("window_plan_identity_missing")
