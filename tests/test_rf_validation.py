from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from deforestation_pipeline.rf_validation import (
    BlindAdjudication,
    GateThresholds,
    SampleQuotas,
    build_blind_and_sealed_rows,
    compute_validation_report,
    select_stratified_rows,
    validate_adjudications,
    write_csv_rows,
)


def _rows(*, split: str, per_label: int = 8) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for zone in (20, 21):
        for label, vote_count in (("forest", 4), ("non_forest", 0), ("ambiguous", 2)):
            for index in range(per_label):
                rows.append(
                    {
                        "sample_id": f"s_{split}_z{zone}_{label}_{index}",
                        "block_id": f"z{zone}_b{index % 5}_{index // 5}",
                        "split": split,
                        "proxy_label": label,
                        "longitude": str(-60.5 + index / 100),
                        "latitude": str(-31.5 + index / 100),
                        "utm_zone": str(zone),
                        "forest_vote_count": str(vote_count),
                        "source_vote_count": "4",
                        "rf_prediction": str(index % 2),
                    }
                )
    return rows


def test_selection_is_deterministic_spatially_diverse_and_prediction_blind() -> None:
    rows = _rows(split="test")
    quotas = SampleQuotas(forest=4, non_forest=4, ambiguous=2)

    first = select_stratified_rows(rows, split="test", quotas=quotas, seed=71)
    modified_predictions = [dict(row, rf_prediction="9") for row in rows]
    second = select_stratified_rows(
        modified_predictions,
        split="test",
        quotas=quotas,
        seed=71,
    )

    assert [row["sample_id"] for row in first] == [row["sample_id"] for row in second]
    assert len(first) == 20
    assert len({row["block_id"] for row in first}) >= 10
    assert all(row["split"] == "test" for row in first)


def test_selection_adapts_to_available_quota_without_inventing_rows() -> None:
    rows = _rows(split="test", per_label=2)
    selected = select_stratified_rows(
        rows,
        split="test",
        quotas=SampleQuotas(forest=4, non_forest=4, ambiguous=2),
        seed=3,
    )

    assert len(selected) == 12
    assert {row["sample_id"] for row in selected} <= {row["sample_id"] for row in rows}


def test_pilot_and_test_are_disjoint() -> None:
    pilot = select_stratified_rows(
        _rows(split="validation"),
        split="validation",
        quotas=SampleQuotas(forest=2, non_forest=2, ambiguous=1),
        seed=42,
    )
    test = select_stratified_rows(
        _rows(split="test"),
        split="test",
        quotas=SampleQuotas(forest=4, non_forest=4, ambiguous=2),
        seed=42,
    )

    assert {row["sample_id"] for row in pilot}.isdisjoint({row["sample_id"] for row in test})


def test_blind_table_excludes_private_and_model_fields() -> None:
    selected = select_stratified_rows(
        _rows(split="test"),
        split="test",
        quotas=SampleQuotas(forest=1, non_forest=1, ambiguous=1),
        seed=4,
    )
    blind, sealed = build_blind_and_sealed_rows(selected, phase="test", seed=4)

    assert set(blind[0]) == {
        "blind_id",
        "reference_label",
        "confidence",
        "reason_code",
        "reviewer_id",
        "reviewed_at",
        "notes",
    }
    forbidden = {"sample_id", "longitude", "latitude", "proxy_label", "rf_prediction"}
    assert forbidden.isdisjoint(blind[0])
    assert forbidden <= set(sealed[0])
    assert {row["blind_id"] for row in blind} == {row["blind_id"] for row in sealed}


def test_adjudication_schema_accepts_uncertain_and_rejects_invalid_reason() -> None:
    valid = BlindAdjudication(
        blind_id="T-001",
        reference_label="uncertain",
        confidence="low",
        reason_code="insufficient_evidence",
        reviewer_id="reviewer-a",
        reviewed_at=date(2026, 8, 3),
        notes="",
    )
    assert validate_adjudications([valid.model_dump()])[0] == valid

    diagnostic_uncertain = valid.model_copy(update={"reason_code": "open_woody"})
    assert validate_adjudications([diagnostic_uncertain.model_dump()])[0] == diagnostic_uncertain

    invalid = valid.model_dump()
    invalid["reason_code"] = "proxy_disagreement"
    with pytest.raises(ValueError, match="reason_code"):
        validate_adjudications([invalid])


def test_metrics_exclude_uncertain_and_gate_passes_only_all_thresholds() -> None:
    sealed: list[dict[str, str]] = []
    adjudications: list[dict[str, str]] = []
    for index in range(200):
        forest = index < 100
        zone = 20 if index % 2 == 0 else 21
        prediction = 1 if forest else 0
        sealed.append(
            {
                "blind_id": f"T-{index:03d}",
                "utm_zone": str(zone),
                "rf_prediction": str(prediction),
                "rf_vote_fraction": "0.9" if prediction else "0.1",
            }
        )
        adjudications.append(
            {
                "blind_id": f"T-{index:03d}",
                "reference_label": (
                    "uncertain" if index >= 190 else ("forest" if forest else "non_forest")
                ),
                "confidence": "low" if index >= 190 else "high",
                "reason_code": (
                    "insufficient_evidence"
                    if index >= 190
                    else ("open_woody" if index < 20 else ("closed_woody" if forest else "pasture"))
                ),
                "reviewer_id": "reviewer-a",
                "reviewed_at": "2026-08-03",
                "notes": "",
            }
        )

    report = compute_validation_report(
        sealed_rows=sealed,
        adjudication_rows=adjudications,
        expected_test_size=200,
        thresholds=GateThresholds(),
    )

    assert report.adjudicable_count == 190
    assert report.uncertain_count == 10
    assert report.global_metrics.recall_forest == 1
    assert report.passed is True

    sealed[0]["rf_prediction"] = "0"
    strict_report = compute_validation_report(
        sealed_rows=sealed,
        adjudication_rows=adjudications,
        expected_test_size=200,
        thresholds=GateThresholds(minimum_global_forest_recall=1.0),
    )
    assert strict_report.passed is False
    assert "global_forest_recall" in strict_report.failed_checks


def test_csv_writer_preserves_explicit_field_order(tmp_path: Path) -> None:
    path = tmp_path / "table.csv"
    write_csv_rows(path, [{"b": "2", "a": "1"}], fieldnames=("a", "b"))

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["a", "b"]
        assert list(reader) == [{"a": "1", "b": "2"}]
