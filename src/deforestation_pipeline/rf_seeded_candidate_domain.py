"""Segmentación pura del dominio candidato sembrado por persistencia RF terminal."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal

import numpy as np
from numpy.typing import NDArray

from deforestation_pipeline.candidate_segmentation import (
    connected_components_8,
    grid_pixel_area_ha,
    spatial_candidate_id,
)
from deforestation_pipeline.rf_terminal_persistence import RFTerminalPersistenceResult
from deforestation_pipeline.schemas import RasterGridSpec

RF_SEEDED_CANDIDATE_DOMAIN_SCHEMA_VERSION: Final = "1.0.0"


class CandidateBaselineDomain(StrEnum):
    """Procedencia del bosque elegible contenido por el parche."""

    AUTOMATED_FOREST = "automated_forest"
    BASELINE_REVIEW = "baseline_review"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class RFSeededCandidate:
    """Parche espacial cuya única semilla es pérdida RF terminal persistente."""

    candidate_id: str
    pixels: tuple[tuple[int, int], ...]
    pixel_count: int
    area_ha: float
    seed_source: Literal["rf_terminal_persistent_forest_loss"]
    baseline_domain: CandidateBaselineDomain
    automated_forest_pixel_count: int
    baseline_review_pixel_count: int
    first_loss_year_range: tuple[int, int]
    last_evaluable_year_range: tuple[int, int]
    terminal_consecutive_loss_year_range: tuple[int, int]
    evaluable_year_count_range: tuple[int, int]
    gap_year_count_range: tuple[int, int]
    segmentation_semantics: Literal["spatial_8_connected_no_gap_filling"]
    rf_evidence_role: Literal["learned_candidate_segmentation"]
    rf_calibrated_probability: Literal[False]
    rf_independent_evidence: Literal[False]
    automatic_promotion_allowed: Literal[False]


@dataclass(frozen=True, slots=True)
class RFSeededCandidateDomainResult:
    """Inventario primario y pérdidas terminales excluidas por baseline."""

    schema_version: str
    candidate_count: int
    candidates: tuple[RFSeededCandidate, ...]
    candidate_mask: NDArray[np.bool_]
    ineligible_terminal_loss_mask: NDArray[np.bool_]
    ineligible_terminal_loss_pixel_count: int


def segment_rf_seeded_candidate_domain(
    *,
    persistence: RFTerminalPersistenceResult,
    automated_forest_mask: NDArray[Any],
    baseline_review_mask: NDArray[Any],
    grid_spec: RasterGridSpec,
) -> RFSeededCandidateDomainResult:
    """Segmenta sólo pérdida terminal RF sobre bosque elegible al corte."""
    shape = (grid_spec.height, grid_spec.width)
    terminal = _bool_mask(
        persistence.terminal_persistent_loss_mask,
        shape=shape,
        name="terminal_persistent_loss_mask",
    )
    automated = _bool_mask(
        automated_forest_mask,
        shape=shape,
        name="automated_forest_mask",
    )
    review = _bool_mask(
        baseline_review_mask,
        shape=shape,
        name="baseline_review_mask",
    )
    if np.any(automated & review):
        raise ValueError("rf_seeded_candidate_baseline_domains_overlap")
    metadata: dict[str, NDArray[Any]] = {
        "first_loss_year": persistence.first_loss_year,
        "last_evaluable_year": persistence.last_evaluable_year,
        "terminal_consecutive_loss_years": persistence.terminal_consecutive_loss_years,
        "evaluable_year_count": persistence.evaluable_year_count,
        "gap_year_count": persistence.gap_year_count,
    }
    for name, values in metadata.items():
        if np.asarray(values).shape != shape:
            raise ValueError(f"rf_seeded_candidate_{name}_shape")

    eligible = automated | review
    candidate_mask = terminal & eligible
    ineligible = terminal & ~eligible
    pixel_area_ha = grid_pixel_area_ha(
        grid_spec,
        error_code="rf_seeded_candidate_grid_pixel_area_invalid",
    )
    candidates: list[RFSeededCandidate] = []
    for pixels in connected_components_8(candidate_mask):
        rows, columns = zip(*pixels, strict=True)
        indexes = (np.asarray(rows), np.asarray(columns))
        automated_count = int(np.count_nonzero(automated[indexes]))
        review_count = int(np.count_nonzero(review[indexes]))
        baseline_domain = (
            CandidateBaselineDomain.MIXED
            if automated_count and review_count
            else CandidateBaselineDomain.AUTOMATED_FOREST
            if automated_count
            else CandidateBaselineDomain.BASELINE_REVIEW
        )
        candidates.append(
            RFSeededCandidate(
                candidate_id=spatial_candidate_id(
                    prefix="RFC",
                    grid_sha256=grid_spec.grid_sha256,
                    pixels=pixels,
                ),
                pixels=pixels,
                pixel_count=len(pixels),
                area_ha=round(len(pixels) * pixel_area_ha, 8),
                seed_source="rf_terminal_persistent_forest_loss",
                baseline_domain=baseline_domain,
                automated_forest_pixel_count=automated_count,
                baseline_review_pixel_count=review_count,
                first_loss_year_range=_range(metadata["first_loss_year"][indexes]),
                last_evaluable_year_range=_range(metadata["last_evaluable_year"][indexes]),
                terminal_consecutive_loss_year_range=_range(
                    metadata["terminal_consecutive_loss_years"][indexes]
                ),
                evaluable_year_count_range=_range(metadata["evaluable_year_count"][indexes]),
                gap_year_count_range=_range(metadata["gap_year_count"][indexes], allow_zero=True),
                segmentation_semantics="spatial_8_connected_no_gap_filling",
                rf_evidence_role="learned_candidate_segmentation",
                rf_calibrated_probability=False,
                rf_independent_evidence=False,
                automatic_promotion_allowed=False,
            )
        )
    ordered = tuple(sorted(candidates, key=lambda item: (-item.area_ha, item.candidate_id)))
    candidate_mask.setflags(write=False)
    ineligible.setflags(write=False)
    return RFSeededCandidateDomainResult(
        schema_version=RF_SEEDED_CANDIDATE_DOMAIN_SCHEMA_VERSION,
        candidate_count=len(ordered),
        candidates=ordered,
        candidate_mask=candidate_mask,
        ineligible_terminal_loss_mask=ineligible,
        ineligible_terminal_loss_pixel_count=int(np.count_nonzero(ineligible)),
    )


def _bool_mask(array: NDArray[Any], *, shape: tuple[int, int], name: str) -> NDArray[np.bool_]:
    values = np.asarray(array)
    if values.shape != shape:
        raise ValueError(f"rf_seeded_candidate_{name}_shape")
    if not np.issubdtype(values.dtype, np.bool_):
        raise ValueError(f"rf_seeded_candidate_{name}_dtype")
    return values.astype(np.bool_, copy=False)


def _range(values: NDArray[Any], *, allow_zero: bool = False) -> tuple[int, int]:
    integers = np.asarray(values, dtype=np.int64)
    if integers.size == 0 or (not allow_zero and np.any(integers <= 0)) or np.any(integers < 0):
        raise ValueError("rf_seeded_candidate_metadata_invalid")
    return int(integers.min()), int(integers.max())
