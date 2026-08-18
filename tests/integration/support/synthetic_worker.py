"""Worker one-shot descartable con un runner que no consulta GEE."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from deforestation_pipeline.complete_analysis import CompleteAnalysisRequest
from deforestation_worker.service import AnalysisWorker, WorkerSettings


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def synthetic_runner(request: CompleteAnalysisRequest, *, analysis_id: UUID) -> Path:
    result = request.output_root / f"synthetic-e2e-{analysis_id}"
    report_root = result / "report_assets"
    events_path = report_root / "data" / "main" / "events.json"
    image_path = report_root / "figures" / "general" / "overview.png"
    dataset_path = report_root / "report_dataset.json"

    _write_json(
        events_path,
        {"events": [{"event_id": "synthetic-event-1", "area_ha": 0.75}]},
    )
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"synthetic-png")
    _write_json(
        dataset_path,
        {
            "schema_version": "2.0.0",
            "analysis": {
                "analysis_id": str(analysis_id),
                "establishment_id": request.establishment_id,
                "status": "complete",
            },
            "headline_metrics": {"event_count": 1},
            "event_selection": {
                "complete_event_inventory_path": ("report_assets/data/main/events.json")
            },
            "limitations": ["synthetic integration fixture; no scientific execution"],
        },
    )
    files = []
    for category, component, event_id, path in (
        ("data_main", "main_pipeline", None, events_path),
        ("figure_general", "main_pipeline", None, image_path),
    ):
        files.append(
            {
                "category": category,
                "component": component,
                "event_id": event_id,
                "report_path": path.relative_to(result).as_posix(),
                "report_sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    _write_json(
        report_root / "index.json",
        {
            "schema_version": "2.0.0",
            "dataset_path": "report_assets/report_dataset.json",
            "dataset_sha256": _sha256(dataset_path),
            "files": files,
        },
    )
    _write_json(
        result / "run_manifest.json",
        {
            "analysis_id": str(analysis_id),
            "overall_status": "complete",
            "execution_mode": "synthetic-e2e",
        },
    )
    return result


def main() -> None:
    settings = WorkerSettings.from_environment()
    worker = AnalysisWorker(settings, runner=synthetic_runner)
    worker.initialize()
    if not worker.run_once():
        raise RuntimeError("synthetic_e2e_job_missing")


if __name__ == "__main__":
    main()
