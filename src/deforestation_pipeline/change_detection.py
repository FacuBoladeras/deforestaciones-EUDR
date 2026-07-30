"""Contrato del Paso 14 para detectar perturbaciones sin atribuir deforestación."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deforestation_pipeline.artifact_layout import (
    evidence_figure,
    evidence_json,
    evidence_table,
    evidence_tiff,
)
from deforestation_pipeline.schemas import EUDR_CUTOFF_DATE

DISTURBANCE_DETECTION_SCHEMA_VERSION: Literal["1.4.0"] = "1.4.0"
DISTURBANCE_DETECTION_SCHEMA_ID = "urn:deforestation-pipeline:disturbance-detection:1.4.0"


class DisturbanceStateCode(IntEnum):
    """Estados de detección; ninguno representa atribución o cumplimiento."""

    NOT_EVALUABLE = 0
    STABLE_OBSERVED = 1
    TRANSIENT_SIGNAL = 2
    PERSISTENT_CANDIDATE = 3
    DETECTOR_DISAGREEMENT = 4


class DisturbanceQualityBit(IntEnum):
    """Posiciones estables de un futuro bitmask raster de calidad."""

    BASELINE_UNCERTAIN = 0
    INSUFFICIENT_REFERENCE_HISTORY = 1
    INSUFFICIENT_POST_CUTOFF_OBSERVATIONS = 2
    PERSISTENT_CLOUD_OR_SHADOW = 3
    CUTOFF_STRADDLING_PERIOD_EXCLUDED = 4
    SENSOR_IMBALANCE = 5
    LONG_OBSERVATION_GAP = 6
    CCDC_FIT_FAILURE = 7
    EDGE_PIXEL = 8
    CCDC_INSUFFICIENT_OBSERVATIONS = 9


class DetectorConvergenceReasonCode(IntEnum):
    """Motivo estable y auditable de la convergencia conservadora 14.5."""

    NO_DETECTOR_AVAILABLE = 0
    BOTH_STABLE = 1
    ROBUST_TRANSIENT_CCDC_NO_BREAK = 2
    COMPATIBLE_TRANSIENT_SIGNALS = 3
    COMPATIBLE_PERSISTENT_SIGNALS = 4
    DETECTOR_POLARITY_DISAGREEMENT = 5
    POSITIVE_SIGNALS_TEMPORALLY_INCOMPATIBLE = 6
    ROBUST_ONLY_STABLE = 7
    ROBUST_ONLY_TRANSIENT = 8
    ROBUST_ONLY_PERSISTENT = 9
    CCDC_ONLY_STABLE = 10
    CCDC_ONLY_BREAK_REQUIRES_REVIEW = 11


class ForestEvaluationDomainCode(IntEnum):
    """Clasificación de elegibilidad forestal previa a cualquier detector."""

    NOT_EVALUABLE_INSUFFICIENT_SOURCES = 0
    NOT_EVALUABLE_NON_FOREST = 1
    EVALUABLE_CONSENSUS_FOREST = 2
    EVALUABLE_BASELINE_UNCERTAIN = 3


class DisturbanceDetector(StrEnum):
    """Detectores planificados con roles diferentes y evidencia separada."""

    ROBUST_SEASONAL_PRE_POST = "robust_seasonal_pre_post"
    CCDC_BENCHMARK = "ccdc_benchmark"


class DisturbanceIndex(StrEnum):
    """Subconjunto inicial, deliberadamente pequeño, para detectar señales."""

    NDVI = "NDVI"
    NBR = "NBR"
    NDMI = "NDMI"
    NIRV = "NIRv"


class SeasonalPeriod(StrEnum):
    """Estaciones meteorológicas usadas sin mezclar respuestas fenológicas."""

    DJF = "DJF"
    MAM = "MAM"
    JJA = "JJA"
    SON = "SON"


DISTURBANCE_SUMMARY_BAND_NAMES = (
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

DISTURBANCE_DIAGNOSTIC_BAND_NAMES = (
    "robust_multi_index_support_count",
    "robust_max_standardized_anomaly",
    "ccdc_break_day_offset_from_cutoff",
    "ccdc_change_magnitude",
    "recovery_indicator",
    "baseline_evidence_fraction",
)


class RobustSeasonalDetectorConfig(BaseModel):
    """Parámetros versionados del diagnóstico 14.2 y su regla de señal 14.3."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1.0"] = "1.1.0"
    minimum_reference_observations: Annotated[int, Field(ge=1, strict=True)]
    mad_scale_constant: Annotated[float, Field(gt=0, strict=True)]
    vegetation_loss_direction: Literal["decrease_is_positive"]
    reference_comparison_policy: Literal["same_season_only"]
    zero_scale_policy: Literal["not_standardizable"]
    missing_data_policy: Literal["preserve_nan_without_interpolation"]
    standardized_magnitude_threshold: Annotated[float, Field(gt=0, strict=True)]
    minimum_index_support_count: Annotated[int, Field(ge=2, strict=True)]
    minimum_consecutive_signal_periods: Annotated[int, Field(ge=2, strict=True)]
    minimum_valid_post_cutoff_periods: Annotated[int, Field(ge=2, strict=True)]
    missing_period_streak_policy: Literal["break_without_recovery_inference"]
    long_gap_minimum_consecutive_missing_periods: Annotated[
        int,
        Field(ge=2, strict=True),
    ]
    threshold_calibration_status: Literal["conservative_benchmark_requires_independent_validation"]
    score_aggregation: Literal["maximum_positive_standardized_magnitude"]
    anomaly_threshold_applied: Literal[True]
    persistence_rule_applied: Literal[True]


class CcdcBenchmarkConfig(BaseModel):
    """Perfil reproducible para el benchmark CCDC de Earth Engine."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_version: Literal["1.0.0"] = "1.0.0"
    earth_engine_python_api_version: Literal["1.7.36"]
    api_reference_url: Literal[
        "https://developers.google.com/earth-engine/apidocs/ee-algorithms-temporalsegmentation-ccdc"
    ]
    api_reference_last_updated: date
    api_reference_accessed_at: date
    output_semantics_reference_url: Literal[
        "https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_GLOBAL_CCDC_V1"
    ]
    source_profile: Literal["hlsl30_only"]
    source_product: Literal["HLSL30"]
    input_temporal_granularity: Literal["dense_masked_observations"]
    breakpoint_bands: Annotated[tuple[DisturbanceIndex, ...], Field(min_length=1)]
    min_observations: Annotated[int, Field(ge=1, strict=True)]
    chi_square_probability: Annotated[float, Field(ge=0, le=1, strict=True)]
    min_num_of_years_scaler: Annotated[float, Field(gt=0, strict=True)]
    date_format: Literal[1]
    lambda_: Annotated[float, Field(alias="lambda", ge=0, strict=True)]
    max_iterations: Annotated[int, Field(ge=0, strict=True)]
    tmask_policy: Literal["disabled_hls_fmask_preapplied"]
    tmask_bands: tuple[str, ...]
    magnitude_direction: Literal["decrease_is_positive"]
    change_probability_semantics: Literal[
        "algorithmic_breakpoint_pseudo_probability_not_deforestation_probability"
    ]
    comparative_sensor_profile: Literal["hlsl30_hlss30_deferred"]
    comparative_sensor_profile_enabled: Literal[False]
    raw_array_output_preserved: Literal[True]
    post_cutoff_break_threshold_applied: Literal[False]

    @field_validator("breakpoint_bands")
    @classmethod
    def breakpoint_bands_are_unique(
        cls,
        value: tuple[DisturbanceIndex, ...],
    ) -> tuple[DisturbanceIndex, ...]:
        if len(value) != len(set(value)):
            raise ValueError("breakpoint_bands contiene duplicados")
        return value

    @model_validator(mode="after")
    def tmask_and_lasso_policies_are_coherent(self) -> CcdcBenchmarkConfig:
        if self.api_reference_accessed_at < self.api_reference_last_updated:
            raise ValueError("api_reference_accessed_at no puede preceder la actualización")
        if self.tmask_bands:
            raise ValueError("tmask_bands debe estar vacío cuando HLS Fmask ya fue aplicado")
        if (self.lambda_ == 0) != (self.max_iterations == 0):
            raise ValueError("lambda y max_iterations deben seleccionar juntos LASSO u OLS")
        return self


class DetectorConvergenceConfig(BaseModel):
    """Políticas 14.5 sin fusión de escalas ni resolución silenciosa."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    temporal_compatibility_policy: Literal["ccdc_break_within_robust_signal_interval"]
    interval_boundary_policy: Literal["closed_start_open_end"]
    temporal_compatibility_margin_days: Literal[0]
    unavailable_detector_policy: Literal["preserve_available_evidence_without_convergence"]
    isolated_ccdc_break_policy: Literal["transient_signal_requires_review"]
    detector_disagreement_resolution_policy: Literal["preserve"]
    score_fusion_allowed: Literal[False]


class DisturbanceDetectionConfig(BaseModel):
    """Políticas científicas previas a implementar o parametrizar detectores."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.4.0"] = DISTURBANCE_DETECTION_SCHEMA_VERSION
    reference_history_start_date: date
    analysis_start_date: date
    minimum_baseline_source_count: Annotated[int, Field(ge=2)]
    detection_indices: Annotated[tuple[DisturbanceIndex, ...], Field(min_length=2)]
    planned_detectors: tuple[DisturbanceDetector, DisturbanceDetector]
    robust_seasonal: RobustSeasonalDetectorConfig
    ccdc_benchmark: CcdcBenchmarkConfig
    convergence: DetectorConvergenceConfig
    score_semantics: Literal["uncalibrated_disturbance_evidence_score"]
    baseline_disagreement_policy: Literal["evaluate_and_flag"]
    cutoff_straddling_period_policy: Literal["exclude"]
    preserve_detector_evidence: Literal[True]
    preserve_detector_disagreement: Literal[True]
    long_gap_interpolation_allowed: Literal[False]
    attribution_allowed: Literal[False]
    spatial_area_threshold_applied: Literal[False]
    automatic_final_assessment_allowed: Literal[False]

    @field_validator("analysis_start_date")
    @classmethod
    def analysis_starts_after_cutoff(cls, value: date) -> date:
        if value != EUDR_CUTOFF_DATE + timedelta(days=1):
            raise ValueError("analysis_start_date debe ser 2021-01-01")
        return value

    @field_validator("reference_history_start_date")
    @classmethod
    def reference_history_starts_on_calendar_year(cls, value: date) -> date:
        if (value.month, value.day) != (1, 1):
            raise ValueError("reference_history_start_date debe comenzar el 1 de enero")
        if value >= EUDR_CUTOFF_DATE:
            raise ValueError("reference_history_start_date debe ser anterior a la fecha de corte")
        return value

    @field_validator("detection_indices")
    @classmethod
    def detection_indices_are_unique(
        cls,
        value: tuple[DisturbanceIndex, ...],
    ) -> tuple[DisturbanceIndex, ...]:
        if len(value) != len(set(value)):
            raise ValueError("detection_indices contiene duplicados")
        return value

    @model_validator(mode="after")
    def detector_roles_are_fixed(self) -> DisturbanceDetectionConfig:
        expected = (
            DisturbanceDetector.ROBUST_SEASONAL_PRE_POST,
            DisturbanceDetector.CCDC_BENCHMARK,
        )
        if self.planned_detectors != expected:
            raise ValueError("planned_detectors debe conservar detector robusto y benchmark CCDC")
        robust = self.robust_seasonal
        if robust.minimum_index_support_count > len(self.detection_indices):
            raise ValueError("minimum_index_support_count no puede superar detection_indices")
        if robust.minimum_valid_post_cutoff_periods < robust.minimum_consecutive_signal_periods:
            raise ValueError(
                "minimum_valid_post_cutoff_periods debe ser al menos "
                "minimum_consecutive_signal_periods"
            )
        if self.ccdc_benchmark.breakpoint_bands != self.detection_indices:
            raise ValueError("ccdc breakpoint_bands debe coincidir con detection_indices")
        return self


@dataclass(frozen=True, slots=True)
class ForestEvaluationDomain:
    """Dominio sintético 14.1; no contiene señales ni decisiones de cambio."""

    domain_code: NDArray[np.uint8]
    evaluable: NDArray[np.bool_]
    baseline_uncertain: NDArray[np.bool_]
    quality_flags_bitmask: NDArray[np.uint16]


@dataclass(frozen=True, slots=True)
class RobustSeasonalDiagnostics:
    """Diagnósticos continuos 14.2; no contienen estados ni probabilidades."""

    reference_center: NDArray[np.float64]
    robust_scale: NDArray[np.float64]
    raw_directional_delta: NDArray[np.float64]
    standardized_magnitude: NDArray[np.float64]
    reference_valid_count: NDArray[np.uint16]
    quality_flags_bitmask: NDArray[np.uint16]


@dataclass(frozen=True, slots=True)
class RobustSeasonalSignalResult:
    """Regla 14.3 sobre diagnósticos robustos; no atribuye ni calibra riesgo."""

    period_signal_mask: NDArray[np.bool_]
    period_multi_index_support_count: NDArray[np.uint8]
    state_code: NDArray[np.uint8]
    first_signal_period_index: NDArray[np.int32]
    maximum_disturbance_magnitude: NDArray[np.float64]
    maximum_consecutive_signal_periods: NDArray[np.uint16]
    maximum_multi_index_support_count: NDArray[np.uint8]
    valid_post_cutoff_observation_count: NDArray[np.uint16]
    uncalibrated_disturbance_evidence_score: NDArray[np.float64]
    quality_flags_bitmask: NDArray[np.uint16]


@dataclass(frozen=True, slots=True)
class CcdcBenchmarkResult:
    """Resumen puro del benchmark; conserva semántica CCDC sin atribución."""

    fit_eligible: NDArray[np.bool_]
    fit_succeeded: NDArray[np.bool_]
    has_post_cutoff_break: NDArray[np.bool_]
    first_post_cutoff_break_segment_index: NDArray[np.int32]
    first_post_cutoff_break_fractional_year: NDArray[np.float64]
    ccdc_breakpoint_pseudo_probability: NDArray[np.float64]
    raw_signed_magnitude: NDArray[np.float64]
    decrease_positive_magnitude: NDArray[np.float64]
    first_break_segment_observation_count: NDArray[np.uint16]
    total_valid_observation_count: NDArray[np.uint16]
    quality_flags_bitmask: NDArray[np.uint16]


@dataclass(frozen=True, slots=True)
class DetectorConvergenceResult:
    """Convergencia 14.5; conserva evidencia y escalas por detector."""

    state_code: NDArray[np.uint8]
    reason_code: NDArray[np.uint8]
    available_detector_count: NDArray[np.uint8]
    agreeing_detector_count: NDArray[np.uint8]
    robust_supports_signal: NDArray[np.bool_]
    ccdc_supports_break: NDArray[np.bool_]
    first_robust_signal_period_index: NDArray[np.int32]
    first_ccdc_break_fractional_year: NDArray[np.float64]
    robust_maximum_disturbance_magnitude: NDArray[np.float64]
    robust_uncalibrated_disturbance_evidence_score: NDArray[np.float64]
    ccdc_breakpoint_pseudo_probability: NDArray[np.float64]
    ccdc_decrease_positive_magnitude: NDArray[np.float64]
    quality_flags_bitmask: NDArray[np.uint16]


def build_forest_evaluation_domain(
    *,
    source_count: NDArray[Any],
    consensus_forest: NDArray[Any],
    disagreement: NDArray[Any],
    config: DisturbanceDetectionConfig,
) -> ForestEvaluationDomain:
    """Delimita dónde detectar y conserva la incertidumbre de la línea base."""
    counts = np.asarray(source_count)
    consensus = np.asarray(consensus_forest)
    disagrees = np.asarray(disagreement)
    _validate_forest_domain_inputs(
        source_count=counts,
        consensus_forest=consensus,
        disagreement=disagrees,
        minimum_source_count=config.minimum_baseline_source_count,
    )

    sufficient_sources = counts >= config.minimum_baseline_source_count
    consensus_mask = consensus.astype(np.bool_, copy=False)
    disagreement_mask = disagrees.astype(np.bool_, copy=False)
    evaluable = sufficient_sources & (consensus_mask | disagreement_mask)
    baseline_uncertain = (~sufficient_sources) | disagreement_mask

    domain_code = np.full(
        counts.shape,
        ForestEvaluationDomainCode.NOT_EVALUABLE_INSUFFICIENT_SOURCES,
        dtype=np.uint8,
    )
    domain_code[sufficient_sources] = ForestEvaluationDomainCode.NOT_EVALUABLE_NON_FOREST
    domain_code[consensus_mask] = ForestEvaluationDomainCode.EVALUABLE_CONSENSUS_FOREST
    domain_code[disagreement_mask] = ForestEvaluationDomainCode.EVALUABLE_BASELINE_UNCERTAIN

    quality_flags = np.zeros(counts.shape, dtype=np.uint16)
    baseline_uncertain_mask = np.uint16(1 << DisturbanceQualityBit.BASELINE_UNCERTAIN)
    quality_flags[baseline_uncertain] |= baseline_uncertain_mask
    arrays = (domain_code, evaluable, baseline_uncertain, quality_flags)
    for array in arrays:
        array.setflags(write=False)
    return ForestEvaluationDomain(
        domain_code=domain_code,
        evaluable=evaluable,
        baseline_uncertain=baseline_uncertain,
        quality_flags_bitmask=quality_flags,
    )


def summarize_ccdc_segments(
    *,
    break_time_fractional_year: NDArray[Any],
    change_pseudo_probability: NDArray[Any],
    segment_observation_count: NDArray[Any],
    signed_magnitude: NDArray[Any],
    total_valid_observation_count: NDArray[Any],
    fit_succeeded: NDArray[Any],
    band_names: tuple[str, ...],
    domain: ForestEvaluationDomain,
    config: DisturbanceDetectionConfig,
) -> CcdcBenchmarkResult:
    """Selecciona la primera ruptura poscorte desde arrays CCDC sintéticos."""
    break_times = np.asarray(break_time_fractional_year)
    pseudo_probability = np.asarray(change_pseudo_probability)
    segment_observations = np.asarray(segment_observation_count)
    magnitudes = np.asarray(signed_magnitude)
    total_observations = np.asarray(total_valid_observation_count)
    supplied_fit = np.asarray(fit_succeeded)
    _validate_ccdc_segment_inputs(
        break_times=break_times,
        pseudo_probability=pseudo_probability,
        segment_observations=segment_observations,
        magnitudes=magnitudes,
        total_observations=total_observations,
        supplied_fit=supplied_fit,
        band_names=band_names,
        domain=domain,
        config=config,
    )

    ccdc = config.ccdc_benchmark
    spatial_shape = total_observations.shape
    evaluable = np.asarray(domain.evaluable, dtype=np.bool_)
    fit_eligible = evaluable & (total_observations >= ccdc.min_observations)
    fit_ok = fit_eligible & supplied_fit.astype(np.bool_, copy=False)
    post_cutoff_fractional_year = float(config.analysis_start_date.year)
    candidates = (
        np.isfinite(break_times)
        & (break_times >= post_cutoff_fractional_year)
        & fit_ok[np.newaxis, :, :]
    )

    first_segment = np.full(spatial_shape, -1, dtype=np.int32)
    first_break = np.full(spatial_shape, np.nan, dtype=np.float64)
    first_pseudo_probability = np.full(spatial_shape, np.nan, dtype=np.float64)
    first_segment_observations = np.zeros(spatial_shape, dtype=np.uint16)
    normalized_total_observations = total_observations.astype(np.uint16, copy=True)
    raw_magnitude = np.full(
        (len(band_names), *spatial_shape),
        np.nan,
        dtype=np.float64,
    )
    has_break = candidates.any(axis=0)
    if break_times.shape[0] > 0:
        sortable_breaks = np.where(candidates, break_times, np.inf)
        candidate_index = np.argmin(sortable_breaks, axis=0).astype(np.int32)
        first_segment[has_break] = candidate_index[has_break]
        for segment_index in range(break_times.shape[0]):
            selected = has_break & (candidate_index == segment_index)
            first_break[selected] = break_times[segment_index][selected]
            first_pseudo_probability[selected] = pseudo_probability[segment_index][selected]
            first_segment_observations[selected] = segment_observations[segment_index][selected]
            for band_index in range(len(band_names)):
                raw_magnitude[band_index][selected] = magnitudes[segment_index, band_index][
                    selected
                ]

    decrease_positive_magnitude = -raw_magnitude
    quality_flags = np.asarray(domain.quality_flags_bitmask, dtype=np.uint16).copy()
    insufficient = evaluable & ~fit_eligible
    insufficient_bit = np.uint16(1 << DisturbanceQualityBit.CCDC_INSUFFICIENT_OBSERVATIONS)
    quality_flags[insufficient] |= insufficient_bit
    fit_failed = fit_eligible & ~fit_ok
    fit_failure_bit = np.uint16(1 << DisturbanceQualityBit.CCDC_FIT_FAILURE)
    quality_flags[fit_failed] |= fit_failure_bit

    arrays = (
        fit_eligible,
        fit_ok,
        has_break,
        first_segment,
        first_break,
        first_pseudo_probability,
        raw_magnitude,
        decrease_positive_magnitude,
        first_segment_observations,
        normalized_total_observations,
        quality_flags,
    )
    immutable_arrays: list[NDArray[Any]] = []
    for array in arrays:
        owned = np.array(array, copy=True)
        owned.setflags(write=False)
        immutable_arrays.append(owned)
    return CcdcBenchmarkResult(
        fit_eligible=immutable_arrays[0],
        fit_succeeded=immutable_arrays[1],
        has_post_cutoff_break=immutable_arrays[2],
        first_post_cutoff_break_segment_index=immutable_arrays[3],
        first_post_cutoff_break_fractional_year=immutable_arrays[4],
        ccdc_breakpoint_pseudo_probability=immutable_arrays[5],
        raw_signed_magnitude=immutable_arrays[6],
        decrease_positive_magnitude=immutable_arrays[7],
        first_break_segment_observation_count=immutable_arrays[8],
        total_valid_observation_count=immutable_arrays[9],
        quality_flags_bitmask=immutable_arrays[10],
    )


def compute_robust_seasonal_diagnostics(
    *,
    reference_values: NDArray[Any],
    reference_seasons: tuple[str, ...],
    reference_period_start_dates: tuple[date, ...],
    reference_period_end_dates_exclusive: tuple[date, ...],
    post_cutoff_values: NDArray[Any],
    post_cutoff_seasons: tuple[str, ...],
    post_period_start_dates: tuple[date, ...],
    post_period_end_dates_exclusive: tuple[date, ...],
    index_names: tuple[str, ...],
    domain: ForestEvaluationDomain,
    config: DisturbanceDetectionConfig,
) -> RobustSeasonalDiagnostics:
    """Compara cada período poscorte con su misma estación histórica."""
    reference = np.asarray(reference_values)
    post = np.asarray(post_cutoff_values)
    _validate_robust_seasonal_inputs(
        reference=reference,
        reference_seasons=reference_seasons,
        reference_start_dates=reference_period_start_dates,
        reference_end_dates_exclusive=reference_period_end_dates_exclusive,
        post=post,
        post_seasons=post_cutoff_seasons,
        post_start_dates=post_period_start_dates,
        post_end_dates_exclusive=post_period_end_dates_exclusive,
        index_names=index_names,
        domain=domain,
        config=config,
    )

    reference_float = reference.astype(np.float64, copy=True)
    post_float = post.astype(np.float64, copy=True)
    output_shape = post_float.shape
    center_output = np.full(output_shape, np.nan, dtype=np.float64)
    scale_output = np.full(output_shape, np.nan, dtype=np.float64)
    raw_delta_output = np.full(output_shape, np.nan, dtype=np.float64)
    standardized_output = np.full(output_shape, np.nan, dtype=np.float64)
    valid_count_output = np.zeros(output_shape, dtype=np.uint16)
    quality_flags = np.broadcast_to(
        np.asarray(domain.quality_flags_bitmask, dtype=np.uint16),
        output_shape,
    ).copy()
    evaluable = np.asarray(domain.evaluable, dtype=np.bool_)
    robust_config = config.robust_seasonal
    insufficient_bit = np.uint16(1 << DisturbanceQualityBit.INSUFFICIENT_REFERENCE_HISTORY)
    excluded_bit = np.uint16(1 << DisturbanceQualityBit.CUTOFF_STRADDLING_PERIOD_EXCLUDED)

    reference_season_array = np.asarray(reference_seasons)
    for post_index, season in enumerate(post_cutoff_seasons):
        straddles_cutoff = (
            post_period_start_dates[post_index]
            < config.analysis_start_date
            < post_period_end_dates_exclusive[post_index]
        )
        if straddles_cutoff:
            quality_flags[post_index] |= excluded_bit
            continue

        same_season = reference_season_array == season
        seasonal_reference = reference_float[same_season]
        finite_reference = np.isfinite(seasonal_reference)
        valid_count = finite_reference.sum(axis=0, dtype=np.uint16)
        valid_count[:, ~evaluable] = 0
        valid_count_output[post_index] = valid_count

        insufficient = evaluable[np.newaxis, :, :] & (
            valid_count < robust_config.minimum_reference_observations
        )
        quality_flags[post_index][insufficient] |= insufficient_bit
        sufficient = evaluable[np.newaxis, :, :] & ~insufficient
        observed = np.isfinite(post_float[post_index])
        diagnostic_mask = sufficient & observed
        if not np.any(diagnostic_mask):
            continue

        with np.errstate(invalid="ignore"):
            center = np.nanmedian(seasonal_reference, axis=0)
            absolute_deviation = np.abs(seasonal_reference - center[np.newaxis])
            mad = np.nanmedian(absolute_deviation, axis=0)
        scale = mad * robust_config.mad_scale_constant
        raw_delta = center - post_float[post_index]

        center_output[post_index][diagnostic_mask] = center[diagnostic_mask]
        scale_output[post_index][diagnostic_mask] = scale[diagnostic_mask]
        raw_delta_output[post_index][diagnostic_mask] = raw_delta[diagnostic_mask]
        standardizable = diagnostic_mask & (scale > 0)
        standardized_output[post_index][standardizable] = (
            raw_delta[standardizable] / scale[standardizable]
        )

    arrays = (
        center_output,
        scale_output,
        raw_delta_output,
        standardized_output,
        valid_count_output,
        quality_flags,
    )
    for array in arrays:
        array.setflags(write=False)
    return RobustSeasonalDiagnostics(
        reference_center=center_output,
        robust_scale=scale_output,
        raw_directional_delta=raw_delta_output,
        standardized_magnitude=standardized_output,
        reference_valid_count=valid_count_output,
        quality_flags_bitmask=quality_flags,
    )


def classify_robust_seasonal_signals(
    *,
    diagnostics: RobustSeasonalDiagnostics,
    post_period_start_dates: tuple[date, ...],
    post_period_end_dates_exclusive: tuple[date, ...],
    index_names: tuple[str, ...],
    domain: ForestEvaluationDomain,
    config: DisturbanceDetectionConfig,
) -> RobustSeasonalSignalResult:
    """Convierte diagnósticos 14.2 en señales y rachas auditables 14.3."""
    magnitudes = np.asarray(diagnostics.standardized_magnitude)
    _validate_robust_signal_inputs(
        diagnostics=diagnostics,
        magnitudes=magnitudes,
        post_start_dates=post_period_start_dates,
        post_end_dates_exclusive=post_period_end_dates_exclusive,
        index_names=index_names,
        domain=domain,
        config=config,
    )

    robust = config.robust_seasonal
    finite = np.isfinite(magnitudes)
    cutoff_straddling_periods = np.fromiter(
        (
            start < config.analysis_start_date < end
            for start, end in zip(
                post_period_start_dates,
                post_period_end_dates_exclusive,
                strict=True,
            )
        ),
        dtype=np.bool_,
        count=magnitudes.shape[0],
    )
    finite[cutoff_straddling_periods] = False
    valid_index_count = finite.sum(axis=1)
    period_valid = valid_index_count >= robust.minimum_index_support_count
    support_count_wide = (finite & (magnitudes >= robust.standardized_magnitude_threshold)).sum(
        axis=1
    )
    support_count = support_count_wide.astype(np.uint8, copy=False)
    evaluable = np.asarray(domain.evaluable, dtype=np.bool_)
    period_signal = (
        period_valid
        & (support_count >= robust.minimum_index_support_count)
        & evaluable[np.newaxis, :, :]
    )

    valid_period_count = period_valid.sum(axis=0, dtype=np.uint16)
    valid_period_count[~evaluable] = 0
    maximum_support = support_count.max(axis=0)
    maximum_support[~evaluable] = 0

    any_signal = period_signal.any(axis=0)
    first_signal = np.full(evaluable.shape, -1, dtype=np.int32)
    first_signal[any_signal] = np.argmax(period_signal, axis=0)[any_signal].astype(np.int32)

    current_signal_streak = np.zeros(evaluable.shape, dtype=np.uint16)
    maximum_signal_streak = np.zeros(evaluable.shape, dtype=np.uint16)
    current_missing_streak = np.zeros(evaluable.shape, dtype=np.uint16)
    maximum_missing_streak = np.zeros(evaluable.shape, dtype=np.uint16)
    for period_index in range(magnitudes.shape[0]):
        current_signal_streak = np.where(
            period_signal[period_index],
            current_signal_streak + np.uint16(1),
            np.uint16(0),
        ).astype(np.uint16, copy=False)
        maximum_signal_streak = np.maximum(
            maximum_signal_streak,
            current_signal_streak,
        )
        missing = evaluable & ~period_valid[period_index]
        current_missing_streak = np.where(
            missing,
            current_missing_streak + np.uint16(1),
            np.uint16(0),
        ).astype(np.uint16, copy=False)
        maximum_missing_streak = np.maximum(
            maximum_missing_streak,
            current_missing_streak,
        )

    positive_or_missing = np.where(finite, np.maximum(magnitudes, 0.0), -np.inf)
    maximum_magnitude_raw = positive_or_missing.max(axis=(0, 1))
    any_finite = finite.any(axis=(0, 1))
    maximum_magnitude = np.full(evaluable.shape, np.nan, dtype=np.float64)
    has_magnitude = evaluable & any_finite
    maximum_magnitude[has_magnitude] = maximum_magnitude_raw[has_magnitude]
    evidence_score = maximum_magnitude.copy()

    inherited_flags = np.bitwise_or.reduce(
        np.asarray(diagnostics.quality_flags_bitmask, dtype=np.uint16),
        axis=(0, 1),
    )
    quality_flags = inherited_flags | np.asarray(
        domain.quality_flags_bitmask,
        dtype=np.uint16,
    )
    if np.any(cutoff_straddling_periods):
        excluded_bit = np.uint16(1 << DisturbanceQualityBit.CUTOFF_STRADDLING_PERIOD_EXCLUDED)
        quality_flags |= excluded_bit
    insufficient = evaluable & (valid_period_count < robust.minimum_valid_post_cutoff_periods)
    insufficient_bit = np.uint16(1 << DisturbanceQualityBit.INSUFFICIENT_POST_CUTOFF_OBSERVATIONS)
    quality_flags[insufficient] |= insufficient_bit
    long_gap = evaluable & (
        maximum_missing_streak >= robust.long_gap_minimum_consecutive_missing_periods
    )
    long_gap_bit = np.uint16(1 << DisturbanceQualityBit.LONG_OBSERVATION_GAP)
    quality_flags[long_gap] |= long_gap_bit

    sufficiently_observed = evaluable & ~insufficient
    state_code = np.full(
        evaluable.shape,
        DisturbanceStateCode.NOT_EVALUABLE,
        dtype=np.uint8,
    )
    state_code[sufficiently_observed] = DisturbanceStateCode.STABLE_OBSERVED
    state_code[sufficiently_observed & any_signal] = DisturbanceStateCode.TRANSIENT_SIGNAL
    persistent = sufficiently_observed & (
        maximum_signal_streak >= robust.minimum_consecutive_signal_periods
    )
    state_code[persistent] = DisturbanceStateCode.PERSISTENT_CANDIDATE

    arrays = (
        period_signal,
        support_count,
        state_code,
        first_signal,
        maximum_magnitude,
        maximum_signal_streak,
        maximum_support,
        valid_period_count,
        evidence_score,
        quality_flags,
    )
    for array in arrays:
        array.setflags(write=False)
    return RobustSeasonalSignalResult(
        period_signal_mask=period_signal,
        period_multi_index_support_count=support_count,
        state_code=state_code,
        first_signal_period_index=first_signal,
        maximum_disturbance_magnitude=maximum_magnitude,
        maximum_consecutive_signal_periods=maximum_signal_streak,
        maximum_multi_index_support_count=maximum_support,
        valid_post_cutoff_observation_count=valid_period_count,
        uncalibrated_disturbance_evidence_score=evidence_score,
        quality_flags_bitmask=quality_flags,
    )


def converge_disturbance_detectors(
    *,
    robust_result: RobustSeasonalSignalResult,
    ccdc_result: CcdcBenchmarkResult,
    post_period_start_dates: tuple[date, ...],
    post_period_end_dates_exclusive: tuple[date, ...],
    config: DisturbanceDetectionConfig,
) -> DetectorConvergenceResult:
    """Converge resultados 14.3/14.4 sin recalcularlos ni fusionar escalas."""
    _validate_detector_convergence_inputs(
        robust_result=robust_result,
        ccdc_result=ccdc_result,
        post_start_dates=post_period_start_dates,
        post_end_dates_exclusive=post_period_end_dates_exclusive,
        config=config,
    )

    robust_state = np.asarray(robust_result.state_code)
    robust_available = robust_state != DisturbanceStateCode.NOT_EVALUABLE
    ccdc_available = np.asarray(ccdc_result.fit_succeeded)
    robust_supports = robust_available & np.isin(
        robust_state,
        (
            DisturbanceStateCode.TRANSIENT_SIGNAL,
            DisturbanceStateCode.PERSISTENT_CANDIDATE,
        ),
    )
    ccdc_supports = ccdc_available & np.asarray(ccdc_result.has_post_cutoff_break)
    available_count = robust_available.astype(np.uint8) + ccdc_available.astype(np.uint8)

    state = np.full(
        robust_state.shape,
        DisturbanceStateCode.NOT_EVALUABLE,
        dtype=np.uint8,
    )
    reason = np.full(
        robust_state.shape,
        DetectorConvergenceReasonCode.NO_DETECTOR_AVAILABLE,
        dtype=np.uint8,
    )
    agreeing_count = np.zeros(robust_state.shape, dtype=np.uint8)

    robust_only = robust_available & ~ccdc_available
    _assign_robust_only_convergence(
        state=state,
        reason=reason,
        agreeing_count=agreeing_count,
        robust_state=robust_state,
        mask=robust_only,
    )

    ccdc_only = ~robust_available & ccdc_available
    ccdc_only_stable = ccdc_only & ~ccdc_supports
    state[ccdc_only_stable] = DisturbanceStateCode.STABLE_OBSERVED
    reason[ccdc_only_stable] = DetectorConvergenceReasonCode.CCDC_ONLY_STABLE
    agreeing_count[ccdc_only_stable] = 1
    ccdc_only_break = ccdc_only & ccdc_supports
    state[ccdc_only_break] = DisturbanceStateCode.TRANSIENT_SIGNAL
    reason[ccdc_only_break] = DetectorConvergenceReasonCode.CCDC_ONLY_BREAK_REQUIRES_REVIEW
    agreeing_count[ccdc_only_break] = 1

    both_available = robust_available & ccdc_available
    robust_stable = robust_state == DisturbanceStateCode.STABLE_OBSERVED
    robust_transient = robust_state == DisturbanceStateCode.TRANSIENT_SIGNAL
    robust_persistent = robust_state == DisturbanceStateCode.PERSISTENT_CANDIDATE

    both_stable = both_available & robust_stable & ~ccdc_supports
    state[both_stable] = DisturbanceStateCode.STABLE_OBSERVED
    reason[both_stable] = DetectorConvergenceReasonCode.BOTH_STABLE
    agreeing_count[both_stable] = 2

    transient_without_break = both_available & robust_transient & ~ccdc_supports
    state[transient_without_break] = DisturbanceStateCode.TRANSIENT_SIGNAL
    reason[transient_without_break] = DetectorConvergenceReasonCode.ROBUST_TRANSIENT_CCDC_NO_BREAK
    agreeing_count[transient_without_break] = 1

    polarity_disagreement = both_available & (
        (robust_stable & ccdc_supports) | (robust_persistent & ~ccdc_supports)
    )
    state[polarity_disagreement] = DisturbanceStateCode.DETECTOR_DISAGREEMENT
    reason[polarity_disagreement] = DetectorConvergenceReasonCode.DETECTOR_POLARITY_DISAGREEMENT
    agreeing_count[polarity_disagreement] = 1

    temporal_compatibility = _ccdc_break_is_temporally_compatible(
        period_signal_mask=np.asarray(robust_result.period_signal_mask),
        robust_state=robust_state,
        ccdc_break_fractional_year=np.asarray(ccdc_result.first_post_cutoff_break_fractional_year),
        post_start_dates=post_period_start_dates,
        post_end_dates_exclusive=post_period_end_dates_exclusive,
        minimum_consecutive_signal_periods=(
            config.robust_seasonal.minimum_consecutive_signal_periods
        ),
    )
    both_positive = both_available & robust_supports & ccdc_supports
    compatible_transient = both_positive & robust_transient & temporal_compatibility
    state[compatible_transient] = DisturbanceStateCode.TRANSIENT_SIGNAL
    reason[compatible_transient] = DetectorConvergenceReasonCode.COMPATIBLE_TRANSIENT_SIGNALS
    agreeing_count[compatible_transient] = 2
    compatible_persistent = both_positive & robust_persistent & temporal_compatibility
    state[compatible_persistent] = DisturbanceStateCode.PERSISTENT_CANDIDATE
    reason[compatible_persistent] = DetectorConvergenceReasonCode.COMPATIBLE_PERSISTENT_SIGNALS
    agreeing_count[compatible_persistent] = 2
    temporally_incompatible = both_positive & ~temporal_compatibility
    state[temporally_incompatible] = DisturbanceStateCode.DETECTOR_DISAGREEMENT
    reason[temporally_incompatible] = (
        DetectorConvergenceReasonCode.POSITIVE_SIGNALS_TEMPORALLY_INCOMPATIBLE
    )
    agreeing_count[temporally_incompatible] = 1

    quality_flags = np.bitwise_or(
        np.asarray(robust_result.quality_flags_bitmask, dtype=np.uint16),
        np.asarray(ccdc_result.quality_flags_bitmask, dtype=np.uint16),
    )
    source_arrays = (
        state,
        reason,
        available_count,
        agreeing_count,
        robust_supports,
        ccdc_supports,
        robust_result.first_signal_period_index,
        ccdc_result.first_post_cutoff_break_fractional_year,
        robust_result.maximum_disturbance_magnitude,
        robust_result.uncalibrated_disturbance_evidence_score,
        ccdc_result.ccdc_breakpoint_pseudo_probability,
        ccdc_result.decrease_positive_magnitude,
        quality_flags,
    )
    immutable_arrays: list[NDArray[Any]] = []
    for source in source_arrays:
        owned = np.array(source, copy=True)
        owned.setflags(write=False)
        immutable_arrays.append(owned)
    return DetectorConvergenceResult(
        state_code=immutable_arrays[0],
        reason_code=immutable_arrays[1],
        available_detector_count=immutable_arrays[2],
        agreeing_detector_count=immutable_arrays[3],
        robust_supports_signal=immutable_arrays[4],
        ccdc_supports_break=immutable_arrays[5],
        first_robust_signal_period_index=immutable_arrays[6],
        first_ccdc_break_fractional_year=immutable_arrays[7],
        robust_maximum_disturbance_magnitude=immutable_arrays[8],
        robust_uncalibrated_disturbance_evidence_score=immutable_arrays[9],
        ccdc_breakpoint_pseudo_probability=immutable_arrays[10],
        ccdc_decrease_positive_magnitude=immutable_arrays[11],
        quality_flags_bitmask=immutable_arrays[12],
    )


def _assign_robust_only_convergence(
    *,
    state: NDArray[np.uint8],
    reason: NDArray[np.uint8],
    agreeing_count: NDArray[np.uint8],
    robust_state: NDArray[np.uint8],
    mask: NDArray[np.bool_],
) -> None:
    """Preserva el resultado robusto sin simular acuerdo con CCDC ausente."""
    for robust_code, reason_code in (
        (
            DisturbanceStateCode.STABLE_OBSERVED,
            DetectorConvergenceReasonCode.ROBUST_ONLY_STABLE,
        ),
        (
            DisturbanceStateCode.TRANSIENT_SIGNAL,
            DetectorConvergenceReasonCode.ROBUST_ONLY_TRANSIENT,
        ),
        (
            DisturbanceStateCode.PERSISTENT_CANDIDATE,
            DetectorConvergenceReasonCode.ROBUST_ONLY_PERSISTENT,
        ),
    ):
        selected = mask & (robust_state == robust_code)
        state[selected] = robust_code
        reason[selected] = reason_code
        agreeing_count[selected] = 1


def _ccdc_break_is_temporally_compatible(
    *,
    period_signal_mask: NDArray[np.bool_],
    robust_state: NDArray[np.uint8],
    ccdc_break_fractional_year: NDArray[np.float64],
    post_start_dates: tuple[date, ...],
    post_end_dates_exclusive: tuple[date, ...],
    minimum_consecutive_signal_periods: int,
) -> NDArray[np.bool_]:
    """Compara tBreak con intervalos robustos reales bajo política [inicio, fin)."""
    persistent_periods = np.zeros_like(period_signal_mask, dtype=np.bool_)
    window_count = period_signal_mask.shape[0] - minimum_consecutive_signal_periods + 1
    for window_start in range(max(window_count, 0)):
        window_end = window_start + minimum_consecutive_signal_periods
        qualifying = period_signal_mask[window_start:window_end].all(axis=0)
        persistent_periods[window_start:window_end] |= qualifying

    robust_persistent = robust_state == DisturbanceStateCode.PERSISTENT_CANDIDATE
    compatible_signal_periods = np.where(
        robust_persistent[np.newaxis, :, :],
        persistent_periods,
        period_signal_mask,
    )
    compatible = np.zeros(robust_state.shape, dtype=np.bool_)
    finite_break = np.isfinite(ccdc_break_fractional_year)
    for period_index, (start, end) in enumerate(
        zip(post_start_dates, post_end_dates_exclusive, strict=True)
    ):
        start_fractional_year = _date_as_fractional_year(start)
        end_fractional_year = _date_as_fractional_year(end)
        break_inside_period = (
            finite_break
            & (ccdc_break_fractional_year >= start_fractional_year)
            & (ccdc_break_fractional_year < end_fractional_year)
        )
        compatible |= compatible_signal_periods[period_index] & break_inside_period
    return compatible


def _date_as_fractional_year(value: date) -> float:
    """Representa un límite calendario en la misma escala continua de CCDC."""
    year_start = date(value.year, 1, 1)
    next_year_start = date(value.year + 1, 1, 1)
    return value.year + (value - year_start).days / (next_year_start - year_start).days


def _validate_detector_convergence_inputs(
    *,
    robust_result: RobustSeasonalSignalResult,
    ccdc_result: CcdcBenchmarkResult,
    post_start_dates: tuple[date, ...],
    post_end_dates_exclusive: tuple[date, ...],
    config: DisturbanceDetectionConfig,
) -> None:
    robust_state = np.asarray(robust_result.state_code)
    if robust_state.dtype != np.uint8:
        raise ValueError("robust_result.state_code debe usar uint8")
    if robust_state.ndim != 2:
        raise ValueError("robust_result.state_code debe definir la forma espacial y, x")
    spatial_shape = robust_state.shape
    allowed_robust_states = {
        int(DisturbanceStateCode.NOT_EVALUABLE),
        int(DisturbanceStateCode.STABLE_OBSERVED),
        int(DisturbanceStateCode.TRANSIENT_SIGNAL),
        int(DisturbanceStateCode.PERSISTENT_CANDIDATE),
    }
    if not set(np.unique(robust_state)).issubset(allowed_robust_states):
        raise ValueError("robust_result contiene un estado no emitido por 14.3")

    period_signal = np.asarray(robust_result.period_signal_mask)
    if period_signal.dtype != np.bool_ or period_signal.ndim != 3:
        raise ValueError("robust_result.period_signal_mask debe ser booleano período, y, x")
    if period_signal.shape[1:] != spatial_shape:
        raise ValueError("robust_result.period_signal_mask no coincide con la forma espacial")
    period_count = period_signal.shape[0]
    if not (len(post_start_dates) == len(post_end_dates_exclusive) == period_count):
        raise ValueError("los límites temporales deben identificar cada período robusto")
    for period_index, (start, end) in enumerate(
        zip(post_start_dates, post_end_dates_exclusive, strict=True)
    ):
        if start >= end:
            raise ValueError("cada período robusto debe ser un intervalo no vacío")
        if period_index and start < post_end_dates_exclusive[period_index - 1]:
            raise ValueError("los períodos robustos deben estar ordenados y no superpuestos")

    robust_fields = (
        ("period_multi_index_support_count", np.uint8, (period_count, *spatial_shape)),
        ("first_signal_period_index", np.int32, spatial_shape),
        ("maximum_disturbance_magnitude", np.float64, spatial_shape),
        ("maximum_consecutive_signal_periods", np.uint16, spatial_shape),
        ("maximum_multi_index_support_count", np.uint8, spatial_shape),
        ("valid_post_cutoff_observation_count", np.uint16, spatial_shape),
        ("uncalibrated_disturbance_evidence_score", np.float64, spatial_shape),
        ("quality_flags_bitmask", np.uint16, spatial_shape),
    )
    for field_name, expected_dtype, expected_shape in robust_fields:
        array = np.asarray(getattr(robust_result, field_name))
        if array.shape != expected_shape:
            raise ValueError(f"robust_result.{field_name} no coincide con la forma espacial")
        if array.dtype != expected_dtype:
            raise ValueError(f"robust_result.{field_name} usa un dtype inválido")

    ccdc_spatial_fields = (
        ("fit_eligible", np.bool_),
        ("fit_succeeded", np.bool_),
        ("has_post_cutoff_break", np.bool_),
        ("first_post_cutoff_break_segment_index", np.int32),
        ("first_post_cutoff_break_fractional_year", np.float64),
        ("ccdc_breakpoint_pseudo_probability", np.float64),
        ("first_break_segment_observation_count", np.uint16),
        ("total_valid_observation_count", np.uint16),
        ("quality_flags_bitmask", np.uint16),
    )
    for field_name, ccdc_expected_dtype in ccdc_spatial_fields:
        array = np.asarray(getattr(ccdc_result, field_name))
        if array.shape != spatial_shape:
            raise ValueError(f"ccdc_result.{field_name} no coincide con la forma espacial")
        if array.dtype != ccdc_expected_dtype:
            raise ValueError(f"ccdc_result.{field_name} usa un dtype inválido")
    expected_magnitude_shape = (len(config.ccdc_benchmark.breakpoint_bands), *spatial_shape)
    for field_name in ("raw_signed_magnitude", "decrease_positive_magnitude"):
        array = np.asarray(getattr(ccdc_result, field_name))
        if array.shape != expected_magnitude_shape:
            raise ValueError(f"ccdc_result.{field_name} no coincide con la forma espacial")
        if array.dtype != np.float64:
            raise ValueError(f"ccdc_result.{field_name} debe usar float64")

    fit_eligible = np.asarray(ccdc_result.fit_eligible)
    fit_succeeded = np.asarray(ccdc_result.fit_succeeded)
    has_break = np.asarray(ccdc_result.has_post_cutoff_break)
    if np.any(fit_succeeded & ~fit_eligible):
        raise ValueError("CCDC no puede informar ajuste exitoso fuera del dominio elegible")
    if np.any(has_break & ~fit_succeeded):
        raise ValueError("CCDC no puede informar ruptura sin ajuste exitoso")
    break_time = np.asarray(ccdc_result.first_post_cutoff_break_fractional_year)
    if np.any(has_break & ~np.isfinite(break_time)):
        raise ValueError("una ruptura CCDC debe conservar tBreak finito")
    if np.any(~has_break & np.isfinite(break_time)):
        raise ValueError("CCDC sin ruptura no debe inventar tBreak")


def _validate_robust_signal_inputs(
    *,
    diagnostics: RobustSeasonalDiagnostics,
    magnitudes: NDArray[Any],
    post_start_dates: tuple[date, ...],
    post_end_dates_exclusive: tuple[date, ...],
    index_names: tuple[str, ...],
    domain: ForestEvaluationDomain,
    config: DisturbanceDetectionConfig,
) -> None:
    if magnitudes.ndim != 4:
        raise ValueError("diagnostics.standardized_magnitude debe tener período, índice, y, x")
    if magnitudes.shape[0] == 0:
        raise ValueError("se requiere al menos un período poscorte")
    if not np.issubdtype(magnitudes.dtype, np.floating):
        raise ValueError("standardized_magnitude debe usar valores de punto flotante")
    if np.isinf(magnitudes).any():
        raise ValueError("standardized_magnitude no admite infinitos")
    for field_name in (
        "reference_center",
        "robust_scale",
        "raw_directional_delta",
        "reference_valid_count",
        "quality_flags_bitmask",
    ):
        if np.asarray(getattr(diagnostics, field_name)).shape != magnitudes.shape:
            raise ValueError(f"diagnostics.{field_name} debe compartir la forma diagnóstica")

    period_count, index_count, *spatial_shape = magnitudes.shape
    if not (len(post_start_dates) == len(post_end_dates_exclusive) == period_count):
        raise ValueError("los metadatos temporales deben identificar cada período poscorte")
    if any(
        start >= end
        for start, end in zip(
            post_start_dates,
            post_end_dates_exclusive,
            strict=True,
        )
    ):
        raise ValueError("cada período poscorte debe ser un intervalo no vacío")
    for period_index in range(1, period_count):
        if post_start_dates[period_index] < post_end_dates_exclusive[period_index - 1]:
            raise ValueError("los períodos poscorte deben estar ordenados y no superpuestos")
        if post_start_dates[period_index] > post_end_dates_exclusive[period_index - 1]:
            raise ValueError(
                "los períodos poscorte deben ser continuos; los faltantes deben "
                "representarse con NaN"
            )
    if any(end <= config.analysis_start_date for end in post_end_dates_exclusive):
        raise ValueError("cada período debe terminar después del inicio poscorte")

    if len(index_names) != index_count or len(index_names) != len(set(index_names)):
        raise ValueError("index_names debe identificar cada índice sin duplicados")
    allowed_indices = {index.value for index in config.detection_indices}
    if not set(index_names).issubset(allowed_indices):
        raise ValueError("index_names debe ser un subconjunto de detection_indices")
    if index_count < config.robust_seasonal.minimum_index_support_count:
        raise ValueError("faltan índices para aplicar minimum_index_support_count")

    expected_spatial_shape = tuple(spatial_shape)
    for field_name in ("evaluable", "quality_flags_bitmask"):
        if np.asarray(getattr(domain, field_name)).shape != expected_spatial_shape:
            raise ValueError(f"domain.{field_name} debe coincidir con la forma espacial")


def _validate_robust_seasonal_inputs(
    *,
    reference: NDArray[Any],
    reference_seasons: tuple[str, ...],
    reference_start_dates: tuple[date, ...],
    reference_end_dates_exclusive: tuple[date, ...],
    post: NDArray[Any],
    post_seasons: tuple[str, ...],
    post_start_dates: tuple[date, ...],
    post_end_dates_exclusive: tuple[date, ...],
    index_names: tuple[str, ...],
    domain: ForestEvaluationDomain,
    config: DisturbanceDetectionConfig,
) -> None:
    for name, values in (("reference_values", reference), ("post_cutoff_values", post)):
        if values.ndim != 4:
            raise ValueError(f"{name} debe tener cuatro dimensiones: período, índice, y, x")
        if not np.issubdtype(values.dtype, np.number) or np.issubdtype(
            values.dtype, np.complexfloating
        ):
            raise ValueError(f"{name} debe contener valores numéricos reales")
    if reference.shape[1] != post.shape[1]:
        raise ValueError("referencia y poscorte deben contener los mismos índices")
    if reference.shape[2:] != post.shape[2:]:
        raise ValueError("referencia y poscorte deben compartir forma espacial")
    reference_period_count = reference.shape[0]
    if not (
        len(reference_seasons)
        == len(reference_start_dates)
        == len(reference_end_dates_exclusive)
        == reference_period_count
    ):
        raise ValueError("metadatos temporales de referencia no coinciden con sus períodos")
    post_period_count = post.shape[0]
    if not (
        len(post_seasons)
        == len(post_start_dates)
        == len(post_end_dates_exclusive)
        == post_period_count
    ):
        raise ValueError("metadatos temporales poscorte no coinciden con sus períodos")
    allowed_seasons = {season.value for season in SeasonalPeriod}
    if not set(reference_seasons).issubset(allowed_seasons) or not set(post_seasons).issubset(
        allowed_seasons
    ):
        raise ValueError("cada estación debe ser DJF, MAM, JJA o SON")
    all_intervals = (
        ("referencia", reference_start_dates, reference_end_dates_exclusive),
        ("poscorte", post_start_dates, post_end_dates_exclusive),
    )
    for label, start_dates, end_dates_exclusive in all_intervals:
        if any(start >= end for start, end in zip(start_dates, end_dates_exclusive, strict=True)):
            raise ValueError(f"cada intervalo de {label} debe ser un intervalo no vacío")
    if any(start < config.reference_history_start_date for start in reference_start_dates):
        raise ValueError("la referencia no puede comenzar antes de reference_history_start_date")
    if any(end > config.analysis_start_date for end in reference_end_dates_exclusive):
        raise ValueError("cada período de referencia debe ser completamente precorte")
    if any(end <= config.analysis_start_date for end in post_end_dates_exclusive):
        raise ValueError("los períodos poscorte deben terminar posteriores a la fecha de corte")
    expected_indices = tuple(index.value for index in config.detection_indices)
    if len(index_names) != post.shape[1] or len(index_names) != len(set(index_names)):
        raise ValueError("index_names debe identificar cada índice sin duplicados")
    if any(index not in expected_indices for index in index_names):
        raise ValueError("index_names debe ser un subconjunto de detection_indices")
    spatial_shape = post.shape[2:]
    for name, values in (
        ("domain.evaluable", domain.evaluable),
        ("domain.quality_flags_bitmask", domain.quality_flags_bitmask),
    ):
        if np.asarray(values).shape != spatial_shape:
            raise ValueError(f"{name} debe coincidir con la forma espacial")


def _validate_forest_domain_inputs(
    *,
    source_count: NDArray[Any],
    consensus_forest: NDArray[Any],
    disagreement: NDArray[Any],
    minimum_source_count: int,
) -> None:
    shapes = {source_count.shape, consensus_forest.shape, disagreement.shape}
    if len(shapes) != 1:
        raise ValueError("los insumos de línea base deben tener la misma forma")
    if source_count.ndim != 2:
        raise ValueError("los insumos de línea base deben ser rasters bidimensionales")
    if not np.issubdtype(source_count.dtype, np.integer) or np.any(source_count < 0):
        raise ValueError("source_count debe contener enteros no negativos")
    for name, values in (
        ("consensus_forest", consensus_forest),
        ("disagreement", disagreement),
    ):
        if not np.all(np.isin(values, (0, 1))):
            raise ValueError(f"{name} debe ser binaria")
    consensus_mask = consensus_forest.astype(np.bool_, copy=False)
    disagreement_mask = disagreement.astype(np.bool_, copy=False)
    if np.any(consensus_mask & disagreement_mask):
        raise ValueError("consensus_forest y disagreement deben ser mutuamente excluyentes")
    insufficient = source_count < minimum_source_count
    if np.any(insufficient & (consensus_mask | disagreement_mask)):
        raise ValueError("la evidencia forestal requiere fuentes suficientes")


def _validate_ccdc_segment_inputs(
    *,
    break_times: NDArray[Any],
    pseudo_probability: NDArray[Any],
    segment_observations: NDArray[Any],
    magnitudes: NDArray[Any],
    total_observations: NDArray[Any],
    supplied_fit: NDArray[Any],
    band_names: tuple[str, ...],
    domain: ForestEvaluationDomain,
    config: DisturbanceDetectionConfig,
) -> None:
    if break_times.ndim != 3:
        raise ValueError("break_time_fractional_year debe tener segmento, y, x")
    segment_count, *spatial_shape_values = break_times.shape
    spatial_shape = tuple(spatial_shape_values)
    if pseudo_probability.shape != break_times.shape:
        raise ValueError("change_pseudo_probability debe coincidir con tBreak")
    if segment_observations.shape != break_times.shape:
        raise ValueError("segment_observation_count debe coincidir con tBreak")
    if magnitudes.shape != (segment_count, len(band_names), *spatial_shape):
        raise ValueError("signed_magnitude debe tener segmento, banda, y, x")
    for name, array in (
        ("total_valid_observation_count", total_observations),
        ("fit_succeeded", supplied_fit),
        ("domain.evaluable", np.asarray(domain.evaluable)),
        ("domain.quality_flags_bitmask", np.asarray(domain.quality_flags_bitmask)),
    ):
        if array.shape != spatial_shape:
            raise ValueError(f"{name} debe coincidir con la forma espacial")

    for name, array in (
        ("break_time_fractional_year", break_times),
        ("change_pseudo_probability", pseudo_probability),
        ("signed_magnitude", magnitudes),
    ):
        if not np.issubdtype(array.dtype, np.floating):
            raise ValueError(f"{name} debe usar punto flotante")
        if np.isinf(array).any():
            raise ValueError(f"{name} no admite infinitos")
    finite_probability = pseudo_probability[np.isfinite(pseudo_probability)]
    if np.any((finite_probability < 0) | (finite_probability > 1)):
        raise ValueError("change_pseudo_probability debe estar entre 0 y 1")
    for name, array in (
        ("segment_observation_count", segment_observations),
        ("total_valid_observation_count", total_observations),
    ):
        if not np.issubdtype(array.dtype, np.integer) or np.any(array < 0):
            raise ValueError(f"{name} debe contener enteros no negativos")
        if np.any(array > np.iinfo(np.uint16).max):
            raise ValueError(f"{name} debe caber en uint16")
    if not np.issubdtype(supplied_fit.dtype, np.bool_):
        raise ValueError("fit_succeeded debe ser booleano")

    expected_bands = tuple(index.value for index in config.ccdc_benchmark.breakpoint_bands)
    if band_names != expected_bands:
        raise ValueError("band_names debe coincidir con ccdc breakpoint_bands")


def disturbance_detection_output_paths() -> dict[str, str]:
    """Reserva cinco artefactos compactos sin materializarlos."""
    return {
        "metadata": evidence_json("disturbance_detection.json"),
        "summary_raster": evidence_tiff("disturbance_summary.tif"),
        "diagnostics_raster": evidence_tiff("disturbance_diagnostics.tif"),
        "qa_figure": evidence_figure("disturbance_detection.png"),
        "period_summary_table": evidence_table("disturbance_period_summary.csv"),
    }


def disturbance_detection_json_schema() -> dict[str, Any]:
    """Devuelve el JSON Schema canónico del contrato metodológico vigente."""
    schema = DisturbanceDetectionConfig.model_json_schema(mode="serialization")
    schema["$id"] = DISTURBANCE_DETECTION_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = DISTURBANCE_DETECTION_SCHEMA_VERSION
    return schema
