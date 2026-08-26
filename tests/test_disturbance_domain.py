"""Dominio forestal evaluable del Paso 14.1, sin ejecutar detectores."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from deforestation_pipeline.change_detection import (
    DisturbanceQualityBit,
    ForestEvaluationDomainCode,
    build_forest_evaluation_domain,
)
from deforestation_pipeline.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_domain_keeps_consensus_and_disagreement_separate() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    result = build_forest_evaluation_domain(
        source_count=np.array([[2, 2, 2, 1]], dtype=np.uint8),
        consensus_forest=np.array([[1, 0, 0, 0]], dtype=np.uint8),
        disagreement=np.array([[0, 1, 0, 0]], dtype=np.uint8),
        config=config.disturbance_detection,
    )

    np.testing.assert_array_equal(
        result.domain_code,
        np.array(
            [
                [
                    ForestEvaluationDomainCode.EVALUABLE_CONSENSUS_FOREST,
                    ForestEvaluationDomainCode.EVALUABLE_BASELINE_UNCERTAIN,
                    ForestEvaluationDomainCode.NOT_EVALUABLE_NON_FOREST,
                    ForestEvaluationDomainCode.NOT_EVALUABLE_INSUFFICIENT_SOURCES,
                ]
            ],
            dtype=np.uint8,
        ),
    )
    np.testing.assert_array_equal(
        result.evaluable,
        np.array([[True, True, False, False]]),
    )


def test_baseline_uncertainty_is_propagated_without_inventing_probability() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    result = build_forest_evaluation_domain(
        source_count=np.array([[2, 2, 1]], dtype=np.uint8),
        consensus_forest=np.array([[1, 0, 0]], dtype=np.uint8),
        disagreement=np.array([[0, 1, 0]], dtype=np.uint8),
        config=config.disturbance_detection,
    )

    baseline_uncertain_bit = np.uint16(1 << DisturbanceQualityBit.BASELINE_UNCERTAIN)
    np.testing.assert_array_equal(
        result.baseline_uncertain,
        np.array([[False, True, True]]),
    )
    np.testing.assert_array_equal(
        (result.quality_flags_bitmask & baseline_uncertain_bit) > 0,
        np.array([[False, True, True]]),
    )
    assert not hasattr(result, "probability")


def test_evaluable_domain_boundary_is_flagged_without_excluding_interior() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    consensus = np.zeros((5, 5), dtype=np.uint8)
    consensus[1:4, 1:4] = 1

    result = build_forest_evaluation_domain(
        source_count=np.full((5, 5), 2, dtype=np.uint8),
        consensus_forest=consensus,
        disagreement=np.zeros((5, 5), dtype=np.uint8),
        config=config.disturbance_detection,
    )

    edge_bit = np.uint16(1 << DisturbanceQualityBit.EDGE_PIXEL)
    expected_edge = np.zeros((5, 5), dtype=np.bool_)
    expected_edge[1:4, 1:4] = True
    expected_edge[2, 2] = False
    np.testing.assert_array_equal(
        (result.quality_flags_bitmask & edge_bit) > 0,
        expected_edge,
    )
    assert result.evaluable[2, 2]
    assert not (result.quality_flags_bitmask[2, 2] & edge_bit)


def test_domain_does_not_mutate_baseline_inputs() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    source_count = np.array([[2, 2]], dtype=np.uint8)
    consensus = np.array([[1, 0]], dtype=np.uint8)
    disagreement = np.array([[0, 1]], dtype=np.uint8)
    original = tuple(array.copy() for array in (source_count, consensus, disagreement))

    build_forest_evaluation_domain(
        source_count=source_count,
        consensus_forest=consensus,
        disagreement=disagreement,
        config=config.disturbance_detection,
    )

    for actual, expected in zip(
        (source_count, consensus, disagreement),
        original,
        strict=True,
    ):
        np.testing.assert_array_equal(actual, expected)


def test_domain_rejects_incompatible_shapes() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    with pytest.raises(ValueError, match="misma forma"):
        build_forest_evaluation_domain(
            source_count=np.ones((2, 2), dtype=np.uint8) * 2,
            consensus_forest=np.ones((2, 2), dtype=np.uint8),
            disagreement=np.zeros((1, 2), dtype=np.uint8),
            config=config.disturbance_detection,
        )


@pytest.mark.parametrize("field", ["consensus_forest", "disagreement"])
def test_domain_rejects_non_binary_baseline_masks(field: str) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    inputs = {
        "source_count": np.array([[2]], dtype=np.uint8),
        "consensus_forest": np.array([[1]], dtype=np.uint8),
        "disagreement": np.array([[0]], dtype=np.uint8),
    }
    inputs[field] = np.array([[2]], dtype=np.uint8)

    with pytest.raises(ValueError, match=f"{field} debe ser binaria"):
        build_forest_evaluation_domain(
            **inputs,
            config=config.disturbance_detection,
        )


def test_domain_rejects_logically_inconsistent_baseline_evidence() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    with pytest.raises(ValueError, match="mutuamente excluyentes"):
        build_forest_evaluation_domain(
            source_count=np.array([[2]], dtype=np.uint8),
            consensus_forest=np.array([[1]], dtype=np.uint8),
            disagreement=np.array([[1]], dtype=np.uint8),
            config=config.disturbance_detection,
        )

    with pytest.raises(ValueError, match="fuentes suficientes"):
        build_forest_evaluation_domain(
            source_count=np.array([[1]], dtype=np.uint8),
            consensus_forest=np.array([[1]], dtype=np.uint8),
            disagreement=np.array([[0]], dtype=np.uint8),
            config=config.disturbance_detection,
        )
