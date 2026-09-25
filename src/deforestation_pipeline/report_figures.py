"""Materializa un paquete acotado de insumos para el informe técnico."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom

REPORT_FIGURE_COLLECTION_SCHEMA_VERSION = "2.5.0"
REPORT_FIGURE_SELECTION_POLICY_VERSION = "2.4.0"
LEGACY_REPORT_DATASET_SCHEMA_VERSION = "1.4.0"
PRIOR_REPORT_DATASET_SCHEMA_VERSION = "2.0.0"
REPORT_DATASET_SCHEMA_VERSION = "2.1.0"
REPORT_DATASET_SCHEMA_ID = "urn:deforestation-pipeline:report-dataset:2.1.0"
_MAX_REVIEW_EVENT_SHEETS = 10
_SEASON_ORDER = {"DJF": 0, "MAM": 1, "JJA": 2, "SON": 3}
_SEASONAL_INDICES = re.compile(
    r"^tiffs/seasonal/(?P<period>[0-9]{4}-(?:DJF|MAM|JJA|SON))/indices\.tif$"
)
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
        "agricultural_collection",
        re.compile(r"^figures/evidence/dynamic_world_annual_land_cover_[0-9]{4}_[0-9]{4}\.png$"),
        "figures/main/300_dynamic_world_annual_land_cover.png",
        "main_figure",
        "serie anual descriptiva de cobertura/uso del suelo Dynamic World",
    ),
    _Selection(
        "agricultural_collection",
        re.compile(
            r"^tiffs/evidence/agricultural/annual_land_cover/"
            r"dynamic_world_land_cover_(?P<year>[0-9]{4})\.tif$"
        ),
        "annex/rasters/dynamic_world_land_cover_{year}.tif",
        "annex_raster",
        "GeoTIFF anual categórico Dynamic World por moda de label top-1",
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
        "disturbance_candidate_fusion",
        "json/evidence/disturbance_candidate_fusion.json",
        "data/main/030_disturbance_events.json",
        "main_data",
        "dominio candidato RF-first con soporte robusto y CCDC adaptado para reporte",
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
        "disturbance_candidate_fusion",
        "json/evidence/disturbance_events.geojson",
        "annex/spatial/disturbance_events.geojson",
        "annex_spatial",
        "geometrías completas de candidatos RF-first",
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
            if (
                "fused_disturbance" in source_documents
                and bundle.name == "full_pipeline"
                and relative_text
                in {
                    "json/evidence/disturbance_events.json",
                    "json/evidence/disturbance_events.geojson",
                }
            ):
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
            target_relative = selection.target.format(
                event_id=event_id,
                **{key: value for key, value in match.groupdict().items() if value is not None},
            )
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
    report_root = staging_root / "report_assets"
    temporary_root = staging_root / ".report_assets.staging"
    if report_root.exists() or temporary_root.exists():
        raise FileExistsError("report_asset_collection_already_exists")
    try:
        public_files = _copy_selected(selected, temporary_root)
        public_files.extend(
            _materialize_rf_candidate_spectral_figures(
                staging=staging_root,
                temporary_root=temporary_root,
                bundles=bundles,
                source_documents=source_documents,
                selected_event_ids=selected_event_ids,
                existing_report_paths={str(item["report_path"]) for item in public_files},
            )
        )
        public_files.extend(
            _copy_status_receipts(
                staging=staging_root,
                components=components,
                temporary_root=temporary_root,
            )
        )
        _adapt_fused_disturbance_report_asset(
            public_files=public_files,
            temporary_root=temporary_root,
            source_documents=source_documents,
        )
        _materialize_subthreshold_candidate_csv(
            public_files=public_files,
            temporary_root=temporary_root,
            source_documents=source_documents,
        )
        public_files.sort(key=lambda item: str(item["report_path"]))
        for item in public_files:
            if item["category"] not in {"main_figure", "event_figure"}:
                continue
            status = component_statuses[str(item["component"])]
            count = status["selected_figure_count"]
            assert isinstance(count, int)
            status["selected_figure_count"] = count + 1
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
        _validate_report_dataset_payload(dataset)
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


def _materialize_rf_candidate_spectral_figures(
    *,
    staging: Path,
    temporary_root: Path,
    bundles: Mapping[str, _ComponentBundle],
    source_documents: Mapping[str, Mapping[str, object]],
    selected_event_ids: Sequence[str],
    existing_report_paths: set[str],
) -> list[dict[str, object]]:
    """Deriva series espectrales por candidato RF usando sólo rasters verificados."""
    fusion = bundles.get("disturbance_candidate_fusion")
    full = bundles.get("full_pipeline")
    if fusion is None or full is None or "fused_disturbance" not in source_documents:
        return []
    geojson_relative = "json/evidence/disturbance_events.geojson"
    geojson_artifact = fusion.artifacts.get(geojson_relative)
    if geojson_artifact is None:
        return []
    geojson_path = _verified_source(fusion.bundle, Path(geojson_relative), geojson_artifact)
    geojson = json.loads(geojson_path.read_text(encoding="utf-8"))
    features = {
        str(_mapping(feature.get("properties")).get("candidate_id")): feature
        for feature in _object_list(geojson.get("features"))
    }
    seasonal = sorted(
        (
            (match.group("period"), relative, artifact)
            for relative, artifact in full.artifacts.items()
            if (match := _SEASONAL_INDICES.fullmatch(relative)) is not None
        ),
        key=lambda item: _period_sort_key(item[0]),
    )
    if not seasonal:
        return []
    detection = _mapping(source_documents.get("detection"))
    configured_indices = tuple(
        item for item in _string_list(_mapping(detection.get("parameters")).get("indices")) if item
    )
    result: list[dict[str, object]] = []
    for candidate_id in selected_event_ids:
        report_path = f"report_assets/figures/events/{candidate_id}/disturbance.png"
        if report_path in existing_report_paths:
            continue
        feature = features.get(candidate_id)
        if feature is None:
            raise ValueError("rf_candidate_spectral_feature_missing")
        properties = _mapping(feature.get("properties"))
        geometry = _mapping(feature.get("geometry"))
        periods, series, index_names, ndvi_cube, mask, dependencies = _candidate_spectral_series(
            full=full,
            seasonal=seasonal,
            geometry=geometry,
            configured_indices=configured_indices,
            expected_pixel_count=properties.get(
                "candidate_pixel_count", properties.get("pixel_count")
            ),
            staging=staging,
        )
        content = _render_candidate_spectral_series(
            candidate_id=candidate_id,
            area_ha=properties.get("area_ha"),
            first_loss_year_range=properties.get("first_loss_year_range"),
            periods=periods,
            index_names=index_names,
            series=series,
            ndvi_cube=ndvi_cube,
            candidate_mask=mask,
        )
        relative = Path("figures/events") / candidate_id / "disturbance.png"
        target = temporary_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        result.append(
            {
                "category": "event_figure",
                "selection_reason": (
                    "comparación NDVI pre/post y serie espectral dentro del candidato RF-first"
                ),
                "component": "disturbance_candidate_fusion",
                "event_id": candidate_id,
                "source_bundle": fusion.bundle.relative_to(staging).as_posix(),
                "source_manifest": fusion.manifest_path.relative_to(staging).as_posix(),
                "source_manifest_sha256": fusion.manifest_sha256,
                "source_path": geojson_path.relative_to(staging).as_posix(),
                "source_sha256": digest,
                "size_bytes": len(content),
                "report_path": f"report_assets/{relative.as_posix()}",
                "report_sha256": digest,
                "derivation_sources": [
                    {
                        "path": geojson_path.relative_to(staging).as_posix(),
                        "sha256": str(geojson_artifact["sha256"]),
                    },
                    *dependencies,
                ],
            }
        )
    return result


def _candidate_spectral_series(
    *,
    full: _ComponentBundle,
    seasonal: Sequence[tuple[str, str, Mapping[str, object]]],
    geometry: Mapping[str, object],
    configured_indices: tuple[str, ...],
    expected_pixel_count: object,
    staging: Path,
) -> tuple[
    tuple[str, ...],
    np.ndarray[Any, np.dtype[np.float64]],
    tuple[str, ...],
    np.ndarray[Any, np.dtype[np.float64]],
    np.ndarray[Any, np.dtype[np.bool_]],
    list[dict[str, str]],
]:
    rows: list[list[float]] = []
    periods: list[str] = []
    dependencies: list[dict[str, str]] = []
    mask: np.ndarray[Any, np.dtype[np.bool_]] | None = None
    index_names: tuple[str, ...] | None = None
    reference_grid: tuple[object, ...] | None = None
    ndvi_layers: list[np.ndarray[Any, np.dtype[np.float64]]] = []
    for period, relative, artifact in seasonal:
        path = _verified_source(full.bundle, Path(relative), artifact)
        with rasterio.open(path) as dataset:
            grid = (dataset.crs, dataset.transform, dataset.width, dataset.height)
            if reference_grid is None:
                reference_grid = grid
                projected = transform_geom("EPSG:4326", dataset.crs, dict(geometry), precision=8)
                mask = geometry_mask(
                    [projected],
                    out_shape=(dataset.height, dataset.width),
                    transform=dataset.transform,
                    invert=True,
                    all_touched=False,
                )
                if not mask.any():
                    raise ValueError("rf_candidate_spectral_mask_empty")
                if (
                    isinstance(expected_pixel_count, int)
                    and int(mask.sum()) != expected_pixel_count
                ):
                    raise ValueError("rf_candidate_spectral_pixel_count_mismatch")
                available = tuple(name or "" for name in dataset.descriptions)
                index_names = tuple(name for name in configured_indices if name in available)
                if not index_names:
                    index_names = tuple(name for name in available if name)
                if not index_names:
                    raise ValueError("rf_candidate_spectral_indices_missing")
            elif grid != reference_grid:
                raise ValueError("rf_candidate_spectral_grid_mismatch")
            assert mask is not None and index_names is not None
            available_positions = {
                name or "": position + 1 for position, name in enumerate(dataset.descriptions)
            }
            values = dataset.read([available_positions[name] for name in index_names]).astype(
                np.float64
            )
            if dataset.nodata is not None:
                values[values == float(dataset.nodata)] = np.nan
            rows.append(
                [
                    float(np.nanmean(band[mask])) if np.isfinite(band[mask]).any() else math.nan
                    for band in values
                ]
            )
            if "NDVI" in index_names:
                ndvi_layers.append(values[index_names.index("NDVI")].copy())
        periods.append(period)
        dependencies.append(
            {"path": path.relative_to(staging).as_posix(), "sha256": str(artifact["sha256"])}
        )
    assert index_names is not None and mask is not None
    if "NDVI" not in index_names:
        raise ValueError("rf_candidate_spectral_ndvi_missing")
    return (
        tuple(periods),
        np.asarray(rows, dtype=np.float64),
        index_names,
        np.stack(ndvi_layers),
        mask,
        dependencies,
    )


def _render_candidate_spectral_series(
    *,
    candidate_id: str,
    area_ha: object,
    first_loss_year_range: object,
    periods: tuple[str, ...],
    index_names: tuple[str, ...],
    series: np.ndarray[Any, np.dtype[np.float64]],
    ndvi_cube: np.ndarray[Any, np.dtype[np.float64]],
    candidate_mask: np.ndarray[Any, np.dtype[np.bool_]],
) -> bytes:
    figure = Figure(figsize=(12, 8), constrained_layout=True)
    FigureCanvasAgg(figure)
    pre_axis, post_axis, delta_axis, series_axis = figure.subplots(2, 2).flat
    x = np.arange(len(periods))
    years = np.asarray([int(period[:4]) for period in periods])
    loss_range = (
        tuple(int(value) for value in first_loss_year_range)
        if isinstance(first_loss_year_range, list | tuple) and len(first_loss_year_range) == 2
        else ()
    )
    comparison = _select_rf_ndvi_comparison_indices(
        periods=periods,
        index_names=index_names,
        series=series,
        first_loss_year_range=first_loss_year_range,
    )
    row_indexes, column_indexes = np.nonzero(candidate_mask)
    row_min = max(0, int(np.min(row_indexes)) - 2)
    row_max = min(candidate_mask.shape[0], int(np.max(row_indexes)) + 3)
    column_min = max(0, int(np.min(column_indexes)) - 2)
    column_max = min(candidate_mask.shape[1], int(np.max(column_indexes)) + 3)
    crop = np.s_[row_min:row_max, column_min:column_max]
    if comparison is None:
        for axis, title in (
            (pre_axis, "NDVI pre-corte"),
            (post_axis, "NDVI post-corte"),
            (delta_axis, "Delta NDVI (post - pre)"),
        ):
            axis.text(0.5, 0.5, "Comparación no disponible", ha="center", va="center")
            axis.set_title(title)
    else:
        pre_index, post_index = comparison
        pre_values = ndvi_cube[pre_index]
        post_values = ndvi_cube[post_index]
        pre_axis.imshow(pre_values[crop], vmin=-1, vmax=1, cmap="RdYlGn")
        post_axis.imshow(post_values[crop], vmin=-1, vmax=1, cmap="RdYlGn")
        delta_axis.imshow((post_values - pre_values)[crop], vmin=-0.6, vmax=0.6, cmap="RdBu")
        for axis in (pre_axis, post_axis, delta_axis):
            axis.contour(candidate_mask[crop], levels=(0.5,), colors=("black",), linewidths=1)
        pre_axis.set_title(f"NDVI pre-corte: {periods[pre_index]}")
        post_axis.set_title(f"NDVI post-corte: {periods[post_index]}")
        delta_axis.set_title("Delta NDVI (post - pre)")
    for axis in (pre_axis, post_axis, delta_axis):
        axis.set_xticks([])
        axis.set_yticks([])
    for position, index_name in enumerate(index_names):
        series_axis.plot(
            x,
            series[:, position],
            marker="o",
            markersize=2.5,
            linewidth=1,
            label=index_name,
        )
    cutoff = np.flatnonzero(years == 2020)
    if cutoff.size:
        series_axis.axvline(float(cutoff[-1]) + 0.5, color="black", linestyle="--", linewidth=1)
    if loss_range:
        loss_positions = np.flatnonzero((years >= loss_range[0]) & (years <= loss_range[1]))
        if loss_positions.size:
            series_axis.axvspan(
                float(loss_positions[0]) - 0.4,
                float(loss_positions[-1]) + 0.4,
                color="#cc0000",
                alpha=0.12,
            )
    tick_step = max(1, len(periods) // 8)
    tick_positions = x[::tick_step]
    series_axis.set_xticks(tick_positions, [periods[item] for item in tick_positions])
    series_axis.tick_params(axis="x", rotation=45, labelsize=7)
    series_axis.set_ylim(-1.0, 1.0)
    series_axis.set_title("Serie media del candidato")
    series_axis.set_ylabel("Índice espectral")
    series_axis.grid(alpha=0.25)
    series_axis.legend(ncol=2, fontsize=7)
    area = f"{float(area_ha):.2f} ha" if isinstance(area_ha, int | float) else "área no disponible"
    figure.suptitle(
        f"{candidate_id} · {area} · serie media dentro del candidato RF-first\n"
        "Línea discontinua: corte EUDR · franja roja: rango anual de primera pérdida RF",
        fontsize=12,
    )
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=140, metadata={"Software": "matplotlib"})
    return buffer.getvalue()


def _select_rf_ndvi_comparison_indices(
    *,
    periods: tuple[str, ...],
    index_names: tuple[str, ...],
    series: np.ndarray[Any, np.dtype[np.float64]],
    first_loss_year_range: object,
) -> tuple[int, int] | None:
    """Elige el par estacional pre/post con mayor caída media de NDVI."""
    if "NDVI" not in index_names:
        return None
    loss_range = (
        tuple(int(value) for value in first_loss_year_range)
        if isinstance(first_loss_year_range, list | tuple) and len(first_loss_year_range) == 2
        else ()
    )
    ndvi_position = index_names.index("NDVI")
    candidates: list[tuple[float, int, int]] = []
    for post_index, post_period in enumerate(periods):
        post_year, post_season = post_period.split("-", maxsplit=1)
        year = int(post_year)
        if year <= 2020 or (loss_range and not loss_range[0] <= year <= loss_range[1]):
            continue
        pre_options = [
            index
            for index, period in enumerate(periods)
            if int(period[:4]) <= 2020 and period.split("-", maxsplit=1)[1] == post_season
        ]
        if not pre_options:
            continue
        pre_index = pre_options[-1]
        pre_value = series[pre_index, ndvi_position]
        post_value = series[post_index, ndvi_position]
        if np.isfinite(pre_value) and np.isfinite(post_value):
            candidates.append((float(post_value - pre_value), pre_index, post_index))
    if not candidates:
        return None
    _, pre_index, post_index = min(candidates, key=lambda item: (item[0], item[2]))
    return pre_index, post_index


def _period_sort_key(period: str) -> tuple[int, int]:
    year, season = period.split("-", maxsplit=1)
    return int(year), _SEASON_ORDER[season]


def _materialize_subthreshold_candidate_csv(
    *,
    public_files: list[dict[str, object]],
    temporary_root: Path,
    source_documents: Mapping[str, Mapping[str, object]],
) -> None:
    attribution = _attribution_records(source_documents.get("attribution", {}))
    fused = {
        str(item.get("candidate_id")): item
        for item in _object_list(source_documents.get("fused_disturbance", {}).get("candidates"))
    }
    persistence = {
        str(item.get("candidate_id", item.get("event_id"))): item
        for item in _object_list(source_documents.get("persistence", {}).get("events"))
    }
    rows: list[dict[str, object]] = []
    for record in attribution:
        candidate_id = str(record.get("candidate_id", record.get("event_id", "")))
        area_value = record.get("candidate_area_ha", record.get("area_ha"))
        if not candidate_id or not isinstance(area_value, int | float) or float(area_value) > 0.5:
            continue
        fused_record = fused.get(candidate_id, {})
        persistent = persistence.get(candidate_id, {})
        rows.append(
            {
                "candidate_id": candidate_id,
                "area_ha": f"{float(area_value):.8f}",
                "threshold_ha": "0.50000000",
                "estimated_onset_period_id": record.get(
                    "estimated_onset_period_id",
                    fused_record.get("estimated_onset_period_id", ""),
                ),
                "automatic_status": record.get("automatic_status", ""),
                "interpretation_status": record.get("interpretation_status", ""),
                "support_sources": "|".join(_string_list(fused_record.get("support_sources"))),
                "robust_support_fraction": fused_record.get("robust_support_fraction", ""),
                "ccdc_support_fraction": fused_record.get("ccdc_support_fraction", ""),
                "post_change_use": record.get("post_change_use", ""),
                "agricultural_signal_strength": persistent.get("signal_strength", ""),
                "qualifying_period_count": persistent.get("qualifying_period_count", ""),
                "candidate_agricultural_coverage_fraction": persistent.get(
                    "candidate_agricultural_coverage_fraction", ""
                ),
                "candidate_agricultural_coverage_threshold": persistent.get(
                    "candidate_agricultural_coverage_threshold", ""
                ),
                "candidate_agricultural_coverage_gate_met": persistent.get(
                    "candidate_agricultural_coverage_gate_met", ""
                ),
                "human_review_required": record.get("human_review_required", True),
            }
        )
    if not rows:
        return
    rows.sort(key=lambda row: (-float(str(row["area_ha"])), str(row["candidate_id"])))
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    content = stream.getvalue().encode("utf-8")
    relative = Path("annex/tables/subthreshold_candidates.csv")
    target = temporary_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    public_files.append(
        {
            "category": "annex_table",
            "selection_reason": "detalle íntegro de candidatos en o bajo el umbral operativo",
            "component": "post_change_attribution",
            "event_id": None,
            "source_bundle": None,
            "source_manifest": None,
            "source_manifest_sha256": None,
            "source_path": None,
            "source_sha256": digest,
            "size_bytes": len(content),
            "report_path": f"report_assets/{relative.as_posix()}",
            "report_sha256": digest,
        }
    )


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
            "available": status == "completed",
            "reason": _component_status_reason(component, status),
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


def _component_status_reason(component: Mapping[str, object], status: str) -> str | None:
    if status == "completed":
        return None
    error = component.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    if status == "skipped":
        return "dependency_not_completed"
    return status or "component_unavailable"


def _load_source_documents(
    bundles: Mapping[str, _ComponentBundle],
) -> dict[str, Mapping[str, object]]:
    wanted = {
        "full_summary": ("full_pipeline", "json/run/summary.json"),
        "baseline": ("full_pipeline", "json/evidence/forest_baseline_2020.json"),
        "detection": ("full_pipeline", "json/evidence/disturbance_detection.json"),
        "disturbance": ("full_pipeline", "json/evidence/disturbance_events.json"),
        "fused_disturbance": (
            "disturbance_candidate_fusion",
            "json/evidence/disturbance_candidate_fusion.json",
        ),
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
    return any(key in documents for key in ("attribution", "fused_disturbance", "disturbance"))


def _select_event_ids(documents: Mapping[str, Mapping[str, object]]) -> list[str]:
    attribution_document = documents.get("attribution", {})
    attribution_events = _attribution_records(attribution_document)
    likely = [
        event
        for event in attribution_events
        if event.get("automatic_status") == "conversion_likely"
    ]
    review = [
        event
        for event in attribution_events
        if event.get("automatic_status") == "review_required"
        and (
            event.get("candidate_footprint_above_visec_area_reference") is True
            or (
                attribution_document.get("schema_version") not in {"5.0.0", "6.0.0", "7.0.0"}
                and event.get("area_threshold_met") is True
            )
        )
    ]
    likely.sort(key=_event_sort_key)
    review.sort(key=_event_sort_key)
    selected = likely + review[: max(0, _MAX_REVIEW_EVENT_SHEETS - len(likely))]
    if not selected and not attribution_document:
        fused = _object_list(documents.get("fused_disturbance", {}).get("candidates"))
        disturbance_events = fused or _object_list(documents.get("disturbance", {}).get("events"))
        disturbance_events.sort(key=_event_sort_key)
        selected = disturbance_events[:5]
    result: list[str] = []
    seen: set[str] = set()
    for event in selected:
        event_id = event.get("candidate_id", event.get("event_id"))
        if isinstance(event_id, str) and event_id and event_id.casefold() not in seen:
            result.append(event_id)
            seen.add(event_id.casefold())
    return result


def _event_sort_key(event: Mapping[str, object]) -> tuple[float, str]:
    area = event.get("area_ha")
    numeric_area = float(area) if isinstance(area, (int, float)) else 0.0
    return (-numeric_area, str(event.get("candidate_id", event.get("event_id", ""))))


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


def _adapt_fused_disturbance_report_asset(
    *,
    public_files: list[dict[str, object]],
    temporary_root: Path,
    source_documents: Mapping[str, Mapping[str, object]],
) -> None:
    fused = source_documents.get("fused_disturbance")
    if fused is None:
        return
    candidates = [_adapt_fused_candidate(item) for item in _object_list(fused.get("candidates"))]
    payload = {
        "schema_version": "fusion-report-adapter-v1.0.0",
        "source_schema_version": fused.get("schema_version"),
        "event_count": len(candidates),
        "total_event_area_ha": round(
            sum(_candidate_area(item.get("area_ha")) for item in candidates),
            8,
        ),
        "events": candidates,
        "adapter_semantics": (
            "fused_candidates_for_review;missing_exact_onset_remains_explicitly_unavailable"
        ),
    }
    report_path = "report_assets/data/main/030_disturbance_events.json"
    matching = [item for item in public_files if item.get("report_path") == report_path]
    if len(matching) != 1 or matching[0].get("component") != "disturbance_candidate_fusion":
        raise ValueError("report_fused_disturbance_asset_missing_or_duplicated")
    target = temporary_root / "data/main/030_disturbance_events.json"
    _write_json(target, payload)
    matching[0]["size_bytes"] = target.stat().st_size
    matching[0]["report_sha256"] = _sha256(target)
    matching[0]["selection_reason"] = "adaptador explícito del dominio candidato RF-first"


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
    fused_disturbance = source_documents.get("fused_disturbance", {})
    detection = source_documents.get("detection", {})
    attribution = source_documents.get("attribution", {})
    baseline_metrics = _nested_mapping(baseline, "screening", "metrics")
    detection_screening = _mapping(detection.get("screening"))
    area = _mapping(full.get("area"))
    selected_events = [_event_record(event_id, source_documents) for event_id in selected_event_ids]
    fused_candidates = _object_list(fused_disturbance.get("candidates"))
    fused_candidate_area_ha = (
        round(
            sum(_candidate_area(candidate.get("area_ha")) for candidate in fused_candidates),
            8,
        )
        if fused_disturbance
        else None
    )
    candidate_count = len(fused_candidates) if fused_disturbance else disturbance.get("event_count")
    candidate_area_ha = (
        fused_candidate_area_ha if fused_disturbance else disturbance.get("total_event_area_ha")
    )
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
    attribution_available = _component_available(component_statuses, "post_change_attribution")
    unavailable_limitations = _component_unavailability_limitations(component_statuses)
    attribution_inventory_path = (
        "report_assets/data/main/080_post_change_attribution.json"
        if attribution_available
        else None
    )
    candidate_inventory_path = next(
        (
            str(item["report_path"])
            for item in files
            if item.get("report_path") == "report_assets/data/main/030_disturbance_events.json"
        ),
        None,
    )
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
            "spectral_candidate_area_ha": candidate_area_ha,
            "spectral_candidate_count": candidate_count,
            "candidate_episode_area_ha": (
                fused_candidate_area_ha
                if fused_disturbance
                else disturbance.get(
                    "above_visec_area_reference_candidate_area_ha",
                    disturbance.get(
                        "operational_event_area_ha", disturbance.get("total_event_area_ha")
                    ),
                )
            ),
            "candidate_episode_count": (
                len(fused_candidates)
                if fused_disturbance
                else disturbance.get(
                    "above_visec_area_reference_candidate_count",
                    disturbance.get("operational_event_count", disturbance.get("event_count")),
                )
            ),
            "likely_conversion_area_ha": attribution.get("likely_conversion_area_ha"),
            "conversion_likely_count": attribution.get("conversion_likely_count"),
            "automatic_final_assessment_generated": detection_screening.get(
                "final_assessment_generated"
            ),
        },
        "selected_disturbances": selected_events,
        "disturbance_selection": {
            "policy": (
                "all_conversion_likely_events_then_persistent_unattributed_or_insufficient_"
                "candidates_over_area_threshold_up_to_10"
            ),
            "selected_candidate_ids": list(selected_event_ids),
            "complete_candidate_inventory_path": (
                attribution_inventory_path or candidate_inventory_path
            ),
            "complete_event_inventory_path": (attribution_inventory_path),
        },
        "component_statuses": component_statuses,
        "source_paths_by_category": source_paths,
        "limitations": [
            "Resultado técnico automático sujeto a revisión humana.",
            "No constituye certificación ni confirmación legal EUDR.",
            *inherited_limitations,
            *unavailable_limitations,
        ],
    }


def _component_available(component_statuses: Mapping[str, Mapping[str, object]], name: str) -> bool:
    component = component_statuses.get(name)
    return component is not None and component.get("status") == "completed"


def _component_unavailability_limitations(
    component_statuses: Mapping[str, Mapping[str, object]],
) -> list[str]:
    labels = {
        "agricultural_collection": "La recolección agrícola",
        "agricultural_persistence": "La evaluación de persistencia agrícola",
        "post_change_attribution": "La atribución post-cambio",
    }
    limitations: list[str] = []
    for name, label in labels.items():
        component = component_statuses.get(name)
        if component is None or component.get("status") == "completed":
            continue
        status = str(component.get("status", "unavailable"))
        reason = str(component.get("reason", status))
        limitations.append(f"{label} no está disponible ({status}: {reason}).")
    attribution = component_statuses.get("post_change_attribution")
    if attribution is not None and attribution.get("status") != "completed":
        limitations.append(
            "No es posible concluir ausencia ni presencia de conversión porque la "
            "atribución post-cambio no se completó."
        )
    return limitations


def _event_record(
    event_id: str, documents: Mapping[str, Mapping[str, object]]
) -> dict[str, object]:
    fused = _find_fused_candidate(documents.get("fused_disturbance"), event_id)
    disturbance = (
        _adapt_fused_candidate(fused)
        if fused is not None
        else _find_event(documents.get("disturbance"), event_id)
    )
    return {
        "candidate_id": event_id,
        "disturbance_source": (
            "rf_first_candidate_domain" if fused is not None else "historical_robust"
        ),
        "disturbance": disturbance,
        "fusion_candidate": fused,
        "agricultural_collection": _find_event(documents.get("agriculture"), event_id),
        "agricultural_persistence_schema_version": documents.get("persistence", {}).get(
            "schema_version"
        ),
        "agricultural_persistence": _find_event(documents.get("persistence"), event_id),
        "attribution": _find_event(documents.get("attribution"), event_id),
    }


def _find_fused_candidate(
    document: Mapping[str, object] | None, event_id: str
) -> Mapping[str, object] | None:
    if document is None:
        return None
    for candidate in _object_list(document.get("candidates")):
        candidate_id = candidate.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id.casefold() == event_id.casefold():
            return candidate
    return None


def _adapt_fused_candidate(candidate: Mapping[str, object]) -> dict[str, object]:
    candidate_id = candidate.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("report_fused_candidate_id_invalid")
    onset_range = candidate.get("robust_onset_period_range")
    rf_year_range = candidate.get(
        "first_loss_year_range", candidate.get("rf_persistent_loss_year_range")
    )
    onset_period_id = (
        f"robust-period-index-{onset_range[-1]}"
        if isinstance(onset_range, list) and len(onset_range) == 2
        else (
            f"RF-{rf_year_range[0]}-{rf_year_range[-1]}-onset-undetermined"
            if isinstance(rf_year_range, list) and len(rf_year_range) == 2
            else "onset-undetermined"
        )
    )
    area = _candidate_area(candidate.get("area_ha"))
    return {
        "event_id": candidate_id,
        "candidate_id": candidate_id,
        "record_type": "persistent_disturbance_candidate",
        "interpretation_level": "candidate_episode",
        "automatic_status": "review_required",
        "area_ha": area,
        "area_threshold_ha": 0.5,
        "area_threshold_met": area > 0.5,
        "candidate_footprint_above_visec_area_reference": area > 0.5,
        "pixel_count": candidate.get("pixel_count"),
        "estimated_onset_period_id": onset_period_id,
        "estimated_onset_window_start": None,
        "estimated_onset_window_end": None,
        "onset_available": False,
        "sources": _candidate_sources(candidate),
        "segmentation_source": candidate.get("segmentation_source"),
        "support_sources": candidate.get("support_sources"),
        "resolution": candidate.get("resolution"),
        "quality_flags": ["fused_candidate_report_adapter", "exact_onset_unavailable"],
    }


def _candidate_sources(candidate: Mapping[str, object]) -> object:
    legacy = candidate.get("sources")
    if legacy is not None:
        return legacy
    segmentation = candidate.get("segmentation_source")
    support = candidate.get("support_sources")
    values = [segmentation] if isinstance(segmentation, str) else []
    if isinstance(support, list):
        values.extend(item for item in support if isinstance(item, str))
    return values


def _candidate_area(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("report_fused_candidate_area_invalid")
    return float(value)


def _find_event(
    document: Mapping[str, object] | None, event_id: str
) -> Mapping[str, object] | None:
    if document is None:
        return None
    records = (
        _attribution_records(document)
        if "records" in document
        else _object_list(document.get("events"))
    )
    for event in records:
        candidate = event.get("candidate_id", event.get("event_id"))
        if isinstance(candidate, str) and candidate.casefold() == event_id.casefold():
            return event
    return None


def _attribution_records(document: Mapping[str, object]) -> list[Mapping[str, object]]:
    records = document.get("records")
    if isinstance(records, list):
        return _object_list(records)
    return _object_list(document.get("events"))


def _object_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def load_report_dataset_document(path: Path) -> dict[str, object]:
    """Lee versiones históricas o la actual sin actualizarlas implícitamente."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("report_dataset_root_must_be_mapping")
    version = payload.get("schema_version")
    if version not in {
        LEGACY_REPORT_DATASET_SCHEMA_VERSION,
        PRIOR_REPORT_DATASET_SCHEMA_VERSION,
        REPORT_DATASET_SCHEMA_VERSION,
    }:
        raise ValueError("report_dataset_schema_version_unsupported")
    if version == REPORT_DATASET_SCHEMA_VERSION:
        _validate_report_dataset_payload(payload)
    return payload


def _validate_report_dataset_payload(payload: Mapping[str, object]) -> None:
    if payload.get("schema_version") != REPORT_DATASET_SCHEMA_VERSION:
        raise ValueError("report_dataset_current_schema_version_required")
    required = report_dataset_json_schema()["required"]
    if not all(key in payload for key in required):
        raise ValueError("report_dataset_required_field_missing")
    selected = payload.get("selected_disturbances")
    if not isinstance(selected, list):
        raise ValueError("report_dataset_selected_disturbances_invalid")
    for raw in selected:
        if not isinstance(raw, dict):
            raise ValueError("report_dataset_selected_disturbance_invalid")
        required_record = report_dataset_json_schema()["$defs"]["selectedDisturbance"]["required"]
        if not all(key in raw for key in required_record):
            raise ValueError("report_dataset_selected_disturbance_field_missing")
        if raw.get("agricultural_persistence_schema_version") in {"2.0.0", "2.1.0"}:
            persistence = raw.get("agricultural_persistence")
            if persistence is not None:
                if not isinstance(persistence, dict) or not all(
                    key in persistence
                    for key in (
                        "signal_strength",
                        "effective_observation_date",
                        "qualifying_period_count",
                        "signal_strength_areas",
                    )
                ):
                    raise ValueError("report_dataset_agricultural_occurrence_fields_missing")


def report_dataset_json_schema() -> dict[str, Any]:
    """Contrato editorial v2; los subdocumentos científicos conservan su versión propia."""
    nullable_object = {"type": ["object", "null"]}
    selected_properties = {
        "candidate_id": {"type": "string", "minLength": 1},
        "disturbance_source": {"enum": ["historical_robust", "rf_first_candidate_domain"]},
        "disturbance": nullable_object,
        "fusion_candidate": nullable_object,
        "agricultural_collection": nullable_object,
        "agricultural_persistence_schema_version": {"type": ["string", "null"]},
        "agricultural_persistence": nullable_object,
        "attribution": nullable_object,
    }
    occurrence_fields = {
        "signal_strength": {"enum": ["weak", "moderate", "strong", None]},
        "effective_observation_date": {
            "type": ["string", "null"],
            "format": "date",
        },
        "qualifying_period_count": {"type": "integer", "minimum": 0},
        "signal_strength_areas": {
            "type": "array",
            "items": {"$ref": "#/$defs/agriculturalStrengthArea"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": REPORT_DATASET_SCHEMA_ID,
        "title": "Report dataset",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "recorded_at",
            "analysis",
            "headline_metrics",
            "selected_disturbances",
            "disturbance_selection",
            "component_statuses",
            "source_paths_by_category",
            "limitations",
        ],
        "properties": {
            "schema_version": {"const": REPORT_DATASET_SCHEMA_VERSION},
            "recorded_at": {"type": "string", "format": "date-time"},
            "analysis": {"type": "object"},
            "headline_metrics": {"type": "object"},
            "selected_disturbances": {
                "type": "array",
                "items": {"$ref": "#/$defs/selectedDisturbance"},
            },
            "disturbance_selection": {"type": "object"},
            "component_statuses": {"type": "object"},
            "source_paths_by_category": {"type": "object"},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "$defs": {
            "selectedDisturbance": {
                "type": "object",
                "additionalProperties": False,
                "required": list(selected_properties),
                "properties": selected_properties,
                "allOf": [
                    {
                        "if": {
                            "properties": {
                                "agricultural_persistence_schema_version": {
                                    "enum": ["2.0.0", "2.1.0"]
                                }
                            },
                            "required": ["agricultural_persistence_schema_version"],
                        },
                        "then": {
                            "properties": {
                                "agricultural_persistence": {
                                    "anyOf": [
                                        {"type": "null"},
                                        {"$ref": "#/$defs/agriculturalOccurrence"},
                                    ]
                                }
                            }
                        },
                    }
                ],
            },
            "agriculturalOccurrence": {
                "type": "object",
                "required": list(occurrence_fields),
                "properties": occurrence_fields,
            },
            "agriculturalStrengthArea": {
                "type": "object",
                "required": ["signal_strength", "area_ha", "event_fraction"],
                "properties": {
                    "signal_strength": {"enum": ["weak", "moderate", "strong"]},
                    "area_ha": {"type": "number", "minimum": 0},
                    "event_fraction": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "x-schema-version": REPORT_DATASET_SCHEMA_VERSION,
        "x-readable-historical-versions": [
            LEGACY_REPORT_DATASET_SCHEMA_VERSION,
            PRIOR_REPORT_DATASET_SCHEMA_VERSION,
        ],
    }


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
