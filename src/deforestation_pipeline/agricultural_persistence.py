"""Persistencia agrícola post-cambio derivada sin interpolar meses faltantes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal
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
    AgriculturalEvidenceDocument,
    AgriculturalEvidenceObservation,
    canonical_payload_sha256,
)
from deforestation_pipeline.agricultural_visualization import (
    render_agricultural_persistence_evidence,
)
from deforestation_pipeline.local_runner import _publish_bundle
from deforestation_pipeline.schemas import NonEmptyString, Sha256Digest, StrictModel

AGRICULTURAL_PERSISTENCE_SCHEMA_VERSION: Final = "1.0.0"
AGRICULTURAL_PERSISTENCE_SCHEMA_ID: Final = (
    "urn:deforestation-pipeline:agricultural-persistence:1.0.0"
)
_RAW_DOCUMENT = "json/evidence/agricultural_evidence.json"
_RAW_METADATA = "json/evidence/agricultural_evidence_metadata.json"
_RAW_EVENTS = "json/evidence/agricultural_evidence.geojson"


class AgriculturalPersistenceConfig(StrictModel):
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
    def thresholds_are_coherent(self) -> AgriculturalPersistenceConfig:
        values = self.sensitivity_crop_observation_fractions
        if values != sorted(set(values)):
            raise ValueError("sensitivity thresholds deben ser únicos y ordenados")
        if self.primary_crop_observation_fraction not in values:
            raise ValueError("primary threshold debe estar en sensitivity thresholds")
        if self.minimum_qualifying_periods > self.minimum_valid_periods:
            raise ValueError("minimum qualifying periods supera minimum valid periods")
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
    nodata_area_ha: Annotated[float, Field(ge=0)]
    matched_scene_count: Annotated[int, Field(ge=0, strict=True)]
    raster_path: NonEmptyString


class AgriculturalPersistenceEvent(StrictModel):
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
    def status_matches_support(self) -> AgriculturalPersistenceEvent:
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


class AgriculturalPersistenceDocument(StrictModel):
    """Resultado de persistencia, incluyendo insuficiencia y desacuerdo explícitos."""

    schema_version: Literal["1.0.0"]
    analysis_id: UUID
    establishment_id: NonEmptyString
    created_at: datetime
    source_collection_bundle: SourceBundleReference
    persistence_rule: NonEmptyString
    primary_crop_observation_fraction: Annotated[float, Field(gt=0, le=1)]
    events: list[AgriculturalPersistenceEvent]

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


def load_agricultural_persistence_config(path: Path) -> AgriculturalPersistenceConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("agricultural_persistence_config_root_must_be_mapping")
    return AgriculturalPersistenceConfig.model_validate(payload)


def materialize_agricultural_persistence(
    *,
    source_collection_bundle: Path,
    output_root: Path,
    config_path: Path,
    created_at: datetime,
) -> Path:
    """Deriva soporte persistente por píxel desde múltiples períodos pos-onset."""
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
    analysis_id = uuid5(
        NAMESPACE_URL,
        "agricultural-persistence-v1:"
        + json.dumps(identity, sort_keys=True, separators=(",", ":")),
    )
    files: dict[str, bytes] = {}
    event_results: list[dict[str, Any]] = []
    observations: list[AgriculturalEvidenceObservation] = []
    visualizations: list[dict[str, Any]] = []
    for event in raw.events:
        result, raster_content, observation = _evaluate_event(
            event=event,
            raw=raw,
            bundle=bundle,
            manifest=manifest,
            source=source,
            geometry=geometries[event.event_id],
            config=config,
        )
        event_results.append(result)
        if raster_content is not None and result["support_raster_path"] is not None:
            files[result["support_raster_path"]] = raster_content
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

    persistence = AgriculturalPersistenceDocument.model_validate(
        {
            "schema_version": AGRICULTURAL_PERSISTENCE_SCHEMA_VERSION,
            "analysis_id": str(analysis_id),
            "establishment_id": raw.establishment_id,
            "created_at": created_at.isoformat(),
            "source_collection_bundle": source_reference,
            "persistence_rule": _rule_name(config),
            "primary_crop_observation_fraction": config.primary_crop_observation_fraction,
            "events": event_results,
        }
    )
    evidence = AgriculturalEvidenceDocument(
        schema_version="1.0.0",
        evidence_set_id=analysis_id,
        created_at=created_at,
        observations=observations,
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
                        item.persistence_status == "persistent_crop_support"
                        for item in persistence.events
                    ),
                    "attribution_generated": False,
                    "conversion_confirmed": False,
                }
            ),
        }
    )
    run_name = (
        f"{_safe_id(raw.establishment_id)}-agriculture-persistence-v1__"
        f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}__{str(analysis_id)[:8]}"
    )
    run_directory = output_root.resolve() / run_name
    _publish_bundle(
        run_directory=run_directory,
        files=files,
        manifest_metadata={
            "schema_version": AGRICULTURAL_PERSISTENCE_SCHEMA_VERSION,
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
    config: AgriculturalPersistenceConfig,
) -> tuple[dict[str, Any], bytes | None, AgriculturalEvidenceObservation | None]:
    rows = [row for row in raw.rows if row.event_id == event.event_id]
    windows = sorted({row.window_id for row in rows})
    if event.collection_status != "collected" or not windows:
        return _insufficient_event(event, "insufficient_collection"), None, None
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
        arrays, current = _read_four_bands(_artifact(bundle, manifest, row.raster_path))
        if current != profile:
            raise ValueError("agricultural_persistence_rasters_do_not_share_grid")
        valid_count, _, _, crop_fraction = arrays
        valid = (
            footprint_valid
            & np.isfinite(valid_count)
            & (valid_count != profile["nodata"])
            & (valid_count >= config.minimum_valid_observations_per_period)
        )
        fraction = np.where(valid, crop_fraction, 0).astype("float32")
        valid_stack.append(valid)
        fraction_stack.append(fraction)
        qualifying = valid & (fraction >= config.primary_crop_observation_fraction)
        valid_area = _weighted_area(valid, footprint, pixel_area_ha)
        qualifying_area = _weighted_area(qualifying, footprint, pixel_area_ha)
        period_support.append(
            {
                "window_id": window,
                "start_date": row.start_date,
                "end_date_exclusive": row.end_date_exclusive,
                "valid_area_ha": valid_area,
                "qualifying_crop_area_ha": qualifying_area,
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
    persistent = sufficient_observation & (qualifying_counts >= config.minimum_qualifying_periods)
    support = np.full(footprint.shape, config.support_raster_nodata, dtype="float32")
    support[footprint_valid] = 0.0
    support[persistent] = footprint[persistent].astype("float32")
    support_path = "tiffs/evidence/agricultural_persistence/" + (
        f"{_safe_id(event.event_id)}_persistent_crop_support.tif"
    )
    support_content = _write_single_band(support, profile, "persistent_crop_event_fraction")
    support_hash = hashlib.sha256(support_content).hexdigest()
    area = _weighted_area(persistent, footprint, pixel_area_ha)
    sensitivity = []
    for threshold in config.sensitivity_crop_observation_fractions:
        qualifying_at_threshold = (valid_cube & (fraction_cube >= threshold)).sum(axis=0)
        mask = sufficient_observation & (
            qualifying_at_threshold >= config.minimum_qualifying_periods
        )
        threshold_area = _weighted_area(mask, footprint, pixel_area_ha)
        sensitivity.append(
            {
                "crop_observation_fraction_threshold": threshold,
                "persistent_crop_area_ha": threshold_area,
                "persistent_event_fraction": min(1.0, threshold_area / event_area),
            }
        )
    period_has_valid = [item["valid_area_ha"] > 0 for item in period_support]
    period_qualifies = [item["qualifying_crop_area_ha"] > 0 for item in period_support]
    gap_count = sum(not value for value in period_has_valid)
    max_gap = _longest_run([not value for value in period_has_valid])
    consecutive_qualifying_by_pixel = _maximum_consecutive_true(primary_qualifying)
    if np.any(persistent):
        observed_duration = int(np.min(observed_duration_by_pixel[persistent]))
        valid_period_count = int(np.min(valid_counts[persistent]))
        qualifying_period_count = int(np.min(qualifying_counts[persistent]))
        max_consecutive = int(np.min(consecutive_qualifying_by_pixel[persistent]))
    else:
        observed_duration = int(np.max(observed_duration_by_pixel[footprint_valid]))
        valid_period_count = int(np.max(valid_counts[footprint_valid]))
        qualifying_period_count = int(np.max(qualifying_counts[footprint_valid]))
        max_consecutive = int(np.max(consecutive_qualifying_by_pixel[footprint_valid]))
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
    quality_flags.extend(
        [
            "single_source_no_cross_source_disagreement_assessment",
            "forest_recovery_requires_rf_attributor",
        ]
    )
    if not series_sufficient:
        status = "insufficient_series"
    elif area > 0:
        status = "persistent_crop_support"
    else:
        status = "not_persistent"
    result = {
        "event_id": event.event_id,
        "persistence_status": status,
        "estimated_onset_window_end": event.estimated_onset_window_end,
        "post_change_window_start": first.start_date,
        "post_change_window_end": last.end_date_exclusive - timedelta(days=1),
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
        "persistent_crop_area_ha": area,
        "persistent_event_fraction": min(1.0, area / event_area),
        "support_raster_path": support_path,
        "support_raster_sha256": support_hash,
        "sensitivity": sensitivity,
        "per_period_support": period_support,
        "quality_flags": list(dict.fromkeys(quality_flags)),
    }
    observation = None
    if status == "persistent_crop_support":
        observation = _observation(
            event=event,
            result=result,
            source=source,
            geometry=geometry,
            support=support,
            profile=profile,
            support_path=support_path,
            support_hash=support_hash,
            config=config,
            valid_period_count=valid_period_count,
            qualifying_period_count=qualifying_period_count,
            max_consecutive=max_consecutive,
            duration=observed_duration,
            event_area=event_area,
        )
    return result, support_content, observation


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
    config: AgriculturalPersistenceConfig,
    valid_period_count: int,
    qualifying_period_count: int,
    max_consecutive: int,
    duration: int,
    event_area: float,
) -> AgriculturalEvidenceObservation:
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


def _insufficient_event(event: Any, status: str) -> dict[str, Any]:
    return {
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


def _rule_name(config: AgriculturalPersistenceConfig) -> str:
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


def _persistence_csv(document: AgriculturalPersistenceDocument) -> bytes:
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
