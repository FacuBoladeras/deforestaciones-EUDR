"""Contrato editorial tipado para el informe orientado a clientes."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

LEGAL_DISCLAIMER = "Este informe no constituye certificación ni confirmación legal EUDR."

NonEmptyString = Annotated[str, Field(min_length=1)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
Fraction = Annotated[float, Field(ge=0, le=1)]
Coordinate = tuple[float, float]
Ring = tuple[Coordinate, ...]
GeometryRings = tuple[Ring, ...]


class StrictViewModel(BaseModel):
    """Contrato editorial inmutable que nunca expone documentos científicos completos."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class AnalysisOverview(StrictViewModel):
    analysis_id: NonEmptyString
    establishment_id: NonEmptyString
    analysis_end_date: date
    overall_status: Literal["complete", "partial"]
    recorded_at: datetime


class HeadlineMetrics(StrictViewModel):
    establishment_area_ha: NonNegativeFloat | None
    forest_area_2020_ha: NonNegativeFloat | None
    spectral_candidate_area_ha: NonNegativeFloat | None
    spectral_candidate_count: Annotated[int, Field(ge=0)] | None
    candidate_episode_area_ha: NonNegativeFloat | None
    candidate_episode_count: Annotated[int, Field(ge=0)] | None
    likely_conversion_area_ha: NonNegativeFloat | None
    conversion_likely_count: Annotated[int, Field(ge=0)] | None
    automatic_final_assessment_generated: bool


class ComponentSummary(StrictViewModel):
    name: NonEmptyString
    status: NonEmptyString
    available: bool
    reason: str | None
    selected_figure_count: Annotated[int, Field(ge=0)]


class GeometrySummary(StrictViewModel):
    geometry_type: NonEmptyString
    normalized_crs: NonEmptyString
    repair_performed: bool
    calculation_crs: NonEmptyString
    area_method: NonEmptyString
    threshold_ha: NonNegativeFloat


class BaselineSummary(StrictViewModel):
    vector_establishment_area_ha: NonNegativeFloat
    raster_aoi_area_ha: NonNegativeFloat
    automated_evaluable_area_ha: NonNegativeFloat
    automated_forest_area_ha: NonNegativeFloat
    automated_nonforest_area_ha: NonNegativeFloat
    review_required_area_ha: NonNegativeFloat
    insufficient_data_area_ha: NonNegativeFloat


class QualitySummary(StrictViewModel):
    seasonal_period_count: Annotated[int, Field(ge=0)]
    minimum_valid_pixel_fraction: Fraction | None
    detection_period_count: Annotated[int, Field(ge=0)]
    evaluable_detection_period_count: Annotated[int, Field(ge=0)]
    minimum_mean_observation_count: NonNegativeFloat | None
    maximum_mean_observation_count: NonNegativeFloat | None


class DatasetSummary(StrictViewModel):
    dataset_id: NonEmptyString
    provider: NonEmptyString
    collection: NonEmptyString
    version: NonEmptyString
    access_date: date
    license: NonEmptyString
    spatial_resolution_m: NonNegativeFloat | None = None


class ClientFigure(StrictViewModel):
    kind: Literal[
        "annual_forest_change",
        "rgb_timeline",
        "spectral_index_timeline",
        "observation_coverage",
        "disturbance_detection",
        "post_change_attribution",
        "event_spectral_evidence",
    ]
    event_id: NonEmptyString | None = None
    report_path: NonEmptyString
    absolute_path: Path
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class EvidenceGate(StrictViewModel):
    key: Literal[
        "forest_at_cutoff",
        "post_cutoff_loss",
        "persistent_change",
        "agricultural_or_livestock_post_use",
        "defensible_area_and_geometry",
        "strong_alternative_explanation_absent",
        "visec_operational_area_strictly_greater_than_threshold",
    ]
    passed: bool


class AnnualForestPoint(StrictViewModel):
    year: Annotated[int, Field(ge=2020, le=2100)]
    forest_fraction: Fraction


class EventSummary(StrictViewModel):
    ordinal: Annotated[int, Field(gt=0)]
    candidate_id: NonEmptyString
    event_id: NonEmptyString | None
    record_type: Literal["disturbance_candidate", "conversion_likely_event"]
    interpretation_level: Literal["candidate_episode", "event"]
    interpretation_status: Literal[
        "candidate_only",
        "temporary_or_recovered",
        "persistent_unattributed",
        "insufficient_data",
        "subthreshold_conversion_evidence",
        "conversion_likely",
    ]
    selected_for_detail: bool
    automatic_status: NonEmptyString
    area_ha: NonNegativeFloat
    area_threshold_ha: NonNegativeFloat
    area_threshold_met: bool
    likely_conversion_area_ha: NonNegativeFloat
    conjunctive_conversion_evidence_area_ha: NonNegativeFloat
    estimated_onset_period_id: NonEmptyString
    estimated_onset_window_start: date | None
    estimated_onset_window_end: date | None
    post_change_use: NonEmptyString
    confidence: Fraction | None
    confidence_semantics: str | None
    human_review_required: bool
    observed_period_count: Annotated[int, Field(ge=0)] | None
    qualifying_period_count: Annotated[int, Field(ge=0)] | None
    gap_period_count: Annotated[int, Field(ge=0)] | None
    persistent_agricultural_area_ha: NonNegativeFloat
    persistent_event_fraction: Fraction | None
    sensitivity_min_area_ha: NonNegativeFloat | None
    sensitivity_max_area_ha: NonNegativeFloat | None
    dual_detector_pixel_count: Annotated[int, Field(ge=0)] | None
    pixel_count: Annotated[int, Field(gt=0)] | None
    gates: tuple[EvidenceGate, ...]
    forest_trajectory: tuple[AnnualForestPoint, ...]
    quality_flags: tuple[str, ...]
    event_geometry: GeometryRings
    candidate_geometry: GeometryRings
    agricultural_geometry: GeometryRings


class ReportViewModel(StrictViewModel):
    schema_version: Literal["2.8.0"] = "2.8.0"
    title: Literal["Informe técnico de evidencia geoespacial"] = (
        "Informe técnico de evidencia geoespacial"
    )
    analysis: AnalysisOverview
    verified_asset_count: Annotated[int, Field(gt=0)]
    result_status: NonEmptyString
    metrics: HeadlineMetrics
    geometry: GeometrySummary
    establishment_geometry: GeometryRings
    seasonal_period_ids: tuple[NonEmptyString, ...]
    baseline: BaselineSummary
    quality: QualitySummary
    datasets: tuple[DatasetSummary, ...]
    client_figures: tuple[ClientFigure, ...]
    components: tuple[ComponentSummary, ...]
    events: tuple[EventSummary, ...]
    limitations: tuple[NonEmptyString, ...]
    legal_disclaimer: Literal[
        "Este informe no constituye certificación ni confirmación legal EUDR."
    ] = "Este informe no constituye certificación ni confirmación legal EUDR."


class ReportArtifact(StrictViewModel):
    path: Path
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    size_bytes: Annotated[int, Field(gt=0)]
