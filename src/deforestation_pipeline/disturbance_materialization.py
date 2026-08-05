"""I/O compacto del Paso 14.6 para evidencia de perturbaciones."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO, StringIO
from typing import Any, Literal

import matplotlib
import numpy as np
import rasterio
from matplotlib.colors import BoundaryNorm, ListedColormap, PowerNorm
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from numpy.typing import NDArray
from rasterio.io import MemoryFile

from deforestation_pipeline.ccdc_benchmark import CCDC_SCALAR_SUMMARY_BAND_NAMES
from deforestation_pipeline.change_detection import (
    DISTURBANCE_DETECTION_SCHEMA_VERSION,
    DISTURBANCE_DIAGNOSTIC_BAND_NAMES,
    DISTURBANCE_SUMMARY_BAND_NAMES,
    CcdcBenchmarkResult,
    DetectorConvergenceReasonCode,
    DisturbanceDetectionConfig,
    DisturbanceQualityBit,
    DisturbanceStateCode,
    ForestEvaluationDomain,
    classify_robust_seasonal_signals,
    compute_robust_seasonal_diagnostics,
    converge_disturbance_detectors,
    disturbance_detection_output_paths,
)
from deforestation_pipeline.disturbance_events import (
    DisturbanceEventCollection,
    DisturbanceEventConfig,
    materialize_persistent_disturbance_events,
)
from deforestation_pipeline.disturbance_evidence import (
    DISTURBANCE_EVIDENCE_SCHEMA_VERSION,
    validate_disturbance_evidence_payload,
)
from deforestation_pipeline.forest_screening import ForestScreeningDomain
from deforestation_pipeline.schemas import RasterGridSpec

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

ROBUST_ANOMALY_VISUALIZATION_LABEL = "escala raíz cuadrada; recorte superior p99"
ROBUST_ANOMALY_VISUALIZATION_PERCENTILE = 99.0


@dataclass(frozen=True, slots=True)
class DisturbancePeriodRaster:
    """Un período del cubo, publicado o usado sólo como soporte histórico."""

    period_id: str
    season: str
    start_date: date
    end_date_exclusive: date
    indices_raster: bytes
    observation_count_raster: bytes
    published: bool


@dataclass(frozen=True, slots=True)
class DisturbanceCcdcProvenance:
    """Disponibilidad del resumen escalar CCDC conocida por quien materializa."""

    status: Literal["scalar_summary_materialized", "unavailable_global_failure"]
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.status == "scalar_summary_materialized":
            if self.failure_code is not None:
                raise ValueError("ccdc_materialized_rejects_failure_code")
        elif self.failure_code is None or not self.failure_code.strip():
            raise ValueError("ccdc_unavailable_requires_failure_code")


@dataclass(frozen=True, slots=True)
class DisturbanceMaterialization:
    """Evidencia raster, tabular y por eventos, todavía sin atribución."""

    files: Mapping[str, bytes]
    metadata: Mapping[str, Any]
    events: DisturbanceEventCollection
    analysis_end_date: date


def materialize_disturbance_detection(
    *,
    periods: Sequence[DisturbancePeriodRaster],
    domain: ForestEvaluationDomain,
    screening_domain: ForestScreeningDomain,
    baseline_evidence_fraction: NDArray[Any],
    spatial_footprint: NDArray[Any],
    ccdc_result: CcdcBenchmarkResult,
    ccdc_provenance: DisturbanceCcdcProvenance,
    grid_spec: RasterGridSpec,
    config: DisturbanceDetectionConfig,
    event_config: DisturbanceEventConfig,
    scientific_parameters_hash: str,
    requested_start_year: int,
    requested_end_year: int,
    generated_at: datetime,
    png_dpi: int,
) -> DisturbanceMaterialization:
    """Apila rasters validados, ejecuta 14.2-14.5 y serializa cinco productos."""
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at_requires_timezone")
    if len(scientific_parameters_hash) != 64:
        raise ValueError("scientific_parameters_hash_invalid")
    footprint = np.asarray(spatial_footprint)
    if footprint.shape != (grid_spec.height, grid_spec.width):
        raise ValueError("spatial_footprint_shape_mismatch")
    if footprint.dtype != np.bool_:
        raise ValueError("spatial_footprint_dtype_invalid")
    if not np.any(footprint):
        raise ValueError("spatial_footprint_empty")
    if not np.array_equal(footprint, screening_domain.aoi_footprint):
        raise ValueError("screening_footprint_mismatch")
    ordered = tuple(sorted(periods, key=lambda item: (item.start_date, item.period_id)))
    if not ordered:
        raise ValueError("disturbance_periods_empty")
    if len({item.period_id for item in ordered}) != len(ordered):
        raise ValueError("disturbance_period_ids_duplicated")

    expected_indices = tuple(index.value for index in config.detection_indices)
    values: list[NDArray[np.float64]] = []
    observation_counts: list[NDArray[np.float64]] = []
    for period in ordered:
        values.append(
            _read_raster(
                period.indices_raster,
                grid_spec=grid_spec,
                expected_band_names=expected_indices,
                allow_band_subset=True,
            )
        )
        observation_counts.append(
            _read_single_band_raster(
                period.observation_count_raster,
                grid_spec=grid_spec,
            )
        )

    cube = np.stack(values, axis=0)
    counts = np.stack(observation_counts, axis=0)
    reference_indices = tuple(
        index
        for index, item in enumerate(ordered)
        if item.start_date >= config.reference_history_start_date
        and item.end_date_exclusive <= config.analysis_start_date
    )
    post_indices = tuple(
        index
        for index, item in enumerate(ordered)
        if item.end_date_exclusive > config.analysis_start_date and item.published
    )
    if not reference_indices:
        raise ValueError("reference_history_unavailable")
    if not post_indices:
        raise ValueError("post_cutoff_periods_unavailable")

    reference_items = tuple(ordered[index] for index in reference_indices)
    post_items = tuple(ordered[index] for index in post_indices)
    if not any(item.start_date >= config.analysis_start_date for item in post_items):
        raise ValueError("fully_post_cutoff_periods_unavailable")
    reference_selector = np.asarray(reference_indices, dtype=np.intp)
    post_selector = np.asarray(post_indices, dtype=np.intp)
    diagnostics = compute_robust_seasonal_diagnostics(
        reference_values=cube[reference_selector],
        reference_seasons=tuple(item.season for item in reference_items),
        reference_period_start_dates=tuple(item.start_date for item in reference_items),
        reference_period_end_dates_exclusive=tuple(
            item.end_date_exclusive for item in reference_items
        ),
        post_cutoff_values=cube[post_selector],
        post_cutoff_seasons=tuple(item.season for item in post_items),
        post_period_start_dates=tuple(item.start_date for item in post_items),
        post_period_end_dates_exclusive=tuple(item.end_date_exclusive for item in post_items),
        index_names=expected_indices,
        domain=domain,
        config=config,
    )
    robust = classify_robust_seasonal_signals(
        diagnostics=diagnostics,
        post_period_start_dates=tuple(item.start_date for item in post_items),
        post_period_end_dates_exclusive=tuple(item.end_date_exclusive for item in post_items),
        index_names=expected_indices,
        domain=domain,
        config=config,
    )
    convergence = converge_disturbance_detectors(
        robust_result=robust,
        ccdc_result=ccdc_result,
        post_period_start_dates=tuple(item.start_date for item in post_items),
        post_period_end_dates_exclusive=tuple(item.end_date_exclusive for item in post_items),
        config=config,
    )

    fraction = np.asarray(baseline_evidence_fraction, dtype=np.float64)
    if fraction.shape != (grid_spec.height, grid_spec.width):
        raise ValueError("baseline_evidence_fraction_shape_mismatch")
    post_counts = counts[post_selector]
    robust_z = _finite_max(diagnostics.standardized_magnitude, axes=(0, 1))
    ccdc_magnitude = _finite_max(ccdc_result.decrease_positive_magnitude, axes=(0,))
    ccdc_day_offset = _fractional_year_day_offsets(
        ccdc_result.first_post_cutoff_break_fractional_year,
        origin=config.analysis_start_date,
    )
    summary = np.stack(
        (
            convergence.state_code,
            convergence.reason_code,
            convergence.first_robust_signal_period_index,
            convergence.robust_maximum_disturbance_magnitude,
            robust.maximum_consecutive_signal_periods,
            convergence.robust_uncalibrated_disturbance_evidence_score,
            convergence.available_detector_count,
            convergence.agreeing_detector_count,
            robust.valid_post_cutoff_observation_count,
            convergence.quality_flags_bitmask,
        ),
        axis=0,
    ).astype(np.float32)
    summary[:, ~footprint] = np.nan
    recovery = np.full(fraction.shape, np.nan, dtype=np.float64)
    diagnostics_stack = np.stack(
        (
            robust.maximum_multi_index_support_count,
            robust_z,
            ccdc_day_offset,
            ccdc_magnitude,
            recovery,
            fraction,
        ),
        axis=0,
    ).astype(np.float32)
    diagnostics_stack[:, ~footprint] = np.nan

    paths = disturbance_detection_output_paths()
    summary_bytes = _write_raster(
        values=summary,
        band_names=DISTURBANCE_SUMMARY_BAND_NAMES,
        grid_spec=grid_spec,
        tags={
            "schema_version": DISTURBANCE_DETECTION_SCHEMA_VERSION,
            "product": "disturbance_summary",
            "grid_sha256": grid_spec.grid_sha256,
            "integer_semantics_preserved": "true",
            "aoi_footprint_applied": "true",
            "score_semantics": config.score_semantics,
            "final_assessment_generated": "false",
            "attribution_generated": "false",
        },
    )
    diagnostics_bytes = _write_raster(
        values=diagnostics_stack,
        band_names=DISTURBANCE_DIAGNOSTIC_BAND_NAMES,
        grid_spec=grid_spec,
        tags={
            "schema_version": DISTURBANCE_DETECTION_SCHEMA_VERSION,
            "product": "disturbance_diagnostics",
            "grid_sha256": grid_spec.grid_sha256,
            "recovery_indicator": "reserved_nodata",
            "aoi_footprint_applied": "true",
            "ccdc_raw_array_downloaded": "false",
            "final_assessment_generated": "false",
            "attribution_generated": "false",
        },
    )
    period_rows = _period_rows(
        items=post_items,
        post_values=cube[post_selector],
        post_counts=post_counts,
        period_signal=robust.period_signal_mask,
        excluded_flags=diagnostics.quality_flags_bitmask,
        spatial_footprint=footprint,
        config=config,
    )
    actual_analysis_end = max(item.end_date_exclusive for item in post_items)
    actual_analysis_end = actual_analysis_end.fromordinal(actual_analysis_end.toordinal() - 1)
    metadata = validate_disturbance_evidence_payload(
        _metadata_payload(
            config=config,
            scientific_parameters_hash=scientific_parameters_hash,
            requested_start_year=requested_start_year,
            requested_end_year=requested_end_year,
            reference_items=reference_items,
            post_items=post_items,
            convergence_state=convergence.state_code,
            convergence_reason=convergence.reason_code,
            quality_flags=convergence.quality_flags_bitmask,
            robust_any_signal=np.any(robust.period_signal_mask, axis=0),
            ccdc_supports_break=convergence.ccdc_supports_break,
            screening_domain=screening_domain,
            spatial_footprint=footprint,
            grid_spec=grid_spec,
            paths=paths,
            ccdc_provenance=ccdc_provenance,
            generated_at=generated_at,
            actual_analysis_end=actual_analysis_end,
        )
    )
    event_materialization = materialize_persistent_disturbance_events(
        persistent_mask=(
            convergence.state_code == np.uint8(DisturbanceStateCode.PERSISTENT_CANDIDATE)
        ),
        automated_forest_mask=screening_domain.automated_forest,
        first_anomalous_period_index=convergence.first_robust_signal_period_index,
        maximum_disturbance_magnitude=convergence.robust_maximum_disturbance_magnitude,
        disturbance_score=convergence.robust_uncalibrated_disturbance_evidence_score,
        convergence_reason_code=convergence.reason_code,
        available_detector_count=convergence.available_detector_count,
        agreeing_detector_count=convergence.agreeing_detector_count,
        ccdc_break_day_offset=ccdc_day_offset,
        quality_flags_bitmask=convergence.quality_flags_bitmask,
        post_period_ids=tuple(item.period_id for item in post_items),
        post_period_start_dates=tuple(item.start_date for item in post_items),
        post_period_end_dates_exclusive=tuple(item.end_date_exclusive for item in post_items),
        all_period_ids=tuple(item.period_id for item in ordered),
        all_period_seasons=tuple(item.season for item in ordered),
        all_period_start_dates=tuple(item.start_date for item in ordered),
        all_period_end_dates_exclusive=tuple(item.end_date_exclusive for item in ordered),
        index_names=expected_indices,
        index_cube=cube,
        grid_spec=grid_spec,
        config=event_config,
        generated_at=generated_at,
        scientific_parameters_hash=scientific_parameters_hash,
        png_dpi=png_dpi,
    )
    files = {
        paths["metadata"]: _json_bytes(metadata),
        paths["summary_raster"]: summary_bytes,
        paths["diagnostics_raster"]: diagnostics_bytes,
        paths["qa_figure"]: _render_qa(
            state=convergence.state_code,
            reason=convergence.reason_code,
            robust_z=robust_z,
            ccdc_magnitude=ccdc_magnitude,
            spatial_footprint=footprint,
            grid_spec=grid_spec,
            dpi=png_dpi,
        ),
        paths["period_summary_table"]: _csv_bytes(period_rows),
        **event_materialization.files,
    }
    if not set(paths.values()).issubset(files):
        raise RuntimeError("disturbance_artifact_contract_violation")
    return DisturbanceMaterialization(
        files=files,
        metadata=metadata,
        events=event_materialization.collection,
        analysis_end_date=actual_analysis_end,
    )


def _read_raster(
    content: bytes,
    *,
    grid_spec: RasterGridSpec,
    expected_band_names: tuple[str, ...],
    allow_band_subset: bool = False,
) -> NDArray[np.float64]:
    with MemoryFile(content) as memory:
        with memory.open() as dataset:
            _validate_grid(dataset, grid_spec)
            descriptions = tuple(item or "" for item in dataset.descriptions)
            if allow_band_subset:
                if len(descriptions) != len(set(descriptions)) or not set(
                    expected_band_names
                ).issubset(descriptions):
                    raise ValueError("raster_band_names_mismatch")
                band_indexes = tuple(descriptions.index(name) + 1 for name in expected_band_names)
                values = dataset.read(band_indexes).astype(np.float64)
            else:
                if descriptions != expected_band_names:
                    raise ValueError("raster_band_names_mismatch")
                values = dataset.read().astype(np.float64)
            nodata = float(dataset.nodata)
            if np.isinf(values).any():
                raise ValueError("raster_infinite_values")
    values[np.isclose(values, nodata) | ~np.isfinite(values)] = np.nan
    return np.asarray(values, dtype=np.float64)


def _read_single_band_raster(
    content: bytes,
    *,
    grid_spec: RasterGridSpec,
) -> NDArray[np.float64]:
    with MemoryFile(content) as memory:
        with memory.open() as dataset:
            _validate_grid(dataset, grid_spec)
            if dataset.count != 1:
                raise ValueError("observation_count_band_count_invalid")
            values = dataset.read().astype(np.float64)[0]
            nodata = float(dataset.nodata)
            if np.isinf(values).any():
                raise ValueError("raster_infinite_values")
    values[np.isclose(values, nodata) | ~np.isfinite(values)] = np.nan
    return np.asarray(values, dtype=np.float64)


def read_baseline_domain_inputs(
    *,
    files: Mapping[str, bytes],
    paths: Mapping[str, str],
    grid_spec: RasterGridSpec,
) -> tuple[NDArray[np.float64], ...]:
    """Lee los cuatro rasters forestales requeridos sobre la grilla declarada."""
    return tuple(
        _read_single_band_raster(files[paths[key]], grid_spec=grid_spec)
        for key in ("source_count", "consensus", "disagreement", "evidence_fraction")
    )


def ccdc_result_from_scalar_raster(
    *,
    content: bytes,
    domain: ForestEvaluationDomain,
    grid_spec: RasterGridSpec,
    config: DisturbanceDetectionConfig,
) -> CcdcBenchmarkResult:
    """Normaliza el resumen escalar descargable a la entrada pura de 14.5."""
    values = _read_raster(
        content,
        grid_spec=grid_spec,
        expected_band_names=CCDC_SCALAR_SUMMARY_BAND_NAMES,
    )
    by_name = dict(zip(CCDC_SCALAR_SUMMARY_BAND_NAMES, values, strict=True))
    spatial_shape = (grid_spec.height, grid_spec.width)
    total_float = by_name["ccdc_total_valid_observation_count"]
    finite_total = total_float[np.isfinite(total_float)]
    if (
        np.any(finite_total < 0)
        or np.any(finite_total > np.iinfo(np.uint16).max)
        or np.any(finite_total != np.floor(finite_total))
    ):
        raise ValueError("ccdc_total_observation_count_invalid")
    total = np.clip(np.nan_to_num(total_float, nan=0), 0, np.iinfo(np.uint16).max).astype(np.uint16)
    evaluable = np.asarray(domain.evaluable, dtype=np.bool_)
    for field_name in ("ccdc_fit_succeeded", "ccdc_has_post_cutoff_break"):
        finite_binary = by_name[field_name][np.isfinite(by_name[field_name])]
        if not np.all(np.isin(finite_binary, (0, 1))):
            raise ValueError(f"{field_name}_invalid")
    fit_eligible = evaluable & (total >= config.ccdc_benchmark.min_observations)
    fit_succeeded = (
        fit_eligible
        & np.isfinite(by_name["ccdc_fit_succeeded"])
        & (by_name["ccdc_fit_succeeded"] > 0)
    )
    first_break = by_name["ccdc_first_break_fractional_year"].astype(np.float64)
    has_break = (
        fit_succeeded
        & np.isfinite(first_break)
        & np.isfinite(by_name["ccdc_has_post_cutoff_break"])
        & (by_name["ccdc_has_post_cutoff_break"] > 0)
    )
    if np.any(has_break & (first_break < float(config.analysis_start_date.year))):
        raise ValueError("ccdc_break_precedes_analysis_start")
    first_break[~has_break] = np.nan
    pseudo = by_name["ccdc_change_pseudo_probability"].astype(np.float64)
    finite_pseudo = pseudo[np.isfinite(pseudo)]
    if np.any((finite_pseudo < 0) | (finite_pseudo > 1)):
        raise ValueError("ccdc_change_pseudo_probability_invalid")
    pseudo[~has_break] = np.nan
    first_obs_float = by_name["ccdc_first_break_segment_observation_count"]
    finite_first_obs = first_obs_float[np.isfinite(first_obs_float)]
    if (
        np.any(finite_first_obs < 0)
        or np.any(finite_first_obs > np.iinfo(np.uint16).max)
        or np.any(finite_first_obs != np.floor(finite_first_obs))
    ):
        raise ValueError("ccdc_segment_observation_count_invalid")
    first_obs = np.clip(
        np.nan_to_num(first_obs_float, nan=0),
        0,
        np.iinfo(np.uint16).max,
    ).astype(np.uint16)
    first_obs[~has_break] = 0
    signed = np.stack(
        tuple(
            by_name[f"ccdc_{index.value}_signed_magnitude"]
            for index in config.ccdc_benchmark.breakpoint_bands
        ),
        axis=0,
    ).astype(np.float64)
    signed[:, ~has_break] = np.nan
    first_segment = np.full(spatial_shape, -1, dtype=np.int32)
    first_segment[has_break] = 0
    flags = np.asarray(domain.quality_flags_bitmask, dtype=np.uint16).copy()
    insufficient = evaluable & ~fit_eligible
    flags[insufficient] |= np.uint16(1 << 9)
    failed = fit_eligible & ~fit_succeeded
    flags[failed] |= np.uint16(1 << 7)
    return CcdcBenchmarkResult(
        fit_eligible=_readonly(fit_eligible),
        fit_succeeded=_readonly(fit_succeeded),
        has_post_cutoff_break=_readonly(has_break),
        first_post_cutoff_break_segment_index=_readonly(first_segment),
        first_post_cutoff_break_fractional_year=_readonly(first_break),
        ccdc_breakpoint_pseudo_probability=_readonly(pseudo),
        raw_signed_magnitude=_readonly(signed),
        decrease_positive_magnitude=_readonly(-signed),
        first_break_segment_observation_count=_readonly(first_obs),
        total_valid_observation_count=_readonly(total),
        quality_flags_bitmask=_readonly(flags),
    )


def unavailable_ccdc_result(
    *,
    domain: ForestEvaluationDomain,
    index_count: int,
) -> CcdcBenchmarkResult:
    """Representa una falla global CCDC como indisponibilidad, no desacuerdo."""
    spatial_shape = np.asarray(domain.evaluable).shape
    false = np.zeros(spatial_shape, dtype=np.bool_)
    nan = np.full(spatial_shape, np.nan, dtype=np.float64)
    flags = np.asarray(domain.quality_flags_bitmask, dtype=np.uint16).copy()
    flags[np.asarray(domain.evaluable)] |= np.uint16(1 << 7)
    return CcdcBenchmarkResult(
        fit_eligible=_readonly(false),
        fit_succeeded=_readonly(false.copy()),
        has_post_cutoff_break=_readonly(false.copy()),
        first_post_cutoff_break_segment_index=_readonly(np.full(spatial_shape, -1, dtype=np.int32)),
        first_post_cutoff_break_fractional_year=_readonly(nan),
        ccdc_breakpoint_pseudo_probability=_readonly(nan.copy()),
        raw_signed_magnitude=_readonly(
            np.full((index_count, *spatial_shape), np.nan, dtype=np.float64)
        ),
        decrease_positive_magnitude=_readonly(
            np.full((index_count, *spatial_shape), np.nan, dtype=np.float64)
        ),
        first_break_segment_observation_count=_readonly(np.zeros(spatial_shape, dtype=np.uint16)),
        total_valid_observation_count=_readonly(np.zeros(spatial_shape, dtype=np.uint16)),
        quality_flags_bitmask=_readonly(flags),
    )


def _readonly(values: NDArray[Any]) -> NDArray[Any]:
    output = np.array(values, copy=True)
    output.setflags(write=False)
    return output


def _validate_grid(dataset: Any, grid_spec: RasterGridSpec) -> None:
    if dataset.crs != rasterio.crs.CRS.from_user_input(grid_spec.target_crs):
        raise ValueError("raster_grid_crs_mismatch")
    if dataset.width != grid_spec.width or dataset.height != grid_spec.height:
        raise ValueError("raster_grid_dimensions_mismatch")
    transform = tuple(float(value) for value in dataset.transform[:6])
    if not all(
        math.isclose(actual, expected, abs_tol=1e-8)
        for actual, expected in zip(transform, grid_spec.transform, strict=True)
    ):
        raise ValueError("raster_grid_transform_mismatch")
    if dataset.nodata is None or not math.isclose(
        float(dataset.nodata), grid_spec.nodata, abs_tol=1e-6
    ):
        raise ValueError("raster_grid_nodata_mismatch")


def _write_raster(
    *,
    values: NDArray[Any],
    band_names: tuple[str, ...],
    grid_spec: RasterGridSpec,
    tags: Mapping[str, str],
) -> bytes:
    output = np.asarray(values, dtype=np.float32).copy()
    output[~np.isfinite(output)] = np.float32(grid_spec.nodata)
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=grid_spec.width,
            height=grid_spec.height,
            count=len(band_names),
            dtype="float32",
            crs=grid_spec.target_crs,
            transform=rasterio.Affine(*grid_spec.transform),
            nodata=grid_spec.nodata,
            compress="deflate",
        ) as dataset:
            dataset.write(output)
            dataset.descriptions = band_names
            dataset.update_tags(**tags)
            for index, band_name in enumerate(band_names, start=1):
                dataset.update_tags(index, semantic_name=band_name)
        return bytes(memory.read())


def _finite_max(values: NDArray[Any], *, axes: tuple[int, ...]) -> NDArray[np.float64]:
    array = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(array)
    safe = np.where(finite, array, -np.inf)
    maximum = np.max(safe, axis=axes)
    maximum = np.asarray(maximum, dtype=np.float64)
    maximum[~np.any(finite, axis=axes)] = np.nan
    return maximum


def _fractional_year_day_offsets(
    values: NDArray[Any],
    *,
    origin: date,
) -> NDArray[np.float64]:
    result = np.full(np.asarray(values).shape, np.nan, dtype=np.float64)
    for index in np.ndindex(result.shape):
        value = float(np.asarray(values)[index])
        if not math.isfinite(value):
            continue
        year = math.floor(value)
        year_start = date(year, 1, 1)
        next_start = date(year + 1, 1, 1)
        day = round((value - year) * (next_start - year_start).days)
        break_date = year_start.fromordinal(year_start.toordinal() + day)
        result[index] = (break_date - origin).days
    return result


def _period_rows(
    *,
    items: tuple[DisturbancePeriodRaster, ...],
    post_values: NDArray[np.float64],
    post_counts: NDArray[np.float64],
    period_signal: NDArray[np.bool_],
    excluded_flags: NDArray[np.uint16],
    spatial_footprint: NDArray[np.bool_],
    config: DisturbanceDetectionConfig,
) -> tuple[dict[str, object], ...]:
    excluded_mask = 1 << 4
    rows: list[dict[str, object]] = []
    for period_index, item in enumerate(items):
        finite_pixels = np.isfinite(post_values[period_index]).any(axis=0) & spatial_footprint
        valid_count_mask = np.isfinite(post_counts[period_index]) & spatial_footprint
        valid_counts = post_counts[period_index][valid_count_mask]
        rows.append(
            {
                "period_index": period_index,
                "period_id": item.period_id,
                "start_date": item.start_date.isoformat(),
                "end_date_exclusive": item.end_date_exclusive.isoformat(),
                "valid_pixel_count": int(finite_pixels.sum()),
                "mean_valid_observation_count": (
                    round(float(valid_counts.mean()), 6) if valid_counts.size else ""
                ),
                "signal_pixel_count": int(
                    np.count_nonzero(period_signal[period_index] & spatial_footprint)
                ),
                "cutoff_straddling_excluded": bool(
                    np.any(excluded_flags[period_index][..., spatial_footprint] & excluded_mask)
                ),
                "signal_threshold": config.robust_seasonal.standardized_magnitude_threshold,
                "minimum_index_support_count": (config.robust_seasonal.minimum_index_support_count),
            }
        )
    return tuple(rows)


def _metadata_payload(
    *,
    config: DisturbanceDetectionConfig,
    scientific_parameters_hash: str,
    requested_start_year: int,
    requested_end_year: int,
    reference_items: tuple[DisturbancePeriodRaster, ...],
    post_items: tuple[DisturbancePeriodRaster, ...],
    convergence_state: NDArray[np.uint8],
    convergence_reason: NDArray[np.uint8],
    quality_flags: NDArray[np.uint16],
    robust_any_signal: NDArray[np.bool_],
    ccdc_supports_break: NDArray[np.bool_],
    screening_domain: ForestScreeningDomain,
    spatial_footprint: NDArray[np.bool_],
    grid_spec: RasterGridSpec,
    paths: Mapping[str, str],
    ccdc_provenance: DisturbanceCcdcProvenance,
    generated_at: datetime,
    actual_analysis_end: date,
) -> dict[str, Any]:
    evaluable_items = tuple(
        item for item in post_items if item.start_date >= config.analysis_start_date
    )
    cutoff_straddling_items = tuple(
        item
        for item in post_items
        if item.start_date < config.analysis_start_date < item.end_date_exclusive
    )
    signal_counts = _temporal_signal_counts_by_domain(
        robust_any_signal=robust_any_signal,
        ccdc_supports_break=ccdc_supports_break,
        convergence_state=convergence_state,
        screening_domain=screening_domain,
    )
    statement_fields = _screening_statement_fields(
        signal_counts=signal_counts,
        period_start_date=min(item.start_date for item in evaluable_items),
        period_end_date=actual_analysis_end,
    )
    return {
        "schema_version": DISTURBANCE_EVIDENCE_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "scientific_parameters_hash": scientific_parameters_hash,
        "requested_range": {
            "start_year": requested_start_year,
            "end_year": requested_end_year,
        },
        "reference_support": {
            "configured_start_date": config.reference_history_start_date.isoformat(),
            "actual_start_date": min(item.start_date for item in reference_items).isoformat(),
            "actual_end_date_exclusive": max(
                item.end_date_exclusive for item in reference_items
            ).isoformat(),
            "period_ids": [item.period_id for item in reference_items],
            "internal_period_ids": [
                item.period_id for item in reference_items if not item.published
            ],
            "minimum_observations_per_season": (
                config.robust_seasonal.minimum_reference_observations
            ),
        },
        "evaluable_range": {
            "start_date": min(item.start_date for item in evaluable_items).isoformat(),
            "end_date": actual_analysis_end.isoformat(),
            "period_ids": [item.period_id for item in evaluable_items],
            "excluded_cutoff_straddling_period_ids": [
                item.period_id for item in cutoff_straddling_items
            ],
        },
        "parameters": {
            "indices": [index.value for index in config.detection_indices],
            "robust_signal_threshold": (config.robust_seasonal.standardized_magnitude_threshold),
            "minimum_index_support_count": (config.robust_seasonal.minimum_index_support_count),
            "minimum_consecutive_signal_periods": (
                config.robust_seasonal.minimum_consecutive_signal_periods
            ),
            "baseline_disagreement_policy": config.baseline_disagreement_policy,
            "detector_disagreement_policy": (
                config.convergence.detector_disagreement_resolution_policy
            ),
        },
        "sources": {
            "robust": "published_and_internal_hls_seasonal_composites",
            "ccdc": "dense_masked_hlsl30_collection",
            "forest_domain": "forest_baseline_2020",
        },
        "ccdc": {
            "raw_array_downloaded": False,
            "scalar_summary_downloaded": (ccdc_provenance.status == "scalar_summary_materialized"),
            "source_product": config.ccdc_benchmark.source_product,
            "change_metric_semantics": "algorithmic_breakpoint_score_not_calibrated",
            "status": ccdc_provenance.status,
            **(
                {"failure_code": ccdc_provenance.failure_code}
                if ccdc_provenance.failure_code is not None
                else {}
            ),
        },
        "statistics": {
            "state_code": _code_counts(
                convergence_state,
                DisturbanceStateCode,
                spatial_footprint,
            ),
            "reason_code": _code_counts(
                convergence_reason,
                DetectorConvergenceReasonCode,
                spatial_footprint,
            ),
            "quality_flag_bit_counts": _quality_bit_counts(
                quality_flags,
                spatial_footprint,
            ),
        },
        "screening": {
            "primary_interpretation_domain": "automated_forest",
            "temporal_signal_scope": "entire_aoi",
            "baseline_metrics": screening_domain.metrics.model_dump(mode="json"),
            "temporal_signals_by_domain": signal_counts,
            **statement_fields,
            "final_assessment_generated": False,
        },
        "rasters": {
            "dtype": "float32",
            "nodata": grid_spec.nodata,
            "grid_sha256": grid_spec.grid_sha256,
            "summary_band_names": list(DISTURBANCE_SUMMARY_BAND_NAMES),
            "diagnostic_band_names": list(DISTURBANCE_DIAGNOSTIC_BAND_NAMES),
            "integer_semantics_preserved": True,
            "aoi_footprint_applied": True,
        },
        "assets": dict(paths),
        "final_assessment_generated": False,
        "attribution_generated": False,
        "limitations": [
            "Los estados representan señales de perturbación, no atribución de uso.",
            "El score robusto no está calibrado.",
            "La métrica de ruptura CCDC se conserva separada del score robusto.",
            "El indicador de recuperación está reservado y permanece sin datos.",
            "No se aplicó agregación espacial ni umbral de superficie.",
            "El resultado requiere revisión humana antes de cualquier conclusión.",
            (
                "La interpretación automática primaria se limita al bosque 2020 de alta "
                "confianza; las señales de los demás estratos se conservan para revisión."
            ),
        ],
    }


def _screening_statement_fields(
    *,
    signal_counts: Mapping[str, Mapping[str, int]],
    period_start_date: date,
    period_end_date: date,
) -> dict[str, object]:
    automated_forest_has_signal = any(
        count > 0 for count in signal_counts["automated_forest"].values()
    )
    return {
        "automatic_statement_policy": "only_when_no_signal_in_automated_forest",
        "automatic_statement_scope": "automated_forest_high_confidence",
        "automatic_statement_period": {
            "start_date": period_start_date.isoformat(),
            "end_date": period_end_date.isoformat(),
        },
        "automatic_statement_generated": not automated_forest_has_signal,
        **(
            {
                "automatic_statement": (
                    "sin cambio detectado en el bosque automático de alta confianza "
                    f"entre {period_start_date.isoformat()} y {period_end_date.isoformat()}"
                )
            }
            if not automated_forest_has_signal
            else {}
        ),
    }


def _temporal_signal_counts_by_domain(
    *,
    robust_any_signal: NDArray[np.bool_],
    ccdc_supports_break: NDArray[np.bool_],
    convergence_state: NDArray[np.uint8],
    screening_domain: ForestScreeningDomain,
) -> dict[str, dict[str, int]]:
    masks = {
        "automated_forest": screening_domain.automated_forest,
        "automated_nonforest": screening_domain.automated_nonforest,
        "review_required": screening_domain.review_required,
        "insufficient_data": screening_domain.insufficient_data,
    }
    state = np.asarray(convergence_state)
    robust = np.asarray(robust_any_signal, dtype=np.bool_)
    ccdc = np.asarray(ccdc_supports_break, dtype=np.bool_)
    return {
        name: {
            "any_robust_signal_pixel_count": int(np.count_nonzero(robust & mask)),
            "ccdc_break_pixel_count": int(np.count_nonzero(ccdc & mask)),
            "persistent_candidate_pixel_count": int(
                np.count_nonzero((state == DisturbanceStateCode.PERSISTENT_CANDIDATE) & mask)
            ),
            "transient_signal_pixel_count": int(
                np.count_nonzero((state == DisturbanceStateCode.TRANSIENT_SIGNAL) & mask)
            ),
            "detector_disagreement_pixel_count": int(
                np.count_nonzero((state == DisturbanceStateCode.DETECTOR_DISAGREEMENT) & mask)
            ),
        }
        for name, mask in masks.items()
    }


def _code_counts(
    values: NDArray[Any],
    enum_type: Any,
    spatial_footprint: NDArray[np.bool_],
) -> dict[str, int]:
    counts = Counter(int(value) for value in np.asarray(values)[spatial_footprint].flat)
    return {member.name: counts.get(int(member), 0) for member in enum_type}


def _quality_bit_counts(
    values: NDArray[np.uint16],
    spatial_footprint: NDArray[np.bool_],
) -> dict[str, int]:
    inside = np.asarray(values)[spatial_footprint]
    return {
        member.name: int(np.count_nonzero(inside & (1 << int(member))))
        for member in DisturbanceQualityBit
        if np.any(inside & (1 << int(member)))
    }


def _robust_anomaly_visualization(
    values: NDArray[Any],
    *,
    spatial_footprint: NDArray[np.bool_],
) -> tuple[np.ma.MaskedArray, float]:
    """Prepara sólo la visualización robusta sin alterar la evidencia científica."""
    anomaly = np.asarray(values, dtype=np.float64)
    if anomaly.shape != spatial_footprint.shape:
        raise ValueError("robust_visualization_footprint_shape_mismatch")
    panel = np.ma.masked_where(
        ~spatial_footprint,
        np.ma.masked_invalid(anomaly),
    )
    visible = anomaly[spatial_footprint & np.isfinite(anomaly) & (anomaly > 0)]
    upper_limit = (
        float(
            np.percentile(
                visible,
                ROBUST_ANOMALY_VISUALIZATION_PERCENTILE,
                method="lower",
            )
        )
        if visible.size
        else 1.0
    )
    if upper_limit <= 0:
        upper_limit = 1.0
    return panel, upper_limit


def _render_qa(
    *,
    state: NDArray[Any],
    reason: NDArray[Any],
    robust_z: NDArray[Any],
    ccdc_magnitude: NDArray[Any],
    spatial_footprint: NDArray[np.bool_],
    grid_spec: RasterGridSpec,
    dpi: int,
) -> bytes:
    figure, axes = plt.subplots(2, 2, figsize=(11, 8), constrained_layout=True)
    state_colors = ("#8c8c8c", "#2ca25f", "#fdae6b", "#de2d26", "#756bb1")
    state_cmap = ListedColormap(state_colors)
    state_panel = np.ma.masked_where(~spatial_footprint, state)
    axes[0, 0].imshow(
        state_panel,
        cmap=state_cmap,
        norm=BoundaryNorm(np.arange(-0.5, 5.5), state_cmap.N),
        interpolation="nearest",
    )
    axes[0, 0].set_title("Estado de perturbación")
    axes[0, 0].legend(
        handles=[
            Patch(facecolor=color, label=label)
            for color, label in zip(
                state_colors,
                (
                    "No evaluable",
                    "Estable observado",
                    "Señal transitoria",
                    "Candidato persistente",
                    "Desacuerdo",
                ),
                strict=True,
            )
        ],
        loc="upper left",
        fontsize=7,
        framealpha=0.85,
    )
    reason_panel = np.ma.masked_where(~spatial_footprint, reason)
    reason_image = axes[0, 1].imshow(reason_panel, cmap="tab20", interpolation="nearest")
    axes[0, 1].set_title("Motivo de convergencia/desacuerdo")
    figure.colorbar(reason_image, ax=axes[0, 1], shrink=0.75, label="Código de motivo")
    robust_panel, robust_upper_limit = _robust_anomaly_visualization(
        robust_z,
        spatial_footprint=spatial_footprint,
    )
    robust_image = axes[1, 0].imshow(
        robust_panel,
        cmap="magma",
        interpolation="nearest",
        norm=PowerNorm(
            gamma=0.5,
            vmin=0.0,
            vmax=robust_upper_limit,
            clip=True,
        ),
    )
    axes[1, 0].set_title(
        f"Máxima anomalía robusta estandarizada\n({ROBUST_ANOMALY_VISUALIZATION_LABEL})"
    )
    figure.colorbar(
        robust_image,
        ax=axes[1, 0],
        shrink=0.75,
        extend="max",
        label="Anomalía visualizada",
    )
    ccdc_panel = np.ma.masked_where(
        ~spatial_footprint,
        np.ma.masked_invalid(np.asarray(ccdc_magnitude, dtype=np.float64)),
    )
    ccdc_image = axes[1, 1].imshow(ccdc_panel, cmap="viridis", interpolation="nearest")
    axes[1, 1].set_title("Magnitud CCDC (escala separada)")
    figure.colorbar(ccdc_image, ax=axes[1, 1], shrink=0.75)
    for axis in axes.flat:
        axis.set_xlabel("x de grilla")
        axis.set_ylabel("y de grilla")
    figure.suptitle(
        "Evidencia preliminar de perturbaciones — sin atribución",
        fontsize=13,
    )
    figure.text(
        0.5,
        0.005,
        f"Grilla {grid_spec.grid_sha256[:12]} · los paneles continuos no son comparables",
        ha="center",
        fontsize=8,
    )
    return _figure_bytes(figure, dpi)


def _figure_bytes(figure: Figure, dpi: int) -> bytes:
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi)
    plt.close(figure)
    return buffer.getvalue()


def _csv_bytes(rows: tuple[dict[str, object], ...]) -> bytes:
    fieldnames = (
        "period_index",
        "period_id",
        "start_date",
        "end_date_exclusive",
        "valid_pixel_count",
        "mean_valid_observation_count",
        "signal_pixel_count",
        "cutoff_straddling_excluded",
        "signal_threshold",
        "minimum_index_support_count",
    )
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
