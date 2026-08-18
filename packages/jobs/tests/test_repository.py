"""Pruebas del registro transaccional de jobs compartido por API y worker."""

from __future__ import annotations

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
    assert repository.claim_next("worker-2") is None

    running = repository.transition(
        claimed.analysis_id,
        expected=JobStatus.VALIDATING,
        target=JobStatus.RUNNING,
        stage="full_pipeline",
    )
    completed = repository.complete(
        running.analysis_id,
        status=JobStatus.COMPLETED,
        output_prefix="analyses/example/output",
        parent_manifest_sha256="d" * 64,
        report_dataset_sha256="e" * 64,
    )

    assert completed.status is JobStatus.COMPLETED
    with pytest.raises(InvalidJobTransitionError):
        repository.transition(
            completed.analysis_id,
            expected=JobStatus.COMPLETED,
            target=JobStatus.RUNNING,
            stage="full_pipeline",
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
    )

    recovered = repository.recover_active_jobs()
    job = repository.get(claimed.analysis_id)

    assert recovered == 1
    assert job is not None
    assert job.status is JobStatus.QUEUED
    assert job.stage == "recovered_after_restart"
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
    finished = repository.mark_cancelled(active.analysis_id)

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
    )
    repository.request_cancellation(claimed.analysis_id)

    failed = repository.fail(claimed.analysis_id, safe_error_code="analysis_execution_failed")

    assert failed.status is JobStatus.FAILED
