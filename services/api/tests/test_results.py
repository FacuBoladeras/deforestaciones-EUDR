"""Pruebas del ciclo resultado-descarga del tercer incremento."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from deforestation_api.app import create_app
from deforestation_api.results import ResultCatalog, _attribution_records, _is_likely_conversion
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


def _completed_bundle(
    settings: ApiSettings, *, legacy_attribution: bool = False
) -> tuple[SQLiteJobRepository, Path]:
    result = settings.storage_root / "analyses" / ANALYSIS_ID / "results" / "attempt-1"
    report_root = result / "report_assets"
    dataset_path = report_root / "report_dataset.json"
    events_path = report_root / "data" / "main" / "080_post_change_attribution.json"
    image_path = report_root / "figures" / "general" / "overview.png"
    likely_geometry = {
        "type": "Polygon",
        "coordinates": [[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.2]]],
    }
    candidate_geometry = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
    }
    attribution = (
        {
            "events": [
                {
                    "event_id": "legacy-likely",
                    "automatic_status": "conversion_likely",
                    "area_ha": 2.0,
                },
                {
                    "event_id": "legacy-candidate",
                    "automatic_status": "review_required",
                    "area_ha": 1.0,
                },
                {
                    "event_id": "legacy-conflicting",
                    "record_type": "conversion_likely_event",
                    "automatic_status": "review_required",
                    "area_ha": 0.8,
                },
            ]
        }
        if legacy_attribution
        else {
            "schema_version": "5.0.0",
            "records": [
                {
                    "candidate_id": "candidate-2",
                    "event_id": "event-2",
                    "record_type": "conversion_likely_event",
                    "interpretation_status": "conversion_likely",
                    "automatic_status": "conversion_likely",
                    "candidate_area_ha": 2.0,
                    "conjunctive_conversion_evidence_area_ha": 0.75,
                    "likely_conversion_area_ha": 0.75,
                    "candidate_geometry": candidate_geometry,
                    "likely_conversion_geometry": likely_geometry,
                },
                {
                    "candidate_id": "candidate-3",
                    "event_id": None,
                    "record_type": "disturbance_candidate",
                    "interpretation_status": "subthreshold_conversion_evidence",
                    "automatic_status": "review_required",
                    "candidate_area_ha": 1.0,
                    "conjunctive_conversion_evidence_area_ha": 0.4,
                    "likely_conversion_area_ha": 0.0,
                    "candidate_geometry": candidate_geometry,
                    "likely_conversion_geometry": None,
                },
                {
                    "candidate_id": "candidate-1",
                    "event_id": None,
                    "record_type": "disturbance_candidate",
                    "interpretation_status": "candidate_only",
                    "automatic_status": "low_risk",
                    "candidate_area_ha": 0.5,
                    "conjunctive_conversion_evidence_area_ha": 0.0,
                    "likely_conversion_area_ha": 0.0,
                    "candidate_geometry": candidate_geometry,
                    "likely_conversion_geometry": None,
                },
            ],
        }
    )
    _write_json(events_path, attribution)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"synthetic-png")
    dataset = {
        "schema_version": "1.2.0",
        "analysis": {"analysis_id": ANALYSIS_ID, "status": "complete"},
        "headline_metrics": {"event_count": 2},
        "selected_disturbances": [{"candidate_id": "candidate-2"}],
        ("event_selection" if legacy_attribution else "disturbance_selection"): {
            "complete_event_inventory_path": (
                "report_assets/data/main/080_post_change_attribution.json"
            ),
            **(
                {}
                if legacy_attribution
                else {
                    "complete_candidate_inventory_path": (
                        "report_assets/data/main/080_post_change_attribution.json"
                    )
                }
            ),
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
    client_report = result / "client_report" / "informe-tecnico.pdf"
    client_report.parent.mkdir(parents=True, exist_ok=True)
    client_report.write_bytes(b"%PDF-1.4\nsynthetic report\n")
    _write_json(
        result / "client_report" / "report.json",
        {
            "media_type": "application/pdf",
            "path": "client_report/informe-tecnico.pdf",
            "schema_version": "1.0.0",
            "sha256": _sha256(client_report),
            "size_bytes": client_report.stat().st_size,
        },
    )

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
        lease_owner_id=claimed.lease_owner_id or "",
        attempt=claimed.attempt,
    )
    repository.complete(
        ANALYSIS_ID,
        status=JobStatus.COMPLETED,
        output_prefix="synthetic-result",
        parent_manifest_sha256=_sha256(manifest_path),
        report_dataset_sha256=_sha256(dataset_path),
        lease_owner_id=claimed.lease_owner_id or "",
        attempt=claimed.attempt,
    )
    return repository, result


def test_report_events_assets_and_allowlisted_asset_download(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _completed_bundle(settings)

    with TestClient(create_app(settings)) as client:
        report = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/report")
        events = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/events?page=1&page_size=1")
        candidates = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/candidates?page=1&page_size=10")
        assets = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/assets?page=1&page_size=10")
        image = next(item for item in assets.json()["items"] if item["media_type"] == "image/png")
        downloaded = client.get(image["download_url"])
        pdf = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/report.pdf")

    assert report.status_code == 200
    assert report.json()["analysis"]["analysis_id"] == ANALYSIS_ID
    event_item = events.json()["items"][0]
    assert event_item["candidate_id"] == "candidate-2"
    assert event_item["record_type"] == "conversion_likely_event"
    assert event_item["geometry"] == event_item["likely_conversion_geometry"]
    assert event_item["geometry"] != event_item["candidate_geometry"]
    assert {**events.json(), "items": ["checked-above"]} == {
        "items": ["checked-above"],
        "page": 1,
        "page_size": 1,
        "total": 1,
        "next_page": None,
    }
    candidate_items = candidates.json()["items"]
    assert [item["candidate_id"] for item in candidate_items] == ["candidate-3", "candidate-1"]
    assert candidate_items[0]["interpretation_status"] == "subthreshold_conversion_evidence"
    assert candidate_items[0]["geometry"] == candidate_items[0]["candidate_geometry"]
    assert candidate_items[0]["event_id"] is None
    assert {**candidates.json(), "items": ["checked-above"]} == {
        "items": ["checked-above"],
        "page": 1,
        "page_size": 10,
        "total": 2,
        "next_page": None,
    }
    assert assets.status_code == 200
    assert assets.json()["total"] == 2
    assert all("report_path" not in item for item in assets.json()["items"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"synthetic-png"
    assert downloaded.headers["etag"] == f'"{image["sha256"]}"'
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].endswith(
        'filename="informe-tecnico-synthetic-establishment-aaaaaaaa.pdf"'
    )
    assert pdf.content.startswith(b"%PDF-")


def test_catalog_resolves_only_the_database_selected_attempt_generation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository, first = _completed_bundle(settings)
    current = repository.get(ANALYSIS_ID)
    assert current is not None and current.attempt == 1
    second = first.parent / "attempt-2"
    shutil.copytree(first, second)
    dataset_path = second / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset["analysis"]["generation"] = 2
    _write_json(dataset_path, dataset)
    index_path = second / "report_assets" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)

    selected = replace(current, attempt=2, report_dataset_sha256=_sha256(dataset_path))

    assert ResultCatalog(settings.storage_root).report(selected)["analysis"]["generation"] == 2
    assert "generation" not in ResultCatalog(settings.storage_root).report(current)["analysis"]


def test_legacy_attribution_is_partitioned_without_promoting_candidates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _completed_bundle(settings, legacy_attribution=True)

    with TestClient(create_app(settings)) as client:
        events = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/events")
        candidates = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/candidates")

    assert [item["event_id"] for item in events.json()["items"]] == ["legacy-likely"]
    assert [item["event_id"] for item in candidates.json()["items"]] == [
        "legacy-candidate",
        "legacy-conflicting",
    ]


def test_partial_published_dataset_without_attribution_inventory_remains_readable(
    tmp_path: Path,
) -> None:
    """Reproduce el contrato 1.3.0 publicado por el smoke real de nativo."""
    settings = _settings(tmp_path)
    repository, result = _completed_bundle(settings)
    current = repository.get(ANALYSIS_ID)
    assert current is not None
    dataset_path = result / "report_assets" / "report_dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset["schema_version"] = "1.3.0"
    dataset["analysis"]["status"] = "partial"
    dataset["component_statuses"] = {
        "post_change_attribution": {
            "status": "failed",
            "available": False,
            "reason": "component_validation_failed",
        }
    }
    dataset["disturbance_selection"] = {
        "complete_candidate_inventory_path": None,
        "complete_event_inventory_path": None,
        "selected_candidate_ids": ["PDE-PARTIAL-1"],
    }
    dataset["selected_disturbances"] = [
        {
            "candidate_id": "PDE-PARTIAL-1",
            "disturbance": {
                "candidate_id": "PDE-PARTIAL-1",
                "area_ha": 0.8,
                "record_type": "persistent_disturbance_candidate",
            },
            "agricultural_persistence": {"persistence_status": "not_persistent"},
            "attribution": None,
        }
    ]
    _write_json(dataset_path, dataset)
    index_path = result / "report_assets" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    detector_path = result / "report_assets" / "data" / "main" / "030_disturbance_events.json"
    _write_json(
        detector_path,
        {
            "events": [
                {
                    "candidate_id": "PDE-PARTIAL-1",
                    "area_ha": 0.8,
                    "record_type": "persistent_disturbance_candidate",
                },
                {
                    "candidate_id": "PDE-NOT-EDITORIALLY-SELECTED",
                    "area_ha": 0.2,
                    "record_type": "persistent_disturbance_candidate",
                },
            ]
        },
    )
    index["files"].append(
        {
            "category": "main_data",
            "component": "full_pipeline",
            "event_id": None,
            "report_path": detector_path.relative_to(result).as_posix(),
            "report_sha256": _sha256(detector_path),
            "size_bytes": detector_path.stat().st_size,
        }
    )
    index["dataset_sha256"] = _sha256(dataset_path)
    _write_json(index_path, index)
    published = replace(current, report_dataset_sha256=_sha256(dataset_path))

    catalog = ResultCatalog(settings.storage_root)

    assert catalog.events(published) == []
    assert catalog.candidates(published) == [
        {
            "candidate_id": "PDE-PARTIAL-1",
            "area_ha": 0.8,
            "record_type": "persistent_disturbance_candidate",
        },
        {
            "candidate_id": "PDE-NOT-EDITORIALLY-SELECTED",
            "area_ha": 0.2,
            "record_type": "persistent_disturbance_candidate",
        },
    ]


def test_v4_partitioned_inventory_and_interpretation_only_records_remain_compatible() -> None:
    records = _attribution_records(
        {
            "events": [{"candidate_id": "event-1"}],
            "candidates": [{"candidate_id": "candidate-1"}],
        },
        "events",
    )

    assert [record["candidate_id"] for record in records] == ["event-1", "candidate-1"]
    assert _is_likely_conversion({"interpretation_status": "conversion_likely"}) is True
    assert _is_likely_conversion({"interpretation_status": "candidate_only"}) is False


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
        settings.storage_root
        / "analyses"
        / ANALYSIS_ID
        / "downloads"
        / "attempt-1"
        / "evidence-package.zip"
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
    scientific = tmp_path / "outputs" / "must-remain.txt"
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


def test_client_report_download_rejects_missing_and_tampered_pdf(tmp_path: Path) -> None:
    missing_settings = _settings(tmp_path / "missing")
    _completed_bundle(missing_settings)
    (
        missing_settings.storage_root
        / "analyses"
        / ANALYSIS_ID
        / "results"
        / "attempt-1"
        / "client_report"
        / "informe-tecnico.pdf"
    ).unlink()

    with TestClient(create_app(missing_settings)) as client:
        missing = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/report.pdf")

    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "report_pdf_not_ready"

    tampered_settings = _settings(tmp_path / "tampered-pdf")
    _completed_bundle(tampered_settings)
    pdf = (
        tampered_settings.storage_root
        / "analyses"
        / ANALYSIS_ID
        / "results"
        / "attempt-1"
        / "client_report"
        / "informe-tecnico.pdf"
    )
    pdf.write_bytes(b"%PDF-1.4\ntampered\n")

    with TestClient(create_app(tampered_settings)) as client:
        tampered = client.get(f"/api/v1/analyses/{ANALYSIS_ID}/report.pdf")

    assert tampered.status_code == 409
    assert tampered.json()["error"]["code"] == "report_pdf_not_ready"
