"""Reglas auditables de señal y persistencia del Paso 14.3."""

from __future__ import annotations

from dataclasses import fields
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from deforestation_pipeline.change_detection import (
    DisturbanceDetectionConfig,
    DisturbanceQualityBit,
    DisturbanceStateCode,
    ForestEvaluationDomain,
    ForestEvaluationDomainCode,
    RobustSeasonalDiagnostics,
    RobustSeasonalSignalResult,
    classify_robust_seasonal_signals,
)
from deforestation_pipeline.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _domain(*, evaluable: bool = True, uncertain: bool = False) -> ForestEvaluationDomain:
    shape = (1, 1)
    evaluable_array = np.full(shape, evaluable, dtype=np.bool_)
    uncertain_array = np.full(shape, uncertain, dtype=np.bool_)
    if not evaluable:
        code = ForestEvaluationDomainCode.NOT_EVALUABLE_NON_FOREST
    elif uncertain:
        code = ForestEvaluationDomainCode.EVALUABLE_BASELINE_UNCERTAIN
    else:
        code = ForestEvaluationDomainCode.EVALUABLE_CONSENSUS_FOREST
    domain_code = np.full(shape, code, dtype=np.uint8)
    flags = np.zeros(shape, dtype=np.uint16)
    if uncertain:
        flags |= np.uint16(1 << DisturbanceQualityBit.BASELINE_UNCERTAIN)
    return ForestEvaluationDomain(
        domain_code=domain_code,
        evaluable=evaluable_array,
        baseline_uncertain=uncertain_array,
        quality_flags_bitmask=flags,
    )


def _diagnostics(
    standardized_magnitude: np.ndarray,
    *,
    quality_flags: np.ndarray | None = None,
) -> RobustSeasonalDiagnostics:
    values = np.asarray(standardized_magnitude, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError("el helper espera período x índice")
    shaped = values[:, :, np.newaxis, np.newaxis]
    flags = (
        np.zeros(shaped.shape, dtype=np.uint16)
        if quality_flags is None
        else np.asarray(quality_flags, dtype=np.uint16)
    )
    return RobustSeasonalDiagnostics(
        reference_center=np.zeros(shaped.shape, dtype=np.float64),
        robust_scale=np.ones(shaped.shape, dtype=np.float64),
        raw_directional_delta=shaped.copy(),
        standardized_magnitude=shaped,
        reference_valid_count=np.full(shaped.shape, 3, dtype=np.uint16),
        quality_flags_bitmask=flags,
    )


def _classify(
    standardized_magnitude: np.ndarray,
    *,
    domain: ForestEvaluationDomain | None = None,
) -> RobustSeasonalSignalResult:
    values = np.asarray(standardized_magnitude, dtype=np.float64)
    period_count = values.shape[0]
    available_intervals = (
        (date(2021, 3, 1), date(2021, 6, 1)),
        (date(2021, 6, 1), date(2021, 9, 1)),
        (date(2021, 9, 1), date(2021, 12, 1)),
        (date(2021, 12, 1), date(2022, 3, 1)),
    )
    starts = tuple(start for start, _ in available_intervals[:period_count])
    ends = tuple(end for _, end in available_intervals[:period_count])
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    return classify_robust_seasonal_signals(
        diagnostics=_diagnostics(values),
        post_period_start_dates=starts,
        post_period_end_dates_exclusive=ends,
        index_names=("NDVI", "NBR", "NDMI", "NIRv"),
        domain=domain or _domain(),
        config=config.disturbance_detection,
    )


def test_default_rule_is_explicit_versioned_and_conservative() -> None:
    detection = load_config(PROJECT_ROOT / "configs" / "default.yml").disturbance_detection
    robust = detection.robust_seasonal

    assert robust.schema_version == "1.1.0"
    assert robust.standardized_magnitude_threshold == 3.0
    assert robust.minimum_index_support_count == 2
    assert robust.minimum_consecutive_signal_periods == 2
    assert robust.minimum_valid_post_cutoff_periods == 2
    assert robust.missing_period_streak_policy == "break_without_recovery_inference"
    assert robust.long_gap_minimum_consecutive_missing_periods == 2
    assert robust.threshold_calibration_status == (
        "conservative_benchmark_requires_independent_validation"
    )
    assert robust.anomaly_threshold_applied is True
    assert robust.persistence_rule_applied is True


def test_stable_observations_remain_stable() -> None:
    result = _classify(
        np.array(
            [
                [0.2, 0.1, 0.3, 0.0],
                [0.5, 0.4, 0.2, 0.1],
                [0.1, 0.0, 0.2, 0.3],
            ]
        )
    )

    assert result.state_code.item() == DisturbanceStateCode.STABLE_OBSERVED
    assert result.first_signal_period_index.item() == -1
    assert result.maximum_consecutive_signal_periods.item() == 0
    assert result.maximum_multi_index_support_count.item() == 0
    assert result.valid_post_cutoff_observation_count.item() == 3
    assert result.maximum_disturbance_magnitude.item() == pytest.approx(0.5)
    assert result.uncalibrated_disturbance_evidence_score.item() == pytest.approx(0.5)


def test_isolated_multi_index_signal_is_transient() -> None:
    result = _classify(
        np.array(
            [
                [0.2, 0.1, 0.3, 0.0],
                [4.0, 3.2, 0.2, 0.1],
                [0.1, 0.0, 0.2, 0.3],
            ]
        )
    )

    assert result.state_code.item() == DisturbanceStateCode.TRANSIENT_SIGNAL
    assert result.first_signal_period_index.item() == 1
    assert result.maximum_consecutive_signal_periods.item() == 1
    assert result.maximum_multi_index_support_count.item() == 2
    assert result.period_signal_mask[:, 0, 0].tolist() == [False, True, False]


def test_two_consecutive_supported_signals_form_persistent_candidate() -> None:
    result = _classify(
        np.array(
            [
                [3.0, 3.1, 0.3, 0.0],
                [4.0, 3.2, 0.2, 0.1],
                [0.1, 0.0, 0.2, 0.3],
            ]
        )
    )

    assert result.state_code.item() == DisturbanceStateCode.PERSISTENT_CANDIDATE
    assert result.first_signal_period_index.item() == 0
    assert result.maximum_consecutive_signal_periods.item() == 2
    assert result.maximum_multi_index_support_count.item() == 2
    assert DisturbanceStateCode.DETECTOR_DISAGREEMENT not in result.state_code


def test_cutoff_straddling_period_is_excluded_without_shifting_period_index() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    diagnostics = _diagnostics(
        np.array(
            [
                [9.0, 8.0, 0.2, 0.1],
                [4.0, 3.2, 0.2, 0.1],
                [4.1, 3.4, 0.2, 0.1],
            ]
        )
    )

    result = classify_robust_seasonal_signals(
        diagnostics=diagnostics,
        post_period_start_dates=(
            date(2020, 12, 1),
            date(2021, 3, 1),
            date(2021, 6, 1),
        ),
        post_period_end_dates_exclusive=(
            date(2021, 3, 1),
            date(2021, 6, 1),
            date(2021, 9, 1),
        ),
        index_names=("NDVI", "NBR", "NDMI", "NIRv"),
        domain=_domain(),
        config=config.disturbance_detection,
    )

    excluded_bit = np.uint16(1 << DisturbanceQualityBit.CUTOFF_STRADDLING_PERIOD_EXCLUDED)
    assert result.period_signal_mask[:, 0, 0].tolist() == [False, True, True]
    assert result.first_signal_period_index.item() == 1
    assert result.maximum_consecutive_signal_periods.item() == 2
    assert result.valid_post_cutoff_observation_count.item() == 2
    assert result.maximum_disturbance_magnitude.item() == pytest.approx(4.1)
    assert result.quality_flags_bitmask.item() & excluded_bit
    assert result.state_code.item() == DisturbanceStateCode.PERSISTENT_CANDIDATE


def test_one_index_above_threshold_is_not_a_signal() -> None:
    result = _classify(
        np.array(
            [
                [4.0, 0.1, 0.3, 0.0],
                [4.5, 0.4, 0.2, 0.1],
            ]
        )
    )

    assert result.state_code.item() == DisturbanceStateCode.STABLE_OBSERVED
    assert result.maximum_multi_index_support_count.item() == 1
    assert not result.period_signal_mask.any()


def test_isolated_nan_period_breaks_streak_without_claiming_long_gap_or_recovery() -> None:
    result = _classify(
        np.array(
            [
                [4.0, 3.2, 0.2, 0.1],
                [np.nan, np.nan, np.nan, np.nan],
                [4.1, 3.4, 0.2, 0.1],
            ]
        )
    )

    assert result.state_code.item() == DisturbanceStateCode.TRANSIENT_SIGNAL
    assert result.maximum_consecutive_signal_periods.item() == 1
    assert result.valid_post_cutoff_observation_count.item() == 2
    long_gap_bit = np.uint16(1 << DisturbanceQualityBit.LONG_OBSERVATION_GAP)
    assert not result.quality_flags_bitmask.item() & long_gap_bit


def test_two_missing_periods_set_long_gap_without_interpolation() -> None:
    result = _classify(
        np.array(
            [
                [4.0, 3.2, 0.2, 0.1],
                [np.nan, np.nan, np.nan, np.nan],
                [np.nan, np.nan, np.nan, np.nan],
                [4.1, 3.4, 0.2, 0.1],
            ]
        )
    )

    long_gap_bit = np.uint16(1 << DisturbanceQualityBit.LONG_OBSERVATION_GAP)
    assert result.state_code.item() == DisturbanceStateCode.TRANSIENT_SIGNAL
    assert result.quality_flags_bitmask.item() & long_gap_bit


def test_uncertain_forest_domain_is_evaluated_and_flagged() -> None:
    result = _classify(
        np.array(
            [
                [4.0, 3.2, 0.2, 0.1],
                [4.1, 3.4, 0.2, 0.1],
            ]
        ),
        domain=_domain(uncertain=True),
    )

    baseline_bit = np.uint16(1 << DisturbanceQualityBit.BASELINE_UNCERTAIN)
    assert result.state_code.item() == DisturbanceStateCode.PERSISTENT_CANDIDATE
    assert result.quality_flags_bitmask.item() & baseline_bit


def test_insufficient_valid_post_cutoff_periods_are_not_evaluable_and_flagged() -> None:
    result = _classify(
        np.array(
            [
                [4.0, 3.2, 0.2, 0.1],
                [np.nan, np.nan, np.nan, np.nan],
            ]
        )
    )

    insufficient_bit = np.uint16(1 << DisturbanceQualityBit.INSUFFICIENT_POST_CUTOFF_OBSERVATIONS)
    assert result.state_code.item() == DisturbanceStateCode.NOT_EVALUABLE
    assert result.valid_post_cutoff_observation_count.item() == 1
    assert result.quality_flags_bitmask.item() & insufficient_bit


def test_non_forest_domain_remains_not_evaluable_without_temporal_claim() -> None:
    result = _classify(
        np.array(
            [
                [4.0, 3.2, 0.2, 0.1],
                [4.1, 3.4, 0.2, 0.1],
            ]
        ),
        domain=_domain(evaluable=False),
    )

    insufficient_bit = np.uint16(1 << DisturbanceQualityBit.INSUFFICIENT_POST_CUTOFF_OBSERVATIONS)
    assert result.state_code.item() == DisturbanceStateCode.NOT_EVALUABLE
    assert not result.quality_flags_bitmask.item() & insufficient_bit


def test_rejects_non_chronological_or_overlapping_periods() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    diagnostics = _diagnostics(np.zeros((2, 4)))

    with pytest.raises(ValueError, match="ordenados y no superpuestos"):
        classify_robust_seasonal_signals(
            diagnostics=diagnostics,
            post_period_start_dates=(date(2021, 6, 1), date(2021, 3, 1)),
            post_period_end_dates_exclusive=(date(2021, 9, 1), date(2021, 6, 1)),
            index_names=("NDVI", "NBR", "NDMI", "NIRv"),
            domain=_domain(),
            config=config.disturbance_detection,
        )


def test_rejects_omitted_period_instead_of_inferring_consecutive_signal() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    diagnostics = _diagnostics(
        np.array(
            [
                [4.0, 3.2, 0.2, 0.1],
                [4.1, 3.4, 0.2, 0.1],
            ]
        )
    )

    with pytest.raises(ValueError, match="continuos"):
        classify_robust_seasonal_signals(
            diagnostics=diagnostics,
            post_period_start_dates=(date(2021, 3, 1), date(2021, 9, 1)),
            post_period_end_dates_exclusive=(date(2021, 6, 1), date(2021, 12, 1)),
            index_names=("NDVI", "NBR", "NDMI", "NIRv"),
            domain=_domain(),
            config=config.disturbance_detection,
        )


def test_result_arrays_are_immutable_and_use_no_attribution_language() -> None:
    result = _classify(
        np.array(
            [
                [4.0, 3.2, 0.2, 0.1],
                [4.1, 3.4, 0.2, 0.1],
            ]
        )
    )

    for field in fields(result):
        array = getattr(result, field.name)
        assert isinstance(array, np.ndarray)
        assert not array.flags.writeable
    names = " ".join(field.name for field in fields(result)).lower()
    assert "probability" not in names
    assert "deforestation" not in names
    assert "conversion" not in names


def test_rule_rejects_support_or_persistence_incompatible_with_contract() -> None:
    detection = load_config(
        PROJECT_ROOT / "configs" / "default.yml"
    ).disturbance_detection.model_dump()

    too_many_indices = detection.copy()
    too_many_indices["robust_seasonal"] = {
        **detection["robust_seasonal"],
        "minimum_index_support_count": 5,
    }
    with pytest.raises(ValidationError, match="minimum_index_support_count"):
        DisturbanceDetectionConfig.model_validate(too_many_indices)

    too_few_valid_periods = detection.copy()
    too_few_valid_periods["robust_seasonal"] = {
        **detection["robust_seasonal"],
        "minimum_valid_post_cutoff_periods": 1,
    }
    with pytest.raises(ValidationError, match="minimum_valid_post_cutoff_periods"):
        DisturbanceDetectionConfig.model_validate(too_few_valid_periods)
