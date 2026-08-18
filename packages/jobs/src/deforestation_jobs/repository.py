"""Repositorio SQLite con adquisición y transiciones condicionales."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from deforestation_jobs.models import AnalysisJob, JobStatus


class IdempotencyConflictError(RuntimeError):
    """La misma clave fue utilizada para una solicitud diferente."""


class InvalidJobTransitionError(RuntimeError):
    """La transición perdió una carrera o viola el autómata del job."""


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


class SQLiteJobRepository:
    """Persistencia local durable; cada operación usa una conexión breve."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def initialize(self) -> None:
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
                    UNIQUE (owner_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_jobs_queue
                    ON analysis_jobs(status, created_at);
                """
            )

    def create_or_get(self, job: AnalysisJob) -> tuple[AnalysisJob, bool]:
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM analysis_jobs
                WHERE owner_id = ? AND idempotency_key = ?
                """,
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
                    declared_context_source
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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

    def claim_next(self, worker_id: str) -> AnalysisJob | None:
        now = datetime.now(UTC)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT analysis_id FROM analysis_jobs
                WHERE status = ? AND expires_at > ?
                ORDER BY created_at, analysis_id LIMIT 1
                """,
                (JobStatus.QUEUED.value, now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            analysis_id = str(row["analysis_id"])
            changed = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, attempt = attempt + 1,
                    worker_task_id = ?, started_at = COALESCE(started_at, ?),
                    updated_at = ?, safe_error_code = NULL
                WHERE analysis_id = ? AND status = ?
                """,
                (
                    JobStatus.VALIDATING.value,
                    "validating",
                    worker_id,
                    now.isoformat(),
                    now.isoformat(),
                    analysis_id,
                    JobStatus.QUEUED.value,
                ),
            ).rowcount
            if changed != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            if claimed is None:  # pragma: no cover - protegido por la transacción
                raise RuntimeError("claimed_job_disappeared")
            return _job_from_row(claimed)

    def transition(
        self,
        analysis_id: str,
        *,
        expected: JobStatus,
        target: JobStatus,
        stage: str,
    ) -> AnalysisJob:
        if (expected, target) not in _ALLOWED_TRANSITIONS:
            raise InvalidJobTransitionError(f"transition_not_allowed:{expected}:{target}")
        now = datetime.now(UTC).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE analysis_jobs SET status = ?, stage = ?, updated_at = ?
                WHERE analysis_id = ? AND status = ?
                """,
                (target.value, stage, now, analysis_id, expected.value),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("conditional_transition_failed")
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - protegido por la transacción
                raise RuntimeError("transitioned_job_disappeared")
            return _job_from_row(row)

    def complete(
        self,
        analysis_id: str,
        *,
        status: JobStatus,
        output_prefix: str,
        parent_manifest_sha256: str,
        report_dataset_sha256: str | None,
    ) -> AnalysisJob:
        if status not in {JobStatus.COMPLETED, JobStatus.PARTIAL}:
            raise InvalidJobTransitionError("completion_status_invalid")
        now = datetime.now(UTC).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?,
                    output_prefix = ?, parent_manifest_sha256 = ?,
                    report_dataset_sha256 = ?, safe_error_code = NULL
                WHERE analysis_id = ? AND status IN (?, ?)
                """,
                (
                    status.value,
                    status.value,
                    now,
                    now,
                    output_prefix,
                    parent_manifest_sha256,
                    report_dataset_sha256,
                    analysis_id,
                    JobStatus.RUNNING.value,
                    JobStatus.CANCELLING.value,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("conditional_completion_failed")
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("completed_job_disappeared")
            return _job_from_row(row)

    def fail(self, analysis_id: str, *, safe_error_code: str) -> AnalysisJob:
        now = datetime.now(UTC).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?,
                    safe_error_code = ?
                WHERE analysis_id = ? AND status IN (?, ?, ?)
                """,
                (
                    JobStatus.FAILED.value,
                    JobStatus.FAILED.value,
                    now,
                    now,
                    safe_error_code,
                    analysis_id,
                    JobStatus.VALIDATING.value,
                    JobStatus.RUNNING.value,
                    JobStatus.CANCELLING.value,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("conditional_failure_failed")
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("failed_job_disappeared")
            return _job_from_row(row)

    def request_cancellation(self, analysis_id: str) -> AnalysisJob | None:
        """Solicita cancelación sin prometer detener trabajo remoto ya enviado."""
        now = datetime.now(UTC).isoformat()
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
                    UPDATE analysis_jobs
                    SET status = ?, stage = ?, updated_at = ?, completed_at = ?
                    WHERE analysis_id = ? AND status = ?
                    """,
                    (
                        JobStatus.CANCELLED.value,
                        JobStatus.CANCELLED.value,
                        now,
                        now,
                        analysis_id,
                        JobStatus.QUEUED.value,
                    ),
                )
            elif current.status in {JobStatus.VALIDATING, JobStatus.RUNNING}:
                connection.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = ?, stage = ?, updated_at = ?
                    WHERE analysis_id = ? AND status = ?
                    """,
                    (
                        JobStatus.CANCELLING.value,
                        JobStatus.CANCELLING.value,
                        now,
                        analysis_id,
                        current.status.value,
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            if updated is None:  # pragma: no cover - protegido por la transacción
                raise RuntimeError("cancelled_job_disappeared")
            return _job_from_row(updated)

    def mark_cancelled(self, analysis_id: str) -> AnalysisJob:
        """Confirma una cancelación cuando el worker aún no inició el pipeline."""
        now = datetime.now(UTC).isoformat()
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?
                WHERE analysis_id = ? AND status = ?
                """,
                (
                    JobStatus.CANCELLED.value,
                    JobStatus.CANCELLED.value,
                    now,
                    now,
                    analysis_id,
                    JobStatus.CANCELLING.value,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidJobTransitionError("conditional_cancellation_failed")
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - protegido por la transacción
                raise RuntimeError("cancelled_job_disappeared")
            return _job_from_row(row)

    def expired_analysis_ids(self, *, now: datetime | None = None) -> list[str]:
        """Lista objetos privados purgables sin borrar jobs activos ni su auditoría."""
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
                """
                SELECT analysis_id FROM analysis_jobs
                WHERE expires_at <= ? AND status IN (?, ?, ?, ?, ?)
                ORDER BY analysis_id
                """,
                (cutoff, *statuses),
            ).fetchall()
        return [str(row["analysis_id"]) for row in rows]

    def recover_active_jobs(self) -> int:
        now = datetime.now(UTC).isoformat()
        with self._transaction() as connection:
            requeued = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, worker_task_id = NULL
                WHERE status IN (?, ?)
                """,
                (
                    JobStatus.QUEUED.value,
                    "recovered_after_restart",
                    now,
                    JobStatus.VALIDATING.value,
                    JobStatus.RUNNING.value,
                ),
            ).rowcount
            cancelled = connection.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, stage = ?, updated_at = ?, completed_at = ?, worker_task_id = NULL
                WHERE status = ?
                """,
                (
                    JobStatus.CANCELLED.value,
                    JobStatus.CANCELLED.value,
                    now,
                    now,
                    JobStatus.CANCELLING.value,
                ),
            ).rowcount
            return requeued + cancelled

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
        started_at=(datetime.fromisoformat(str(row["started_at"])) if row["started_at"] else None),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        completed_at=(
            datetime.fromisoformat(str(row["completed_at"])) if row["completed_at"] else None
        ),
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
    )
