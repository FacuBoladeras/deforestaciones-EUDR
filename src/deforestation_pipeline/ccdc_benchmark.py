"""Frontera GEE perezosa para el benchmark CCDC independiente."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.catalog import BenchmarkSourcePlan
from deforestation_pipeline.change_detection import (
    CcdcBenchmarkConfig,
    DisturbanceDetectionConfig,
)
from deforestation_pipeline.config import DataConfig
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import (
    HlsDenseObservationCollection,
    build_masked_hls_dense_collection,
)

CCDC_SCALAR_SUMMARY_BAND_NAMES = (
    "ccdc_fit_succeeded",
    "ccdc_has_post_cutoff_break",
    "ccdc_first_break_fractional_year",
    "ccdc_change_pseudo_probability",
    "ccdc_first_break_segment_observation_count",
    "ccdc_total_valid_observation_count",
    "ccdc_NDVI_signed_magnitude",
    "ccdc_NBR_signed_magnitude",
    "ccdc_NDMI_signed_magnitude",
    "ccdc_NIRv_signed_magnitude",
)


class CcdcBenchmarkError(RuntimeError):
    """Fallo sanitizado al construir el benchmark remoto."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Benchmark CCDC fallido: {code}")


@dataclass(frozen=True, slots=True)
class CcdcRemoteBenchmark:
    """Salida array cruda de CCDC y procedencia de su colección perezosa."""

    raw_array_image: Any = field(repr=False, compare=False)
    input_collection: HlsDenseObservationCollection = field(repr=False, compare=False)
    reference_history_start_date: date
    analysis_end_date: date
    collection_end_date_exclusive: date
    breakpoint_bands: tuple[str, ...]
    change_probability_semantics: str
    benchmark_config: CcdcBenchmarkConfig


def build_ccdc_remote_benchmark(
    *,
    session: GeeSession,
    plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    aoi_wgs84: BaseGeometry,
    analysis_end_date: date,
    config: DisturbanceDetectionConfig,
) -> CcdcRemoteBenchmark:
    """Invoca CCDC sobre HLSL30 denso sin evaluar ni descargar el resultado."""
    if analysis_end_date < config.analysis_start_date:
        raise CcdcBenchmarkError("analysis_end_precedes_post_cutoff_period")
    ccdc = config.ccdc_benchmark
    l30_sources = tuple(source for source in plan.sources if source.product == "HLSL30")
    if len(l30_sources) != 1:
        raise CcdcBenchmarkError("hlsl30_source_profile_unavailable")
    if ccdc.source_profile != "hlsl30_only" or ccdc.source_product != "HLSL30":
        raise CcdcBenchmarkError("unsupported_sensor_profile")

    end_date_exclusive = analysis_end_date + timedelta(days=1)
    input_collection = build_masked_hls_dense_collection(
        session=session,
        source=l30_sources[0],
        data_config=data_config,
        aoi_wgs84=aoi_wgs84,
        start_date=config.reference_history_start_date,
        end_date_exclusive=end_date_exclusive,
        requested_indices=tuple(index.value for index in ccdc.breakpoint_bands),
    )
    arguments: dict[str, object] = {
        "collection": input_collection.collection,
        "breakpointBands": [index.value for index in ccdc.breakpoint_bands],
        "tmaskBands": None,
        "minObservations": ccdc.min_observations,
        "chiSquareProbability": ccdc.chi_square_probability,
        "minNumOfYearsScaler": ccdc.min_num_of_years_scaler,
        "dateFormat": ccdc.date_format,
        "lambda": ccdc.lambda_,
        "maxIterations": ccdc.max_iterations,
    }
    try:
        raw_array_image = session.module.Algorithms.TemporalSegmentation.Ccdc(**arguments)
    except Exception:
        raise CcdcBenchmarkError("remote_algorithm_invocation_failed") from None

    return CcdcRemoteBenchmark(
        raw_array_image=raw_array_image,
        input_collection=input_collection,
        reference_history_start_date=config.reference_history_start_date,
        analysis_end_date=analysis_end_date,
        collection_end_date_exclusive=end_date_exclusive,
        breakpoint_bands=tuple(index.value for index in ccdc.breakpoint_bands),
        change_probability_semantics=ccdc.change_probability_semantics,
        benchmark_config=ccdc,
    )


def build_ccdc_scalar_summary_image(
    *,
    session: GeeSession,
    remote: CcdcRemoteBenchmark,
) -> Any:
    """Reduce arrays CCDC server-side; nunca descarga las bandas array crudas."""
    try:
        return _build_ccdc_scalar_summary_image(session=session, remote=remote)
    except CcdcBenchmarkError:
        raise
    except Exception:
        raise CcdcBenchmarkError("scalar_summary_derivation_failed") from None


def _build_ccdc_scalar_summary_image(
    *,
    session: GeeSession,
    remote: CcdcRemoteBenchmark,
) -> Any:
    module = session.module
    raw = remote.raw_array_image
    analysis_end_fractional = _date_as_fractional_year(remote.analysis_end_date + timedelta(days=1))
    break_times = raw.select("tBreak")
    # El inicio real de análisis de cambios es fijo y posterior al corte EUDR.
    post_cutoff = break_times.gte(2021.0).And(break_times.lt(analysis_end_fractional))
    selected_breaks = break_times.arrayMask(post_cutoff)
    first_break = (
        selected_breaks.arrayPad([1], analysis_end_fractional)
        .arrayReduce(module.Reducer.min(), [0])
        .arrayGet([0])
    )
    has_break = (
        post_cutoff.arrayPad([1], 0)
        .arrayReduce(module.Reducer.anyNonZero(), [0])
        .arrayGet([0])
        .unmask(0)
    )
    first_selector = post_cutoff.And(break_times.eq(first_break))

    def first_scalar(array_band: str, output_band: str) -> Any:
        return (
            raw.select(array_band)
            .arrayMask(first_selector)
            .arrayPad([1], 0)
            .arraySlice(0, 0, 1)
            .arrayGet([0])
            .rename(output_band)
        )

    fit_succeeded = raw.select("tStart").arrayLength(0).gt(0).rename("ccdc_fit_succeeded").toUint8()
    summary = fit_succeeded.addBands(has_break.rename("ccdc_has_post_cutoff_break").toUint8())
    summary = summary.addBands(first_break.rename("ccdc_first_break_fractional_year"))
    summary = summary.addBands(first_scalar("changeProb", "ccdc_change_pseudo_probability"))
    summary = summary.addBands(first_scalar("numObs", "ccdc_first_break_segment_observation_count"))
    total_valid = (
        remote.input_collection.collection.select(list(remote.breakpoint_bands))
        .count()
        .reduce(module.Reducer.min())
        .rename("ccdc_total_valid_observation_count")
    )
    summary = summary.addBands(total_valid)
    for band_name in remote.breakpoint_bands:
        summary = summary.addBands(
            first_scalar(
                f"{band_name}_magnitude",
                f"ccdc_{band_name}_signed_magnitude",
            )
        )
    # El resumen escalar es interno: cero permite descargar incluso cuando no
    # hay rupturas; ``ccdc_has_post_cutoff_break`` gobierna luego el nodata.
    return summary.unmask(0, False)


def _date_as_fractional_year(value: date) -> float:
    year_start = date(value.year, 1, 1)
    next_year_start = date(value.year + 1, 1, 1)
    return value.year + (value - year_start).days / (next_year_start - year_start).days
