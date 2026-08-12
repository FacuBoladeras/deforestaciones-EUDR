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
    summary.write_text("{}\n", encoding="utf-8")
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
    assert index["selection_policy_version"] == "1.0.0"
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
