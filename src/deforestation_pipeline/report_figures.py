"""Recolecta figuras manifestadas para el futuro informe técnico."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPORT_FIGURE_COLLECTION_SCHEMA_VERSION = "1.0.0"
REPORT_FIGURE_SELECTION_POLICY_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class _Selection:
    component: str
    pattern: re.Pattern[str]
    order: int
    category: str
    reason: str
    report_stem: str | None = None


_SELECTIONS = (
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/evidence/forest_baseline_2020\.png$"),
        10,
        "forest_baseline",
        "línea base forestal al corte",
        "forest_baseline_2020",
    ),
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/evidence/forest_screening_2020\.png$"),
        20,
        "forest_screening",
        "dominio automático, revisión e insuficiencia",
        "forest_screening_2020",
    ),
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/seasonal/qa/rgb_timeline\.png$"),
        30,
        "rgb_timeline",
        "contexto RGB multitemporal acotado",
        "rgb_timeline",
    ),
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/seasonal/qa/index_timeseries\.png$"),
        40,
        "spectral_timeline",
        "trayectoria temporal de índices",
        "spectral_index_timeline",
    ),
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/seasonal/qa/observation_coverage\.png$"),
        50,
        "observation_coverage",
        "cobertura temporal y calidad observacional",
        "observation_coverage",
    ),
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/evidence/disturbance_detection\.png$"),
        60,
        "disturbance_detection",
        "resumen raster de señales de perturbación",
        "disturbance_detection",
    ),
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/evidence/disturbance_events\.png$"),
        70,
        "disturbance_events",
        "mapa general de eventos persistentes",
        "disturbance_events_overview",
    ),
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/evidence/(disturbance_event_[a-zA-Z0-9_-]+)\.png$"),
        80,
        "disturbance_event_sheet",
        "ficha individual de evento priorizado",
    ),
    _Selection(
        "hampel_benchmark",
        re.compile(r"^figures/evidence/seasonal_hampel_benchmark\.png$"),
        100,
        "hampel_diagnostic",
        "diagnóstico Hampel protegido",
        "hampel_benchmark",
    ),
    _Selection(
        "rf_annual_deltas",
        re.compile(r"^figures/evidence/rf_forest_deltas_[0-9]{4}_[0-9]{4}\.png$"),
        200,
        "rf_timeline",
        "trayectoria y deltas forestales RF",
        "rf_forest_deltas_timeline",
    ),
    _Selection(
        "rf_annual_deltas",
        re.compile(r"^figures/evidence/rf_model_comparison_[0-9]{4}\.png$"),
        210,
        "rf_model_comparison",
        "comparación del modelo RF en el año de referencia",
        "rf_model_comparison",
    ),
    _Selection(
        "agricultural_collection",
        re.compile(r"^figures/evidence/(agricultural_monthly_evidence_[a-zA-Z0-9_-]+)\.png$"),
        300,
        "agricultural_monthly_evidence",
        "serie mensual y mapas agrícolas representativos",
    ),
    _Selection(
        "agricultural_persistence",
        re.compile(r"^figures/evidence/(agricultural_persistence_[a-zA-Z0-9_-]+)\.png$"),
        400,
        "agricultural_persistence",
        "soporte agrícola persistente por evento",
    ),
    _Selection(
        "post_change_attribution",
        re.compile(r"^figures/evidence/post_change_attribution\.png$"),
        500,
        "post_change_attribution",
        "resultado conservador de atribución post-cambio",
        "post_change_attribution",
    ),
    _Selection(
        "post_change_attribution",
        re.compile(r"^figures/evidence/(likely_conversion_conjunction_[a-zA-Z0-9_-]+)\.png$"),
        510,
        "likely_conversion_conjunction",
        "conjunción espacial de evidencia compatible con conversión",
    ),
)


def materialize_report_figure_collection(
    *,
    staging: Path,
    components: Sequence[Mapping[str, object]],
    overall_status: str,
    recorded_at: str,
) -> dict[str, object]:
    """Copia una selección versionada sin alterar los bundles científicos."""
    staging_root = staging.resolve()
    selected: list[dict[str, Any]] = []
    component_statuses: dict[str, dict[str, object]] = {}
    targets: set[str] = set()

    for component in components:
        name = str(component.get("name", ""))
        status = str(component.get("status", ""))
        status_record: dict[str, object] = {
            "status": status,
            "selected_figure_count": 0,
            "status_receipt": component.get("status_receipt"),
            "status_receipt_sha256": component.get("status_receipt_sha256"),
        }
        component_statuses[name] = status_record
        if status != "completed":
            continue
        bundle = _component_bundle(staging_root, component)
        manifest_path, manifest = _component_manifest(staging_root, component, bundle)
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("report_figure_manifest_artifacts_invalid")
        source_bundle_relative = bundle.relative_to(staging_root).as_posix()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError("report_figure_artifact_invalid")
            relative_text = artifact.get("path")
            if not isinstance(relative_text, str) or not relative_text:
                raise ValueError("report_figure_artifact_path_invalid")
            relative = Path(relative_text)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("report_figure_artifact_path_unsafe")
            selection = _selection(name, relative.as_posix())
            if selection is None:
                continue
            source = _safe_source(bundle, relative)
            expected_size = artifact.get("size_bytes")
            expected_sha = artifact.get("sha256")
            if not isinstance(expected_size, int) or expected_size < 0:
                raise ValueError("report_figure_source_size_invalid")
            if source.stat().st_size != expected_size:
                raise ValueError("report_figure_source_size_mismatch")
            if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
                raise ValueError("report_figure_source_sha256_invalid")
            source_sha = _sha256(source)
            if source_sha != expected_sha:
                raise ValueError("report_figure_source_sha256_mismatch")
            match = selection.pattern.fullmatch(relative.as_posix())
            assert match is not None
            stem = selection.report_stem or match.group(1)
            target_name = f"{selection.order:03d}_{stem}.png"
            if target_name in targets:
                raise ValueError("report_figure_target_collision")
            targets.add(target_name)
            source_parent_relative = source.relative_to(staging_root).as_posix()
            selected.append(
                {
                    "order": selection.order,
                    "category": selection.category,
                    "selection_reason": selection.reason,
                    "component": name,
                    "source_bundle": source_bundle_relative,
                    "source_manifest": manifest_path.relative_to(staging_root).as_posix(),
                    "source_manifest_sha256": component["child_manifest_sha256"],
                    "source_path": source_parent_relative,
                    "source_sha256": source_sha,
                    "size_bytes": expected_size,
                    "report_path": f"report_assets/figures/{target_name}",
                    "report_sha256": source_sha,
                    "_source": source,
                }
            )
            selected_count = status_record["selected_figure_count"]
            assert isinstance(selected_count, int)
            status_record["selected_figure_count"] = selected_count + 1

    selected.sort(key=lambda item: str(item["report_path"]))
    report_root = staging_root / "report_assets"
    temporary_root = staging_root / ".report_assets.staging"
    if report_root.exists() or temporary_root.exists():
        raise FileExistsError("report_figure_collection_already_exists")
    try:
        figures_root = temporary_root / "figures"
        figures_root.mkdir(parents=True)
        public_figures: list[dict[str, object]] = []
        for item in selected:
            source = item.pop("_source")
            assert isinstance(source, Path)
            target = figures_root / Path(str(item["report_path"])).name
            shutil.copyfile(source, target)
            if _sha256(target) != item["report_sha256"]:
                raise ValueError("report_figure_copy_sha256_mismatch")
            public_figures.append(item)
        index = {
            "schema_version": REPORT_FIGURE_COLLECTION_SCHEMA_VERSION,
            "selection_policy_version": REPORT_FIGURE_SELECTION_POLICY_VERSION,
            "recorded_at": recorded_at,
            "parent_run_status": overall_status,
            "copy_semantics": "byte_for_byte_duplicates; originals remain authoritative",
            "figures_root": "report_assets/figures",
            "figure_count": len(public_figures),
            "component_statuses": component_statuses,
            "figures": public_figures,
            "limitations": [
                "La selección facilita el informe y no reemplaza los bundles científicos.",
                "La ausencia de una figura puede reflejar soporte nulo o componente no disponible.",
                "Ninguna figura constituye certificación ni confirmación legal.",
            ],
        }
        temporary_index = temporary_root / "index.json"
        temporary_index.write_text(
            json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary_root.rename(report_root)
    except Exception:
        resolved_temporary = temporary_root.resolve()
        if temporary_root.exists() and resolved_temporary.is_relative_to(staging_root):
            shutil.rmtree(resolved_temporary)
        raise
    index_path = report_root / "index.json"
    return {
        "schema_version": REPORT_FIGURE_COLLECTION_SCHEMA_VERSION,
        "selection_policy_version": REPORT_FIGURE_SELECTION_POLICY_VERSION,
        "status": "completed",
        "figures_root": "report_assets/figures",
        "index_path": "report_assets/index.json",
        "index_sha256": _sha256(index_path),
        "figure_count": len(public_figures),
    }


def _selection(component: str, relative: str) -> _Selection | None:
    for selection in _SELECTIONS:
        if selection.component == component and selection.pattern.fullmatch(relative):
            return selection
    return None


def _component_bundle(staging: Path, component: Mapping[str, object]) -> Path:
    relative_text = component.get("output_bundle")
    if not isinstance(relative_text, str) or not relative_text:
        raise ValueError("report_figure_component_bundle_missing")
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("report_figure_component_bundle_unsafe")
    if _contains_symlink(staging, relative):
        raise ValueError("report_figure_component_bundle_symlink")
    bundle = (staging / relative).resolve()
    if not bundle.is_relative_to(staging) or not bundle.is_dir():
        raise ValueError("report_figure_component_bundle_invalid")
    return bundle


def _component_manifest(
    staging: Path, component: Mapping[str, object], bundle: Path
) -> tuple[Path, Mapping[str, object]]:
    relative_text = component.get("child_manifest")
    if not isinstance(relative_text, str) or not relative_text:
        raise ValueError("report_figure_child_manifest_missing")
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts or _contains_symlink(staging, relative):
        raise ValueError("report_figure_child_manifest_unsafe")
    manifest = (staging / relative).resolve()
    if not manifest.is_relative_to(bundle) or not manifest.is_file():
        raise ValueError("report_figure_child_manifest_invalid")
    expected_sha = component.get("child_manifest_sha256")
    if not isinstance(expected_sha, str) or _sha256(manifest) != expected_sha:
        raise ValueError("report_figure_child_manifest_sha256_mismatch")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("report_figure_child_manifest_not_object")
    return manifest, payload


def _safe_source(bundle: Path, relative: Path) -> Path:
    if _contains_symlink(bundle, relative):
        raise ValueError("report_figure_source_symlink")
    source = (bundle / relative).resolve()
    if not source.is_relative_to(bundle) or not source.is_file():
        raise ValueError("report_figure_source_missing_or_escape")
    return source


def _contains_symlink(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
