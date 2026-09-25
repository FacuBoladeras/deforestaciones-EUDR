"""Asociación de soporte robusto y CCDC dentro de candidatos sembrados por RF."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal

import numpy as np
from numpy.typing import NDArray

from deforestation_pipeline.candidate_segmentation import grid_pixel_area_ha
from deforestation_pipeline.rf_seeded_candidate_domain import RFSeededCandidateDomainResult
from deforestation_pipeline.schemas import RasterGridSpec

RF_CANDIDATE_EVIDENCE_ASSOCIATION_SCHEMA_VERSION: Final = "1.0.0"


class CandidateSupportSource(StrEnum):
    """Fuentes que describen soporte sin proponer geometría."""

    ROBUST = "robust_persistent_support"
    CCDC = "ccdc_break_support"


@dataclass(frozen=True, slots=True)
class RFCandidateEvidenceAssociation:
    """Soporte localizado que nunca cambia el footprint candidato."""

    candidate_id: str
    candidate_pixel_count: int
    rf_seed_pixel_count: int
    robust_support_pixel_count: int
    robust_support_fraction: float
    robust_support_area_ha: float
    robust_unknown_onset_pixel_count: int
    robust_onset_period_range: tuple[int, int] | None
    ccdc_support_pixel_count: int
    ccdc_support_fraction: float
    ccdc_support_area_ha: float
    ccdc_unknown_date_pixel_count: int
    ccdc_break_day_offset_range: tuple[int, int] | None
    support_sources: tuple[CandidateSupportSource, ...]
    segmentation_source: Literal["rf_terminal_persistent_forest_loss"]
    robust_role: Literal["support_inside_rf_footprint_never_expands_geometry"]
    ccdc_role: Literal["shared_hls_support_or_date_never_veto"]
    shared_hls_support_is_independent: Literal[False]
    geometry_expansion_pixel_count: Literal[0]
    automatic_promotion_allowed: Literal[False]


@dataclass(frozen=True, slots=True)
class RFCandidateEvidenceAssociationResult:
    """Asociaciones por RFC y máscaras de soporte dentro/fuera del dominio."""

    schema_version: str
    candidate_count: int
    records: tuple[RFCandidateEvidenceAssociation, ...]
    candidate_mask: NDArray[np.bool_]
    robust_support_inside_candidate_mask: NDArray[np.bool_]
    ccdc_support_inside_candidate_mask: NDArray[np.bool_]
    ccdc_outside_candidate_mask: NDArray[np.bool_]
    robust_support_inside_candidate_pixel_count: int
    ccdc_support_inside_candidate_pixel_count: int
    ccdc_outside_candidate_pixel_count: int


def associate_rf_candidate_evidence(
    *,
    candidate_domain: RFSeededCandidateDomainResult,
    robust_persistent_mask: NDArray[Any],
    robust_onset_period_index: NDArray[Any],
    ccdc_break_mask: NDArray[Any],
    ccdc_break_day_offset: NDArray[Any],
    grid_spec: RasterGridSpec,
) -> RFCandidateEvidenceAssociationResult:
    """Asocia soporte y fecha sin permitir propuesta, expansión ni veto."""
    shape = (grid_spec.height, grid_spec.width)
    candidate_mask = _validate_candidate_domain(candidate_domain, shape=shape)
    robust = _bool_mask(robust_persistent_mask, shape=shape, name="robust_persistent_mask")
    ccdc = _bool_mask(ccdc_break_mask, shape=shape, name="ccdc_break_mask")
    robust_onsets = np.asarray(robust_onset_period_index, dtype=np.float64)
    if robust_onsets.shape != shape:
        raise ValueError("rf_candidate_evidence_robust_onset_shape")
    ccdc_offsets = np.asarray(ccdc_break_day_offset, dtype=np.float64)
    if ccdc_offsets.shape != shape:
        raise ValueError("rf_candidate_evidence_ccdc_break_day_offset_shape")
    if np.isinf(ccdc_offsets).any():
        raise ValueError("rf_candidate_evidence_ccdc_break_day_offset_infinite")
    if np.any(np.isfinite(ccdc_offsets) & ~ccdc):
        raise ValueError("rf_candidate_evidence_ccdc_date_without_support")

    robust_inside = robust & candidate_mask
    ccdc_inside = ccdc & candidate_mask
    ccdc_outside = ccdc & ~candidate_mask
    inside_onsets = robust_onsets[robust_inside]
    finite_inside_onsets = inside_onsets[np.isfinite(inside_onsets)]
    if (
        np.isinf(inside_onsets).any()
        or np.any(finite_inside_onsets < 0)
        or np.any(finite_inside_onsets != np.floor(finite_inside_onsets))
    ):
        raise ValueError("rf_candidate_evidence_robust_onset_invalid")

    pixel_area_ha = grid_pixel_area_ha(
        grid_spec,
        error_code="rf_candidate_evidence_grid_pixel_area_invalid",
    )
    records: list[RFCandidateEvidenceAssociation] = []
    for candidate in candidate_domain.candidates:
        rows, columns = zip(*candidate.pixels, strict=True)
        indexes = (np.asarray(rows), np.asarray(columns))
        robust_values = robust[indexes]
        ccdc_values = ccdc[indexes]
        robust_count = int(np.count_nonzero(robust_values))
        ccdc_count = int(np.count_nonzero(ccdc_values))
        candidate_onsets = robust_onsets[indexes][robust_values]
        known_onsets = candidate_onsets[np.isfinite(candidate_onsets)].astype(np.int64)
        candidate_ccdc_offsets = ccdc_offsets[indexes][ccdc_values]
        known_ccdc_offsets = candidate_ccdc_offsets[np.isfinite(candidate_ccdc_offsets)]
        sources = tuple(
            source
            for source, present in (
                (CandidateSupportSource.ROBUST, robust_count > 0),
                (CandidateSupportSource.CCDC, ccdc_count > 0),
            )
            if present
        )
        records.append(
            RFCandidateEvidenceAssociation(
                candidate_id=candidate.candidate_id,
                candidate_pixel_count=candidate.pixel_count,
                rf_seed_pixel_count=candidate.pixel_count,
                robust_support_pixel_count=robust_count,
                robust_support_fraction=round(robust_count / candidate.pixel_count, 8),
                robust_support_area_ha=round(robust_count * pixel_area_ha, 8),
                robust_unknown_onset_pixel_count=int(
                    np.count_nonzero(~np.isfinite(candidate_onsets))
                ),
                robust_onset_period_range=(
                    None
                    if known_onsets.size == 0
                    else (int(known_onsets.min()), int(known_onsets.max()))
                ),
                ccdc_support_pixel_count=ccdc_count,
                ccdc_support_fraction=round(ccdc_count / candidate.pixel_count, 8),
                ccdc_support_area_ha=round(ccdc_count * pixel_area_ha, 8),
                ccdc_unknown_date_pixel_count=int(
                    np.count_nonzero(~np.isfinite(candidate_ccdc_offsets))
                ),
                ccdc_break_day_offset_range=(
                    None
                    if known_ccdc_offsets.size == 0
                    else (
                        int(np.floor(known_ccdc_offsets.min())),
                        int(np.ceil(known_ccdc_offsets.max())),
                    )
                ),
                support_sources=sources,
                segmentation_source="rf_terminal_persistent_forest_loss",
                robust_role="support_inside_rf_footprint_never_expands_geometry",
                ccdc_role="shared_hls_support_or_date_never_veto",
                shared_hls_support_is_independent=False,
                geometry_expansion_pixel_count=0,
                automatic_promotion_allowed=False,
            )
        )
    for mask in (candidate_mask, robust_inside, ccdc_inside, ccdc_outside):
        mask.setflags(write=False)
    return RFCandidateEvidenceAssociationResult(
        schema_version=RF_CANDIDATE_EVIDENCE_ASSOCIATION_SCHEMA_VERSION,
        candidate_count=len(records),
        records=tuple(records),
        candidate_mask=candidate_mask,
        robust_support_inside_candidate_mask=robust_inside,
        ccdc_support_inside_candidate_mask=ccdc_inside,
        ccdc_outside_candidate_mask=ccdc_outside,
        robust_support_inside_candidate_pixel_count=int(np.count_nonzero(robust_inside)),
        ccdc_support_inside_candidate_pixel_count=int(np.count_nonzero(ccdc_inside)),
        ccdc_outside_candidate_pixel_count=int(np.count_nonzero(ccdc_outside)),
    )


def _validate_candidate_domain(
    candidate_domain: RFSeededCandidateDomainResult,
    *,
    shape: tuple[int, int],
) -> NDArray[np.bool_]:
    candidate_mask = _bool_mask(candidate_domain.candidate_mask, shape=shape, name="candidate_mask")
    reconstructed = np.zeros(shape, dtype=np.bool_)
    candidate_ids: set[str] = set()
    for candidate in candidate_domain.candidates:
        if candidate.candidate_id in candidate_ids or candidate.pixel_count != len(
            candidate.pixels
        ):
            raise ValueError("rf_candidate_evidence_candidate_domain_invalid")
        candidate_ids.add(candidate.candidate_id)
        for row, column in candidate.pixels:
            if not (0 <= row < shape[0] and 0 <= column < shape[1]) or reconstructed[row, column]:
                raise ValueError("rf_candidate_evidence_candidate_domain_invalid")
            reconstructed[row, column] = True
    if candidate_domain.candidate_count != len(candidate_domain.candidates) or not np.array_equal(
        reconstructed, candidate_mask
    ):
        raise ValueError("rf_candidate_evidence_candidate_domain_invalid")
    return candidate_mask.copy()


def _bool_mask(array: NDArray[Any], *, shape: tuple[int, int], name: str) -> NDArray[np.bool_]:
    values = np.asarray(array)
    if values.shape != shape:
        raise ValueError(f"rf_candidate_evidence_{name}_shape")
    if values.dtype != np.bool_:
        raise ValueError(f"rf_candidate_evidence_{name}_dtype")
    return values.astype(np.bool_, copy=False)
