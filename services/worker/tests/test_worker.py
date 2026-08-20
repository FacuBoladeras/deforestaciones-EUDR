"""Pruebas del worker local separado sin ejecutar Google Earth Engine."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

import deforestation_worker.service as worker_service
from deforestation_jobs.models import AnalysisJob, JobStatus
from deforestation_jobs.repository import SQLiteJobRepository
from deforestation_pipeline.complete_analysis import CompleteAnalysisError, CompleteAnalysisRequest
from deforestation_worker.package import create_evidence_package, publish_curated_results
from deforestation_worker.service import AnalysisWorker, WorkerSettings, render_client_report


def _enqueue(repository: SQLiteJobRepository, storage_root: Path) -> AnalysisJob:
    analysis_id = "77777777-7777-4777-8777-777777777777"
    relative = Path("analyses") / analysis_id / "input.geojson"
    payload = {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-60.3, -32.1], [-60.2, -32.1], [-60.2, -32.0], [-60.3, -32.1]]],
        },
    }
    content = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    input_path = storage_root / relative
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(content)
    now = datetime(2026, 8, 17, tzinfo=UTC)
    job = AnalysisJob(
        analysis_id=analysis_id,
        owner_id="local-internal",
        idempotency_key="88888888-8888-4888-8888-888888888888",
        request_sha256="a" * 64,
        establishment_id="establecimiento-2",
        input_object_key=relative.as_posix(),
        input_sha256=hashlib.sha256(content).hexdigest(),
        status=JobStatus.QUEUED,
        stage="queued",
        attempt=0,
        created_at=now,
        updated_at=now,
        configuration_versions="{}",
        code_revision="test-revision",
        expires_at=now + timedelta(days=30),
        declared_land_use="unknown",
        declared_context_source="user_declared",
    )
    repository.create_or_get(job)
    return job


def _settings(tmp_path: Path) -> WorkerSettings:
    project_root = Path(__file__).resolve().parents[3]
    return WorkerSettings(
        database_path=tmp_path / "jobs.sqlite3",
        storage_root=tmp_path / "objects",
        output_root=tmp_path / "outputs",
        project_root=project_root,
        credentials_path=None,
        poll_interval_seconds=0.01,
        worker_id="test-worker",
    )


def test_default_report_adapter_loads_package_and_returns_rendered_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_root = tmp_path / "result"
    output_path = tmp_path / "report.pdf"
    input_path = tmp_path / "private" / "input.geojson"
    package = object()
    monkeypatch.setattr(
        worker_service,
        "load_report_package",
        lambda root, *, input_path: (
            package
            if root == result_root and input_path == tmp_path / "private" / "input.geojson"
            else None
        ),
    )

    def fake_render(received: object, output: Path) -> SimpleNamespace:
        assert received is package
        assert output == output_path
        return SimpleNamespace(path=output)

    monkeypatch.setattr(worker_service, "render_technical_report", fake_render)

    assert render_client_report(result_root, output_path, input_path) == output_path


def test_worker_claims_once_invokes_python_runner_and_publishes_completion(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    expected = _enqueue(repository, settings.storage_root)

    def fake_runner(request: CompleteAnalysisRequest, *, analysis_id: UUID) -> Path:
        assert request.establishment_id == expected.establishment_id
        assert request.input_path.is_file()
        assert request.output_root == settings.output_root.resolve()
        assert str(analysis_id) == expected.analysis_id
        result = settings.output_root / "synthetic-complete"
        (result / "report_assets").mkdir(parents=True)
        (result / "run_manifest.json").write_text(
            json.dumps({"overall_status": "complete"}), encoding="utf-8"
        )
        dataset = result / "report_assets" / "report_dataset.json"
        dataset.write_text(json.dumps({"analysis_id": expected.analysis_id}), encoding="utf-8")
        (result / "report_assets" / "index.json").write_text(
            json.dumps(
                {
                    "dataset_path": "report_assets/report_dataset.json",
                    "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        return result

    def fake_report_renderer(result_root: Path, output_path: Path, input_path: Path) -> Path:
        assert result_root == settings.output_root / "synthetic-complete"
        assert input_path == settings.storage_root / expected.input_object_key
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"%PDF-1.4\nsynthetic client report\n")
        return output_path

    worker = AnalysisWorker(
        settings,
        repository=repository,
        runner=fake_runner,
        report_renderer=fake_report_renderer,
    )

    assert worker.run_once() is True
    assert worker.run_once() is False
    completed = repository.get(expected.analysis_id)
    assert completed is not None
    assert completed.status is JobStatus.COMPLETED
    assert completed.stage == "completed"
    assert completed.attempt == 1
    assert completed.output_prefix == "synthetic-complete"
    assert completed.parent_manifest_sha256 is not None
    assert completed.report_dataset_sha256 is not None
    package = (
        settings.storage_root
        / "analyses"
        / expected.analysis_id
        / "downloads"
        / "evidence-package.zip"
    )
    assert package.is_file()
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist()) == {
            "client_report/informe-tecnico.pdf",
            "client_report/report.json",
            "run_manifest.json",
            "report_assets/index.json",
            "report_assets/report_dataset.json",
        }
        assert archive.read("client_report/informe-tecnico.pdf").startswith(b"%PDF-")
    metadata = json.loads(package.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata == {
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "size_bytes": package.stat().st_size,
    }
    published = settings.storage_root / "analyses" / expected.analysis_id / "results"
    assert (published / "run_manifest.json").is_file()
    assert (published / "report_assets" / "index.json").is_file()
    assert (published / "report_assets" / "report_dataset.json").is_file()
    published_pdf = published / "client_report" / "informe-tecnico.pdf"
    assert published_pdf.read_bytes().startswith(b"%PDF-")
    report_metadata = json.loads(
        (published / "client_report" / "report.json").read_text(encoding="utf-8")
    )
    assert report_metadata == {
        "media_type": "application/pdf",
        "path": "client_report/informe-tecnico.pdf",
        "schema_version": "1.0.0",
        "sha256": hashlib.sha256(published_pdf.read_bytes()).hexdigest(),
        "size_bytes": published_pdf.stat().st_size,
    }
    assert not (
        settings.storage_root / "analyses" / expected.analysis_id / ".client-report.tmp.pdf"
    ).exists()


def test_worker_requeues_interrupted_job_before_polling(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    job = _enqueue(repository, settings.storage_root)
    claimed = repository.claim_next("dead-worker")
    assert claimed is not None

    worker = AnalysisWorker(
        settings,
        repository=repository,
        runner=lambda *_args, **_kwargs: settings.output_root,
    )
    recovered = worker.recover_interrupted_jobs()

    assert recovered == 1
    current = repository.get(job.analysis_id)
    assert current is not None
    assert current.status is JobStatus.QUEUED
    assert current.stage == "recovered_after_restart"


def test_worker_honors_cancellation_before_starting_scientific_runner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    job = _enqueue(repository, settings.storage_root)
    claimed = repository.claim_next("test-worker")
    assert claimed is not None
    repository.request_cancellation(job.analysis_id)
    invoked = False

    def runner(_request: CompleteAnalysisRequest, *, analysis_id: UUID) -> Path:
        nonlocal invoked
        invoked = True
        raise AssertionError(analysis_id)

    worker = AnalysisWorker(settings, repository=repository, runner=runner)

    assert worker.process_claimed(claimed) is True
    cancelled = repository.get(job.analysis_id)
    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert invoked is False


def test_worker_records_safe_failure_code_without_exception_message(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    job = _enqueue(repository, settings.storage_root)

    def failing_runner(_request: CompleteAnalysisRequest, *, analysis_id: UUID) -> Path:
        raise RuntimeError(f"token=secret-for-{analysis_id}")

    worker = AnalysisWorker(settings, repository=repository, runner=failing_runner)
    assert worker.run_once() is True

    failed = repository.get(job.analysis_id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED
    assert failed.safe_error_code == "analysis_execution_failed"
    assert "secret" not in json.dumps(failed.to_public_dict())


def test_worker_preserves_partial_bundle_reported_by_pipeline(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    job = _enqueue(repository, settings.storage_root)

    def partial_runner(_request: CompleteAnalysisRequest, *, analysis_id: UUID) -> Path:
        result = settings.output_root / "synthetic-partial.failed"
        (result / "report_assets").mkdir(parents=True)
        (result / "run_manifest.json").write_text(
            json.dumps({"overall_status": "partial"}), encoding="utf-8"
        )
        dataset = result / "report_assets" / "report_dataset.json"
        dataset.write_text(json.dumps({"analysis_id": str(analysis_id)}), encoding="utf-8")
        (result / "report_assets" / "index.json").write_text(
            json.dumps(
                {
                    "dataset_path": "report_assets/report_dataset.json",
                    "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        raise CompleteAnalysisError("complete_analysis_partial", result)

    def fake_report_renderer(
        _result_root: Path,
        output_path: Path,
        _input_path: Path,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"%PDF-1.4\nsynthetic partial report\n")
        return output_path

    worker = AnalysisWorker(
        settings,
        repository=repository,
        runner=partial_runner,
        report_renderer=fake_report_renderer,
    )
    assert worker.run_once() is True

    partial = repository.get(job.analysis_id)
    assert partial is not None
    assert partial.status is JobStatus.PARTIAL
    assert partial.output_prefix == "synthetic-partial.failed"


def test_partial_pipeline_failure_also_contains_report_renderer_errors(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    job = _enqueue(repository, settings.storage_root)

    def partial_runner(_request: CompleteAnalysisRequest, *, analysis_id: UUID) -> Path:
        result = settings.output_root / "partial-with-report-failure.failed"
        report = result / "report_assets"
        report.mkdir(parents=True)
        (result / "run_manifest.json").write_text(
            json.dumps({"overall_status": "partial"}),
            encoding="utf-8",
        )
        dataset = report / "report_dataset.json"
        dataset.write_text(json.dumps({"analysis_id": str(analysis_id)}), encoding="utf-8")
        (report / "index.json").write_text(
            json.dumps(
                {
                    "dataset_path": "report_assets/report_dataset.json",
                    "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        raise CompleteAnalysisError("partial", result)

    def failing_report_renderer(_result: Path, _output: Path, _input: Path) -> Path:
        raise RuntimeError("secret renderer detail")

    worker = AnalysisWorker(
        settings,
        repository=repository,
        runner=partial_runner,
        report_renderer=failing_report_renderer,
    )

    assert worker.run_once() is True
    failed = repository.get(job.analysis_id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED
    assert failed.safe_error_code == "analysis_execution_failed"


def test_worker_rejects_tampered_input_and_output_outside_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    first = _enqueue(repository, settings.storage_root)
    input_path = settings.storage_root / first.input_object_key
    input_path.write_text("tampered", encoding="utf-8")

    assert AnalysisWorker(settings, repository=repository).run_once() is True
    failed = repository.get(first.analysis_id)
    assert failed is not None and failed.status is JobStatus.FAILED

    second = replace(
        first,
        analysis_id="99999999-9999-4999-8999-999999999999",
        idempotency_key="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        input_object_key="analyses/99999999-9999-4999-8999-999999999999/input.geojson",
    )
    target = settings.storage_root / second.input_object_key
    target.parent.mkdir(parents=True, exist_ok=True)
    valid_content = b"{}\n"
    target.write_bytes(valid_content)
    second = replace(second, input_sha256=hashlib.sha256(valid_content).hexdigest())
    repository.create_or_get(second)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run_manifest.json").write_text(
        json.dumps({"overall_status": "complete"}), encoding="utf-8"
    )
    worker = AnalysisWorker(
        settings,
        repository=repository,
        runner=lambda *_args, **_kwargs: outside,
    )
    assert worker.run_once() is True
    rejected = repository.get(second.analysis_id)
    assert rejected is not None and rejected.status is JobStatus.FAILED


def test_invalid_partial_manifest_falls_back_to_failed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    job = _enqueue(repository, settings.storage_root)

    def invalid_partial(_request: CompleteAnalysisRequest, *, analysis_id: UUID) -> Path:
        failure = settings.output_root / f"{analysis_id}.failed"
        failure.mkdir(parents=True)
        raise CompleteAnalysisError("failed", failure)

    worker = AnalysisWorker(settings, repository=repository, runner=invalid_partial)
    assert worker.run_once() is True
    failed = repository.get(job.analysis_id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED


def test_partial_failure_outside_output_root_is_safely_failed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository = SQLiteJobRepository(settings.database_path)
    repository.initialize()
    job = _enqueue(repository, settings.storage_root)

    def unsafe_partial(_request: CompleteAnalysisRequest, *, analysis_id: UUID) -> Path:
        failure = tmp_path / "outside-partial.failed"
        failure.mkdir(parents=True)
        (failure / "run_manifest.json").write_text(
            json.dumps({"overall_status": "partial", "analysis_id": str(analysis_id)}),
            encoding="utf-8",
        )
        raise CompleteAnalysisError("partial", failure)

    worker = AnalysisWorker(settings, repository=repository, runner=unsafe_partial)

    assert worker.run_once() is True
    failed = repository.get(job.analysis_id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED


def test_settings_from_environment_and_forever_idle_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEFORESTATION_API_PRIVATE_ROOT", str(tmp_path / "private"))
    monkeypatch.setenv("DEFORESTATION_ANALYSIS_OUTPUT_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("DEFORESTATION_GEE_CREDENTIALS", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("DEFORESTATION_WORKER_POLL_SECONDS", "0.25")
    monkeypatch.setenv("DEFORESTATION_WORKER_ID", "worker-env")
    settings = WorkerSettings.from_environment()

    assert settings.database_path == tmp_path / "private" / "jobs.sqlite3"
    assert settings.output_root == tmp_path / "runs"
    assert settings.credentials_path == tmp_path / "credentials.json"
    assert settings.poll_interval_seconds == 0.25
    assert settings.worker_id == "worker-env"

    repository = SQLiteJobRepository(settings.database_path)
    worker = AnalysisWorker(settings, repository=repository)
    monkeypatch.setattr(worker, "run_once", lambda: False)

    def stop_after_sleep(seconds: float) -> None:
        assert seconds == 0.25
        raise RuntimeError("stop-loop")

    monkeypatch.setattr("deforestation_worker.service.time.sleep", stop_after_sleep)
    with pytest.raises(RuntimeError, match="stop-loop"):
        worker.run_forever()


def test_evidence_package_includes_only_verified_allowlisted_files(tmp_path: Path) -> None:
    result = tmp_path / "result"
    report = result / "report_assets"
    asset = report / "figures" / "overview.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")
    dataset = report / "report_dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    (result / "run_manifest.json").write_text("{}", encoding="utf-8")
    (report / "index.json").write_text(
        json.dumps(
            {
                "dataset_path": "report_assets/report_dataset.json",
                "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                "files": [
                    {
                        "report_path": "report_assets/figures/overview.png",
                        "report_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                        "size_bytes": asset.stat().st_size,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    package = create_evidence_package(result, tmp_path / "private", "analysis-1")

    assert package is not None
    with zipfile.ZipFile(package) as archive:
        assert "report_assets/figures/overview.png" in archive.namelist()

    asset.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="size_mismatch"):
        create_evidence_package(result, tmp_path / "private", "analysis-1")


def test_client_report_is_published_and_packaged_outside_scientific_allowlist(
    tmp_path: Path,
) -> None:
    result = tmp_path / "result"
    report = result / "report_assets"
    report.mkdir(parents=True)
    dataset = report / "report_dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    (result / "run_manifest.json").write_text("{}", encoding="utf-8")
    (report / "index.json").write_text(
        json.dumps(
            {
                "dataset_path": "report_assets/report_dataset.json",
                "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    client_report = tmp_path / "generated.pdf"
    client_report.write_bytes(b"%PDF-1.4\nclient report\n")

    published = publish_curated_results(
        result,
        tmp_path / "private",
        "analysis-1",
        client_report=client_report,
    )
    package = create_evidence_package(
        result,
        tmp_path / "private",
        "analysis-1",
        client_report=client_report,
    )

    assert published is not None
    assert package is not None
    assert (published / "client_report" / "informe-tecnico.pdf").read_bytes() == (
        client_report.read_bytes()
    )
    with zipfile.ZipFile(package) as archive:
        assert archive.read("client_report/informe-tecnico.pdf") == client_report.read_bytes()
        metadata = json.loads(archive.read("client_report/report.json"))
    assert metadata["sha256"] == hashlib.sha256(client_report.read_bytes()).hexdigest()

    client_report.write_bytes(b"not-a-pdf")
    with pytest.raises(ValueError, match="client_report_invalid"):
        publish_curated_results(
            result,
            tmp_path / "private",
            "analysis-1",
            client_report=client_report,
        )


def test_curated_results_are_published_from_the_verified_allowlist(tmp_path: Path) -> None:
    result = tmp_path / "result"
    report = result / "report_assets"
    asset = report / "figures" / "overview.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")
    dataset = report / "report_dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    manifest = result / "run_manifest.json"
    manifest.write_text('{"overall_status":"complete"}', encoding="utf-8")
    (report / "index.json").write_text(
        json.dumps(
            {
                "dataset_path": "report_assets/report_dataset.json",
                "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                "files": [
                    {
                        "report_path": "report_assets/figures/overview.png",
                        "report_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                        "size_bytes": asset.stat().st_size,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    published = publish_curated_results(result, tmp_path / "private", "analysis-1")

    assert published == tmp_path / "private" / "analyses" / "analysis-1" / "results"
    assert (published / "run_manifest.json").read_bytes() == manifest.read_bytes()
    assert (published / "report_assets" / "figures" / "overview.png").read_bytes() == b"png"
    assert not (published / "unlisted.txt").exists()

    asset.write_bytes(b"new")
    index = json.loads((report / "index.json").read_text(encoding="utf-8"))
    index["files"][0]["report_sha256"] = hashlib.sha256(asset.read_bytes()).hexdigest()
    (report / "index.json").write_text(json.dumps(index), encoding="utf-8")
    republished = publish_curated_results(result, tmp_path / "private", "analysis-1")
    assert republished is not None
    assert (republished / "report_assets" / "figures" / "overview.png").read_bytes() == b"new"


def test_publication_handles_missing_contract_unsafe_id_and_stale_staging(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    assert create_evidence_package(empty, tmp_path / "private", "analysis-1") is None
    assert publish_curated_results(empty, tmp_path / "private", "analysis-1") is None

    result = tmp_path / "result"
    report = result / "report_assets"
    report.mkdir(parents=True)
    dataset = report / "report_dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    (result / "run_manifest.json").write_text("{}", encoding="utf-8")
    (report / "index.json").write_text(
        json.dumps(
            {
                "dataset_path": "report_assets/report_dataset.json",
                "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                "files": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="analysis_id_unsafe"):
        publish_curated_results(result, tmp_path / "private", "../escape")

    staging = tmp_path / "private" / "analyses" / "analysis-1" / ".results.tmp"
    staging.mkdir(parents=True)
    (staging / "interrupted.txt").write_text("stale", encoding="utf-8")
    published = publish_curated_results(result, tmp_path / "private", "analysis-1")
    assert published is not None
    assert not (published / "interrupted.txt").exists()

    previous = published.with_name(".results.previous")
    published.rename(previous)
    recovered = publish_curated_results(result, tmp_path / "private", "analysis-1")
    assert recovered is not None and recovered.is_dir()
    assert not previous.exists()

    previous.mkdir()
    (previous / "obsolete.txt").write_text("old", encoding="utf-8")
    cleaned = publish_curated_results(result, tmp_path / "private", "analysis-1")
    assert cleaned is not None and cleaned.is_dir()
    assert not previous.exists()


@pytest.mark.parametrize(
    ("index_payload", "error"),
    [
        ({"dataset_path": "", "files": []}, "relative_path_invalid"),
        (
            {
                "dataset_path": "report_assets/report_dataset.json",
                "dataset_sha256": "0" * 64,
                "files": {},
            },
            "allowlist_invalid",
        ),
    ],
)
def test_publication_rejects_invalid_index_contracts(
    tmp_path: Path, index_payload: dict[str, object], error: str
) -> None:
    result = tmp_path / error
    report = result / "report_assets"
    report.mkdir(parents=True)
    dataset = report / "report_dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    (result / "run_manifest.json").write_text("{}", encoding="utf-8")
    if index_payload.get("dataset_sha256") == "0" * 64:
        index_payload["dataset_sha256"] = hashlib.sha256(dataset.read_bytes()).hexdigest()
    (report / "index.json").write_text(json.dumps(index_payload), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        publish_curated_results(result, tmp_path / "private", "analysis-1")
