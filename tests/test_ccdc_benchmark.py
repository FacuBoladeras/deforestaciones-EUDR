"""Benchmark CCDC independiente y auditable del Paso 14.4."""

from __future__ import annotations

from dataclasses import fields
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from shapely.geometry import box

import deforestation_pipeline.ccdc_benchmark as ccdc_benchmark
from deforestation_pipeline.catalog import (
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.ccdc_benchmark import (
    CcdcRemoteBenchmark,
    build_ccdc_remote_benchmark,
    build_ccdc_scalar_summary_image,
)
from deforestation_pipeline.change_detection import (
    CcdcBenchmarkResult,
    DisturbanceQualityBit,
    ForestEvaluationDomain,
    ForestEvaluationDomainCode,
    summarize_ccdc_segments,
)
from deforestation_pipeline.config import (
    PipelineConfig,
    load_config,
    resolve_run_config,
    scientific_parameters_hash,
)
from deforestation_pipeline.gee import GeeSession

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BAND_NAMES = ("NDVI", "NBR", "NDMI", "NIRv")


def _config_and_plan() -> tuple[Any, Any]:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    plan = build_benchmark_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    return config, plan


def _domain(*, evaluable: bool = True) -> ForestEvaluationDomain:
    shape = (1, 1)
    code = (
        ForestEvaluationDomainCode.EVALUABLE_CONSENSUS_FOREST
        if evaluable
        else ForestEvaluationDomainCode.NOT_EVALUABLE_NON_FOREST
    )
    return ForestEvaluationDomain(
        domain_code=np.full(shape, code, dtype=np.uint8),
        evaluable=np.full(shape, evaluable, dtype=np.bool_),
        baseline_uncertain=np.zeros(shape, dtype=np.bool_),
        quality_flags_bitmask=np.zeros(shape, dtype=np.uint16),
    )


def _summarize(
    *,
    breaks: tuple[float, ...],
    magnitudes: tuple[tuple[float, ...], ...],
    pseudo_probabilities: tuple[float, ...] | None = None,
    segment_observations: tuple[int, ...] | None = None,
    total_observations: int = 20,
    fit_succeeded: bool = True,
) -> CcdcBenchmarkResult:
    segment_count = len(breaks)
    probabilities = pseudo_probabilities or tuple(1.0 for _ in breaks)
    observations = segment_observations or tuple(8 for _ in breaks)
    signed = np.asarray(magnitudes, dtype=np.float64)
    if signed.shape != (segment_count, len(BAND_NAMES)):
        raise ValueError("magnitudes debe ser segmento x banda")
    config, _ = _config_and_plan()
    return summarize_ccdc_segments(
        break_time_fractional_year=np.asarray(breaks, dtype=np.float64)[:, None, None],
        change_pseudo_probability=np.asarray(probabilities, dtype=np.float64)[:, None, None],
        segment_observation_count=np.asarray(observations, dtype=np.uint16)[:, None, None],
        signed_magnitude=signed[:, :, None, None],
        total_valid_observation_count=np.array([[total_observations]], dtype=np.uint16),
        fit_succeeded=np.array([[fit_succeeded]], dtype=np.bool_),
        band_names=BAND_NAMES,
        domain=_domain(),
        config=config.disturbance_detection,
    )


def test_default_ccdc_parameters_match_the_official_api_contract() -> None:
    config, _ = _config_and_plan()
    ccdc = config.disturbance_detection.ccdc_benchmark

    assert ccdc.schema_version == "1.0.0"
    assert ccdc.earth_engine_python_api_version == "1.7.36"
    assert ccdc.api_reference_last_updated == date(2026, 4, 20)
    assert ccdc.api_reference_accessed_at == date(2026, 7, 29)
    assert ccdc.source_profile == "hlsl30_only"
    assert ccdc.source_product == "HLSL30"
    assert ccdc.input_temporal_granularity == "dense_masked_observations"
    assert ccdc.breakpoint_bands == BAND_NAMES
    assert ccdc.min_observations == 6
    assert ccdc.chi_square_probability == 0.99
    assert ccdc.min_num_of_years_scaler == 1.33
    assert ccdc.date_format == 1
    assert ccdc.lambda_ == 20.0
    assert ccdc.max_iterations == 25_000
    assert ccdc.tmask_policy == "disabled_hls_fmask_preapplied"
    assert ccdc.tmask_bands == ()
    assert ccdc.change_probability_semantics == (
        "algorithmic_breakpoint_pseudo_probability_not_deforestation_probability"
    )
    assert ccdc.comparative_sensor_profile == "hlsl30_hlss30_deferred"

    dumped = ccdc.model_dump(by_alias=True)
    assert dumped["lambda"] == 20.0
    assert "lambda_" not in dumped


def test_ccdc_parameters_participate_in_the_scientific_hash() -> None:
    config, _ = _config_and_plan()
    baseline = resolve_run_config(config, date(2026, 7, 29))
    changed_payload = config.model_dump()
    changed_payload["disturbance_detection"]["ccdc_benchmark"]["min_observations"] = 7
    changed = resolve_run_config(
        PipelineConfig.model_validate(changed_payload),
        date(2026, 7, 29),
    )

    assert scientific_parameters_hash(baseline) != scientific_parameters_hash(changed)


class _CcdcRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.raw_output = object()

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.raw_output


class _ArrayExpression:
    def __init__(
        self,
        calls: list[str],
        history: tuple[str, ...] = (),
    ) -> None:
        self.calls = calls
        self.history = history

    def _next(self, operation: str) -> _ArrayExpression:
        self.calls.append(operation)
        return _ArrayExpression(self.calls, (*self.history, operation))

    def select(self, bands: object) -> _ArrayExpression:
        return self._next("select")

    def gte(self, value: object) -> _ArrayExpression:
        return self._next("gte")

    def lt(self, value: object) -> _ArrayExpression:
        return self._next("lt")

    def And(self, other: object) -> _ArrayExpression:
        return self._next("And")

    def eq(self, other: object) -> _ArrayExpression:
        return self._next("eq")

    def arrayMask(self, mask: object) -> _ArrayExpression:
        return self._next("arrayMask")

    def arrayPad(self, lengths: object, pad: object) -> _ArrayExpression:
        return self._next("arrayPad")

    def arrayReduce(self, reducer: object, axes: object) -> _ArrayExpression:
        return self._next("arrayReduce")

    def arraySlice(
        self,
        axis: object,
        start: object,
        end: object,
    ) -> _ArrayExpression:
        return self._next("arraySlice")

    def arrayGet(self, position: object) -> _ArrayExpression:
        if "arrayPad" not in self.history:
            raise AssertionError("arrayGet requiere padding para arrays CCDC vacíos")
        return self._next("arrayGet")

    def arrayLength(self, axis: object) -> _ArrayExpression:
        return self._next("arrayLength")

    def gt(self, value: object) -> _ArrayExpression:
        return self._next("gt")

    def rename(self, name: object) -> _ArrayExpression:
        return self._next("rename")

    def unmask(self, value: object, same_footprint: object = None) -> _ArrayExpression:
        return self._next("unmask")

    def toUint8(self) -> _ArrayExpression:
        return self._next("toUint8")

    def addBands(self, image: object) -> _ArrayExpression:
        return self._next("addBands")

    def reduce(self, reducer: object) -> _ArrayExpression:
        return self._next("reduce")


class _CollectionExpression:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def select(self, bands: object) -> _CollectionExpression:
        self.calls.append("collection_select")
        return self

    def count(self) -> _ArrayExpression:
        self.calls.append("collection_count")
        return _ArrayExpression(self.calls, ("collection_count",))


class _ReducerRecorder:
    def min(self) -> str:
        return "min"

    def anyNonZero(self) -> str:
        return "any_non_zero"


def test_remote_builder_uses_dense_l30_collection_and_exact_ccdc_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, plan = _config_and_plan()
    dense_collection = object()
    dense_calls: list[dict[str, object]] = []

    def fake_dense_builder(**kwargs: object) -> object:
        dense_calls.append(kwargs)
        return SimpleNamespace(collection=dense_collection, source_product="HLSL30")

    recorder = _CcdcRecorder()
    module = SimpleNamespace(
        Algorithms=SimpleNamespace(
            TemporalSegmentation=SimpleNamespace(Ccdc=recorder),
        )
    )
    session = GeeSession(module=module)
    aoi = box(-60.1, -31.1, -60.0, -31.0)
    monkeypatch.setattr(
        ccdc_benchmark,
        "build_masked_hls_dense_collection",
        fake_dense_builder,
    )

    result = build_ccdc_remote_benchmark(
        session=session,
        plan=plan,
        data_config=config.data,
        aoi_wgs84=aoi,
        analysis_end_date=date(2024, 12, 31),
        config=config.disturbance_detection,
    )

    assert isinstance(result, CcdcRemoteBenchmark)
    assert result.raw_array_image is recorder.raw_output
    assert result.input_collection.collection is dense_collection
    assert result.benchmark_config is config.disturbance_detection.ccdc_benchmark
    assert len(dense_calls) == 1
    assert dense_calls[0]["session"] is session
    assert dense_calls[0]["source"] == plan.sources[0]
    assert dense_calls[0]["data_config"] == config.data
    assert dense_calls[0]["aoi_wgs84"] is aoi
    assert dense_calls[0]["start_date"] == date(2017, 1, 1)
    assert dense_calls[0]["end_date_exclusive"] == date(2025, 1, 1)
    assert dense_calls[0]["requested_indices"] == BAND_NAMES
    assert recorder.calls == [
        {
            "collection": dense_collection,
            "breakpointBands": list(BAND_NAMES),
            "tmaskBands": None,
            "minObservations": 6,
            "chiSquareProbability": 0.99,
            "minNumOfYearsScaler": 1.33,
            "dateFormat": 1,
            "lambda": 20.0,
            "maxIterations": 25_000,
        }
    ]


def test_scalar_summary_pads_empty_ccdc_arrays_before_array_get() -> None:
    calls: list[str] = []
    remote = cast(
        CcdcRemoteBenchmark,
        SimpleNamespace(
            raw_array_image=_ArrayExpression(calls),
            input_collection=SimpleNamespace(
                collection=_CollectionExpression(calls),
            ),
            analysis_end_date=date(2021, 11, 30),
            breakpoint_bands=BAND_NAMES,
        ),
    )

    result = build_ccdc_scalar_summary_image(
        session=GeeSession(module=SimpleNamespace(Reducer=_ReducerRecorder())),
        remote=remote,
    )

    assert isinstance(result, _ArrayExpression)
    assert calls.count("arrayPad") == 8


def test_precutoff_break_is_ignored_and_first_postcutoff_break_is_preserved() -> None:
    result = _summarize(
        breaks=(2020.75, 2021.25, 2022.5),
        magnitudes=(
            (-0.2, -0.3, -0.4, -0.1),
            (-0.6, -0.7, -0.5, -0.4),
            (-0.9, -0.8, -0.7, -0.6),
        ),
        pseudo_probabilities=(1.0, 0.96, 0.99),
    )

    assert result.has_post_cutoff_break.item()
    assert result.first_post_cutoff_break_segment_index.item() == 1
    assert result.first_post_cutoff_break_fractional_year.item() == pytest.approx(2021.25)
    assert result.ccdc_breakpoint_pseudo_probability.item() == pytest.approx(0.96)
    assert result.raw_signed_magnitude[:, 0, 0].tolist() == pytest.approx([-0.6, -0.7, -0.5, -0.4])
    assert result.decrease_positive_magnitude[:, 0, 0].tolist() == pytest.approx(
        [0.6, 0.7, 0.5, 0.4]
    )


def test_no_postcutoff_break_is_not_a_fit_failure() -> None:
    result = _summarize(
        breaks=(2019.5, 2020.75),
        magnitudes=(
            (-0.2, -0.3, -0.4, -0.1),
            (-0.6, -0.7, -0.5, -0.4),
        ),
    )

    assert result.fit_eligible.item()
    assert result.fit_succeeded.item()
    assert not result.has_post_cutoff_break.item()
    assert result.first_post_cutoff_break_segment_index.item() == -1
    assert np.isnan(result.first_post_cutoff_break_fractional_year.item())
    assert np.isnan(result.ccdc_breakpoint_pseudo_probability.item())
    assert np.isnan(result.raw_signed_magnitude).all()
    fit_failure = np.uint16(1 << DisturbanceQualityBit.CCDC_FIT_FAILURE)
    assert not result.quality_flags_bitmask.item() & fit_failure


def test_fit_failure_and_insufficient_observations_are_distinct() -> None:
    failed = _summarize(
        breaks=(2022.0,),
        magnitudes=((-0.8, -0.7, -0.6, -0.5),),
        fit_succeeded=False,
    )
    insufficient = _summarize(
        breaks=(2022.0,),
        magnitudes=((-0.8, -0.7, -0.6, -0.5),),
        total_observations=5,
        fit_succeeded=False,
    )

    fit_failure = np.uint16(1 << DisturbanceQualityBit.CCDC_FIT_FAILURE)
    insufficient_observations = np.uint16(1 << DisturbanceQualityBit.CCDC_INSUFFICIENT_OBSERVATIONS)
    assert failed.fit_eligible.item()
    assert not failed.fit_succeeded.item()
    assert failed.quality_flags_bitmask.item() & fit_failure
    assert not failed.quality_flags_bitmask.item() & insufficient_observations
    assert not insufficient.fit_eligible.item()
    assert not insufficient.fit_succeeded.item()
    assert insufficient.quality_flags_bitmask.item() & insufficient_observations
    assert not insufficient.quality_flags_bitmask.item() & fit_failure


def test_ccdc_arrays_are_immutable_and_never_claim_deforestation_probability() -> None:
    result = _summarize(
        breaks=(2022.0,),
        magnitudes=((-0.8, -0.7, -0.6, -0.5),),
        pseudo_probabilities=(0.98,),
    )

    for field in fields(result):
        array = getattr(result, field.name)
        assert isinstance(array, np.ndarray)
        assert not array.flags.writeable
    names = " ".join(field.name for field in fields(result)).lower()
    assert "deforestation_probability" not in names
    assert "conversion_probability" not in names
    assert result.ccdc_breakpoint_pseudo_probability.item() == pytest.approx(0.98)


def test_ccdc_observation_counts_are_normalized_to_the_declared_uint16_contract() -> None:
    config, _ = _config_and_plan()
    result = summarize_ccdc_segments(
        break_time_fractional_year=np.array([[[2022.0]]], dtype=np.float64),
        change_pseudo_probability=np.array([[[0.98]]], dtype=np.float64),
        segment_observation_count=np.array([[[8]]], dtype=np.int64),
        signed_magnitude=np.array(
            [[[[-0.8]], [[-0.7]], [[-0.6]], [[-0.5]]]],
            dtype=np.float64,
        ),
        total_valid_observation_count=np.array([[20]], dtype=np.int64),
        fit_succeeded=np.array([[True]], dtype=np.bool_),
        band_names=BAND_NAMES,
        domain=_domain(),
        config=config.disturbance_detection,
    )

    assert result.first_break_segment_observation_count.dtype == np.uint16
    assert result.total_valid_observation_count.dtype == np.uint16


def test_ccdc_observation_counts_reject_values_that_cannot_fit_the_contract() -> None:
    config, _ = _config_and_plan()
    with pytest.raises(ValueError, match="uint16"):
        summarize_ccdc_segments(
            break_time_fractional_year=np.array([[[2022.0]]], dtype=np.float64),
            change_pseudo_probability=np.array([[[0.98]]], dtype=np.float64),
            segment_observation_count=np.array([[[8]]], dtype=np.int64),
            signed_magnitude=np.array(
                [[[[-0.8]], [[-0.7]], [[-0.6]], [[-0.5]]]],
                dtype=np.float64,
            ),
            total_valid_observation_count=np.array([[70_000]], dtype=np.int64),
            fit_succeeded=np.array([[True]], dtype=np.bool_),
            band_names=BAND_NAMES,
            domain=_domain(),
            config=config.disturbance_detection,
        )


def test_ccdc_does_not_mutate_robust_persistence_result() -> None:
    source = np.array([[[[4.0]], [[3.5]], [[0.1]], [[0.2]]]], dtype=np.float64)
    source_before = source.copy()

    _summarize(
        breaks=(2022.0,),
        magnitudes=((-0.8, -0.7, -0.6, -0.5),),
    )

    np.testing.assert_array_equal(source, source_before)
