"""Pruebas de las invariantes semánticas previas a exportar resultados."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest

from deforestation_pipeline.result_validation import (
    AnalysisSummaryValidationError,
    validate_analysis_summary,
)
from deforestation_pipeline.schemas import (
    AnalysisSummary,
    AssessmentStatus,
    ChangeEvent,
    DatasetRecord,
    GeometrySource,
    PostChangeLandUse,
)


def _event(
    *,
    event_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    start_date: date = date(2025, 1, 10),
    end_date: date | None = date(2025, 2, 10),
    detected_change_area_ha: float = 0.4,
    likely_conversion_area_ha: float = 0.0,
    post_change_land_use: PostChangeLandUse = PostChangeLandUse.UNKNOWN,
) -> ChangeEvent:
    return ChangeEvent(
        event_id=UUID(event_id),
        start_date=start_date,
        end_date=end_date,
        detected_change_area_ha=detected_change_area_ha,
        likely_conversion_area_ha=likely_conversion_area_ha,
        change_magnitude=-0.35,
        persistence=0.8,
        sensors=["sentinel-2"],
        post_change_land_use=post_change_land_use,
        confidence=0.7,
        quality_flags=[],
        reason_codes=[],
    )


def _dataset(*, dataset_id: str = "sentinel-2-l2a") -> DatasetRecord:
    return DatasetRecord(
        dataset_id=dataset_id,
        provider="Copernicus",
        collection="Sentinel-2 L2A",
        version="1",
        license="Copernicus Data Space Legal Notice",
        access_date=date(2026, 7, 23),
        attribution="Contains modified Copernicus Sentinel data",
        restrictions=[],
    )


def _summary(**overrides: Any) -> AnalysisSummary:
    payload: dict[str, Any] = {
        "establishment_id": "synthetic-establishment-001",
        "analysis_id": UUID("12345678-1234-5678-1234-567812345678"),
        "analysis_version": "0.1.0",
        "cutoff_date": date(2020, 12, 31),
        "analysis_end_date": date(2026, 7, 23),
        "geometry_source": GeometrySource.OTHER,
        "forest_area_2020_ha": 25.0,
        "detected_change_area_ha": 0.4,
        "likely_conversion_area_ha": 0.0,
        "status": AssessmentStatus.REVIEW_REQUIRED,
        "confidence": 0.65,
        "quality_flags": ["subthreshold_change"],
        "events": [],
        "datasets": [],
        "parameters_hash": "a" * 64,
        "created_at": datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    }
    payload.update(overrides)
    return AnalysisSummary.model_validate(payload)


def test_validation_returns_the_same_validated_summary() -> None:
    """La capa de dominio no debe reconstruir ni alterar el contrato de transporte."""
    summary = _summary()

    assert validate_analysis_summary(summary) is summary


def test_automatic_status_still_excludes_conversion_confirmed() -> None:
    """La confirmación humana no forma parte de los estados automáticos."""
    with pytest.raises(ValueError):
        AssessmentStatus("conversion_confirmed")


def test_validation_rejects_duplicate_event_ids() -> None:
    event = _event()
    summary = _summary(events=[event, event])

    with pytest.raises(AnalysisSummaryValidationError, match="event_id repetido"):
        validate_analysis_summary(summary)


def test_validation_rejects_duplicate_dataset_ids() -> None:
    dataset = _dataset()
    summary = _summary(datasets=[dataset, dataset])

    with pytest.raises(AnalysisSummaryValidationError, match="dataset_id repetido"):
        validate_analysis_summary(summary)


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (
            _event(start_date=date(2026, 7, 24), end_date=None),
            "start_date posterior a analysis_end_date",
        ),
        (
            _event(start_date=date(2026, 7, 22), end_date=date(2026, 7, 24)),
            "end_date posterior a analysis_end_date",
        ),
        (
            ChangeEvent.model_construct(
                **{
                    **_event().model_dump(),
                    "start_date": date(2020, 12, 31),
                }
            ),
            "start_date no es posterior a cutoff_date",
        ),
        (
            ChangeEvent.model_construct(
                **{
                    **_event().model_dump(),
                    "start_date": date(2025, 2, 10),
                    "end_date": date(2025, 2, 9),
                }
            ),
            "end_date anterior a start_date",
        ),
    ],
)
def test_validation_rejects_events_outside_the_analysis_period(
    event: ChangeEvent, message: str
) -> None:
    # model_copy permite verificar que la frontera de dominio se defienda incluso
    # si el objeto llegó desde una reconstrucción confiable que omitió Pydantic.
    summary = _summary().model_copy(update={"events": [event]})

    with pytest.raises(AnalysisSummaryValidationError, match=message):
        validate_analysis_summary(summary)


@pytest.mark.parametrize(
    ("status", "likely_area", "message"),
    [
        (AssessmentStatus.LOW_RISK, 0.1, "low_risk requiere likely_conversion_area_ha igual a 0"),
        (
            AssessmentStatus.CONVERSION_LIKELY,
            0.0,
            "conversion_likely requiere likely_conversion_area_ha mayor a 0",
        ),
        (
            AssessmentStatus.INSUFFICIENT_DATA,
            0.1,
            "insufficient_data requiere likely_conversion_area_ha igual a 0",
        ),
    ],
)
def test_validation_rejects_status_area_contradictions(
    status: AssessmentStatus, likely_area: float, message: str
) -> None:
    summary = _summary(
        detected_change_area_ha=max(0.4, likely_area),
        likely_conversion_area_ha=likely_area,
        status=status,
    )

    with pytest.raises(AnalysisSummaryValidationError, match=message):
        validate_analysis_summary(summary)


@pytest.mark.parametrize(
    "land_use",
    [
        PostChangeLandUse.FOREST_OR_REGENERATION,
        PostChangeLandUse.FIRE_OR_TEMPORARY_DISTURBANCE,
        PostChangeLandUse.UNKNOWN,
    ],
)
def test_validation_rejects_conversion_attributed_to_incompatible_land_use(
    land_use: PostChangeLandUse,
) -> None:
    event = _event(
        likely_conversion_area_ha=0.2,
        post_change_land_use=land_use,
    )
    summary = _summary(
        detected_change_area_ha=0.4,
        likely_conversion_area_ha=0.2,
        status=AssessmentStatus.CONVERSION_LIKELY,
        events=[event],
    )

    with pytest.raises(
        AnalysisSummaryValidationError,
        match=f"conversión probable incompatible con post_change_land_use={land_use.value}",
    ):
        validate_analysis_summary(summary)


def test_validation_accepts_likely_conversion_attributed_to_pasture() -> None:
    event = _event(
        likely_conversion_area_ha=0.2,
        post_change_land_use=PostChangeLandUse.PASTURE,
    )
    summary = _summary(
        likely_conversion_area_ha=0.2,
        status=AssessmentStatus.CONVERSION_LIKELY,
        events=[event],
    )

    assert validate_analysis_summary(summary) is summary


def test_event_conversion_requires_positive_summary_area_and_compatible_status() -> None:
    event = _event(
        likely_conversion_area_ha=0.2,
        post_change_land_use=PostChangeLandUse.PASTURE,
    )
    summary = _summary(
        likely_conversion_area_ha=0.0,
        status=AssessmentStatus.LOW_RISK,
        events=[event],
    )

    with pytest.raises(AnalysisSummaryValidationError) as exc_info:
        validate_analysis_summary(summary)

    assert "eventos con conversión probable requieren likely_conversion_area_ha mayor a 0" in str(
        exc_info.value
    )
    assert "eventos con conversión probable son incompatibles con status low_risk" in str(
        exc_info.value
    )


def test_positive_summary_conversion_requires_a_supporting_event() -> None:
    summary = _summary(
        likely_conversion_area_ha=0.2,
        status=AssessmentStatus.CONVERSION_LIKELY,
        events=[],
    )

    with pytest.raises(
        AnalysisSummaryValidationError,
        match="likely_conversion_area_ha positiva requiere al menos un evento",
    ):
        validate_analysis_summary(summary)


def test_validation_does_not_require_totals_to_equal_event_sums() -> None:
    """Eventos espaciales pueden solaparse y no deben sumarse ingenuamente."""
    events = [
        _event(
            event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            detected_change_area_ha=0.4,
        ),
        _event(
            event_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            detected_change_area_ha=0.4,
        ),
    ]
    summary = _summary(detected_change_area_ha=0.6, events=events)

    assert validate_analysis_summary(summary) is summary


def test_validation_reports_all_detected_contradictions() -> None:
    event = _event(start_date=date(2026, 7, 24), end_date=None)
    summary = _summary(
        status=AssessmentStatus.LOW_RISK,
        likely_conversion_area_ha=0.1,
        events=[event, event],
    )

    with pytest.raises(AnalysisSummaryValidationError) as exc_info:
        validate_analysis_summary(summary)

    assert len(exc_info.value.violations) == 4
    assert "event_id repetido" in str(exc_info.value)
    assert "start_date posterior a analysis_end_date" in str(exc_info.value)
    assert "low_risk requiere likely_conversion_area_ha igual a 0" in str(exc_info.value)
    assert "likely_conversion_area_ha positiva requiere al menos un evento" in str(exc_info.value)
