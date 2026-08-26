"""Pruebas del registro transaccional de jobs compartido por API y worker."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from deforestation_jobs.models import AnalysisJob, JobStatus
from deforestation_jobs.repository import (
    IdempotencyConflictError,
    InvalidJobTransitionError,
    SQLiteJobRepository,
)


def _job(*, analysis_id: str = "11111111-1111-4111-8111-111111111111") -> AnalysisJob:
    return AnalysisJob(
        analysis_id=analysis_id,
        owner_id="local-internal",
        idempotency_key="22222222-2222-4222-8222-222222222222",
        request_sha256="a" * 64,
        establishment_id="establecimiento-2",
        input_object_key=f"analyses/{analysis_id}/input.geojson",
        input_sha256="b" * 64,
        status=JobStatus.QUEUED,
        stage="queued",
        attempt=0,
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
        updated_at=datetime(2026, 8, 17, tzinfo=UTC),
        configuration_versions="{}",
        code_revision="test-revision",
        expires_at=datetime(2026, 9, 16, tzinfo=UTC),
    )


def test_create_or_get_is_idempotent_and_rejects_payload_reuse(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    original = _job()

    first, first_created = repository.create_or_get(original)
    repeated, repeated_created = repository.create_or_get(original)

    assert first_created is True
    assert repeated_created is False
    assert repeated.analysis_id == first.analysis_id

    conflicting = _job(analysis_id="33333333-3333-4333-8333-333333333333")
    conflicting = conflicting.with_request_sha256("c" * 64)
    with pytest.raises(IdempotencyConflictError):
        repository.create_or_get(conflicting)


def test_claim_is_atomic_and_terminal_jobs_cannot_return_to_running(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.create_or_get(_job())

    claimed = repository.claim_next("worker-1")

    assert claimed is not None
    assert claimed.status is JobStatus.VALIDATING
    assert claimed.stage == "validating"
    assert claimed.attempt == 1
    assert claimed.lease_owner_id == "worker-1"
    assert claimed.lease_expires_at is not None
    assert repository.claim_next("worker-2") is None

    running = repository.transition(
        claimed.analysis_id,
        expected=JobStatus.VALIDATING,
        target=JobStatus.RUNNING,
        stage="full_pipeline",
        lease_owner_id="worker-1",
        attempt=1,
    )
    completed = repository.complete(
        running.analysis_id,
        status=JobStatus.COMPLETED,
        output_prefix="analyses/example/output",
        parent_manifest_sha256="d" * 64,
        report_dataset_sha256="e" * 64,
        lease_owner_id="worker-1",
        attempt=1,
    )

    assert completed.status is JobStatus.COMPLETED
    with pytest.raises(InvalidJobTransitionError):
        repository.transition(
            completed.analysis_id,
            expected=JobStatus.COMPLETED,
            target=JobStatus.RUNNING,
            stage="full_pipeline",
            lease_owner_id="worker-1",
            attempt=1,
        )


def test_recover_active_jobs_requeues_without_creating_a_second_job(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.create_or_get(_job())
    claimed = repository.claim_next("dead-worker")
    assert claimed is not None
    repository.transition(
        claimed.analysis_id,
        expected=JobStatus.VALIDATING,
        target=JobStatus.RUNNING,
        stage="agricultural_collection",
        lease_owner_id="dead-worker",
        attempt=1,
    )

    assert claimed.lease_expires_at is not None
    recovered = repository.recover_active_jobs(
        now=claimed.lease_expires_at + timedelta(microseconds=1)
    )
    job = repository.get(claimed.analysis_id)

    assert recovered == 1
    assert job is not None
    assert job.status is JobStatus.QUEUED
    assert job.stage == "recovered_after_lease_expiry"
    assert job.attempt == 1


def test_expired_queued_jobs_are_not_claimed_and_remain_auditable(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    expired = replace(_job(), expires_at=datetime.now(UTC) - timedelta(seconds=1))
    repository.create_or_get(expired)

    assert repository.claim_next("worker-1") is None
    assert repository.expired_analysis_ids(now=datetime.now(UTC)) == [expired.analysis_id]


def test_cancellation_is_idempotent_for_queued_and_active_jobs(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    queued = _job()
    repository.create_or_get(queued)

    cancelled = repository.request_cancellation(queued.analysis_id)
    repeated = repository.request_cancellation(queued.analysis_id)

    assert cancelled is not None
    assert repeated is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.completed_at is not None
    assert repeated.status is JobStatus.CANCELLED

    active = replace(
        _job(analysis_id="33333333-3333-4333-8333-333333333333"),
        idempotency_key="44444444-4444-4444-8444-444444444444",
    )
    repository.create_or_get(active)
    claimed = repository.claim_next("worker-1")
    assert claimed is not None

    cancelling = repository.request_cancellation(active.analysis_id)
    finished = repository.mark_cancelled(
        active.analysis_id,
        lease_owner_id="worker-1",
        attempt=claimed.attempt,
    )

    assert cancelling is not None
    assert cancelling.status is JobStatus.CANCELLING
    assert finished.status is JobStatus.CANCELLED


def test_cancelling_job_can_record_safe_failure(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.create_or_get(_job())
    claimed = repository.claim_next("worker-1")
    assert claimed is not None
    repository.transition(
        claimed.analysis_id,
        expected=JobStatus.VALIDATING,
        target=JobStatus.RUNNING,
        stage="full_pipeline",
        lease_owner_id="worker-1",
        attempt=1,
    )
    repository.request_cancellation(claimed.analysis_id)

    failed = repository.fail(
        claimed.analysis_id,
        safe_error_code="analysis_execution_failed",
        lease_owner_id="worker-1",
        attempt=1,
    )

    assert failed.status is JobStatus.FAILED


def test_initialize_migrates_existing_database_without_losing_jobs(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    legacy = _job()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE analysis_jobs (
                analysis_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                establishment_id TEXT NOT NULL,
                input_object_key TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                attempt INTEGER NOT NULL,
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
            """
        )
        connection.execute(
            """INSERT INTO analysis_jobs VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, NULL,
                NULL, NULL, NULL, NULL, ?, ?, ?
            )""",
            (
                legacy.analysis_id,
                legacy.owner_id,
                legacy.idempotency_key,
                legacy.request_sha256,
                legacy.establishment_id,
                legacy.input_object_key,
                legacy.input_sha256,
                legacy.status.value,
                legacy.stage,
                legacy.attempt,
                legacy.created_at.isoformat(),
                legacy.updated_at.isoformat(),
                legacy.configuration_versions,
                legacy.code_revision,
                legacy.expires_at.isoformat(),
                legacy.declared_land_use,
                legacy.declared_context_source,
            ),
        )

    repository = SQLiteJobRepository(database)
    repository.initialize()

    migrated = repository.get(legacy.analysis_id)
    assert migrated is not None
    assert migrated.analysis_id == legacy.analysis_id
    assert migrated.lease_owner_id is None
    assert migrated.lease_expires_at is None


def test_heartbeat_renews_only_the_current_unexpired_lease(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.create_or_get(_job())
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    claimed = repository.claim_next("worker-1", now=now, lease_duration=timedelta(seconds=30))
    assert claimed is not None

    renewed = repository.heartbeat(
        claimed.analysis_id,
        lease_owner_id="worker-1",
        attempt=claimed.attempt,
        stage="full_pipeline",
        now=now + timedelta(seconds=10),
        lease_duration=timedelta(seconds=30),
    )
    assert renewed.stage == "full_pipeline"
    assert renewed.updated_at == now + timedelta(seconds=10)
    assert renewed.lease_expires_at == now + timedelta(seconds=40)

    with pytest.raises(InvalidJobTransitionError, match="lease_heartbeat_rejected"):
        repository.heartbeat(
            claimed.analysis_id,
            lease_owner_id="worker-2",
            attempt=claimed.attempt,
            stage="publishing_results",
            now=now + timedelta(seconds=11),
            lease_duration=timedelta(seconds=30),
        )
    with pytest.raises(InvalidJobTransitionError, match="lease_heartbeat_rejected"):
        repository.heartbeat(
            claimed.analysis_id,
            lease_owner_id="worker-1",
            attempt=claimed.attempt,
            stage="publishing_results",
            now=now + timedelta(seconds=41),
            lease_duration=timedelta(seconds=30),
        )


def test_recovery_only_takes_expired_leases_and_fences_old_attempt(tmp_path: Path) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.create_or_get(_job())
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    first = repository.claim_next("worker-old", now=now, lease_duration=timedelta(seconds=30))
    assert first is not None
    repository.transition(
        first.analysis_id,
        expected=JobStatus.VALIDATING,
        target=JobStatus.RUNNING,
        stage="full_pipeline",
        lease_owner_id="worker-old",
        attempt=first.attempt,
        now=now + timedelta(seconds=1),
    )

    assert repository.recover_active_jobs(now=now + timedelta(seconds=20)) == 0
    still_owned = repository.get(first.analysis_id)
    assert still_owned is not None
    assert still_owned.status is JobStatus.RUNNING

    assert repository.recover_active_jobs(now=now + timedelta(seconds=31)) == 1
    second = repository.claim_next(
        "worker-new",
        now=now + timedelta(seconds=32),
        lease_duration=timedelta(seconds=30),
    )
    assert second is not None
    assert second.attempt == 2
    repository.transition(
        second.analysis_id,
        expected=JobStatus.VALIDATING,
        target=JobStatus.RUNNING,
        stage="full_pipeline",
        lease_owner_id="worker-new",
        attempt=second.attempt,
        now=now + timedelta(seconds=33),
    )

    with pytest.raises(InvalidJobTransitionError, match="conditional_completion_failed"):
        repository.complete(
            first.analysis_id,
            status=JobStatus.COMPLETED,
            output_prefix="old/output",
            parent_manifest_sha256="d" * 64,
            report_dataset_sha256=None,
            lease_owner_id="worker-old",
            attempt=first.attempt,
            now=now + timedelta(seconds=34),
        )
    completed = repository.complete(
        second.analysis_id,
        status=JobStatus.COMPLETED,
        output_prefix="new/output",
        parent_manifest_sha256="e" * 64,
        report_dataset_sha256=None,
        lease_owner_id="worker-new",
        attempt=second.attempt,
        now=now + timedelta(seconds=34),
    )
    assert completed.output_prefix == "new/output"


def test_postprocess_retry_preserves_science_and_enforces_attempt_limit(
    tmp_path: Path,
) -> None:
    repository = SQLiteJobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.create_or_get(_job())
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    first = repository.claim_next("worker-1", now=now)
    assert first is not None
    repository.transition(
        first.analysis_id,
        expected=JobStatus.VALIDATING,
        target=JobStatus.RUNNING,
        stage="full_pipeline",
        lease_owner_id="worker-1",
        attempt=1,
        now=now + timedelta(seconds=1),
    )
    recorded = repository.record_scientific_result(
        first.analysis_id,
        output_prefix="run-1",
        parent_manifest_sha256="f" * 64,
        scientific_status=JobStatus.PARTIAL,
        lease_owner_id="worker-1",
        attempt=1,
        now=now + timedelta(seconds=2),
    )
    assert recorded.output_prefix == "run-1"
    assert recorded.scientific_status is JobStatus.PARTIAL

    queued = repository.retry_postprocessing(
        first.analysis_id,
        safe_error_code="pdf_render_failed",
        failed_stage="rendering_pdf",
        lease_owner_id="worker-1",
        attempt=1,
        max_attempts=2,
        now=now + timedelta(seconds=3),
    )
    assert queued.status is JobStatus.QUEUED
    assert queued.output_prefix == "run-1"
    assert queued.stage == "rendering_pdf"

    second = repository.claim_next("worker-2", now=now + timedelta(seconds=4))
    assert second is not None
    assert second.attempt == 2
    assert second.stage == "rendering_pdf"
    exhausted = repository.retry_postprocessing(
        second.analysis_id,
        safe_error_code="pdf_render_failed",
        failed_stage="rendering_pdf",
        lease_owner_id="worker-2",
        attempt=2,
        max_attempts=2,
        now=now + timedelta(seconds=5),
    )
    assert exhausted.status is JobStatus.FAILED
    assert exhausted.safe_error_code == "pdf_render_failed"
    assert exhausted.output_prefix == "run-1"
