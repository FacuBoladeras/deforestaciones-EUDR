"""Atribución post-cambio v0, separada de la detección de perturbaciones."""

from __future__ import annotations

import csv
import hashlib
import json
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
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom
from shapely.geometry import MultiPolygon, Polygon, shape

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

PostChangeUse = Literal[
    "agriculture_likely",
    "forest_recovery",
    "managed_harvest",
    "temporary_disturbance",
    "unknown",
]
AgriculturalLandUse = Literal["crop", "pasture", "livestock_infrastructure"]
DeclaredLandUse = Literal["unknown", "managed_forest_plantation"]

POST_CHANGE_ATTRIBUTION_SCHEMA_VERSION: Final = "1.0.0"
POST_CHANGE_ATTRIBUTION_BUNDLE_SCHEMA_VERSION: Final = "1.0.0"
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
class AgriculturalUseEvidence:
    land_use: AgriculturalLandUse
    source: str
    independent: bool

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("agricultural use evidence source no puede estar vacío")


@dataclass(frozen=True, slots=True)
class AttributionContext:
    declared_land_use: DeclaredLandUse = "unknown"
    declared_context_source: str = "not_provided"
    agricultural_use_evidence: dict[str, AgriculturalUseEvidence] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.declared_context_source.strip():
            raise ValueError("declared_context_source no puede estar vacío")


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
    evidence = context.agricultural_use_evidence.get(event_id)
    agriculture_supported = bool(evidence is not None and evidence.independent)
    alternative_absent = not recovery and not declared_managed

    gates = _gates(
        baseline_forest,
        post_loss,
        persistence,
        agriculture_supported,
        area_defensible,
        alternative_absent,
    )
    reason_codes: list[str] = []
    if declared_managed:
        reason_codes.append("declared_managed_forest_context")
    if recovery:
        reason_codes.append("forest_recovery_after_trough")
    if evidence is None:
        reason_codes.append("agricultural_use_evidence_missing")
    elif not evidence.independent:
        reason_codes.append("agricultural_use_evidence_not_independent")
    else:
        reason_codes.append(f"independent_{evidence.land_use}_evidence")

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
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise ValueError("evento GeoJSON inválido")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("evento sin geometría")
        trajectory = _event_trajectory(geometry, arrays, raster_contract)
        result = attribute_post_change_event(
            event=dict(feature["properties"]),
            trajectory=trajectory,
            context=context,
            rules=rules,
        )
        result.update(
            {
                "area_ha": feature["properties"].get("area_ha"),
                "area_threshold_met": feature["properties"].get("area_threshold_met"),
                "estimated_onset_period_id": feature["properties"].get("estimated_onset_period_id"),
                "confidence": None,
                "confidence_semantics": "not_calibrated_in_v0",
                "quality_flags": (
                    ["insufficient_annual_rf_support"]
                    if 0 in trajectory.evaluable_pixel_count
                    else []
                ),
            }
        )
        results.append(result)
        output_features.append(
            {"type": "Feature", "geometry": geometry, "properties": dict(result)}
        )

    source_identity = {
        "events": _source_identity(event_bundle, event_manifest),
        "rf_deltas": _source_identity(rf_bundle, rf_manifest),
    }
    rules_payload = _rules_payload(rules)
    context_payload = _context_payload(context)
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
    analysis_id = uuid5(NAMESPACE_URL, f"post-change-attribution-v0:{identity_payload}")
    run_name = (
        f"{_slug(establishment_id)}-attribution-v0__"
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
        payload = {
            "schema_version": POST_CHANGE_ATTRIBUTION_SCHEMA_VERSION,
            "analysis_id": str(analysis_id),
            "establishment_id": establishment_id,
            "created_at": created_at.isoformat(),
            "status": "review_required",
            "automatic_final_assessment_generated": False,
            "conversion_confirmed_count": 0,
            "conversion_likely_count": conversion_likely_count,
            "counts_by_post_change_use": dict(sorted(counts.items())),
            "events": results,
        }
        metadata = {
            "schema_version": POST_CHANGE_ATTRIBUTION_SCHEMA_VERSION,
            "method": "deterministic_post_change_attributor_v0",
            "source_bundles": source_identity,
            "rules": rules_payload,
            "context": context_payload,
            "code": code_payload,
            "rf_score_semantics": "uncalibrated_binary_tree_vote_fraction",
            "agriculture_gate": (
                "requires explicit independent crop, pasture or livestock infrastructure evidence"
            ),
            "limitations": [
                "RF forest loss alone does not identify agricultural use.",
                "Declared managed-forest context is an alternative explanation, "
                "not independent evidence.",
                "The automatic attributor never emits conversion_confirmed.",
            ],
        }
        files = {
            "tables/evidence/post_change_attribution.csv": _csv_bytes(results),
            "json/evidence/post_change_attribution.json": _json_bytes(payload),
            "json/evidence/post_change_attribution.geojson": _json_bytes(
                {"type": "FeatureCollection", "features": output_features}
            ),
            "json/evidence/post_change_attribution_metadata.json": _json_bytes(metadata),
            "figures/evidence/post_change_attribution.png": _render_figure(output_features),
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
    evidence: AgriculturalUseEvidence | None,
) -> dict[str, Any]:
    conversion_likely = post_change_use == "agriculture_likely" and gates.get(
        "all_conversion_likely_gates", False
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
        "agricultural_use_evidence": (
            None
            if evidence is None
            else {
                "land_use": evidence.land_use,
                "source": evidence.source,
                "independent": evidence.independent,
            }
        ),
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
    schema = PROJECT_ROOT / "data/schemas/post-change-attribution-v1.0.0.json"
    if not schema.is_file():
        raise ValueError("post-change attribution schema file no existe")
    return {
        "module_path": "src/deforestation_pipeline/post_change_attribution.py",
        "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "schema_path": "data/schemas/post-change-attribution-v1.0.0.json",
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
            event_id: {
                "land_use": evidence.land_use,
                "source": evidence.source,
                "independent": evidence.independent,
            }
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
    axis.set_title("Atribución post-cambio v0 — requiere revisión humana")
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
