"""Invariantes de dominio para finalizar un resumen antes de exportarlo."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from deforestation_pipeline.schemas import (
    AnalysisSummary,
    AssessmentStatus,
    PostChangeLandUse,
)

_NON_CONVERSION_LAND_USES: Final = frozenset(
    {
        PostChangeLandUse.FOREST_OR_REGENERATION,
        PostChangeLandUse.FIRE_OR_TEMPORARY_DISTURBANCE,
        PostChangeLandUse.UNKNOWN,
    }
)
_CONVERSION_SUPPORTING_STATUSES: Final = frozenset(
    {
        AssessmentStatus.REVIEW_REQUIRED,
        AssessmentStatus.CONVERSION_LIKELY,
    }
)


class AnalysisSummaryValidationError(ValueError):
    """Agrupa contradicciones semánticas encontradas en un resumen."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(violations)
        details = "\n".join(f"- {violation}" for violation in self.violations)
        super().__init__(f"AnalysisSummary contiene contradicciones semánticas:\n{details}")


def validate_analysis_summary(summary: AnalysisSummary) -> AnalysisSummary:
    """Valida reglas entre campos y devuelve la misma instancia si es coherente.

    Esta función complementa la validación estructural de Pydantic. Debe
    ejecutarse después de construir ``AnalysisSummary`` y antes de exportar
    evidencia o publicar el estado automático.
    """
    violations: list[str] = []

    _validate_unique_identifiers(summary, violations)
    _validate_event_dates(summary, violations)
    _validate_status_and_area(summary, violations)
    _validate_conversion_land_use(summary, violations)

    if violations:
        raise AnalysisSummaryValidationError(violations)
    return summary


def _validate_unique_identifiers(summary: AnalysisSummary, violations: list[str]) -> None:
    event_ids = [str(event.event_id) for event in summary.events]
    for event_id in _duplicates(event_ids):
        _append_unique(violations, f"events contiene event_id repetido: {event_id}")

    dataset_ids = [dataset.dataset_id for dataset in summary.datasets]
    for dataset_id in _duplicates(dataset_ids):
        _append_unique(violations, f"datasets contiene dataset_id repetido: {dataset_id}")


def _validate_event_dates(summary: AnalysisSummary, violations: list[str]) -> None:
    for event in summary.events:
        prefix = f"evento {event.event_id}"
        if event.start_date <= summary.cutoff_date:
            _append_unique(
                violations,
                f"{prefix}: start_date no es posterior a cutoff_date",
            )
        if event.start_date > summary.analysis_end_date:
            _append_unique(
                violations,
                f"{prefix}: start_date posterior a analysis_end_date",
            )
        if event.end_date is None:
            continue
        if event.end_date < event.start_date:
            _append_unique(violations, f"{prefix}: end_date anterior a start_date")
        if event.end_date > summary.analysis_end_date:
            _append_unique(
                violations,
                f"{prefix}: end_date posterior a analysis_end_date",
            )


def _validate_status_and_area(summary: AnalysisSummary, violations: list[str]) -> None:
    likely_area = summary.likely_conversion_area_ha
    has_likely_conversion_event = any(
        event.likely_conversion_area_ha > 0 for event in summary.events
    )

    if summary.status is AssessmentStatus.LOW_RISK and likely_area > 0:
        violations.append("status low_risk requiere likely_conversion_area_ha igual a 0")
    if summary.status is AssessmentStatus.CONVERSION_LIKELY and likely_area <= 0:
        violations.append("status conversion_likely requiere likely_conversion_area_ha mayor a 0")
    if summary.status is AssessmentStatus.INSUFFICIENT_DATA and likely_area > 0:
        violations.append("status insufficient_data requiere likely_conversion_area_ha igual a 0")
    if has_likely_conversion_event and likely_area <= 0:
        violations.append(
            "eventos con conversión probable requieren likely_conversion_area_ha mayor a 0"
        )
    if has_likely_conversion_event and summary.status not in _CONVERSION_SUPPORTING_STATUSES:
        violations.append(
            f"eventos con conversión probable son incompatibles con status {summary.status.value}"
        )
    if likely_area > 0 and not has_likely_conversion_event:
        violations.append(
            "likely_conversion_area_ha positiva requiere al menos un evento con conversión probable"
        )


def _validate_conversion_land_use(summary: AnalysisSummary, violations: list[str]) -> None:
    for event in summary.events:
        if (
            event.likely_conversion_area_ha > 0
            and event.post_change_land_use in _NON_CONVERSION_LAND_USES
        ):
            _append_unique(
                violations,
                (
                    f"evento {event.event_id}: conversión probable incompatible con "
                    f"post_change_land_use={event.post_change_land_use.value}"
                ),
            )


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen:
            _append_unique(duplicates, value)
        else:
            seen.add(value)
    return duplicates


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
