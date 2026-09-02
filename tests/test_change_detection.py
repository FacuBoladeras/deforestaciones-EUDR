"""Contrato científico del Paso 14.0, todavía sin ejecutar detectores."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from deforestation_pipeline.artifact_layout import (
    evidence_figure,
    evidence_json,
    evidence_table,
    evidence_tiff,
)
from deforestation_pipeline.change_detection import (
    DISTURBANCE_DIAGNOSTIC_BAND_NAMES,
    DISTURBANCE_SUMMARY_BAND_NAMES,
    DisturbanceDetectionConfig,
    DisturbanceDetector,
    DisturbanceQualityBit,
    DisturbanceStateCode,
    disturbance_detection_json_schema,
    disturbance_detection_output_paths,
)
from deforestation_pipeline.config import (
    PipelineConfig,
    load_config,
    resolve_run_config,
    scientific_parameters_hash,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_config_declares_the_versioned_step_14_3_rule() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    detection = config.disturbance_detection

    assert config.schema_version == "1.13.0"
    assert detection.schema_version == "1.5.0"
    assert detection.reference_history_start_date == date(2017, 1, 1)
    assert detection.analysis_start_date == date(2021, 1, 1)
    assert detection.minimum_baseline_source_count == 2
    assert detection.detection_indices == ("NDVI", "NBR", "NDMI", "NIRv")
    assert detection.planned_detectors == (
        DisturbanceDetector.ROBUST_SEASONAL_PRE_POST,
        DisturbanceDetector.CCDC_BENCHMARK,
    )
    assert detection.robust_seasonal.minimum_reference_observations == 3
    assert detection.robust_seasonal.mad_scale_constant == 1.4826
    assert detection.robust_seasonal.vegetation_loss_direction == "decrease_is_positive"
    assert detection.robust_seasonal.reference_comparison_policy == "same_season_only"
    assert detection.robust_seasonal.zero_scale_policy == "not_standardizable"
    assert (
        detection.robust_seasonal.scale_stabilization_policy
        == "seasonal_index_spatial_quantile_floor"
    )
    assert detection.robust_seasonal.scale_floor_quantile == 0.25
    assert detection.robust_seasonal.scale_floor_minimum_valid_pixels == 4
    assert detection.robust_seasonal.missing_data_policy == "preserve_nan_without_interpolation"
    assert detection.robust_seasonal.standardized_magnitude_threshold == 3.0
    assert detection.robust_seasonal.minimum_index_support_count == 2
    assert detection.robust_seasonal.minimum_consecutive_signal_periods == 2
    assert detection.robust_seasonal.anomaly_threshold_applied is True
    assert detection.robust_seasonal.persistence_rule_applied is True
    assert detection.score_semantics == "uncalibrated_disturbance_evidence_score"
    assert detection.baseline_disagreement_policy == "evaluate_and_flag"
    assert detection.cutoff_straddling_period_policy == "exclude"
    assert detection.preserve_detector_evidence is True
    assert detection.preserve_detector_disagreement is True
    assert detection.long_gap_interpolation_allowed is False
    assert detection.attribution_allowed is False
    assert detection.spatial_area_threshold_applied is False
    assert detection.automatic_final_assessment_allowed is False


def test_detection_states_and_quality_bits_are_stable_machine_codes() -> None:
    assert {state.name: state.value for state in DisturbanceStateCode} == {
        "NOT_EVALUABLE": 0,
        "STABLE_OBSERVED": 1,
        "TRANSIENT_SIGNAL": 2,
        "PERSISTENT_CANDIDATE": 3,
        "DETECTOR_DISAGREEMENT": 4,
    }
    assert {flag.name: flag.value for flag in DisturbanceQualityBit} == {
        "BASELINE_UNCERTAIN": 0,
        "INSUFFICIENT_REFERENCE_HISTORY": 1,
        "INSUFFICIENT_POST_CUTOFF_OBSERVATIONS": 2,
        "PERSISTENT_CLOUD_OR_SHADOW": 3,
        "CUTOFF_STRADDLING_PERIOD_EXCLUDED": 4,
        "SENSOR_IMBALANCE": 5,
        "LONG_OBSERVATION_GAP": 6,
        "CCDC_FIT_FAILURE": 7,
        "EDGE_PIXEL": 8,
        "CCDC_INSUFFICIENT_OBSERVATIONS": 9,
        "ROBUST_SCALE_STABILIZED": 10,
    }


def test_reserved_raster_bands_do_not_claim_probability_or_attribution() -> None:
    assert DISTURBANCE_SUMMARY_BAND_NAMES == (
        "disturbance_state_code",
        "convergence_reason_code",
        "first_anomalous_period_index",
        "maximum_disturbance_magnitude",
        "persistence_period_count",
        "disturbance_score",
        "available_detector_count",
        "agreeing_detector_count",
        "valid_post_cutoff_observation_count",
        "quality_flags_bitmask",
    )
    assert DISTURBANCE_DIAGNOSTIC_BAND_NAMES == (
        "robust_multi_index_support_count",
        "robust_max_standardized_anomaly",
        "robust_scale_floor_maximum",
        "robust_scale_stabilization_count",
        "ccdc_break_day_offset_from_cutoff",
        "ccdc_change_magnitude",
        "recovery_indicator",
        "baseline_evidence_fraction",
    )
    names = " ".join((*DISTURBANCE_SUMMARY_BAND_NAMES, *DISTURBANCE_DIAGNOSTIC_BAND_NAMES)).lower()
    assert "probability" not in names
    assert "deforestation" not in names
    assert "conversion" not in names


def test_disturbance_paths_publish_additive_raw_robust_state_for_fusion() -> None:
    paths = disturbance_detection_output_paths()

    assert paths["robust_state_raster"] == "tiffs/evidence/disturbance_robust_state.tif"


def test_temporal_boundary_and_non_decision_policies_cannot_be_relaxed() -> None:
    payload = load_config(
        PROJECT_ROOT / "configs" / "default.yml"
    ).disturbance_detection.model_dump()

    invalid_changes = (
        ("analysis_start_date", date(2020, 12, 31)),
        ("cutoff_straddling_period_policy", "include"),
        ("attribution_allowed", True),
        ("spatial_area_threshold_applied", True),
        ("automatic_final_assessment_allowed", True),
        ("long_gap_interpolation_allowed", True),
    )
    for field, value in invalid_changes:
        changed = {**payload, field: value}
        with pytest.raises(ValidationError):
            DisturbanceDetectionConfig.model_validate(changed)


def test_reference_history_must_use_complete_pre_cutoff_calendar_years() -> None:
    payload = load_config(
        PROJECT_ROOT / "configs" / "default.yml"
    ).disturbance_detection.model_dump()

    with pytest.raises(ValidationError, match="1 de enero"):
        DisturbanceDetectionConfig.model_validate(
            {**payload, "reference_history_start_date": date(2017, 2, 1)}
        )
    with pytest.raises(ValidationError, match="anterior"):
        DisturbanceDetectionConfig.model_validate(
            {**payload, "reference_history_start_date": date(2021, 1, 1)}
        )


def test_disturbance_outputs_reuse_the_flat_evidence_domain() -> None:
    paths = disturbance_detection_output_paths()

    assert paths == {
        "metadata": "json/evidence/disturbance_detection.json",
        "summary_raster": "tiffs/evidence/disturbance_summary.tif",
        "diagnostics_raster": "tiffs/evidence/disturbance_diagnostics.tif",
        "robust_state_raster": "tiffs/evidence/disturbance_robust_state.tif",
        "qa_figure": "figures/evidence/disturbance_detection.png",
        "period_summary_table": "tables/evidence/disturbance_period_summary.csv",
    }
    assert evidence_json("disturbance_detection.json") == paths["metadata"]
    assert evidence_tiff("disturbance_summary.tif") == paths["summary_raster"]
    assert evidence_figure("disturbance_detection.png") == paths["qa_figure"]
    assert evidence_table("disturbance_period_summary.csv") == paths["period_summary_table"]
    assert all(len(Path(path).parts) == 3 for path in paths.values())


def test_committed_disturbance_detection_schema_matches_model() -> None:
    path = PROJECT_ROOT / "data" / "schemas" / "disturbance-detection-v1.5.0.json"
    committed = json.loads(path.read_text(encoding="utf-8"))

    assert committed == disturbance_detection_json_schema()


def test_pipeline_requires_the_disturbance_contract_and_hashes_it() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    missing = config.model_dump()
    missing.pop("disturbance_detection")
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(missing)

    baseline = resolve_run_config(config, date(2026, 7, 29))
    changed_payload = config.model_dump()
    changed_payload["disturbance_detection"]["reference_history_start_date"] = date(2016, 1, 1)
    changed = resolve_run_config(
        PipelineConfig.model_validate(changed_payload),
        date(2026, 7, 29),
    )
    assert scientific_parameters_hash(baseline) != scientific_parameters_hash(changed)


def test_robust_seasonal_parameters_participate_in_scientific_hash() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    baseline = resolve_run_config(config, date(2026, 7, 29))
    changed_payload = config.model_dump()
    changed_payload["disturbance_detection"]["robust_seasonal"][
        "minimum_reference_observations"
    ] = 4
    changed = resolve_run_config(
        PipelineConfig.model_validate(changed_payload),
        date(2026, 7, 29),
    )

    assert scientific_parameters_hash(baseline) != scientific_parameters_hash(changed)


def test_pipeline_rejects_detection_inputs_incompatible_with_upstream_contracts() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    wrong_source_count = config.model_dump()
    wrong_source_count["disturbance_detection"]["minimum_baseline_source_count"] = 3
    with pytest.raises(ValidationError, match="minimum_baseline_source_count"):
        PipelineConfig.model_validate(wrong_source_count)

    missing_index = config.model_dump()
    missing_index["data"]["indices"] = tuple(
        item for item in missing_index["data"]["indices"] if item != "NBR"
    )
    missing_index["output"]["index_visualization_ranges"] = [
        item
        for item in missing_index["output"]["index_visualization_ranges"]
        if item["index"] != "NBR"
    ]
    with pytest.raises(ValidationError, match="detection_indices"):
        PipelineConfig.model_validate(missing_index)
