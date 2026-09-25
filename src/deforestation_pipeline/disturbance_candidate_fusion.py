"""Contrato histórico v1 de fusión robusto + RF, preservado para compatibilidad.

El DAG canónico ya no usa esta unión geométrica. La materialización v2 se
construye desde persistencia terminal RF y asocia robusto/CCDC como soporte.
"""

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
from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode
from deforestation_pipeline.schemas import RasterGridSpec

CANDIDATE_FUSION_SCHEMA_VERSION: Final = "1.0.0"
RF_TEMPORAL_TRANSFER_YEARS: Final = frozenset(range(2021, 2025))


class CandidateSource(StrEnum):
    """Fuentes capaces de proponer píxeles, sin implicar independencia."""

    ROBUST = "robust_persistent_candidate"
    RF_TEMPORAL = "rf_persistent_forest_loss"


class CandidateResolution(StrEnum):
    """Resolución inicial previa a persistencia y atribución de uso posterior."""

    CANDIDATE_ONLY = "candidate_only"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True)
class CandidateSubepisode:
    """Resumen temporal interno que no fragmenta el parche espacial."""

    onset_period_index: int
    pixel_count: int


@dataclass(frozen=True, slots=True)
class FusedDisturbanceCandidate:
    """Componente espacial deduplicado con procedencia por fuente."""

    candidate_id: str
    pixels: tuple[tuple[int, int], ...]
    pixel_count: int
    area_ha: float
    sources: tuple[CandidateSource, ...]
    resolution: CandidateResolution
    robust_pixel_count: int
    rf_persistent_loss_pixel_count: int
    robust_rf_overlap_pixel_count: int
    ccdc_support_pixel_count: int
    ccdc_break_day_offset_range: tuple[int, int] | None
    baseline_review_pixel_count: int
    robust_unknown_onset_pixel_count: int
    robust_onset_period_range: tuple[int, int] | None
    rf_persistent_loss_year_range: tuple[int, int] | None
    subepisodes: tuple[CandidateSubepisode, ...]
    rf_evidence_role: Literal["learned_supporting_evidence"]
    rf_calibrated_probability: Literal[False]
    rf_independent_evidence: Literal[False]
    ccdc_support_semantics: Literal["shared_hls_support_or_date_never_veto"]
    automatic_promotion_allowed: Literal[False]


@dataclass(frozen=True, slots=True)
class CandidateFusionResult:
    """Contrato versionado de la función pura de fusión."""

    schema_version: str
    candidate_count: int
    candidates: tuple[FusedDisturbanceCandidate, ...]
    fused_candidate_mask: NDArray[np.bool_]
    rf_persistent_loss_mask: NDArray[np.bool_]


def fuse_disturbance_candidates(
    *,
    robust_persistent_mask: NDArray[Any],
    robust_onset_period_index: NDArray[Any],
    rf_transition_cube: NDArray[Any],
    rf_years: tuple[int, ...],
    ccdc_break_mask: NDArray[Any],
    automated_forest_mask: NDArray[Any],
    baseline_review_mask: NDArray[Any],
    grid_spec: RasterGridSpec,
    rf_minimum_consecutive_loss_years: int = 2,
    ccdc_break_day_offset: NDArray[Any] | None = None,
) -> CandidateFusionResult:
    """Une candidatos por conectividad espacial y conserva el tiempo como atributos.

    El detector robusto y RF pueden proponer píxeles. RF requiere pérdida en
    años consecutivos y siempre conserva su rol no calibrado. CCDC sólo se
    cuenta como soporte: ni crea ni elimina candidatos.
    """
    shape = (grid_spec.height, grid_spec.width)
    robust = _bool_mask(robust_persistent_mask, shape=shape, name="robust_persistent_mask")
    ccdc = _bool_mask(ccdc_break_mask, shape=shape, name="ccdc_break_mask")
    automated = _bool_mask(automated_forest_mask, shape=shape, name="automated_forest_mask")
    review = _bool_mask(baseline_review_mask, shape=shape, name="baseline_review_mask")
    if np.any(automated & review):
        raise ValueError("candidate_fusion_baseline_domains_overlap")
    eligible_baseline = automated | review
    robust_candidate = robust & eligible_baseline
    ccdc_offsets = (
        np.full(shape, np.nan, dtype=np.float64)
        if ccdc_break_day_offset is None
        else np.asarray(ccdc_break_day_offset, dtype=np.float64)
    )
    if ccdc_offsets.shape != shape:
        raise ValueError("candidate_fusion_ccdc_break_day_offset_shape_mismatch")
    if np.isinf(ccdc_offsets).any():
        raise ValueError("candidate_fusion_ccdc_break_day_offset_contains_infinity")
    if np.any(np.isfinite(ccdc_offsets) & ~ccdc):
        raise ValueError("candidate_fusion_ccdc_break_date_without_support")
    onsets = np.asarray(robust_onset_period_index, dtype=np.float64)
    if onsets.shape != shape:
        raise ValueError("candidate_fusion_robust_onset_period_index_shape_mismatch")
    if np.isinf(onsets).any():
        raise ValueError("candidate_fusion_robust_onset_period_index_contains_infinity")
    candidate_onsets = onsets[robust_candidate]
    finite_onsets = candidate_onsets[np.isfinite(candidate_onsets)]
    if np.any(finite_onsets < 0) or np.any(finite_onsets != np.floor(finite_onsets)):
        raise ValueError("candidate_fusion_robust_onset_period_index_invalid")

    transitions = np.asarray(rf_transition_cube)
    expected_cube_shape = (len(rf_years), *shape)
    if transitions.shape != expected_cube_shape:
        raise ValueError("candidate_fusion_rf_transition_cube_shape_mismatch")
    if (
        not rf_years
        or rf_years != tuple(sorted(set(rf_years)))
        or any(year not in RF_TEMPORAL_TRANSFER_YEARS for year in rf_years)
    ):
        raise ValueError("candidate_fusion_rf_years_out_of_scope")
    if rf_minimum_consecutive_loss_years < 2:
        raise ValueError("candidate_fusion_rf_minimum_consecutive_loss_years_invalid")

    rf_persistent, rf_first_year, rf_last_year = _persistent_rf_loss(
        transitions=transitions,
        years=rf_years,
        minimum_consecutive_years=rf_minimum_consecutive_loss_years,
    )
    rf_candidate = rf_persistent & eligible_baseline
    fused = robust_candidate | rf_candidate
    components = connected_components_8(fused)
    pixel_area_ha = grid_pixel_area_ha(
        grid_spec,
        error_code="candidate_fusion_grid_pixel_area_invalid",
    )
    candidates: list[FusedDisturbanceCandidate] = []
    for pixels in components:
        component_mask = np.zeros(shape, dtype=np.bool_)
        rows, columns = zip(*pixels, strict=True)
        component_mask[np.asarray(rows), np.asarray(columns)] = True
        robust_component = component_mask & robust_candidate
        rf_component = component_mask & rf_candidate
        robust_values = onsets[robust_component]
        known_onsets = robust_values[np.isfinite(robust_values)].astype(np.int64)
        onset_range = (
            None if known_onsets.size == 0 else (int(known_onsets.min()), int(known_onsets.max()))
        )
        subepisodes = tuple(
            CandidateSubepisode(
                onset_period_index=int(value),
                pixel_count=int(np.count_nonzero(known_onsets == value)),
            )
            for value in sorted(set(known_onsets.tolist()))
        )
        rf_year_values = np.concatenate((rf_first_year[rf_component], rf_last_year[rf_component]))
        known_rf_years = rf_year_values[rf_year_values > 0]
        rf_year_range = (
            None
            if known_rf_years.size == 0
            else (int(known_rf_years.min()), int(known_rf_years.max()))
        )
        sources = tuple(
            source
            for source, present in (
                (CandidateSource.ROBUST, bool(robust_component.any())),
                (CandidateSource.RF_TEMPORAL, bool(rf_component.any())),
            )
            if present
        )
        rf_only_extension = rf_component & ~robust_component
        needs_review = bool((component_mask & review).any() or rf_only_extension.any())
        ccdc_values = ccdc_offsets[component_mask & ccdc]
        known_ccdc_offsets = ccdc_values[np.isfinite(ccdc_values)]
        ccdc_offset_range = (
            None
            if known_ccdc_offsets.size == 0
            else (
                int(np.floor(known_ccdc_offsets.min())),
                int(np.ceil(known_ccdc_offsets.max())),
            )
        )
        candidate = FusedDisturbanceCandidate(
            candidate_id=spatial_candidate_id(
                prefix="DFC",
                grid_sha256=grid_spec.grid_sha256,
                pixels=pixels,
            ),
            pixels=pixels,
            pixel_count=len(pixels),
            area_ha=round(len(pixels) * pixel_area_ha, 8),
            sources=sources,
            resolution=(
                CandidateResolution.REVIEW_REQUIRED
                if needs_review
                else CandidateResolution.CANDIDATE_ONLY
            ),
            robust_pixel_count=int(np.count_nonzero(robust_component)),
            rf_persistent_loss_pixel_count=int(np.count_nonzero(rf_component)),
            robust_rf_overlap_pixel_count=int(np.count_nonzero(robust_component & rf_component)),
            ccdc_support_pixel_count=int(np.count_nonzero(component_mask & ccdc)),
            ccdc_break_day_offset_range=ccdc_offset_range,
            baseline_review_pixel_count=int(np.count_nonzero(component_mask & review)),
            robust_unknown_onset_pixel_count=int(
                np.count_nonzero(robust_component & ~np.isfinite(onsets))
            ),
            robust_onset_period_range=onset_range,
            rf_persistent_loss_year_range=rf_year_range,
            subepisodes=subepisodes,
            rf_evidence_role="learned_supporting_evidence",
            rf_calibrated_probability=False,
            rf_independent_evidence=False,
            ccdc_support_semantics="shared_hls_support_or_date_never_veto",
            automatic_promotion_allowed=False,
        )
        candidates.append(candidate)
    ordered = tuple(sorted(candidates, key=lambda item: (-item.area_ha, item.candidate_id)))
    for array in (fused, rf_persistent):
        array.setflags(write=False)
    return CandidateFusionResult(
        schema_version=CANDIDATE_FUSION_SCHEMA_VERSION,
        candidate_count=len(ordered),
        candidates=ordered,
        fused_candidate_mask=fused,
        rf_persistent_loss_mask=rf_persistent,
    )


def _persistent_rf_loss(
    *,
    transitions: NDArray[Any],
    years: tuple[int, ...],
    minimum_consecutive_years: int,
) -> tuple[NDArray[np.bool_], NDArray[np.int16], NDArray[np.int16]]:
    loss = np.asarray(transitions) == int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)
    persistent = np.zeros(loss.shape[1:], dtype=np.bool_)
    run_length = np.zeros(loss.shape[1:], dtype=np.uint8)
    previous_year: int | None = None
    for index, year in enumerate(years):
        if previous_year is None or year != previous_year + 1:
            run_length.fill(0)
        run_length = np.where(loss[index], run_length + 1, 0).astype(np.uint8)
        persistent |= run_length >= minimum_consecutive_years
        previous_year = year
    first_year = np.zeros(loss.shape[1:], dtype=np.int16)
    last_year = np.zeros(loss.shape[1:], dtype=np.int16)
    for index, year in enumerate(years):
        selected = persistent & loss[index]
        first_year[selected & (first_year == 0)] = year
        last_year[selected] = year
    return persistent, first_year, last_year


def _bool_mask(values: NDArray[Any], *, shape: tuple[int, int], name: str) -> NDArray[np.bool_]:
    array = np.asarray(values)
    if array.shape != shape:
        raise ValueError(f"candidate_fusion_{name}_shape_mismatch")
    if array.dtype != np.bool_:
        raise ValueError(f"candidate_fusion_{name}_dtype_invalid")
    return np.asarray(array, dtype=np.bool_)
