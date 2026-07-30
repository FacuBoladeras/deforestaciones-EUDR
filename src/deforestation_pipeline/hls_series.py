"""Serie anual HLS alineada, QA por sensor y productos temporales locales."""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from io import BytesIO, StringIO
from typing import Any, Literal

import matplotlib
import numpy as np
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from deforestation_pipeline.artifact_layout import (
    annual_figure,
    annual_json,
    annual_table,
    annual_tiff,
    published_raster_path,
)
from deforestation_pipeline.catalog import BenchmarkSourcePlan
from deforestation_pipeline.config import DataConfig, OutputConfig
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import (
    REFLECTANCE_BAND_NAMES,
    HlsCompositeImages,
    HlsCompositeRequest,
    build_hls_annual_composite,
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

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

HLS_SERIES_METADATA_SCHEMA_VERSION: Literal["3.0.0"] = "3.0.0"
HLS_SERIES_COVERAGE_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
HLS_SERIES_METADATA_SCHEMA_ID = "urn:deforestation-pipeline:hls-series-metadata:3.0.0"
HLS_SERIES_COVERAGE_SCHEMA_ID = "urn:deforestation-pipeline:hls-series-coverage:1.0.0"
HLS_SERIES_MINIMUM_YEAR = 2015


class HlsSeriesError(RuntimeError):
    """Fallo legible y sanitizado de la serie temporal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Serie HLS fallida: {code}")


class StrictSeriesModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HlsSeriesRequest(StrictSeriesModel):
    """Rango inclusivo de años calendario completos."""

    start_year: int = Field(ge=HLS_SERIES_MINIMUM_YEAR)
    end_year: int = Field(ge=HLS_SERIES_MINIMUM_YEAR)

    @model_validator(mode="after")
    def years_are_ordered(self) -> HlsSeriesRequest:
        if self.start_year > self.end_year:
            raise ValueError("start_year no puede ser posterior a end_year")
        return self

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(range(self.start_year, self.end_year + 1))


class CoverageState(StrEnum):
    COMPLETE = "complete_observed_coverage"
    PARTIAL = "partial_observed_coverage"
    EMPTY = "no_observed_coverage"


class ObservationCountStatistics(StrictSeriesModel):
    minimum: float = Field(ge=0)
    p05: float = Field(ge=0)
    median: float = Field(ge=0)
    p95: float = Field(ge=0)
    maximum: float = Field(ge=0)


class HlsYearCoverage(StrictSeriesModel):
    year: int
    input_scene_count: int = Field(ge=1)
    l30_scene_count: int = Field(ge=0)
    s30_scene_count: int = Field(ge=0)
    roi_pixel_count: int = Field(gt=0)
    observed_pixel_count_total: int = Field(ge=0)
    observed_pixel_count_l30: int = Field(ge=0)
    observed_pixel_count_s30: int = Field(ge=0)
    valid_pixel_fraction_total: float = Field(ge=0, le=1)
    valid_pixel_fraction_l30: float = Field(ge=0, le=1)
    valid_pixel_fraction_s30: float = Field(ge=0, le=1)
    total_count: ObservationCountStatistics
    l30_count: ObservationCountStatistics
    s30_count: ObservationCountStatistics
    coverage_state: CoverageState
    quality_flags: tuple[str, ...]


class HlsSeriesCoverageDocument(StrictSeriesModel):
    schema_version: Literal["1.0.0"] = HLS_SERIES_COVERAGE_SCHEMA_VERSION
    grid_sha256: str
    years: tuple[HlsYearCoverage, ...]


class HlsAnnualVariableSummary(StrictSeriesModel):
    year: int
    variable_kind: Literal["reflectance", "index"]
    variable: str
    roi_pixel_count: int = Field(gt=0)
    valid_pixel_count: int = Field(ge=0)
    valid_pixel_fraction: float = Field(ge=0, le=1)
    p10: float | None
    median: float | None
    p90: float | None


class HlsSeriesYearProductRecord(StrictSeriesModel):
    year: int
    composite_metadata_path: str
    composite_input_inventory_path: str
    reflectance_raster_path: str
    indices_raster_path: str
    valid_observation_count_raster_path: str
    l30_observation_count_raster_path: str
    s30_observation_count_raster_path: str


class HlsSeriesMetadata(StrictSeriesModel):
    schema_version: Literal["3.0.0"] = HLS_SERIES_METADATA_SCHEMA_VERSION
    generated_at: datetime
    start_year: int
    end_year: int
    years: tuple[int, ...]
    grid_sha256: str
    grid_path: Literal["json/temporal/annual/grid.json"] = "json/temporal/annual/grid.json"
    coverage_path: Literal["json/temporal/annual/coverage.json"] = (
        "json/temporal/annual/coverage.json"
    )
    summary_table_path: Literal["tables/annual/summary.csv"] = "tables/annual/summary.csv"
    source_products: tuple[Literal["HLSL30"], Literal["HLSS30"]]
    annual_composite_method: Literal["median_reflectance_then_indices"]
    total_estimated_uncompressed_bytes: int = Field(gt=0)
    year_products: tuple[HlsSeriesYearProductRecord, ...]
    final_assessment_generated: Literal[False] = False

    @model_validator(mode="after")
    def metadata_is_consistent(self) -> HlsSeriesMetadata:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        if self.years != tuple(range(self.start_year, self.end_year + 1)):
            raise ValueError("years debe representar todo el rango inclusivo")
        if tuple(item.year for item in self.year_products) != self.years:
            raise ValueError("year_products debe coincidir con years")
        return self


def hls_series_metadata_json_schema() -> dict[str, Any]:
    """Devuelve el JSON Schema canónico de metadatos temporales 3.0.0."""
    schema = HlsSeriesMetadata.model_json_schema(mode="serialization")
    schema["$id"] = HLS_SERIES_METADATA_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = HLS_SERIES_METADATA_SCHEMA_VERSION
    return schema


def hls_series_coverage_json_schema() -> dict[str, Any]:
    """Devuelve el JSON Schema canónico del QA de cobertura 1.0.0."""
    schema = HlsSeriesCoverageDocument.model_json_schema(mode="serialization")
    schema["$id"] = HLS_SERIES_COVERAGE_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = HLS_SERIES_COVERAGE_SCHEMA_VERSION
    return schema


@dataclass(frozen=True, slots=True)
class HlsAnnualSeriesItem:
    year: int
    composite: HlsCompositeImages = field(repr=False, compare=False)
    materialization: HlsRasterMaterialization = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class HlsSeriesMaterialization:
    files: Mapping[str, bytes]
    metadata: HlsSeriesMetadata
    coverage: tuple[HlsYearCoverage, ...]
    summaries: tuple[HlsAnnualVariableSummary, ...]
    annual_items: tuple[HlsAnnualSeriesItem, ...] = field(repr=False, compare=False)
    grid_spec: RasterGridSpec
    estimates: tuple[DirectDownloadEstimate, ...]


@dataclass(frozen=True, slots=True)
class _LoadedRaster:
    values: NDArray[np.float64]
    band_names: tuple[str, ...]
    nodata: float


def materialize_hls_annual_series(
    *,
    session: GeeSession,
    plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    output_config: OutputConfig,
    aoi_wgs84: BaseGeometry,
    grid_spec: RasterGridSpec,
    request: HlsSeriesRequest,
    generated_at: datetime | None = None,
    fetch_bytes: FetchBytes | None = None,
) -> HlsSeriesMaterialization:
    """Construye, descarga y resume todos los años sobre una única grilla."""
    generation_time = generated_at or datetime.now(UTC)
    _validate_series_inputs(
        data_config=data_config,
        output_config=output_config,
        grid_spec=grid_spec,
        generated_at=generation_time,
    )
    if request.end_year >= generation_time.year:
        raise HlsSeriesError("series_requires_closed_calendar_years")
    annual_estimates = _annual_download_estimates(
        grid_spec=grid_spec,
        output_config=output_config,
        index_count=len(data_config.indices),
    )
    total_estimated_bytes = sum(
        estimate.estimated_uncompressed_bytes for estimate in annual_estimates
    ) * len(request.years)
    if total_estimated_bytes > output_config.maximum_series_download_bytes:
        raise HlsSeriesError("series_download_budget_exceeded")

    annual_items: list[HlsAnnualSeriesItem] = []
    all_estimates: list[DirectDownloadEstimate] = []
    for year in request.years:
        composite = build_hls_annual_composite(
            session=session,
            plan=plan,
            data_config=data_config,
            aoi_wgs84=aoi_wgs84,
            request=HlsCompositeRequest(
                start_date=datetime(year, 1, 1).date(),
                end_date_exclusive=datetime(year + 1, 1, 1).date(),
                target_crs=grid_spec.target_crs,
                scale_m=data_config.target_resolution_m,
            ),
            generated_at=generation_time,
        )
        materialization = materialize_hls_raster_products(
            product=composite,
            aoi_wgs84=aoi_wgs84,
            output_config=output_config,
            grid_spec=grid_spec,
            fetch_bytes=fetch_bytes,
        )
        if materialization.grid_spec.grid_sha256 != grid_spec.grid_sha256:
            raise HlsSeriesError("annual_grid_identity_mismatch")
        annual_items.append(
            HlsAnnualSeriesItem(
                year=year,
                composite=composite,
                materialization=materialization,
            )
        )
        all_estimates.extend(materialization.estimates)

    return _assemble_series_materialization(
        annual_items=tuple(annual_items),
        aoi_wgs84=aoi_wgs84,
        grid_spec=grid_spec,
        output_config=output_config,
        generated_at=generation_time,
        total_estimated_bytes=total_estimated_bytes,
        estimates=tuple(all_estimates),
    )


def _validate_series_inputs(
    *,
    data_config: DataConfig,
    output_config: OutputConfig,
    grid_spec: RasterGridSpec,
    generated_at: datetime,
) -> None:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise HlsSeriesError("generated_at_requires_timezone")
    if not math.isclose(
        grid_spec.resolution_m,
        data_config.target_resolution_m,
        abs_tol=1e-9,
    ):
        raise HlsSeriesError("grid_resolution_mismatch")
    if not math.isclose(
        grid_spec.nodata,
        output_config.raster_nodata,
        abs_tol=1e-9,
    ):
        raise HlsSeriesError("grid_nodata_mismatch")


def _annual_download_estimates(
    *,
    grid_spec: RasterGridSpec,
    output_config: OutputConfig,
    index_count: int,
) -> tuple[DirectDownloadEstimate, ...]:
    estimates = tuple(
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
            (len(REFLECTANCE_BAND_NAMES), 4),
            (index_count, 4),
            (1, 2),
            (1, 2),
            (1, 2),
        )
    )
    return estimates


def _assemble_series_materialization(
    *,
    annual_items: tuple[HlsAnnualSeriesItem, ...],
    aoi_wgs84: BaseGeometry,
    grid_spec: RasterGridSpec,
    output_config: OutputConfig,
    generated_at: datetime,
    total_estimated_bytes: int,
    estimates: tuple[DirectDownloadEstimate, ...],
) -> HlsSeriesMaterialization:
    roi_mask = _rasterize_aoi_mask(aoi_wgs84, grid_spec)
    roi_pixel_count = int(np.count_nonzero(roi_mask))
    if roi_pixel_count == 0:
        raise HlsSeriesError("aoi_has_no_pixel_centres_in_grid")

    files: dict[str, bytes] = {}
    coverage: list[HlsYearCoverage] = []
    summaries: list[HlsAnnualVariableSummary] = []
    year_records: list[HlsSeriesYearProductRecord] = []
    rgb_inputs: list[tuple[int, _LoadedRaster, NDArray[np.bool_]]] = []
    for item in annual_items:
        files.update(
            {
                _year_artifact_path(path, item.year): content
                for path, content in item.materialization.files.items()
            }
        )
        composite_metadata, inventory = _annual_metadata_payload(item)
        files[annual_json("composite_metadata.json", year=item.year)] = _json_bytes(
            composite_metadata
        )
        files[annual_json("input_inventory.json", year=item.year)] = _json_bytes(inventory)

        reflectance = _load_expected_raster(
            item.materialization.files["tiffs/hls_annual_reflectance.tif"],
            grid_spec,
        )
        indices = _load_expected_raster(
            item.materialization.files["tiffs/hls_annual_indices.tif"],
            grid_spec,
        )
        total = _load_expected_raster(
            item.materialization.files["tiffs/hls_valid_observation_count.tif"],
            grid_spec,
        )
        l30 = _load_expected_raster(
            item.materialization.files["tiffs/hls_valid_observation_count_l30.tif"],
            grid_spec,
        )
        s30 = _load_expected_raster(
            item.materialization.files["tiffs/hls_valid_observation_count_s30.tif"],
            grid_spec,
        )
        year_coverage, observed_mask = _year_coverage(
            item=item,
            roi_mask=roi_mask,
            total=total,
            l30=l30,
            s30=s30,
        )
        coverage.append(year_coverage)
        summaries.extend(
            _variable_summaries(
                year=item.year,
                roi_mask=roi_mask,
                observed_mask=observed_mask,
                raster=reflectance,
                variable_kind="reflectance",
            )
        )
        summaries.extend(
            _variable_summaries(
                year=item.year,
                roi_mask=roi_mask,
                observed_mask=observed_mask,
                raster=indices,
                variable_kind="index",
            )
        )
        rgb_inputs.append((item.year, reflectance, observed_mask))
        year_records.append(_year_product_record(item.year))

    coverage_document = HlsSeriesCoverageDocument(
        grid_sha256=grid_spec.grid_sha256,
        years=tuple(coverage),
    )
    metadata = HlsSeriesMetadata(
        generated_at=generated_at,
        start_year=annual_items[0].year,
        end_year=annual_items[-1].year,
        years=tuple(item.year for item in annual_items),
        grid_sha256=grid_spec.grid_sha256,
        source_products=("HLSL30", "HLSS30"),
        annual_composite_method="median_reflectance_then_indices",
        total_estimated_uncompressed_bytes=total_estimated_bytes,
        year_products=tuple(year_records),
    )
    files[annual_json("grid.json")] = _json_bytes(grid_spec.model_dump(mode="json"))
    files[annual_json("coverage.json")] = _json_bytes(coverage_document.model_dump(mode="json"))
    files[annual_json("series_metadata.json")] = _json_bytes(metadata.model_dump(mode="json"))
    files[annual_table("summary.csv")] = _summary_csv(tuple(summaries))
    files[annual_figure(None, "rgb_panel.png")] = _render_annual_rgb_panel(
        tuple(rgb_inputs),
        output_config,
    )
    files[annual_figure(None, "index_timeseries.png")] = _render_index_timeseries(
        tuple(summaries),
        output_config,
    )
    files[annual_figure(None, "observation_coverage.png")] = _render_observation_coverage(
        tuple(coverage), output_config.png_dpi
    )
    return HlsSeriesMaterialization(
        files=files,
        metadata=metadata,
        coverage=tuple(coverage),
        summaries=tuple(summaries),
        annual_items=annual_items,
        grid_spec=grid_spec,
        estimates=estimates,
    )


def _annual_metadata_payload(
    item: HlsAnnualSeriesItem,
) -> tuple[dict[str, object], dict[str, object]]:
    metadata = item.composite.metadata.model_dump(mode="json")
    sources = metadata.pop("sources")
    inventory_path = annual_json("input_inventory.json", year=item.year)
    inventory = {
        "complete_scene_inventory": True,
        "input_scene_count": item.composite.metadata.input_scene_count,
        "sources": sources,
    }
    materialization = item.materialization.metadata_payload()
    materialization["raster_validations"] = [
        {
            **record.model_dump(mode="json"),
            "artifact_path": _year_artifact_path(record.artifact_path, item.year),
        }
        for record in item.materialization.validations
    ]
    materialization["visualizations"] = [
        {
            **record.model_dump(mode="json"),
            "path": _year_artifact_path(record.path, item.year),
            "source_raster": _year_artifact_path(record.source_raster, item.year),
        }
        for record in item.materialization.visualizations
    ]
    return (
        {
            **metadata,
            "input_inventory_path": inventory_path,
            "raster_materialization": materialization,
        },
        inventory,
    )


def _year_product_record(year: int) -> HlsSeriesYearProductRecord:
    return HlsSeriesYearProductRecord(
        year=year,
        composite_metadata_path=annual_json("composite_metadata.json", year=year),
        composite_input_inventory_path=annual_json("input_inventory.json", year=year),
        reflectance_raster_path=annual_tiff(year, "reflectance.tif"),
        indices_raster_path=annual_tiff(year, "indices.tif"),
        valid_observation_count_raster_path=annual_tiff(year, "observation_count_total.tif"),
        l30_observation_count_raster_path=annual_tiff(year, "observation_count_l30.tif"),
        s30_observation_count_raster_path=annual_tiff(year, "observation_count_s30.tif"),
    )


def _year_artifact_path(path: str, year: int) -> str:
    try:
        return published_raster_path(path, cadence="annual", period_id=str(year))
    except ValueError:
        raise HlsSeriesError("annual_artifact_path_not_flat_by_type") from None


def _rasterize_aoi_mask(
    aoi_wgs84: BaseGeometry,
    grid_spec: RasterGridSpec,
) -> NDArray[np.bool_]:
    try:
        transformer = Transformer.from_crs(
            "EPSG:4326",
            grid_spec.target_crs,
            always_xy=True,
        )
        projected = transform(transformer.transform, aoi_wgs84)
        mask = geometry_mask(
            [mapping(projected)],
            out_shape=(grid_spec.height, grid_spec.width),
            transform=Affine(*grid_spec.transform),
            all_touched=False,
            invert=True,
        )
    except Exception:
        raise HlsSeriesError("aoi_mask_rasterization_failed") from None
    return np.asarray(mask, dtype=np.bool_)


def _load_expected_raster(content: bytes, grid_spec: RasterGridSpec) -> _LoadedRaster:
    try:
        with MemoryFile(content) as memory:
            with memory.open() as dataset:
                if dataset.crs is None or dataset.crs.to_string() != grid_spec.target_crs:
                    raise HlsSeriesError("annual_raster_crs_mismatch")
                if dataset.width != grid_spec.width or dataset.height != grid_spec.height:
                    raise HlsSeriesError("annual_raster_dimensions_mismatch")
                actual_transform = (
                    float(dataset.transform.a),
                    float(dataset.transform.b),
                    float(dataset.transform.c),
                    float(dataset.transform.d),
                    float(dataset.transform.e),
                    float(dataset.transform.f),
                )
                if not all(
                    math.isclose(actual, expected, abs_tol=1e-8)
                    for actual, expected in zip(
                        actual_transform,
                        grid_spec.transform,
                        strict=True,
                    )
                ):
                    raise HlsSeriesError("annual_raster_transform_mismatch")
                if dataset.nodata is None:
                    raise HlsSeriesError("annual_raster_missing_nodata")
                return _LoadedRaster(
                    values=dataset.read().astype(np.float64, copy=False),
                    band_names=tuple(name or "" for name in dataset.descriptions),
                    nodata=float(dataset.nodata),
                )
    except HlsSeriesError:
        raise
    except Exception:
        raise HlsSeriesError("annual_raster_unreadable") from None


def _year_coverage(
    *,
    item: HlsAnnualSeriesItem,
    roi_mask: NDArray[np.bool_],
    total: _LoadedRaster,
    l30: _LoadedRaster,
    s30: _LoadedRaster,
) -> tuple[HlsYearCoverage, NDArray[np.bool_]]:
    total_values = total.values[0]
    l30_values = l30.values[0]
    s30_values = s30.values[0]
    for values, nodata in (
        (total_values, total.nodata),
        (l30_values, l30.nodata),
        (s30_values, s30.nodata),
    ):
        valid = roi_mask & np.isfinite(values) & ~np.isclose(values, nodata)
        if np.any(values[valid] < 0):
            raise HlsSeriesError("negative_observation_count")
    common_valid = (
        roi_mask
        & np.isfinite(total_values)
        & ~np.isclose(total_values, total.nodata)
        & np.isfinite(l30_values)
        & ~np.isclose(l30_values, l30.nodata)
        & np.isfinite(s30_values)
        & ~np.isclose(s30_values, s30.nodata)
    )
    if np.any(
        ~np.isclose(
            total_values[common_valid],
            l30_values[common_valid] + s30_values[common_valid],
            atol=0,
        )
    ):
        raise HlsSeriesError("sensor_counts_do_not_sum_to_total")

    total_observed = common_valid & (total_values > 0)
    l30_observed = common_valid & (l30_values > 0)
    s30_observed = common_valid & (s30_values > 0)
    roi_count = int(np.count_nonzero(roi_mask))
    total_count = int(np.count_nonzero(total_observed))
    l30_count = int(np.count_nonzero(l30_observed))
    s30_count = int(np.count_nonzero(s30_observed))
    total_fraction = total_count / roi_count
    l30_fraction = l30_count / roi_count
    s30_fraction = s30_count / roi_count
    if total_count == roi_count:
        state = CoverageState.COMPLETE
    elif total_count == 0:
        state = CoverageState.EMPTY
    else:
        state = CoverageState.PARTIAL

    source_counts = {
        source.product: source.scene_count for source in item.composite.metadata.sources
    }
    flags: list[str] = []
    if state is CoverageState.EMPTY:
        flags.append("no_total_valid_observations")
    elif state is CoverageState.PARTIAL:
        flags.append("partial_total_valid_observations")
    if l30_count == 0:
        flags.append("no_l30_valid_observations")
    if s30_count == 0:
        flags.append("no_s30_valid_observations")
    if source_counts.get("HLSL30", 0) == 0:
        flags.append("no_l30_input_scenes")
    if source_counts.get("HLSS30", 0) == 0:
        flags.append("no_s30_input_scenes")

    return (
        HlsYearCoverage(
            year=item.year,
            input_scene_count=item.composite.metadata.input_scene_count,
            l30_scene_count=source_counts.get("HLSL30", 0),
            s30_scene_count=source_counts.get("HLSS30", 0),
            roi_pixel_count=roi_count,
            observed_pixel_count_total=total_count,
            observed_pixel_count_l30=l30_count,
            observed_pixel_count_s30=s30_count,
            valid_pixel_fraction_total=total_fraction,
            valid_pixel_fraction_l30=l30_fraction,
            valid_pixel_fraction_s30=s30_fraction,
            total_count=_count_statistics(total_values[common_valid]),
            l30_count=_count_statistics(l30_values[common_valid]),
            s30_count=_count_statistics(s30_values[common_valid]),
            coverage_state=state,
            quality_flags=tuple(flags),
        ),
        total_observed,
    )


def _count_statistics(values: NDArray[np.float64]) -> ObservationCountStatistics:
    if values.size == 0:
        return ObservationCountStatistics(
            minimum=0,
            p05=0,
            median=0,
            p95=0,
            maximum=0,
        )
    return ObservationCountStatistics(
        minimum=float(np.min(values)),
        p05=float(np.percentile(values, 5)),
        median=float(np.median(values)),
        p95=float(np.percentile(values, 95)),
        maximum=float(np.max(values)),
    )


def _variable_summaries(
    *,
    year: int,
    roi_mask: NDArray[np.bool_],
    observed_mask: NDArray[np.bool_],
    raster: _LoadedRaster,
    variable_kind: Literal["reflectance", "index"],
) -> tuple[HlsAnnualVariableSummary, ...]:
    rows: list[HlsAnnualVariableSummary] = []
    roi_count = int(np.count_nonzero(roi_mask))
    for band_name, band in zip(raster.band_names, raster.values, strict=True):
        valid = observed_mask & np.isfinite(band) & ~np.isclose(band, raster.nodata)
        values = band[valid]
        count = int(values.size)
        rows.append(
            HlsAnnualVariableSummary(
                year=year,
                variable_kind=variable_kind,
                variable=band_name,
                roi_pixel_count=roi_count,
                valid_pixel_count=count,
                valid_pixel_fraction=count / roi_count,
                p10=(float(np.percentile(values, 10)) if count else None),
                median=(float(np.median(values)) if count else None),
                p90=(float(np.percentile(values, 90)) if count else None),
            )
        )
    return tuple(rows)


def _summary_csv(rows: tuple[HlsAnnualVariableSummary, ...]) -> bytes:
    output = StringIO(newline="")
    fieldnames = (
        "year",
        "variable_kind",
        "variable",
        "roi_pixel_count",
        "valid_pixel_count",
        "valid_pixel_fraction",
        "p10",
        "median",
        "p90",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        payload = row.model_dump(mode="json")
        writer.writerow({key: ("" if payload[key] is None else payload[key]) for key in fieldnames})
    return output.getvalue().encode("utf-8")


def _render_annual_rgb_panel(
    inputs: tuple[tuple[int, _LoadedRaster, NDArray[np.bool_]], ...],
    output_config: OutputConfig,
) -> bytes:
    columns = min(3, len(inputs))
    rows = math.ceil(len(inputs) / columns)
    figure, axes = plt.subplots(
        rows,
        columns,
        figsize=(4.5 * columns, 4.2 * rows),
        squeeze=False,
    )
    for axis, (year, raster, observed_mask) in zip(
        axes.flat,
        inputs,
        strict=False,
    ):
        band_lookup = {name: position for position, name in enumerate(raster.band_names)}
        rgb = np.stack(
            [
                raster.values[band_lookup["red"]],
                raster.values[band_lookup["green"]],
                raster.values[band_lookup["blue"]],
            ],
            axis=-1,
        )
        scaled = np.clip(
            (rgb - output_config.rgb_min_reflectance)
            / (output_config.rgb_max_reflectance - output_config.rgb_min_reflectance),
            0,
            1,
        )
        alpha = observed_mask & np.all(np.isfinite(rgb), axis=-1)
        rgba = np.dstack((scaled, alpha.astype(np.float64)))
        axis.imshow(rgba)
        axis.set_title(f"HLS anual {year}")
        axis.set_axis_off()
    for axis in tuple(axes.flat)[len(inputs) :]:
        axis.set_axis_off()
    figure.suptitle("Composición RGB anual — misma grilla")
    figure.tight_layout()
    return _figure_bytes(figure, output_config.png_dpi)


def _render_index_timeseries(
    summaries: tuple[HlsAnnualVariableSummary, ...],
    output_config: OutputConfig,
) -> bytes:
    index_names = tuple(item.index.value for item in output_config.index_visualization_ranges)
    columns = 2
    rows = math.ceil(len(index_names) / columns)
    figure, axes = plt.subplots(
        rows,
        columns,
        figsize=(11, 3.2 * rows),
        squeeze=False,
    )
    for axis, index_name in zip(axes.flat, index_names, strict=False):
        selected = [
            row for row in summaries if row.variable_kind == "index" and row.variable == index_name
        ]
        years = np.array([row.year for row in selected], dtype=np.int32)
        available = [row for row in selected if row.median is not None]
        if available:
            available_years = np.array([row.year for row in available], dtype=np.int32)
            medians = np.array([row.median for row in available], dtype=np.float64)
            p10 = np.array([row.p10 for row in available], dtype=np.float64)
            p90 = np.array([row.p90 for row in available], dtype=np.float64)
            axis.plot(available_years, medians, marker="o", color="#166534")
            axis.fill_between(
                available_years,
                p10,
                p90,
                color="#86efac",
                alpha=0.45,
            )
        axis.set_title(index_name)
        axis.set_xticks(years)
        axis.grid(alpha=0.25)
    for axis in tuple(axes.flat)[len(index_names) :]:
        axis.set_axis_off()
    figure.suptitle("Índices HLS por año — mediana y percentiles 10-90")
    figure.tight_layout()
    return _figure_bytes(figure, output_config.png_dpi)


def _render_observation_coverage(
    coverage: tuple[HlsYearCoverage, ...],
    dpi: int,
) -> bytes:
    years = [item.year for item in coverage]
    figure, axis = plt.subplots(figsize=(9, 4.8))
    _coverage_line(
        axis,
        years,
        [item.valid_pixel_fraction_total for item in coverage],
        "Total",
        "#111827",
    )
    _coverage_line(
        axis,
        years,
        [item.valid_pixel_fraction_l30 for item in coverage],
        "HLSL30",
        "#2563eb",
    )
    _coverage_line(
        axis,
        years,
        [item.valid_pixel_fraction_s30 for item in coverage],
        "HLSS30",
        "#ea580c",
    )
    axis.set_ylim(0, 1.05)
    axis.set_ylabel("Fracción de píxeles del ROI con observaciones")
    axis.set_xticks(years)
    axis.grid(alpha=0.25)
    axis.legend()
    axis.set_title("Cobertura anual de observaciones válidas")
    figure.tight_layout()
    return _figure_bytes(figure, dpi)


def _coverage_line(
    axis: Axes,
    years: list[int],
    values: list[float],
    label: str,
    color: str,
) -> None:
    axis.plot(years, values, marker="o", label=label, color=color)


def _figure_bytes(figure: Figure, dpi: int) -> bytes:
    output = BytesIO()
    figure.savefig(
        output,
        format="png",
        dpi=dpi,
        bbox_inches="tight",
        metadata={"Software": "deforestation-pipeline"},
    )
    plt.close(figure)
    return output.getvalue()


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
