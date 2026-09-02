"""Inventario sombra de señales robustas fuera del dominio candidato RF."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np
from numpy.typing import NDArray

from deforestation_pipeline.candidate_segmentation import (
    connected_components_8,
    grid_pixel_area_ha,
    spatial_candidate_id,
)
from deforestation_pipeline.rf_seeded_candidate_domain import CandidateBaselineDomain
from deforestation_pipeline.schemas import RasterGridSpec

ROBUST_SHADOW_INVENTORY_SCHEMA_VERSION: Final = "1.0.0"


@dataclass(frozen=True, slots=True)
class RobustShadowAlert:
    """Parche robusto auditable que no pertenece al dominio candidato primario."""

    alert_id: str
    pixels: tuple[tuple[int, int], ...]
    pixel_count: int
    area_ha: float
    baseline_domain: CandidateBaselineDomain
    automated_forest_pixel_count: int
    baseline_review_pixel_count: int
    robust_known_onset_pixel_count: int
    robust_unknown_onset_pixel_count: int
    robust_onset_period_range: tuple[int, int] | None
    reason: Literal["robust_persistent_outside_rf_seeded_candidate_domain"]
    disposition: Literal["review_only_shadow_inventory"]
    segmentation_semantics: Literal["spatial_8_connected_no_gap_filling"]
    can_seed_primary_candidate: Literal[False]
    automatic_promotion_allowed: Literal[False]


@dataclass(frozen=True, slots=True)
class RobustShadowInventoryResult:
    """Alertas sombra y partición completa de los píxeles robustos."""

    schema_version: str
    alert_count: int
    alerts: tuple[RobustShadowAlert, ...]
    shadow_mask: NDArray[np.bool_]
    robust_inside_rf_candidate_mask: NDArray[np.bool_]
    ineligible_robust_mask: NDArray[np.bool_]
    shadow_pixel_count: int
    robust_inside_rf_candidate_pixel_count: int
    ineligible_robust_pixel_count: int


def build_robust_shadow_inventory(
    *,
    robust_persistent_mask: NDArray[Any],
    robust_onset_period_index: NDArray[Any],
    rf_candidate_mask: NDArray[Any],
    automated_forest_mask: NDArray[Any],
    baseline_review_mask: NDArray[Any],
    grid_spec: RasterGridSpec,
) -> RobustShadowInventoryResult:
    """Particiona robusto sin permitir que expanda el dominio RF primario."""
    shape = (grid_spec.height, grid_spec.width)
    robust = _bool_mask(robust_persistent_mask, shape=shape, name="robust_persistent_mask")
    rf_candidates = _bool_mask(rf_candidate_mask, shape=shape, name="rf_candidate_mask")
    automated = _bool_mask(automated_forest_mask, shape=shape, name="automated_forest_mask")
    review = _bool_mask(baseline_review_mask, shape=shape, name="baseline_review_mask")
    if np.any(automated & review):
        raise ValueError("robust_shadow_baseline_domains_overlap")
    eligible = automated | review
    if np.any(rf_candidates & ~eligible):
        raise ValueError("robust_shadow_rf_candidate_outside_eligible_baseline")

    onsets = np.asarray(robust_onset_period_index, dtype=np.float64)
    if onsets.shape != shape:
        raise ValueError("robust_shadow_onset_period_index_shape")
    shadow = robust & eligible & ~rf_candidates
    inside = robust & rf_candidates
    ineligible = robust & ~eligible
    shadow_onsets = onsets[shadow]
    finite_shadow_onsets = shadow_onsets[np.isfinite(shadow_onsets)]
    if (
        np.isinf(shadow_onsets).any()
        or np.any(finite_shadow_onsets < 0)
        or np.any(finite_shadow_onsets != np.floor(finite_shadow_onsets))
    ):
        raise ValueError("robust_shadow_onset_period_index_invalid")

    pixel_area_ha = grid_pixel_area_ha(
        grid_spec,
        error_code="robust_shadow_grid_pixel_area_invalid",
    )
    alerts: list[RobustShadowAlert] = []
    for pixels in connected_components_8(shadow):
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
        component_onsets = onsets[indexes]
        known_onsets = component_onsets[np.isfinite(component_onsets)].astype(np.int64)
        onset_range = (
            None if known_onsets.size == 0 else (int(known_onsets.min()), int(known_onsets.max()))
        )
        alerts.append(
            RobustShadowAlert(
                alert_id=spatial_candidate_id(
                    prefix="RSA",
                    grid_sha256=grid_spec.grid_sha256,
                    pixels=pixels,
                ),
                pixels=pixels,
                pixel_count=len(pixels),
                area_ha=round(len(pixels) * pixel_area_ha, 8),
                baseline_domain=baseline_domain,
                automated_forest_pixel_count=automated_count,
                baseline_review_pixel_count=review_count,
                robust_known_onset_pixel_count=int(known_onsets.size),
                robust_unknown_onset_pixel_count=int(
                    np.count_nonzero(~np.isfinite(component_onsets))
                ),
                robust_onset_period_range=onset_range,
                reason="robust_persistent_outside_rf_seeded_candidate_domain",
                disposition="review_only_shadow_inventory",
                segmentation_semantics="spatial_8_connected_no_gap_filling",
                can_seed_primary_candidate=False,
                automatic_promotion_allowed=False,
            )
        )
    ordered = tuple(sorted(alerts, key=lambda item: (-item.area_ha, item.alert_id)))
    for mask in (shadow, inside, ineligible):
        mask.setflags(write=False)
    return RobustShadowInventoryResult(
        schema_version=ROBUST_SHADOW_INVENTORY_SCHEMA_VERSION,
        alert_count=len(ordered),
        alerts=ordered,
        shadow_mask=shadow,
        robust_inside_rf_candidate_mask=inside,
        ineligible_robust_mask=ineligible,
        shadow_pixel_count=int(np.count_nonzero(shadow)),
        robust_inside_rf_candidate_pixel_count=int(np.count_nonzero(inside)),
        ineligible_robust_pixel_count=int(np.count_nonzero(ineligible)),
    )


def _bool_mask(array: NDArray[Any], *, shape: tuple[int, int], name: str) -> NDArray[np.bool_]:
    values = np.asarray(array)
    if values.shape != shape:
        raise ValueError(f"robust_shadow_{name}_shape")
    if values.dtype != np.bool_:
        raise ValueError(f"robust_shadow_{name}_dtype")
    return values.astype(np.bool_, copy=False)
