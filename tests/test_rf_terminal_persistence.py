from __future__ import annotations

import numpy as np
import pytest

from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode
from deforestation_pipeline.rf_terminal_persistence import (
    RF_TERMINAL_PERSISTENCE_SCHEMA_VERSION,
    RFTerminalPersistenceStatus,
    classify_rf_terminal_persistence,
)


def _cube(*trajectories: tuple[ForestTransitionCode, ...]) -> np.ndarray:
    return np.asarray(trajectories, dtype=np.uint8).T[:, np.newaxis, :]


def test_classifies_terminal_recovered_stable_and_short_loss_trajectories() -> None:
    loss = ForestTransitionCode.CANDIDATE_FOREST_LOSS
    forest = ForestTransitionCode.STABLE_FOREST
    result = classify_rf_terminal_persistence(
        transitions=_cube(
            (forest, loss, loss, loss),
            (loss, loss, forest, forest),
            (forest, forest, forest, forest),
            (forest, forest, forest, loss),
        ),
        years=(2021, 2022, 2023, 2024),
        minimum_consecutive_loss_years=2,
    )

    assert result.schema_version == RF_TERMINAL_PERSISTENCE_SCHEMA_VERSION
    assert result.status.tolist() == [
        [
            int(RFTerminalPersistenceStatus.TERMINAL_PERSISTENT_LOSS),
            int(RFTerminalPersistenceStatus.RECOVERED_AFTER_LOSS),
            int(RFTerminalPersistenceStatus.NO_LOSS),
            int(RFTerminalPersistenceStatus.INSUFFICIENT_LOSS_DURATION),
        ]
    ]
    assert result.terminal_persistent_loss_mask.tolist() == [[True, False, False, False]]
    assert result.recovered_loss_mask.tolist() == [[False, True, False, False]]
    assert result.first_loss_year.tolist() == [[2022, 2021, 0, 2024]]
    assert result.last_evaluable_year.tolist() == [[2024, 2024, 2024, 2024]]
    assert result.terminal_consecutive_loss_years.tolist() == [[3, 0, 0, 1]]
    assert result.evaluable_year_count.tolist() == [[4, 4, 4, 4]]
    assert result.gap_year_count.tolist() == [[0, 0, 0, 0]]


def test_unknown_after_loss_is_insufficient_instead_of_negative_or_terminal() -> None:
    loss = ForestTransitionCode.CANDIDATE_FOREST_LOSS
    forest = ForestTransitionCode.STABLE_FOREST
    unknown = ForestTransitionCode.INSUFFICIENT_DATA
    result = classify_rf_terminal_persistence(
        transitions=_cube(
            (forest, loss, loss, unknown),
            (loss, unknown, loss, loss),
            (unknown, forest, forest, forest),
            (forest, forest, forest, unknown),
        ),
        years=(2021, 2022, 2023, 2024),
        minimum_consecutive_loss_years=2,
    )

    assert result.status.tolist() == [
        [
            int(RFTerminalPersistenceStatus.INSUFFICIENT_DATA_AFTER_LOSS),
            int(RFTerminalPersistenceStatus.INSUFFICIENT_DATA_AFTER_LOSS),
            int(RFTerminalPersistenceStatus.INSUFFICIENT_DATA_WITHOUT_OBSERVED_LOSS),
            int(RFTerminalPersistenceStatus.INSUFFICIENT_DATA_WITHOUT_OBSERVED_LOSS),
        ]
    ]
    assert not result.terminal_persistent_loss_mask.any()
    assert result.insufficient_data_mask.tolist() == [[True, True, True, True]]
    assert result.gap_year_count.tolist() == [[1, 1, 1, 1]]


def test_contract_rejects_nonconsecutive_years_and_unknown_transition_codes() -> None:
    loss = ForestTransitionCode.CANDIDATE_FOREST_LOSS
    cube = _cube((loss, loss))

    with pytest.raises(ValueError, match="rf_terminal_persistence_years_invalid"):
        classify_rf_terminal_persistence(
            transitions=cube,
            years=(2021, 2023),
            minimum_consecutive_loss_years=2,
        )

    with pytest.raises(ValueError, match="rf_terminal_persistence_transition_code_invalid"):
        classify_rf_terminal_persistence(
            transitions=np.asarray([[[3]], [[99]]], dtype=np.uint8),
            years=(2021, 2022),
            minimum_consecutive_loss_years=2,
        )
