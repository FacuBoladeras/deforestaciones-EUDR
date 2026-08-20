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
    render_technical_report,
)
from deforestation_reporting.basemap import (
    MapBounds,
    OSMBasemap,
    OSMFeature,
    OverpassOSMProvider,
)


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


def _write_figure(
    path: Path, label: str, color: str, *, size: tuple[int, int] = (1200, 700)
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    ImageDraw.Draw(image).text((60, 60), label, fill="white")
    image.save(path, format="PNG")


def _report_package(tmp_path: Path) -> Path:
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
    event_figure = report / "figures" / "events" / "PDE-1" / "disturbance.png"
    _write_figure(overview, "Resumen espacial", "#1d4ed8", size=(2400, 600))
    _write_figure(rgb_timeline, "Serie RGB", "#365314", size=(1400, 1000))
    _write_figure(rf_timeline, "Cobertura RF anual", "#166534", size=(1600, 950))
    _write_figure(event_figure, "Evento PDE-1", "#b45309")
    dataset: dict[str, Any] = {
        "schema_version": "1.0.0",
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
            "detected_event_area_ha": 17.28,
            "detected_event_count": 4,
            "likely_conversion_area_ha": 8.09,
            "conversion_likely_count": 1,
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
    dataset_path = report / "report_dataset.json"
    _write_json(dataset_path, dataset)
    files = []
    for category, event_id, reason, path in (
        ("main_figure", None, "línea base y perturbaciones", overview),
        ("main_figure", None, "contexto RGB multitemporal acotado", rgb_timeline),
        ("main_figure", None, "trayectoria y deltas forestales RF", rf_timeline),
        ("event_figure", "PDE-1", "detalle de perturbación", event_figure),
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
                    "gates": dataset["selected_events"][0]["attribution"]["gates"],
                    "trajectory": dataset["selected_events"][0]["attribution"]["trajectory"],
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
    assert view.metrics.detected_event_count == 4
    assert view.components[1].status == "diagnostic_unavailable"
    assert view.events[0].automatic_status == "conversion_likely"
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
        "event_spectral_evidence",
    ]
    assert view.client_figures[2].event_id == "PDE-1"
    assert len(view.events) == 2
    assert view.events[0].selected_for_detail is True
    assert view.events[1].selected_for_detail is False
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

    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.size_bytes > 10_000

    reader = PdfReader(first.path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    page_sizes = [
        (float(page.mediabox.width), float(page.mediabox.height)) for page in reader.pages
    ]
    assert 12 <= len(reader.pages) <= 17
    assert reader.metadata is not None
    assert reader.metadata.title == "Informe técnico de evidencia geoespacial"
    assert "establecimiento-sintetico" in text
    assert "ID de análisis" in text
    assert "Índice de contenidos" in text
    assert "Qué clase de informe es" in text
    assert "línea base de cobertura forestal al 31/12/2020" in text.replace("\n", " ")
    assert "series multitemporales" in text
    assert "convergencia de evidencias" in text
    assert "Cómo leer este informe" in text
    assert "Referencia espacial" in text
    assert "Coordenadas WGS 84" in text
    assert "geometría declarada" in text
    assert "© OpenStreetMap contributors · ODbL" in text
    assert "1.438,42 ha" in text
    assert "Revisión humana requerida" in text
    assert "Evidencia compatible con conversión" in text
    assert "Inventario completo de eventos" in text
    assert "Cobertura forestal anual y pérdidas candidatas" in text
    assert "Evolución visual estacional en color natural" in text
    assert "Evidencia espectral complementaria del evento E1" in text
    assert "Calidad y fuentes" in text
    assert "99,00 %" in text
    assert f"{len(package.assets)} assets del expediente" in text.replace("\n", " ")
    assert LEGAL_DISCLAIMER in text
    assert "conversion_confirmed: true" not in text
    assert "conversion_likely" not in text
    assert "gate_passed" not in text
    assert "P0" not in text
    assert "Voto candidato" not in text
    assert "línea base y perturbaciones" not in text
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


def test_renderer_is_atomic_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    package = load_report_package(_report_package(tmp_path))
    output = tmp_path / "nested" / "report.pdf"

    artifact = render_technical_report(
        package,
        output,
        osm_basemap_provider=_static_osm_provider(),
    )

    assert artifact.path == output.resolve()
    assert output.is_file()
    assert not output.with_suffix(".tmp").exists()
    assert artifact.sha256 == _sha256(output)
