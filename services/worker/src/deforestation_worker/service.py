"""Adquisición condicional y ejecución secuencial de jobs locales."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from deforestation_jobs.models import AnalysisJob, JobStatus
from deforestation_jobs.repository import InvalidJobTransitionError, SQLiteJobRepository
from deforestation_pipeline.complete_analysis import (
    CompleteAnalysisError,
    CompleteAnalysisRequest,
    run_complete_analysis,
)
from deforestation_reporting import load_report_package, render_technical_report
from deforestation_worker.package import create_evidence_package, publish_curated_results

PROJECT_ROOT = Path(__file__).resolve().parents[4]
AnalysisRunner = Callable[..., Path]
ReportRenderer = Callable[[Path, Path, Path], Path]


def render_client_report(result_root: Path, output_path: Path, input_path: Path) -> Path:
    """Proyecta el contrato curado a PDF fuera del núcleo científico."""
    package = load_report_package(result_root, input_path=input_path)
    return render_technical_report(package, output_path).path


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    database_path: Path
    storage_root: Path
    output_root: Path
    project_root: Path
    credentials_path: Path | None
    poll_interval_seconds: float = 2.0
    worker_id: str = "local-worker-1"

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        private_root = Path(
            os.environ.get(
                "DEFORESTATION_API_PRIVATE_ROOT",
                PROJECT_ROOT / "outputs" / "api" / "private",
            )
        )
        credentials_value = os.environ.get("DEFORESTATION_GEE_CREDENTIALS")
        default_credentials = PROJECT_ROOT / "credentials.json"
        credentials = (
            Path(credentials_value)
            if credentials_value
            else default_credentials
            if default_credentials.is_file()
            else None
        )
        return cls(
            database_path=Path(
                os.environ.get("DEFORESTATION_API_DATABASE", private_root / "jobs.sqlite3")
            ),
            storage_root=Path(
                os.environ.get("DEFORESTATION_API_STORAGE", private_root / "objects")
            ),
            output_root=Path(
                os.environ.get(
                    "DEFORESTATION_ANALYSIS_OUTPUT_ROOT",
                    PROJECT_ROOT / "outputs" / "runs",
                )
            ),
            project_root=PROJECT_ROOT,
            credentials_path=credentials,
            poll_interval_seconds=float(os.environ.get("DEFORESTATION_WORKER_POLL_SECONDS", "2")),
            worker_id=os.environ.get("DEFORESTATION_WORKER_ID", "local-worker-1"),
        )


class AnalysisWorker:
    """Un consumidor; la concurrencia nace agregando procesos deliberadamente."""

    def __init__(
        self,
        settings: WorkerSettings,
        *,
        repository: SQLiteJobRepository | None = None,
        runner: AnalysisRunner = run_complete_analysis,
        report_renderer: ReportRenderer = render_client_report,
    ) -> None:
        self.settings = settings
        self.repository = repository or SQLiteJobRepository(settings.database_path)
        self.runner = runner
        self.report_renderer = report_renderer

    def initialize(self) -> int:
        self.repository.initialize()
        return self.recover_interrupted_jobs()

    def recover_interrupted_jobs(self) -> int:
        return self.repository.recover_active_jobs()

    def run_once(self) -> bool:
        job = self.repository.claim_next(self.settings.worker_id)
        if job is None:
            return False
        return self.process_claimed(job)

    def process_claimed(self, job: AnalysisJob) -> bool:
        """Procesa una adquisición existente y respeta cancelaciones pre-ejecución."""
        current = self.repository.get(job.analysis_id)
        if current is not None and current.status is JobStatus.CANCELLING:
            self.repository.mark_cancelled(job.analysis_id)
            return True
        try:
            request = self._build_request(job)
            try:
                self.repository.transition(
                    job.analysis_id,
                    expected=JobStatus.VALIDATING,
                    target=JobStatus.RUNNING,
                    stage="full_pipeline",
                )
            except InvalidJobTransitionError:
                raced = self.repository.get(job.analysis_id)
                if raced is not None and raced.status is JobStatus.CANCELLING:
                    self.repository.mark_cancelled(job.analysis_id)
                    return True
                raise
            result = self.runner(request, analysis_id=UUID(job.analysis_id))
            self._publish_result(job, result)
        except CompleteAnalysisError as error:
            self._handle_complete_analysis_error(job, error)
        except Exception:
            self.repository.fail(job.analysis_id, safe_error_code="analysis_execution_failed")
        return True

    def run_forever(self) -> None:
        self.initialize()
        while True:
            if not self.run_once():
                time.sleep(self.settings.poll_interval_seconds)

    def _build_request(self, job: AnalysisJob) -> CompleteAnalysisRequest:
        input_path = _safe_path(self.settings.storage_root, job.input_object_key)
        if not input_path.is_file():
            raise FileNotFoundError("analysis_input_missing")
        if _sha256(input_path) != job.input_sha256:
            raise ValueError("analysis_input_sha256_mismatch")
        project = self.settings.project_root.resolve()
        return CompleteAnalysisRequest(
            input_path=input_path,
            output_root=self.settings.output_root.resolve(),
            config_path=project / "configs" / "default.yml",
            forest_model_config_path=project / "configs" / "rf-forest-entrerios-2020-2024.yml",
            hampel_config_path=project / "configs" / "hampel-benchmark.yml",
            establishment_id=job.establishment_id,
            agricultural_evidence_policy_path=project / "configs" / "agricultural-evidence.yml",
            agricultural_collector_config_path=project / "configs" / "agricultural-collector.yml",
            agricultural_persistence_config_path=project
            / "configs"
            / "agricultural-persistence.yml",
            catalog_path=project / "data" / "catalog.yml",
            licenses_path=project / "data" / "licenses.yml",
            credentials_path=self.settings.credentials_path,
            declared_land_use=job.declared_land_use,  # type: ignore[arg-type]
            declared_context_source=job.declared_context_source,
        )

    def _publish_result(self, job: AnalysisJob, result: Path) -> None:
        analysis_id = job.analysis_id
        resolved_result = result.resolve()
        output_root = self.settings.output_root.resolve()
        if not resolved_result.is_relative_to(output_root):
            raise ValueError("analysis_output_outside_root")
        manifest_path = resolved_result / "run_manifest.json"
        manifest = _load_manifest(manifest_path)
        status_value = manifest.get("overall_status")
        if status_value not in {"complete", "partial"}:
            raise ValueError("analysis_manifest_status_invalid")
        public_status = JobStatus.COMPLETED if status_value == "complete" else JobStatus.PARTIAL
        report_staging = _client_report_staging_path(self.settings.storage_root, analysis_id)
        report_staging.parent.mkdir(parents=True, exist_ok=True)
        report_staging.unlink(missing_ok=True)
        input_path = _safe_path(self.settings.storage_root, job.input_object_key)
        try:
            client_report = self.report_renderer(
                resolved_result,
                report_staging,
                input_path,
            ).resolve()
            if client_report != report_staging.resolve() or not client_report.is_file():
                raise ValueError("client_report_output_invalid")
            published = publish_curated_results(
                resolved_result,
                self.settings.storage_root,
                analysis_id,
                client_report=client_report,
            )
            package = create_evidence_package(
                resolved_result,
                self.settings.storage_root,
                analysis_id,
                client_report=client_report,
            )
        finally:
            report_staging.unlink(missing_ok=True)
        if published is None or package is None:
            raise ValueError("analysis_report_assets_missing")
        report_dataset = published / "report_assets" / "report_dataset.json"
        self.repository.complete(
            analysis_id,
            status=public_status,
            output_prefix=resolved_result.relative_to(output_root).as_posix(),
            parent_manifest_sha256=_sha256(manifest_path),
            report_dataset_sha256=_sha256(report_dataset) if report_dataset.is_file() else None,
        )

    def _handle_complete_analysis_error(
        self, job: AnalysisJob, error: CompleteAnalysisError
    ) -> None:
        analysis_id = job.analysis_id
        try:
            manifest = _load_manifest(error.failure_path / "run_manifest.json")
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            self.repository.fail(analysis_id, safe_error_code="analysis_execution_failed")
            return
        if manifest.get("overall_status") == "partial":
            try:
                self._publish_result(job, error.failure_path)
            except Exception:
                self.repository.fail(analysis_id, safe_error_code="analysis_execution_failed")
        else:
            self.repository.fail(analysis_id, safe_error_code="analysis_execution_failed")


def _safe_path(storage_root: Path, object_key: str) -> Path:
    root = storage_root.resolve()
    relative = Path(object_key)
    candidate = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not candidate.is_relative_to(root):
        raise ValueError("analysis_input_object_key_unsafe")
    return candidate


def _client_report_staging_path(storage_root: Path, analysis_id: str) -> Path:
    UUID(analysis_id)
    root = storage_root.resolve()
    analysis_root = (root / "analyses" / analysis_id).resolve()
    if not analysis_root.is_relative_to(root):  # pragma: no cover - UUID lo impide
        raise ValueError("analysis_id_unsafe")
    return analysis_root / ".client-report.tmp.pdf"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("analysis_manifest_not_object")
    return payload
