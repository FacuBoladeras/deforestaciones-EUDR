from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deforestation_pipeline.report_figures import materialize_report_figure_collection


def _component(
    staging: Path,
    name: str,
    figures: dict[str, bytes],
    *,
    status: str = "completed",
    documents: dict[str, object | str | bytes] | None = None,
) -> dict[str, object]:
    component: dict[str, object] = {
        "name": name,
        "status": status,
        "required_for_publication": name != "hampel_benchmark",
        "status_receipt": None,
    }
    if status != "completed":
        receipt = staging / "components" / name / "component_status.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"component": name, "status": status}), encoding="utf-8")
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
    assert result["figure_count"] == 15
    assert len(index["figures"]) == 15
    assert index["selection_policy_version"] == "2.0.0"
    assert index["component_statuses"]["hampel_benchmark"] == {
        "status": "diagnostic_unavailable",
        "selected_figure_count": 0,
        "status_receipt": "components/hampel_benchmark/component_status.json",
        "status_receipt_sha256": components[1]["status_receipt_sha256"],
    }
    report_paths = [item["report_path"] for item in index["figures"]]
    assert report_paths == sorted(report_paths)
    assert all(path.startswith("report_assets/figures/") for path in report_paths)
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
        "schema_version": "1.0.0",
        "event_count": 3,
        "total_event_area_ha": 17.1,
        "events": [
            {"event_id": "PDE-A", "area_ha": 10.0, "area_threshold_met": True},
            {"event_id": "PDE-B", "area_ha": 7.0, "area_threshold_met": True},
            {"event_id": "PDE-C", "area_ha": 0.1, "area_threshold_met": False},
        ],
    }
    attribution = {
        "schema_version": "3.0.0",
        "status": "review_required",
        "likely_conversion_area_ha": 4.0,
        "conversion_likely_count": 1,
        "events": [
            {
                "event_id": "PDE-A",
                "automatic_status": "conversion_likely",
                "area_ha": 10.0,
                "area_threshold_met": True,
                "likely_conversion_area_ha": 4.0,
            },
            {
                "event_id": "PDE-B",
                "automatic_status": "review_required",
                "area_ha": 7.0,
                "area_threshold_met": True,
                "likely_conversion_area_ha": 0.0,
            },
            {
                "event_id": "PDE-C",
                "automatic_status": "review_required",
                "area_ha": 0.1,
                "area_threshold_met": False,
                "likely_conversion_area_ha": 0.0,
            },
        ],
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
                "json/evidence/agricultural_persistence.json": {"events": []},
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

    assert result["schema_version"] == "2.0.0"
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
        "detected_event_area_ha": 17.1,
        "detected_event_count": 3,
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
    assert [event["event_id"] for event in dataset["selected_events"]] == [
        "PDE-A",
        "PDE-B",
    ]
    index = json.loads((staging / "report_assets/index.json").read_text("utf-8"))
    report_paths = {item["report_path"] for item in index["files"]}
    assert "report_assets/figures/events/PDE-A/disturbance.png" in report_paths
    assert "report_assets/figures/events/PDE-B/agricultural_monthly.png" in report_paths
    assert not any("PDE-C" in path for path in report_paths)
    assert "report_assets/data/main/030_disturbance_events.json" in report_paths
    assert "report_assets/annex/spatial/disturbance_events.geojson" in report_paths
    assert "report_assets/annex/tables/agricultural_evidence.csv" in report_paths
    assert "report_assets/annex/methodology/agricultural_evidence_metadata.json" in report_paths
    assert "report_assets/annex/provenance/hampel_benchmark_component_status.json" in report_paths
    for item in index["files"]:
        copied = staging / item["report_path"]
        assert copied.is_file()
        assert item["report_sha256"] == hashlib.sha256(copied.read_bytes()).hexdigest()
        assert item["source_sha256"] == item["report_sha256"]


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
