"""Contrato puro para persistencia terminal de pérdida forestal RF.

La clasificación describe trayectorias aprendidas y no convierte una salida RF
en probabilidad calibrada, evidencia independiente ni conclusión EUDR.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from itertools import pairwise
from typing import Any, Final

import numpy as np
from numpy.typing import NDArray

from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode

RF_TERMINAL_PERSISTENCE_SCHEMA_VERSION: Final = "1.0.0"
RF_TERMINAL_PERSISTENCE_TRANSFER_YEARS: Final = frozenset(range(2021, 2025))


class RFTerminalPersistenceStatus(IntEnum):
    """Resolución temporal por píxel respecto de la línea base RF 2020."""

    NO_LOSS = 0
    TERMINAL_PERSISTENT_LOSS = 1
    RECOVERED_AFTER_LOSS = 2
    INSUFFICIENT_DATA_AFTER_LOSS = 3
    INSUFFICIENT_LOSS_DURATION = 4
    INSUFFICIENT_DATA_WITHOUT_OBSERVED_LOSS = 5


@dataclass(frozen=True, slots=True)
class RFTerminalPersistenceResult:
    """Resultado espacial auditable de la clasificación temporal RF."""

    schema_version: str
    years: tuple[int, ...]
    minimum_consecutive_loss_years: int
    status: NDArray[np.uint8]
    terminal_persistent_loss_mask: NDArray[np.bool_]
    recovered_loss_mask: NDArray[np.bool_]
    insufficient_data_mask: NDArray[np.bool_]
    first_loss_year: NDArray[np.int16]
    last_evaluable_year: NDArray[np.int16]
    terminal_consecutive_loss_years: NDArray[np.uint8]
    evaluable_year_count: NDArray[np.uint8]
    gap_year_count: NDArray[np.uint8]


def classify_rf_terminal_persistence(
    *,
    transitions: NDArray[Any],
    years: tuple[int, ...],
    minimum_consecutive_loss_years: int = 2,
) -> RFTerminalPersistenceResult:
    """Clasifica pérdida terminal sin convertir huecos en evidencia negativa."""
    values = np.asarray(transitions)
    _validate_inputs(
        transitions=values,
        years=years,
        minimum_consecutive_loss_years=minimum_consecutive_loss_years,
    )
    height, width = values.shape[1:]
    shape = (height, width)
    status = np.full(shape, int(RFTerminalPersistenceStatus.NO_LOSS), dtype=np.uint8)
    first_loss_year = np.zeros(shape, dtype=np.int16)
    last_evaluable_year = np.zeros(shape, dtype=np.int16)
    terminal_run = np.zeros(shape, dtype=np.uint8)

    insufficient_code = int(ForestTransitionCode.INSUFFICIENT_DATA)
    loss_code = int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)
    evaluable = values != insufficient_code
    losses = values == loss_code
    evaluable_count = np.sum(evaluable, axis=0, dtype=np.uint16).astype(np.uint8)
    gap_count = np.sum(~evaluable, axis=0, dtype=np.uint16).astype(np.uint8)

    for row in range(height):
        for column in range(width):
            pixel_losses = losses[:, row, column]
            pixel_evaluable = evaluable[:, row, column]
            evaluable_indices = np.flatnonzero(pixel_evaluable)
            if evaluable_indices.size:
                last_evaluable_year[row, column] = years[int(evaluable_indices[-1])]
            loss_indices = np.flatnonzero(pixel_losses)
            if not loss_indices.size:
                if gap_count[row, column] > 0:
                    status[row, column] = int(
                        RFTerminalPersistenceStatus.INSUFFICIENT_DATA_WITHOUT_OBSERVED_LOSS
                    )
                continue

            first_index = int(loss_indices[0])
            first_loss_year[row, column] = years[first_index]
            after_first_loss = slice(first_index, len(years))
            observed_nonloss_after_loss = np.any(
                pixel_evaluable[after_first_loss] & ~pixel_losses[after_first_loss]
            )
            if observed_nonloss_after_loss:
                status[row, column] = int(RFTerminalPersistenceStatus.RECOVERED_AFTER_LOSS)
                continue
            if np.any(~pixel_evaluable[after_first_loss]):
                status[row, column] = int(RFTerminalPersistenceStatus.INSUFFICIENT_DATA_AFTER_LOSS)
                continue

            run_length = 0
            for is_loss in pixel_losses[::-1]:
                if not is_loss:
                    break
                run_length += 1
            terminal_run[row, column] = run_length
            status[row, column] = int(
                RFTerminalPersistenceStatus.TERMINAL_PERSISTENT_LOSS
                if run_length >= minimum_consecutive_loss_years
                else RFTerminalPersistenceStatus.INSUFFICIENT_LOSS_DURATION
            )

    terminal_mask = status == int(RFTerminalPersistenceStatus.TERMINAL_PERSISTENT_LOSS)
    recovered_mask = status == int(RFTerminalPersistenceStatus.RECOVERED_AFTER_LOSS)
    insufficient_mask = np.isin(
        status,
        (
            int(RFTerminalPersistenceStatus.INSUFFICIENT_DATA_AFTER_LOSS),
            int(RFTerminalPersistenceStatus.INSUFFICIENT_DATA_WITHOUT_OBSERVED_LOSS),
        ),
    )
    arrays = (
        status,
        terminal_mask,
        recovered_mask,
        insufficient_mask,
        first_loss_year,
        last_evaluable_year,
        terminal_run,
        evaluable_count,
        gap_count,
    )
    for array in arrays:
        array.setflags(write=False)
    return RFTerminalPersistenceResult(
        schema_version=RF_TERMINAL_PERSISTENCE_SCHEMA_VERSION,
        years=years,
        minimum_consecutive_loss_years=minimum_consecutive_loss_years,
        status=status,
        terminal_persistent_loss_mask=terminal_mask,
        recovered_loss_mask=recovered_mask,
        insufficient_data_mask=insufficient_mask,
        first_loss_year=first_loss_year,
        last_evaluable_year=last_evaluable_year,
        terminal_consecutive_loss_years=terminal_run,
        evaluable_year_count=evaluable_count,
        gap_year_count=gap_count,
    )


def _validate_inputs(
    *,
    transitions: NDArray[Any],
    years: tuple[int, ...],
    minimum_consecutive_loss_years: int,
) -> None:
    if (
        not years
        or any(year not in RF_TERMINAL_PERSISTENCE_TRANSFER_YEARS for year in years)
        or any(current != previous + 1 for previous, current in pairwise(years))
    ):
        raise ValueError("rf_terminal_persistence_years_invalid")
    if transitions.ndim != 3 or transitions.shape[0] != len(years):
        raise ValueError("rf_terminal_persistence_transition_cube_shape_invalid")
    if minimum_consecutive_loss_years < 2 or minimum_consecutive_loss_years > len(years):
        raise ValueError("rf_terminal_persistence_minimum_consecutive_years_invalid")
    valid_codes = np.asarray([int(code) for code in ForestTransitionCode])
    if not np.isin(transitions, valid_codes).all():
        raise ValueError("rf_terminal_persistence_transition_code_invalid")
