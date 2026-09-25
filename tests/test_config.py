"""Pruebas de carga y reproducibilidad de configuración."""

from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from deforestation_pipeline.config import (
    ConfigurationError,
    OutputFormat,
    PipelineConfig,
    ResolvedPipelineConfig,
    execution_config_hash,
    load_config,
    load_forest_model_config,
    load_license_registry,
    resolve_run_config,
    scientific_parameters_hash,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_FOREST_MODEL_CONFIG = PROJECT_ROOT / "configs" / "rf-forest-entrerios-2020-2024.yml"


def test_candidate_forest_model_override_is_explicit_and_default_stays_p0() -> None:
    default = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_model
    candidate = load_forest_model_config(CANDIDATE_FOREST_MODEL_CONFIG)

    assert default.schema_version == "1.0.0"
    assert default.asset_id.endswith("/rf_forest_2020")
    assert default.supported_observation_years == (2020,)
    assert default.temporal_transfer_validated is False
    assert candidate.schema_version == "1.1.0"
    assert candidate.asset_id.endswith("/rf_forest_multiyear_2020_2024_v1")
    assert candidate.inference_backend == "local_sklearn_joblib"
    assert candidate.local_joblib_path is not None
    assert candidate.local_joblib_path.endswith("/random_forest_forest_multiyear_2020_2024.joblib")
    assert candidate.comparison_asset_id == default.asset_id
    assert candidate.supported_observation_years == (2020, 2021, 2022, 2023, 2024)
    assert candidate.temporal_transfer_validated is False


def _forest_baseline_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "reference_date": "2020-12-31",
        "feature_start_date": "2019-01-01",
        "feature_end_date_exclusive": "2021-01-01",
        "benchmark_resolution_m": 30,
        "minimum_independent_sources": 2,
        "forest_definition": {
            "minimum_area_ha": 0.5,
            "minimum_tree_height_m": 5.0,
            "minimum_canopy_cover_percent": 10.0,
            "exclude_predominantly_agricultural_or_urban": True,
        },
        "preserve_source_evidence": True,
        "preserve_disagreement": True,
        "post_cutoff_observations_allowed": False,
        "long_gap_interpolation_allowed": False,
    }


def _forest_model_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "asset_id": "projects/ee-facuboladerasgee/assets/models/rf_forest_2020",
        "asset_type": "geemap_tree_feature_collection",
        "tree_property": "tree",
        "expected_tree_count": 500,
        "reference_year": 2020,
        "jurisdiction": "entre_rios",
        "non_forest_class_code": 0,
        "forest_class_code": 1,
        "training_label_semantics": "multisource_unanimity_proxy_v1",
        "score_semantics": "uncalibrated_binary_tree_vote_fraction",
        "evidence_role": "learned_supporting_evidence",
        "independent_evidence": False,
        "temporal_transfer_validated": False,
        "calibrated_probability": False,
    }


def _forest_screening_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.1.0",
        "minimum_core_source_count": 2,
        "expected_tree_count": 500,
        "forest_vote_fraction_minimum": 0.8,
        "nonforest_vote_fraction_maximum": 0.2,
        "core_nonforest_max_evidence_fraction": 0.0,
        "forest_rule": (
            "rf_complete_and_core_sufficient_and_unanimous_forest_and_vote_gte_threshold"
        ),
        "nonforest_rule": (
            "rf_complete_and_core_sufficient_and_unanimous_nonforest_and_vote_lte_threshold"
        ),
        "temporal_signal_scope": ("entire_aoi_primary_interpretation_automated_forest_only"),
        "score_semantics": "uncalibrated_binary_tree_vote_fraction",
        "automatic_final_assessment_allowed": False,
    }


def _disturbance_detection_payload(
    indices: tuple[str, ...] = ("NDVI", "NBR"),
) -> dict[str, Any]:
    return {
        "schema_version": "1.5.0",
        "reference_history_start_date": "2017-01-01",
        "analysis_start_date": "2021-01-01",
        "minimum_baseline_source_count": 2,
        "detection_indices": indices,
        "planned_detectors": (
            "robust_seasonal_pre_post",
            "ccdc_benchmark",
        ),
        "robust_seasonal": {
            "schema_version": "1.2.0",
            "minimum_reference_observations": 3,
            "mad_scale_constant": 1.4826,
            "vegetation_loss_direction": "decrease_is_positive",
            "reference_comparison_policy": "same_season_only",
            "zero_scale_policy": "not_standardizable",
            "scale_stabilization_policy": "seasonal_index_spatial_quantile_floor",
            "scale_floor_quantile": 0.25,
            "scale_floor_minimum_valid_pixels": 4,
            "scale_floor_fallback_policy": "leave_local_scale_unchanged",
            "missing_data_policy": "preserve_nan_without_interpolation",
            "standardized_magnitude_threshold": 3.0,
            "minimum_index_support_count": 2,
            "minimum_consecutive_signal_periods": 2,
            "minimum_valid_post_cutoff_periods": 2,
            "missing_period_streak_policy": "break_without_recovery_inference",
            "long_gap_minimum_consecutive_missing_periods": 2,
            "threshold_calibration_status": (
                "conservative_benchmark_requires_independent_validation"
            ),
            "score_aggregation": "maximum_positive_standardized_magnitude",
            "anomaly_threshold_applied": True,
            "persistence_rule_applied": True,
        },
        "ccdc_benchmark": {
            "schema_version": "1.0.0",
            "earth_engine_python_api_version": "1.7.36",
            "api_reference_url": (
                "https://developers.google.com/earth-engine/apidocs/"
                "ee-algorithms-temporalsegmentation-ccdc"
            ),
            "api_reference_last_updated": "2026-04-20",
            "api_reference_accessed_at": "2026-07-29",
            "output_semantics_reference_url": (
                "https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_GLOBAL_CCDC_V1"
            ),
            "source_profile": "hlsl30_only",
            "source_product": "HLSL30",
            "input_temporal_granularity": "dense_masked_observations",
            "breakpoint_bands": indices,
            "min_observations": 6,
            "chi_square_probability": 0.99,
            "min_num_of_years_scaler": 1.33,
            "date_format": 1,
            "lambda": 20.0,
            "max_iterations": 25_000,
            "tmask_policy": "disabled_hls_fmask_preapplied",
            "tmask_bands": (),
            "magnitude_direction": "decrease_is_positive",
            "change_probability_semantics": (
                "algorithmic_breakpoint_pseudo_probability_not_deforestation_probability"
            ),
            "comparative_sensor_profile": "hlsl30_hlss30_deferred",
            "comparative_sensor_profile_enabled": False,
            "raw_array_output_preserved": True,
            "post_cutoff_break_threshold_applied": False,
        },
        "convergence": {
            "schema_version": "1.0.0",
            "temporal_compatibility_policy": ("ccdc_break_within_robust_signal_interval"),
            "interval_boundary_policy": "closed_start_open_end",
            "temporal_compatibility_margin_days": 0,
            "unavailable_detector_policy": ("preserve_available_evidence_without_convergence"),
            "isolated_ccdc_break_policy": "transient_signal_requires_review",
            "detector_disagreement_resolution_policy": "preserve",
            "score_fusion_allowed": False,
        },
        "score_semantics": "uncalibrated_disturbance_evidence_score",
        "baseline_disagreement_policy": "evaluate_and_flag",
        "cutoff_straddling_period_policy": "exclude",
        "preserve_detector_evidence": True,
        "preserve_detector_disagreement": True,
        "long_gap_interpolation_allowed": False,
        "attribution_allowed": False,
        "spatial_area_threshold_applied": False,
        "automatic_final_assessment_allowed": False,
    }


def _disturbance_events_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.2.0",
        "connectivity": 8,
        "visec_operational_event_area_threshold_ha": 0.5,
        "visec_area_threshold_relation": "strictly_greater_than",
        "area_threshold_basis": "candidate_footprint_reference_only_no_event_promotion",
        "area_method": "projected_grid_affine_determinant",
        "area_threshold_policy": "preserve_all_candidates_publish_events_only_after_attribution",
        "maximum_onset_period_difference": 1,
        "missing_onset_policy": "separate_unknown_episode",
        "event_id_strategy": "grid_temporal_episode_and_pixels_sha256_v2",
        "ordering_policy": "area_descending_then_event_id",
        "detail_figure_limit": 5,
        "detail_selection_policy": "largest_area_then_event_id",
        "comparison_policy": ("latest_pre_cutoff_same_season_vs_earliest_event_onset_window"),
        "geometry_crs": "EPSG:4326",
        "primary_interpretation_domain": "automated_forest",
        "automatic_final_assessment_allowed": False,
        "attribution_allowed": False,
    }


def test_default_config_is_valid_and_deterministic() -> None:
    """La configuración versionada debe cargar y producir un SHA-256 estable."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    resolved = resolve_run_config(config, date(2026, 7, 30))

    assert config.analysis.cutoff_date == date(2020, 12, 31)
    assert config.spatial.forest_definition_min_area_ha == 0.5
    assert config.spatial.preserve_subthreshold_events is True
    assert config.disturbance_events.visec_operational_event_area_threshold_ha == 0.5
    assert config.disturbance_events.visec_area_threshold_relation == "strictly_greater_than"
    assert config.disturbance_events.area_threshold_basis == (
        "candidate_footprint_reference_only_no_event_promotion"
    )
    assert config.spatial.raster_crs_strategy == "local_utm"
    assert len(scientific_parameters_hash(resolved)) == 64
    assert len(execution_config_hash(resolved)) == 64


def test_default_config_declares_current_hls_raster_contract() -> None:
    """Los defaults operativos usados por el flujo HLS permanecen explícitos."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    assert config.schema_version == "1.13.0"
    assert config.data.composition_interval == "annual"
    assert config.data.composition_reducer == "median"
    assert config.data.source_native_reflectance_scale_factor == 0.0001
    assert config.data.source_native_reflectance_offset == 0.0
    assert config.data.earth_engine_reflectance_multiplier == 1.0
    assert config.data.earth_engine_reflectance_offset == 0.0
    assert config.data.mask_high_aerosol is True
    assert config.data.preserve_water is True
    assert config.spatial.raster_crs_strategy == "local_utm"
    assert OutputFormat.GEOTIFF in config.output.formats
    assert config.output.artifact_profile == "lean"
    assert config.output.raster_nodata == -9999.0
    assert config.output.maximum_direct_download_bytes == 32_000_000
    assert config.output.maximum_direct_download_dimension == 10_000
    assert config.output.maximum_series_download_bytes == 256_000_000
    assert config.output.rgb_min_reflectance < config.output.rgb_max_reflectance


def test_execution_hash_matches_golden_for_controlled_configuration() -> None:
    """El digest se calcula sobre el contenido validado, no sobre un YAML dado."""
    config = PipelineConfig.model_validate(
        {
            "schema_version": "1.13.0",
            "analysis": {
                "cutoff_date": "2020-12-31",
                "benchmark_start_date": "2019-01-01",
                "analysis_end_date": "2021-01-01",
                "random_seed": 7,
            },
            "spatial": {
                "interchange_crs": "EPSG:4326",
                "area_crs_strategy": "auto_equal_area",
                "raster_crs_strategy": "local_utm",
                "forest_definition_min_area_ha": 0.5,
                "minimum_coordinate_decimals": 6,
                "preserve_subthreshold_events": True,
            },
            "data": {
                "benchmark_sensor": "HLS",
                "target_resolution_m": 30,
                "composition_interval": "annual",
                "composition_reducer": "median",
                "source_native_reflectance_scale_factor": 0.0001,
                "source_native_reflectance_offset": 0.0,
                "earth_engine_reflectance_multiplier": 1.0,
                "earth_engine_reflectance_offset": 0.0,
                "mask_high_aerosol": True,
                "preserve_water": True,
                "indices": ["NDVI", "NBR"],
            },
            "forest_baseline": _forest_baseline_payload(),
            "forest_model": _forest_model_payload(),
            "forest_screening": _forest_screening_payload(),
            "disturbance_detection": _disturbance_detection_payload(),
            "disturbance_events": _disturbance_events_payload(),
            "output": {
                "artifact_profile": "lean",
                "directory": "outputs",
                "formats": ["json", "png", "geotiff"],
                "raster_nodata": -9999.0,
                "maximum_direct_download_bytes": 32_000_000,
                "maximum_direct_download_dimension": 10_000,
                "maximum_series_download_bytes": 256_000_000,
                "rgb_min_reflectance": 0.01,
                "rgb_max_reflectance": 0.18,
                "index_visualization_ranges": [
                    {"index": "NDVI", "minimum": -1.0, "maximum": 1.0},
                    {"index": "NBR", "minimum": -1.0, "maximum": 1.0},
                ],
                "png_dpi": 150,
            },
        }
    )
    resolved = resolve_run_config(config, date(2021, 1, 1))

    assert execution_config_hash(resolved) == (
        "db3986409777990f73bdaac42ad4a14c9f58aff36d6d4001d8e21b44e26d0772"
    )


def test_forest_definition_and_visec_operational_thresholds_are_separate_contracts() -> None:
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump(mode="json")

    assert payload["spatial"]["forest_definition_min_area_ha"] == 0.5
    assert payload["disturbance_events"]["visec_operational_event_area_threshold_ha"] == 0.5
    assert "area_threshold_ha" not in payload["disturbance_events"]


def test_execution_hash_ignores_yaml_key_order_and_surface_format(
    tmp_path: Path,
) -> None:
    """YAML equivalente produce el mismo modelo validado y el mismo digest."""
    first = tmp_path / "first.yml"
    second = tmp_path / "second.yml"
    first.write_text(
        "\n".join(
            [
                'schema_version: "1.13.0"',
                "analysis:",
                "  cutoff_date: 2020-12-31",
                "  benchmark_start_date: 2019-01-01",
                "  analysis_end_date: 2021-01-01",
                "  random_seed: 7",
                "spatial:",
                "  interchange_crs: EPSG:4326",
                "  area_crs_strategy: auto_equal_area",
                "  raster_crs_strategy: local_utm",
                "  forest_definition_min_area_ha: 0.5",
                "  minimum_coordinate_decimals: 6",
                "  preserve_subthreshold_events: true",
                "data:",
                "  benchmark_sensor: HLS",
                "  target_resolution_m: 30",
                "  composition_interval: annual",
                "  composition_reducer: median",
                "  source_native_reflectance_scale_factor: 0.0001",
                "  source_native_reflectance_offset: 0.0",
                "  earth_engine_reflectance_multiplier: 1.0",
                "  earth_engine_reflectance_offset: 0.0",
                "  mask_high_aerosol: true",
                "  preserve_water: true",
                "  indices: [NDVI, EVI2, NBR]",
                "forest_baseline:",
                '  schema_version: "1.0.0"',
                "  reference_date: 2020-12-31",
                "  feature_start_date: 2019-01-01",
                "  feature_end_date_exclusive: 2021-01-01",
                "  benchmark_resolution_m: 30",
                "  minimum_independent_sources: 2",
                "  forest_definition:",
                "    minimum_area_ha: 0.5",
                "    minimum_tree_height_m: 5.0",
                "    minimum_canopy_cover_percent: 10.0",
                "    exclude_predominantly_agricultural_or_urban: true",
                "  preserve_source_evidence: true",
                "  preserve_disagreement: true",
                "  post_cutoff_observations_allowed: false",
                "  long_gap_interpolation_allowed: false",
                "forest_model:",
                '  schema_version: "1.0.0"',
                ("  asset_id: projects/ee-facuboladerasgee/assets/models/rf_forest_2020"),
                "  asset_type: geemap_tree_feature_collection",
                "  tree_property: tree",
                "  expected_tree_count: 500",
                "  reference_year: 2020",
                "  jurisdiction: entre_rios",
                "  non_forest_class_code: 0",
                "  forest_class_code: 1",
                "  training_label_semantics: multisource_unanimity_proxy_v1",
                "  score_semantics: uncalibrated_binary_tree_vote_fraction",
                "  evidence_role: learned_supporting_evidence",
                "  independent_evidence: false",
                "  temporal_transfer_validated: false",
                "  calibrated_probability: false",
                "forest_screening:",
                '  schema_version: "1.1.0"',
                "  minimum_core_source_count: 2",
                "  expected_tree_count: 500",
                "  forest_vote_fraction_minimum: 0.8",
                "  nonforest_vote_fraction_maximum: 0.2",
                "  core_nonforest_max_evidence_fraction: 0.0",
                (
                    "  forest_rule: rf_complete_and_core_sufficient_and_"
                    "unanimous_forest_and_vote_gte_threshold"
                ),
                (
                    "  nonforest_rule: rf_complete_and_core_sufficient_and_"
                    "unanimous_nonforest_and_vote_lte_threshold"
                ),
                (
                    "  temporal_signal_scope: entire_aoi_primary_interpretation_"
                    "automated_forest_only"
                ),
                "  score_semantics: uncalibrated_binary_tree_vote_fraction",
                "  automatic_final_assessment_allowed: false",
                "disturbance_detection:",
                '  schema_version: "1.5.0"',
                "  reference_history_start_date: 2017-01-01",
                "  analysis_start_date: 2021-01-01",
                "  minimum_baseline_source_count: 2",
                "  detection_indices: [NDVI, NBR]",
                "  planned_detectors: [robust_seasonal_pre_post, ccdc_benchmark]",
                "  robust_seasonal:",
                '    schema_version: "1.2.0"',
                "    minimum_reference_observations: 3",
                "    mad_scale_constant: 1.4826",
                "    vegetation_loss_direction: decrease_is_positive",
                "    reference_comparison_policy: same_season_only",
                "    zero_scale_policy: not_standardizable",
                "    scale_stabilization_policy: seasonal_index_spatial_quantile_floor",
                "    scale_floor_quantile: 0.25",
                "    scale_floor_minimum_valid_pixels: 4",
                "    scale_floor_fallback_policy: leave_local_scale_unchanged",
                "    missing_data_policy: preserve_nan_without_interpolation",
                "    standardized_magnitude_threshold: 3.0",
                "    minimum_index_support_count: 2",
                "    minimum_consecutive_signal_periods: 2",
                "    minimum_valid_post_cutoff_periods: 2",
                "    missing_period_streak_policy: break_without_recovery_inference",
                "    long_gap_minimum_consecutive_missing_periods: 2",
                (
                    "    threshold_calibration_status: "
                    "conservative_benchmark_requires_independent_validation"
                ),
                "    score_aggregation: maximum_positive_standardized_magnitude",
                "    anomaly_threshold_applied: true",
                "    persistence_rule_applied: true",
                "  ccdc_benchmark:",
                '    schema_version: "1.0.0"',
                '    earth_engine_python_api_version: "1.7.36"',
                (
                    "    api_reference_url: "
                    "https://developers.google.com/earth-engine/apidocs/"
                    "ee-algorithms-temporalsegmentation-ccdc"
                ),
                "    api_reference_last_updated: 2026-04-20",
                "    api_reference_accessed_at: 2026-07-29",
                (
                    "    output_semantics_reference_url: "
                    "https://developers.google.com/earth-engine/datasets/catalog/"
                    "GOOGLE_GLOBAL_CCDC_V1"
                ),
                "    source_profile: hlsl30_only",
                "    source_product: HLSL30",
                "    input_temporal_granularity: dense_masked_observations",
                "    breakpoint_bands: [NDVI, NBR]",
                "    min_observations: 6",
                "    chi_square_probability: 0.99",
                "    min_num_of_years_scaler: 1.33",
                "    date_format: 1",
                "    lambda: 20.0",
                "    max_iterations: 25000",
                "    tmask_policy: disabled_hls_fmask_preapplied",
                "    tmask_bands: []",
                "    magnitude_direction: decrease_is_positive",
                (
                    "    change_probability_semantics: "
                    "algorithmic_breakpoint_pseudo_probability_not_deforestation_probability"
                ),
                "    comparative_sensor_profile: hlsl30_hlss30_deferred",
                "    comparative_sensor_profile_enabled: false",
                "    raw_array_output_preserved: true",
                "    post_cutoff_break_threshold_applied: false",
                "  convergence:",
                '    schema_version: "1.0.0"',
                ("    temporal_compatibility_policy: ccdc_break_within_robust_signal_interval"),
                "    interval_boundary_policy: closed_start_open_end",
                "    temporal_compatibility_margin_days: 0",
                (
                    "    unavailable_detector_policy: "
                    "preserve_available_evidence_without_convergence"
                ),
                "    isolated_ccdc_break_policy: transient_signal_requires_review",
                "    detector_disagreement_resolution_policy: preserve",
                "    score_fusion_allowed: false",
                "  score_semantics: uncalibrated_disturbance_evidence_score",
                "  baseline_disagreement_policy: evaluate_and_flag",
                "  cutoff_straddling_period_policy: exclude",
                "  preserve_detector_evidence: true",
                "  preserve_detector_disagreement: true",
                "  long_gap_interpolation_allowed: false",
                "  attribution_allowed: false",
                "  spatial_area_threshold_applied: false",
                "  automatic_final_assessment_allowed: false",
                "disturbance_events:",
                '  schema_version: "1.2.0"',
                "  connectivity: 8",
                "  visec_operational_event_area_threshold_ha: 0.5",
                "  visec_area_threshold_relation: strictly_greater_than",
                ("  area_threshold_basis: candidate_footprint_reference_only_no_event_promotion"),
                "  area_method: projected_grid_affine_determinant",
                (
                    "  area_threshold_policy: preserve_all_candidates_publish_events_"
                    "only_after_attribution"
                ),
                "  maximum_onset_period_difference: 1",
                "  missing_onset_policy: separate_unknown_episode",
                "  event_id_strategy: grid_temporal_episode_and_pixels_sha256_v2",
                "  ordering_policy: area_descending_then_event_id",
                "  detail_figure_limit: 5",
                "  detail_selection_policy: largest_area_then_event_id",
                (
                    "  comparison_policy: latest_pre_cutoff_same_season_vs_"
                    "earliest_event_onset_window"
                ),
                "  geometry_crs: EPSG:4326",
                "  primary_interpretation_domain: automated_forest",
                "  automatic_final_assessment_allowed: false",
                "  attribution_allowed: false",
                "output:",
                "  artifact_profile: lean",
                "  directory: outputs",
                "  formats: [json, geojson, png, geotiff]",
                "  raster_nodata: -9999.0",
                "  maximum_direct_download_bytes: 32000000",
                "  maximum_direct_download_dimension: 10000",
                "  maximum_series_download_bytes: 256000000",
                "  rgb_min_reflectance: 0.01",
                "  rgb_max_reflectance: 0.18",
                "  index_visualization_ranges:",
                "    - {index: NDVI, minimum: -1.0, maximum: 1.0}",
                "    - {index: EVI2, minimum: -1.0, maximum: 1.0}",
                "    - {index: NBR, minimum: -1.0, maximum: 1.0}",
                "  png_dpi: 150",
                "",
            ]
        ),
        encoding="utf-8",
    )
    second.write_text(
        "\n".join(
            [
                "# Same semantic configuration, deliberately reordered and formatted.",
                (
                    "output: {png_dpi: 150, index_visualization_ranges: "
                    "[{maximum: 1.0, minimum: -1.0, index: NDVI}, "
                    "{minimum: -1.0, index: EVI2, maximum: 1.0}, "
                    "{index: NBR, maximum: 1.0, minimum: -1.0}], "
                    "rgb_max_reflectance: 0.18, rgb_min_reflectance: 0.01, "
                    "maximum_direct_download_dimension: 10000, "
                    "maximum_direct_download_bytes: 32000000, "
                    "maximum_series_download_bytes: 256000000, "
                    "raster_nodata: -9999.0, formats: [json, geojson, png, geotiff], "
                    "artifact_profile: lean, directory: outputs}"
                ),
                (
                    "data: {indices: [NDVI, EVI2, NBR], preserve_water: true, "
                    "mask_high_aerosol: true, earth_engine_reflectance_offset: 0.0, "
                    "earth_engine_reflectance_multiplier: 1.0, "
                    "source_native_reflectance_offset: 0.0, "
                    "source_native_reflectance_scale_factor: 0.0001, "
                    "composition_reducer: median, "
                    "composition_interval: annual, target_resolution_m: 30, "
                    "benchmark_sensor: HLS}"
                ),
                (
                    "forest_baseline: {schema_version: 1.0.0, "
                    "reference_date: 2020-12-31, feature_start_date: 2019-01-01, "
                    "feature_end_date_exclusive: 2021-01-01, benchmark_resolution_m: 30, "
                    "minimum_independent_sources: 2, forest_definition: "
                    "{minimum_area_ha: 0.5, minimum_tree_height_m: 5.0, "
                    "minimum_canopy_cover_percent: 10.0, "
                    "exclude_predominantly_agricultural_or_urban: true}, "
                    "preserve_source_evidence: true, preserve_disagreement: true, "
                    "post_cutoff_observations_allowed: false, "
                    "long_gap_interpolation_allowed: false}"
                ),
                (
                    "forest_model: {schema_version: 1.0.0, asset_id: "
                    "projects/ee-facuboladerasgee/assets/models/rf_forest_2020, "
                    "asset_type: geemap_tree_feature_collection, tree_property: tree, "
                    "expected_tree_count: 500, reference_year: 2020, "
                    "jurisdiction: entre_rios, non_forest_class_code: 0, "
                    "forest_class_code: 1, training_label_semantics: "
                    "multisource_unanimity_proxy_v1, score_semantics: "
                    "uncalibrated_binary_tree_vote_fraction, evidence_role: "
                    "learned_supporting_evidence, independent_evidence: false, "
                    "temporal_transfer_validated: false, calibrated_probability: false}"
                ),
                (
                    "forest_screening: {schema_version: 1.1.0, "
                    "minimum_core_source_count: 2, expected_tree_count: 500, "
                    "forest_vote_fraction_minimum: 0.8, "
                    "nonforest_vote_fraction_maximum: 0.2, "
                    "core_nonforest_max_evidence_fraction: 0.0, forest_rule: "
                    "rf_complete_and_core_sufficient_and_unanimous_forest_and_"
                    "vote_gte_threshold, nonforest_rule: rf_complete_and_core_sufficient_"
                    "and_unanimous_nonforest_and_vote_lte_threshold, "
                    "temporal_signal_scope: entire_aoi_primary_interpretation_"
                    "automated_forest_only, score_semantics: "
                    "uncalibrated_binary_tree_vote_fraction, "
                    "automatic_final_assessment_allowed: false}"
                ),
                (
                    "disturbance_detection: {schema_version: 1.5.0, "
                    "reference_history_start_date: 2017-01-01, "
                    "analysis_start_date: 2021-01-01, "
                    "minimum_baseline_source_count: 2, "
                    "detection_indices: [NDVI, NBR], "
                    "planned_detectors: [robust_seasonal_pre_post, ccdc_benchmark], "
                    "robust_seasonal: {schema_version: 1.2.0, "
                    "minimum_reference_observations: 3, mad_scale_constant: 1.4826, "
                    "vegetation_loss_direction: decrease_is_positive, "
                    "reference_comparison_policy: same_season_only, "
                    "zero_scale_policy: not_standardizable, "
                    "scale_stabilization_policy: seasonal_index_spatial_quantile_floor, "
                    "scale_floor_quantile: 0.25, "
                    "scale_floor_minimum_valid_pixels: 4, "
                    "scale_floor_fallback_policy: leave_local_scale_unchanged, "
                    "missing_data_policy: preserve_nan_without_interpolation, "
                    "standardized_magnitude_threshold: 3.0, "
                    "minimum_index_support_count: 2, "
                    "minimum_consecutive_signal_periods: 2, "
                    "minimum_valid_post_cutoff_periods: 2, "
                    "missing_period_streak_policy: break_without_recovery_inference, "
                    "long_gap_minimum_consecutive_missing_periods: 2, "
                    "threshold_calibration_status: "
                    "conservative_benchmark_requires_independent_validation, "
                    "score_aggregation: maximum_positive_standardized_magnitude, "
                    "anomaly_threshold_applied: true, "
                    "persistence_rule_applied: true}, "
                    "ccdc_benchmark: {schema_version: 1.0.0, "
                    "earth_engine_python_api_version: 1.7.36, "
                    "api_reference_url: "
                    "https://developers.google.com/earth-engine/apidocs/"
                    "ee-algorithms-temporalsegmentation-ccdc, "
                    "api_reference_last_updated: 2026-04-20, "
                    "api_reference_accessed_at: 2026-07-29, "
                    "output_semantics_reference_url: "
                    "https://developers.google.com/earth-engine/datasets/catalog/"
                    "GOOGLE_GLOBAL_CCDC_V1, "
                    "source_profile: hlsl30_only, source_product: HLSL30, "
                    "input_temporal_granularity: dense_masked_observations, "
                    "breakpoint_bands: [NDVI, NBR], min_observations: 6, "
                    "chi_square_probability: 0.99, min_num_of_years_scaler: 1.33, "
                    "date_format: 1, lambda: 20.0, max_iterations: 25000, "
                    "tmask_policy: disabled_hls_fmask_preapplied, tmask_bands: [], "
                    "magnitude_direction: decrease_is_positive, "
                    "change_probability_semantics: "
                    "algorithmic_breakpoint_pseudo_probability_not_deforestation_probability, "
                    "comparative_sensor_profile: hlsl30_hlss30_deferred, "
                    "comparative_sensor_profile_enabled: false, "
                    "raw_array_output_preserved: true, "
                    "post_cutoff_break_threshold_applied: false}, "
                    "convergence: {schema_version: 1.0.0, "
                    "temporal_compatibility_policy: "
                    "ccdc_break_within_robust_signal_interval, "
                    "interval_boundary_policy: closed_start_open_end, "
                    "temporal_compatibility_margin_days: 0, "
                    "unavailable_detector_policy: "
                    "preserve_available_evidence_without_convergence, "
                    "isolated_ccdc_break_policy: transient_signal_requires_review, "
                    "detector_disagreement_resolution_policy: preserve, "
                    "score_fusion_allowed: false}, "
                    "score_semantics: uncalibrated_disturbance_evidence_score, "
                    "baseline_disagreement_policy: evaluate_and_flag, "
                    "cutoff_straddling_period_policy: exclude, "
                    "preserve_detector_evidence: true, "
                    "preserve_detector_disagreement: true, "
                    "long_gap_interpolation_allowed: false, attribution_allowed: false, "
                    "spatial_area_threshold_applied: false, "
                    "automatic_final_assessment_allowed: false}"
                ),
                (
                    "disturbance_events: {schema_version: 1.2.0, connectivity: 8, "
                    "visec_operational_event_area_threshold_ha: 0.5, "
                    "visec_area_threshold_relation: strictly_greater_than, "
                    "area_threshold_basis: candidate_footprint_reference_only_no_event_"
                    "promotion, area_threshold_policy: preserve_all_candidates_publish_"
                    "events_only_after_attribution, area_method: "
                    "projected_grid_affine_determinant, maximum_onset_period_difference: 1, "
                    "missing_onset_policy: separate_unknown_episode, event_id_strategy: "
                    "grid_temporal_episode_and_pixels_sha256_v2, ordering_policy: "
                    "area_descending_then_event_id, detail_figure_limit: 5, "
                    "detail_selection_policy: largest_area_then_event_id, "
                    "comparison_policy: latest_pre_cutoff_same_season_vs_"
                    "earliest_event_onset_window, geometry_crs: EPSG:4326, "
                    "primary_interpretation_domain: automated_forest, "
                    "automatic_final_assessment_allowed: false, "
                    "attribution_allowed: false}"
                ),
                (
                    "spatial: {preserve_subthreshold_events: true, "
                    "minimum_coordinate_decimals: 6, forest_definition_min_area_ha: 0.5, "
                    "raster_crs_strategy: local_utm, area_crs_strategy: auto_equal_area, "
                    "interchange_crs: EPSG:4326}"
                ),
                (
                    "analysis: {random_seed: 7, analysis_end_date: 2021-01-01, "
                    "benchmark_start_date: 2019-01-01, cutoff_date: 2020-12-31}"
                ),
                'schema_version: "1.13.0"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    first_resolved = resolve_run_config(load_config(first), date(2021, 1, 1))
    second_resolved = resolve_run_config(load_config(second), date(2021, 1, 1))
    assert execution_config_hash(first_resolved) == execution_config_hash(second_resolved)


def test_scientific_hash_changes_when_a_valid_parameter_changes() -> None:
    """Cambiar un parámetro válido modifica la identidad vigente."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    baseline = resolve_run_config(PipelineConfig.model_validate(payload), date(2026, 7, 30))
    payload["analysis"]["random_seed"] = 43
    changed = resolve_run_config(PipelineConfig.model_validate(payload), date(2026, 7, 30))

    assert scientific_parameters_hash(changed) != scientific_parameters_hash(baseline)


def test_scientific_hash_treats_index_order_as_significant() -> None:
    """Caracteriza que el orden de índices y formatos forma parte del digest."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    baseline = resolve_run_config(PipelineConfig.model_validate(payload), date(2026, 7, 30))
    payload["data"]["indices"] = list(reversed(payload["data"]["indices"]))
    changed = resolve_run_config(PipelineConfig.model_validate(payload), date(2026, 7, 30))

    assert scientific_parameters_hash(changed) != scientific_parameters_hash(baseline)


def test_indices_are_deeply_immutable_and_cannot_change_the_hash() -> None:
    """Los índices validados no pueden mutarse luego de calcular su identidad."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    digest = scientific_parameters_hash(resolve_run_config(config, date(2026, 7, 30)))

    with pytest.raises(TypeError):
        cast(Any, config.data.indices)[0] = "EVI2"

    assert scientific_parameters_hash(resolve_run_config(config, date(2026, 7, 30))) == digest


def test_output_formats_are_deeply_immutable_and_cannot_change_the_hash() -> None:
    """Los formatos validados no pueden mutarse luego de calcular su identidad."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    digest = execution_config_hash(resolve_run_config(config, date(2026, 7, 30)))

    with pytest.raises(TypeError):
        cast(Any, config.output.formats)[0] = "csv"

    assert execution_config_hash(resolve_run_config(config, date(2026, 7, 30))) == digest


def test_resolve_run_config_replaces_a_missing_analysis_end_date() -> None:
    """Una corrida recibe siempre una fecha final concreta y explícita."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    resolved = resolve_run_config(config, date(2024, 12, 31))

    assert isinstance(resolved, ResolvedPipelineConfig)
    assert resolved.analysis.analysis_end_date == date(2024, 12, 31)


def test_resolve_run_config_preserves_an_explicit_matching_end_date() -> None:
    """Una fecha declarada se conserva cuando coincide con la de la corrida."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload["analysis"]["analysis_end_date"] = date(2024, 12, 31)
    config = PipelineConfig.model_validate(payload)

    resolved = resolve_run_config(config, date(2024, 12, 31))

    assert resolved.analysis.analysis_end_date == date(2024, 12, 31)


def test_resolve_run_config_rejects_a_conflicting_explicit_end_date() -> None:
    """La fecha efectiva no puede contradecir un YAML ya fijado."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload["analysis"]["analysis_end_date"] = date(2024, 12, 31)
    config = PipelineConfig.model_validate(payload)

    with pytest.raises(ValueError, match="analysis_end_date"):
        resolve_run_config(config, date(2025, 1, 1))


def test_resolve_run_config_does_not_mutate_the_source_config() -> None:
    """Resolver una corrida devuelve otro modelo y conserva el config de entrada."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    resolved = resolve_run_config(config, date(2024, 12, 31))

    assert config.analysis.analysis_end_date is None
    assert resolved is not config


def test_scientific_hash_ignores_operational_output_changes() -> None:
    """La identidad científica excluye el destino y formatos de salida."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    baseline = resolve_run_config(config, date(2024, 12, 31))
    payload = baseline.model_dump()
    payload["output"]["directory"] = "other-output"
    changed = ResolvedPipelineConfig.model_validate(payload)

    assert scientific_parameters_hash(changed) == scientific_parameters_hash(baseline)


def test_execution_hash_includes_operational_output_changes() -> None:
    """La identidad de ejecución incluye el destino y formatos de salida."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    baseline = resolve_run_config(config, date(2024, 12, 31))
    payload = baseline.model_dump()
    payload["output"]["directory"] = "other-output"
    changed = ResolvedPipelineConfig.model_validate(payload)

    assert execution_config_hash(changed) != execution_config_hash(baseline)


def test_resolved_end_date_changes_scientific_and_execution_identities() -> None:
    """El período efectivo forma parte de ambas identidades de la corrida."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    first = resolve_run_config(config, date(2024, 12, 31))
    second = resolve_run_config(config, date(2025, 1, 1))

    assert scientific_parameters_hash(first) != scientific_parameters_hash(second)
    assert execution_config_hash(first) != execution_config_hash(second)


def test_config_rejects_a_modified_cutoff_date() -> None:
    """La fecha de corte no puede usarse como hiperparámetro."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload["analysis"]["cutoff_date"] = date(2021, 1, 1)

    with pytest.raises(ValidationError, match="cutoff_date debe ser 2020-12-31"):
        PipelineConfig.model_validate(payload)


def test_config_rejects_a_modified_forest_area_threshold() -> None:
    """El umbral de superficie forestal no puede calibrarse con el modelo."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload["spatial"]["forest_definition_min_area_ha"] = 0.6

    with pytest.raises(ValidationError, match=r"debe ser 0\.5"):
        PipelineConfig.model_validate(payload)


def test_license_registry_records_operational_pixel_sources() -> None:
    """Toda fuente seleccionada para producir píxeles registra sus condiciones."""
    registry = load_license_registry(PROJECT_ROOT / "data" / "licenses.yml")

    assert registry.schema_version == "1.0.0"
    assert tuple(dataset.dataset_id for dataset in registry.datasets) == (
        "hls_l30_v2",
        "hls_s30_v2",
        "jrc_gfc2020_v3",
        "esa_worldcover_2020_v100",
        "hansen_gfc_2025_v1_13",
        "mapbiomas_argentina_collection2",
        "dynamic_world_v1",
    )
    assert all(dataset.attribution.strip() for dataset in registry.datasets)
    assert all(
        "modified" in dataset.attribution.lower()
        for dataset in registry.datasets
        if dataset.dataset_id != "mapbiomas_argentina_collection2"
    )
    mapbiomas = next(
        dataset
        for dataset in registry.datasets
        if dataset.dataset_id == "mapbiomas_argentina_collection2"
    )
    assert "mapbiomas" in mapbiomas.attribution.lower()
    assert "colección 2" in mapbiomas.attribution.lower()
    assert all(dataset.restrictions for dataset in registry.datasets)


def test_yaml_root_must_be_a_mapping(tmp_path: Path) -> None:
    """Un documento escalar no puede validarse como configuración."""
    invalid_config = tmp_path / "invalid.yml"
    invalid_config.write_text("- item\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="debe ser un objeto YAML"):
        load_config(invalid_config)
