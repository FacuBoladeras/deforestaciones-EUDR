"""Atribución post-cambio v1, separada de la detección de perturbaciones."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Final, Literal
from uuid import NAMESPACE_URL, uuid5

import matplotlib
import numpy as np
import rasterio
from matplotlib.patches import Patch
from matplotlib.patches import Polygon as PlotPolygon
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.io import MemoryFile
from rasterio.warp import reproject, transform_geom
from shapely.geometry import MultiPolygon, Polygon, shape

from deforestation_pipeline.agricultural_evidence import (
    AgriculturalEvidenceDocument,
    AgriculturalEvidencePolicy,
    EvaluatedAgriculturalEvidence,
    evaluate_agricultural_evidence,
)
from deforestation_pipeline.agricultural_persistence import AgriculturalPersistenceDocument
from deforestation_pipeline.agricultural_visualization import (
    render_likely_conversion_conjunction,
)

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

PostChangeUse = Literal[
    "agriculture_likely",
    "forest_recovery",
    "managed_harvest",
    "temporary_disturbance",
    "unknown",
]
DeclaredLandUse = Literal["unknown", "managed_forest_plantation"]

POST_CHANGE_ATTRIBUTION_SCHEMA_VERSION: Final = "3.0.0"
POST_CHANGE_ATTRIBUTION_BUNDLE_SCHEMA_VERSION: Final = "3.0.0"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
_YEARS: Final = (2020, 2021, 2022, 2023, 2024)
_CLASS_COLORS: Final = {
    "agriculture_likely": "#d73027",
    "forest_recovery": "#1a9850",
    "managed_harvest": "#fee08b",
    "temporary_disturbance": "#91bfdb",
    "unknown": "#969696",
}


@dataclass(frozen=True, slots=True)
class AttributionContext:
    declared_land_use: DeclaredLandUse = "unknown"
    declared_context_source: str = "not_provided"
    agricultural_use_evidence: dict[str, tuple[EvaluatedAgriculturalEvidence, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not self.declared_context_source.strip():
            raise ValueError("declared_context_source no puede estar vacío")


@dataclass(frozen=True, slots=True)
class AgriculturalAttributionSupport:
    """Área materializada que evita convertir una etiqueta en hectáreas."""

    persistent_agricultural_area_ha: float
    conjunctive_likely_area_ha: float
    persistent_support_raster_path: str
    conjunctive_support_raster_path: str
    source_bundle_manifest_sha256: str

    def __post_init__(self) -> None:
        if (
            self.persistent_agricultural_area_ha < 0
            or self.conjunctive_likely_area_ha < 0
            or self.conjunctive_likely_area_ha > self.persistent_agricultural_area_ha + 1e-9
        ):
            raise ValueError("áreas agrícolas de atribución inconsistentes")
        if not self.persistent_support_raster_path or not self.conjunctive_support_raster_path:
            raise ValueError("soporte agrícola requiere paths reversibles")
        if len(self.source_bundle_manifest_sha256) != 64:
            raise ValueError("source_bundle_manifest_sha256 inválido")


@dataclass(frozen=True, slots=True)
class EventTrajectory:
    years: tuple[int, ...]
    forest_fraction: tuple[float, ...]
    mean_hard_vote_fraction: tuple[float, ...]
    candidate_loss_fraction: tuple[float, ...]
    evaluable_pixel_count: tuple[int, ...]

    def __post_init__(self) -> None:
        lengths = {
            len(self.years),
            len(self.forest_fraction),
            len(self.mean_hard_vote_fraction),
            len(self.candidate_loss_fraction),
            len(self.evaluable_pixel_count),
        }
        if lengths != {len(self.years)} or len(self.years) < 2:
            raise ValueError("la trayectoria debe tener vectores no vacíos del mismo largo")
        if self.years != tuple(sorted(set(self.years))) or self.years[0] != 2020:
            raise ValueError("la trayectoria debe ser anual, única, ordenada y comenzar en 2020")
        for values in (
            self.forest_fraction,
            self.mean_hard_vote_fraction,
            self.candidate_loss_fraction,
        ):
            if any(value < 0 or value > 1 for value in values):
                raise ValueError("las fracciones de trayectoria deben estar en [0, 1]")
        if any(count < 0 for count in self.evaluable_pixel_count):
            raise ValueError("evaluable_pixel_count no puede ser negativo")


@dataclass(frozen=True, slots=True)
class AttributionRules:
    baseline_forest_fraction_minimum: float = 0.5
    candidate_loss_fraction_minimum: float = 0.1
    persistent_nonforest_fraction_maximum: float = 0.25
    persistent_nonforest_years: int = 2
    recovery_fraction_minimum: float = 0.5
    recovery_gain_minimum: float = 0.25


_DEFAULT_RULES: Final = AttributionRules()


def attribute_post_change_event(
    *,
    event: Mapping[str, Any],
    trajectory: EventTrajectory,
    context: AttributionContext,
    rules: AttributionRules = _DEFAULT_RULES,
    agricultural_support: AgriculturalAttributionSupport | None = None,
) -> dict[str, Any]:
    """Aplica gates conservadores; una pérdida RF sola nunca atribuye agricultura."""
    event_id = str(event.get("event_id", "")).strip()
    if not event_id:
        raise ValueError("event_id no puede estar vacío")
    if any(count == 0 for count in trajectory.evaluable_pixel_count):
        return _result(
            event_id=event_id,
            post_change_use="unknown",
            reason_codes=("insufficient_annual_rf_support",),
            gates=_gates(False, False, False, False, False, False),
            trajectory=trajectory,
            context=context,
            evidence=None,
            agricultural_support=agricultural_support,
        )

    baseline_forest = (
        event.get("primary_interpretation_domain") == "automated_forest"
        and trajectory.forest_fraction[0] >= rules.baseline_forest_fraction_minimum
    )
    post_loss = max(trajectory.candidate_loss_fraction[1:]) >= (
        rules.candidate_loss_fraction_minimum
    )
    persistent_event = event.get("event_type") == "persistent_disturbance_event"
    tail = trajectory.forest_fraction[-rules.persistent_nonforest_years :]
    persistent_nonforest = len(tail) == rules.persistent_nonforest_years and all(
        value <= rules.persistent_nonforest_fraction_maximum for value in tail
    )
    persistence = bool(persistent_event and persistent_nonforest)
    area_defensible = bool(event.get("area_threshold_met")) and float(event.get("area_ha", 0.0)) > 0
    trough_index = min(
        range(1, len(trajectory.years)), key=lambda index: trajectory.forest_fraction[index]
    )
    later = trajectory.forest_fraction[trough_index + 1 :]
    recovery = bool(
        later
        and max(later) >= rules.recovery_fraction_minimum
        and max(later) - trajectory.forest_fraction[trough_index] >= rules.recovery_gain_minimum
    )
    declared_managed = context.declared_land_use == "managed_forest_plantation"
    evidence = context.agricultural_use_evidence.get(event_id, ())
    eligible_evidence = tuple(item for item in evidence if item.assessment.decision_eligible)
    agriculture_supported = bool(
        eligible_evidence
        and agricultural_support is not None
        and agricultural_support.persistent_agricultural_area_ha > 0
        and agricultural_support.conjunctive_likely_area_ha > 0
    )
    alternative_absent = not recovery and not declared_managed
    spatial_area_defensible = bool(
        area_defensible
        and agricultural_support is not None
        and agricultural_support.conjunctive_likely_area_ha > 0
    )

    gates = _gates(
        baseline_forest,
        post_loss,
        persistence,
        agriculture_supported,
        spatial_area_defensible,
        alternative_absent,
    )
    reason_codes: list[str] = []
    if declared_managed:
        reason_codes.append("declared_managed_forest_context")
    if recovery:
        reason_codes.append("forest_recovery_after_trough")
    if not evidence:
        reason_codes.append("agricultural_use_evidence_missing")
    elif not eligible_evidence:
        reason_codes.append("agricultural_use_evidence_not_policy_eligible")
    elif agricultural_support is None:
        reason_codes.append("persistent_agricultural_spatial_support_missing")
    elif agricultural_support.conjunctive_likely_area_ha <= 0:
        reason_codes.append("conjunctive_likely_area_empty")
    else:
        reason_codes.extend(
            f"policy_eligible_{item.observation.land_use}_evidence" for item in eligible_evidence
        )

    post_change_use: PostChangeUse
    if declared_managed and post_loss and recovery:
        post_change_use = "managed_harvest"
    elif recovery:
        post_change_use = "forest_recovery" if persistent_event else "temporary_disturbance"
    elif all(gates.values()):
        post_change_use = "agriculture_likely"
    else:
        post_change_use = "unknown"

    if not reason_codes:
        reason_codes.append("no_attribution_rule_satisfied")
    return _result(
        event_id=event_id,
        post_change_use=post_change_use,
        reason_codes=tuple(reason_codes),
        gates=gates,
        trajectory=trajectory,
        context=context,
        evidence=evidence,
        agricultural_support=agricultural_support,
    )


def materialize_post_change_attribution(
    *,
    source_event_bundle: Path,
    source_rf_bundle: Path,
    output_root: Path,
    establishment_id: str,
    context: AttributionContext,
    created_at: datetime,
    rules: AttributionRules = _DEFAULT_RULES,
    source_agricultural_bundle: Path | None = None,
    agricultural_policy: AgriculturalEvidencePolicy | None = None,
) -> Path:
    """Cruza eventos vectoriales con trayectorias RF y publica un bundle atómico."""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at debe incluir zona horaria")
    if not establishment_id.strip():
        raise ValueError("establishment_id no puede estar vacío")
    event_bundle, event_manifest = _load_source_bundle(source_event_bundle)
    rf_bundle, rf_manifest = _load_source_bundle(source_rf_bundle)
    if event_manifest.get("input_sha256") != rf_manifest.get("input_sha256"):
        raise ValueError("source bundles input_sha256 no coincide")

    events_path = _manifested_path(
        event_bundle,
        event_manifest,
        "json/evidence/disturbance_events.geojson",
    )
    event_collection = json.loads(events_path.read_text(encoding="utf-8"))
    features = event_collection.get("features") if isinstance(event_collection, dict) else None
    if not isinstance(features, list):
        raise ValueError("disturbance_events.geojson no es un FeatureCollection válido")
    _validate_disjoint_event_geometries(features)
    event_ids = {
        str(feature.get("properties", {}).get("event_id", ""))
        for feature in features
        if isinstance(feature, dict) and isinstance(feature.get("properties"), dict)
    }
    agricultural_bundle: Path | None = None
    agricultural_manifest: dict[str, Any] | None = None
    persistence_by_event: dict[str, Any] = {}
    effective_context = context
    if source_agricultural_bundle is not None:
        if agricultural_policy is None:
            raise ValueError("agricultural policy requerida para bundle persistente")
        if context.agricultural_use_evidence:
            raise ValueError("no mezclar evidencia agrícola caller con bundle persistente")
        agricultural_bundle, agricultural_manifest = _load_source_bundle(source_agricultural_bundle)
        if agricultural_manifest.get("input_sha256") != event_manifest.get("input_sha256"):
            raise ValueError("agricultural bundle input_sha256 no coincide")
        persistence = AgriculturalPersistenceDocument.model_validate_json(
            _manifested_path(
                agricultural_bundle,
                agricultural_manifest,
                "json/evidence/agricultural_persistence.json",
            ).read_text(encoding="utf-8")
        )
        evidence_document = AgriculturalEvidenceDocument.model_validate_json(
            _manifested_path(
                agricultural_bundle,
                agricultural_manifest,
                "json/evidence/agricultural_evidence_persistent.json",
            ).read_text(encoding="utf-8")
        )
        if persistence.analysis_id != evidence_document.evidence_set_id:
            raise ValueError("persistence and evidence analysis identity mismatch")
        _validate_persistence_evidence_consistency(persistence, evidence_document)
        upstream_event = persistence.source_collection_bundle.source_event_bundle
        if (
            upstream_event.event_artifact_path != "json/evidence/disturbance_events.geojson"
            or upstream_event.event_artifact_sha256
            != hashlib.sha256(events_path.read_bytes()).hexdigest()
        ):
            raise ValueError(
                "agricultural source event artifact does not match current event artifact"
            )
        if str(upstream_event.analysis_id) != str(event_manifest.get("analysis_id")):
            raise ValueError(
                "agricultural source event analysis_id does not match current event analysis_id"
            )
        persistence_by_event = {item.event_id: item for item in persistence.events}
        evaluated = evaluate_agricultural_evidence(evidence_document, agricultural_policy)
        effective_context = AttributionContext(
            declared_land_use=context.declared_land_use,
            declared_context_source=context.declared_context_source,
            agricultural_use_evidence=evaluated,
        )
    unknown_evidence_events = set(effective_context.agricultural_use_evidence) - event_ids
    if unknown_evidence_events:
        raise ValueError(
            "agricultural evidence referencia eventos inexistentes:"
            + ",".join(sorted(unknown_evidence_events))
        )

    raster_paths: dict[tuple[str, int], Path] = {}
    rf_metadata_path = _manifested_path(
        rf_bundle,
        rf_manifest,
        "json/evidence/rf_forest_deltas_metadata.json",
    )
    rf_metadata = json.loads(rf_metadata_path.read_text(encoding="utf-8"))
    if rf_metadata.get("score_semantics") != "uncalibrated_binary_tree_vote_fraction":
        raise ValueError("RF delta bundle no usa fracción de votos binarios reproducible")
    for year in _YEARS:
        for kind in ("class", "vote_fraction"):
            raster_paths[(kind, year)] = _manifested_path(
                rf_bundle,
                rf_manifest,
                f"tiffs/evidence/rf_forest_{kind}_{year}.tif",
            )
        if year > 2020:
            raster_paths[("transition", year)] = _manifested_path(
                rf_bundle,
                rf_manifest,
                f"tiffs/evidence/rf_forest_transition_{year}_vs_2020.tif",
            )

    arrays, raster_contract = _read_rf_rasters(raster_paths)
    results: list[dict[str, Any]] = []
    output_features: list[dict[str, Any]] = []
    conjunction_files: dict[str, bytes] = {}
    conjunction_visualizations: list[dict[str, Any]] = []
    agriculture_manifest_sha = (
        hashlib.sha256((agricultural_bundle / "json/run/manifest.json").read_bytes()).hexdigest()
        if agricultural_bundle is not None
        else None
    )
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise ValueError("evento GeoJSON inválido")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("evento sin geometría")
        trajectory = _event_trajectory(geometry, arrays, raster_contract)
        support = None
        conjunction_for_visualization: tuple[str, bytes] | None = None
        persistence_event = persistence_by_event.get(str(feature["properties"]["event_id"]))
        if (
            persistence_event is not None
            and agricultural_bundle is not None
            and agricultural_manifest is not None
            and agriculture_manifest_sha is not None
            and persistence_event.support_raster_path is not None
        ):
            persistent_path = _manifested_path(
                agricultural_bundle,
                agricultural_manifest,
                persistence_event.support_raster_path,
            )
            if hashlib.sha256(persistent_path.read_bytes()).hexdigest() != (
                persistence_event.support_raster_sha256
            ):
                raise ValueError("agricultural persistent support hash mismatch")
            conjunction_relative = (
                "tiffs/evidence/likely_conversion/"
                f"{_slug(str(feature['properties']['event_id']))}_conjunctive_support.tif"
            )
            persistent_area, conjunctive_area, conjunction_content = (
                _materialize_conjunctive_support(
                    persistent_path=persistent_path,
                    arrays=arrays,
                    rf_contract=raster_contract,
                    rules=rules,
                )
            )
            declared_persistent_area = float(persistence_event.persistent_crop_area_ha)
            area_tolerance = max(0.005, declared_persistent_area * 1e-6)
            if not math.isclose(
                persistent_area,
                declared_persistent_area,
                abs_tol=area_tolerance,
            ):
                raise ValueError("persistent raster area does not match persistence contract")
            conjunction_files[conjunction_relative] = conjunction_content
            conjunction_for_visualization = (conjunction_relative, conjunction_content)
            support = AgriculturalAttributionSupport(
                persistent_agricultural_area_ha=persistent_area,
                conjunctive_likely_area_ha=conjunctive_area,
                persistent_support_raster_path=persistence_event.support_raster_path,
                conjunctive_support_raster_path=conjunction_relative,
                source_bundle_manifest_sha256=agriculture_manifest_sha,
            )
        result = attribute_post_change_event(
            event=dict(feature["properties"]),
            trajectory=trajectory,
            context=effective_context,
            rules=rules,
            agricultural_support=support,
        )
        result.update(
            {
                "area_ha": feature["properties"].get("area_ha"),
                "area_threshold_met": feature["properties"].get("area_threshold_met"),
                "estimated_onset_period_id": feature["properties"].get("estimated_onset_period_id"),
                "confidence": None,
                "confidence_semantics": "not_calibrated_in_v1",
                "quality_flags": (
                    ["insufficient_annual_rf_support"]
                    if 0 in trajectory.evaluable_pixel_count
                    else []
                ),
            }
        )
        results.append(result)
        if conjunction_for_visualization is not None:
            conjunction_relative, conjunction_content = conjunction_for_visualization
            visualization = render_likely_conversion_conjunction(
                event=result,
                raster_content=conjunction_content,
                source_raster_path=conjunction_relative,
            )
            if visualization is not None:
                conjunction_files[visualization.path] = visualization.content
                conjunction_visualizations.append(visualization.record)
        output_features.append(
            {"type": "Feature", "geometry": geometry, "properties": dict(result)}
        )

    source_identity = {
        "events": _source_identity(event_bundle, event_manifest),
        "rf_deltas": _source_identity(rf_bundle, rf_manifest),
    }
    if agricultural_bundle is not None and agricultural_manifest is not None:
        source_identity["agricultural_persistence"] = _source_identity(
            agricultural_bundle, agricultural_manifest
        )
    rules_payload = _rules_payload(rules)
    context_payload = _context_payload(effective_context)
    code_payload = _code_identity()
    identity_payload = json.dumps(
        {
            "input_sha256": event_manifest.get("input_sha256"),
            "sources": source_identity,
            "rules": rules_payload,
            "context": context_payload,
            "code": code_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    analysis_id = uuid5(NAMESPACE_URL, f"post-change-attribution-v1:{identity_payload}")
    run_name = (
        f"{_slug(establishment_id)}-attribution-v1__"
        f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}__{str(analysis_id)[:8]}"
    )
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    final = output_root / run_name
    staging = output_root / f".{run_name}.staging"
    if final.exists() or staging.exists():
        raise FileExistsError(f"post_change_attribution_run_collision:{run_name}")
    staging.mkdir()
    try:
        counts = Counter(str(result["post_change_use"]) for result in results)
        conversion_likely_count = sum(
            result["automatic_status"] == "conversion_likely" for result in results
        )
        likely_conversion_area_ha = float(
            sum(float(result["likely_conversion_area_ha"]) for result in results)
        )
        payload = {
            "schema_version": POST_CHANGE_ATTRIBUTION_SCHEMA_VERSION,
            "analysis_id": str(analysis_id),
            "establishment_id": establishment_id,
            "created_at": created_at.isoformat(),
            "status": "review_required",
            "automatic_final_assessment_generated": False,
            "conversion_confirmed_count": 0,
            "conversion_likely_count": conversion_likely_count,
            "likely_conversion_area_ha": likely_conversion_area_ha,
            "counts_by_post_change_use": dict(sorted(counts.items())),
            "events": results,
        }
        metadata = {
            "schema_version": POST_CHANGE_ATTRIBUTION_SCHEMA_VERSION,
            "method": "deterministic_post_change_attributor_v1",
            "source_bundles": source_identity,
            "rules": rules_payload,
            "context": context_payload,
            "code": code_payload,
            "rf_score_semantics": "uncalibrated_binary_tree_vote_fraction",
            "agriculture_gate": (
                "requires policy-eligible persistent crop, pasture or livestock infrastructure "
                "evidence"
            ),
            "limitations": [
                "RF forest loss alone does not identify agricultural use.",
                "Declared managed-forest context is an alternative explanation, "
                "not independent evidence.",
                "The automatic attributor never emits conversion_confirmed.",
            ],
            "visualizations": conjunction_visualizations,
        }
        files = {
            "tables/evidence/post_change_attribution.csv": _csv_bytes(results),
            "json/evidence/post_change_attribution.json": _json_bytes(payload),
            "json/evidence/post_change_attribution.geojson": _json_bytes(
                {"type": "FeatureCollection", "features": output_features}
            ),
            "json/evidence/post_change_attribution_metadata.json": _json_bytes(metadata),
            "figures/evidence/post_change_attribution.png": _render_figure(output_features),
            **conjunction_files,
        }
        summary = {
            **payload,
            "events": [],
            "attribution_generated": True,
            "source_bundles": source_identity,
            "code": code_payload,
            "assets": sorted(files),
        }
        files["json/run/summary.json"] = _json_bytes(summary)
        artifacts = []
        for relative, content in files.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            artifacts.append(
                {
                    "path": relative,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "media_type": (
                        "image/png"
                        if relative.lower().endswith(".png")
                        else "image/tiff"
                        if relative.lower().endswith((".tif", ".tiff"))
                        else "application/json"
                        if relative.lower().endswith((".json", ".geojson"))
                        else "text/csv"
                        if relative.lower().endswith(".csv")
                        else "application/octet-stream"
                    ),
                }
            )
        manifest = {
            "schema_version": POST_CHANGE_ATTRIBUTION_BUNDLE_SCHEMA_VERSION,
            "analysis_id": str(analysis_id),
            "establishment_id": establishment_id,
            "created_at": created_at.isoformat(),
            "input_sha256": event_manifest.get("input_sha256"),
            "source_bundles": source_identity,
            "code": code_payload,
            "rules_sha256": hashlib.sha256(
                json.dumps(rules_payload, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "context_sha256": hashlib.sha256(
                json.dumps(context_payload, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "visualizations": conjunction_visualizations,
            "artifacts": artifacts,
            "manifest_self_excluded": True,
        }
        manifest_path = staging / "json/run/manifest.json"
        manifest_path.write_bytes(_json_bytes(manifest))
        staging.rename(final)
        return final
    except Exception:
        if staging.exists():
            import shutil

            shutil.rmtree(staging)
        raise


def _gates(
    baseline_forest: bool,
    post_cutoff_loss: bool,
    persistence: bool,
    agricultural_use: bool,
    area_geometry: bool,
    alternative_absent: bool,
) -> dict[str, bool]:
    gates = {
        "forest_at_cutoff": baseline_forest,
        "post_cutoff_loss": post_cutoff_loss,
        "persistent_change": persistence,
        "agricultural_or_livestock_post_use": agricultural_use,
        "defensible_area_and_geometry": area_geometry,
        "strong_alternative_explanation_absent": alternative_absent,
    }
    gates["all_conversion_likely_gates"] = all(gates.values())
    return gates


def _result(
    *,
    event_id: str,
    post_change_use: PostChangeUse,
    reason_codes: tuple[str, ...],
    gates: dict[str, bool],
    trajectory: EventTrajectory,
    context: AttributionContext,
    evidence: tuple[EvaluatedAgriculturalEvidence, ...] | None,
    agricultural_support: AgriculturalAttributionSupport | None,
) -> dict[str, Any]:
    conversion_likely = post_change_use == "agriculture_likely" and gates.get(
        "all_conversion_likely_gates", False
    )
    reasons_for = [
        f"gate_passed:{name}"
        for name, passed in gates.items()
        if passed and name != "all_conversion_likely_gates"
    ]
    reasons_against = [
        f"gate_failed:{name}"
        for name, passed in gates.items()
        if not passed and name != "all_conversion_likely_gates"
    ]
    if agricultural_support is None:
        reasons_against.append("persistent_agricultural_spatial_support_missing")
    if not gates.get("strong_alternative_explanation_absent", False):
        reasons_against.append("strong_alternative_explanation_present")
    if conversion_likely:
        reasons_for.append("all_conjunctive_conversion_gates_passed")
    likely_area = (
        agricultural_support.conjunctive_likely_area_ha
        if conversion_likely and agricultural_support is not None
        else 0.0
    )
    return {
        "schema_version": POST_CHANGE_ATTRIBUTION_SCHEMA_VERSION,
        "event_id": event_id,
        "post_change_use": post_change_use,
        "automatic_status": "conversion_likely" if conversion_likely else "review_required",
        "conversion_confirmed": False,
        "human_review_required": True,
        "gates": gates,
        "reason_codes": list(reason_codes),
        "reasons_for": reasons_for,
        "reasons_against": list(dict.fromkeys(reasons_against)),
        "persistent_agricultural_area_ha": (
            agricultural_support.persistent_agricultural_area_ha
            if agricultural_support is not None
            else 0.0
        ),
        "likely_conversion_area_ha": likely_area,
        "agricultural_spatial_support": (
            None
            if agricultural_support is None
            else {
                "persistent_support_raster_path": (
                    agricultural_support.persistent_support_raster_path
                ),
                "conjunctive_support_raster_path": (
                    agricultural_support.conjunctive_support_raster_path
                ),
                "source_bundle_manifest_sha256": agricultural_support.source_bundle_manifest_sha256,
            }
        ),
        "trajectory": {
            "years": list(trajectory.years),
            "forest_fraction": list(trajectory.forest_fraction),
            "mean_hard_vote_fraction": list(trajectory.mean_hard_vote_fraction),
            "candidate_loss_fraction": list(trajectory.candidate_loss_fraction),
            "evaluable_pixel_count": list(trajectory.evaluable_pixel_count),
        },
        "context": {
            "declared_land_use": context.declared_land_use,
            "declared_context_source": context.declared_context_source,
            "is_independent_evidence": False,
        },
        "agricultural_use_evidence": [item.model_dump(mode="json") for item in (evidence or ())],
    }


def _load_source_bundle(bundle: Path) -> tuple[Path, dict[str, Any]]:
    resolved = bundle.resolve()
    manifest_path = resolved / "json/run/manifest.json"
    if not resolved.is_dir() or not manifest_path.is_file():
        raise ValueError("source bundle no contiene json/run/manifest.json")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source bundle manifest inválido") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        raise ValueError("source bundle manifest no contiene artifacts")
    seen: set[str] = set()
    for artifact in payload["artifacts"]:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise ValueError("source bundle artifact inválido")
        relative = artifact["path"]
        if relative in seen:
            raise ValueError("source bundle contiene artifact path duplicado")
        seen.add(relative)
        path = _safe_child(resolved, relative)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != artifact.get("size_bytes")
            or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.get("sha256")
        ):
            raise ValueError(f"source bundle artifact integrity mismatch:{relative}")
    return resolved, payload


def _manifested_path(bundle: Path, manifest: Mapping[str, Any], relative: str) -> Path:
    records = [
        item
        for item in manifest.get("artifacts", [])
        if isinstance(item, Mapping) and item.get("path") == relative
    ]
    if len(records) != 1:
        raise ValueError(f"source bundle no manifiesta exactamente una vez:{relative}")
    return _safe_child(bundle, relative)


def _safe_child(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("source bundle contiene path inseguro")
    resolved = (root / candidate).resolve()
    if root not in resolved.parents:
        raise ValueError("source bundle path escapa del root")
    return resolved


def _read_rf_rasters(
    paths: Mapping[tuple[str, int], Path],
) -> tuple[dict[tuple[str, int], np.ndarray], dict[str, Any]]:
    arrays: dict[tuple[str, int], np.ndarray] = {}
    contract: dict[str, Any] | None = None
    for key, path in paths.items():
        with rasterio.open(path) as dataset:
            current = {
                "width": dataset.width,
                "height": dataset.height,
                "crs": dataset.crs,
                "transform": dataset.transform,
                "nodata": dataset.nodata,
            }
            if contract is None:
                if dataset.crs is None or dataset.nodata is None:
                    raise ValueError("RF raster carece de CRS o nodata")
                contract = current
            elif current != contract:
                raise ValueError("RF rasters no comparten grilla, CRS y nodata")
            arrays[key] = dataset.read()[0].copy()
    if contract is None:
        raise ValueError("no se encontraron RF rasters")
    return arrays, contract


def _validate_disjoint_event_geometries(features: list[Any]) -> None:
    """La suma por evento sólo es segura si los componentes no se superponen."""
    geometries: list[tuple[str, Polygon | MultiPolygon]] = []
    event_ids: set[str] = set()
    for feature in features:
        if not isinstance(feature, Mapping) or not isinstance(feature.get("geometry"), Mapping):
            raise ValueError("evento GeoJSON inválido")
        properties = feature.get("properties")
        event_id = str(properties.get("event_id", "")) if isinstance(properties, Mapping) else ""
        geometry = shape(dict(feature["geometry"]))
        if (
            not event_id
            or not isinstance(geometry, (Polygon, MultiPolygon))
            or geometry.is_empty
            or not geometry.is_valid
        ):
            raise ValueError("evento GeoJSON requiere geometría poligonal válida")
        if event_id in event_ids:
            raise ValueError(f"event_id duplicado:{event_id}")
        event_ids.add(event_id)
        for other_id, other in geometries:
            if geometry.intersection(other).area > 1e-15:
                raise ValueError(f"event geometries overlap:{other_id}:{event_id}")
        geometries.append((event_id, geometry))


def _validate_persistence_evidence_consistency(
    persistence: AgriculturalPersistenceDocument,
    evidence: AgriculturalEvidenceDocument,
) -> None:
    """Impide combinar un raster persistente con una observación ajena."""
    observations_by_event: dict[str, list[Any]] = {}
    for observation in evidence.observations:
        observations_by_event.setdefault(observation.event_id, []).append(observation)
    persistence_by_event = {event.event_id: event for event in persistence.events}
    unknown = set(observations_by_event) - set(persistence_by_event)
    if unknown:
        raise ValueError(
            "persistent evidence references unknown persistence events:" + ",".join(sorted(unknown))
        )
    for event_id, event in persistence_by_event.items():
        observations = observations_by_event.get(event_id, [])
        if event.persistence_status != "persistent_crop_support":
            if observations:
                raise ValueError("nonpersistent event cannot emit agricultural evidence")
            continue
        if len(observations) != 1:
            raise ValueError("persistent event requires exactly one agricultural observation")
        observation = observations[0]
        if (
            observation.artifact_sha256 != event.support_raster_sha256
            or observation.source.asset != event.support_raster_path
        ):
            raise ValueError("persistent observation does not reference event support raster")
        tolerance = max(0.005, float(event.persistent_crop_area_ha) * 1e-6)
        if not math.isclose(
            observation.spatial_support.attributed_area_ha,
            event.persistent_crop_area_ha,
            abs_tol=tolerance,
        ):
            raise ValueError("persistent observation area does not match event support area")


def _materialize_conjunctive_support(
    *,
    persistent_path: Path,
    arrays: Mapping[tuple[str, int], np.ndarray],
    rf_contract: Mapping[str, Any],
    rules: AttributionRules,
) -> tuple[float, float, bytes]:
    """Cruza soporte agrícola con bosque 2020, pérdida y no-bosque persistente."""
    with rasterio.open(persistent_path) as dataset:
        if dataset.count != 1 or dataset.crs is None or dataset.nodata is None:
            raise ValueError("agricultural persistent support raster inválido")
        crs = CRS.from_user_input(dataset.crs)
        if not crs.is_projected:
            raise ValueError("agricultural persistent support requiere CRS métrico")
        units = {axis.unit_name.lower() for axis in crs.axis_info if axis.unit_name}
        if not units or not units <= {"metre", "meter"}:
            raise ValueError("agricultural persistent support requiere unidades métricas")
        support = dataset.read()[0].astype("float32", copy=True)
        profile = {
            "width": dataset.width,
            "height": dataset.height,
            "crs": dataset.crs,
            "transform": dataset.transform,
            "nodata": float(dataset.nodata),
        }
    if np.any((support != profile["nodata"]) & ((support < 0) | (support > 1))):
        raise ValueError("agricultural persistent support fuera de [0,1]")
    rf_nodata = rf_contract["nodata"]
    baseline = arrays[("class", 2020)]
    valid = baseline != rf_nodata
    forest_at_cutoff = valid & (baseline == 1)
    post_loss = np.zeros(baseline.shape, dtype=bool)
    for year in _YEARS[1:]:
        transition = arrays[("transition", year)]
        post_loss |= (transition != rf_nodata) & (transition == 3)
    tail_years = _YEARS[-rules.persistent_nonforest_years :]
    persistent_nonforest = np.ones(baseline.shape, dtype=bool)
    for year in tail_years:
        classes = arrays[("class", year)]
        persistent_nonforest &= (classes != rf_nodata) & (classes == 0)
    rf_conjunction = (forest_at_cutoff & post_loss & persistent_nonforest).astype("uint8")
    projected = np.zeros(support.shape, dtype="uint8")
    reproject(
        source=rf_conjunction,
        destination=projected,
        src_transform=rf_contract["transform"],
        src_crs=rf_contract["crs"],
        dst_transform=profile["transform"],
        dst_crs=profile["crs"],
        src_nodata=0,
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    inside = support != profile["nodata"]
    persistent_area_values = np.where(inside & (support > 0), support, 0.0)
    conjunction = np.full(support.shape, profile["nodata"], dtype="float32")
    conjunction[inside] = persistent_area_values[inside] * projected[inside]
    pixel_area_ha = abs(float(profile["transform"].a * profile["transform"].e)) / 10000.0
    persistent_area = float(np.sum(persistent_area_values, dtype=np.float64) * pixel_area_ha)
    conjunctive_area = float(
        np.sum(conjunction[inside & (conjunction > 0)], dtype=np.float64) * pixel_area_ha
    )
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
        ) as output:
            output.write(conjunction, 1)
            output.set_band_description(1, "likely_conversion_conjunctive_event_fraction")
        content = memory.read()
    return persistent_area, conjunctive_area, content


def _event_trajectory(
    geometry: Mapping[str, Any],
    arrays: Mapping[tuple[str, int], np.ndarray],
    contract: Mapping[str, Any],
) -> EventTrajectory:
    raster_geometry = transform_geom("EPSG:4326", contract["crs"], dict(geometry))
    inside = geometry_mask(
        [raster_geometry],
        out_shape=(contract["height"], contract["width"]),
        transform=contract["transform"],
        invert=True,
        all_touched=False,
    )
    nodata = contract["nodata"]
    forests: list[float] = []
    votes: list[float] = []
    losses: list[float] = []
    counts: list[int] = []
    for year in _YEARS:
        classes = arrays[("class", year)]
        scores = arrays[("vote_fraction", year)]
        evaluable = inside & (classes != nodata) & (scores != nodata) & np.isfinite(scores)
        count = int(np.count_nonzero(evaluable))
        counts.append(count)
        if count == 0:
            forests.append(0.0)
            votes.append(0.0)
            losses.append(0.0)
            continue
        forests.append(float(np.mean(classes[evaluable] == 1)))
        votes.append(float(np.mean(scores[evaluable])))
        if year == 2020:
            losses.append(0.0)
        else:
            transitions = arrays[("transition", year)]
            losses.append(float(np.mean(transitions[evaluable] == 3)))
    return EventTrajectory(
        years=_YEARS,
        forest_fraction=tuple(forests),
        mean_hard_vote_fraction=tuple(votes),
        candidate_loss_fraction=tuple(losses),
        evaluable_pixel_count=tuple(counts),
    )


def _source_identity(bundle: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    manifest_path = bundle / "json/run/manifest.json"
    return {
        "analysis_id": manifest.get("analysis_id"),
        "created_at": manifest.get("created_at"),
        "input_sha256": manifest.get("input_sha256"),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "bundle_name": bundle.name,
    }


def _code_identity() -> dict[str, str]:
    module = Path(__file__).resolve()
    schema = PROJECT_ROOT / "data/schemas/post-change-attribution-v3.0.0.json"
    if not schema.is_file():
        raise ValueError("post-change attribution schema file no existe")
    return {
        "module_path": "src/deforestation_pipeline/post_change_attribution.py",
        "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "schema_path": "data/schemas/post-change-attribution-v3.0.0.json",
        "schema_sha256": hashlib.sha256(schema.read_bytes()).hexdigest(),
    }


def _rules_payload(rules: AttributionRules) -> dict[str, object]:
    return {
        "baseline_forest_fraction_minimum": rules.baseline_forest_fraction_minimum,
        "candidate_loss_fraction_minimum": rules.candidate_loss_fraction_minimum,
        "persistent_nonforest_fraction_maximum": rules.persistent_nonforest_fraction_maximum,
        "persistent_nonforest_years": rules.persistent_nonforest_years,
        "recovery_fraction_minimum": rules.recovery_fraction_minimum,
        "recovery_gain_minimum": rules.recovery_gain_minimum,
    }


def _context_payload(context: AttributionContext) -> dict[str, object]:
    return {
        "declared_land_use": context.declared_land_use,
        "declared_context_source": context.declared_context_source,
        "declared_context_is_independent_evidence": False,
        "agricultural_use_evidence": {
            event_id: [item.model_dump(mode="json") for item in evidence]
            for event_id, evidence in sorted(context.agricultural_use_evidence.items())
        },
    }


def _csv_bytes(results: list[dict[str, Any]]) -> bytes:
    fields = (
        "event_id",
        "post_change_use",
        "automatic_status",
        "conversion_confirmed",
        "human_review_required",
        "area_ha",
        "area_threshold_met",
        "estimated_onset_period_id",
        "confidence",
        "confidence_semantics",
        "reason_codes",
        "reasons_for",
        "reasons_against",
        "persistent_agricultural_area_ha",
        "likely_conversion_area_ha",
        "quality_flags",
        "gates",
        "trajectory",
        "context",
        "agricultural_use_evidence",
    )
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for result in results:
        row = {key: result.get(key) for key in fields}
        for key in (
            "reason_codes",
            "reasons_for",
            "reasons_against",
            "quality_flags",
            "gates",
            "trajectory",
            "context",
            "agricultural_use_evidence",
        ):
            row[key] = json.dumps(row[key], ensure_ascii=False, sort_keys=True)
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def _render_figure(features: list[dict[str, Any]]) -> bytes:
    figure, axis = plt.subplots(figsize=(9, 7), constrained_layout=True)
    for feature in features:
        geometry = shape(feature["geometry"])
        label = str(feature["properties"]["post_change_use"])
        polygons = geometry.geoms if isinstance(geometry, MultiPolygon) else (geometry,)
        for polygon in polygons:
            if not isinstance(polygon, Polygon):
                continue
            axis.add_patch(
                PlotPolygon(
                    np.asarray(polygon.exterior.coords),
                    closed=True,
                    facecolor=_CLASS_COLORS[label],
                    edgecolor="black",
                    linewidth=0.5,
                    alpha=0.8,
                )
            )
    axis.autoscale_view()
    axis.set_aspect("equal", adjustable="datalim")
    axis.set_xlabel("Longitud (WGS 84)")
    axis.set_ylabel("Latitud (WGS 84)")
    axis.set_title("Atribución post-cambio v1 — requiere revisión humana")
    present = sorted({str(feature["properties"]["post_change_use"]) for feature in features})
    if present:
        axis.legend(
            handles=[
                Patch(facecolor=_CLASS_COLORS[label], edgecolor="black", label=label)
                for label in present
            ],
            loc="best",
        )
    axis.text(
        0.01,
        0.01,
        "RF 30 m · no equivale a confirmación de deforestación",
        transform=axis.transAxes,
        fontsize=8,
        bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none"},
    )
    output = BytesIO()
    figure.savefig(output, format="png", dpi=160)
    plt.close(figure)
    return output.getvalue()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _slug(value: str) -> str:
    slug = "-".join(part for part in value.lower().replace("_", "-").split("-") if part)
    safe = "".join(character for character in slug if character.isalnum() or character == "-")
    if not safe:
        raise ValueError("establishment_id no contiene caracteres seguros")
    return safe[:80]
