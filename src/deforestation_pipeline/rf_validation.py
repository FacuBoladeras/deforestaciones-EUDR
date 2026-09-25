"""Selección ciega y métricas para validar independientemente el RF forestal P0."""

from __future__ import annotations

import csv
import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ReferenceLabel = Literal["forest", "non_forest", "uncertain"]
Confidence = Literal["high", "medium", "low"]
ReasonCode = Literal[
    "closed_woody",
    "open_woody",
    "pasture",
    "crop",
    "mixed_pixel",
    "water",
    "infrastructure",
    "insufficient_evidence",
    "other",
]

BLIND_FIELDS = (
    "blind_id",
    "reference_label",
    "confidence",
    "reason_code",
    "reviewer_id",
    "reviewed_at",
    "notes",
)


class StrictValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SampleQuotas(StrictValidationModel):
    forest: Annotated[int, Field(ge=0)]
    non_forest: Annotated[int, Field(ge=0)]
    ambiguous: Annotated[int, Field(ge=0)]

    @property
    def total(self) -> int:
        return self.forest + self.non_forest + self.ambiguous


class GateThresholds(StrictValidationModel):
    minimum_adjudicable_fraction: float = Field(default=0.80, ge=0, le=1)
    minimum_global_forest_recall: float = Field(default=0.90, ge=0, le=1)
    minimum_utm_forest_recall: float = Field(default=0.85, ge=0, le=1)
    minimum_forest_precision: float = Field(default=0.80, ge=0, le=1)
    minimum_balanced_accuracy: float = Field(default=0.85, ge=0, le=1)
    minimum_open_woody_recall: float = Field(default=0.70, ge=0, le=1)


class BlindAdjudication(StrictValidationModel):
    blind_id: str = Field(min_length=1)
    reference_label: ReferenceLabel
    confidence: Confidence
    reason_code: ReasonCode
    reviewer_id: str = Field(min_length=1)
    reviewed_at: date
    notes: str = ""


class BinaryMetrics(StrictValidationModel):
    evaluated_count: int = Field(ge=0)
    true_positive: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    true_negative: int = Field(ge=0)
    precision_forest: float | None = Field(default=None, ge=0, le=1)
    recall_forest: float | None = Field(default=None, ge=0, le=1)
    specificity_non_forest: float | None = Field(default=None, ge=0, le=1)
    f1_forest: float | None = Field(default=None, ge=0, le=1)
    balanced_accuracy: float | None = Field(default=None, ge=0, le=1)
    commission_error: float | None = Field(default=None, ge=0, le=1)
    omission_error: float | None = Field(default=None, ge=0, le=1)


class ValidationReport(StrictValidationModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    expected_test_size: int = Field(gt=0)
    completed_count: int = Field(ge=0)
    adjudicable_count: int = Field(ge=0)
    uncertain_count: int = Field(ge=0)
    adjudicable_fraction: float = Field(ge=0, le=1)
    global_metrics: BinaryMetrics
    metrics_by_utm: dict[str, BinaryMetrics]
    open_woody_metrics: BinaryMetrics
    thresholds: GateThresholds
    checks: dict[str, bool]
    passed: bool
    failed_checks: tuple[str, ...]
    test_set_used_for_threshold_tuning: Literal[False] = False


def read_csv_rows(paths: Sequence[Path]) -> list[dict[str, str]]:
    """Lee CSVs P0 con stdlib y rechaza esquemas heterogéneos."""
    rows: list[dict[str, str]] = []
    expected_fields: tuple[str, ...] | None = None
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = tuple(reader.fieldnames or ())
            if expected_fields is None:
                expected_fields = fields
            elif fields != expected_fields:
                raise ValueError("los CSV P0 no comparten el mismo esquema y orden")
            rows.extend(dict(row) for row in reader)
    required = {
        "sample_id",
        "block_id",
        "split",
        "proxy_label",
        "longitude",
        "latitude",
        "utm_zone",
    }
    if expected_fields is None or not required <= set(expected_fields):
        raise ValueError("los CSV P0 no contienen la metadata mínima")
    if len({row["sample_id"] for row in rows}) != len(rows):
        raise ValueError("sample_id debe ser único entre ambos CSV")
    return rows


def select_stratified_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    split: str,
    quotas: SampleQuotas,
    seed: int,
) -> list[dict[str, str]]:
    """Selecciona por zona/clase, maximizando bloques antes de repetirlos."""
    selected: list[dict[str, str]] = []
    quota_by_label = quotas.model_dump()
    zones = sorted({row["utm_zone"] for row in rows if row.get("split") == split})
    for zone in zones:
        for label in ("forest", "non_forest", "ambiguous"):
            candidates = [
                dict(row)
                for row in rows
                if row.get("split") == split
                and row.get("utm_zone") == zone
                and row.get("proxy_label") == label
            ]
            ordered = sorted(
                candidates,
                key=lambda row: _stable_rank(seed, split, zone, label, row["sample_id"]),
            )
            diverse = _blocks_first(ordered)
            selected.extend(diverse[: quota_by_label[label]])
    return selected


def build_blind_and_sealed_rows(
    selected_rows: Sequence[Mapping[str, str]],
    *,
    phase: Literal["pilot", "test"],
    seed: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Separa el formulario público de toda metadata capaz de sesgar al revisor."""
    prefix = "P" if phase == "pilot" else "T"
    shuffled = sorted(
        selected_rows,
        key=lambda row: _stable_rank(seed, "blind", phase, row["sample_id"]),
    )
    blind: list[dict[str, str]] = []
    sealed: list[dict[str, str]] = []
    for index, row in enumerate(shuffled, start=1):
        blind_id = f"{prefix}-{index:03d}"
        blind.append(
            {
                "blind_id": blind_id,
                "reference_label": "",
                "confidence": "",
                "reason_code": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "notes": "",
            }
        )
        sealed.append({"blind_id": blind_id, "phase": phase, **dict(row)})
    return blind, sealed


def validate_adjudications(rows: Iterable[Mapping[str, str]]) -> tuple[BlindAdjudication, ...]:
    """Valida etiquetas completadas; el template vacío no es un resultado."""
    adjudications = tuple(BlindAdjudication.model_validate(dict(row)) for row in rows)
    identifiers = [item.blind_id for item in adjudications]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("blind_id duplicado en adjudicaciones")
    return adjudications


def compute_validation_report(
    *,
    sealed_rows: Sequence[Mapping[str, str]],
    adjudication_rows: Sequence[Mapping[str, str]],
    expected_test_size: int,
    thresholds: GateThresholds,
) -> ValidationReport:
    """Une por ID ciego, excluye uncertain y evalúa el gate previamente congelado."""
    adjudications = validate_adjudications(adjudication_rows)
    sealed_by_id = _unique_by_blind_id(sealed_rows)
    completed = []
    for adjudication in adjudications:
        sealed = sealed_by_id.get(adjudication.blind_id)
        if sealed is None:
            raise ValueError(f"blind_id sin fila sellada: {adjudication.blind_id}")
        prediction = _binary_prediction(sealed.get("rf_prediction"))
        completed.append((adjudication, sealed, prediction))
    evaluated = [item for item in completed if item[0].reference_label != "uncertain"]
    global_metrics = _metrics(evaluated)
    by_utm: dict[str, BinaryMetrics] = {}
    zones = sorted({str(item[1].get("utm_zone", "")) for item in evaluated})
    for zone in zones:
        by_utm[zone] = _metrics([item for item in evaluated if str(item[1]["utm_zone"]) == zone])
    open_woody = _metrics(
        [
            item
            for item in evaluated
            if item[0].reference_label == "forest" and item[0].reason_code == "open_woody"
        ]
    )
    adjudicable_fraction = len(evaluated) / expected_test_size
    checks = {
        "adjudicable_fraction": (adjudicable_fraction >= thresholds.minimum_adjudicable_fraction),
        "global_forest_recall": _at_least(
            global_metrics.recall_forest,
            thresholds.minimum_global_forest_recall,
        ),
        "each_utm_forest_recall": bool(by_utm)
        and all(
            _at_least(metrics.recall_forest, thresholds.minimum_utm_forest_recall)
            for metrics in by_utm.values()
        ),
        "forest_precision": _at_least(
            global_metrics.precision_forest,
            thresholds.minimum_forest_precision,
        ),
        "balanced_accuracy": _at_least(
            global_metrics.balanced_accuracy,
            thresholds.minimum_balanced_accuracy,
        ),
        "no_severe_open_woody_omission": _at_least(
            open_woody.recall_forest,
            thresholds.minimum_open_woody_recall,
        ),
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    return ValidationReport(
        expected_test_size=expected_test_size,
        completed_count=len(completed),
        adjudicable_count=len(evaluated),
        uncertain_count=len(completed) - len(evaluated),
        adjudicable_fraction=adjudicable_fraction,
        global_metrics=global_metrics,
        metrics_by_utm=by_utm,
        open_woody_metrics=open_woody,
        thresholds=thresholds,
        checks=checks,
        passed=not failed,
        failed_checks=failed,
    )


def write_csv_rows(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    fieldnames: Sequence[str],
) -> None:
    """Escribe tablas con orden explícito y reemplazo atómico."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _blocks_first(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    by_block: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_block[row["block_id"]].append(row)
    ordered: list[dict[str, str]] = []
    max_depth = max((len(items) for items in by_block.values()), default=0)
    block_order = sorted(by_block, key=lambda block: rows.index(by_block[block][0]))
    for depth in range(max_depth):
        ordered.extend(
            by_block[block][depth] for block in block_order if depth < len(by_block[block])
        )
    return ordered


def _stable_rank(seed: int, *parts: object) -> str:
    payload = "|".join((str(seed), *(str(part) for part in parts)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unique_by_blind_id(rows: Sequence[Mapping[str, str]]) -> dict[str, Mapping[str, str]]:
    result: dict[str, Mapping[str, str]] = {}
    for row in rows:
        blind_id = row.get("blind_id", "")
        if not blind_id or blind_id in result:
            raise ValueError("tabla sellada requiere blind_id único y no vacío")
        result[blind_id] = row
    return result


def _binary_prediction(value: object) -> int:
    if str(value) not in {"0", "1"}:
        raise ValueError("rf_prediction debe ser 0 o 1")
    return int(str(value))


def _metrics(
    rows: Sequence[tuple[BlindAdjudication, Mapping[str, str], int]],
) -> BinaryMetrics:
    confusion: Counter[tuple[int, int]] = Counter()
    for adjudication, _sealed, prediction in rows:
        reference = 1 if adjudication.reference_label == "forest" else 0
        confusion[(reference, prediction)] += 1
    tp = confusion[(1, 1)]
    fp = confusion[(0, 1)]
    fn = confusion[(1, 0)]
    tn = confusion[(0, 0)]
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    specificity = _ratio(tn, tn + fp)
    f1 = (
        None
        if precision is None or recall is None or precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )
    balanced = None if recall is None or specificity is None else (recall + specificity) / 2
    return BinaryMetrics(
        evaluated_count=len(rows),
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=tn,
        precision_forest=precision,
        recall_forest=recall,
        specificity_non_forest=specificity,
        f1_forest=f1,
        balanced_accuracy=balanced,
        commission_error=None if precision is None else 1 - precision,
        omission_error=None if recall is None else 1 - recall,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and not math.isnan(value) and value >= threshold
