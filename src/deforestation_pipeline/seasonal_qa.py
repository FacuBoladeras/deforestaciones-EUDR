"""QA observacional y espectral para rasters estacionales HLS."""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from io import BytesIO, StringIO
from typing import Any, Literal

import matplotlib
import numpy as np
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
    seasonal_figure,
    seasonal_json,
    seasonal_table,
)
from deforestation_pipeline.config import OutputConfig, SpectralIndex
from deforestation_pipeline.hls_composite import REFLECTANCE_BAND_NAMES
from deforestation_pipeline.schemas import RasterGridSpec
from deforestation_pipeline.temporal_cube import TemporalWindow

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

SEASONAL_COVERAGE_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
SEASONAL_COVERAGE_SCHEMA_ID = "urn:deforestation-pipeline:hls-seasonal-coverage:1.0.0"


class SeasonalQaError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"QA estacional fallido: {code}")


class StrictQaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeasonalCoverageState(StrEnum):
    COMPLETE = "complete_observed_coverage"
    PARTIAL = "partial_observed_coverage"
    EMPTY = "no_observed_coverage"


class SeasonalCountStatistics(StrictQaModel):
    minimum: float = Field(ge=0)
    p05: float = Field(ge=0)
    median: float = Field(ge=0)
    p95: float = Field(ge=0)
    maximum: float = Field(ge=0)


class SeasonalPeriodCoverage(StrictQaModel):
    period_id: str = Field(pattern=r"^\d{4}-(DJF|MAM|JJA|SON)$")
    season_year: int
    season: str
    start_date: str
    end_date_exclusive: str
    expected_days: int = Field(gt=0)
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
    total_count: SeasonalCountStatistics
    l30_count: SeasonalCountStatistics
    s30_count: SeasonalCountStatistics
    coverage_state: SeasonalCoverageState
    quality_flags: tuple[str, ...]


class SeasonalCoverageDocument(StrictQaModel):
    schema_version: Literal["1.0.0"] = SEASONAL_COVERAGE_SCHEMA_VERSION
    generated_at: datetime
    grid_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    periods: tuple[SeasonalPeriodCoverage, ...]
    interpolation_applied: Literal[False] = False

    @model_validator(mode="after")
    def document_is_consistent(self) -> SeasonalCoverageDocument:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        period_ids = tuple(item.period_id for item in self.periods)
        if len(period_ids) != len(set(period_ids)):
            raise ValueError("periods no puede contener períodos duplicados")
        return self


class SeasonalVariableSummary(StrictQaModel):
    period_id: str
    season_year: int
    season: str
    variable_kind: Literal["reflectance", "index"]
    variable: str
    roi_pixel_count: int = Field(gt=0)
    valid_pixel_count: int = Field(ge=0)
    valid_pixel_fraction: float = Field(ge=0, le=1)
    p10: float | None
    median: float | None
    p90: float | None


@dataclass(frozen=True, slots=True)
class SeasonalQaInput:
    window: TemporalWindow
    input_scene_count: int
    l30_scene_count: int
    s30_scene_count: int
    reflectance_bytes: bytes
    indices_bytes: bytes
    total_count_bytes: bytes
    l30_count_bytes: bytes
    s30_count_bytes: bytes


@dataclass(frozen=True, slots=True)
class SeasonalQaResult:
    files: Mapping[str, bytes]
    coverage: SeasonalCoverageDocument
    summaries: tuple[SeasonalVariableSummary, ...]


@dataclass(frozen=True, slots=True)
class _LoadedRaster:
    values: NDArray[np.float64]
    band_names: tuple[str, ...]
    nodata: float


def analyze_seasonal_rasters(
    *,
    periods: tuple[SeasonalQaInput, ...],
    aoi_wgs84: BaseGeometry,
    grid_spec: RasterGridSpec,
    spectral_indices: tuple[SpectralIndex, ...],
    output_config: OutputConfig,
    window_plan_sha256: str,
    generated_at: datetime,
) -> SeasonalQaResult:
    if not periods:
        raise SeasonalQaError("periods_required")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise SeasonalQaError("generated_at_requires_timezone")
    roi_mask = _rasterize_aoi_mask(aoi_wgs84, grid_spec)
    roi_pixel_count = int(np.count_nonzero(roi_mask))
    if roi_pixel_count == 0:
        raise SeasonalQaError("aoi_has_no_pixel_centres_in_grid")

    ordered_periods = tuple(
        sorted(periods, key=lambda item: (item.window.start_date, item.window.period_id))
    )
    coverage_items: list[SeasonalPeriodCoverage] = []
    summaries: list[SeasonalVariableSummary] = []
    rgb_inputs: list[tuple[TemporalWindow, _LoadedRaster, NDArray[np.bool_]]] = []
    expected_indices = tuple(index.value for index in spectral_indices)
    for period in ordered_periods:
        reflectance = _load_raster(
            period.reflectance_bytes,
            grid_spec,
            REFLECTANCE_BAND_NAMES,
        )
        indices = _load_raster(period.indices_bytes, grid_spec, expected_indices)
        total = _load_raster(
            period.total_count_bytes,
            grid_spec,
            ("valid_observation_count",),
        )
        l30 = _load_raster(
            period.l30_count_bytes,
            grid_spec,
            ("valid_observation_count_l30",),
        )
        s30 = _load_raster(
            period.s30_count_bytes,
            grid_spec,
            ("valid_observation_count_s30",),
        )
        total_values = _count_values(total)
        l30_values = _count_values(l30)
        s30_values = _count_values(s30)
        if not np.allclose(
            total_values[roi_mask],
            (l30_values + s30_values)[roi_mask],
            atol=0,
            rtol=0,
        ):
            raise SeasonalQaError("sensor_count_sum_mismatch")
        observed_total = roi_mask & (total_values > 0)
        observed_l30 = roi_mask & (l30_values > 0)
        observed_s30 = roi_mask & (s30_values > 0)
        rgb_inputs.append((period.window, reflectance, observed_total))
        coverage_items.append(
            _coverage(
                period=period,
                roi_mask=roi_mask,
                observed_total=observed_total,
                observed_l30=observed_l30,
                observed_s30=observed_s30,
                total_values=total_values,
                l30_values=l30_values,
                s30_values=s30_values,
            )
        )
        summaries.extend(
            _summaries(
                period=period,
                raster=reflectance,
                roi_mask=roi_mask,
                observed_mask=observed_total,
                variable_kind="reflectance",
            )
        )
        summaries.extend(
            _summaries(
                period=period,
                raster=indices,
                roi_mask=roi_mask,
                observed_mask=observed_total,
                variable_kind="index",
            )
        )

    coverage_document = SeasonalCoverageDocument(
        generated_at=generated_at,
        grid_sha256=grid_spec.grid_sha256,
        window_plan_sha256=window_plan_sha256,
        periods=tuple(coverage_items),
    )
    summary_tuple = tuple(summaries)
    files = {
        seasonal_json("coverage.json"): _json_bytes(coverage_document.model_dump(mode="json")),
        seasonal_table("summary.csv"): _summary_csv(summary_tuple),
        seasonal_figure(None, "index_timeseries.png"): _render_index_timeseries(
            summary_tuple,
            output_config,
        ),
        seasonal_figure(None, "observation_coverage.png"): _render_coverage(
            tuple(coverage_items),
            output_config.png_dpi,
        ),
        seasonal_figure(None, "rgb_timeline.png"): _render_rgb_timeline(
            tuple(rgb_inputs),
            output_config,
        ),
    }
    return SeasonalQaResult(
        files=files,
        coverage=coverage_document,
        summaries=summary_tuple,
    )


def _count_values(raster: _LoadedRaster) -> NDArray[np.float64]:
    """Interpreta máscara de ``ImageCollection.count`` como cero observaciones."""
    values = raster.values[0]
    observed = np.isfinite(values) & ~np.isclose(values, raster.nodata)
    return np.where(observed, values, 0.0)


def seasonal_coverage_json_schema() -> dict[str, Any]:
    schema = SeasonalCoverageDocument.model_json_schema(mode="serialization")
    schema["$id"] = SEASONAL_COVERAGE_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = SEASONAL_COVERAGE_SCHEMA_VERSION
    return schema


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
        result = geometry_mask(
            [mapping(projected)],
            out_shape=(grid_spec.height, grid_spec.width),
            transform=Affine(*grid_spec.transform),
            all_touched=False,
            invert=True,
        )
    except Exception:
        raise SeasonalQaError("aoi_mask_rasterization_failed") from None
    return np.asarray(result, dtype=np.bool_)


def _load_raster(
    content: bytes,
    grid_spec: RasterGridSpec,
    expected_band_names: tuple[str, ...],
) -> _LoadedRaster:
    try:
        with MemoryFile(content) as memory:
            with memory.open() as dataset:
                descriptions = tuple(item or "" for item in dataset.descriptions)
                if descriptions != expected_band_names:
                    raise SeasonalQaError("raster_band_names_mismatch")
                if dataset.width != grid_spec.width or dataset.height != grid_spec.height:
                    raise SeasonalQaError("raster_grid_dimensions_mismatch")
                if dataset.crs is None or dataset.crs.to_string() != grid_spec.target_crs:
                    raise SeasonalQaError("raster_crs_mismatch")
                transform_values = tuple(float(value) for value in dataset.transform[:6])
                if not all(
                    math.isclose(left, right, abs_tol=1e-8)
                    for left, right in zip(
                        transform_values,
                        grid_spec.transform,
                        strict=True,
                    )
                ):
                    raise SeasonalQaError("raster_transform_mismatch")
                if dataset.nodata is None:
                    raise SeasonalQaError("raster_nodata_missing")
                values = dataset.read().astype(np.float64, copy=False)
                return _LoadedRaster(
                    values=values,
                    band_names=descriptions,
                    nodata=float(dataset.nodata),
                )
    except SeasonalQaError:
        raise
    except Exception:
        raise SeasonalQaError("raster_unreadable") from None


def _coverage(
    *,
    period: SeasonalQaInput,
    roi_mask: NDArray[np.bool_],
    observed_total: NDArray[np.bool_],
    observed_l30: NDArray[np.bool_],
    observed_s30: NDArray[np.bool_],
    total_values: NDArray[np.float64],
    l30_values: NDArray[np.float64],
    s30_values: NDArray[np.float64],
) -> SeasonalPeriodCoverage:
    roi_count = int(np.count_nonzero(roi_mask))
    total_count = int(np.count_nonzero(observed_total))
    l30_count = int(np.count_nonzero(observed_l30))
    s30_count = int(np.count_nonzero(observed_s30))
    if total_count == 0:
        state = SeasonalCoverageState.EMPTY
    elif total_count == roi_count:
        state = SeasonalCoverageState.COMPLETE
    else:
        state = SeasonalCoverageState.PARTIAL
    flags: list[str] = []
    if state is SeasonalCoverageState.EMPTY:
        flags.append("no_observed_coverage")
    elif state is SeasonalCoverageState.PARTIAL:
        flags.append("partial_observed_coverage")
    if l30_count == 0:
        flags.append("no_l30_observed_coverage")
    if s30_count == 0:
        flags.append("no_s30_observed_coverage")
    if state is SeasonalCoverageState.EMPTY:
        flags.append("insufficient_data")
    return SeasonalPeriodCoverage(
        period_id=period.window.period_id,
        season_year=period.window.season_year,
        season=period.window.season.value,
        start_date=period.window.start_date.isoformat(),
        end_date_exclusive=period.window.end_date_exclusive.isoformat(),
        expected_days=period.window.expected_days,
        input_scene_count=period.input_scene_count,
        l30_scene_count=period.l30_scene_count,
        s30_scene_count=period.s30_scene_count,
        roi_pixel_count=roi_count,
        observed_pixel_count_total=total_count,
        observed_pixel_count_l30=l30_count,
        observed_pixel_count_s30=s30_count,
        valid_pixel_fraction_total=total_count / roi_count,
        valid_pixel_fraction_l30=l30_count / roi_count,
        valid_pixel_fraction_s30=s30_count / roi_count,
        total_count=_count_statistics(total_values[roi_mask]),
        l30_count=_count_statistics(l30_values[roi_mask]),
        s30_count=_count_statistics(s30_values[roi_mask]),
        coverage_state=state,
        quality_flags=tuple(flags),
    )


def _count_statistics(values: NDArray[np.float64]) -> SeasonalCountStatistics:
    return SeasonalCountStatistics(
        minimum=float(np.min(values)),
        p05=float(np.percentile(values, 5)),
        median=float(np.median(values)),
        p95=float(np.percentile(values, 95)),
        maximum=float(np.max(values)),
    )


def _summaries(
    *,
    period: SeasonalQaInput,
    raster: _LoadedRaster,
    roi_mask: NDArray[np.bool_],
    observed_mask: NDArray[np.bool_],
    variable_kind: Literal["reflectance", "index"],
) -> tuple[SeasonalVariableSummary, ...]:
    result: list[SeasonalVariableSummary] = []
    roi_count = int(np.count_nonzero(roi_mask))
    for band_name, band in zip(raster.band_names, raster.values, strict=True):
        valid = roi_mask & observed_mask & np.isfinite(band) & ~np.isclose(band, raster.nodata)
        values = band[valid]
        valid_count = int(values.size)
        percentiles = (
            (None, None, None)
            if valid_count == 0
            else tuple(float(value) for value in np.percentile(values, (10, 50, 90)))
        )
        result.append(
            SeasonalVariableSummary(
                period_id=period.window.period_id,
                season_year=period.window.season_year,
                season=period.window.season.value,
                variable_kind=variable_kind,
                variable=band_name,
                roi_pixel_count=roi_count,
                valid_pixel_count=valid_count,
                valid_pixel_fraction=valid_count / roi_count,
                p10=percentiles[0],
                median=percentiles[1],
                p90=percentiles[2],
            )
        )
    return tuple(result)


def _summary_csv(rows: tuple[SeasonalVariableSummary, ...]) -> bytes:
    stream = StringIO(newline="")
    fieldnames = list(SeasonalVariableSummary.model_fields)
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row.model_dump(mode="json"))
    return stream.getvalue().encode("utf-8")


def _render_index_timeseries(
    rows: tuple[SeasonalVariableSummary, ...],
    output_config: OutputConfig,
) -> bytes:
    index_rows = [row for row in rows if row.variable_kind == "index"]
    period_ids = tuple(dict.fromkeys(row.period_id for row in index_rows))
    figure, axis = plt.subplots(figsize=(max(8, len(period_ids) * 0.7), 5))
    for index in output_config.index_visualization_ranges:
        selected = [row for row in index_rows if row.variable == index.index.value]
        if not selected:
            continue
        axis.plot(
            [row.period_id for row in selected],
            [np.nan if row.median is None else row.median for row in selected],
            marker="o",
            linewidth=1.2,
            label=index.index.value,
        )
    axis.set_title("Mediana estacional de índices dentro del ROI")
    axis.set_ylabel("Valor")
    axis.tick_params(axis="x", rotation=60)
    axis.grid(alpha=0.25)
    axis.legend(ncol=2, fontsize=8)
    figure.tight_layout()
    return _figure_bytes(figure, output_config.png_dpi)


def _render_coverage(
    periods: tuple[SeasonalPeriodCoverage, ...],
    dpi: int,
) -> bytes:
    labels = [item.period_id for item in periods]
    x = np.arange(len(labels))
    figure, axis = plt.subplots(figsize=(max(8, len(labels) * 0.7), 5))
    axis.plot(
        x,
        [item.valid_pixel_fraction_total for item in periods],
        marker="o",
        label="total",
    )
    axis.plot(
        x,
        [item.valid_pixel_fraction_l30 for item in periods],
        marker="o",
        label="HLSL30",
    )
    axis.plot(
        x,
        [item.valid_pixel_fraction_s30 for item in periods],
        marker="o",
        label="HLSS30",
    )
    axis.set_xticks(x, labels, rotation=60)
    axis.set_ylim(0, 1.05)
    axis.set_ylabel("Fracción del ROI con observaciones")
    axis.set_title("Cobertura observacional estacional")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    return _figure_bytes(figure, dpi)


def _render_rgb_timeline(
    inputs: tuple[
        tuple[TemporalWindow, _LoadedRaster, NDArray[np.bool_]],
        ...,
    ],
    output_config: OutputConfig,
) -> bytes:
    columns = min(4, len(inputs))
    rows = math.ceil(len(inputs) / columns)
    figure, axes = plt.subplots(
        rows,
        columns,
        figsize=(4 * columns, 3.5 * rows),
        squeeze=False,
    )
    for position, (axis, (window, raster, observed_mask)) in enumerate(
        zip(axes.flat, inputs, strict=False)
    ):
        band_lookup = {name: index for index, name in enumerate(raster.band_names)}
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
        alpha = (
            observed_mask
            & np.all(np.isfinite(rgb), axis=-1)
            & np.all(~np.isclose(rgb, raster.nodata), axis=-1)
        )
        axis.imshow(np.dstack((scaled, alpha.astype(np.float64))))
        axis.set_title(f"t{position} · {window.period_id}")
        axis.set_axis_off()
    for axis in tuple(axes.flat)[len(inputs) :]:
        axis.set_axis_off()
    figure.suptitle(f"Serie temporal RGB HLS — t0 a t{len(inputs) - 1} — misma grilla y escala")
    figure.tight_layout()
    return _figure_bytes(figure, output_config.png_dpi)


def _figure_bytes(figure: Figure, dpi: int) -> bytes:
    stream = BytesIO()
    figure.savefig(stream, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(figure)
    return stream.getvalue()


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
