"""Repositorio SQLite con leases, fencing y transiciones condicionales."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deforestation_jobs.models import AnalysisJob, JobStatus

DEFAULT_LEASE_DURATION = timedelta(minutes=5)
DEFAULT_MAX_ATTEMPTS = 3


class IdempotencyConflictError(RuntimeError):
    """La misma clave fue utilizada para una solicitud diferente."""


class InvalidJobTransitionError(RuntimeError):
    """La transición perdió una carrera, lease o intento."""


_ALLOWED_TRANSITIONS = frozenset(
    {
        (JobStatus.QUEUED, JobStatus.VALIDATING),
        (JobStatus.VALIDATING, JobStatus.RUNNING),
        (JobStatus.VALIDATING, JobStatus.FAILED),
        (JobStatus.RUNNING, JobStatus.COMPLETED),
        (JobStatus.RUNNING, JobStatus.PARTIAL),
        (JobStatus.RUNNING, JobStatus.FAILED),
        (JobStatus.RUNNING, JobStatus.CANCELLING),
        (JobStatus.CANCELLING, JobStatus.CANCELLED),
        (JobStatus.CANCELLING, JobStatus.COMPLETED),
        (JobStatus.CANCELLING, JobStatus.PARTIAL),
    }
)
_ACTIVE_STATUSES = (JobStatus.VALIDATING.value, JobStatus.RUNNING.value)
_OWNED_STATUSES = (*_ACTIVE_STATUSES, JobStatus.CANCELLING.value)


class SQLiteJobRepository:
    """Persistencia local durable; cada operación usa una conexión breve."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def initialize(self) -> None:
        """Crea el esquema y agrega columnas nuevas sin reconstruir tablas existentes."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    analysis_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    establishment_id TEXT NOT NULL,
                    input_object_key TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    configuration_versions TEXT NOT NULL,
                    code_revision TEXT NOT NULL,
                    worker_task_id TEXT,
                    output_prefix TEXT,
                    parent_manifest_sha256 TEXT,
                    report_dataset_sha256 TEXT,
                    safe_error_code TEXT,
                    expires_at TEXT NOT NULL,
                    declared_land_use TEXT NOT NULL,
                    declared_context_source TEXT NOT NULL,
                    lease_owner_id TEXT,
                    lease_expires_at TEXT,
                    scientific_status TEXT,
                    UNIQUE (owner_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_jobs_queue
                    ON analysis_jobs(status, created_at);
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(analysis_jobs)")
            }
            for name, data_type in (
                ("lease_owner_id", "TEXT"),
                ("lease_expires_at", "TEXT"),
                ("scientific_status", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE analysis_jobs ADD COLUMN {name} {data_type}")
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_analysis_jobs_lease
                   ON analysis_jobs(status, lease_expires_at)"""
            )

    def create_or_get(self, job: AnalysisJob) -> tuple[AnalysisJob, bool]:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM analysis_jobs WHERE owner_id = ? AND idempotency_key = ?",
                (job.owner_id, job.idempotency_key),
            ).fetchone()
            if existing is not None:
                current = _job_from_row(existing)
                if current.request_sha256 != job.request_sha256:
                    raise IdempotencyConflictError("idempotency_key_payload_mismatch")
                return current, False
            connection.execute(
                """
                INSERT INTO analysis_jobs (
                    analysis_id, owner_id, idempotency_key, request_sha256,
                    establishment_id, input_object_key, input_sha256, status,
                    stage, attempt, created_at, started_at, updated_at, completed_at,
                    configuration_versions, code_revision, worker_task_id,
                    output_prefix, parent_manifest_sha256, report_dataset_sha256,
                    safe_error_code, expires_at, declared_land_use,
                    declared_context_source, lease_owner_id, lease_expires_at,
                    scientific_status
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                _job_values(job),
            )
            return job, True

    def get(self, analysis_id: str) -> AnalysisJob | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> AnalysisJob | None:
        current_time = now or datetime.now(UTC)
        _validate_lease_options(lease_duration, max_attempts)
        timestamp = current_time.isoformat()
        lease_expires_at = (current_time + lease_duration).isoformat()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, completed_at = ?, updated_at = ?,
                    safe_error_code = ?, lease_owner_id = NULL, lease_expires_at = NULL,
                    worker_task_id = NULL
                WHERE status = ? AND attempt >= ? AND expires_at > ?
                """,
                (
                    JobStatus.FAILED.value,
                    JobStatus.FAILED.value,
                    timestamp,
                    timestamp,
                    "worker_attempts_exhausted",
                    JobStatus.QUEUED.value,
                    max_attempts,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT analysis_id, output_prefix, stage FROM analysis_jobs
                WHERE status = ? AND attempt < ? AND expires_at > ?
                ORDER BY created_at, analysis_id LIMIT 1
                """,
                (JobStatus.QUEUED.value, max_attempts, timestamp),
            ).fetchone()
            if row is None:
                return None
            analysis_id = str(row["analysis_id"])
            resume_stage = str(row["stage"]) if row["output_prefix"] else "validating"
            changed = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, attempt = attempt + 1,
                    worker_task_id = ?, lease_owner_id = ?, lease_expires_at = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?, safe_error_code = NULL
                WHERE analysis_id = ? AND status = ? AND attempt < ?
                """,
                (
                    JobStatus.VALIDATING.value,
                    resume_stage,
                    worker_id,
                    worker_id,
                    lease_expires_at,
                    timestamp,
                    timestamp,
                    analysis_id,
                    JobStatus.QUEUED.value,
                    max_attempts,
                ),
            ).rowcount
            if changed != 1:
                return None
            return self._select_required(connection, analysis_id, "claimed_job_disappeared")

    def heartbeat(
        self,
        analysis_id: str,
        *,
        lease_owner_id: str,
        attempt: int,
        stage: str,
        now: datetime | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> AnalysisJob:
        current_time = now or datetime.now(UTC)
        _validate_lease_options(lease_duration, max(attempt, 1))
        timestamp = current_time.isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                f"""
                UPDATE analysis_jobs SET stage = ?, updated_at = ?, lease_expires_at = ?
                WHERE analysis_id = ? AND status IN ({_placeholders(_OWNED_STATUSES)})
                  AND lease_owner_id = ? AND attempt = ? AND lease_expires_at > ?
                """,
                (
                    stage,
                    timestamp,
                    (current_time + lease_duration).isoformat(),
                    analysis_id,
                    *_OWNED_STATUSES,
                    lease_owner_id,
                    attempt,
                    timestamp,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("lease_heartbeat_rejected")
            return self._select_required(connection, analysis_id, "heartbeat_job_disappeared")

    def transition(
        self,
        analysis_id: str,
        *,
        expected: JobStatus,
        target: JobStatus,
        stage: str,
        lease_owner_id: str,
        attempt: int,
        now: datetime | None = None,
    ) -> AnalysisJob:
        if (expected, target) not in _ALLOWED_TRANSITIONS:
            raise InvalidJobTransitionError(f"transition_not_allowed:{expected}:{target}")
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE analysis_jobs SET status = ?, stage = ?, updated_at = ?
                WHERE analysis_id = ? AND status = ? AND lease_owner_id = ?
                  AND attempt = ? AND lease_expires_at > ?
                """,
                (
                    target.value,
                    stage,
                    timestamp,
                    analysis_id,
                    expected.value,
                    lease_owner_id,
                    attempt,
                    timestamp,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("conditional_transition_failed")
            return self._select_required(connection, analysis_id, "transitioned_job_disappeared")

    def record_scientific_result(
        self,
        analysis_id: str,
        *,
        output_prefix: str,
        parent_manifest_sha256: str,
        scientific_status: JobStatus,
        lease_owner_id: str,
        attempt: int,
        now: datetime | None = None,
    ) -> AnalysisJob:
        if scientific_status not in {JobStatus.COMPLETED, JobStatus.PARTIAL}:
            raise InvalidJobTransitionError("scientific_status_invalid")
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE analysis_jobs
                SET output_prefix = ?, parent_manifest_sha256 = ?, scientific_status = ?,
                    stage = ?, updated_at = ?
                WHERE analysis_id = ? AND status IN (?, ?) AND lease_owner_id = ?
                  AND attempt = ? AND lease_expires_at > ?
                """,
                (
                    output_prefix,
                    parent_manifest_sha256,
                    scientific_status.value,
                    "rendering_pdf",
                    timestamp,
                    analysis_id,
                    JobStatus.RUNNING.value,
                    JobStatus.CANCELLING.value,
                    lease_owner_id,
                    attempt,
                    timestamp,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("scientific_result_record_rejected")
            return self._select_required(connection, analysis_id, "scientific_job_disappeared")

    def complete(
        self,
        analysis_id: str,
        *,
        status: JobStatus,
        output_prefix: str,
        parent_manifest_sha256: str,
        report_dataset_sha256: str | None,
        lease_owner_id: str,
        attempt: int,
        now: datetime | None = None,
    ) -> AnalysisJob:
        if status not in {JobStatus.COMPLETED, JobStatus.PARTIAL}:
            raise InvalidJobTransitionError("completion_status_invalid")
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?,
                    output_prefix = ?, parent_manifest_sha256 = ?, report_dataset_sha256 = ?,
                    safe_error_code = NULL, lease_owner_id = NULL, lease_expires_at = NULL
                WHERE analysis_id = ? AND status IN (?, ?) AND lease_owner_id = ?
                  AND attempt = ? AND lease_expires_at > ?
                """,
                (
                    status.value,
                    status.value,
                    timestamp,
                    timestamp,
                    output_prefix,
                    parent_manifest_sha256,
                    report_dataset_sha256,
                    analysis_id,
                    JobStatus.RUNNING.value,
                    JobStatus.CANCELLING.value,
                    lease_owner_id,
                    attempt,
                    timestamp,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("conditional_completion_failed")
            return self._select_required(connection, analysis_id, "completed_job_disappeared")

    def fail(
        self,
        analysis_id: str,
        *,
        safe_error_code: str,
        lease_owner_id: str,
        attempt: int,
        now: datetime | None = None,
    ) -> AnalysisJob:
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                f"""
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?,
                    safe_error_code = ?, lease_owner_id = NULL, lease_expires_at = NULL
                WHERE analysis_id = ? AND status IN ({_placeholders(_OWNED_STATUSES)})
                  AND lease_owner_id = ? AND attempt = ? AND lease_expires_at > ?
                """,
                (
                    JobStatus.FAILED.value,
                    JobStatus.FAILED.value,
                    timestamp,
                    timestamp,
                    safe_error_code,
                    analysis_id,
                    *_OWNED_STATUSES,
                    lease_owner_id,
                    attempt,
                    timestamp,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("conditional_failure_failed")
            return self._select_required(connection, analysis_id, "failed_job_disappeared")

    def retry_postprocessing(
        self,
        analysis_id: str,
        *,
        safe_error_code: str,
        failed_stage: str,
        lease_owner_id: str,
        attempt: int,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: datetime | None = None,
    ) -> AnalysisJob:
        if max_attempts < 1:
            raise ValueError("max_attempts_invalid")
        timestamp = (now or datetime.now(UTC)).isoformat()
        exhausted = attempt >= max_attempts
        target = JobStatus.FAILED if exhausted else JobStatus.QUEUED
        stage = JobStatus.FAILED.value if exhausted else failed_stage
        with self._transaction() as connection:
            changed = connection.execute(
                f"""
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?,
                    safe_error_code = ?, lease_owner_id = NULL, lease_expires_at = NULL,
                    worker_task_id = NULL
                WHERE analysis_id = ? AND status IN ({_placeholders(_OWNED_STATUSES)})
                  AND lease_owner_id = ? AND attempt = ? AND lease_expires_at > ?
                  AND output_prefix IS NOT NULL
                """,
                (
                    target.value,
                    stage,
                    timestamp,
                    timestamp if exhausted else None,
                    safe_error_code,
                    analysis_id,
                    *_OWNED_STATUSES,
                    lease_owner_id,
                    attempt,
                    timestamp,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("postprocess_retry_rejected")
            return self._select_required(connection, analysis_id, "retried_job_disappeared")

    def request_cancellation(self, analysis_id: str) -> AnalysisJob | None:
        """Solicita cancelación sin prometer detener trabajo remoto ya enviado."""
        timestamp = datetime.now(UTC).isoformat()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            if row is None:
                return None
            current = _job_from_row(row)
            if current.status is JobStatus.QUEUED:
                connection.execute(
                    """
                    UPDATE analysis_jobs SET status = ?, stage = ?, updated_at = ?,
                        completed_at = ? WHERE analysis_id = ? AND status = ?
                    """,
                    (
                        JobStatus.CANCELLED.value,
                        JobStatus.CANCELLED.value,
                        timestamp,
                        timestamp,
                        analysis_id,
                        JobStatus.QUEUED.value,
                    ),
                )
            elif current.status in {JobStatus.VALIDATING, JobStatus.RUNNING}:
                connection.execute(
                    """
                    UPDATE analysis_jobs SET status = ?, stage = ?, updated_at = ?
                    WHERE analysis_id = ? AND status = ?
                    """,
                    (
                        JobStatus.CANCELLING.value,
                        JobStatus.CANCELLING.value,
                        timestamp,
                        analysis_id,
                        current.status.value,
                    ),
                )
            return self._select_required(connection, analysis_id, "cancelled_job_disappeared")

    def mark_cancelled(
        self,
        analysis_id: str,
        *,
        lease_owner_id: str,
        attempt: int,
        now: datetime | None = None,
    ) -> AnalysisJob:
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?,
                    lease_owner_id = NULL, lease_expires_at = NULL
                WHERE analysis_id = ? AND status = ? AND lease_owner_id = ?
                  AND attempt = ? AND lease_expires_at > ?
                """,
                (
                    JobStatus.CANCELLED.value,
                    JobStatus.CANCELLED.value,
                    timestamp,
                    timestamp,
                    analysis_id,
                    JobStatus.CANCELLING.value,
                    lease_owner_id,
                    attempt,
                    timestamp,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("conditional_cancellation_failed")
            return self._select_required(connection, analysis_id, "cancelled_job_disappeared")

    def expired_analysis_ids(self, *, now: datetime | None = None) -> list[str]:
        cutoff = (now or datetime.now(UTC)).isoformat()
        statuses = (
            JobStatus.QUEUED.value,
            JobStatus.COMPLETED.value,
            JobStatus.PARTIAL.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        )
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT analysis_id FROM analysis_jobs
                WHERE expires_at <= ? AND status IN ({_placeholders(statuses)})
                ORDER BY analysis_id
                """,
                (cutoff, *statuses),
            ).fetchall()
        return [str(row["analysis_id"]) for row in rows]

    def recover_active_jobs(self, *, now: datetime | None = None) -> int:
        """Recupera sólo trabajo cuyo lease venció; un worker vivo conserva ownership."""
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._transaction() as connection:
            requeued = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = CASE WHEN output_prefix IS NULL
                        THEN ? ELSE stage END,
                    updated_at = ?, worker_task_id = NULL, lease_owner_id = NULL,
                    lease_expires_at = NULL
                WHERE status IN (?, ?) AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (
                    JobStatus.QUEUED.value,
                    "recovered_after_lease_expiry",
                    timestamp,
                    JobStatus.VALIDATING.value,
                    JobStatus.RUNNING.value,
                    timestamp,
                ),
            ).rowcount
            cancelled = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?,
                    worker_task_id = NULL, lease_owner_id = NULL, lease_expires_at = NULL
                WHERE status = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (
                    JobStatus.CANCELLED.value,
                    JobStatus.CANCELLED.value,
                    timestamp,
                    timestamp,
                    JobStatus.CANCELLING.value,
                    timestamp,
                ),
            ).rowcount
            return requeued + cancelled

    def _select_required(
        self, connection: sqlite3.Connection, analysis_id: str, error_code: str
    ) -> AnalysisJob:
        row = connection.execute(
            "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - protegido por la transacción
            raise RuntimeError(error_code)
        return _job_from_row(row)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection


def _validate_lease_options(lease_duration: timedelta, max_attempts: int) -> None:
    if lease_duration <= timedelta(0):
        raise ValueError("lease_duration_invalid")
    if max_attempts < 1:
        raise ValueError("max_attempts_invalid")


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def _job_values(job: AnalysisJob) -> tuple[object, ...]:
    return (
        job.analysis_id,
        job.owner_id,
        job.idempotency_key,
        job.request_sha256,
        job.establishment_id,
        job.input_object_key,
        job.input_sha256,
        job.status.value,
        job.stage,
        job.attempt,
        job.created_at.isoformat(),
        job.started_at.isoformat() if job.started_at else None,
        job.updated_at.isoformat(),
        job.completed_at.isoformat() if job.completed_at else None,
        job.configuration_versions,
        job.code_revision,
        job.worker_task_id,
        job.output_prefix,
        job.parent_manifest_sha256,
        job.report_dataset_sha256,
        job.safe_error_code,
        job.expires_at.isoformat(),
        job.declared_land_use,
        job.declared_context_source,
        job.lease_owner_id,
        job.lease_expires_at.isoformat() if job.lease_expires_at else None,
        job.scientific_status.value if job.scientific_status else None,
    )


def _job_from_row(row: sqlite3.Row) -> AnalysisJob:
    return AnalysisJob(
        analysis_id=str(row["analysis_id"]),
        owner_id=str(row["owner_id"]),
        idempotency_key=str(row["idempotency_key"]),
        request_sha256=str(row["request_sha256"]),
        establishment_id=str(row["establishment_id"]),
        input_object_key=str(row["input_object_key"]),
        input_sha256=str(row["input_sha256"]),
        status=JobStatus(str(row["status"])),
        stage=str(row["stage"]),
        attempt=int(row["attempt"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        started_at=_optional_datetime(row["started_at"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        completed_at=_optional_datetime(row["completed_at"]),
        configuration_versions=str(row["configuration_versions"]),
        code_revision=str(row["code_revision"]),
        worker_task_id=str(row["worker_task_id"]) if row["worker_task_id"] else None,
        output_prefix=str(row["output_prefix"]) if row["output_prefix"] else None,
        parent_manifest_sha256=(
            str(row["parent_manifest_sha256"]) if row["parent_manifest_sha256"] else None
        ),
        report_dataset_sha256=(
            str(row["report_dataset_sha256"]) if row["report_dataset_sha256"] else None
        ),
        safe_error_code=str(row["safe_error_code"]) if row["safe_error_code"] else None,
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        declared_land_use=str(row["declared_land_use"]),
        declared_context_source=str(row["declared_context_source"]),
        lease_owner_id=str(row["lease_owner_id"]) if row["lease_owner_id"] else None,
        lease_expires_at=_optional_datetime(row["lease_expires_at"]),
        scientific_status=(
            JobStatus(str(row["scientific_status"])) if row["scientific_status"] else None
        ),
    )


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None
