"""Pruebas del ciclo resultado-descarga del tercer incremento."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from deforestation_api.app import create_app
from deforestation_api.settings import ApiSettings
from deforestation_jobs.models import AnalysisJob, JobStatus
from deforestation_jobs.repository import SQLiteJobRepository

ANALYSIS_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _settings(tmp_path: Path) -> ApiSettings:
    boundary = tmp_path / "boundary.geojson"
    _write_json(
        boundary,
        {
            "type": "Polygon",
            "coordinates": [
                [[-65.0, -35.0], [-58.0, -35.0], [-58.0, -28.0], [-65.0, -28.0], [-65.0, -35.0]]
            ],
        },
    )
    return ApiSettings(
        database_path=tmp_path / "private" / "jobs.sqlite3",
        storage_root=tmp_path / "private" / "objects",
        output_root=tmp_path / "outputs",
        jurisdiction_boundary_path=boundary,
        owner_id="local-internal",
    )


def _job(*, expires_at: datetime | None = None) -> AnalysisJob:
    now = datetime.now(UTC)
    return AnalysisJob(
        analysis_id=ANALYSIS_ID,
        owner_id="local-internal",
        idempotency_key="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        request_sha256="a" * 64,
        establishment_id="synthetic-establishment",
        input_object_key=f"analyses/{ANALYSIS_ID}/input.geojson",
        input_sha256="b" * 64,
        status=JobStatus.QUEUED,
        stage="queued",
        attempt=0,
        created_at=now,
        updated_at=now,
        configuration_versions="{}",
        code_revision="test",
        expires_at=expires_at or now + timedelta(days=30),
    )


def _completed_bundle(settings: ApiSettings) -> tuple[SQLiteJobRepository, Path]:
    result = settings.output_root / "synthetic-result"
    report_root = result / "report_assets"
    dataset_path = report_root / "report_dataset.json"
    events_path = report_root / "data" / "main" / "030_disturbance_events.json"
    image_path = report_root / "figures" / "general" / "overview.png"
    _write_json(
        events_path,
        {
            "events": [
                {"event_id": "event-2", "area_ha": 2.0},
                {"event_id": "event-1", "area_ha": 1.0},
            ]
        },
    )
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"synthetic-png")
    dataset = {
        "schema_version": "2.0.0",
        "analysis": {"analysis_id": ANALYSIS_ID, "status": "complete"},
        "headline_metrics": {"event_count": 2},
        "selected_events": [{"event_id": "event-2"}],
        "event_selection": {
            "complete_event_inventory_path": "report_assets/data/main/030_disturbance_events.json"
        },
        "component_statuses": {},
        "source_paths_by_category": {},
        "limitations": ["synthetic fixture"],
    }
    _write_json(dataset_path, dataset)
    files = []
    for category, path in (("data_main", events_path), ("figure_general", image_path)):
        files.append(
            {
                "category": category,
                "component": "main_pipeline",
                "event_id": None,
                "report_path": path.relative_to(result).as_posix(),
                "report_sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    index_path = report_root / "index.json"
    _write_json(
        index_path,
        {
            "schema_version": "2.0.0",
            "dataset_path": "report_assets/report_dataset.json",
            "dataset_sha256": _sha256(dataset_path),
            "files": files,
        },
    )
    manifest_path = result / "run_manifest.json"
    _write_json(manifest_path, {"overall_status": "complete"})

    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    repository.create_or_get(_job())
    claimed = repository.claim_next("test-worker")
    assert claimed is not None
    repository.transition(
        ANALYSIS_ID,
        expected=JobStatus.VALIDATING,
        target=JobStatus.RUNNING,
        stage="full_pipeline",
    )
    repository.complete(
        ANALYSIS_ID,
        status=JobStatus.COMPLETED,
        output_prefix="synthetic-result",
        parent_manifest_sha256=_sha256(manifest_path),
        report_dataset_sha256=_sha256(dataset_path),
    )
    return repository, result


def test_report_events_assets_and_allowlisted_asset_download(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _completed_bundle(settings)

    with TestClient(create_app(settings)) as client:
        report = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/report")
        events = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/events?page=1&page_size=1")
        assets = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/assets?page=1&page_size=10")
        image = next(item for item in assets.json()["items"] if item["media_type"] == "image/png")
        downloaded = client.get(image["download_url"])

    assert report.status_code == 200
    assert report.json()["analysis"]["analysis_id"] == ANALYSIS_ID
    assert events.json() == {
        "items": [{"event_id": "event-2", "area_ha": 2.0}],
        "page": 1,
        "page_size": 1,
        "total": 2,
        "next_page": 2,
    }
    assert assets.status_code == 200
    assert assets.json()["total"] == 2
    assert all("report_path" not in item for item in assets.json()["items"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"synthetic-png"
    assert downloaded.headers["etag"] == f'"{image["sha256"]}"'


def test_results_reject_unready_expired_and_tampered_bundles(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    repository.create_or_get(_job())

    with TestClient(create_app(settings)) as client:
        unready = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/report")

    assert unready.status_code == 409
    assert unready.json()["error"]["code"] == "analysis_not_ready"

    expired_settings = _settings(tmp_path / "expired")
    expired_repository = SQLiteJobRepository(expired_settings.database_path)
    expired_repository.initialize()
    expired_repository.create_or_get(_job(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    with TestClient(create_app(expired_settings)) as client:
        expired = client.get(f"/api/v1/analyses/{ANALYSIS_ID}")
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "analysis_expired"

    tampered_settings = _settings(tmp_path / "tampered")
    _, result = _completed_bundle(tampered_settings)
    (result / "report_assets" / "report_dataset.json").write_text("{}", encoding="utf-8")
    with TestClient(create_app(tampered_settings)) as client:
        tampered = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/report")
    assert tampered.status_code == 409
    assert tampered.json()["error"]["code"] == "result_integrity_error"


def test_cancel_and_packaged_download_are_private_and_verifiable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository, _result = _completed_bundle(settings)
    package = (
        settings.storage_root / "analyses" / ANALYSIS_ID / "downloads" / "evidence-package.zip"
    )
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"synthetic-zip")
    _write_json(
        package.with_suffix(".json"),
        {"sha256": _sha256(package), "size_bytes": package.stat().st_size},
    )

    with TestClient(create_app(settings)) as client:
        download = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/download")
        terminal_cancel = client.post(f"/api/v1/analyses/{ANALYSIS_ID}/cancel")

    assert download.status_code == 200
    assert download.content == b"synthetic-zip"
    assert str(settings.storage_root) not in download.text
    assert terminal_cancel.status_code == 200
    assert terminal_cancel.json()["status"] == "completed"
    assert repository.get(ANALYSIS_ID) is not None


def test_startup_purges_only_expired_private_objects(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    repository.create_or_get(_job(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    expired_private = settings.storage_root / "analyses" / ANALYSIS_ID
    expired_private.mkdir(parents=True)
    (expired_private / "input.geojson").write_text("sensitive", encoding="utf-8")
    scientific = settings.output_root / "must-remain.txt"
    scientific.parent.mkdir(parents=True)
    scientific.write_text("preserve", encoding="utf-8")

    with TestClient(create_app(settings)):
        pass

    assert not expired_private.exists()
    assert scientific.read_text(encoding="utf-8") == "preserve"


def test_missing_allowlisted_asset_and_package_return_stable_errors(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _completed_bundle(settings)

    with TestClient(create_app(settings)) as client:
        asset = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/assets/{'0' * 64}")
        package = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/download")

    assert asset.status_code == 404
    assert asset.json()["error"]["code"] == "asset_not_found"
    assert package.status_code == 409
    assert package.json()["error"]["code"] == "download_not_ready"
