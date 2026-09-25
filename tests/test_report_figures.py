from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from matplotlib.image import imread
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

import deforestation_pipeline.report_figures as report_figures
from deforestation_pipeline.report_figures import (
    _load_components,
    _load_source_documents,
    _select_event_ids,
    load_report_dataset_document,
    materialize_report_figure_collection,
)


def test_rf_first_candidates_are_report_fallback_before_historical_events(tmp_path: Path) -> None:
    staging = tmp_path / "run"
    full = _component(
        staging,
        "full_pipeline",
        {},
        documents={
            "json/evidence/disturbance_events.json": {
                "events": [{"candidate_id": "HISTORICAL", "area_ha": 9.0}]
            }
        },
    )
    fusion = _component(
        staging,
        "disturbance_candidate_fusion",
        {},
        documents={
            "json/evidence/disturbance_candidate_fusion.json": {
                "schema_version": "2.0.0",
                "candidates": [
                    {
                        "candidate_id": "RFC-ONLY",
                        "area_ha": 1.2,
                        "first_loss_year_range": [2022, 2022],
                        "segmentation_source": "rf_terminal_persistent_forest_loss",
                        "support_sources": ["ccdc_break_support"],
                        "resolution": "review_required",
                    }
                ],
            },
            "json/evidence/disturbance_events.geojson": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"candidate_id": "RFC-ONLY", "event_id": "RFC-ONLY"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-60.01, -31.01],
                                    [-60.0, -31.01],
                                    [-60.0, -31.0],
                                    [-60.01, -31.01],
                                ]
                            ],
                        },
                    }
                ],
            },
        },
    )

    bundles, _ = _load_components(staging.resolve(), [full, fusion])
    assert _select_event_ids(_load_source_documents(bundles)) == ["RFC-ONLY"]

    result = materialize_report_figure_collection(
        staging=staging,
        components=[full, fusion],
        overall_status="partial",
        recorded_at="2026-08-28T12:00:00+00:00",
    )
    dataset = json.loads((staging / str(result["dataset_path"])).read_text("utf-8"))
    selected = dataset["selected_disturbances"][0]
    assert selected["candidate_id"] == "RFC-ONLY"
    assert selected["disturbance_source"] == "rf_first_candidate_domain"
    assert selected["fusion_candidate"]["candidate_id"] == "RFC-ONLY"
    assert selected["disturbance"]["automatic_status"] == "review_required"
    assert dataset["headline_metrics"]["spectral_candidate_count"] == 1
    assert dataset["headline_metrics"]["spectral_candidate_area_ha"] == pytest.approx(1.2)
    assert dataset["headline_metrics"]["candidate_episode_count"] == 1
    assert dataset["headline_metrics"]["candidate_episode_area_ha"] == pytest.approx(1.2)
    assert dataset["headline_metrics"]["likely_conversion_area_ha"] is None
    assert dataset["headline_metrics"]["conversion_likely_count"] is None
    adapted = json.loads(
        (staging / "report_assets/data/main/030_disturbance_events.json").read_text("utf-8")
    )
    assert [event["event_id"] for event in adapted["events"]] == ["RFC-ONLY"]
    assert adapted["events"][0]["estimated_onset_period_id"] == "RF-2022-2022-onset-undetermined"
    assert adapted["events"][0]["sources"] == [
        "rf_terminal_persistent_forest_loss",
        "ccdc_break_support",
    ]
    spatial = json.loads(
        (staging / "report_assets/annex/spatial/disturbance_events.geojson").read_text("utf-8")
    )
    assert spatial["features"][0]["properties"]["candidate_id"] == "RFC-ONLY"


def test_rf_first_selected_candidate_gets_its_own_spectral_time_series(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "run"
    periods = ("2020-DJF", "2020-MAM", "2021-DJF", "2021-MAM")
    figures = {
        f"tiffs/seasonal/{period}/indices.tif": _index_tiff(
            ndvi=0.8 - position * 0.15,
            nbr=0.6 - position * 0.12,
        )
        for position, period in enumerate(periods)
    }
    full = _component(
        staging,
        "full_pipeline",
        figures,
        documents={
            "json/run/summary.json": {
                "analysis_id": "analysis-rf-series",
                "establishment_id": "farm-rf-series",
                "analysis_end_date": "2021-05-31",
                "area": {"total_area_ha": 4.0},
                "limitations": [],
            },
            "json/evidence/forest_baseline_2020.json": {
                "screening": {"metrics": {"automated_forest_area_ha": 2.0}}
            },
            "json/evidence/disturbance_detection.json": {
                "parameters": {"indices": ["NDVI", "NBR"]},
                "screening": {"final_assessment_generated": False},
            },
            "json/evidence/disturbance_events.json": {"events": []},
            "tables/seasonal/summary.csv": (
                "period_id,season_year,season,variable_kind,variable\n"
                + "".join(f"{period},{period[:4]},{period[5:]},index,NDVI\n" for period in periods)
            ),
        },
    )
    fusion = _component(
        staging,
        "disturbance_candidate_fusion",
        {},
        documents={
            "json/evidence/disturbance_candidate_fusion.json": {
                "schema_version": "2.0.0",
                "candidates": [
                    {
                        "candidate_id": "RFC-SERIES",
                        "area_ha": 1.0,
                        "first_loss_year_range": [2021, 2021],
                        "segmentation_source": "rf_terminal_persistent_forest_loss",
                        "support_sources": [],
                        "resolution": "review_required",
                    }
                ],
            },
            "json/evidence/disturbance_events.geojson": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "candidate_id": "RFC-SERIES",
                            "candidate_pixel_count": 1,
                            "area_ha": 1.0,
                            "first_loss_year_range": [2021, 2021],
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0.0, 2.0], [1.0, 2.0], [1.0, 1.0], [0.0, 1.0], [0.0, 2.0]]
                            ],
                        },
                    }
                ],
            },
        },
    )

    materialize_report_figure_collection(
        staging=staging,
        components=[full, fusion],
        overall_status="partial",
        recorded_at="2026-09-02T12:00:00+00:00",
    )

    target = staging / "report_assets/figures/events/RFC-SERIES/disturbance.png"
    assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    rendered = imread(BytesIO(target.read_bytes()), format="png")
    assert rendered.shape[1] / rendered.shape[0] < 2
    index = json.loads((staging / "report_assets/index.json").read_text("utf-8"))
    entry = next(
        item
        for item in index["files"]
        if item["report_path"] == target.relative_to(staging).as_posix()
    )
    assert entry["event_id"] == "RFC-SERIES"
    assert entry["selection_reason"] == (
        "comparación NDVI pre/post y serie espectral dentro del candidato RF-first"
    )
    assert len(entry["derivation_sources"]) == len(periods) + 1


def test_rf_ndvi_comparison_uses_same_season_pre_cutoff_and_strongest_decline() -> None:
    periods = ("2020-DJF", "2020-MAM", "2021-DJF", "2021-MAM")
    series = np.asarray(
        [[0.80, 0.60], [0.70, 0.50], [0.35, 0.20], [0.55, 0.30]],
        dtype=np.float64,
    )

    selected = report_figures._select_rf_ndvi_comparison_indices(
        periods=periods,
        index_names=("NDVI", "NBR"),
        series=series,
        first_loss_year_range=[2021, 2021],
    )

    assert selected == (0, 2)


def _component(
    staging: Path,
    name: str,
    figures: dict[str, bytes],
    *,
    status: str = "completed",
    documents: dict[str, object | str | bytes] | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    component: dict[str, object] = {
        "name": name,
        "status": status,
        "required_for_publication": name != "hampel_benchmark",
        "status_receipt": None,
    }
    if status != "completed":
        component["error"] = (
            {"error_type": "SyntheticComponentError", "message": reason}
            if reason is not None
            else None
        )
        receipt = staging / "components" / name / "component_status.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps({"component": name, "status": status, "reason": reason}),
            encoding="utf-8",
        )
        component["status_receipt"] = receipt.relative_to(staging).as_posix()
        component["status_receipt_sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
        return component

    bundle = staging / "components" / name / f"{name}-bundle"
    artifacts: list[dict[str, object]] = []
    summary = bundle / "json/run/summary.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary_payload = (documents or {}).get("json/run/summary.json", {})
    summary.write_text(json.dumps(summary_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    artifacts.append(
        {
            "path": "json/run/summary.json",
            "size_bytes": summary.stat().st_size,
            "sha256": hashlib.sha256(summary.read_bytes()).hexdigest(),
        }
    )
    for relative, content in figures.items():
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        artifacts.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    for relative, document_content in (documents or {}).items():
        if relative == "json/run/summary.json":
            continue
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(document_content, bytes):
            path.write_bytes(document_content)
        elif isinstance(document_content, str):
            path.write_text(document_content, encoding="utf-8")
        else:
            path.write_text(
                json.dumps(document_content, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        artifacts.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    manifest = bundle / "json/run/manifest.json"
    manifest.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
    component["output_bundle"] = bundle.relative_to(staging).as_posix()
    component["child_manifest"] = manifest.relative_to(staging).as_posix()
    component["child_manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    return component


def _index_tiff(*, ndvi: float, nbr: float) -> bytes:
    values = np.asarray(
        [
            [[ndvi, ndvi - 0.1], [ndvi - 0.2, ndvi - 0.3]],
            [[nbr, nbr - 0.1], [nbr - 0.2, nbr - 0.3]],
        ],
        dtype=np.float32,
    )
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=2,
            height=2,
            count=2,
            dtype="float32",
            crs="EPSG:4326",
            transform=from_origin(0.0, 2.0, 1.0, 1.0),
            nodata=-9999.0,
        ) as dataset:
            dataset.write(values)
            dataset.set_band_description(1, "NDVI")
            dataset.set_band_description(2, "NBR")
        return bytes(memory.read())


def test_partial_run_records_component_availability_without_inventing_outputs(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "run"
    components = [
        _component(
            staging,
            "full_pipeline",
            {"figures/evidence/forest_baseline_2020.png": b"baseline"},
            documents={
                "json/run/summary.json": {
                    "analysis_id": "analysis-partial",
                    "establishment_id": "farm-partial",
                    "analysis_end_date": "2025-12-31",
                    "area": {"total_area_ha": 100.0},
                    "limitations": [],
                },
                "json/evidence/forest_baseline_2020.json": {
                    "screening": {"metrics": {"automated_forest_area_ha": 60.0}}
                },
                "json/evidence/disturbance_detection.json": {
                    "screening": {"final_assessment_generated": False}
                },
                "json/evidence/disturbance_events.json": {
                    "schema_version": "1.2.0",
                    "event_count": 1,
                    "total_event_area_ha": 0.8,
                    "events": [
                        {
                            "candidate_id": "PDC-PARTIAL-1",
                            "area_ha": 0.8,
                            "estimated_onset_period_id": "2024-SON",
                        }
                    ],
                },
            },
        ),
        _component(
            staging,
            "agricultural_collection",
            {},
            status="failed",
            reason="remote_server_error",
        ),
        _component(
            staging,
            "agricultural_persistence",
            {},
            status="skipped",
            reason="dependency_not_completed:agricultural_collection",
        ),
        _component(
            staging,
            "post_change_attribution",
            {},
            status="skipped",
            reason="dependency_not_completed:agricultural_persistence",
        ),
    ]

    result = materialize_report_figure_collection(
        staging=staging,
        components=components,
        overall_status="partial",
        recorded_at="2026-08-24T12:00:00+00:00",
    )

    dataset = json.loads((staging / str(result["dataset_path"])).read_text("utf-8"))
    assert dataset["schema_version"] == "2.1.0"
    assert dataset["component_statuses"]["agricultural_collection"] == {
        "status": "failed",
        "available": False,
        "reason": "remote_server_error",
        "selected_figure_count": 0,
        "status_receipt": components[1]["status_receipt"],
        "status_receipt_sha256": components[1]["status_receipt_sha256"],
    }
    assert dataset["disturbance_selection"]["complete_event_inventory_path"] is None
    assert dataset["disturbance_selection"]["complete_candidate_inventory_path"] == (
        "report_assets/data/main/030_disturbance_events.json"
    )
    index = json.loads((staging / "report_assets/index.json").read_text("utf-8"))
    assert any(
        item["report_path"] == "report_assets/data/main/030_disturbance_events.json"
        for item in index["files"]
    )
    assert dataset["headline_metrics"]["likely_conversion_area_ha"] is None
    assert dataset["disturbance_selection"]["selected_candidate_ids"] == ["PDC-PARTIAL-1"]


def test_collects_bounded_report_figures_without_moving_sources(tmp_path: Path) -> None:
    staging = tmp_path / "run"
    components = [
        _component(
            staging,
            "full_pipeline",
            {
                "figures/evidence/forest_baseline_2020.png": b"baseline",
                "figures/evidence/forest_screening_2020.png": b"screening",
                "figures/evidence/disturbance_detection.png": b"detection",
                "figures/evidence/disturbance_events.png": b"events",
                "figures/evidence/disturbance_event_pde-a.png": b"event-a",
                "figures/seasonal/qa/rgb_timeline.png": b"rgb-timeline",
                "figures/seasonal/qa/index_timeseries.png": b"index-timeline",
                "figures/seasonal/qa/observation_coverage.png": b"coverage",
                "figures/seasonal/2025-SON/rgb.png": b"not-selected",
            },
        ),
        _component(staging, "hampel_benchmark", {}, status="diagnostic_unavailable"),
        _component(
            staging,
            "rf_annual_deltas",
            {
                "figures/evidence/rf_forest_deltas_2020_2024.png": b"rf-deltas",
                "figures/evidence/rf_model_comparison_2020.png": b"rf-comparison",
            },
        ),
        _component(
            staging,
            "agricultural_collection",
            {
                "figures/evidence/agricultural_monthly_evidence_pde-a.png": b"agri-a",
                "figures/evidence/agricultural_monthly_evidence_pde-b.png": b"agri-b",
                "figures/evidence/dynamic_world_annual_land_cover_2020_2025.png": b"dw",
                (
                    "tiffs/evidence/agricultural/annual_land_cover/"
                    "dynamic_world_land_cover_2020.tif"
                ): b"tif-2020",
                (
                    "tiffs/evidence/agricultural/annual_land_cover/"
                    "dynamic_world_land_cover_2025.tif"
                ): b"tif-2025",
            },
        ),
        _component(
            staging,
            "agricultural_persistence",
            {"figures/evidence/agricultural_persistence_pde-a.png": b"persistence-a"},
        ),
        _component(
            staging,
            "post_change_attribution",
            {
                "figures/evidence/post_change_attribution.png": b"attribution",
                "figures/evidence/likely_conversion_conjunction_pde-a.png": b"likely-a",
            },
        ),
    ]

    result = materialize_report_figure_collection(
        staging=staging,
        components=components,
        overall_status="partial",
        recorded_at="2026-08-12T12:00:00+00:00",
    )

    index_path = staging / str(result["index_path"])
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert result["figure_count"] == 16
    assert len(index["figures"]) == 16
    assert index["selection_policy_version"] == "2.4.0"
    assert index["component_statuses"]["hampel_benchmark"] == {
        "status": "diagnostic_unavailable",
        "available": False,
        "reason": "diagnostic_unavailable",
        "selected_figure_count": 0,
        "status_receipt": "components/hampel_benchmark/component_status.json",
        "status_receipt_sha256": components[1]["status_receipt_sha256"],
    }
    report_paths = [item["report_path"] for item in index["figures"]]
    assert report_paths == sorted(report_paths)
    assert all(path.startswith("report_assets/figures/") for path in report_paths)
    assert "report_assets/figures/main/300_dynamic_world_annual_land_cover.png" in report_paths
    all_paths = {item["report_path"] for item in index["files"]}
    assert "report_assets/annex/rasters/dynamic_world_land_cover_2020.tif" in all_paths
    assert "report_assets/annex/rasters/dynamic_world_land_cover_2025.tif" in all_paths
    assert not any(path.endswith("2025-SON_rgb.png") for path in report_paths)
    assert len(report_paths) == len(set(report_paths))
    for item in index["figures"]:
        source = staging / item["source_path"]
        copied = staging / item["report_path"]
        assert source.is_file()
        assert copied.read_bytes() == source.read_bytes()
        assert item["source_sha256"] == item["report_sha256"]
        assert item["report_sha256"] == hashlib.sha256(copied.read_bytes()).hexdigest()
        assert not Path(item["source_path"]).is_absolute()
    assert (
        staging
        / "components/full_pipeline/full_pipeline-bundle/figures/seasonal/qa/rgb_timeline.png"
    ).is_file()


def test_builds_structured_report_package_and_selects_priority_events(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "run"
    disturbance_events = {
        "schema_version": "1.1.0",
        "event_count": 3,
        "total_event_area_ha": 17.1,
        "operational_event_count": 2,
        "operational_event_area_ha": 17.0,
        "events": [
            {"event_id": "PDE-A", "area_ha": 10.0, "area_threshold_met": True},
            {"event_id": "PDE-B", "area_ha": 7.0, "area_threshold_met": True},
            {"event_id": "PDE-C", "area_ha": 0.1, "area_threshold_met": False},
        ],
    }
    attribution_records = [
        {
            "candidate_id": "PDE-A",
            "event_id": "PDE-A",
            "record_type": "conversion_likely_event",
            "interpretation_level": "event",
            "interpretation_status": "conversion_likely",
            "automatic_status": "conversion_likely",
            "area_ha": 10.0,
            "area_threshold_met": True,
            "likely_conversion_area_ha": 4.0,
        },
        {
            "candidate_id": "PDE-B",
            "event_id": None,
            "record_type": "disturbance_candidate",
            "interpretation_level": "candidate_episode",
            "interpretation_status": "persistent_unattributed",
            "automatic_status": "review_required",
            "area_ha": 7.0,
            "area_threshold_met": True,
            "likely_conversion_area_ha": 0.0,
        },
        {
            "candidate_id": "PDE-C",
            "event_id": None,
            "record_type": "disturbance_candidate",
            "interpretation_level": "candidate_episode",
            "interpretation_status": "candidate_only",
            "automatic_status": "low_risk",
            "area_ha": 0.1,
            "area_threshold_met": False,
            "likely_conversion_area_ha": 0.0,
        },
    ]
    attribution = {
        "schema_version": "4.0.0",
        "status": "review_required",
        "likely_conversion_area_ha": 4.0,
        "conversion_likely_count": 1,
        "records": attribution_records,
        "candidates": attribution_records[1:],
        "events": attribution_records[:1],
    }
    components = [
        _component(
            staging,
            "full_pipeline",
            {
                "figures/evidence/forest_baseline_2020.png": b"baseline",
                "figures/evidence/disturbance_event_PDE-A.png": b"event-a",
                "figures/evidence/disturbance_event_PDE-B.png": b"event-b",
                "figures/evidence/disturbance_event_PDE-C.png": b"event-c",
                "figures/seasonal/2025-SON/rgb.png": b"not-reportable",
            },
            documents={
                "json/run/summary.json": {
                    "analysis_id": "analysis-full",
                    "establishment_id": "farm-1",
                    "analysis_end_date": "2025-11-30",
                    "area": {"total_area_ha": 100.0},
                    "limitations": [
                        "full limitation",
                        (
                            "Se detectaron señales de perturbación sin atribuir su causa "
                            "o uso posterior."
                        ),
                        "No se ejecutó atribución de conversión ni se generó una conclusión final.",
                        "No se determinó deforestación.",
                    ],
                },
                "json/evidence/forest_baseline_2020.json": {
                    "screening": {"metrics": {"automated_forest_area_ha": 60.0}}
                },
                "json/evidence/disturbance_detection.json": {
                    "screening": {"final_assessment_generated": False}
                },
                "json/evidence/disturbance_events.json": disturbance_events,
                "json/evidence/disturbance_events.geojson": {
                    "type": "FeatureCollection",
                    "features": [],
                },
                "tables/evidence/disturbance_events.csv": "event_id,area_ha\nPDE-A,10\n",
                "tables/evidence/disturbance_period_summary.csv": (
                    "period_id,signal_pixel_count\n2025-SON,1\n"
                ),
                "tables/seasonal/summary.csv": "period_id,median\n2025-SON,0.5\n",
            },
        ),
        _component(staging, "hampel_benchmark", {}, status="diagnostic_unavailable"),
        _component(
            staging,
            "rf_annual_deltas",
            {},
            documents={
                "json/run/summary.json": {"status": "review_required"},
                "json/evidence/rf_forest_deltas_2020_2024.json": {
                    "scientific_status": "review_required",
                    "yearly_summaries": [],
                },
                "tables/evidence/rf_forest_deltas_2020_2024.csv": (
                    "year,candidate_forest_loss_area_ha\n2024,5\n"
                ),
                "json/evidence/rf_forest_deltas_metadata.json": {"limitations": []},
            },
        ),
        _component(
            staging,
            "agricultural_collection",
            {
                "figures/evidence/agricultural_monthly_evidence_PDE-A.png": b"agri-a",
                "figures/evidence/agricultural_monthly_evidence_PDE-B.png": b"agri-b",
                "figures/evidence/agricultural_monthly_evidence_PDE-C.png": b"agri-c",
            },
            documents={
                "json/run/summary.json": {"status": "collected", "events": []},
                "json/evidence/agricultural_evidence.json": {"events": [], "rows": []},
                "json/evidence/agricultural_evidence.geojson": {
                    "type": "FeatureCollection",
                    "features": [],
                },
                "json/evidence/agricultural_evidence_metadata.json": {"limitations": []},
                "tables/evidence/agricultural_evidence.csv": "event_id,window_id\nPDE-A,2025-01\n",
            },
        ),
        _component(
            staging,
            "agricultural_persistence",
            {
                "figures/evidence/agricultural_persistence_PDE-A.png": b"persist-a",
                "figures/evidence/agricultural_persistence_PDE-C.png": b"persist-c",
            },
            documents={
                "json/run/summary.json": {"persistent_event_count": 2},
                "json/evidence/agricultural_persistence.json": {
                    "schema_version": "2.0.0",
                    "events": [
                        {
                            "event_id": "PDE-A",
                            "signal_strength": "weak",
                            "effective_observation_date": "2022-06-30",
                            "qualifying_period_count": 1,
                            "signal_strength_areas": [
                                {
                                    "signal_strength": "weak",
                                    "qualifying_period_minimum": 1,
                                    "qualifying_period_maximum": 1,
                                    "area_ha": 0.7,
                                    "event_fraction": 0.1,
                                }
                            ],
                        }
                    ],
                },
                "json/evidence/agricultural_evidence_persistent.json": {"observations": []},
                "tables/evidence/agricultural_persistence.csv": (
                    "event_id,persistence_status\nPDE-A,persistent\n"
                ),
            },
        ),
        _component(
            staging,
            "post_change_attribution",
            {
                "figures/evidence/post_change_attribution.png": b"attribution",
                "figures/evidence/likely_conversion_conjunction_PDE-A.png": b"likely-a",
                "figures/evidence/likely_conversion_conjunction_PDE-C.png": b"likely-c",
            },
            documents={
                "json/run/summary.json": {
                    "status": "review_required",
                    "likely_conversion_area_ha": 4.0,
                },
                "json/evidence/post_change_attribution.json": attribution,
                "json/evidence/post_change_attribution.geojson": {
                    "type": "FeatureCollection",
                    "features": [],
                },
                "json/evidence/post_change_attribution_metadata.json": {"limitations": []},
                "tables/evidence/post_change_attribution.csv": (
                    "event_id,automatic_status\nPDE-A,conversion_likely\n"
                ),
            },
        ),
    ]

    result = materialize_report_figure_collection(
        staging=staging,
        components=components,
        overall_status="partial",
        recorded_at="2026-08-13T12:00:00+00:00",
        parent_analysis_id="parent-analysis",
    )

    assert result["schema_version"] == "2.5.0"
    assert result["selection_policy_version"] == "2.4.0"
    assert result["dataset_path"] == "report_assets/report_dataset.json"
    assert result["selected_event_count"] == 2
    dataset = json.loads((staging / str(result["dataset_path"])).read_text("utf-8"))
    assert dataset["analysis"]["analysis_id"] == "parent-analysis"
    assert dataset["analysis"]["full_pipeline_analysis_id"] == "analysis-full"
    assert dataset["analysis"]["establishment_id"] == "farm-1"
    assert dataset["analysis"]["overall_status"] == "partial"
    assert dataset["headline_metrics"] == {
        "establishment_area_ha": 100.0,
        "forest_area_2020_ha": 60.0,
        "candidate_episode_area_ha": 17.0,
        "candidate_episode_count": 2,
        "spectral_candidate_area_ha": 17.1,
        "spectral_candidate_count": 3,
        "likely_conversion_area_ha": 4.0,
        "conversion_likely_count": 1,
        "automatic_final_assessment_generated": False,
    }
    assert dataset["limitations"] == [
        "Resultado técnico automático sujeto a revisión humana.",
        "No constituye certificación ni confirmación legal EUDR.",
        "full limitation",
        "No se determinó deforestación.",
    ]
    assert [item["candidate_id"] for item in dataset["selected_disturbances"]] == [
        "PDE-A",
        "PDE-B",
    ]
    assert all(
        "agricultural_persistence_schema_version" in item
        for item in dataset["selected_disturbances"]
    )
    selected_a = dataset["selected_disturbances"][0]
    assert selected_a["agricultural_persistence_schema_version"] == "2.0.0"
    assert selected_a["agricultural_persistence"]["signal_strength"] == "weak"
    assert selected_a["agricultural_persistence"]["qualifying_period_count"] == 1
    assert dataset["disturbance_selection"]["complete_event_inventory_path"] == (
        "report_assets/data/main/080_post_change_attribution.json"
    )
    index = json.loads((staging / "report_assets/index.json").read_text("utf-8"))
    report_paths = {item["report_path"] for item in index["files"]}
    assert "report_assets/figures/events/PDE-A/disturbance.png" in report_paths
    assert "report_assets/figures/events/PDE-B/agricultural_monthly.png" in report_paths
    assert not any("PDE-C" in path for path in report_paths)
    assert "report_assets/data/main/030_disturbance_events.json" in report_paths
    assert "report_assets/annex/spatial/disturbance_events.geojson" in report_paths
    assert "report_assets/annex/tables/agricultural_evidence.csv" in report_paths
    assert "report_assets/annex/tables/subthreshold_candidates.csv" in report_paths
    subthreshold_csv = (
        staging / "report_assets/annex/tables/subthreshold_candidates.csv"
    ).read_text("utf-8")
    assert "PDE-C" in subthreshold_csv
    assert "PDE-A" not in subthreshold_csv
    assert "candidate_agricultural_coverage_fraction" in subthreshold_csv
    assert "candidate_agricultural_coverage_threshold" in subthreshold_csv
    assert "candidate_agricultural_coverage_gate_met" in subthreshold_csv
    assert "report_assets/annex/methodology/agricultural_evidence_metadata.json" in report_paths
    assert "report_assets/annex/provenance/hampel_benchmark_component_status.json" in report_paths
    for item in index["files"]:
        copied = staging / item["report_path"]
        assert copied.is_file()
        assert item["report_sha256"] == hashlib.sha256(copied.read_bytes()).hexdigest()
        assert item["source_sha256"] == item["report_sha256"]


def test_report_dataset_loader_preserves_historical_v1_4_without_implicit_upgrade(
    tmp_path: Path,
) -> None:
    path = tmp_path / "report_dataset.json"
    path.write_text('{"schema_version":"1.4.0","selected_events":[]}', encoding="utf-8")

    loaded = load_report_dataset_document(path)

    assert loaded["schema_version"] == "1.4.0"
    assert "selected_disturbances" not in loaded


def test_report_dataset_loader_preserves_historical_v2_0_without_implicit_upgrade(
    tmp_path: Path,
) -> None:
    path = tmp_path / "report_dataset.json"
    path.write_text('{"schema_version":"2.0.0"}', encoding="utf-8")

    loaded = load_report_dataset_document(path)

    assert loaded == {"schema_version": "2.0.0"}


def test_rejects_tampered_or_unsafe_manifested_figure(tmp_path: Path) -> None:
    staging = tmp_path / "run"
    component = _component(
        staging,
        "full_pipeline",
        {"figures/seasonal/qa/rgb_timeline.png": b"original"},
    )
    source = staging / str(component["output_bundle"]) / "figures/seasonal/qa/rgb_timeline.png"
    source.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="report_figure_source_sha256_mismatch"):
        materialize_report_figure_collection(
            staging=staging,
            components=[component],
            overall_status="complete",
            recorded_at="2026-08-12T12:00:00+00:00",
        )
    source.write_bytes(b"original")

    manifest = staging / str(component["child_manifest"])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["artifacts"].append(
        {
            "path": "../figures/seasonal/qa/rgb_timeline.png",
            "size_bytes": 1,
            "sha256": "0" * 64,
        }
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    component["child_manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="report_figure_artifact_path_unsafe"):
        materialize_report_figure_collection(
            staging=staging,
            components=[component],
            overall_status="complete",
            recorded_at="2026-08-12T12:00:00+00:00",
        )


def test_rejects_tampered_component_status_receipt(tmp_path: Path) -> None:
    staging = tmp_path / "run"
    component = _component(
        staging,
        "hampel_benchmark",
        {},
        status="diagnostic_unavailable",
    )
    receipt = staging / str(component["status_receipt"])
    receipt.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="report_asset_status_receipt_sha256_mismatch"):
        materialize_report_figure_collection(
            staging=staging,
            components=[component],
            overall_status="partial",
            recorded_at="2026-08-12T12:00:00+00:00",
        )
