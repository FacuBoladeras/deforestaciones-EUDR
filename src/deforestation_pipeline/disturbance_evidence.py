"""Contrato versionado de la evidencia de perturbaciones publicada."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from math import isfinite
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from deforestation_pipeline.change_detection import (
    DISTURBANCE_DIAGNOSTIC_BAND_NAMES,
    DISTURBANCE_SUMMARY_BAND_NAMES,
    DetectorConvergenceReasonCode,
    DisturbanceQualityBit,
    DisturbanceStateCode,
)
from deforestation_pipeline.forest_screening import ForestScreeningMetrics
from deforestation_pipeline.schemas import NonEmptyString, Sha256Digest, StrictModel

DISTURBANCE_EVIDENCE_SCHEMA_VERSION: Literal["1.3.0"] = "1.3.0"
DISTURBANCE_EVIDENCE_SCHEMA_ID = "urn:deforestation-pipeline:disturbance-evidence:1.3.0"
NonNegativeCount = Annotated[int, Field(ge=0, strict=True)]


class DisturbanceRequestedRange(StrictModel):
    """Años solicitados por quien ejecuta el análisis."""

    start_year: Annotated[int, Field(ge=1900, strict=True)]
    end_year: Annotated[int, Field(ge=1900, strict=True)]

    @model_validator(mode="after")
    def range_is_ordered(self) -> DisturbanceRequestedRange:
        if self.end_year < self.start_year:
            raise ValueError("end_year no puede preceder a start_year")
        return self


class DisturbanceReferenceSupport(StrictModel):
    """Historia previa usada como referencia estacional."""

    configured_start_date: date
    actual_start_date: date
    actual_end_date_exclusive: date
    period_ids: list[NonEmptyString]
    internal_period_ids: list[NonEmptyString]
    minimum_observations_per_season: Annotated[int, Field(ge=1, strict=True)]

    @model_validator(mode="after")
    def dates_and_periods_are_consistent(self) -> DisturbanceReferenceSupport:
        if not self.actual_start_date < self.actual_end_date_exclusive:
            raise ValueError("reference_support debe definir un intervalo no vacío")
        if len(self.period_ids) != len(set(self.period_ids)):
            raise ValueError("reference_support.period_ids contiene duplicados")
        if not set(self.internal_period_ids).issubset(self.period_ids):
            raise ValueError("internal_period_ids debe ser subconjunto de period_ids")
        return self


class DisturbanceEvaluableRange(StrictModel):
    """Período posterior al corte efectivamente evaluado."""

    start_date: date
    end_date: date
    period_ids: list[NonEmptyString]
    excluded_cutoff_straddling_period_ids: list[NonEmptyString]

    @model_validator(mode="after")
    def dates_and_periods_are_consistent(self) -> DisturbanceEvaluableRange:
        if self.end_date < self.start_date:
            raise ValueError("evaluable_range.end_date no puede preceder a start_date")
        if len(self.period_ids) != len(set(self.period_ids)):
            raise ValueError("evaluable_range.period_ids contiene duplicados")
        return self


class DisturbanceEvidenceParameters(StrictModel):
    """Parámetros científicos mínimos necesarios para interpretar la evidencia."""

    indices: Annotated[list[NonEmptyString], Field(min_length=1)]
    robust_signal_threshold: Annotated[float, Field(gt=0, strict=True)]
    minimum_index_support_count: Annotated[int, Field(ge=2, strict=True)]
    minimum_consecutive_signal_periods: Annotated[int, Field(ge=2, strict=True)]
    baseline_disagreement_policy: NonEmptyString
    detector_disagreement_policy: NonEmptyString

    @field_validator("indices")
    @classmethod
    def indices_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("parameters.indices contiene duplicados")
        return value


class DisturbanceEvidenceSources(StrictModel):
    """Fuentes lógicas que respaldan la detección sin atribución."""

    robust: Literal["published_and_internal_hls_seasonal_composites"]
    ccdc: Literal["dense_masked_hlsl30_collection"]
    forest_domain: Literal["forest_baseline_2020"]


class DisturbanceCcdcEvidence(StrictModel):
    """Procedencia y disponibilidad del resumen escalar CCDC."""

    raw_array_downloaded: Literal[False]
    scalar_summary_downloaded: bool
    source_product: Literal["HLSL30"]
    change_metric_semantics: Literal["algorithmic_breakpoint_score_not_calibrated"]
    status: Literal["scalar_summary_materialized", "unavailable_global_failure"]
    failure_code: NonEmptyString | None = None

    @model_validator(mode="after")
    def status_is_consistent(self) -> DisturbanceCcdcEvidence:
        if self.status == "scalar_summary_materialized":
            if not self.scalar_summary_downloaded or self.failure_code is not None:
                raise ValueError("ccdc materializado requiere resumen escalar sin failure_code")
        elif self.status == "unavailable_global_failure":
            if self.scalar_summary_downloaded or self.failure_code is None:
                raise ValueError("ccdc indisponible requiere failure_code y sin resumen escalar")
        return self


class DisturbanceEvidenceStatistics(StrictModel):
    """Conteos dentro de la huella exacta del AOI."""

    state_code: dict[NonEmptyString, NonNegativeCount]
    reason_code: dict[NonEmptyString, NonNegativeCount]
    quality_flag_bit_counts: dict[NonEmptyString, NonNegativeCount]

    @model_validator(mode="after")
    def code_names_match_the_domain(self) -> DisturbanceEvidenceStatistics:
        expected_states = {member.name for member in DisturbanceStateCode}
        expected_reasons = {member.name for member in DetectorConvergenceReasonCode}
        allowed_quality_flags = {member.name for member in DisturbanceQualityBit}
        if set(self.state_code) != expected_states:
            raise ValueError("statistics.state_code no contiene el dominio completo")
        if set(self.reason_code) != expected_reasons:
            raise ValueError("statistics.reason_code no contiene el dominio completo")
        if not set(self.quality_flag_bit_counts).issubset(allowed_quality_flags):
            raise ValueError("statistics.quality_flag_bit_counts contiene códigos desconocidos")
        return self


class DisturbanceEvidenceRasters(StrictModel):
    """Identidad espacial y semántica común de los rasters publicados."""

    dtype: Literal["float32"]
    nodata: float
    grid_sha256: Sha256Digest
    summary_band_names: list[NonEmptyString]
    diagnostic_band_names: list[NonEmptyString]
    integer_semantics_preserved: Literal[True]
    aoi_footprint_applied: Literal[True]

    @field_validator("nodata")
    @classmethod
    def nodata_is_finite(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("rasters.nodata debe ser finito")
        return value

    @model_validator(mode="after")
    def band_names_match_the_published_contract(self) -> DisturbanceEvidenceRasters:
        if tuple(self.summary_band_names) != DISTURBANCE_SUMMARY_BAND_NAMES:
            raise ValueError("summary_band_names no coincide con el producto publicado")
        if tuple(self.diagnostic_band_names) != DISTURBANCE_DIAGNOSTIC_BAND_NAMES:
            raise ValueError("diagnostic_band_names no coincide con el producto publicado")
        return self


class TemporalSignalCounts(StrictModel):
    """Conteos conservados por estrato, sin convertir señales en deforestación."""

    any_robust_signal_pixel_count: NonNegativeCount
    ccdc_break_pixel_count: NonNegativeCount
    persistent_candidate_pixel_count: NonNegativeCount
    transient_signal_pixel_count: NonNegativeCount
    detector_disagreement_pixel_count: NonNegativeCount


class DisturbanceStatementPeriod(StrictModel):
    """Cobertura temporal exacta a la que puede referirse la frase automática."""

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def period_is_ordered(self) -> DisturbanceStatementPeriod:
        if self.end_date < self.start_date:
            raise ValueError("automatic_statement_period debe estar ordenado")
        return self


class DisturbanceScreeningEvidence(StrictModel):
    """Alcance del screening y señales dentro y fuera del dominio primario."""

    primary_interpretation_domain: Literal["automated_forest"]
    temporal_signal_scope: Literal["entire_aoi"]
    baseline_metrics: ForestScreeningMetrics
    temporal_signals_by_domain: dict[NonEmptyString, TemporalSignalCounts]
    automatic_statement_policy: Literal["only_when_no_signal_in_automated_forest"]
    automatic_statement_scope: Literal["automated_forest_high_confidence"]
    automatic_statement_period: DisturbanceStatementPeriod
    automatic_statement_generated: bool
    automatic_statement: NonEmptyString | None = None
    final_assessment_generated: Literal[False]

    @model_validator(mode="after")
    def signal_domains_are_complete(self) -> DisturbanceScreeningEvidence:
        expected = {
            "automated_forest",
            "automated_nonforest",
            "review_required",
            "insufficient_data",
        }
        if set(self.temporal_signals_by_domain) != expected:
            raise ValueError("temporal_signals_by_domain debe conservar los cuatro estratos")
        if self.automatic_statement_generated != (self.automatic_statement is not None):
            raise ValueError("automatic_statement debe coincidir con su bandera de generación")
        if self.automatic_statement is not None:
            expected_statement = (
                "sin cambio detectado en el bosque automático de alta confianza entre "
                f"{self.automatic_statement_period.start_date.isoformat()} y "
                f"{self.automatic_statement_period.end_date.isoformat()}"
            )
            if self.automatic_statement != expected_statement:
                raise ValueError("automatic_statement no coincide con scope y período")
        return self


class DisturbanceEvidenceAssets(StrictModel):
    """Activos compactos, incluido el estado robusto previo a convergencia."""

    metadata: NonEmptyString
    summary_raster: NonEmptyString
    diagnostics_raster: NonEmptyString
    robust_state_raster: NonEmptyString
    qa_figure: NonEmptyString
    period_summary_table: NonEmptyString


class DisturbanceEvidenceMetadata(StrictModel):
    """Contrato 1.0.0 del JSON de evidencia, todavía sin atribución."""

    schema_version: Literal["1.3.0"] = DISTURBANCE_EVIDENCE_SCHEMA_VERSION
    generated_at: datetime
    scientific_parameters_hash: Sha256Digest
    requested_range: DisturbanceRequestedRange
    reference_support: DisturbanceReferenceSupport
    evaluable_range: DisturbanceEvaluableRange
    parameters: DisturbanceEvidenceParameters
    sources: DisturbanceEvidenceSources
    ccdc: DisturbanceCcdcEvidence
    statistics: DisturbanceEvidenceStatistics
    screening: DisturbanceScreeningEvidence
    rasters: DisturbanceEvidenceRasters
    assets: DisturbanceEvidenceAssets
    final_assessment_generated: Literal[False]
    attribution_generated: Literal[False]
    limitations: Annotated[list[NonEmptyString], Field(min_length=1)]

    @field_validator("generated_at")
    @classmethod
    def generated_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        return value


def disturbance_evidence_json_schema() -> dict[str, Any]:
    """Devuelve el JSON Schema canónico del JSON de evidencia 1.0.0."""
    schema = DisturbanceEvidenceMetadata.model_json_schema(mode="serialization")
    schema["$id"] = DISTURBANCE_EVIDENCE_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = DISTURBANCE_EVIDENCE_SCHEMA_VERSION
    return schema


def validate_disturbance_evidence_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Valida y normaliza un payload antes de serializarlo como evidencia."""
    return DisturbanceEvidenceMetadata.model_validate(payload).model_dump(
        mode="json",
        exclude_none=True,
    )
