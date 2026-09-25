"""Contrato y renderer del informe técnico, sin pipeline ni HTTP."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from deforestation_reporting import (
    LEGAL_DISCLAIMER,
    ReportContractError,
    ReportIntegrityError,
    build_report_view_model,
    load_report_package,
    render_technical_appendix,
    render_technical_report,
)
from deforestation_reporting.basemap import (
    MapBounds,
    OSMBasemap,
    OSMFeature,
    OverpassOSMProvider,
)
from deforestation_reporting.graphics import _event_color, agricultural_support_map
from deforestation_reporting.renderer import _secondary_event_annex, _styles
from deforestation_reporting.source import (
    _attribution_interpretation_status,
    _attribution_record_type,
    _events,
    _headline_event_metrics,
)


def _outline_titles(items: object) -> list[str]:
    titles: list[str] = []
    if not isinstance(items, list):
        return titles
    for item in items:
        if isinstance(item, list):
            titles.extend(_outline_titles(item))
        elif hasattr(item, "title"):
            titles.append(str(item.title))
    return titles


def _outline_hierarchy(items: object, *, level: int = 0) -> list[tuple[int, str]]:
    hierarchy: list[tuple[int, str]] = []
    if not isinstance(items, list):
        return hierarchy
    for item in items:
        if isinstance(item, list):
            hierarchy.extend(_outline_hierarchy(item, level=level + 1))
        elif hasattr(item, "title"):
            hierarchy.append((level, str(item.title)))
    return hierarchy


class _StaticOSMProvider:
    def get(self, bounds: MapBounds) -> OSMBasemap:
        minimum_x, minimum_y, maximum_x, maximum_y = bounds
        return OSMBasemap(
            bounds=bounds,
            features=(
                OSMFeature(
                    kind="road",
                    geometry=(
                        (minimum_x, minimum_y),
                        (maximum_x, maximum_y),
                    ),
                ),
            ),
        )


def _static_osm_provider() -> _StaticOSMProvider:
    return _StaticOSMProvider()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _replace_verified_json(root: Path, relative: str, payload: object) -> None:
    path = root / "report_assets" / relative
    _write_json(path, payload)
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text("utf-8"))
    report_path = f"report_assets/{relative}"
    item = next(entry for entry in index["files"] if entry["report_path"] == report_path)
    item["report_sha256"] = _sha256(path)
    item["size_bytes"] = path.stat().st_size
    _write_json(index_path, index)


def _set_v2_agricultural_occurrence(
    root: Path,
    *,
    signal_strength: str,
    qualifying_period_count: int,
    strength_areas: list[dict[str, object]],
) -> None:
    relative = "data/main/070_agricultural_persistence.json"
    path = root / "report_assets" / relative
    payload = json.loads(path.read_text("utf-8"))
    payload["schema_version"] = "2.0.0"
    payload["persistence_rule"] = "1_qualifying_post_event_period"
    event = payload["events"][0]
    event.update(
        {
            "persistence_status": "agricultural_support_detected",
            "signal_strength": signal_strength,
            "effective_observation_date": "2022-06-30",
            "qualifying_period_count": qualifying_period_count,
            "signal_strength_areas": strength_areas,
        }
    )
    for not_detected in payload["events"][1:]:
        not_detected.update(
            {
                "persistence_status": "not_detected",
                "signal_strength": None,
                "effective_observation_date": None,
                "signal_strength_areas": [],
            }
        )
    _replace_verified_json(root, relative, payload)
    dataset_path = root / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text("utf-8"))
    dataset["schema_version"] = "2.0.0"
    _write_json(dataset_path, dataset)
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text("utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)


def _set_v2_1_candidate_coverage(root: Path) -> None:
    _set_v2_agricultural_occurrence(
        root,
        signal_strength="moderate",
        qualifying_period_count=2,
        strength_areas=[],
    )
    relative = "data/main/070_agricultural_persistence.json"
    path = root / "report_assets" / relative
    payload = json.loads(path.read_text("utf-8"))
    payload["schema_version"] = "2.1.0"
    payload["events"][0].update(
        {
            "candidate_agricultural_coverage_fraction": 0.071,
            "candidate_agricultural_coverage_threshold": 0.05,
            "candidate_agricultural_coverage_gate_met": True,
        }
    )
    _replace_verified_json(root, relative, payload)


def _write_figure(
    path: Path, label: str, color: str, *, size: tuple[int, int] = (1200, 700)
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    ImageDraw.Draw(image).text((60, 60), label, fill="white")
    image.save(path, format="PNG")


def _report_package(tmp_path: Path, *, with_events: bool = True) -> Path:
    root = tmp_path / "result"
    report = root / "report_assets"
    declared_geometry = {
        "type": "Feature",
        "properties": {"establishment_id": "establecimiento-sintetico"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-60.08, -31.90],
                    [-60.01, -31.90],
                    [-60.01, -31.84],
                    [-60.08, -31.84],
                    [-60.08, -31.90],
                ]
            ],
        },
    }
    input_path = tmp_path / "input.geojson"
    _write_json(input_path, declared_geometry)
    _write_json(
        root / "run_manifest.json",
        {
            "analysis_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "input": {
                "path": "private/analysis/input.geojson",
                "sha256": _sha256(input_path),
                "size_bytes": input_path.stat().st_size,
            },
        },
    )
    overview = report / "figures" / "main" / "010_overview.png"
    rgb_timeline = report / "figures" / "main" / "030_rgb_timeline.png"
    rf_timeline = report / "figures" / "main" / "200_rf_forest_deltas_timeline.png"
    dw_annual = report / "figures" / "main" / "300_dynamic_world_annual_land_cover.png"
    _write_figure(overview, "Resumen espacial", "#1d4ed8", size=(2400, 600))
    _write_figure(rgb_timeline, "Serie RGB", "#365314", size=(1400, 1000))
    _write_figure(rf_timeline, "Cobertura RF anual", "#166534", size=(1600, 950))
    _write_figure(dw_annual, "Cobertura Dynamic World anual", "#e49635", size=(1600, 950))
    event_figure = report / "figures" / "events" / "PDE-1" / "disturbance.png"
    if with_events:
        _write_figure(event_figure, "Evento PDE-1", "#b45309")
    dataset: dict[str, Any] = {
        "schema_version": "1.1.0",
        "recorded_at": "2026-08-18T15:00:00+00:00",
        "analysis": {
            "analysis_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "full_pipeline_analysis_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "establishment_id": "establecimiento-sintetico",
            "analysis_end_date": "2025-12-31",
            "requested_analysis_end_date": "2025-12-31",
            "overall_status": "partial",
        },
        "headline_metrics": {
            "establishment_area_ha": 1438.42,
            "forest_area_2020_ha": 178.83,
            "detected_event_area_ha": 17.1 if with_events else 0.0,
            "detected_event_count": 3 if with_events else 0,
            "candidate_event_area_ha": 17.28 if with_events else 0.0,
            "candidate_event_count": 4 if with_events else 0,
            "likely_conversion_area_ha": 8.09 if with_events else 0.0,
            "conversion_likely_count": 1 if with_events else 0,
            "automatic_final_assessment_generated": False,
        },
        "component_statuses": {
            "full_pipeline": {"status": "completed", "selected_figure_count": 1},
            "hampel_benchmark": {
                "status": "diagnostic_unavailable",
                "selected_figure_count": 0,
            },
            "post_change_attribution": {"status": "completed", "selected_figure_count": 1},
        },
        "selected_events": [
            {
                "event_id": "PDE-1",
                "disturbance": {
                    "area_ha": 10.5,
                    "estimated_onset_period_id": "2022-SON",
                    "quality_flags_union": 0,
                },
                "agricultural_collection": {"window_count": 36},
                "agricultural_persistence": {
                    "valid_period_count": 35,
                    "per_period_support": [
                        {"window_id": f"2024-{month:02d}"} for month in range(1, 13)
                    ],
                },
                "attribution": {
                    "automatic_status": "conversion_likely",
                    "area_ha": 10.5,
                    "likely_conversion_area_ha": 8.09,
                    "post_change_use": "cropland",
                    "confidence": None,
                    "confidence_semantics": "not_calibrated_in_v1",
                    "human_review_required": True,
                    "conversion_confirmed": False,
                    "reasons_for": ["gate_passed:forest_at_cutoff", "gate_passed:post_cutoff_loss"],
                    "reasons_against": ["review_required:independent_evidence_missing"],
                    "quality_flags": ["single_source_no_cross_source_disagreement_assessment"],
                    "gates": {
                        "forest_at_cutoff": True,
                        "post_cutoff_loss": True,
                        "persistent_change": True,
                        "agricultural_or_livestock_post_use": True,
                        "defensible_area_and_geometry": True,
                        "strong_alternative_explanation_absent": True,
                        "all_conversion_likely_gates": True,
                    },
                    "trajectory": {
                        "years": [2020, 2021, 2022, 2023, 2024],
                        "forest_fraction": [1.0, 1.0, 0.4, 0.1, 0.05],
                    },
                },
            }
        ],
        "event_selection": {"selected_event_ids": ["PDE-1"]},
        "source_paths_by_category": {},
        "limitations": [
            "Resultado técnico automático sujeto a revisión humana.",
            "No constituye certificación ni confirmación legal EUDR.",
            "Dynamic World es la única fuente temporal agrícola operativa.",
        ],
    }
    if not with_events:
        dataset["selected_events"] = []
        dataset["event_selection"] = {"selected_event_ids": []}
    dataset_path = report / "report_dataset.json"
    _write_json(dataset_path, dataset)
    files = []
    for category, event_id, reason, path in (
        ("main_figure", None, "línea base y perturbaciones", overview),
        ("main_figure", None, "contexto RGB multitemporal acotado", rgb_timeline),
        ("main_figure", None, "trayectoria y deltas forestales RF", rf_timeline),
        ("main_figure", None, "cobertura anual Dynamic World", dw_annual),
        *(
            (("event_figure", "PDE-1", "detalle de perturbación", event_figure),)
            if with_events
            else ()
        ),
    ):
        files.append(
            {
                "category": category,
                "component": "full_pipeline",
                "event_id": event_id,
                "selection_reason": reason,
                "report_path": path.relative_to(root).as_posix(),
                "report_sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )

    selected_attribution = (
        dataset["selected_events"][0]["attribution"]
        if with_events
        else {"gates": {}, "trajectory": {}}
    )
    structured_json = {
        "data/main/010_full_pipeline_summary.json": {
            "geometry": {
                "analysis_geometry_type": "Polygon",
                "normalized_crs": "EPSG:4326",
                "repair_performed": False,
                "quality_flags": [],
                "warnings": [],
            },
            "area": {
                "total_area_ha": 1438.42,
                "calculation_crs": "EPSG:6933",
                "method": "planar_area_after_pyproj_transform",
                "threshold_ha": 0.5,
            },
            "datasets": [
                {
                    "dataset_id": "hls_s30_v2",
                    "provider": "NASA LP DAAC",
                    "collection": "NASA/HLS/HLSS30/v002",
                    "version": "2.0",
                    "access_date": "2026-08-17",
                    "license": "NASA full and open data sharing",
                }
            ],
            "gee": {
                "period_ids": [
                    f"{year}-{season}"
                    for year in range(2020, 2026)
                    for season in ("DJF", "MAM", "JJA", "SON")
                ]
            },
        },
        "data/main/020_forest_baseline_2020.json": {
            "screening": {
                "metrics": {
                    "aoi_raster_area_ha": 1462.32,
                    "automated_evaluable_area_ha": 1286.82,
                    "automated_forest_area_ha": 178.83,
                    "automated_nonforest_area_ha": 1107.99,
                    "review_required_area_ha": 175.5,
                    "insufficient_data_area_ha": 0.0,
                }
            }
        },
        "data/main/030_disturbance_events.json": {
            "area_threshold_event_count": 1,
            "below_area_threshold_event_count": 1,
            "event_count": 2,
            "total_event_area_ha": 10.68,
            "events": [
                {
                    "event_id": "PDE-1",
                    "area_ha": 10.5,
                    "area_threshold_ha": 0.5,
                    "area_threshold_met": True,
                    "estimated_onset_period_id": "2022-SON",
                    "estimated_onset_window_start": "2022-09-01",
                    "estimated_onset_window_end": "2022-11-30",
                    "dual_detector_agreement_pixel_count": 100,
                    "pixel_count": 100,
                },
                {
                    "event_id": "PDE-2",
                    "area_ha": 0.18,
                    "area_threshold_ha": 0.5,
                    "area_threshold_met": False,
                    "estimated_onset_period_id": "2023-MAM",
                    "estimated_onset_window_start": "2023-03-01",
                    "estimated_onset_window_end": "2023-05-31",
                    "dual_detector_agreement_pixel_count": 2,
                    "pixel_count": 2,
                },
            ],
        },
        "data/main/070_agricultural_persistence.json": {
            "persistence_rule": "2_qualifying_of_3_valid_periods_over_180_days_nonconsecutive",
            "primary_crop_observation_fraction": 0.5,
            "events": [
                {
                    "event_id": "PDE-1",
                    "persistence_status": "persistent_crop_support",
                    "valid_period_count": 35,
                    "qualifying_period_count": 2,
                    "gap_period_count": 1,
                    "persistent_crop_area_ha": 8.12,
                    "persistent_event_fraction": 0.7733,
                    "sensitivity": [
                        {
                            "crop_observation_fraction_threshold": 0.4,
                            "persistent_crop_area_ha": 9.0,
                        },
                        {
                            "crop_observation_fraction_threshold": 0.5,
                            "persistent_crop_area_ha": 8.12,
                        },
                        {
                            "crop_observation_fraction_threshold": 0.6,
                            "persistent_crop_area_ha": 6.7,
                        },
                    ],
                    "quality_flags": ["single_source_no_cross_source_disagreement_assessment"],
                },
                {
                    "event_id": "PDE-2",
                    "persistence_status": "not_persistent",
                    "valid_period_count": 20,
                    "qualifying_period_count": 0,
                    "gap_period_count": 0,
                    "persistent_crop_area_ha": 0.0,
                    "persistent_event_fraction": 0.0,
                    "sensitivity": [],
                    "quality_flags": [],
                },
            ],
        },
        "data/main/080_post_change_attribution.json": {
            "status": "review_required",
            "automatic_final_assessment_generated": False,
            "events": [
                {
                    "event_id": "PDE-1",
                    "automatic_status": "conversion_likely",
                    "area_ha": 10.5,
                    "area_threshold_met": True,
                    "estimated_onset_period_id": "2022-SON",
                    "post_change_use": "agriculture_likely",
                    "persistent_agricultural_area_ha": 8.12,
                    "likely_conversion_area_ha": 8.09,
                    "human_review_required": True,
                    "conversion_confirmed": False,
                    "confidence": None,
                    "confidence_semantics": "not_calibrated_in_v1",
                    "gates": selected_attribution["gates"],
                    "trajectory": selected_attribution["trajectory"],
                    "quality_flags": [],
                },
                {
                    "event_id": "PDE-2",
                    "automatic_status": "review_required",
                    "area_ha": 0.18,
                    "area_threshold_met": False,
                    "estimated_onset_period_id": "2023-MAM",
                    "post_change_use": "unknown",
                    "persistent_agricultural_area_ha": 0.0,
                    "likely_conversion_area_ha": 0.0,
                    "human_review_required": True,
                    "conversion_confirmed": False,
                    "confidence": None,
                    "confidence_semantics": "not_calibrated_in_v1",
                    "gates": {
                        "forest_at_cutoff": True,
                        "post_cutoff_loss": True,
                        "persistent_change": False,
                        "agricultural_or_livestock_post_use": False,
                        "defensible_area_and_geometry": False,
                        "strong_alternative_explanation_absent": True,
                        "all_conversion_likely_gates": False,
                    },
                    "trajectory": {
                        "years": [2020, 2021, 2022, 2023, 2024],
                        "forest_fraction": [1.0, 1.0, 1.0, 0.5, 0.4],
                    },
                    "quality_flags": [],
                },
            ],
        },
        "annex/methodology/agricultural_evidence_metadata.json": {
            "source": {
                "source_id": "dynamic_world_v1",
                "provider": "Google / World Resources Institute",
                "collection": "GOOGLE/DYNAMICWORLD/V1",
                "version": "1",
                "access_date": "2026-08-11",
                "license": "CC-BY-4.0",
                "spatial_resolution_m": 10,
                "evidence_role": "candidate_independent",
            }
        },
        "annex/spatial/disturbance_events.geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"event_id": "PDE-1"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-60.04, -31.88], [-60.03, -31.88], [-60.03, -31.87], [-60.04, -31.88]]
                        ],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {"event_id": "PDE-2"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-60.06, -31.86],
                                [-60.059, -31.86],
                                [-60.059, -31.859],
                                [-60.06, -31.86],
                            ]
                        ],
                    },
                },
            ],
        },
        "annex/spatial/post_change_attribution.geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "event_id": "PDE-1",
                        "agricultural_use_evidence": [
                            {
                                "observation": {
                                    "geometry": {
                                        "type": "Polygon",
                                        "coordinates": [
                                            [
                                                [-60.039, -31.879],
                                                [-60.032, -31.879],
                                                [-60.032, -31.872],
                                                [-60.039, -31.879],
                                            ]
                                        ],
                                    }
                                }
                            }
                        ],
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-60.04, -31.88], [-60.03, -31.88], [-60.03, -31.87], [-60.04, -31.88]]
                        ],
                    },
                }
            ],
        },
    }
    if not with_events:
        structured_json["data/main/030_disturbance_events.json"] = {
            "area_threshold_event_count": 0,
            "below_area_threshold_event_count": 0,
            "event_count": 0,
            "total_event_area_ha": 0.0,
            "events": [],
        }
        structured_json["data/main/070_agricultural_persistence.json"] = {
            "persistence_rule": "2_qualifying_of_3_valid_periods_over_180_days_nonconsecutive",
            "primary_crop_observation_fraction": 0.5,
            "events": [],
        }
        structured_json["data/main/080_post_change_attribution.json"] = {
            "status": "low_risk",
            "automatic_final_assessment_generated": False,
            "records": [],
        }
        structured_json["annex/spatial/disturbance_events.geojson"] = {
            "type": "FeatureCollection",
            "features": [],
        }
        structured_json["annex/spatial/post_change_attribution.geojson"] = {
            "type": "FeatureCollection",
            "features": [],
        }
    for relative, payload in structured_json.items():
        path = report / relative
        _write_json(path, payload)
        files.append(
            {
                "category": "annex_spatial" if relative.endswith(".geojson") else "main_data",
                "component": "full_pipeline",
                "event_id": None,
                "selection_reason": None,
                "report_path": path.relative_to(root).as_posix(),
                "report_sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )

    csv_payloads = {
        "annex/tables/subthreshold_candidates.csv": [
            {
                "candidate_id": "PDE-2",
                "area_ha": 0.18,
                "threshold_ha": 0.5,
                "estimated_onset_period_id": "2023-MAM",
                "automatic_status": "review_required",
                "agricultural_signal_strength": "",
                "qualifying_period_count": 0,
                "human_review_required": True,
            }
        ],
        "annex/tables/seasonal_summary.csv": [
            {
                "period_id": "2020-DJF",
                "season_year": 2020,
                "season": "DJF",
                "variable_kind": "index",
                "variable": "NDVI",
                "roi_pixel_count": 100,
                "valid_pixel_count": 100,
                "valid_pixel_fraction": 1.0,
                "p10": 0.1,
                "median": 0.5,
                "p90": 0.8,
            },
            {
                "period_id": "2020-MAM",
                "season_year": 2020,
                "season": "MAM",
                "variable_kind": "index",
                "variable": "NDVI",
                "roi_pixel_count": 100,
                "valid_pixel_count": 99,
                "valid_pixel_fraction": 0.99,
                "p10": 0.1,
                "median": 0.5,
                "p90": 0.8,
            },
        ],
        "annex/tables/disturbance_period_summary.csv": [
            {
                "period_index": 0,
                "period_id": "2021-MAM",
                "start_date": "2021-03-01",
                "end_date_exclusive": "2021-06-01",
                "valid_pixel_count": 100,
                "mean_valid_observation_count": 15.0,
                "signal_pixel_count": 2,
                "cutoff_straddling_excluded": False,
                "signal_threshold": 3.0,
                "minimum_index_support_count": 2,
            },
            {
                "period_index": 1,
                "period_id": "2021-JJA",
                "start_date": "2021-06-01",
                "end_date_exclusive": "2021-09-01",
                "valid_pixel_count": 100,
                "mean_valid_observation_count": 42.0,
                "signal_pixel_count": 4,
                "cutoff_straddling_excluded": False,
                "signal_threshold": 3.0,
                "minimum_index_support_count": 2,
            },
        ],
    }
    for relative, rows in csv_payloads.items():
        path = report / relative
        _write_csv(path, rows)
        files.append(
            {
                "category": "annex_table",
                "component": "full_pipeline",
                "event_id": None,
                "selection_reason": None,
                "report_path": path.relative_to(root).as_posix(),
                "report_sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    _write_json(
        report / "index.json",
        {
            "schema_version": "2.0.0",
            "dataset_path": "report_assets/report_dataset.json",
            "dataset_sha256": _sha256(dataset_path),
            "files": files,
        },
    )
    return root


def test_builds_bounded_typed_view_model_without_raw_monthly_rows(tmp_path: Path) -> None:
    package = load_report_package(_report_package(tmp_path))

    view = build_report_view_model(package)

    assert view.analysis.establishment_id == "establecimiento-sintetico"
    assert view.metrics.candidate_episode_count == 3
    assert view.metrics.spectral_candidate_count == 4
    assert view.components[1].status == "diagnostic_unavailable"
    assert view.events[0].automatic_status == "conversion_likely"
    assert view.events[0].record_type == "conversion_likely_event"
    assert view.events[0].interpretation_level == "event"
    assert view.events[0].interpretation_status == "conversion_likely"
    assert view.events[0].candidate_id == "PDE-1"
    assert view.events[0].event_id == "PDE-1"
    assert view.events[0].observed_period_count == 35
    assert view.events[0].human_review_required is True
    assert view.result_status == "review_required"
    assert view.baseline.raster_aoi_area_ha == 1462.32
    assert view.baseline.review_required_area_ha == 175.5
    assert view.quality.seasonal_period_count == 2
    assert view.quality.minimum_valid_pixel_fraction == 0.99
    assert view.quality.minimum_mean_observation_count == 15.0
    assert view.quality.maximum_mean_observation_count == 42.0
    assert view.verified_asset_count == len(package.assets)
    assert len(view.establishment_geometry) == 1
    assert view.establishment_geometry[0][0] == (-60.08, -31.9)
    assert [figure.kind for figure in view.client_figures] == [
        "annual_forest_change",
        "rgb_timeline",
        "dynamic_world_annual_land_cover",
        "event_spectral_evidence",
    ]
    assert view.client_figures[3].event_id == "PDE-1"
    assert len(view.events) == 2
    assert view.events[0].selected_for_detail is True
    assert view.events[1].selected_for_detail is False
    assert view.events[1].record_type == "disturbance_candidate"
    assert view.events[1].interpretation_level == "candidate_episode"
    assert view.events[1].interpretation_status == "candidate_only"
    assert view.events[1].candidate_id == "PDE-2"
    assert view.events[1].event_id is None
    assert view.events[0].sensitivity_min_area_ha == 6.7
    assert view.events[0].sensitivity_max_area_ha == 9.0
    assert len(view.events[0].event_geometry) == 1
    assert len(view.events[0].agricultural_geometry) == 1
    assert [source.dataset_id for source in view.datasets] == [
        "dynamic_world_v1",
        "hls_s30_v2",
    ]
    assert "per_period_support" not in view.model_dump_json()
    assert "010_overview.png" not in view.model_dump_json()
    assert "040_spectral_index_timeline.png" not in view.model_dump_json()


def test_v2_weak_agricultural_occurrence_is_reported_as_ordinal_not_persistent(
    tmp_path: Path,
) -> None:
    root = _report_package(tmp_path)
    _set_v2_agricultural_occurrence(
        root,
        signal_strength="weak",
        qualifying_period_count=1,
        strength_areas=[
            {
                "signal_strength": "weak",
                "qualifying_period_minimum": 1,
                "qualifying_period_maximum": 1,
                "area_ha": 8.12,
                "event_fraction": 0.7733,
            }
        ],
    )

    package = load_report_package(root)
    view = build_report_view_model(package)
    event = view.events[0]

    assert event.agricultural_evidence_schema_version == "2.0.0"
    assert view.schema_version == "3.0.0"
    assert event.agricultural_signal_strength == "weak"
    assert event.agricultural_effective_observation_date is not None
    assert event.agricultural_effective_observation_date.isoformat() == "2022-06-30"
    assert event.qualifying_period_count == 1
    assert event.agricultural_strength_areas[0].area_ha == pytest.approx(8.12)

    artifact = render_technical_report(
        package,
        tmp_path / "v2-weak.pdf",
        osm_basemap_provider=_static_osm_provider(),
    )
    text = " ".join(
        " ".join((page.extract_text() or "").split()) for page in PdfReader(artifact.path).pages
    )
    assert "Evidencia agrícola post-evento" in text
    assert "Débil (ordinal, no calibrada)" in text
    assert "30/06/2022" in text
    assert "Agricultura persistente" not in text
    assert "Soporte agrícola persistente" not in text
    assert "Persistencia agrícola" not in text


def test_v2_strong_agricultural_occurrence_reports_each_strength_area(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    _set_v2_agricultural_occurrence(
        root,
        signal_strength="strong",
        qualifying_period_count=4,
        strength_areas=[
            {
                "signal_strength": "weak",
                "qualifying_period_minimum": 1,
                "qualifying_period_maximum": 1,
                "area_ha": 1.0,
                "event_fraction": 0.0952,
            },
            {
                "signal_strength": "moderate",
                "qualifying_period_minimum": 2,
                "qualifying_period_maximum": 2,
                "area_ha": 2.0,
                "event_fraction": 0.1905,
            },
            {
                "signal_strength": "strong",
                "qualifying_period_minimum": 3,
                "qualifying_period_maximum": None,
                "area_ha": 5.12,
                "event_fraction": 0.4876,
            },
        ],
    )

    package = load_report_package(root)
    event = build_report_view_model(package).events[0]

    assert event.agricultural_signal_strength == "strong"
    assert [item.signal_strength for item in event.agricultural_strength_areas] == [
        "weak",
        "moderate",
        "strong",
    ]

    artifact = render_technical_report(
        package,
        tmp_path / "v2-strong.pdf",
        osm_basemap_provider=_static_osm_provider(),
    )
    text = " ".join(
        " ".join((page.extract_text() or "").split()) for page in PdfReader(artifact.path).pages
    )
    assert "Fuerte (ordinal, no calibrada)" in text
    assert "Área débil" in text and "1,00 ha" in text
    assert "Área moderada" in text and "2,00 ha" in text
    assert "Área fuerte" in text and "5,12 ha" in text


def test_v2_1_reports_candidate_coverage_gate(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    _set_v2_1_candidate_coverage(root)

    package = load_report_package(root)
    view = build_report_view_model(package)
    event = view.events[0]

    assert event.agricultural_evidence_schema_version == "2.1.0"
    assert event.candidate_agricultural_coverage_fraction == pytest.approx(0.071)
    assert event.candidate_agricultural_coverage_threshold == pytest.approx(0.05)
    assert event.candidate_agricultural_coverage_gate_met is True

    artifact = render_technical_report(
        package,
        tmp_path / "v2-1-coverage.pdf",
        osm_basemap_provider=_static_osm_provider(),
    )
    text = " ".join(
        " ".join((page.extract_text() or "").split()) for page in PdfReader(artifact.path).pages
    )
    assert "Cobertura agrícola máxima" not in text
    assert "Umbral de cobertura del candidato" not in text


def test_v2_agricultural_occurrence_rejects_legacy_report_dataset(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    _set_v2_agricultural_occurrence(
        root,
        signal_strength="weak",
        qualifying_period_count=1,
        strength_areas=[],
    )
    dataset_path = root / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text("utf-8"))
    dataset["schema_version"] = "1.4.0"
    _write_json(dataset_path, dataset)
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text("utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)

    with pytest.raises(
        ReportContractError, match="agricultural_occurrence_requires_report_dataset_v2"
    ):
        build_report_view_model(load_report_package(root))


def test_v2_agricultural_occurrence_accepts_report_dataset_v2_1(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    _set_v2_agricultural_occurrence(
        root,
        signal_strength="weak",
        qualifying_period_count=1,
        strength_areas=[],
    )
    dataset_path = root / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text("utf-8"))
    dataset["schema_version"] = "2.1.0"
    _write_json(dataset_path, dataset)
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text("utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)

    view = build_report_view_model(load_report_package(root))

    assert view.schema_version == "3.0.0"
    assert view.events[0].agricultural_evidence_schema_version == "2.0.0"


def test_rf_only_fused_candidate_survives_partial_report_with_unavailable_measures(
    tmp_path: Path,
) -> None:
    root = _report_package(tmp_path)
    dataset_path = root / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text("utf-8"))
    dataset.update(
        {
            "schema_version": "2.0.0",
            "headline_metrics": {
                **dataset["headline_metrics"],
                "spectral_candidate_area_ha": 2.4,
                "spectral_candidate_count": 1,
                "candidate_episode_area_ha": 2.4,
                "candidate_episode_count": 1,
                "likely_conversion_area_ha": None,
                "conversion_likely_count": None,
            },
            "selected_disturbances": [
                {
                    "candidate_id": "RF-ONLY",
                    "disturbance_source": "robust_rf_fusion",
                    "disturbance": {
                        "event_id": "RF-ONLY",
                        "candidate_id": "RF-ONLY",
                        "record_type": "persistent_disturbance_candidate",
                        "interpretation_level": "candidate_episode",
                        "automatic_status": "review_required",
                        "area_ha": 2.4,
                        "area_threshold_ha": 0.5,
                        "area_threshold_met": True,
                        "candidate_footprint_above_visec_area_reference": True,
                        "pixel_count": 24,
                        "estimated_onset_period_id": "RF-2021-2023-onset-undetermined",
                        "estimated_onset_window_start": None,
                        "estimated_onset_window_end": None,
                        "onset_available": False,
                        "sources": ["rf_persistent_loss"],
                        "resolution": "10 m",
                        "quality_flags": [
                            "fused_candidate_report_adapter",
                            "exact_onset_unavailable",
                        ],
                    },
                    "fusion_candidate": {
                        "candidate_id": "RF-ONLY",
                        "area_ha": 2.4,
                        "rf_only": True,
                        "automatic_promotion_allowed": False,
                    },
                    "agricultural_collection": None,
                    "agricultural_persistence_schema_version": None,
                    "agricultural_persistence": None,
                    "attribution": None,
                }
            ],
            "disturbance_selection": {
                "selected_candidate_ids": ["RF-ONLY"],
                "complete_candidate_inventory_path": (
                    "report_assets/data/main/030_disturbance_events.json"
                ),
                "complete_event_inventory_path": None,
            },
        }
    )
    dataset.pop("selected_events", None)
    dataset.pop("event_selection", None)
    for component_name in (
        "agricultural_collection",
        "agricultural_persistence",
        "post_change_attribution",
    ):
        dataset["component_statuses"][component_name] = {
            "status": "failed",
            "available": False,
            "reason": "dependency_unavailable",
            "selected_figure_count": 0,
        }
    _write_json(dataset_path, dataset)

    _replace_verified_json(
        root,
        "data/main/030_disturbance_events.json",
        {
            "schema_version": "fusion-report-adapter-v1.0.0",
            "event_count": 1,
            "total_event_area_ha": 2.4,
            "events": [dataset["selected_disturbances"][0]["disturbance"]],
        },
    )
    _replace_verified_json(
        root,
        "annex/spatial/disturbance_events.geojson",
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"candidate_id": "RF-ONLY", "event_id": "RF-ONLY"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-60.04, -31.88],
                                [-60.03, -31.88],
                                [-60.03, -31.87],
                                [-60.04, -31.88],
                            ]
                        ],
                    },
                }
            ],
        },
    )
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text("utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)

    package = load_report_package(root)
    view = build_report_view_model(package)
    candidate = view.events[0]

    assert candidate.candidate_id == "RF-ONLY"
    assert candidate.event_id is None
    assert candidate.record_type == "disturbance_candidate"
    assert candidate.automatic_status == "review_required"
    assert candidate.human_review_required is True
    assert candidate.likely_conversion_area_ha is None
    assert candidate.conjunctive_conversion_evidence_area_ha is None
    assert candidate.persistent_agricultural_area_ha is None
    assert candidate.gates == ()
    assert candidate.candidate_geometry
    assert "post_change_attribution_unavailable" in candidate.quality_flags
    assert "agricultural_persistence_unavailable" in candidate.quality_flags

    artifact = render_technical_report(
        package,
        tmp_path / "rf-only-partial.pdf",
        osm_basemap_provider=_static_osm_provider(),
    )
    assert artifact.path.is_file()


def test_v1_historical_agriculture_remains_readable_and_is_labelled_legacy(
    tmp_path: Path,
) -> None:
    package = load_report_package(_report_package(tmp_path))
    event = build_report_view_model(package).events[0]

    assert event.agricultural_evidence_schema_version is None
    assert event.agricultural_signal_strength is None
    assert event.persistent_agricultural_area_ha == pytest.approx(8.12)
    assert build_report_view_model(package).schema_version == "2.8.0"

    artifact = render_technical_report(
        package,
        tmp_path / "v1-legacy.pdf",
        osm_basemap_provider=_static_osm_provider(),
    )
    text = " ".join(page.extract_text() or "" for page in PdfReader(artifact.path).pages)
    assert "Soporte agrícola persistente (legado v1)" in text


def test_rejects_missing_disturbance_selection_payload() -> None:
    with pytest.raises(ReportContractError, match="selected_events_invalid"):
        _events({}, {"events": []}, {"events": []}, {"records": []}, {}, {})


def test_accepts_empty_attribution_inventory_as_clean_run() -> None:
    events = _events(
        {"selected_disturbances": []},
        {"events": []},
        {"events": []},
        {"records": []},
        {"features": []},
        {"features": []},
    )

    assert events == ()


def test_renders_pdf_for_clean_run_without_events(tmp_path: Path) -> None:
    root = _report_package(tmp_path, with_events=False)
    package = load_report_package(root)
    output = tmp_path / "clean-run.pdf"

    artifact = render_technical_report(
        package,
        output,
        osm_basemap_provider=_static_osm_provider(),
    )
    appendix = render_technical_appendix(package, tmp_path / "clean-run-appendix.pdf")

    assert artifact.path == output.resolve()
    assert artifact.size_bytes > 10_000
    reader = PdfReader(artifact.path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    appendix_text = "\n".join(page.extract_text() or "" for page in PdfReader(appendix.path).pages)
    assert "No se identificó evidencia suficiente de conversión" in text.replace("\n", " ")
    assert "No se detectaron eventos de cambio en el período evaluado" in text.replace("\n", " ")
    assert "no constituye certificación" in text
    assert "Inventario operativo y resumen subumbral" in appendix_text
    assert "Resumen de candidatos subumbrales" in appendix_text
    assert "No se detectaron candidatos en o bajo 0,5 ha" in appendix_text.replace("\n", " ")
    assert "Área compatible con conversión" in text
    assert "conversion_likely" not in text
    assert LEGAL_DISCLAIMER in text


def test_renders_honest_partial_pdf_when_agricultural_persistence_fails(
    tmp_path: Path,
) -> None:
    root = _report_package(tmp_path, with_events=True)
    report = root / "report_assets"
    dataset_path = report / "report_dataset.json"
    index_path = report / "index.json"
    dataset = json.loads(dataset_path.read_text("utf-8"))
    dataset["schema_version"] = "1.3.0"
    dataset["component_statuses"] = {
        "full_pipeline": {
            "status": "completed",
            "available": True,
            "reason": None,
            "selected_figure_count": 1,
        },
        "agricultural_collection": {
            "status": "completed",
            "available": True,
            "reason": None,
            "selected_figure_count": 1,
        },
        "agricultural_persistence": {
            "status": "failed",
            "available": False,
            "reason": "component_validation_failed",
            "selected_figure_count": 0,
        },
        "post_change_attribution": {
            "status": "skipped",
            "available": False,
            "reason": "dependency_not_completed:agricultural_persistence",
            "selected_figure_count": 0,
        },
    }
    dataset["headline_metrics"]["likely_conversion_area_ha"] = None
    dataset["headline_metrics"]["conversion_likely_count"] = None
    dataset["selected_events"] = []
    dataset["disturbance_selection"] = {
        "selected_candidate_ids": [],
        "complete_candidate_inventory_path": None,
        "complete_event_inventory_path": None,
    }
    dataset["limitations"].extend(
        [
            "La persistencia agrícola falló: component_validation_failed.",
            "La atribución post-cambio no se ejecutó.",
            "No es posible concluir ausencia ni presencia de conversión.",
        ]
    )
    _write_json(dataset_path, dataset)
    unavailable = {
        "report_assets/data/main/070_agricultural_persistence.json",
        "report_assets/data/main/080_post_change_attribution.json",
        "report_assets/annex/spatial/post_change_attribution.geojson",
    }
    index = json.loads(index_path.read_text("utf-8"))
    index["files"] = [item for item in index["files"] if item["report_path"] not in unavailable]
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)
    for relative in unavailable:
        path = root / relative
        path.unlink()

    package = load_report_package(root)
    view = build_report_view_model(package)
    output = tmp_path / "partial-run.pdf"
    artifact = render_technical_report(
        package,
        output,
        osm_basemap_provider=_static_osm_provider(),
    )
    appendix = render_technical_appendix(package, tmp_path / "partial-run-appendix.pdf")

    assert view.result_status == "partial_incomplete_evidence"
    assert len(view.events) == 2
    assert all(event.record_type == "disturbance_candidate" for event in view.events)
    assert all(event.interpretation_status == "persistent_unattributed" for event in view.events)
    assert all(event.persistent_agricultural_area_ha is None for event in view.events)
    assert all(event.likely_conversion_area_ha is None for event in view.events)
    assert all(event.conjunctive_conversion_evidence_area_ha is None for event in view.events)
    assert all(event.gates == () for event in view.events)
    persistence = next(
        component for component in view.components if component.name == "agricultural_persistence"
    )
    assert persistence.reason == "component_validation_failed"
    text = "\n".join(page.extract_text() or "" for page in PdfReader(artifact.path).pages)
    normalized = text.replace("\n", " ")
    appendix_text = " ".join(
        (page.extract_text() or "").replace("\n", " ") for page in PdfReader(appendix.path).pages
    )
    assert "La evidencia disponible es incompleta para evaluar conversión" in normalized
    assert "No es posible concluir ausencia ni presencia de conversión" in appendix_text
    assert "No se identificó evidencia suficiente de conversión" not in normalized
    assert "Candidato persistente; atribución no disponible" in normalized
    assert "La atribución post-cambio no está disponible" in normalized
    assert "no disponible" in normalized
    assert "Candidato sin persistencia suficiente" not in normalized
    assert "La señal no presenta persistencia suficiente" not in normalized
    assert "0 de 6 satisfechas" not in normalized
    assert "low_risk" not in text
    assert "Evento probable" not in text


def test_completed_component_with_missing_declared_asset_remains_integrity_error(
    tmp_path: Path,
) -> None:
    root = _report_package(tmp_path, with_events=False)
    report = root / "report_assets"
    index_path = report / "index.json"
    missing_path = "report_assets/data/main/080_post_change_attribution.json"
    index = json.loads(index_path.read_text("utf-8"))
    index["files"] = [item for item in index["files"] if item["report_path"] != missing_path]
    _write_json(index_path, index)
    (root / missing_path).unlink()

    package = load_report_package(root)
    with pytest.raises(ReportContractError, match="required_report_asset_missing"):
        build_report_view_model(package)


def test_attribution_v5_preserves_candidate_geometry_and_uses_probable_event_geometry() -> None:
    candidate_geometry = {
        "type": "Polygon",
        "coordinates": [[[-60.04, -31.88], [-60.02, -31.88], [-60.02, -31.86], [-60.04, -31.88]]],
    }
    likely_geometry = {
        "type": "Polygon",
        "coordinates": [
            [[-60.035, -31.875], [-60.03, -31.875], [-60.03, -31.87], [-60.035, -31.875]]
        ],
    }
    scientific_gates = {
        "forest_at_cutoff": True,
        "post_cutoff_loss": True,
        "persistent_change": True,
        "agricultural_or_livestock_post_use": True,
        "defensible_area_and_geometry": True,
        "strong_alternative_explanation_absent": True,
        "visec_operational_area_strictly_greater_than_threshold": True,
    }
    records = [
        {
            "candidate_id": "PDE-1",
            "event_id": "PDE-1",
            "record_type": "conversion_likely_event",
            "interpretation_status": "conversion_likely",
            "automatic_status": "conversion_likely",
            "candidate_area_ha": 2.0,
            "conjunctive_conversion_evidence_area_ha": 0.75,
            "likely_conversion_area_ha": 0.75,
            "candidate_geometry": candidate_geometry,
            "likely_conversion_geometry": likely_geometry,
            "gates": scientific_gates,
            "trajectory": {"years": [2020, 2024], "forest_fraction": [1.0, 0.1]},
            "human_review_required": True,
        },
        {
            "candidate_id": "PDE-2",
            "event_id": None,
            "record_type": "disturbance_candidate",
            "interpretation_status": "subthreshold_conversion_evidence",
            "automatic_status": "review_required",
            "candidate_area_ha": 1.0,
            "conjunctive_conversion_evidence_area_ha": 0.4,
            "likely_conversion_area_ha": 0.0,
            "candidate_geometry": candidate_geometry,
            "likely_conversion_geometry": None,
            "gates": {
                **scientific_gates,
                "visec_operational_area_strictly_greater_than_threshold": False,
            },
            "trajectory": {"years": [2020, 2024], "forest_fraction": [1.0, 0.2]},
            "human_review_required": True,
        },
    ]
    disturbance_records = [
        {
            "event_id": candidate_id,
            "area_ha": area,
            "area_threshold_ha": 0.5,
            "area_threshold_met": True,
            "estimated_onset_period_id": "2023-MAM",
        }
        for candidate_id, area in (("PDE-1", 2.0), ("PDE-2", 1.0))
    ]
    spatial_features = [
        {
            "type": "Feature",
            "properties": {"candidate_id": "PDE-1", "event_id": "PDE-1"},
            "geometry": likely_geometry,
        },
        {
            "type": "Feature",
            "properties": {"candidate_id": "PDE-2", "event_id": None},
            "geometry": candidate_geometry,
        },
    ]

    events = _events(
        {
            "selected_disturbances": [{"candidate_id": "PDE-1"}, {"candidate_id": "PDE-2"}],
            "disturbance_selection": {"selected_candidate_ids": ["PDE-1", "PDE-2"]},
        },
        {"events": disturbance_records},
        {"events": []},
        {"schema_version": "5.0.0", "records": records},
        {
            "features": [
                {
                    "type": "Feature",
                    "properties": {"candidate_id": item["event_id"]},
                    "geometry": candidate_geometry,
                }
                for item in disturbance_records
            ]
        },
        {"features": spatial_features},
    )

    assert events[0].event_geometry == (
        ((-60.035, -31.875), (-60.03, -31.875), (-60.03, -31.87), (-60.035, -31.875)),
    )
    assert events[0].candidate_geometry != events[0].event_geometry
    assert events[0].conjunctive_conversion_evidence_area_ha == pytest.approx(0.75)
    assert events[1].interpretation_status == "subthreshold_conversion_evidence"
    assert events[1].event_id is None
    assert events[1].event_geometry == events[1].candidate_geometry
    assert events[1].likely_conversion_area_ha == 0.0


def test_rejects_misaligned_candidate_trajectory() -> None:
    attribution = {
        "candidate_id": "PDE-1",
        "record_type": "disturbance_candidate",
        "interpretation_status": "candidate_only",
        "automatic_status": "low_risk",
        "conversion_confirmed": False,
        "gates": {},
        "trajectory": {"years": [2020], "forest_fraction": []},
    }
    with pytest.raises(ReportContractError, match="event_trajectory_invalid"):
        _events(
            {
                "selected_disturbances": [],
                "disturbance_selection": {"selected_candidate_ids": ["PDE-1"]},
            },
            {"events": []},
            {"events": []},
            {"records": [attribution]},
            {"features": []},
            {"features": []},
        )


def test_conflicting_legacy_attribution_is_never_promoted_to_event() -> None:
    record = {
        "record_type": "conversion_likely_event",
        "interpretation_status": "conversion_likely",
        "automatic_status": "review_required",
        "gates": {"persistent_change": True},
    }

    assert _attribution_record_type(record) == "disturbance_candidate"
    assert _attribution_interpretation_status(record) == "persistent_unattributed"


def test_legacy_dataset_derives_operational_metrics_from_preserved_events(
    tmp_path: Path,
) -> None:
    root = _report_package(tmp_path)
    dataset_path = root / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    metrics = dataset["headline_metrics"]
    metrics.pop("candidate_event_area_ha")
    metrics.pop("candidate_event_count")
    metrics["detected_event_area_ha"] = 10.68
    metrics["detected_event_count"] = 2
    dataset["schema_version"] = "1.0.0"
    _write_json(dataset_path, dataset)

    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)

    view = build_report_view_model(load_report_package(root))

    assert view.metrics.spectral_candidate_count == 2
    assert view.metrics.spectral_candidate_area_ha == pytest.approx(10.68)
    assert view.metrics.candidate_episode_count == 1
    assert view.metrics.candidate_episode_area_ha == pytest.approx(10.5)


def test_v12_disturbance_metrics_use_candidate_area_reference_names() -> None:
    disturbance = {
        "schema_version": "1.2.0",
        "events": [
            {
                "area_ha": 0.6,
                "candidate_footprint_above_visec_area_reference": True,
            },
            {
                "area_ha": 0.4,
                "candidate_footprint_above_visec_area_reference": False,
            },
        ],
        "above_visec_area_reference_candidate_count": 1,
        "above_visec_area_reference_candidate_area_ha": 0.6,
    }

    assert _headline_event_metrics({}, disturbance) == (None, None, 0.6, 1)


def test_fusion_adapter_derives_operational_metrics_instead_of_reusing_inventory() -> None:
    metrics = {
        "spectral_candidate_area_ha": 1.0,
        "spectral_candidate_count": 2,
        "candidate_episode_area_ha": 1.0,
        "candidate_episode_count": 2,
    }
    disturbance = {
        "schema_version": "fusion-report-adapter-v1.0.0",
        "events": [
            {
                "area_ha": 0.6,
                "candidate_footprint_above_visec_area_reference": True,
            },
            {
                "area_ha": 0.4,
                "candidate_footprint_above_visec_area_reference": False,
            },
        ],
    }

    assert _headline_event_metrics(metrics, disturbance) == (1.0, 2, 0.6, 1)


def test_loader_accepts_explicit_verified_input_path_for_worker_layout(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    original = tmp_path / "input.geojson"
    private_input = tmp_path / "private" / "analysis" / "input.geojson"
    private_input.parent.mkdir(parents=True)
    original.replace(private_input)

    package = load_report_package(root, input_path=private_input)

    assert package.establishment_geometry


def test_rejects_tampered_declared_geometry_context(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    input_path = root.parent / "input.geojson"
    input_path.write_text('{"type":"Feature","geometry":null}', encoding="utf-8")

    with pytest.raises(ReportIntegrityError, match=r"input_geometry_(size|sha256)_mismatch"):
        load_report_package(root)


def test_suppresses_superseded_detection_limitations_after_attribution(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    dataset_path = root / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    stale = [
        "Se detectaron señales de perturbación sin atribuir su causa o uso posterior.",
        "No se ejecutó atribución de conversión ni se generó una conclusión final.",
    ]
    dataset["limitations"].extend(stale)
    _write_json(dataset_path, dataset)
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)

    package = load_report_package(root)
    view = build_report_view_model(package)

    assert all(item not in view.limitations for item in stale)
    assert all(item in package.dataset["limitations"] for item in stale)
    assert "Resultado técnico automático sujeto a revisión humana." in view.limitations


def test_rejects_tampered_and_unsafe_report_assets(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    figure = root / "report_assets" / "figures" / "main" / "010_overview.png"
    figure.write_bytes(b"tampered")
    with pytest.raises(ReportIntegrityError, match="asset_size_mismatch"):
        load_report_package(root)

    root = _report_package(tmp_path / "unsafe")
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["files"][0]["report_path"] = "../outside.png"
    _write_json(index_path, index)
    with pytest.raises(ReportIntegrityError, match="asset_path_unsafe"):
        load_report_package(root)


def test_rejects_missing_required_editorial_fields(tmp_path: Path) -> None:
    root = _report_package(tmp_path)
    dataset_path = root / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    del dataset["analysis"]["establishment_id"]
    _write_json(dataset_path, dataset)
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)

    with pytest.raises(ReportContractError, match="report_dataset_invalid"):
        build_report_view_model(load_report_package(root))


@pytest.mark.parametrize(
    ("field", "value"),
    (("automatic_status", "conversion_confirmed"), ("conversion_confirmed", True)),
)
def test_rejects_automatic_conversion_confirmation(
    tmp_path: Path, field: str, value: object
) -> None:
    root = _report_package(tmp_path)
    dataset_path = root / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset["selected_events"][0]["attribution"][field] = value
    _write_json(dataset_path, dataset)
    index_path = root / "report_assets" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)

    with pytest.raises(ReportContractError, match="automatic_conversion_confirmed_forbidden"):
        build_report_view_model(load_report_package(root))


def test_caches_osm_vector_context_with_fixed_half_opacity(tmp_path: Path) -> None:
    requests: list[str] = []

    def fetcher(endpoint: str, query: str, user_agent: str) -> dict[str, object]:
        requests.append(query)
        assert endpoint == "https://overpass.example/api/interpreter"
        assert user_agent == "reporting-tests/1.0"
        return {
            "osm3s": {"timestamp_osm_base": "2026-08-20T12:00:00Z"},
            "elements": [
                {
                    "type": "way",
                    "tags": {"highway": "track"},
                    "geometry": [
                        {"lon": -60.08, "lat": -31.90},
                        {"lon": -60.01, "lat": -31.84},
                    ],
                },
                {
                    "type": "way",
                    "tags": {"building": "yes"},
                    "geometry": [
                        {"lon": -60.05, "lat": -31.88},
                        {"lon": -60.04, "lat": -31.88},
                        {"lon": -60.04, "lat": -31.87},
                        {"lon": -60.05, "lat": -31.88},
                    ],
                },
                {
                    "type": "way",
                    "tags": {"waterway": "ditch"},
                    "geometry": [
                        {"lon": -60.07, "lat": -31.89},
                        {"lon": -60.06, "lat": -31.88},
                    ],
                },
                {
                    "type": "way",
                    "tags": {"natural": "water"},
                    "geometry": [
                        {"lon": -60.03, "lat": -31.87},
                        {"lon": -60.02, "lat": -31.86},
                    ],
                },
                {
                    "type": "way",
                    "tags": {"landuse": "farmland"},
                    "geometry": [
                        {"lon": -60.08, "lat": -31.90},
                        {"lon": -60.07, "lat": -31.89},
                    ],
                },
                "invalid",
                {"type": "way", "geometry": []},
                {
                    "type": "way",
                    "tags": {"name": "sin categoría"},
                    "geometry": [
                        {"lon": -60.08, "lat": -31.90},
                        {"lon": -60.07, "lat": -31.89},
                        {"lon": "invalid", "lat": -31.88},
                    ],
                },
            ],
        }

    provider = OverpassOSMProvider(
        cache_dir=tmp_path / "osm-cache",
        endpoint="https://overpass.example/api/interpreter",
        user_agent="reporting-tests/1.0",
        fetcher=fetcher,
    )
    bounds: MapBounds = (-60.09, -31.91, -60.00, -31.83)

    first = provider.get(bounds)
    second = provider.get(bounds)

    assert first == second
    assert len(requests) == 1
    assert first.opacity == 0.5
    assert first.attribution == "© OpenStreetMap contributors · ODbL"
    assert first.source_timestamp == "2026-08-20T12:00:00Z"
    assert [feature.kind for feature in first.features] == [
        "road",
        "building",
        "waterway",
        "water",
        "landuse",
    ]
    assert len(list((tmp_path / "osm-cache").glob("*.json"))) == 1


def test_osm_provider_degrades_without_blocking_report_and_rejects_bad_bounds(
    tmp_path: Path,
) -> None:
    def unavailable_fetcher(endpoint: str, query: str, user_agent: str) -> dict[str, object]:
        raise OSError("temporary_unavailable")

    provider = OverpassOSMProvider(
        cache_dir=tmp_path / "osm-cache",
        fetcher=unavailable_fetcher,
    )

    unavailable = provider.get((-60.09, -31.91, -60.00, -31.83))

    assert unavailable.available is False
    assert unavailable.features == ()
    assert not (tmp_path / "osm-cache").exists()
    with pytest.raises(ValueError, match="osm_bounds_invalid"):
        provider.get((-60.0, -31.9, -60.0, -31.8))
    with pytest.raises(ValueError, match="osm_bounds_not_finite"):
        provider.get((float("nan"), -31.9, -60.0, -31.8))


def test_renders_deterministic_pdf_with_required_technical_language(tmp_path: Path) -> None:
    package = load_report_package(_report_package(tmp_path))
    provider = _static_osm_provider()
    first = render_technical_report(
        package,
        tmp_path / "first.pdf",
        osm_basemap_provider=provider,
    )
    second = render_technical_report(
        package,
        tmp_path / "second.pdf",
        osm_basemap_provider=provider,
    )
    appendix = render_technical_appendix(package, tmp_path / "appendix.pdf")

    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.size_bytes > 10_000

    reader = PdfReader(first.path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    appendix_text = "\n".join(page.extract_text() or "" for page in PdfReader(appendix.path).pages)
    complete_text = f"{text}\n{appendix_text}"
    page_sizes = [
        (float(page.mediabox.width), float(page.mediabox.height)) for page in reader.pages
    ]
    assert 17 <= len(reader.pages) <= 23
    assert reader.metadata is not None
    assert reader.metadata.title == "Informe técnico de evidencia geoespacial"
    assert "establecimiento-sintetico" in text
    assert "ID del análisis" in text
    assert "Resumen ejecutivo" in text
    assert "Índice" in text
    assert "Marco conceptual y alcance" in text
    assert "Qué significa «libre de deforestación»" in text
    assert "1.3.1. Bosque" in text
    assert "1.3.2. Deforestación" in text
    assert "1.3.3. Fecha de corte y superficie" in text
    assert "Protocolo VISEC Carne Libre de Deforestación Argentina" in " ".join(text.split())
    assert "Método de evaluación" in text
    assert "Interpretación y limitaciones" in text
    assert "Fecha de corte EUDR" in text
    assert "Período analizado" in text
    assert "Alcance EUDR" in text
    assert "Identificación y geolocalización" in text
    assert "WGS 84" in text
    assert "geometría declarada" in text.lower()
    assert "© OpenStreetMap contributors · ODbL" in text
    assert "1.438,42 ha" in text
    assert "Se detectaron eventos de cambio que requieren revisión" in text.replace("\n", " ")
    assert "Evidencia compatible con conversión" in text
    assert "Inventario operativo y resumen subumbral" in appendix_text
    assert "Resumen de candidatos subumbrales" in appendix_text
    assert "1 candidato" in appendix_text
    assert "0,18 ha" in appendix_text
    assert "subthreshold_candidates.csv" in appendix_text
    assert "Candidato" in text
    assert "detalle íntegro" in appendix_text.replace("\n", " ").casefold()
    assert "Cobertura forestal anual: referencia y trayectoria" in text
    assert "Cambios anuales respecto de la referencia 2020" in text
    assert "Cobertura/uso del suelo anual Dynamic World" in text
    assert "0 agua, 1 árboles" in text.replace("\n", " ")
    assert "doi:10.1038/s41597-022-01307-4" in text
    assert "Fuentes y licencias" in appendix_text
    assert "Trazabilidad técnica" in appendix_text
    assert "99,00 %" in appendix_text
    assert "Assets verificados" in appendix_text
    assert str(len(package.assets)) in appendix_text
    assert LEGAL_DISCLAIMER in complete_text
    assert "conversion_confirmed: true" not in complete_text
    assert "conversion_likely" not in complete_text
    assert "gate_passed" not in complete_text
    assert "P0" not in complete_text
    assert "Voto candidato" not in complete_text
    assert "línea base y perturbaciones" not in complete_text
    assert page_sizes[0][0] < page_sizes[0][1]
    assert sum(width > height for width, height in page_sizes) >= 5
    assert page_sizes[-1][0] < page_sizes[-1][1]
    embedded_fonts: set[str] = set()
    for page in reader.pages:
        resources = cast(Any, page["/Resources"].get_object())
        fonts = cast(Any, resources["/Font"].get_object())
        embedded_fonts.update(
            str(font.get_object().get("/BaseFont", "")) for font in fonts.values()
        )
    assert any("BitstreamVeraSans" in font for font in embedded_fonts)


def test_splits_client_report_and_appendix_and_keeps_each_candidate_on_one_page(
    tmp_path: Path,
) -> None:
    package = load_report_package(_report_package(tmp_path))
    main = render_technical_report(
        package,
        tmp_path / "client-report.pdf",
        osm_basemap_provider=_static_osm_provider(),
    )
    appendix = render_technical_appendix(package, tmp_path / "technical-appendix.pdf")

    main_pages = [
        " ".join((page.extract_text() or "").split()) for page in PdfReader(main.path).pages
    ]
    appendix_text = " ".join(
        " ".join((page.extract_text() or "").split()) for page in PdfReader(appendix.path).pages
    )

    assert all("Anexos técnicos" not in page for page in main_pages)
    assert "Anexos técnicos" in appendix_text
    assert "Resumen ejecutivo" not in appendix_text
    assert "Ubicación en el establecimiento" not in " ".join(main_pages)
    for ordinal in (1, 2):
        marker = f"6.{ordinal}."
        matching_pages = [page for page in main_pages if marker in page and "ID técnico" in page]
        assert len(matching_pages) == 1
        assert "ID técnico" in matching_pages[0]
        assert "Interpretación técnica:" in matching_pages[0]
    assert not any(
        "6.1." in page and "6.2." in page and "ID técnico" in page for page in main_pages
    )


def test_event_map_does_not_render_establishment_location_inset(tmp_path: Path) -> None:
    view = build_report_view_model(load_report_package(_report_package(tmp_path)))
    drawing = agricultural_support_map(
        view.events[0], establishment_geometry=(), width=240, height=120
    )
    assert drawing.width == 240
    assert drawing.height == 120
    assert _event_color(
        view.events[0].model_copy(
            update={"record_type": "disturbance_candidate", "human_review_required": False}
        )
    )


def test_operational_unselected_candidate_keeps_one_secondary_detail_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("deforestation_reporting.renderer._PRIORITY_EVENT_LIMIT", 1)
    root = _report_package(tmp_path)
    disturbance_path = root / "report_assets/data/main/030_disturbance_events.json"
    disturbance = json.loads(disturbance_path.read_text("utf-8"))
    disturbance["events"][1]["area_ha"] = 0.6
    disturbance["events"][1]["area_threshold_met"] = True
    _replace_verified_json(root, "data/main/030_disturbance_events.json", disturbance)
    attribution_path = root / "report_assets/data/main/080_post_change_attribution.json"
    attribution = json.loads(attribution_path.read_text("utf-8"))
    attribution["events"][1]["area_ha"] = 0.6
    attribution["events"][1]["area_threshold_met"] = True
    _replace_verified_json(root, "data/main/080_post_change_attribution.json", attribution)

    view = build_report_view_model(load_report_package(root))
    secondary_flowables = _secondary_event_annex(view, (view.events[0],), _styles())
    assert len(secondary_flowables) >= 3

    appendix = render_technical_appendix(
        load_report_package(root), tmp_path / "secondary-operational-appendix.pdf"
    )
    text = " ".join(
        " ".join((page.extract_text() or "").split()) for page in PdfReader(appendix.path).pages
    )

    assert "Fichas detalladas de eventos secundarios" in text
    assert "PDE-2" in text
    assert "0,60 ha" in text


def test_renderer_is_atomic_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    package = load_report_package(_report_package(tmp_path))
    output = tmp_path / "nested" / "report.pdf"
    appendix_output = tmp_path / "nested" / "appendix.pdf"

    artifact = render_technical_report(
        package,
        output,
        osm_basemap_provider=_static_osm_provider(),
    )
    appendix = render_technical_appendix(package, appendix_output)

    assert artifact.path == output.resolve()
    assert output.is_file()
    assert not output.with_suffix(".tmp").exists()
    assert artifact.sha256 == _sha256(output)
    assert appendix.path == appendix_output.resolve()
    assert not appendix_output.with_suffix(".tmp").exists()
    assert appendix.sha256 == _sha256(appendix_output)


def test_professional_report_has_audit_structure_dynamic_headline_and_navigation(
    tmp_path: Path,
) -> None:
    package = load_report_package(_report_package(tmp_path))
    artifact = render_technical_report(
        package,
        tmp_path / "professional.pdf",
        osm_basemap_provider=_static_osm_provider(),
    )
    appendix = render_technical_appendix(package, tmp_path / "professional-appendix.pdf")

    reader = PdfReader(artifact.path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    compact = " ".join(text.split())
    appendix_text = "\n".join(page.extract_text() or "" for page in PdfReader(appendix.path).pages)
    outline_titles = _outline_titles(reader.outline)
    outline_hierarchy = _outline_hierarchy(reader.outline)
    view = build_report_view_model(package)

    assert "Fecha de emisión" in text
    assert "Fecha de corte EUDR" in text
    assert "Se detectaron eventos de cambio que requieren revisión." in compact
    assert "Superficie compatible con conversión: 8,09 ha." in compact
    assert "Estado de revisión humana: Pendiente." in compact
    assert "Cambio de cobertura detectado" in compact
    assert "Evidencia compatible con conversión" in compact
    assert "Decisión humana y estado de revisión" in compact
    assert "Alcance EUDR de esta evidencia" in text
    assert "Legalidad" in text and "No evaluada" in text
    assert "Trazabilidad de animales o productos" in compact
    assert "Certificación EUDR" in text and "Fuera del alcance" in text
    assert "Eventos prioritarios" in text
    assert "1 evento, 8,09 ha" in compact
    assert "1 eventos" not in compact
    assert "Inventario operativo y resumen subumbral" in appendix_text
    assert "Anexos técnicos" in appendix_text
    assert "Resultado automático" in text
    assert "Interpretación técnica" in text
    assert "Aspectos pendientes" in text
    assert "Revisor" in text and "Observaciones" in text
    assert all(event.candidate_id in f"{text}\n{appendix_text}" for event in view.events)
    assert "forest recovery" not in text
    assert "MVP" not in text
    assert text.count("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") <= 2
    assert reader.metadata is not None
    assert reader.metadata.creation_date is not None
    assert reader.metadata.creation_date.year == 2026
    assert len(outline_titles) >= 28
    assert outline_titles[:5] == [
        "Portada",
        "Índice",
        "1. Marco conceptual y alcance",
        "1.1. El EUDR y la cadena bovina",
        "1.2. Qué significa «libre de deforestación»",
    ]
    assert (1, "1.3. Definiciones de referencia") in outline_hierarchy
    assert (2, "1.3.1. Bosque") in outline_hierarchy
    assert (2, "1.3.2. Deforestación") in outline_hierarchy
    assert (0, "6. Eventos prioritarios") in outline_hierarchy
    assert (1, "6.1. Evento probable E1") in outline_hierarchy
    assert (0, "7. Evidencia temporal") in outline_hierarchy
    assert (1, "7.1. Cobertura forestal anual: referencia y trayectoria") in outline_hierarchy
    assert (1, "7.5. Cobertura/uso del suelo anual Dynamic World") in outline_hierarchy
