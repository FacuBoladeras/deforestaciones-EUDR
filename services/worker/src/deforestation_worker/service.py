"""Adquisición con lease y ejecución observable de jobs locales."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from deforestation_domain.runtime import resolve_runtime_root
from deforestation_jobs.models import AnalysisJob, JobStatus
from deforestation_jobs.repository import InvalidJobTransitionError, SQLiteJobRepository
from deforestation_pipeline.complete_analysis import (
    CompleteAnalysisError,
    CompleteAnalysisRequest,
    run_complete_analysis,
)
from deforestation_reporting import (
    ReportContractError,
    ReportIntegrityError,
    load_report_package,
    render_technical_appendix,
    render_technical_report,
)
from deforestation_worker.package import (
    create_evidence_package,
    publish_curated_results,
    retry_filesystem_operation,
)

PROJECT_ROOT = resolve_runtime_root(Path(__file__).resolve().parents[4])
LOGGER = logging.getLogger("deforestation_worker")
AnalysisRunner = Callable[..., Path]
ReportRenderer = Callable[[Path, Path, Path], Path]
_Result = TypeVar("_Result")
WORKER_RUNTIME_REQUIRED_FILES = (
    Path("configs/default.yml"),
    Path("configs/rf-forest-entrerios-2020-2024.yml"),
    Path("configs/hampel-benchmark.yml"),
    Path("configs/agricultural-evidence.yml"),
    Path("configs/agricultural-collector.yml"),
    Path("configs/agricultural-persistence.yml"),
    Path("data/catalog.yml"),
    Path("data/licenses.yml"),
    Path("data/models/rf_forest_multiyear_2020_2024_v1.json"),
    Path("data/schemas/post-change-attribution-v7.0.0.json"),
)
_FULL_GIT_REVISION = re.compile(r"[0-9a-f]{40}")


class _LeaseLostError(RuntimeError):
    """El intento dejó de ser dueño del job y debe abandonar sin publicar."""


@dataclass(frozen=True, slots=True)
class _StageFailure(Exception):
    stage: str
    safe_error_code: str
    retryable: bool
    internal_error_class: str | None = None


def render_client_report(result_root: Path, output_path: Path, input_path: Path) -> Path:
    """Proyecta el contrato curado a PDF fuera del núcleo científico."""
    package = load_report_package(result_root, input_path=input_path)
    rendered = render_technical_report(package, output_path).path
    render_technical_appendix(package, _client_appendix_output_path(output_path))
    return rendered


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    database_path: Path
    storage_root: Path
    output_root: Path
    project_root: Path
    credentials_path: Path | None
    model_artifact_path: Path | None = None
    code_revision: str = "working-tree"
    poll_interval_seconds: float = 2.0
    worker_id: str = "local-worker-1"
    lease_duration_seconds: float = 300.0
    heartbeat_interval_seconds: float = 30.0
    max_attempts: int = 3
    filesystem_retry_attempts: int = 5

    def __post_init__(self) -> None:
        if self.lease_duration_seconds <= 0:
            raise ValueError("worker_lease_duration_invalid")
        if not 0 < self.heartbeat_interval_seconds < self.lease_duration_seconds:
            raise ValueError("worker_heartbeat_interval_invalid")
        if self.max_attempts < 1 or self.filesystem_retry_attempts < 1:
            raise ValueError("worker_attempt_limit_invalid")

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        runtime_root = resolve_runtime_root(PROJECT_ROOT)
        private_root = Path(
            os.environ.get(
                "DEFORESTATION_API_PRIVATE_ROOT",
                PROJECT_ROOT / "outputs" / "api" / "private",
            )
        )
        credentials_value = os.environ.get("DEFORESTATION_GEE_CREDENTIALS")
        model_artifact_value = os.environ.get("DEFORESTATION_RF_MODEL_ARTIFACT")
        default_credentials = runtime_root / "credentials.json"
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
                    runtime_root / "outputs" / "runs",
                )
            ),
            project_root=runtime_root,
            credentials_path=credentials,
            model_artifact_path=(Path(model_artifact_value) if model_artifact_value else None),
            code_revision=os.environ.get("DEFORESTATION_CODE_REVISION", "working-tree"),
            poll_interval_seconds=float(os.environ.get("DEFORESTATION_WORKER_POLL_SECONDS", "2")),
            worker_id=os.environ.get("DEFORESTATION_WORKER_ID", "local-worker-1"),
            lease_duration_seconds=float(
                os.environ.get("DEFORESTATION_WORKER_LEASE_SECONDS", "300")
            ),
            heartbeat_interval_seconds=float(
                os.environ.get("DEFORESTATION_WORKER_HEARTBEAT_SECONDS", "30")
            ),
            max_attempts=int(os.environ.get("DEFORESTATION_WORKER_MAX_ATTEMPTS", "3")),
            filesystem_retry_attempts=int(
                os.environ.get("DEFORESTATION_WORKER_FILESYSTEM_RETRIES", "5")
            ),
        )


def check_worker_runtime(settings: WorkerSettings) -> bool:
    """Comprueba mounts y escritura local sin contactar GEE ni cargar el modelo."""
    runtime_root = settings.project_root.resolve()
    for relative in WORKER_RUNTIME_REQUIRED_FILES:
        candidate = (runtime_root / relative).resolve()
        if not candidate.is_relative_to(runtime_root) or not candidate.is_file():
            return False
    required_external_files = (settings.credentials_path, settings.model_artifact_path)
    if any(path is None or not path.is_file() for path in required_external_files):
        return False
    if _FULL_GIT_REVISION.fullmatch(settings.code_revision.strip().lower()) is None:
        return False
    try:
        writable_roots = (
            settings.database_path.parent,
            settings.storage_root,
            settings.output_root,
        )
        for directory in writable_roots:
            directory.mkdir(parents=True, exist_ok=True)
            if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
                return False
    except OSError:
        return False
    return True


class _LeaseGuard:
    """Renueva el lease durante llamadas bloqueantes y detecta fencing."""

    def __init__(
        self,
        repository: SQLiteJobRepository,
        job: AnalysisJob,
        settings: WorkerSettings,
        stage: str,
    ) -> None:
        if job.lease_owner_id is None:
            raise ValueError("claimed_job_without_lease_owner")
        self.repository = repository
        self.job = job
        self.settings = settings
        self.stage = stage
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name=f"worker-heartbeat-{job.analysis_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.settings.heartbeat_interval_seconds * 2)

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self.stage = stage
        self._heartbeat(stage)

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise _LeaseLostError("worker_attempt_fenced")
        with self._lock:
            stage = self.stage
        self._heartbeat(stage)

    def _run(self) -> None:
        while not self._stop.wait(self.settings.heartbeat_interval_seconds):
            with self._lock:
                stage = self.stage
            try:
                self._heartbeat(stage)
            except _LeaseLostError:
                self._lost.set()
                return

    def _heartbeat(self, stage: str) -> None:
        try:
            self.repository.heartbeat(
                self.job.analysis_id,
                lease_owner_id=self.job.lease_owner_id or "",
                attempt=self.job.attempt,
                stage=stage,
                lease_duration=timedelta(seconds=self.settings.lease_duration_seconds),
            )
        except InvalidJobTransitionError as error:
            self._lost.set()
            raise _LeaseLostError("worker_attempt_fenced") from error


class AnalysisWorker:
    """Consumidor local con ownership explícito y postproceso reanudable."""

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
        job = self.repository.claim_next(
            self.settings.worker_id,
            lease_duration=timedelta(seconds=self.settings.lease_duration_seconds),
            max_attempts=self.settings.max_attempts,
        )
        if job is None:
            return False
        return self.process_claimed(job)

    def process_claimed(self, job: AnalysisJob) -> bool:
        """Procesa sólo mientras conserva owner+attempt; nunca completa un intento viejo."""
        current = self.repository.get(job.analysis_id)
        if current is not None and current.status is JobStatus.CANCELLING:
            self._mark_cancelled(job)
            return True
        guard: _LeaseGuard | None = None
        try:
            resume = job.output_prefix is not None
            request = self._build_request(job)
            running_stage = job.stage if resume else "full_pipeline"
            try:
                self.repository.transition(
                    job.analysis_id,
                    expected=JobStatus.VALIDATING,
                    target=JobStatus.RUNNING,
                    stage=running_stage,
                    lease_owner_id=job.lease_owner_id or "",
                    attempt=job.attempt,
                )
            except InvalidJobTransitionError:
                raced = self.repository.get(job.analysis_id)
                if raced is not None and raced.status is JobStatus.CANCELLING:
                    self._mark_cancelled(job)
                    return True
                raise _LeaseLostError("worker_attempt_fenced") from None
            guard = _LeaseGuard(self.repository, job, self.settings, running_stage)
            guard.start()
            if resume:
                result, public_status = self._load_recorded_science(job)
            else:
                result, public_status = self._run_science(job, request, guard)
                job = self._record_science(job, result, public_status)
            self._check_cancellation(job, guard)
            self._postprocess(job, result, public_status, guard)
        except _StageFailure as failure:
            if guard is not None:
                guard.stop()
            self._record_stage_failure(job, failure)
        except _LeaseLostError:
            if guard is not None:
                guard.stop()
            self._log("worker_attempt_fenced", job, stage=job.stage)
        except Exception:
            if guard is not None:
                guard.stop()
            self._record_pipeline_failure(job)
        else:
            if guard is not None:
                guard.stop()
        return True

    def run_forever(self) -> None:
        self.initialize()
        while True:
            if not self.run_once():
                time.sleep(self.settings.poll_interval_seconds)

    def _run_science(
        self,
        job: AnalysisJob,
        request: CompleteAnalysisRequest,
        guard: _LeaseGuard,
    ) -> tuple[Path, JobStatus]:
        try:
            result = self._run_stage(
                job,
                guard,
                "full_pipeline",
                lambda: self.runner(request, analysis_id=UUID(job.analysis_id)),
            )
        except CompleteAnalysisError as error:
            result = error.failure_path
        try:
            return self._validated_scientific_result(result)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise _StageFailure("full_pipeline", "pipeline_execution_failed", False) from error

    def _record_science(
        self,
        job: AnalysisJob,
        result: Path,
        public_status: JobStatus,
    ) -> AnalysisJob:
        output_root = self.settings.output_root.resolve()
        manifest_path = result / "run_manifest.json"
        try:
            return self.repository.record_scientific_result(
                job.analysis_id,
                output_prefix=result.relative_to(output_root).as_posix(),
                parent_manifest_sha256=_sha256(manifest_path),
                scientific_status=public_status,
                lease_owner_id=job.lease_owner_id or "",
                attempt=job.attempt,
            )
        except InvalidJobTransitionError as error:
            raise _LeaseLostError("worker_attempt_fenced") from error

    def _load_recorded_science(self, job: AnalysisJob) -> tuple[Path, JobStatus]:
        if (
            job.output_prefix is None
            or job.parent_manifest_sha256 is None
            or job.scientific_status not in {JobStatus.COMPLETED, JobStatus.PARTIAL}
        ):
            raise _StageFailure(job.stage, "report_contract_invalid", False)
        result = _safe_path(self.settings.output_root, job.output_prefix)
        try:
            validated, status = self._validated_scientific_result(result)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise _StageFailure(job.stage, "report_contract_invalid", False) from error
        if _sha256(validated / "run_manifest.json") != job.parent_manifest_sha256:
            raise _StageFailure(job.stage, "report_contract_invalid", False)
        return validated, status

    def _validated_scientific_result(self, result: Path) -> tuple[Path, JobStatus]:
        resolved_result = result.resolve()
        output_root = self.settings.output_root.resolve()
        if not resolved_result.is_relative_to(output_root):
            raise ValueError("analysis_output_outside_root")
        manifest = _load_manifest(resolved_result / "run_manifest.json")
        status_value = manifest.get("overall_status")
        if status_value not in {"complete", "partial"}:
            raise ValueError("analysis_manifest_status_invalid")
        status = JobStatus.COMPLETED if status_value == "complete" else JobStatus.PARTIAL
        return resolved_result, status

    def _postprocess(
        self,
        job: AnalysisJob,
        result: Path,
        public_status: JobStatus,
        guard: _LeaseGuard,
    ) -> None:
        client_report = self._render_report(job, result, guard)
        client_appendix = _client_appendix_path(
            self.settings.storage_root, job.analysis_id, job.attempt
        )
        published_appendix = client_appendix if _valid_pdf(client_appendix) else None
        self._check_cancellation(job, guard)
        published = self._publish(job, result, client_report, published_appendix, guard)
        self._check_cancellation(job, guard)
        self._package(job, result, client_report, published_appendix, guard)
        self._check_cancellation(job, guard)
        report_dataset = published / "report_assets" / "report_dataset.json"
        guard.ensure_owned()
        try:
            self.repository.complete(
                job.analysis_id,
                status=public_status,
                output_prefix=job.output_prefix or result.name,
                parent_manifest_sha256=job.parent_manifest_sha256
                or _sha256(result / "run_manifest.json"),
                report_dataset_sha256=(
                    _sha256(report_dataset) if report_dataset.is_file() else None
                ),
                lease_owner_id=job.lease_owner_id or "",
                attempt=job.attempt,
            )
        except InvalidJobTransitionError as error:
            raise _LeaseLostError("worker_attempt_fenced") from error

    def _render_report(self, job: AnalysisJob, result: Path, guard: _LeaseGuard) -> Path:
        durable = _client_report_path(self.settings.storage_root, job.analysis_id, job.attempt)

        def render() -> Path:
            if _valid_pdf(durable):
                appendix_durable = _client_appendix_path(
                    self.settings.storage_root, job.analysis_id, job.attempt
                )
                if self.report_renderer is not render_client_report or _valid_pdf(appendix_durable):
                    return durable
                retry_filesystem_operation(
                    lambda: durable.unlink(),
                    attempts=self.settings.filesystem_retry_attempts,
                )
            attempt_path = durable.with_name(".client-report.tmp.pdf")
            attempt_path.parent.mkdir(parents=True, exist_ok=True)
            if attempt_path.exists():
                retry_filesystem_operation(
                    lambda: attempt_path.unlink(),
                    attempts=self.settings.filesystem_retry_attempts,
                )
            input_path = _safe_path(self.settings.storage_root, job.input_object_key)
            rendered = retry_filesystem_operation(
                lambda: self.report_renderer(result, attempt_path, input_path),
                attempts=self.settings.filesystem_retry_attempts,
            ).resolve()
            if rendered != attempt_path.resolve() or not _valid_pdf(rendered):
                raise RuntimeError("client_report_output_invalid")
            appendix_attempt = _client_appendix_output_path(attempt_path)
            guard.ensure_owned()
            if _valid_pdf(appendix_attempt):
                appendix_durable = _client_appendix_path(
                    self.settings.storage_root, job.analysis_id, job.attempt
                )
                retry_filesystem_operation(
                    lambda: appendix_attempt.replace(appendix_durable),
                    attempts=self.settings.filesystem_retry_attempts,
                )
            retry_filesystem_operation(
                lambda: rendered.replace(durable),
                attempts=self.settings.filesystem_retry_attempts,
            )
            return durable

        try:
            return self._run_stage(job, guard, "rendering_pdf", render)
        except ReportContractError as error:
            raise _StageFailure(
                "rendering_pdf",
                "report_contract_invalid",
                False,
                type(error).__name__,
            ) from error
        except ReportIntegrityError as error:
            retryable = _caused_by_os_error(error)
            raise _StageFailure(
                "rendering_pdf",
                "report_integrity_io_failed" if retryable else "report_contract_invalid",
                retryable,
                type(error).__name__,
            ) from error
        except ValueError as error:
            raise _StageFailure(
                "rendering_pdf", "report_contract_invalid", False, type(error).__name__
            ) from error
        except _LeaseLostError:
            raise
        except Exception as error:
            raise _StageFailure(
                "rendering_pdf", "pdf_render_failed", True, type(error).__name__
            ) from error

    def _publish(
        self,
        job: AnalysisJob,
        result: Path,
        client_report: Path,
        client_appendix: Path | None,
        guard: _LeaseGuard,
    ) -> Path:
        try:
            published = self._run_stage(
                job,
                guard,
                "publishing_results",
                lambda: publish_curated_results(
                    result,
                    self.settings.storage_root,
                    job.analysis_id,
                    client_report=client_report,
                    client_appendix=client_appendix,
                    attempt=job.attempt,
                    before_commit=guard.ensure_owned,
                ),
            )
        except ValueError as error:
            raise _StageFailure("publishing_results", "report_contract_invalid", False) from error
        except _LeaseLostError:
            raise
        except OSError as error:
            raise _StageFailure("publishing_results", "results_publish_failed", True) from error
        if published is None:
            raise _StageFailure("publishing_results", "report_contract_invalid", False)
        return published

    def _package(
        self,
        job: AnalysisJob,
        result: Path,
        client_report: Path,
        client_appendix: Path | None,
        guard: _LeaseGuard,
    ) -> Path:
        try:
            package = self._run_stage(
                job,
                guard,
                "packaging",
                lambda: create_evidence_package(
                    result,
                    self.settings.storage_root,
                    job.analysis_id,
                    client_report=client_report,
                    client_appendix=client_appendix,
                    attempt=job.attempt,
                    before_commit=guard.ensure_owned,
                ),
            )
        except ValueError as error:
            raise _StageFailure("packaging", "report_contract_invalid", False) from error
        except _LeaseLostError:
            raise
        except OSError as error:
            raise _StageFailure("packaging", "evidence_package_failed", True) from error
        if package is None:
            raise _StageFailure("packaging", "report_contract_invalid", False)
        return package

    def _run_stage(
        self,
        job: AnalysisJob,
        guard: _LeaseGuard,
        stage: str,
        operation: Callable[[], _Result],
    ) -> _Result:
        guard.set_stage(stage)
        started = time.monotonic()
        self._log("worker_stage_started", job, stage=stage)
        try:
            result = operation()
            guard.ensure_owned()
        except Exception as error:
            self._log(
                "worker_stage_aborted",
                job,
                stage=stage,
                duration_seconds=time.monotonic() - started,
                error_class=type(error).__name__,
            )
            raise
        self._log(
            "worker_stage_completed",
            job,
            stage=stage,
            duration_seconds=time.monotonic() - started,
        )
        return result

    def _check_cancellation(self, job: AnalysisJob, guard: _LeaseGuard) -> None:
        guard.ensure_owned()
        current = self.repository.get(job.analysis_id)
        if current is None:
            raise _LeaseLostError("worker_attempt_fenced")
        if current.status is JobStatus.CANCELLING:
            guard.stop()
            self._mark_cancelled(job)
            raise _LeaseLostError("worker_attempt_cancelled")

    def _mark_cancelled(self, job: AnalysisJob) -> None:
        try:
            self.repository.mark_cancelled(
                job.analysis_id,
                lease_owner_id=job.lease_owner_id or "",
                attempt=job.attempt,
            )
        except InvalidJobTransitionError:
            self._log("worker_attempt_fenced", job, stage="cancelled")

    def _record_pipeline_failure(self, job: AnalysisJob) -> None:
        self._log(
            "worker_stage_failed",
            job,
            stage="full_pipeline",
            safe_error_code="pipeline_execution_failed",
        )
        try:
            self.repository.fail(
                job.analysis_id,
                safe_error_code="pipeline_execution_failed",
                lease_owner_id=job.lease_owner_id or "",
                attempt=job.attempt,
            )
        except InvalidJobTransitionError:
            self._log("worker_attempt_fenced", job, stage="full_pipeline")

    def _record_stage_failure(self, job: AnalysisJob, failure: _StageFailure) -> None:
        self._log(
            "worker_stage_failed",
            job,
            stage=failure.stage,
            safe_error_code=failure.safe_error_code,
            error_class=failure.internal_error_class,
        )
        try:
            if failure.retryable:
                self.repository.retry_postprocessing(
                    job.analysis_id,
                    safe_error_code=failure.safe_error_code,
                    failed_stage=failure.stage,
                    lease_owner_id=job.lease_owner_id or "",
                    attempt=job.attempt,
                    max_attempts=self.settings.max_attempts,
                )
            else:
                self.repository.fail(
                    job.analysis_id,
                    safe_error_code=failure.safe_error_code,
                    lease_owner_id=job.lease_owner_id or "",
                    attempt=job.attempt,
                )
        except InvalidJobTransitionError:
            self._log("worker_attempt_fenced", job, stage=failure.stage)

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
            runtime_root=project,
            model_artifact_path=self.settings.model_artifact_path,
            declared_land_use=cast(
                Literal["unknown", "managed_forest_plantation"], job.declared_land_use
            ),
            declared_context_source=job.declared_context_source,
        )

    def _log(
        self,
        event: str,
        job: AnalysisJob,
        *,
        stage: str,
        duration_seconds: float = 0.0,
        safe_error_code: str | None = None,
        error_class: str | None = None,
    ) -> None:
        LOGGER.info(
            event,
            extra={
                "analysis_id": job.analysis_id,
                "attempt": job.attempt,
                "stage": stage,
                "duration_seconds": round(duration_seconds, 6),
                "safe_error_code": safe_error_code,
                "error_class": error_class,
            },
        )


def _safe_path(root_value: Path, object_key: str) -> Path:
    root = root_value.resolve()
    relative = Path(object_key)
    candidate = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not candidate.is_relative_to(root):
        raise ValueError("analysis_object_key_unsafe")
    return candidate


def _client_report_path(storage_root: Path, analysis_id: str, attempt: int) -> Path:
    UUID(analysis_id)
    root = storage_root.resolve()
    analysis_root = (root / "analyses" / analysis_id).resolve()
    if not analysis_root.is_relative_to(root):  # pragma: no cover - UUID lo impide
        raise ValueError("analysis_id_unsafe")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("worker_attempt_invalid")
    return analysis_root / "postprocess" / f"attempt-{attempt}" / "client-report.pdf"


def _client_appendix_path(storage_root: Path, analysis_id: str, attempt: int) -> Path:
    return _client_report_path(storage_root, analysis_id, attempt).with_name(
        "technical-appendix.pdf"
    )


def _client_appendix_output_path(report_path: Path) -> Path:
    name = (
        ".technical-appendix.tmp.pdf"
        if report_path.name == ".client-report.tmp.pdf"
        else "technical-appendix.pdf"
    )
    return report_path.with_name(name)


def _valid_pdf(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 5 and path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def _caused_by_os_error(error: BaseException) -> bool:
    """Detecta I/O transitorio envuelto sin inspeccionar ni publicar su mensaje."""
    cause = error.__cause__
    while cause is not None:
        if isinstance(cause, OSError):
            return True
        cause = cause.__cause__
    return False


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
