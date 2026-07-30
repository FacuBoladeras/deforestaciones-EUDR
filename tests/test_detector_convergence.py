from __future__ import annotations

from dataclasses import fields, replace
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from deforestation_pipeline.change_detection import (
    CcdcBenchmarkResult,
    DetectorConvergenceReasonCode,
    DetectorConvergenceResult,
    DisturbanceQualityBit,
    DisturbanceStateCode,
    RobustSeasonalSignalResult,
    converge_disturbance_detectors,
)
from deforestation_pipeline.config import load_config, parameters_hash

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PERIOD_STARTS = (
    date(2021, 3, 1),
    date(2021, 6, 1),
    date(2021, 9, 1),
)
PERIOD_ENDS = (
    date(2021, 6, 1),
    date(2021, 9, 1),
    date(2021, 12, 1),
)


def _robust_result(
    state: DisturbanceStateCode,
    *,
    flags: int = 0,
    dtype: type[np.uint8] | type[np.int16] = np.uint8,
) -> RobustSeasonalSignalResult:
    signal_by_state = {
        DisturbanceStateCode.NOT_EVALUABLE: (False, False, False),
        DisturbanceStateCode.STABLE_OBSERVED: (False, False, False),
        DisturbanceStateCode.TRANSIENT_SIGNAL: (True, False, False),
        DisturbanceStateCode.PERSISTENT_CANDIDATE: (True, True, False),
    }
    signals = np.asarray(signal_by_state[state], dtype=np.bool_)[:, None, None]
    first = -1 if not signals.any() else int(np.argmax(signals[:, 0, 0]))
    streak = 2 if state == DisturbanceStateCode.PERSISTENT_CANDIDATE else int(signals.any())
    magnitude = (
        np.nan if state == DisturbanceStateCode.NOT_EVALUABLE else (4.5 if signals.any() else 0.4)
    )
    return RobustSeasonalSignalResult(
        period_signal_mask=signals,
        period_multi_index_support_count=np.where(signals, 2, 0).astype(np.uint8),
        state_code=np.array([[state]], dtype=dtype),
        first_signal_period_index=np.array([[first]], dtype=np.int32),
        maximum_disturbance_magnitude=np.array([[magnitude]], dtype=np.float64),
        maximum_consecutive_signal_periods=np.array([[streak]], dtype=np.uint16),
        maximum_multi_index_support_count=np.array(
            [[2 if signals.any() else 0]],
            dtype=np.uint8,
        ),
        valid_post_cutoff_observation_count=np.array(
            [[0 if state == DisturbanceStateCode.NOT_EVALUABLE else 3]],
            dtype=np.uint16,
        ),
        uncalibrated_disturbance_evidence_score=np.array(
            [[magnitude]],
            dtype=np.float64,
        ),
        quality_flags_bitmask=np.array([[flags]], dtype=np.uint16),
    )


def _ccdc_result(
    *,
    available: bool,
    has_break: bool,
    break_time: float = 2021.5,
    flags: int = 0,
    fit_eligible: bool | None = None,
) -> CcdcBenchmarkResult:
    eligible = available if fit_eligible is None else fit_eligible
    finite_break = break_time if has_break else np.nan
    pseudo_probability = 0.98 if has_break else np.nan
    magnitude = (
        np.array([0.8, 0.7, 0.6, 0.5], dtype=np.float64)[:, None, None]
        if has_break
        else np.full((4, 1, 1), np.nan, dtype=np.float64)
    )
    return CcdcBenchmarkResult(
        fit_eligible=np.array([[eligible]], dtype=np.bool_),
        fit_succeeded=np.array([[available]], dtype=np.bool_),
        has_post_cutoff_break=np.array([[has_break]], dtype=np.bool_),
        first_post_cutoff_break_segment_index=np.array(
            [[0 if has_break else -1]],
            dtype=np.int32,
        ),
        first_post_cutoff_break_fractional_year=np.array(
            [[finite_break]],
            dtype=np.float64,
        ),
        ccdc_breakpoint_pseudo_probability=np.array(
            [[pseudo_probability]],
            dtype=np.float64,
        ),
        raw_signed_magnitude=-magnitude,
        decrease_positive_magnitude=magnitude,
        first_break_segment_observation_count=np.array(
            [[8 if has_break else 0]],
            dtype=np.uint16,
        ),
        total_valid_observation_count=np.array(
            [[12 if eligible else 4]],
            dtype=np.uint16,
        ),
        quality_flags_bitmask=np.array([[flags]], dtype=np.uint16),
    )


def _converge(
    robust_state: DisturbanceStateCode,
    *,
    ccdc_available: bool = True,
    ccdc_break: bool = False,
    break_time: float = 2021.5,
    robust_flags: int = 0,
    ccdc_flags: int = 0,
    fit_eligible: bool | None = None,
) -> DetectorConvergenceResult:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    return converge_disturbance_detectors(
        robust_result=_robust_result(robust_state, flags=robust_flags),
        ccdc_result=_ccdc_result(
            available=ccdc_available,
            has_break=ccdc_break,
            break_time=break_time,
            flags=ccdc_flags,
            fit_eligible=fit_eligible,
        ),
        post_period_start_dates=PERIOD_STARTS,
        post_period_end_dates_exclusive=PERIOD_ENDS,
        config=config.disturbance_detection,
    )


def test_default_convergence_contract_is_versioned_and_forbids_score_fusion() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    convergence = config.disturbance_detection.convergence

    assert config.schema_version == "1.8.0"
    assert config.disturbance_detection.schema_version == "1.4.0"
    assert convergence.schema_version == "1.0.0"
    assert convergence.temporal_compatibility_policy == "ccdc_break_within_robust_signal_interval"
    assert convergence.interval_boundary_policy == "closed_start_open_end"
    assert convergence.temporal_compatibility_margin_days == 0
    assert convergence.score_fusion_allowed is False
    assert convergence.detector_disagreement_resolution_policy == "preserve"
    assert convergence.isolated_ccdc_break_policy == "transient_signal_requires_review"


def test_both_available_and_negative_are_stable_with_two_agreeing_detectors() -> None:
    result = _converge(DisturbanceStateCode.STABLE_OBSERVED)

    assert result.state_code.item() == DisturbanceStateCode.STABLE_OBSERVED
    assert result.reason_code.item() == DetectorConvergenceReasonCode.BOTH_STABLE
    assert result.available_detector_count.item() == 2
    assert result.agreeing_detector_count.item() == 2


def test_robust_transient_and_ccdc_without_break_preserve_transient_signal() -> None:
    result = _converge(DisturbanceStateCode.TRANSIENT_SIGNAL)

    assert result.state_code.item() == DisturbanceStateCode.TRANSIENT_SIGNAL
    assert result.reason_code.item() == DetectorConvergenceReasonCode.ROBUST_TRANSIENT_CCDC_NO_BREAK
    assert result.available_detector_count.item() == 2
    assert result.agreeing_detector_count.item() == 1


@pytest.mark.parametrize(
    ("robust_state", "break_time", "expected_state", "expected_reason"),
    [
        (
            DisturbanceStateCode.TRANSIENT_SIGNAL,
            2021.3,
            DisturbanceStateCode.TRANSIENT_SIGNAL,
            DetectorConvergenceReasonCode.COMPATIBLE_TRANSIENT_SIGNALS,
        ),
        (
            DisturbanceStateCode.PERSISTENT_CANDIDATE,
            2021.5,
            DisturbanceStateCode.PERSISTENT_CANDIDATE,
            DetectorConvergenceReasonCode.COMPATIBLE_PERSISTENT_SIGNALS,
        ),
    ],
)
def test_temporally_compatible_positive_signals_converge_without_score_fusion(
    robust_state: DisturbanceStateCode,
    break_time: float,
    expected_state: DisturbanceStateCode,
    expected_reason: DetectorConvergenceReasonCode,
) -> None:
    result = _converge(
        robust_state,
        ccdc_break=True,
        break_time=break_time,
    )

    assert result.state_code.item() == expected_state
    assert result.reason_code.item() == expected_reason
    assert result.agreeing_detector_count.item() == 2
    assert result.robust_uncalibrated_disturbance_evidence_score.item() == pytest.approx(4.5)
    assert result.ccdc_breakpoint_pseudo_probability.item() == pytest.approx(0.98)


def test_robust_only_preserves_persistent_evidence_without_faking_convergence() -> None:
    failure_bit = 1 << DisturbanceQualityBit.CCDC_FIT_FAILURE
    result = _converge(
        DisturbanceStateCode.PERSISTENT_CANDIDATE,
        ccdc_available=False,
        ccdc_flags=failure_bit,
        fit_eligible=True,
    )

    assert result.state_code.item() == DisturbanceStateCode.PERSISTENT_CANDIDATE
    assert result.reason_code.item() == DetectorConvergenceReasonCode.ROBUST_ONLY_PERSISTENT
    assert result.available_detector_count.item() == 1
    assert result.agreeing_detector_count.item() == 1
    assert result.quality_flags_bitmask.item() & failure_bit


@pytest.mark.parametrize(
    ("has_break", "expected_state", "expected_reason"),
    [
        (
            False,
            DisturbanceStateCode.STABLE_OBSERVED,
            DetectorConvergenceReasonCode.CCDC_ONLY_STABLE,
        ),
        (
            True,
            DisturbanceStateCode.TRANSIENT_SIGNAL,
            DetectorConvergenceReasonCode.CCDC_ONLY_BREAK_REQUIRES_REVIEW,
        ),
    ],
)
def test_ccdc_only_never_invents_robust_persistence(
    has_break: bool,
    expected_state: DisturbanceStateCode,
    expected_reason: DetectorConvergenceReasonCode,
) -> None:
    result = _converge(
        DisturbanceStateCode.NOT_EVALUABLE,
        ccdc_break=has_break,
    )

    assert result.state_code.item() == expected_state
    assert result.reason_code.item() == expected_reason
    assert result.available_detector_count.item() == 1
    assert result.agreeing_detector_count.item() == 1


def test_ccdc_fit_failure_is_unavailability_not_detector_disagreement() -> None:
    failure_bit = 1 << DisturbanceQualityBit.CCDC_FIT_FAILURE
    result = _converge(
        DisturbanceStateCode.STABLE_OBSERVED,
        ccdc_available=False,
        ccdc_flags=failure_bit,
        fit_eligible=True,
    )

    assert result.state_code.item() == DisturbanceStateCode.STABLE_OBSERVED
    assert result.reason_code.item() == DetectorConvergenceReasonCode.ROBUST_ONLY_STABLE
    assert result.state_code.item() != DisturbanceStateCode.DETECTOR_DISAGREEMENT
    assert result.quality_flags_bitmask.item() & failure_bit


def test_both_unavailable_are_not_evaluable() -> None:
    result = _converge(
        DisturbanceStateCode.NOT_EVALUABLE,
        ccdc_available=False,
    )

    assert result.state_code.item() == DisturbanceStateCode.NOT_EVALUABLE
    assert result.reason_code.item() == DetectorConvergenceReasonCode.NO_DETECTOR_AVAILABLE
    assert result.available_detector_count.item() == 0
    assert result.agreeing_detector_count.item() == 0


@pytest.mark.parametrize(
    ("robust_state", "ccdc_break"),
    [
        (DisturbanceStateCode.STABLE_OBSERVED, True),
        (DisturbanceStateCode.PERSISTENT_CANDIDATE, False),
    ],
)
def test_positive_negative_polarity_is_preserved_as_disagreement(
    robust_state: DisturbanceStateCode,
    ccdc_break: bool,
) -> None:
    result = _converge(robust_state, ccdc_break=ccdc_break)

    assert result.state_code.item() == DisturbanceStateCode.DETECTOR_DISAGREEMENT
    assert result.reason_code.item() == DetectorConvergenceReasonCode.DETECTOR_POLARITY_DISAGREEMENT
    assert result.available_detector_count.item() == 2
    assert result.agreeing_detector_count.item() == 1


def test_positive_but_temporally_incompatible_signals_are_disagreement() -> None:
    result = _converge(
        DisturbanceStateCode.PERSISTENT_CANDIDATE,
        ccdc_break=True,
        break_time=2022.2,
    )

    assert result.state_code.item() == DisturbanceStateCode.DETECTOR_DISAGREEMENT
    assert (
        result.reason_code.item()
        == DetectorConvergenceReasonCode.POSITIVE_SIGNALS_TEMPORALLY_INCOMPATIBLE
    )
    assert result.agreeing_detector_count.item() == 1


@pytest.mark.parametrize(
    ("break_time", "expected_state"),
    [
        (2021 + 59 / 365, DisturbanceStateCode.TRANSIENT_SIGNAL),
        (2021 + 151 / 365, DisturbanceStateCode.DETECTOR_DISAGREEMENT),
    ],
)
def test_temporal_compatibility_uses_real_closed_open_period_bounds(
    break_time: float,
    expected_state: DisturbanceStateCode,
) -> None:
    result = _converge(
        DisturbanceStateCode.TRANSIENT_SIGNAL,
        ccdc_break=True,
        break_time=break_time,
    )

    assert result.state_code.item() == expected_state


def test_quality_bitmasks_are_combined_without_losing_baseline_or_temporal_flags() -> None:
    baseline_bit = 1 << DisturbanceQualityBit.BASELINE_UNCERTAIN
    temporal_bit = 1 << DisturbanceQualityBit.LONG_OBSERVATION_GAP
    result = _converge(
        DisturbanceStateCode.PERSISTENT_CANDIDATE,
        ccdc_available=False,
        robust_flags=baseline_bit,
        ccdc_flags=temporal_bit,
    )

    assert result.quality_flags_bitmask.item() == baseline_bit | temporal_bit


def test_convergence_preserves_detector_evidence_and_original_temporal_indices() -> None:
    robust = _robust_result(DisturbanceStateCode.PERSISTENT_CANDIDATE)
    ccdc = _ccdc_result(available=True, has_break=True, break_time=2021.5)
    robust_state_before = robust.state_code.copy()
    ccdc_break_before = ccdc.first_post_cutoff_break_fractional_year.copy()
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    result = converge_disturbance_detectors(
        robust_result=robust,
        ccdc_result=ccdc,
        post_period_start_dates=PERIOD_STARTS,
        post_period_end_dates_exclusive=PERIOD_ENDS,
        config=config.disturbance_detection,
    )

    assert result.first_robust_signal_period_index.item() == 0
    assert result.first_ccdc_break_fractional_year.item() == pytest.approx(2021.5)
    assert result.robust_maximum_disturbance_magnitude.item() == pytest.approx(4.5)
    assert result.ccdc_decrease_positive_magnitude[:, 0, 0].tolist() == pytest.approx(
        [0.8, 0.7, 0.6, 0.5]
    )
    assert np.array_equal(robust.state_code, robust_state_before)
    assert np.array_equal(ccdc.first_post_cutoff_break_fractional_year, ccdc_break_before)
    assert all(
        not np.asarray(getattr(result, field.name)).flags.writeable for field in fields(result)
    )


def test_convergence_rejects_spatial_shape_or_source_dtype_mismatch() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    ccdc_wrong_shape = replace(
        _ccdc_result(available=True, has_break=False),
        fit_succeeded=np.ones((1, 2), dtype=np.bool_),
    )

    with pytest.raises(ValueError, match="forma espacial"):
        converge_disturbance_detectors(
            robust_result=_robust_result(DisturbanceStateCode.STABLE_OBSERVED),
            ccdc_result=ccdc_wrong_shape,
            post_period_start_dates=PERIOD_STARTS,
            post_period_end_dates_exclusive=PERIOD_ENDS,
            config=config.disturbance_detection,
        )

    with pytest.raises(ValueError, match="state_code debe usar uint8"):
        converge_disturbance_detectors(
            robust_result=_robust_result(
                DisturbanceStateCode.STABLE_OBSERVED,
                dtype=np.int16,
            ),
            ccdc_result=_ccdc_result(available=True, has_break=False),
            post_period_start_dates=PERIOD_STARTS,
            post_period_end_dates_exclusive=PERIOD_ENDS,
            config=config.disturbance_detection,
        )


def test_convergence_contract_has_no_combined_probability_or_attribution_fields() -> None:
    field_names = {field.name.lower() for field in fields(DetectorConvergenceResult)}

    assert "probability" not in field_names
    assert not any("deforestation" in name or "conversion" in name for name in field_names)
    assert "robust_uncalibrated_disturbance_evidence_score" in field_names
    assert "ccdc_breakpoint_pseudo_probability" in field_names


def test_convergence_config_participates_in_scientific_hash() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    baseline_hash = parameters_hash(config)
    changed_convergence = config.disturbance_detection.convergence.model_copy(
        update={"schema_version": "9.9.9"}
    )
    changed_detection = config.disturbance_detection.model_copy(
        update={"convergence": changed_convergence}
    )
    changed_config = config.model_copy(update={"disturbance_detection": changed_detection})

    assert parameters_hash(changed_config) != baseline_hash
