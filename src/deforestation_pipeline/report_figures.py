"""Materializa un paquete acotado de insumos para el informe técnico."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPORT_FIGURE_COLLECTION_SCHEMA_VERSION = "2.0.0"
REPORT_FIGURE_SELECTION_POLICY_VERSION = "2.0.0"
REPORT_DATASET_SCHEMA_VERSION = "1.0.0"
_MAX_REVIEW_EVENT_SHEETS = 10
_SUPERSEDED_FULL_PIPELINE_LIMITATIONS_AFTER_ATTRIBUTION = {
    "Se detectaron señales de perturbación sin atribuir su causa o uso posterior.",
    "No se ejecutó atribución de conversión ni se generó una conclusión final.",
}


@dataclass(frozen=True, slots=True)
class _Selection:
    component: str
    pattern: re.Pattern[str]
    target: str
    category: str
    reason: str
    event_kind: str | None = None


@dataclass(frozen=True, slots=True)
class _ComponentBundle:
    name: str
    bundle: Path
    manifest_path: Path
    manifest_sha256: str
    artifacts: Mapping[str, Mapping[str, object]]


def _static(component: str, source: str, target: str, category: str, reason: str) -> _Selection:
    return _Selection(component, re.compile(f"^{re.escape(source)}$"), target, category, reason)


_SELECTIONS = (
    _static(
        "full_pipeline",
        "figures/evidence/forest_baseline_2020.png",
        "figures/main/010_forest_baseline_2020.png",
        "main_figure",
        "línea base forestal al corte",
    ),
    _static(
        "full_pipeline",
        "figures/evidence/forest_screening_2020.png",
        "figures/main/020_forest_screening_2020.png",
        "main_figure",
        "dominio automático, revisión e insuficiencia",
    ),
    _static(
        "full_pipeline",
        "figures/seasonal/qa/rgb_timeline.png",
        "figures/main/030_rgb_timeline.png",
        "main_figure",
        "contexto RGB multitemporal acotado",
    ),
    _static(
        "full_pipeline",
        "figures/seasonal/qa/index_timeseries.png",
        "figures/main/040_spectral_index_timeline.png",
        "main_figure",
        "trayectoria temporal de índices",
    ),
    _static(
        "full_pipeline",
        "figures/seasonal/qa/observation_coverage.png",
        "figures/main/050_observation_coverage.png",
        "main_figure",
        "cobertura temporal y calidad observacional",
    ),
    _static(
        "full_pipeline",
        "figures/evidence/disturbance_detection.png",
        "figures/main/060_disturbance_detection.png",
        "main_figure",
        "resumen raster de señales de perturbación",
    ),
    _static(
        "full_pipeline",
        "figures/evidence/disturbance_events.png",
        "figures/main/070_disturbance_events_overview.png",
        "main_figure",
        "mapa general de eventos persistentes",
    ),
    _Selection(
        "full_pipeline",
        re.compile(r"^figures/evidence/disturbance_event_([a-zA-Z0-9_-]+)\.png$"),
        "figures/events/{event_id}/disturbance.png",
        "event_figure",
        "ficha individual de perturbación",
        "disturbance",
    ),
    _static(
        "hampel_benchmark",
        "figures/evidence/seasonal_hampel_benchmark.png",
        "figures/main/100_hampel_benchmark.png",
        "main_figure",
        "diagnóstico Hampel protegido",
    ),
    _Selection(
        "rf_annual_deltas",
        re.compile(r"^figures/evidence/rf_forest_deltas_[0-9]{4}_[0-9]{4}\.png$"),
        "figures/main/200_rf_forest_deltas_timeline.png",
        "main_figure",
        "trayectoria y deltas forestales RF",
    ),
    _Selection(
        "rf_annual_deltas",
        re.compile(r"^figures/evidence/rf_model_comparison_[0-9]{4}\.png$"),
        "figures/main/210_rf_model_comparison.png",
        "main_figure",
        "comparación del modelo RF de referencia",
    ),
    _Selection(
        "agricultural_collection",
        re.compile(r"^figures/evidence/agricultural_monthly_evidence_([a-zA-Z0-9_-]+)\.png$"),
        "figures/events/{event_id}/agricultural_monthly.png",
        "event_figure",
        "serie mensual y mapas agrícolas",
        "agricultural_monthly",
    ),
    _Selection(
        "agricultural_persistence",
        re.compile(r"^figures/evidence/agricultural_persistence_([a-zA-Z0-9_-]+)\.png$"),
        "figures/events/{event_id}/agricultural_persistence.png",
        "event_figure",
        "soporte agrícola persistente",
        "agricultural_persistence",
    ),
    _static(
        "post_change_attribution",
        "figures/evidence/post_change_attribution.png",
        "figures/main/500_post_change_attribution.png",
        "main_figure",
        "resultado conservador de atribución post-cambio",
    ),
    _Selection(
        "post_change_attribution",
        re.compile(r"^figures/evidence/likely_conversion_conjunction_([a-zA-Z0-9_-]+)\.png$"),
        "figures/events/{event_id}/likely_conversion_conjunction.png",
        "event_figure",
        "conjunción espacial compatible con conversión",
        "likely_conversion_conjunction",
    ),
    _static(
        "full_pipeline",
        "json/run/summary.json",
        "data/main/010_full_pipeline_summary.json",
        "main_data",
        "resumen del análisis base",
    ),
    _static(
        "full_pipeline",
        "json/evidence/forest_baseline_2020.json",
        "data/main/020_forest_baseline_2020.json",
        "main_data",
        "métricas y reglas de línea base",
    ),
    _static(
        "full_pipeline",
        "json/evidence/disturbance_detection.json",
        "data/main/025_disturbance_detection.json",
        "main_data",
        "métricas y reglas de detección",
    ),
    _static(
        "full_pipeline",
        "json/evidence/disturbance_events.json",
        "data/main/030_disturbance_events.json",
        "main_data",
        "inventario canónico de eventos",
    ),
    _static(
        "hampel_benchmark",
        "json/evidence/seasonal_hampel_benchmark.json",
        "data/main/040_hampel_benchmark.json",
        "main_data",
        "resultado resumido del diagnóstico Hampel",
    ),
    _Selection(
        "rf_annual_deltas",
        re.compile(r"^json/evidence/rf_forest_deltas_[0-9]{4}_[0-9]{4}\.json$"),
        "data/main/050_rf_forest_deltas.json",
        "main_data",
        "resumen anual RF",
    ),
    _static(
        "agricultural_collection",
        "json/run/summary.json",
        "data/main/060_agricultural_collection_summary.json",
        "main_data",
        "estado compacto de recolección agrícola",
    ),
    _static(
        "agricultural_collection",
        "json/evidence/agricultural_evidence.json",
        "data/main/061_agricultural_evidence.json",
        "main_data",
        "evidencia mensual por evento",
    ),
    _static(
        "agricultural_persistence",
        "json/evidence/agricultural_persistence.json",
        "data/main/070_agricultural_persistence.json",
        "main_data",
        "persistencia agrícola por evento",
    ),
    _static(
        "post_change_attribution",
        "json/evidence/post_change_attribution.json",
        "data/main/080_post_change_attribution.json",
        "main_data",
        "resultado automático conservador",
    ),
    _Selection(
        "rf_annual_deltas",
        re.compile(r"^tables/evidence/rf_forest_deltas_[0-9]{4}_[0-9]{4}\.csv$"),
        "data/main/050_rf_forest_deltas.csv",
        "main_table",
        "tabla anual RF",
    ),
    _static(
        "agricultural_persistence",
        "tables/evidence/agricultural_persistence.csv",
        "data/main/070_agricultural_persistence.csv",
        "main_table",
        "tabla compacta de persistencia",
    ),
    _static(
        "post_change_attribution",
        "tables/evidence/post_change_attribution.csv",
        "data/main/080_post_change_attribution.csv",
        "main_table",
        "tabla compacta de atribución",
    ),
    _static(
        "full_pipeline",
        "json/evidence/disturbance_events.geojson",
        "annex/spatial/disturbance_events.geojson",
        "annex_spatial",
        "geometrías completas de eventos",
    ),
    _static(
        "agricultural_collection",
        "json/evidence/agricultural_evidence.geojson",
        "annex/spatial/agricultural_evidence.geojson",
        "annex_spatial",
        "geometrías de evidencia agrícola",
    ),
    _static(
        "post_change_attribution",
        "json/evidence/post_change_attribution.geojson",
        "annex/spatial/post_change_attribution.geojson",
        "annex_spatial",
        "geometrías de atribución",
    ),
    _static(
        "full_pipeline",
        "tables/evidence/disturbance_events.csv",
        "annex/tables/disturbance_events.csv",
        "annex_table",
        "tabla completa de eventos",
    ),
    _static(
        "full_pipeline",
        "tables/evidence/disturbance_period_summary.csv",
        "annex/tables/disturbance_period_summary.csv",
        "annex_table",
        "resumen temporal de perturbaciones",
    ),
    _static(
        "full_pipeline",
        "tables/seasonal/summary.csv",
        "annex/tables/seasonal_summary.csv",
        "annex_table",
        "estadística estacional completa",
    ),
    _static(
        "hampel_benchmark",
        "tables/evidence/hampel_benchmark_metrics.csv",
        "annex/tables/hampel_benchmark_metrics.csv",
        "annex_table",
        "métricas del diagnóstico Hampel",
    ),
    _static(
        "hampel_benchmark",
        "tables/evidence/hampel_benchmark_observations.csv",
        "annex/tables/hampel_benchmark_observations.csv",
        "annex_table",
        "observaciones completas del diagnóstico Hampel",
    ),
    _static(
        "agricultural_collection",
        "tables/evidence/agricultural_evidence.csv",
        "annex/tables/agricultural_evidence.csv",
        "annex_table",
        "serie agrícola evento-mes",
    ),
    _static(
        "agricultural_persistence",
        "json/evidence/agricultural_evidence_persistent.json",
        "annex/methodology/agricultural_evidence_persistent.json",
        "annex_methodology",
        "observaciones agrícolas normalizadas",
    ),
    _static(
        "agricultural_collection",
        "json/evidence/agricultural_evidence_metadata.json",
        "annex/methodology/agricultural_evidence_metadata.json",
        "annex_methodology",
        "método y fuente agrícola",
    ),
    _static(
        "rf_annual_deltas",
        "json/evidence/rf_forest_deltas_metadata.json",
        "annex/methodology/rf_forest_deltas_metadata.json",
        "annex_methodology",
        "método y limitaciones RF",
    ),
    _Selection(
        "rf_annual_deltas",
        re.compile(r"^json/evidence/rf_model_comparison_[0-9]{4}\.json$"),
        "annex/methodology/rf_model_comparison.json",
        "annex_methodology",
        "comparación técnica RF",
    ),
    _static(
        "post_change_attribution",
        "json/evidence/post_change_attribution_metadata.json",
        "annex/methodology/post_change_attribution_metadata.json",
        "annex_methodology",
        "reglas y limitaciones de atribución",
    ),
)


def materialize_report_figure_collection(
    *,
    staging: Path,
    components: Sequence[Mapping[str, object]],
    overall_status: str,
    recorded_at: str,
    parent_analysis_id: str | None = None,
) -> dict[str, object]:
    """Copia insumos seleccionados y genera una interfaz normalizada de informe."""
    staging_root = staging.resolve()
    bundles, component_statuses = _load_components(staging_root, components)
    source_documents = _load_source_documents(bundles)
    selected_event_ids = _select_event_ids(source_documents)
    selected_event_lookup = {item.casefold(): item for item in selected_event_ids}
    allow_all_event_figures = not _has_event_selection_sources(source_documents)
    selected: list[dict[str, Any]] = []
    targets: set[str] = set()

    for bundle in bundles.values():
        for relative_text, artifact in bundle.artifacts.items():
            selection = _selection(bundle.name, relative_text)
            if selection is None:
                continue
            match = selection.pattern.fullmatch(relative_text)
            assert match is not None
            event_id: str | None = None
            if selection.event_kind is not None:
                raw_event_id = match.group(1)
                event_id = selected_event_lookup.get(raw_event_id.casefold())
                if event_id is None and not allow_all_event_figures:
                    continue
                event_id = event_id or raw_event_id
            target_relative = selection.target.format(event_id=event_id)
            report_path = f"report_assets/{target_relative}"
            if report_path in targets:
                raise ValueError("report_asset_target_collision")
            targets.add(report_path)
            source = _verified_source(bundle.bundle, Path(relative_text), artifact)
            source_sha = str(artifact["sha256"])
            selected.append(
                {
                    "category": selection.category,
                    "selection_reason": selection.reason,
                    "component": bundle.name,
                    "event_id": event_id,
                    "source_bundle": bundle.bundle.relative_to(staging_root).as_posix(),
                    "source_manifest": bundle.manifest_path.relative_to(staging_root).as_posix(),
                    "source_manifest_sha256": bundle.manifest_sha256,
                    "source_path": source.relative_to(staging_root).as_posix(),
                    "source_sha256": source_sha,
                    "size_bytes": artifact["size_bytes"],
                    "report_path": report_path,
                    "report_sha256": source_sha,
                    "_source": source,
                }
            )

    selected.sort(key=lambda item: str(item["report_path"]))
    for item in selected:
        if item["category"] not in {"main_figure", "event_figure"}:
            continue
        status = component_statuses[str(item["component"])]
        count = status["selected_figure_count"]
        assert isinstance(count, int)
        status["selected_figure_count"] = count + 1
    report_root = staging_root / "report_assets"
    temporary_root = staging_root / ".report_assets.staging"
    if report_root.exists() or temporary_root.exists():
        raise FileExistsError("report_asset_collection_already_exists")
    try:
        public_files = _copy_selected(selected, temporary_root)
        public_files.extend(
            _copy_status_receipts(
                staging=staging_root,
                components=components,
                temporary_root=temporary_root,
            )
        )
        public_files.sort(key=lambda item: str(item["report_path"]))
        figures = [
            item for item in public_files if item["category"] in {"main_figure", "event_figure"}
        ]
        dataset = _build_report_dataset(
            source_documents=source_documents,
            component_statuses=component_statuses,
            selected_event_ids=selected_event_ids,
            overall_status=overall_status,
            recorded_at=recorded_at,
            parent_analysis_id=parent_analysis_id,
            files=public_files,
        )
        dataset_path = temporary_root / "report_dataset.json"
        _write_json(dataset_path, dataset)
        index = {
            "schema_version": REPORT_FIGURE_COLLECTION_SCHEMA_VERSION,
            "selection_policy_version": REPORT_FIGURE_SELECTION_POLICY_VERSION,
            "recorded_at": recorded_at,
            "parent_run_status": overall_status,
            "copy_semantics": "byte_for_byte_duplicates; originals remain authoritative",
            "dataset_path": "report_assets/report_dataset.json",
            "dataset_sha256": _sha256(dataset_path),
            "selected_event_ids": selected_event_ids,
            "selected_event_count": len(selected_event_ids),
            "figure_count": len(figures),
            "file_count": len(public_files),
            "component_statuses": component_statuses,
            "figures": figures,
            "files": public_files,
            "limitations": [
                "La selección facilita el informe y no reemplaza los bundles científicos.",
                "La ausencia de una figura puede reflejar soporte nulo o componente no disponible.",
                "Los eventos no seleccionados permanecen en las tablas y geometrías del anexo.",
                "Ningún producto constituye certificación ni confirmación legal.",
            ],
        }
        _write_json(temporary_root / "index.json", index)
        temporary_root.rename(report_root)
    except Exception:
        resolved_temporary = temporary_root.resolve()
        if temporary_root.exists() and resolved_temporary.is_relative_to(staging_root):
            shutil.rmtree(resolved_temporary)
        raise

    index_path = report_root / "index.json"
    dataset_path = report_root / "report_dataset.json"
    return {
        "schema_version": REPORT_FIGURE_COLLECTION_SCHEMA_VERSION,
        "selection_policy_version": REPORT_FIGURE_SELECTION_POLICY_VERSION,
        "status": "completed",
        "figures_root": "report_assets/figures",
        "index_path": "report_assets/index.json",
        "index_sha256": _sha256(index_path),
        "dataset_path": "report_assets/report_dataset.json",
        "dataset_sha256": _sha256(dataset_path),
        "figure_count": len(figures),
        "file_count": len(public_files),
        "selected_event_count": len(selected_event_ids),
    }


def _load_components(
    staging: Path, components: Sequence[Mapping[str, object]]
) -> tuple[dict[str, _ComponentBundle], dict[str, dict[str, object]]]:
    bundles: dict[str, _ComponentBundle] = {}
    statuses: dict[str, dict[str, object]] = {}
    for component in components:
        name = str(component.get("name", ""))
        status = str(component.get("status", ""))
        statuses[name] = {
            "status": status,
            "selected_figure_count": 0,
            "status_receipt": component.get("status_receipt"),
            "status_receipt_sha256": component.get("status_receipt_sha256"),
        }
        if status != "completed":
            continue
        bundle = _component_bundle(staging, component)
        manifest_path, manifest = _component_manifest(staging, component, bundle)
        artifacts_value = manifest.get("artifacts")
        if not isinstance(artifacts_value, list):
            raise ValueError("report_figure_manifest_artifacts_invalid")
        artifacts: dict[str, Mapping[str, object]] = {}
        for artifact in artifacts_value:
            if not isinstance(artifact, dict):
                raise ValueError("report_figure_artifact_invalid")
            relative_text = artifact.get("path")
            if not isinstance(relative_text, str) or not relative_text:
                raise ValueError("report_figure_artifact_path_invalid")
            relative = Path(relative_text)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("report_figure_artifact_path_unsafe")
            if relative_text in artifacts:
                raise ValueError("report_asset_duplicate_artifact_path")
            artifacts[relative_text] = artifact
        manifest_sha = str(component["child_manifest_sha256"])
        bundles[name] = _ComponentBundle(name, bundle, manifest_path, manifest_sha, artifacts)
    return bundles, statuses


def _load_source_documents(
    bundles: Mapping[str, _ComponentBundle],
) -> dict[str, Mapping[str, object]]:
    wanted = {
        "full_summary": ("full_pipeline", "json/run/summary.json"),
        "baseline": ("full_pipeline", "json/evidence/forest_baseline_2020.json"),
        "detection": ("full_pipeline", "json/evidence/disturbance_detection.json"),
        "disturbance": ("full_pipeline", "json/evidence/disturbance_events.json"),
        "rf": ("rf_annual_deltas", "json/evidence/rf_forest_deltas_2020_2024.json"),
        "agriculture_summary": ("agricultural_collection", "json/run/summary.json"),
        "agriculture": ("agricultural_collection", "json/evidence/agricultural_evidence.json"),
        "persistence": ("agricultural_persistence", "json/evidence/agricultural_persistence.json"),
        "attribution": ("post_change_attribution", "json/evidence/post_change_attribution.json"),
    }
    documents: dict[str, Mapping[str, object]] = {}
    for key, (component, relative) in wanted.items():
        bundle = bundles.get(component)
        if bundle is None or relative not in bundle.artifacts:
            continue
        source = _verified_source(bundle.bundle, Path(relative), bundle.artifacts[relative])
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("report_asset_source_json_not_object")
        documents[key] = payload
    return documents


def _has_event_selection_sources(documents: Mapping[str, Mapping[str, object]]) -> bool:
    return "attribution" in documents or "disturbance" in documents


def _select_event_ids(documents: Mapping[str, Mapping[str, object]]) -> list[str]:
    attribution_events = _object_list(documents.get("attribution", {}).get("events"))
    likely = [
        event
        for event in attribution_events
        if event.get("automatic_status") == "conversion_likely"
    ]
    review = [
        event
        for event in attribution_events
        if event.get("automatic_status") == "review_required"
        and event.get("area_threshold_met") is True
    ]
    likely.sort(key=_event_sort_key)
    review.sort(key=_event_sort_key)
    selected = likely + review[: max(0, _MAX_REVIEW_EVENT_SHEETS - len(likely))]
    if not selected:
        disturbance_events = _object_list(documents.get("disturbance", {}).get("events"))
        disturbance_events.sort(key=_event_sort_key)
        selected = disturbance_events[:5]
    result: list[str] = []
    seen: set[str] = set()
    for event in selected:
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id and event_id.casefold() not in seen:
            result.append(event_id)
            seen.add(event_id.casefold())
    return result


def _event_sort_key(event: Mapping[str, object]) -> tuple[float, str]:
    area = event.get("area_ha")
    numeric_area = float(area) if isinstance(area, (int, float)) else 0.0
    return (-numeric_area, str(event.get("event_id", "")))


def _copy_selected(selected: list[dict[str, Any]], temporary_root: Path) -> list[dict[str, object]]:
    public: list[dict[str, object]] = []
    for item in selected:
        source = item.pop("_source")
        assert isinstance(source, Path)
        relative = Path(str(item["report_path"])).relative_to("report_assets")
        target = temporary_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _sha256(target) != item["report_sha256"]:
            raise ValueError("report_asset_copy_sha256_mismatch")
        public.append(item)
    return public


def _copy_status_receipts(
    *,
    staging: Path,
    components: Sequence[Mapping[str, object]],
    temporary_root: Path,
) -> list[dict[str, object]]:
    copied: list[dict[str, object]] = []
    for component in components:
        receipt_text = component.get("status_receipt")
        if receipt_text is None:
            continue
        name = str(component.get("name", ""))
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValueError("report_asset_component_name_unsafe")
        if not isinstance(receipt_text, str) or not receipt_text:
            raise ValueError("report_asset_status_receipt_invalid")
        receipt_relative = Path(receipt_text)
        if (
            receipt_relative.is_absolute()
            or ".." in receipt_relative.parts
            or _contains_symlink(staging, receipt_relative)
        ):
            raise ValueError("report_asset_status_receipt_unsafe")
        source = (staging / receipt_relative).resolve()
        if not source.is_relative_to(staging) or not source.is_file():
            raise ValueError("report_asset_status_receipt_missing")
        expected_sha = component.get("status_receipt_sha256")
        if not isinstance(expected_sha, str) or _sha256(source) != expected_sha:
            raise ValueError("report_asset_status_receipt_sha256_mismatch")
        report_relative = Path(f"annex/provenance/{name}_component_status.json")
        target = temporary_root / report_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _sha256(target) != expected_sha:
            raise ValueError("report_asset_status_receipt_copy_sha256_mismatch")
        copied.append(
            {
                "category": "annex_provenance",
                "selection_reason": "estado auditable de componente sin bundle",
                "component": name,
                "event_id": None,
                "source_bundle": None,
                "source_manifest": None,
                "source_manifest_sha256": None,
                "source_path": receipt_relative.as_posix(),
                "source_sha256": expected_sha,
                "size_bytes": source.stat().st_size,
                "report_path": f"report_assets/{report_relative.as_posix()}",
                "report_sha256": expected_sha,
            }
        )
    return copied


def _build_report_dataset(
    *,
    source_documents: Mapping[str, Mapping[str, object]],
    component_statuses: Mapping[str, Mapping[str, object]],
    selected_event_ids: Sequence[str],
    overall_status: str,
    recorded_at: str,
    parent_analysis_id: str | None,
    files: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    full = source_documents.get("full_summary", {})
    baseline = source_documents.get("baseline", {})
    disturbance = source_documents.get("disturbance", {})
    detection = source_documents.get("detection", {})
    attribution = source_documents.get("attribution", {})
    baseline_metrics = _nested_mapping(baseline, "screening", "metrics")
    detection_screening = _mapping(detection.get("screening"))
    area = _mapping(full.get("area"))
    selected_events = [_event_record(event_id, source_documents) for event_id in selected_event_ids]
    source_paths = {
        str(item["category"]): sorted(
            str(candidate["report_path"])
            for candidate in files
            if candidate["category"] == item["category"]
        )
        for item in files
    }
    inherited_limitations = _string_list(full.get("limitations"))
    if attribution:
        inherited_limitations = [
            limitation
            for limitation in inherited_limitations
            if limitation not in _SUPERSEDED_FULL_PIPELINE_LIMITATIONS_AFTER_ATTRIBUTION
        ]
    return {
        "schema_version": REPORT_DATASET_SCHEMA_VERSION,
        "recorded_at": recorded_at,
        "analysis": {
            "analysis_id": parent_analysis_id or full.get("analysis_id"),
            "full_pipeline_analysis_id": full.get("analysis_id"),
            "establishment_id": full.get("establishment_id"),
            "analysis_end_date": full.get("analysis_end_date"),
            "requested_analysis_end_date": full.get("requested_analysis_end_date"),
            "overall_status": overall_status,
        },
        "headline_metrics": {
            "establishment_area_ha": area.get("total_area_ha"),
            "forest_area_2020_ha": baseline_metrics.get("automated_forest_area_ha"),
            "detected_event_area_ha": disturbance.get("total_event_area_ha"),
            "detected_event_count": disturbance.get("event_count"),
            "likely_conversion_area_ha": attribution.get("likely_conversion_area_ha"),
            "conversion_likely_count": attribution.get("conversion_likely_count"),
            "automatic_final_assessment_generated": detection_screening.get(
                "final_assessment_generated"
            ),
        },
        "selected_events": selected_events,
        "event_selection": {
            "policy": (
                "all_conversion_likely_then_review_required_over_area_threshold_up_to_10; "
                "fallback_largest_5"
            ),
            "selected_event_ids": list(selected_event_ids),
            "complete_event_inventory_path": "report_assets/data/main/030_disturbance_events.json",
        },
        "component_statuses": component_statuses,
        "source_paths_by_category": source_paths,
        "limitations": [
            "Resultado técnico automático sujeto a revisión humana.",
            "No constituye certificación ni confirmación legal EUDR.",
            *inherited_limitations,
        ],
    }


def _event_record(
    event_id: str, documents: Mapping[str, Mapping[str, object]]
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "disturbance": _find_event(documents.get("disturbance"), event_id),
        "agricultural_collection": _find_event(documents.get("agriculture"), event_id),
        "agricultural_persistence": _find_event(documents.get("persistence"), event_id),
        "attribution": _find_event(documents.get("attribution"), event_id),
    }


def _find_event(
    document: Mapping[str, object] | None, event_id: str
) -> Mapping[str, object] | None:
    if document is None:
        return None
    for event in _object_list(document.get("events")):
        candidate = event.get("event_id")
        if isinstance(candidate, str) and candidate.casefold() == event_id.casefold():
            return event
    return None


def _object_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _nested_mapping(root: Mapping[str, object], *keys: str) -> Mapping[str, object]:
    current: Mapping[str, object] = root
    for key in keys:
        current = _mapping(current.get(key))
    return current


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


def _verified_source(bundle: Path, relative: Path, artifact: Mapping[str, object]) -> Path:
    source = _safe_source(bundle, relative)
    expected_size = artifact.get("size_bytes")
    expected_sha = artifact.get("sha256")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError("report_figure_source_size_invalid")
    if source.stat().st_size != expected_size:
        raise ValueError("report_figure_source_size_mismatch")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("report_figure_source_sha256_invalid")
    if _sha256(source) != expected_sha:
        raise ValueError("report_figure_source_sha256_mismatch")
    return source


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


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
