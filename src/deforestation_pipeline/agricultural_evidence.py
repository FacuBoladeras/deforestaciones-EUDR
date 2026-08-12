"""Contrato y política auditable para evidencia agrícola posterior al cambio."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from uuid import UUID

import yaml
from pydantic import Field, field_validator, model_validator
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import shape

from deforestation_pipeline.schemas import NonEmptyString, Sha256Digest, StrictModel

AGRICULTURAL_EVIDENCE_SCHEMA_VERSION: Final = "1.0.0"
AGRICULTURAL_EVIDENCE_SCHEMA_ID: Final = "urn:deforestation-pipeline:agricultural-evidence:1.0.0"
AGRICULTURAL_EVIDENCE_POLICY_SCHEMA_VERSION: Final = "1.0.0"
AGRICULTURAL_EVIDENCE_POLICY_SCHEMA_ID: Final = (
    "urn:deforestation-pipeline:agricultural-evidence-policy:1.0.0"
)

AgriculturalLandUse = Literal["crop", "pasture", "livestock_infrastructure", "other", "unknown"]
IndependenceLevel = Literal["ineligible", "unknown", "methodological", "full"]
EvidenceRole = Literal[
    "candidate_independent",
    "corroborative_non_independent",
    "declared_context",
    "human_reference",
]
Fraction = Annotated[float, Field(ge=0, le=1, strict=True)]
NonNegativeArea = Annotated[float, Field(ge=0, strict=True)]


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Calcula el hash canónico de un descriptor o política JSON-compatible."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class AgriculturalEvidenceSource(StrictModel):
    """Identidad y licencia del dataset efectivamente observado."""

    dataset_id: NonEmptyString
    provider: NonEmptyString
    collection: NonEmptyString
    version: NonEmptyString
    asset: NonEmptyString
    license: NonEmptyString
    access_date: date
    attribution: NonEmptyString
    restrictions: list[NonEmptyString]


class AgriculturalEvidenceGeometry(StrictModel):
    """Huella atribuida, conservada separadamente de la geometría del evento."""

    type: Literal["Polygon", "MultiPolygon"]
    coordinates: Annotated[list[Any], Field(min_length=1)]
    crs: Annotated[str, Field(pattern=r"^EPSG:\d+$")]

    @field_validator("crs")
    @classmethod
    def crs_is_known(cls, value: str) -> str:
        try:
            CRS.from_user_input(value)
        except CRSError as exc:
            raise ValueError("geometry.crs debe ser un CRS conocido") from exc
        return value

    @model_validator(mode="after")
    def geometry_is_valid_and_nonempty(self) -> AgriculturalEvidenceGeometry:
        geometry = shape({"type": self.type, "coordinates": self.coordinates})
        if geometry.is_empty or not geometry.is_valid or geometry.area <= 0:
            raise ValueError("geometry debe ser poligonal, válida y no vacía")
        return self


class AgriculturalSpatialSupport(StrictModel):
    """Cobertura espacial efectiva medida dentro del evento."""

    source_resolution_m: Annotated[float, Field(gt=0, strict=True)]
    event_area_ha: Annotated[float, Field(gt=0, strict=True)]
    valid_area_ha: NonNegativeArea
    attributed_area_ha: NonNegativeArea
    event_coverage_fraction: Fraction
    attributed_event_fraction: Fraction
    resampling_method: NonEmptyString = "none_native_grid"

    @model_validator(mode="after")
    def areas_and_fractions_are_consistent(self) -> AgriculturalSpatialSupport:
        if self.valid_area_ha > self.event_area_ha or self.attributed_area_ha > self.valid_area_ha:
            raise ValueError("las áreas atribuidas deben ser subconjuntos del evento observado")
        tolerance = max(1e-6, 0.005 / self.event_area_ha)
        if abs(self.event_coverage_fraction - self.valid_area_ha / self.event_area_ha) > tolerance:
            raise ValueError("event_coverage_fraction no coincide con las áreas")
        attributed_fraction_error = abs(
            self.attributed_event_fraction - self.attributed_area_ha / self.event_area_ha
        )
        if attributed_fraction_error > tolerance:
            raise ValueError("attributed_event_fraction no coincide con las áreas")
        return self


class AgriculturalTemporalSupport(StrictModel):
    """Ventana y persistencia posteriores al inicio estimado del evento."""

    estimated_onset_window_end: date
    post_change_window_start: date
    post_change_window_end: date
    effective_observation_date: date
    valid_observation_count: Annotated[int, Field(ge=1, strict=True)]
    qualifying_observation_count: Annotated[int, Field(ge=1, strict=True)]
    minimum_consecutive_qualifying_periods: Annotated[int, Field(ge=2, strict=True)]
    observed_consecutive_qualifying_periods: Annotated[int, Field(ge=0, strict=True)]
    minimum_qualifying_periods: Annotated[int, Field(ge=2, strict=True)] | None = None
    observed_qualifying_periods: Annotated[int, Field(ge=0, strict=True)] | None = None
    minimum_observation_duration_days: Annotated[int, Field(ge=1, strict=True)] | None = None
    observed_duration_days: Annotated[int, Field(ge=0, strict=True)] | None = None
    persistence_basis: Literal["consecutive_periods", "seasonal_nonconsecutive_periods"] = (
        "consecutive_periods"
    )
    persistence_satisfied: bool
    long_gap_interpolation_used: Literal[False]
    persistence_rule: NonEmptyString

    @model_validator(mode="after")
    def temporal_support_is_consistent(self) -> AgriculturalTemporalSupport:
        if self.post_change_window_start <= self.estimated_onset_window_end:
            raise ValueError("la ventana post-cambio debe comenzar después del onset estimado")
        if self.post_change_window_end <= self.post_change_window_start:
            raise ValueError("la ventana post-cambio debe contener múltiples fechas")
        if not (
            self.post_change_window_start
            <= self.effective_observation_date
            <= self.post_change_window_end
        ):
            raise ValueError("effective_observation_date debe pertenecer a la ventana")
        if self.qualifying_observation_count > self.valid_observation_count:
            raise ValueError("qualifying_observation_count supera el soporte válido")
        if self.persistence_basis == "seasonal_nonconsecutive_periods":
            minimum_periods = self.minimum_qualifying_periods
            observed_periods = self.observed_qualifying_periods
            minimum_duration = self.minimum_observation_duration_days
            observed_duration = self.observed_duration_days
            values = (minimum_periods, observed_periods, minimum_duration, observed_duration)
            if any(value is None for value in values):
                raise ValueError("persistencia estacional requiere períodos y duración")
            assert minimum_periods is not None
            assert observed_periods is not None
            assert minimum_duration is not None
            assert observed_duration is not None
            computed = bool(
                observed_periods >= minimum_periods and observed_duration >= minimum_duration
            )
        else:
            computed = (
                self.observed_consecutive_qualifying_periods
                >= self.minimum_consecutive_qualifying_periods
            )
        if self.persistence_satisfied != computed:
            raise ValueError("persistence_satisfied no coincide con la persistencia observada")
        return self


class AgriculturalEvidenceQuality(StrictModel):
    """Calidad declarada sin convertir un score en probabilidad calibrada."""

    confidence: Fraction | None = None
    confidence_semantics: NonEmptyString
    quality_flags: list[NonEmptyString]
    discarded_observation_reasons: list[NonEmptyString]
    nodata_area_ha: NonNegativeArea
    uncertainty_notes: list[NonEmptyString]


class AgriculturalEvidenceObservation(StrictModel):
    """Una observación espacial y temporal atribuible a un evento."""

    evidence_id: NonEmptyString
    event_id: NonEmptyString
    land_use: AgriculturalLandUse
    source: AgriculturalEvidenceSource
    source_descriptor_sha256: Sha256Digest
    geometry: AgriculturalEvidenceGeometry
    spatial_support: AgriculturalSpatialSupport
    temporal_support: AgriculturalTemporalSupport
    quality: AgriculturalEvidenceQuality
    artifact_sha256: Sha256Digest

    @model_validator(mode="after")
    def source_hash_matches_descriptor(self) -> AgriculturalEvidenceObservation:
        expected = canonical_payload_sha256(self.source.model_dump(mode="json"))
        if self.source_descriptor_sha256 != expected:
            raise ValueError("source_descriptor_sha256 no coincide con source")
        return self


class AgriculturalEvidenceDocument(StrictModel):
    """Documento de intercambio de evidencia; nunca acepta independencia declarada."""

    schema_version: Literal["1.0.0"] = AGRICULTURAL_EVIDENCE_SCHEMA_VERSION
    evidence_set_id: UUID
    created_at: datetime
    observations: list[AgriculturalEvidenceObservation]

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at debe incluir zona horaria")
        return value

    @field_validator("observations")
    @classmethod
    def evidence_ids_are_unique(
        cls, value: list[AgriculturalEvidenceObservation]
    ) -> list[AgriculturalEvidenceObservation]:
        identifiers = [item.evidence_id for item in value]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("observations contiene evidence_id duplicados")
        return value


class ConsumerModelLineage(StrictModel):
    """Fuentes y familia del modelo respecto del cual se evalúa independencia."""

    model_id: NonEmptyString
    model_family: NonEmptyString
    training_dataset_ids: list[NonEmptyString]
    validation_dataset_ids: list[NonEmptyString]
    predictor_dataset_ids: list[NonEmptyString]
    sensor_families: list[NonEmptyString]


class AgriculturalEvidenceSourcePolicy(StrictModel):
    """Rol permitido y linaje conocido de un dataset de evidencia."""

    dataset_id: NonEmptyString
    version: NonEmptyString
    provider: NonEmptyString
    collection: NonEmptyString
    evidence_role: EvidenceRole
    upstream_dataset_ids: list[NonEmptyString]
    sensor_families: list[NonEmptyString]
    model_family: NonEmptyString | None = None


class AgriculturalEvidencePolicy(StrictModel):
    """Política versionada que deriva independencia; no recibe decisiones por evento."""

    schema_version: Literal["1.0.0"] = AGRICULTURAL_EVIDENCE_POLICY_SCHEMA_VERSION
    policy_id: NonEmptyString
    consumer_model: ConsumerModelLineage
    decision_eligible_levels: Annotated[
        list[Literal["methodological", "full"]], Field(min_length=1)
    ]
    sources: list[AgriculturalEvidenceSourcePolicy]

    @model_validator(mode="after")
    def source_keys_are_unique(self) -> AgriculturalEvidencePolicy:
        keys = [(item.dataset_id, item.version) for item in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("sources contiene dataset_id/version duplicados")
        if len(self.decision_eligible_levels) != len(set(self.decision_eligible_levels)):
            raise ValueError("decision_eligible_levels contiene duplicados")
        return self


class IndependenceAssessment(StrictModel):
    """Resultado reproducible de aplicar una política a una observación."""

    level: IndependenceLevel
    decision_eligible: bool
    reason_codes: Annotated[list[NonEmptyString], Field(min_length=1)]
    policy_id: NonEmptyString
    policy_sha256: Sha256Digest


class EvaluatedAgriculturalEvidence(StrictModel):
    """Observación original acompañada por la evaluación derivada."""

    observation: AgriculturalEvidenceObservation
    assessment: IndependenceAssessment


def load_agricultural_evidence_document(path: Path) -> AgriculturalEvidenceDocument:
    """Carga un JSON estricto de evidencia agrícola."""
    return AgriculturalEvidenceDocument.model_validate_json(path.read_text(encoding="utf-8"))


def load_agricultural_evidence_policy(path: Path) -> AgriculturalEvidencePolicy:
    """Carga una política YAML estricta."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("agricultural_evidence_policy_root_must_be_mapping")
    return AgriculturalEvidencePolicy.model_validate(payload)


def evaluate_agricultural_evidence(
    document: AgriculturalEvidenceDocument,
    policy: AgriculturalEvidencePolicy,
) -> dict[str, tuple[EvaluatedAgriculturalEvidence, ...]]:
    """Agrupa evidencia por evento después de derivar independencia desde linaje."""
    policy_hash = canonical_payload_sha256(policy.model_dump(mode="json"))
    profiles = {(item.dataset_id, item.version): item for item in policy.sources}
    grouped: defaultdict[str, list[EvaluatedAgriculturalEvidence]] = defaultdict(list)
    for observation in document.observations:
        assessment = _assess_observation(observation, policy, profiles, policy_hash)
        grouped[observation.event_id].append(
            EvaluatedAgriculturalEvidence(observation=observation, assessment=assessment)
        )
    return {event_id: tuple(items) for event_id, items in sorted(grouped.items())}


def _assess_observation(
    observation: AgriculturalEvidenceObservation,
    policy: AgriculturalEvidencePolicy,
    profiles: Mapping[tuple[str, str], AgriculturalEvidenceSourcePolicy],
    policy_hash: str,
) -> IndependenceAssessment:
    profile = profiles.get((observation.source.dataset_id, observation.source.version))
    reasons: list[str] = []
    level: IndependenceLevel
    if profile is None:
        level = "unknown"
        reasons.append("source_not_registered_in_policy")
    elif (
        profile.provider != observation.source.provider
        or profile.collection != observation.source.collection
    ):
        level = "unknown"
        reasons.append("source_descriptor_does_not_match_policy")
    else:
        consumer = policy.consumer_model
        protected_lineage = set((*consumer.training_dataset_ids, *consumer.validation_dataset_ids))
        source_lineage = {profile.dataset_id, *profile.upstream_dataset_ids}
        role_blocked = profile.evidence_role in {
            "corroborative_non_independent",
            "declared_context",
        }
        lineage_overlap = bool(protected_lineage & source_lineage)
        model_overlap = profile.model_family == consumer.model_family
        sensor_overlap = bool(set(profile.sensor_families) & set(consumer.sensor_families))
        predictor_overlap = bool(
            set(profile.upstream_dataset_ids) & set(consumer.predictor_dataset_ids)
        )
        if role_blocked:
            reasons.append(f"policy_role_{profile.evidence_role}")
        if lineage_overlap:
            reasons.append("consumer_training_or_validation_lineage_overlap")
        if model_overlap:
            reasons.append("consumer_model_family_overlap")
        if role_blocked or lineage_overlap or model_overlap:
            level = "ineligible"
        elif sensor_overlap or predictor_overlap:
            level = "methodological"
            if sensor_overlap:
                reasons.append("shared_sensor_family")
            if predictor_overlap:
                reasons.append("shared_predictor_dataset")
        else:
            level = "full"
            reasons.append("no_known_lineage_or_sensor_overlap")
    eligible = bool(
        profile is not None
        and profile.evidence_role in {"candidate_independent", "human_reference"}
        and level in policy.decision_eligible_levels
        and observation.land_use in {"crop", "pasture", "livestock_infrastructure"}
        and observation.temporal_support.persistence_satisfied
        and observation.spatial_support.attributed_area_ha > 0
    )
    if not eligible:
        reasons.append("not_decision_eligible")
    return IndependenceAssessment(
        level=level,
        decision_eligible=eligible,
        reason_codes=list(dict.fromkeys(reasons)),
        policy_id=policy.policy_id,
        policy_sha256=policy_hash,
    )


def agricultural_evidence_json_schema() -> dict[str, Any]:
    schema = AgriculturalEvidenceDocument.model_json_schema(mode="serialization")
    schema["$id"] = AGRICULTURAL_EVIDENCE_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = AGRICULTURAL_EVIDENCE_SCHEMA_VERSION
    return schema


def agricultural_evidence_policy_json_schema() -> dict[str, Any]:
    schema = AgriculturalEvidencePolicy.model_json_schema(mode="serialization")
    schema["$id"] = AGRICULTURAL_EVIDENCE_POLICY_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = AGRICULTURAL_EVIDENCE_POLICY_SCHEMA_VERSION
    return schema
