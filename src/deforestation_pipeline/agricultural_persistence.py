"""Persistencia agrícola post-cambio derivada sin interpolar meses faltantes."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np
import rasterio
import yaml
from numpy.typing import NDArray
from pydantic import Field, field_validator, model_validator
from pyproj import Transformer
from rasterio.features import shapes
from rasterio.io import MemoryFile
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

from deforestation_pipeline.agricultural_collector import AgriculturalCollectionDocument
from deforestation_pipeline.agricultural_evidence import (
    AGRICULTURAL_EVIDENCE_SCHEMA_VERSION,
    AgriculturalEvidenceDocument,
    AgriculturalEvidenceDocumentV2,
    AgriculturalEvidenceObservation,
    AgriculturalEvidenceObservationV2,
    AgriculturalSignalStrength,
    canonical_payload_sha256,
)
from deforestation_pipeline.agricultural_visualization import (
    render_agricultural_persistence_evidence,
)
from deforestation_pipeline.local_runner import _publish_bundle
from deforestation_pipeline.schemas import NonEmptyString, Sha256Digest, StrictModel

LEGACY_AGRICULTURAL_PERSISTENCE_SCHEMA_VERSION: Final = "1.0.0"
AGRICULTURAL_PERSISTENCE_SCHEMA_VERSION: Final = "2.1.0"
AGRICULTURAL_PERSISTENCE_SCHEMA_ID: Final = (
    "urn:deforestation-pipeline:agricultural-persistence:2.1.0"
)
_RAW_DOCUMENT = "json/evidence/agricultural_evidence.json"
_RAW_METADATA = "json/evidence/agricultural_evidence_metadata.json"
_RAW_EVENTS = "json/evidence/agricultural_evidence.geojson"


class LegacyAgriculturalPersistenceConfig(StrictModel):
    """Regla temporal versionada que no exige meses ciegamente consecutivos."""

    schema_version: Literal["1.0.0"]
    source_id: Literal["dynamic_world_v1"]
    minimum_valid_periods: Annotated[int, Field(ge=2, le=72, strict=True)]
    minimum_qualifying_periods: Annotated[int, Field(ge=2, le=72, strict=True)]
    minimum_post_onset_duration_days: Annotated[int, Field(ge=30, le=2192, strict=True)]
    primary_crop_observation_fraction: Annotated[float, Field(gt=0, le=1, strict=True)]
    sensitivity_crop_observation_fractions: Annotated[
        list[Annotated[float, Field(gt=0, le=1, strict=True)]], Field(min_length=3)
    ]
    minimum_valid_observations_per_period: Annotated[int, Field(ge=1, le=31, strict=True)]
    maximum_unobserved_gap_periods: Annotated[int, Field(ge=0, le=24, strict=True)]
    seasonality_rule: Literal["seasonal_nonconsecutive_periods"]
    long_gap_interpolation_used: Literal[False]
    support_raster_nodata: float

    @field_validator("support_raster_nodata")
    @classmethod
    def nodata_is_fixed(cls, value: float) -> float:
        if value != -9999.0:
            raise ValueError("support_raster_nodata debe ser -9999")
        return value

    @model_validator(mode="after")
    def thresholds_are_coherent(self) -> LegacyAgriculturalPersistenceConfig:
        values = self.sensitivity_crop_observation_fractions
        if values != sorted(set(values)):
            raise ValueError("sensitivity thresholds deben ser únicos y ordenados")
        if self.primary_crop_observation_fraction not in values:
            raise ValueError("primary threshold debe estar en sensitivity thresholds")
        if self.minimum_qualifying_periods > self.minimum_valid_periods:
            raise ValueError("minimum qualifying periods supera minimum valid periods")
        return self


class SignalStrengthPeriods(StrictModel):
    weak: Literal[1]
    moderate: Literal[2]
    strong: Literal[3]


@dataclass(frozen=True, slots=True)
class _CandidateCoverageAssessment:
    detected: bool
    signal_strength: AgriculturalSignalStrength | None
    qualifying_period_count: int
    maximum_coverage_fraction: float
    latest_coverage_fraction: float
    first_qualifying_period_index: int | None


def _candidate_period_coverage_assessment(
    *,
    qualifying_areas_ha: list[float],
    candidate_area_ha: float,
    minimum_coverage_fraction: float,
) -> _CandidateCoverageAssessment:
    """Evalúa el gate porcentual contra el área total exacta del candidato."""
    if candidate_area_ha <= 0:
        raise ValueError("candidate_area_ha_must_be_positive")
    if not 0 <= minimum_coverage_fraction <= 1:
        raise ValueError("minimum_candidate_coverage_fraction_out_of_range")
    fractions = tuple(
        min(1.0, max(0.0, float(area)) / candidate_area_ha) for area in qualifying_areas_ha
    )
    qualifies = tuple(
        area > 0
        and (
            fraction > minimum_coverage_fraction
            or math.isclose(fraction, minimum_coverage_fraction, abs_tol=1e-12)
        )
        for area, fraction in zip(qualifying_areas_ha, fractions, strict=True)
    )
    qualifying_count = sum(qualifies)
    first_index = next((index for index, value in enumerate(qualifies) if value), None)
    return _CandidateCoverageAssessment(
        detected=qualifying_count > 0,
        signal_strength=_signal_strength(qualifying_count) if qualifying_count else None,
        qualifying_period_count=qualifying_count,
        maximum_coverage_fraction=max(fractions, default=0.0),
        latest_coverage_fraction=fractions[-1] if fractions else 0.0,
        first_qualifying_period_index=first_index,
    )


class AgriculturalPersistenceConfig(StrictModel):
    """Regla v2: una ocurrencia es evidencia; el seguimiento sólo califica calidad."""

    schema_version: Literal["2.0.0", "2.1.0"]
    source_id: Literal["dynamic_world_v1"]
    minimum_valid_periods: Annotated[int, Field(ge=2, le=72, strict=True)]
    minimum_qualifying_periods: Literal[1]
    minimum_post_onset_duration_days: Annotated[int, Field(ge=30, le=2192, strict=True)]
    primary_crop_observation_fraction: Annotated[float, Field(gt=0, le=1, strict=True)]
    minimum_candidate_agricultural_coverage_fraction: Annotated[
        float, Field(ge=0, le=1, strict=True)
    ] = 0.0
    sensitivity_crop_observation_fractions: Annotated[
        list[Annotated[float, Field(gt=0, le=1, strict=True)]], Field(min_length=3)
    ]
    minimum_valid_observations_per_period: Annotated[int, Field(ge=1, le=31, strict=True)]
    maximum_unobserved_gap_periods: Annotated[int, Field(ge=0, le=24, strict=True)]
    evidence_rule: Literal[
        "any_qualifying_post_event_period",
        "candidate_coverage_at_least_five_percent_post_cutoff",
    ]
    signal_strength_periods: SignalStrengthPeriods
    long_gap_interpolation_used: Literal[False]
    support_raster_nodata: float

    @field_validator("support_raster_nodata")
    @classmethod
    def nodata_is_fixed(cls, value: float) -> float:
        if value != -9999.0:
            raise ValueError("support_raster_nodata debe ser -9999")
        return value

    @model_validator(mode="after")
    def thresholds_are_coherent(self) -> AgriculturalPersistenceConfig:
        values = self.sensitivity_crop_observation_fractions
        if values != sorted(set(values)):
            raise ValueError("sensitivity thresholds deben ser únicos y ordenados")
        if self.primary_crop_observation_fraction not in values:
            raise ValueError("primary threshold debe estar en sensitivity thresholds")
        return self


class PersistenceSensitivity(StrictModel):
    crop_observation_fraction_threshold: Annotated[float, Field(gt=0, le=1)]
    persistent_crop_area_ha: Annotated[float, Field(ge=0)]
    persistent_event_fraction: Annotated[float, Field(ge=0, le=1)]


class PeriodSupport(StrictModel):
    window_id: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]
    start_date: date
    end_date_exclusive: date
    valid_area_ha: Annotated[float, Field(ge=0)]
    qualifying_crop_area_ha: Annotated[float, Field(ge=0)]
    candidate_agricultural_coverage_fraction: Annotated[float, Field(ge=0, le=1)] = 0.0
    candidate_coverage_gate_met: bool = False
    nodata_area_ha: Annotated[float, Field(ge=0)]
    matched_scene_count: Annotated[int, Field(ge=0, strict=True)]
    raster_path: NonEmptyString


class LegacyAgriculturalPersistenceEvent(StrictModel):
    event_id: NonEmptyString
    persistence_status: Literal[
        "persistent_crop_support",
        "not_persistent",
        "insufficient_series",
        "insufficient_collection",
    ]
    estimated_onset_window_end: date | None
    post_change_window_start: date | None
    post_change_window_end: date | None
    observed_duration_days: Annotated[int, Field(ge=0, strict=True)]
    valid_period_count: Annotated[int, Field(ge=0, strict=True)]
    qualifying_period_count: Annotated[int, Field(ge=0, strict=True)]
    observed_consecutive_qualifying_periods: Annotated[int, Field(ge=0, strict=True)]
    gap_period_count: Annotated[int, Field(ge=0, strict=True)]
    maximum_consecutive_gap_periods: Annotated[int, Field(ge=0, strict=True)]
    long_gap_interpolation_used: Literal[False]
    temporal_disagreement_present: bool
    cross_source_disagreement_state: Literal["single_source_not_assessed"]
    forest_recovery_assessed: Literal[False]
    event_area_ha: Annotated[float, Field(gt=0)] | None
    persistent_crop_area_ha: Annotated[float, Field(ge=0)]
    persistent_event_fraction: Annotated[float, Field(ge=0, le=1)]
    support_raster_path: NonEmptyString | None
    support_raster_sha256: Sha256Digest | None
    sensitivity: list[PersistenceSensitivity]
    per_period_support: list[PeriodSupport]
    quality_flags: list[NonEmptyString]

    @model_validator(mode="after")
    def status_matches_support(self) -> LegacyAgriculturalPersistenceEvent:
        if self.persistence_status == "persistent_crop_support":
            if (
                self.persistent_crop_area_ha <= 0
                or not self.support_raster_path
                or self.support_raster_sha256 is None
            ):
                raise ValueError("persistent_crop_support requiere área y raster")
        elif self.persistent_crop_area_ha != 0 or self.persistent_event_fraction != 0:
            raise ValueError("un estado no persistente no puede conservar área persistente")
        if (self.support_raster_path is None) != (self.support_raster_sha256 is None):
            raise ValueError("support raster path y hash deben declararse juntos")
        if self.event_area_ha is not None:
            tolerance = max(1e-6, 0.005 / self.event_area_ha)
            fraction_error = abs(
                self.persistent_event_fraction - self.persistent_crop_area_ha / self.event_area_ha
            )
            if fraction_error > tolerance:
                raise ValueError("persistent_event_fraction no coincide con áreas")
        return self


class UpstreamEventBundleReference(StrictModel):
    analysis_id: UUID
    created_at: datetime | None = None
    input_sha256: Sha256Digest
    manifest_sha256: Sha256Digest
    bundle_name: NonEmptyString
    event_artifact_path: Literal["json/evidence/disturbance_events.geojson"]
    event_artifact_sha256: Sha256Digest

    @field_validator("created_at")
    @classmethod
    def optional_created_at_has_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("source event created_at debe incluir zona horaria")
        return value


class SourceBundleReference(StrictModel):
    analysis_id: UUID
    input_sha256: Sha256Digest
    manifest_sha256: Sha256Digest
    bundle_name: NonEmptyString
    source_event_bundle: UpstreamEventBundleReference


class LegacyAgriculturalPersistenceDocument(StrictModel):
    """Resultado de persistencia, incluyendo insuficiencia y desacuerdo explícitos."""

    schema_version: Literal["1.0.0"]
    analysis_id: UUID
    establishment_id: NonEmptyString
    created_at: datetime
    source_collection_bundle: SourceBundleReference
    persistence_rule: NonEmptyString
    primary_crop_observation_fraction: Annotated[float, Field(gt=0, le=1)]
    events: list[LegacyAgriculturalPersistenceEvent]

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at debe incluir zona horaria")
        return value

    @field_validator("events")
    @classmethod
    def event_ids_are_unique(
        cls, value: list[LegacyAgriculturalPersistenceEvent]
    ) -> list[LegacyAgriculturalPersistenceEvent]:
        identifiers = [event.event_id for event in value]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("agricultural persistence contiene event_id duplicados")
        return value


class SignalStrengthArea(StrictModel):
    signal_strength: AgriculturalSignalStrength
    qualifying_period_minimum: Annotated[int, Field(ge=1, strict=True)]
    qualifying_period_maximum: Annotated[int, Field(ge=1, strict=True)] | None
    area_ha: Annotated[float, Field(ge=0)]
    event_fraction: Annotated[float, Field(ge=0, le=1)]


class AgriculturalPersistenceEvent(StrictModel):
    """Soporte agrícola post-evento v2; nombres persistent_* son aliases transitorios."""

    event_id: NonEmptyString
    persistence_status: Literal[
        "agricultural_support_detected",
        "not_detected",
        "insufficient_series",
        "insufficient_collection",
    ]
    signal_strength: AgriculturalSignalStrength | None
    estimated_onset_window_end: date | None
    post_change_window_start: date | None
    post_change_window_end: date | None
    effective_observation_date: date | None
    observed_duration_days: Annotated[int, Field(ge=0, strict=True)]
    valid_period_count: Annotated[int, Field(ge=0, strict=True)]
    qualifying_period_count: Annotated[int, Field(ge=0, strict=True)]
    observed_consecutive_qualifying_periods: Annotated[int, Field(ge=0, strict=True)]
    gap_period_count: Annotated[int, Field(ge=0, strict=True)]
    maximum_consecutive_gap_periods: Annotated[int, Field(ge=0, strict=True)]
    long_gap_interpolation_used: Literal[False]
    temporal_disagreement_present: bool
    cross_source_disagreement_state: Literal["single_source_not_assessed"]
    forest_recovery_assessed: Literal[False]
    event_area_ha: Annotated[float, Field(gt=0)] | None
    observed_crop_area_ha: Annotated[float, Field(ge=0)] = 0.0
    candidate_agricultural_coverage_fraction: Annotated[float, Field(ge=0, le=1)] = 0.0
    candidate_agricultural_coverage_threshold: Annotated[float, Field(ge=0, le=1)] = 0.0
    candidate_agricultural_coverage_gate_met: bool = False
    persistent_crop_area_ha: Annotated[float, Field(ge=0)]
    persistent_event_fraction: Annotated[float, Field(ge=0, le=1)]
    support_raster_path: NonEmptyString | None
    support_raster_sha256: Sha256Digest | None
    strength_raster_path: NonEmptyString | None
    strength_raster_sha256: Sha256Digest | None
    signal_strength_areas: list[SignalStrengthArea]
    sensitivity: list[PersistenceSensitivity]
    per_period_support: list[PeriodSupport]
    quality_flags: list[NonEmptyString]

    @model_validator(mode="after")
    def status_matches_support(self) -> AgriculturalPersistenceEvent:
        detected = self.persistence_status == "agricultural_support_detected"
        if detected:
            if (
                self.persistent_crop_area_ha <= 0
                or self.signal_strength is None
                or self.effective_observation_date is None
                or self.support_raster_path is None
                or self.support_raster_sha256 is None
                or self.strength_raster_path is None
                or self.strength_raster_sha256 is None
            ):
                raise ValueError("agricultural_support_detected requiere señal, área y rasters")
        elif (
            self.persistent_crop_area_ha != 0
            or self.persistent_event_fraction != 0
            or self.signal_strength is not None
            or self.effective_observation_date is not None
        ):
            raise ValueError("un estado sin detección no puede conservar soporte positivo")
        if (self.support_raster_path is None) != (self.support_raster_sha256 is None):
            raise ValueError("support raster path y hash deben declararse juntos")
        if (self.strength_raster_path is None) != (self.strength_raster_sha256 is None):
            raise ValueError("strength raster path y hash deben declararse juntos")
        if self.event_area_ha is not None:
            tolerance = max(1e-6, 0.005 / self.event_area_ha)
            expected = self.persistent_crop_area_ha / self.event_area_ha
            if abs(self.persistent_event_fraction - expected) > tolerance:
                raise ValueError("persistent_event_fraction no coincide con áreas")
            if (
                abs(
                    sum(item.area_ha for item in self.signal_strength_areas)
                    - self.persistent_crop_area_ha
                )
                > 0.005
            ):
                raise ValueError("signal_strength_areas no coincide con área de soporte")
        return self


class AgriculturalPersistenceDocument(StrictModel):
    """Contrato v2 de ocurrencia agrícola; conserva el nombre de componente por transición."""

    schema_version: Literal["2.0.0", "2.1.0"]
    analysis_id: UUID
    establishment_id: NonEmptyString
    created_at: datetime
    source_collection_bundle: SourceBundleReference
    persistence_rule: NonEmptyString
    primary_crop_observation_fraction: Annotated[float, Field(gt=0, le=1)]
    events: list[AgriculturalPersistenceEvent]

    @model_validator(mode="before")
    @classmethod
    def upgrade_v2_coverage_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("schema_version") != "2.0.0":
            return value
        upgraded = dict(value)
        events = []
        for item in value.get("events", []):
            if not isinstance(item, dict):
                events.append(item)
                continue
            event = dict(item)
            fraction = float(event.get("persistent_event_fraction", 0.0))
            event.setdefault("observed_crop_area_ha", event.get("persistent_crop_area_ha", 0.0))
            event.setdefault("candidate_agricultural_coverage_fraction", fraction)
            event.setdefault("candidate_agricultural_coverage_threshold", 0.0)
            event.setdefault(
                "candidate_agricultural_coverage_gate_met",
                event.get("persistence_status") == "agricultural_support_detected",
            )
            events.append(event)
        upgraded["events"] = events
        return upgraded

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at debe incluir zona horaria")
        return value

    @field_validator("events")
    @classmethod
    def event_ids_are_unique(
        cls, value: list[AgriculturalPersistenceEvent]
    ) -> list[AgriculturalPersistenceEvent]:
        identifiers = [event.event_id for event in value]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("agricultural persistence contiene event_id duplicados")
        return value


def load_agricultural_persistence_config(
    path: Path,
) -> LegacyAgriculturalPersistenceConfig | AgriculturalPersistenceConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("agricultural_persistence_config_root_must_be_mapping")
    if payload.get("schema_version") == "1.0.0":
        return LegacyAgriculturalPersistenceConfig.model_validate(payload)
    if payload.get("schema_version") in {"2.0.0", "2.1.0"}:
        return AgriculturalPersistenceConfig.model_validate(payload)
    raise ValueError("agricultural_persistence_config_schema_version_unsupported")


def load_agricultural_persistence_document(
    raw: str,
) -> LegacyAgriculturalPersistenceDocument | AgriculturalPersistenceDocument:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("agricultural_persistence_root_must_be_mapping")
    if payload.get("schema_version") == "1.0.0":
        return LegacyAgriculturalPersistenceDocument.model_validate(payload)
    if payload.get("schema_version") in {"2.0.0", "2.1.0"}:
        return AgriculturalPersistenceDocument.model_validate(payload)
    raise ValueError("agricultural_persistence_schema_version_unsupported")


def materialize_agricultural_persistence(
    *,
    source_collection_bundle: Path,
    output_root: Path,
    config_path: Path,
    created_at: datetime,
) -> Path:
    """Deriva soporte agrícola post-evento con semántica versionada."""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("agricultural_persistence_created_at_requires_timezone")
    config = load_agricultural_persistence_config(config_path)
    bundle, manifest = _load_bundle(source_collection_bundle)
    raw = AgriculturalCollectionDocument.model_validate_json(
        _artifact(bundle, manifest, _RAW_DOCUMENT).read_text(encoding="utf-8")
    )
    metadata = json.loads(_artifact(bundle, manifest, _RAW_METADATA).read_text("utf-8"))
    collection = json.loads(_artifact(bundle, manifest, _RAW_EVENTS).read_text("utf-8"))
    source = metadata.get("source") if isinstance(metadata, dict) else None
    if not isinstance(source, dict) or source.get("source_id") != config.source_id:
        raise ValueError("agricultural_persistence_source_mismatch")
    geometries = _event_geometries(collection)
    source_reference = _source_reference(bundle, manifest)
    identity = {
        "source_manifest_sha256": source_reference["manifest_sha256"],
        "config_sha256": _sha256_file(config_path),
    }
    schema_version = (
        LEGACY_AGRICULTURAL_PERSISTENCE_SCHEMA_VERSION
        if isinstance(config, LegacyAgriculturalPersistenceConfig)
        else AGRICULTURAL_PERSISTENCE_SCHEMA_VERSION
    )
    analysis_id = uuid5(
        NAMESPACE_URL,
        f"agricultural-persistence-v{schema_version.split('.')[0]}:"
        + json.dumps(identity, sort_keys=True, separators=(",", ":")),
    )
    files: dict[str, bytes] = {}
    event_results: list[dict[str, Any]] = []
    observations: list[AgriculturalEvidenceObservation | AgriculturalEvidenceObservationV2] = []
    visualizations: list[dict[str, Any]] = []
    for event in raw.events:
        result, raster_files, observation = _evaluate_event(
            event=event,
            raw=raw,
            bundle=bundle,
            manifest=manifest,
            source=source,
            geometry=geometries[event.event_id],
            config=config,
        )
        event_results.append(result)
        files.update(raster_files)
        support_path = result["support_raster_path"]
        if support_path is not None and support_path in raster_files:
            raster_content = raster_files[support_path]
            visualization = render_agricultural_persistence_evidence(
                event=result,
                raster_content=raster_content,
                source_raster_path=str(result["support_raster_path"]),
                source_label=(
                    f"Dynamic World V1 · {source.get('collection', 'GOOGLE/DYNAMICWORLD/V1')}"
                ),
            )
            if visualization is not None:
                files[visualization.path] = visualization.content
                visualizations.append(visualization.record)
        if observation is not None:
            observations.append(observation)

    document_type = (
        LegacyAgriculturalPersistenceDocument
        if isinstance(config, LegacyAgriculturalPersistenceConfig)
        else AgriculturalPersistenceDocument
    )
    persistence = document_type.model_validate(
        {
            "schema_version": schema_version,
            "analysis_id": str(analysis_id),
            "establishment_id": raw.establishment_id,
            "created_at": created_at.isoformat(),
            "source_collection_bundle": source_reference,
            "persistence_rule": _rule_name(config),
            "primary_crop_observation_fraction": config.primary_crop_observation_fraction,
            "events": event_results,
        }
    )
    evidence: AgriculturalEvidenceDocument | AgriculturalEvidenceDocumentV2
    if isinstance(config, LegacyAgriculturalPersistenceConfig):
        evidence = AgriculturalEvidenceDocument(
            schema_version="1.0.0",
            evidence_set_id=analysis_id,
            created_at=created_at,
            observations=[
                item for item in observations if isinstance(item, AgriculturalEvidenceObservation)
            ],
        )
    else:
        evidence = AgriculturalEvidenceDocumentV2(
            schema_version=AGRICULTURAL_EVIDENCE_SCHEMA_VERSION,
            evidence_set_id=analysis_id,
            created_at=created_at,
            observations=[
                item for item in observations if isinstance(item, AgriculturalEvidenceObservationV2)
            ],
        )
    persistence_payload = persistence.model_dump(mode="json")
    evidence_payload = evidence.model_dump(mode="json")
    files.update(
        {
            "json/evidence/agricultural_persistence.json": _json_bytes(persistence_payload),
            "json/evidence/agricultural_evidence_persistent.json": _json_bytes(evidence_payload),
            "tables/evidence/agricultural_persistence.csv": _persistence_csv(persistence),
            "json/run/summary.json": _json_bytes(
                {
                    **persistence_payload,
                    "events": [],
                    "persistent_event_count": sum(
                        item.persistence_status
                        in {"persistent_crop_support", "agricultural_support_detected"}
                        for item in persistence.events
                    ),
                    "attribution_generated": False,
                    "conversion_confirmed": False,
                }
            ),
        }
    )
    run_name = (
        f"{_safe_id(raw.establishment_id)}-agriculture-persistence-v{schema_version[0]}__"
        f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}__{str(analysis_id)[:8]}"
    )
    run_directory = output_root.resolve() / run_name
    _publish_bundle(
        run_directory=run_directory,
        files=files,
        manifest_metadata={
            "schema_version": schema_version,
            "analysis_id": str(analysis_id),
            "establishment_id": raw.establishment_id,
            "created_at": created_at.isoformat(),
            "input_sha256": manifest.get("input_sha256"),
            "source_collection_bundle": source_reference,
            "config_sha256": identity["config_sha256"],
            "persistence_evaluated": True,
            "attribution_generated": False,
            "long_gap_interpolation_used": False,
            "visualizations": visualizations,
        },
        remote_data_accessed=False,
        datasets=(source,),
    )
    return run_directory


def _evaluate_event(
    *,
    event: Any,
    raw: AgriculturalCollectionDocument,
    bundle: Path,
    manifest: Mapping[str, Any],
    source: Mapping[str, Any],
    geometry: Mapping[str, Any],
    config: LegacyAgriculturalPersistenceConfig | AgriculturalPersistenceConfig,
) -> tuple[
    dict[str, Any],
    dict[str, bytes],
    AgriculturalEvidenceObservation | AgriculturalEvidenceObservationV2 | None,
]:
    legacy = isinstance(config, LegacyAgriculturalPersistenceConfig)
    candidate_coverage_threshold = (
        0.0
        if legacy
        else cast(
            AgriculturalPersistenceConfig, config
        ).minimum_candidate_agricultural_coverage_fraction
    )
    rows = [row for row in raw.rows if row.event_id == event.event_id]
    windows = sorted({row.window_id for row in rows})
    if event.collection_status != "collected" or not windows:
        return _insufficient_event(event, "insufficient_collection", legacy=legacy), {}, None
    representative = {
        window: next(row for row in rows if row.window_id == window) for window in windows
    }
    first = representative[windows[0]]
    last = representative[windows[-1]]
    footprint_path = first.event_footprint_path
    footprint, profile = _read_single_band(_artifact(bundle, manifest, footprint_path))
    footprint_valid = np.isfinite(footprint) & (footprint > 0) & (footprint <= 1)
    if not np.any(footprint_valid):
        raise ValueError("agricultural_persistence_empty_event_footprint")
    valid_stack: list[NDArray[np.bool_]] = []
    fraction_stack: list[NDArray[np.float32]] = []
    period_support: list[dict[str, Any]] = []
    pixel_area_ha = abs(float(profile["transform"].a * profile["transform"].e)) / 10000.0
    footprint_area = np.sum(footprint[footprint_valid]) * pixel_area_ha
    event_area = float(event.event_area_exact_ha or footprint_area)
    for window in windows:
        row = representative[window]
        if row.valid_count_band is not None and row.qualifying_count_band is not None:
            valid_count, qualifying_count, current = _read_temporal_count_bands(
                _artifact(bundle, manifest, row.raster_path),
                window_id=window,
                valid_count_band=row.valid_count_band,
                qualifying_count_band=row.qualifying_count_band,
            )
            fraction = np.divide(
                qualifying_count,
                valid_count,
                out=np.zeros_like(valid_count, dtype="float32"),
                where=(valid_count > 0) & np.isfinite(valid_count),
            )
        else:
            arrays, current = _read_four_bands(_artifact(bundle, manifest, row.raster_path))
            valid_count, _, _, fraction = arrays
        if not _same_grid(current, profile):
            raise ValueError("agricultural_persistence_rasters_do_not_share_grid")
        valid = (
            footprint_valid
            & np.isfinite(valid_count)
            & (valid_count != current["nodata"])
            & (valid_count >= config.minimum_valid_observations_per_period)
        )
        fraction = np.where(valid, fraction, 0).astype("float32")
        valid_stack.append(valid)
        fraction_stack.append(fraction)
        qualifying = valid & (fraction >= config.primary_crop_observation_fraction)
        valid_area = _weighted_area(valid, footprint, pixel_area_ha)
        qualifying_area = _weighted_area(qualifying, footprint, pixel_area_ha)
        candidate_coverage = min(1.0, qualifying_area / event_area)
        coverage_gate = bool(
            qualifying_area > 0
            and (
                legacy
                or (
                    candidate_coverage > candidate_coverage_threshold
                    or math.isclose(
                        candidate_coverage,
                        candidate_coverage_threshold,
                        abs_tol=1e-12,
                    )
                )
            )
        )
        period_support.append(
            {
                "window_id": window,
                "start_date": row.start_date,
                "end_date_exclusive": row.end_date_exclusive,
                "valid_area_ha": valid_area,
                "qualifying_crop_area_ha": qualifying_area,
                "candidate_agricultural_coverage_fraction": candidate_coverage,
                "candidate_coverage_gate_met": coverage_gate,
                "nodata_area_ha": max(0.0, event_area - valid_area),
                "matched_scene_count": row.matched_scene_count,
                "raster_path": row.raster_path,
            }
        )
    valid_cube = np.stack(valid_stack)
    fraction_cube = np.stack(fraction_stack)
    valid_counts = valid_cube.sum(axis=0)
    primary_qualifying = valid_cube & (fraction_cube >= config.primary_crop_observation_fraction)
    qualifying_counts = primary_qualifying.sum(axis=0)
    observed_duration_by_pixel = _observed_duration_days(
        valid_cube,
        [representative[window].start_date for window in windows],
        [representative[window].end_date_exclusive for window in windows],
    )
    maximum_gap_by_pixel = _maximum_consecutive_false(valid_cube)
    sufficient_observation = (
        footprint_valid
        & (valid_counts >= config.minimum_valid_periods)
        & (observed_duration_by_pixel >= config.minimum_post_onset_duration_days)
        & (maximum_gap_by_pixel <= config.maximum_unobserved_gap_periods)
    )
    series_sufficient = bool(np.any(sufficient_observation))
    occurrence = footprint_valid & (qualifying_counts >= config.minimum_qualifying_periods)
    raw_occurrence_mask = sufficient_observation & occurrence if legacy else occurrence
    observed_crop_area = _bounded_subset_area(
        _weighted_area(raw_occurrence_mask, footprint, pixel_area_ha),
        event_area,
    )
    coverage_assessment = _candidate_period_coverage_assessment(
        qualifying_areas_ha=[float(item["qualifying_crop_area_ha"]) for item in period_support],
        candidate_area_ha=event_area,
        minimum_coverage_fraction=candidate_coverage_threshold,
    )
    candidate_coverage_gate = (
        bool(observed_crop_area > 0) if legacy else coverage_assessment.detected
    )
    support_mask = raw_occurrence_mask if candidate_coverage_gate else np.zeros_like(occurrence)
    support = np.full(footprint.shape, config.support_raster_nodata, dtype="float32")
    support[footprint_valid] = 0.0
    support[support_mask] = footprint[support_mask].astype("float32")
    support_path = "tiffs/evidence/agricultural_persistence/" + (
        f"{_safe_id(event.event_id)}_"
        + ("persistent_crop_support.tif" if legacy else "agricultural_occurrence_support.tif")
    )
    support_content = _write_single_band(
        support,
        profile,
        # El nombre de banda se conserva para que consumidores v1 puedan leer el raster;
        # el contrato v2 cambia la semántica temporal, no la fracción geométrica.
        "persistent_crop_event_fraction",
    )
    support_hash = hashlib.sha256(support_content).hexdigest()
    area = observed_crop_area if candidate_coverage_gate else 0.0
    sensitivity = []
    for threshold in config.sensitivity_crop_observation_fractions:
        qualifying_at_threshold = (valid_cube & (fraction_cube >= threshold)).sum(axis=0)
        qualifying_mask = footprint_valid & (
            qualifying_at_threshold >= config.minimum_qualifying_periods
        )
        mask = sufficient_observation & qualifying_mask if legacy else qualifying_mask
        observed_threshold_area = _bounded_subset_area(
            _weighted_area(mask, footprint, pixel_area_ha),
            event_area,
        )
        threshold_area = (
            observed_threshold_area
            if legacy or observed_threshold_area / event_area >= candidate_coverage_threshold
            else 0.0
        )
        sensitivity.append(
            {
                "crop_observation_fraction_threshold": threshold,
                "persistent_crop_area_ha": threshold_area,
                "persistent_event_fraction": min(1.0, threshold_area / event_area),
            }
        )
    period_has_valid = [item["valid_area_ha"] > 0 for item in period_support]
    period_qualifies = [
        bool(item["candidate_coverage_gate_met"])
        if not legacy
        else item["qualifying_crop_area_ha"] > 0
        for item in period_support
    ]
    gap_count = sum(not value for value in period_has_valid)
    max_gap = _longest_run([not value for value in period_has_valid])
    consecutive_qualifying_by_pixel = _maximum_consecutive_true(primary_qualifying)
    if np.any(support_mask):
        observed_duration = int(np.min(observed_duration_by_pixel[support_mask]))
        valid_period_count = int(np.min(valid_counts[support_mask]))
        qualifying_period_count = int(np.min(qualifying_counts[support_mask]))
        max_consecutive = int(np.min(consecutive_qualifying_by_pixel[support_mask]))
    else:
        observed_duration = int(np.max(observed_duration_by_pixel[footprint_valid]))
        valid_period_count = int(np.max(valid_counts[footprint_valid]))
        qualifying_period_count = int(np.max(qualifying_counts[footprint_valid]))
        max_consecutive = int(np.max(consecutive_qualifying_by_pixel[footprint_valid]))
    if not legacy:
        qualifying_period_count = coverage_assessment.qualifying_period_count
    quality_flags: list[str] = []
    if len(windows) < config.minimum_valid_periods:
        quality_flags.append("minimum_period_count_not_met")
    if observed_duration < config.minimum_post_onset_duration_days:
        quality_flags.append("minimum_post_onset_duration_not_met")
    if valid_period_count < config.minimum_valid_periods:
        quality_flags.append("minimum_valid_period_count_not_met")
    if np.any(footprint_valid & (maximum_gap_by_pixel > config.maximum_unobserved_gap_periods)):
        quality_flags.append("long_unobserved_gap_present")
    all_valid_periods_qualify = all(
        period_qualifies[index] for index, valid in enumerate(period_has_valid) if valid
    )
    if any(period_qualifies) and not all_valid_periods_qualify:
        quality_flags.append("temporal_class_disagreement_preserved")
    if observed_crop_area > 0 and not candidate_coverage_gate:
        quality_flags.append("candidate_agricultural_coverage_below_five_percent")
    quality_flags.extend(
        [
            "single_source_no_cross_source_disagreement_assessment",
            "forest_recovery_requires_rf_attributor",
        ]
    )
    if area > 0:
        status = "persistent_crop_support" if legacy else "agricultural_support_detected"
    elif not series_sufficient:
        status = "insufficient_series"
    else:
        status = "not_persistent" if legacy else "not_detected"

    effective_observation_date = None
    if area > 0:
        first_qualifying_index = next(
            index for index, qualifies in enumerate(period_qualifies) if qualifies
        )
        effective_observation_date = representative[
            windows[first_qualifying_index]
        ].end_date_exclusive - timedelta(days=1)

    signal_strength: AgriculturalSignalStrength | None = None
    strength_content: bytes | None = None
    strength_path: str | None = None
    strength_hash: str | None = None
    signal_strength_areas: list[dict[str, Any]] = []
    if not legacy and area > 0:
        signal_strength = coverage_assessment.signal_strength or _signal_strength(
            qualifying_period_count
        )
        strength = np.full(footprint.shape, config.support_raster_nodata, dtype="float32")
        strength[footprint_valid] = 0.0
        strength[support_mask & (qualifying_counts == 1)] = 1.0
        strength[support_mask & (qualifying_counts == 2)] = 2.0
        strength[support_mask & (qualifying_counts >= 3)] = 3.0
        strength_path = (
            "tiffs/evidence/agricultural_persistence/"
            f"{_safe_id(event.event_id)}_agricultural_signal_strength.tif"
        )
        strength_content = _write_single_band(strength, profile, "agricultural_signal_strength")
        strength_hash = hashlib.sha256(strength_content).hexdigest()
        for level, minimum, maximum, mask in (
            ("weak", 1, 1, support_mask & (qualifying_counts == 1)),
            ("moderate", 2, 2, support_mask & (qualifying_counts == 2)),
            ("strong", 3, None, support_mask & (qualifying_counts >= 3)),
        ):
            level_area = _bounded_subset_area(
                _weighted_area(mask, footprint, pixel_area_ha),
                event_area,
            )
            signal_strength_areas.append(
                {
                    "signal_strength": level,
                    "qualifying_period_minimum": minimum,
                    "qualifying_period_maximum": maximum,
                    "area_ha": level_area,
                    "event_fraction": min(1.0, level_area / event_area),
                }
            )
    result = {
        "event_id": event.event_id,
        "persistence_status": status,
        "estimated_onset_window_end": event.estimated_onset_window_end,
        "post_change_window_start": first.start_date,
        "post_change_window_end": last.end_date_exclusive - timedelta(days=1),
        **({} if legacy else {"effective_observation_date": effective_observation_date}),
        "observed_duration_days": observed_duration,
        "valid_period_count": valid_period_count,
        "qualifying_period_count": qualifying_period_count,
        "observed_consecutive_qualifying_periods": max_consecutive,
        "gap_period_count": gap_count,
        "maximum_consecutive_gap_periods": max_gap,
        "long_gap_interpolation_used": False,
        "temporal_disagreement_present": any(period_qualifies)
        and not all(
            period_qualifies[index] for index, valid in enumerate(period_has_valid) if valid
        ),
        "cross_source_disagreement_state": "single_source_not_assessed",
        "forest_recovery_assessed": False,
        "event_area_ha": event_area,
        **(
            {}
            if legacy
            else {
                "observed_crop_area_ha": observed_crop_area,
                "candidate_agricultural_coverage_fraction": (
                    coverage_assessment.maximum_coverage_fraction
                ),
                "candidate_agricultural_coverage_threshold": (candidate_coverage_threshold),
                "candidate_agricultural_coverage_gate_met": candidate_coverage_gate,
            }
        ),
        "persistent_crop_area_ha": area,
        "persistent_event_fraction": min(1.0, area / event_area),
        "support_raster_path": support_path,
        "support_raster_sha256": support_hash,
        **(
            {}
            if legacy
            else {
                "signal_strength": signal_strength,
                "strength_raster_path": strength_path,
                "strength_raster_sha256": strength_hash,
                "signal_strength_areas": signal_strength_areas,
            }
        ),
        "sensitivity": sensitivity,
        "per_period_support": period_support,
        "quality_flags": list(dict.fromkeys(quality_flags)),
    }
    observation: AgriculturalEvidenceObservation | AgriculturalEvidenceObservationV2 | None = None
    if status in {"persistent_crop_support", "agricultural_support_detected"}:
        observation_kwargs = {
            "event": event,
            "result": result,
            "source": source,
            "geometry": geometry,
            "support": support,
            "profile": profile,
            "support_path": support_path,
            "support_hash": support_hash,
            "valid_period_count": valid_period_count,
            "qualifying_period_count": qualifying_period_count,
            "max_consecutive": max_consecutive,
            "duration": observed_duration,
            "event_area": event_area,
            "effective_observation_date": effective_observation_date,
            "signal_strength": signal_strength,
        }
        if isinstance(config, LegacyAgriculturalPersistenceConfig):
            observation = _observation(config=config, **observation_kwargs)
        else:
            observation = _observation_v2(config=config, **observation_kwargs)
    raster_files = {support_path: support_content}
    if strength_path is not None and strength_content is not None:
        raster_files[strength_path] = strength_content
    return result, raster_files, observation


def _observation(
    *,
    event: Any,
    result: Mapping[str, Any],
    source: Mapping[str, Any],
    geometry: Mapping[str, Any],
    support: NDArray[np.float32],
    profile: Mapping[str, Any],
    support_path: str,
    support_hash: str,
    config: LegacyAgriculturalPersistenceConfig,
    valid_period_count: int,
    qualifying_period_count: int,
    max_consecutive: int,
    duration: int,
    event_area: float,
    effective_observation_date: date | None,
    signal_strength: AgriculturalSignalStrength | None,
) -> AgriculturalEvidenceObservation:
    del effective_observation_date, signal_strength
    attributed_geometry = _attributed_geometry(support, profile, geometry)
    source_payload = {
        "dataset_id": source["source_id"],
        "provider": source["provider"],
        "collection": source["collection"],
        "version": source["version"],
        "asset": support_path,
        "license": source["license"],
        "access_date": source["access_date"],
        "attribution": source["attribution"],
        "restrictions": source.get("restrictions", []),
    }
    area = float(result["persistent_crop_area_ha"])
    valid_area = max(item["valid_area_ha"] for item in result["per_period_support"])
    nodata = max(0.0, event_area - valid_area)
    return AgriculturalEvidenceObservation.model_validate(
        {
            "evidence_id": f"AGR-PERSIST-{event.event_id}-{support_hash[:12]}",
            "event_id": event.event_id,
            "land_use": "crop",
            "source": source_payload,
            "source_descriptor_sha256": canonical_payload_sha256(source_payload),
            "geometry": {
                "type": attributed_geometry["type"],
                "coordinates": attributed_geometry["coordinates"],
                "crs": "EPSG:4326",
            },
            "spatial_support": {
                "source_resolution_m": float(source["spatial_resolution_m"]),
                "event_area_ha": event_area,
                "valid_area_ha": min(event_area, valid_area),
                "attributed_area_ha": area,
                "event_coverage_fraction": min(1.0, valid_area / event_area),
                "attributed_event_fraction": min(1.0, area / event_area),
                "resampling_method": "none_native_equal_area_grid",
            },
            "temporal_support": {
                "estimated_onset_window_end": event.estimated_onset_window_end,
                "post_change_window_start": result["post_change_window_start"],
                "post_change_window_end": result["post_change_window_end"],
                "effective_observation_date": result["post_change_window_end"],
                "valid_observation_count": valid_period_count,
                "qualifying_observation_count": qualifying_period_count,
                "minimum_consecutive_qualifying_periods": config.minimum_qualifying_periods,
                "observed_consecutive_qualifying_periods": max_consecutive,
                "minimum_qualifying_periods": config.minimum_qualifying_periods,
                "observed_qualifying_periods": qualifying_period_count,
                "minimum_observation_duration_days": config.minimum_post_onset_duration_days,
                "observed_duration_days": duration,
                "persistence_basis": config.seasonality_rule,
                "persistence_satisfied": True,
                "long_gap_interpolation_used": False,
                "persistence_rule": _rule_name(config),
            },
            "quality": {
                "confidence": None,
                "confidence_semantics": "deterministic_threshold_support_not_probability",
                "quality_flags": result["quality_flags"],
                "discarded_observation_reasons": ["invalid_or_insufficient_monthly_support"],
                "nodata_area_ha": nodata,
                "uncertainty_notes": [
                    "dynamic_world_class_score_is_not_calibrated_conversion_probability",
                    "cross_source_disagreement_not_assessed_with_single_source",
                ],
            },
            "artifact_sha256": support_hash,
        }
    )


def _observation_v2(
    *,
    event: Any,
    result: Mapping[str, Any],
    source: Mapping[str, Any],
    geometry: Mapping[str, Any],
    support: NDArray[np.float32],
    profile: Mapping[str, Any],
    support_path: str,
    support_hash: str,
    config: AgriculturalPersistenceConfig,
    valid_period_count: int,
    qualifying_period_count: int,
    max_consecutive: int,
    duration: int,
    event_area: float,
    effective_observation_date: date | None,
    signal_strength: AgriculturalSignalStrength | None,
) -> AgriculturalEvidenceObservationV2:
    del max_consecutive, duration
    if effective_observation_date is None or signal_strength is None:
        raise ValueError("agricultural occurrence observation requiere fecha y potencia")
    attributed_geometry = _attributed_geometry(support, profile, geometry)
    source_payload = {
        "dataset_id": source["source_id"],
        "provider": source["provider"],
        "collection": source["collection"],
        "version": source["version"],
        "asset": support_path,
        "license": source["license"],
        "access_date": source["access_date"],
        "attribution": source["attribution"],
        "restrictions": source.get("restrictions", []),
    }
    area = float(result["persistent_crop_area_ha"])
    valid_area = max(item["valid_area_ha"] for item in result["per_period_support"])
    nodata = max(0.0, event_area - valid_area)
    return AgriculturalEvidenceObservationV2.model_validate(
        {
            "evidence_id": f"AGR-OCCURRENCE-{event.event_id}-{support_hash[:12]}",
            "event_id": event.event_id,
            "land_use": "crop",
            "source": source_payload,
            "source_descriptor_sha256": canonical_payload_sha256(source_payload),
            "geometry": {
                "type": attributed_geometry["type"],
                "coordinates": attributed_geometry["coordinates"],
                "crs": "EPSG:4326",
            },
            "spatial_support": {
                "source_resolution_m": float(source["spatial_resolution_m"]),
                "event_area_ha": event_area,
                "valid_area_ha": min(event_area, valid_area),
                "attributed_area_ha": area,
                "event_coverage_fraction": min(1.0, valid_area / event_area),
                "attributed_event_fraction": min(1.0, area / event_area),
                "resampling_method": "none_native_equal_area_grid",
            },
            "temporal_support": {
                "estimated_onset_window_end": event.estimated_onset_window_end,
                "post_change_window_start": result["post_change_window_start"],
                "post_change_window_end": result["post_change_window_end"],
                "effective_observation_date": effective_observation_date,
                "valid_period_count": valid_period_count,
                "qualifying_period_count": qualifying_period_count,
                "evidence_basis": config.evidence_rule,
                "evidence_satisfied": True,
                "signal_strength": signal_strength,
                "long_gap_interpolation_used": False,
                "evidence_rule": _rule_name(config),
            },
            "quality": {
                "confidence": None,
                "confidence_semantics": "signal_strength_not_probability_of_conversion",
                "quality_flags": result["quality_flags"],
                "discarded_observation_reasons": ["invalid_monthly_observations_excluded"],
                "nodata_area_ha": nodata,
                "uncertainty_notes": [
                    "dynamic_world_class_score_is_not_calibrated_conversion_probability",
                    "single_month_occurrence_is_weak_temporal_support",
                    "cross_source_disagreement_not_assessed_with_single_source",
                ],
            },
            "artifact_sha256": support_hash,
        }
    )


def _insufficient_event(event: Any, status: str, *, legacy: bool) -> dict[str, Any]:
    result = {
        "event_id": event.event_id,
        "persistence_status": status,
        "estimated_onset_window_end": event.estimated_onset_window_end,
        "post_change_window_start": None,
        "post_change_window_end": None,
        "observed_duration_days": 0,
        "valid_period_count": 0,
        "qualifying_period_count": 0,
        "observed_consecutive_qualifying_periods": 0,
        "gap_period_count": 0,
        "maximum_consecutive_gap_periods": 0,
        "long_gap_interpolation_used": False,
        "temporal_disagreement_present": False,
        "cross_source_disagreement_state": "single_source_not_assessed",
        "forest_recovery_assessed": False,
        "event_area_ha": event.event_area_exact_ha,
        "persistent_crop_area_ha": 0.0,
        "persistent_event_fraction": 0.0,
        "support_raster_path": None,
        "support_raster_sha256": None,
        "sensitivity": [],
        "per_period_support": [],
        "quality_flags": [
            "source_collection_not_available_for_persistence",
            "single_source_no_cross_source_disagreement_assessment",
            "forest_recovery_requires_rf_attributor",
        ],
    }
    if not legacy:
        result.update(
            {
                "signal_strength": None,
                "effective_observation_date": None,
                "observed_crop_area_ha": 0.0,
                "candidate_agricultural_coverage_fraction": 0.0,
                "candidate_agricultural_coverage_threshold": 0.0,
                "candidate_agricultural_coverage_gate_met": False,
                "strength_raster_path": None,
                "strength_raster_sha256": None,
                "signal_strength_areas": [],
            }
        )
    return result


def _attributed_geometry(
    support: NDArray[np.float32],
    profile: Mapping[str, Any],
    event_geometry_wgs84: Mapping[str, Any],
) -> dict[str, Any]:
    mask = support > 0
    raster_shapes = shapes(mask.astype("uint8"), mask=mask, transform=profile["transform"])
    pieces = [shape(geom) for geom, value in raster_shapes if value == 1]
    if not pieces:
        raise ValueError("persistent support no tiene geometría")
    to_grid = Transformer.from_crs("EPSG:4326", profile["crs"], always_xy=True)
    to_wgs84 = Transformer.from_crs(profile["crs"], "EPSG:4326", always_xy=True)
    event_grid = transform(to_grid.transform, shape(dict(event_geometry_wgs84)))
    attributed = unary_union(pieces).intersection(event_grid)
    if attributed.is_empty:
        raise ValueError("persistent support no intersecta la geometría del evento")
    result = transform(to_wgs84.transform, attributed)
    return mapping(result)


def _read_single_band(path: Path) -> tuple[NDArray[np.float32], dict[str, Any]]:
    with rasterio.open(path) as dataset:
        if dataset.count != 1 or dataset.crs is None or dataset.nodata is None:
            raise ValueError("agricultural persistence raster footprint inválido")
        return dataset.read()[0].astype("float32", copy=True), _profile(dataset)


def _read_four_bands(path: Path) -> tuple[NDArray[np.float32], dict[str, Any]]:
    with rasterio.open(path) as dataset:
        if dataset.count != 4 or dataset.crs is None or dataset.nodata is None:
            raise ValueError("agricultural persistence raster mensual inválido")
        return dataset.read().astype("float32"), _profile(dataset)


def _read_temporal_count_bands(
    path: Path,
    *,
    window_id: str,
    valid_count_band: int,
    qualifying_count_band: int,
) -> tuple[NDArray[np.float32], NDArray[np.float32], dict[str, Any]]:
    with rasterio.open(path) as dataset:
        if dataset.crs is None or dataset.nodata is None:
            raise ValueError("agricultural persistence cubo temporal inválido")
        if qualifying_count_band != valid_count_band + 1 or qualifying_count_band > dataset.count:
            raise ValueError("agricultural persistence bandas temporales inválidas")
        expected = (
            f"{window_id}_valid_observation_count",
            f"{window_id}_qualifying_crop_count",
        )
        descriptions = (
            dataset.descriptions[valid_count_band - 1],
            dataset.descriptions[qualifying_count_band - 1],
        )
        if descriptions != expected:
            raise ValueError("agricultural persistence semántica de cubo temporal inválida")
        arrays = dataset.read((valid_count_band, qualifying_count_band)).astype("float32")
        return arrays[0], arrays[1], _profile(dataset)


def _same_grid(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left[key] == right[key] for key in ("width", "height", "crs", "transform"))


def _profile(dataset: rasterio.io.DatasetReader) -> dict[str, Any]:
    return {
        "width": dataset.width,
        "height": dataset.height,
        "crs": dataset.crs,
        "transform": dataset.transform,
        "nodata": dataset.nodata,
    }


def _write_single_band(
    values: NDArray[np.float32], profile: Mapping[str, Any], description: str
) -> bytes:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=profile["width"],
            height=profile["height"],
            count=1,
            dtype="float32",
            crs=profile["crs"],
            transform=profile["transform"],
            nodata=profile["nodata"],
            compress="deflate",
        ) as dataset:
            dataset.write(values, 1)
            dataset.set_band_description(1, description)
        return bytes(memory.read())


def _weighted_area(
    mask: NDArray[np.bool_],
    footprint: NDArray[np.float32],
    pixel_area_ha: float,
) -> float:
    return float(np.sum(footprint[mask], dtype=np.float64) * pixel_area_ha)


def _bounded_subset_area(area_ha: float, event_area_ha: float) -> float:
    """Acota redondeo de grilla sin ocultar una violación material del footprint."""
    tolerance = max(1e-9, event_area_ha * 1e-9)
    if area_ha > event_area_ha + tolerance:
        raise ValueError("agricultural_subset_area_exceeds_exact_event_area")
    return min(area_ha, event_area_ha)


def _longest_run(values: list[bool]) -> int:
    longest = current = 0
    for value in values:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return longest


def _observed_duration_days(
    valid_cube: NDArray[np.bool_],
    starts: list[date],
    ends_exclusive: list[date],
) -> NDArray[np.int32]:
    """Mide por píxel el intervalo realmente observado, no la ventana solicitada."""
    first = np.full(valid_cube.shape[1:], -1, dtype="int32")
    last = np.full(valid_cube.shape[1:], -1, dtype="int32")
    for index, valid in enumerate(valid_cube):
        start_ordinal = starts[index].toordinal()
        end_ordinal = ends_exclusive[index].toordinal()
        first[(first < 0) & valid] = start_ordinal
        last[valid] = end_ordinal
    return np.where((first >= 0) & (last >= 0), last - first, 0).astype("int32")


def _maximum_consecutive_false(valid_cube: NDArray[np.bool_]) -> NDArray[np.int32]:
    current = np.zeros(valid_cube.shape[1:], dtype="int32")
    maximum = np.zeros(valid_cube.shape[1:], dtype="int32")
    for valid in valid_cube:
        current = np.where(valid, 0, current + 1)
        maximum = np.maximum(maximum, current)
    return maximum


def _maximum_consecutive_true(values: NDArray[np.bool_]) -> NDArray[np.int32]:
    current = np.zeros(values.shape[1:], dtype="int32")
    maximum = np.zeros(values.shape[1:], dtype="int32")
    for value in values:
        current = np.where(value, current + 1, 0)
        maximum = np.maximum(maximum, current)
    return maximum


def _signal_strength(qualifying_period_count: int) -> AgriculturalSignalStrength:
    if qualifying_period_count == 1:
        return "weak"
    if qualifying_period_count == 2:
        return "moderate"
    return "strong"


def _rule_name(
    config: LegacyAgriculturalPersistenceConfig | AgriculturalPersistenceConfig,
) -> str:
    if isinstance(config, AgriculturalPersistenceConfig):
        return (
            "post_cutoff_candidate_coverage_gate_"
            f"{config.minimum_candidate_agricultural_coverage_fraction:.6f};"
            "signal_strength_weak_1_moderate_2_strong_3_plus;"
            "series_sufficiency_reported_as_quality"
        )
    return (
        f"{config.minimum_qualifying_periods}_qualifying_of_"
        f"{config.minimum_valid_periods}_valid_periods_over_"
        f"{config.minimum_post_onset_duration_days}_days_nonconsecutive"
    )


def _load_bundle(path: Path) -> tuple[Path, dict[str, Any]]:
    bundle = path.resolve()
    manifest_path = bundle / "json/run/manifest.json"
    if not bundle.is_dir() or not manifest_path.is_file():
        raise ValueError("agricultural collection bundle manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, list):
        raise ValueError("agricultural collection manifest artifacts invalid")
    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise ValueError("agricultural collection manifest artifact invalid")
        relative = artifact["path"]
        if relative in seen:
            raise ValueError("agricultural collection manifest duplicate artifact")
        seen.add(relative)
        item = _safe_child(bundle, relative)
        if (
            not item.is_file()
            or item.is_symlink()
            or item.stat().st_size != artifact.get("size_bytes")
            or _sha256_file(item) != artifact.get("sha256")
        ):
            raise ValueError(f"agricultural collection artifact integrity mismatch:{relative}")
    return bundle, manifest


def _artifact(bundle: Path, manifest: Mapping[str, Any], relative: str) -> Path:
    matches = [
        item
        for item in manifest.get("artifacts", [])
        if isinstance(item, Mapping) and item.get("path") == relative
    ]
    if len(matches) != 1:
        raise ValueError(f"agricultural collection artifact not manifested exactly once:{relative}")
    return _safe_child(bundle, relative)


def _safe_child(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("agricultural collection unsafe path")
    resolved = (root / candidate).resolve()
    if root not in resolved.parents:
        raise ValueError("agricultural collection path escapes bundle")
    return resolved


def _event_geometries(collection: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(collection, dict) or not isinstance(collection.get("features"), list):
        raise ValueError("agricultural collection geojson invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for feature in collection["features"]:
        event_id = str(feature.get("properties", {}).get("event_id", ""))
        geometry = feature.get("geometry")
        if not event_id or not isinstance(geometry, Mapping) or event_id in result:
            raise ValueError("agricultural collection event geometry invalid")
        result[event_id] = geometry
    return result


def _source_reference(bundle: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    upstream = UpstreamEventBundleReference.model_validate(manifest.get("source_event_bundle"))
    if upstream.input_sha256 != manifest.get("input_sha256"):
        raise ValueError("agricultural collection upstream event input_sha256 mismatch")
    return {
        "analysis_id": manifest.get("analysis_id"),
        "input_sha256": manifest.get("input_sha256"),
        "manifest_sha256": _sha256_file(bundle / "json/run/manifest.json"),
        "bundle_name": bundle.name,
        "source_event_bundle": upstream.model_dump(mode="json"),
    }


def _persistence_csv(
    document: LegacyAgriculturalPersistenceDocument | AgriculturalPersistenceDocument,
) -> bytes:
    import csv
    from io import StringIO

    fields = [
        "event_id",
        "persistence_status",
        "post_change_window_start",
        "post_change_window_end",
        "observed_duration_days",
        "valid_period_count",
        "qualifying_period_count",
        "gap_period_count",
        "persistent_crop_area_ha",
        "persistent_event_fraction",
        "quality_flags",
    ]
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for event in document.events:
        row = event.model_dump(mode="json", include=set(fields))
        row["quality_flags"] = json.dumps(row["quality_flags"], ensure_ascii=False)
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def _safe_id(value: str) -> str:
    safe = "".join(character for character in value if character.isalnum() or character in "_-")
    if not safe:
        raise ValueError("agricultural persistence unsafe identifier")
    return safe[:100]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    return f"{serialized}\n".encode()


def agricultural_persistence_json_schema() -> dict[str, Any]:
    schema = AgriculturalPersistenceDocument.model_json_schema(mode="serialization")
    schema["$id"] = AGRICULTURAL_PERSISTENCE_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = AGRICULTURAL_PERSISTENCE_SCHEMA_VERSION
    return schema
