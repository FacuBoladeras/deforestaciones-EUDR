"""Modelo inmutable del registro operativo de análisis."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    """Estados operativos públicos de un análisis."""

    QUEUED = "queued"
    VALIDATING = "validating"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    """Snapshot completo de un job, sin geometría ni secretos embebidos."""

    analysis_id: str
    owner_id: str
    idempotency_key: str
    request_sha256: str
    establishment_id: str
    input_object_key: str
    input_sha256: str
    status: JobStatus
    stage: str
    attempt: int
    created_at: datetime
    updated_at: datetime
    configuration_versions: str
    code_revision: str
    expires_at: datetime
    declared_land_use: str = "unknown"
    declared_context_source: str = "not_provided"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    worker_task_id: str | None = None
    output_prefix: str | None = None
    parent_manifest_sha256: str | None = None
    report_dataset_sha256: str | None = None
    safe_error_code: str | None = None

    def with_request_sha256(self, digest: str) -> AnalysisJob:
        """Crea una variante para probar o comparar una solicitud idempotente."""
        return replace(self, request_sha256=digest)

    def to_public_dict(self) -> dict[str, Any]:
        """Proyecta únicamente estado operativo seguro para HTTP."""
        return {
            "analysis_id": self.analysis_id,
            "status": self.status.value,
            "stage": self.stage,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "attempt": self.attempt,
            "safe_error_code": self.safe_error_code,
        }
