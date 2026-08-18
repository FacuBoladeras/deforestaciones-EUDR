"""Contratos compartidos para la ejecución asíncrona de análisis."""

from deforestation_jobs.models import AnalysisJob, JobStatus
from deforestation_jobs.repository import SQLiteJobRepository

__all__ = ["AnalysisJob", "JobStatus", "SQLiteJobRepository"]
