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
        {
            "schema_version": "5.0.0",
            "records": [
                {
                    "candidate_id": "synthetic-candidate-1",
                    "event_id": "synthetic-event-1",
                    "record_type": "conversion_likely_event",
                    "interpretation_status": "conversion_likely",
                    "automatic_status": "conversion_likely",
                    "candidate_area_ha": 1.25,
                    "conjunctive_conversion_evidence_area_ha": 0.75,
                    "likely_conversion_area_ha": 0.75,
                    "candidate_geometry": {"type": "Polygon", "coordinates": []},
                    "likely_conversion_geometry": {"type": "Polygon", "coordinates": []},
                },
                {
                    "candidate_id": "synthetic-candidate-2",
                    "event_id": None,
                    "record_type": "disturbance_candidate",
                    "interpretation_status": "subthreshold_conversion_evidence",
                    "automatic_status": "review_required",
                    "candidate_area_ha": 0.8,
                    "conjunctive_conversion_evidence_area_ha": 0.4,
                    "likely_conversion_area_ha": 0.0,
                    "candidate_geometry": {"type": "Polygon", "coordinates": []},
                    "likely_conversion_geometry": None,
                },
            ],
        },
    )
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"synthetic-png")
    _write_json(
        dataset_path,
        {
            "schema_version": "1.3.0",
            "analysis": {
                "analysis_id": str(analysis_id),
                "establishment_id": request.establishment_id,
                "status": "complete",
            },
            "headline_metrics": {"conversion_likely_count": 1},
            "disturbance_selection": {
                "complete_event_inventory_path": "report_assets/data/main/events.json",
                "complete_candidate_inventory_path": "report_assets/data/main/events.json",
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
            "schema_version": "2.1.0",
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


def synthetic_report_renderer(
    _result_root: Path,
    output_path: Path,
    _input_path: Path,
) -> Path:
    """Emula sólo el renderer en el gate E2E; producción no tiene modo sintético."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"%PDF-1.4\nsynthetic integration report\n")
    return output_path


def main() -> None:
    settings = WorkerSettings.from_environment()
    worker = AnalysisWorker(
        settings,
        runner=synthetic_runner,
        report_renderer=synthetic_report_renderer,
    )
    worker.initialize()
    if not worker.run_once():
        raise RuntimeError("synthetic_e2e_job_missing")


if __name__ == "__main__":
    main()
