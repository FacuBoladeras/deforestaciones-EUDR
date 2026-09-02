from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from deforestation_pipeline.rf_seeded_candidate_domain import CandidateBaselineDomain
from deforestation_pipeline.robust_shadow_inventory import (
    ROBUST_SHADOW_INVENTORY_SCHEMA_VERSION,
    build_robust_shadow_inventory,
)
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256


def _grid(*, width: int, height: int) -> RasterGridSpec:
    identity: dict[str, Any] = {
        "target_crs": "EPSG:32721",
        "resolution_m": 30.0,
        "width": width,
        "height": height,
        "transform": (30.0, 0.0, 500000.0, 0.0, -30.0, 6200000.0),
        "bounds": (
            500000.0,
            6200000.0 - 30.0 * height,
            500000.0 + 30.0 * width,
            6200000.0,
        ),
        "alignment_strategy": "projected_crs_origin_outward_snap",
        "pixel_orientation": "north_up",
    }
    return RasterGridSpec(
        **identity,
        nodata=-9999.0,
        aoi_mask_required=True,
        grid_sha256=raster_grid_sha256(**identity),
    )


def test_preserves_robust_only_patches_without_expanding_rf_candidate_domain() -> None:
    shape = (2, 6)
    rf_candidates = np.zeros(shape, dtype=np.bool_)
    rf_candidates[0, 0] = True
    rf_candidates[1, 1] = True
    robust = np.zeros(shape, dtype=np.bool_)
    robust[0, 0] = True  # soporte dentro de RFC; no es shadow
    robust[0, 1] = True  # shadow adyacente; no expande RFC
    robust[0, 4] = True
    robust[1, 5] = True  # conectado diagonalmente con (0, 4)
    robust[1, 3] = True  # fuera de baseline elegible
    onset = np.full(shape, np.nan)
    onset[0, 0] = 1.0
    onset[0, 1] = 2.0
    onset[1, 5] = 4.0
    onset[1, 3] = 3.0
    automated = np.zeros(shape, dtype=np.bool_)
    automated[0, 0] = True
    automated[1, 1] = True
    automated[0, 1] = True
    automated[0, 4] = True
    review = np.zeros(shape, dtype=np.bool_)
    review[1, 5] = True
    original_candidates = rf_candidates.copy()

    result = build_robust_shadow_inventory(
        robust_persistent_mask=robust,
        robust_onset_period_index=onset,
        rf_candidate_mask=rf_candidates,
        automated_forest_mask=automated,
        baseline_review_mask=review,
        grid_spec=_grid(width=6, height=2),
    )

    assert result.schema_version == ROBUST_SHADOW_INVENTORY_SCHEMA_VERSION
    assert result.alert_count == 2
    assert result.shadow_mask.tolist() == [
        [False, True, False, False, True, False],
        [False, False, False, False, False, True],
    ]
    assert result.robust_inside_rf_candidate_mask.tolist() == [
        [True, False, False, False, False, False],
        [False, False, False, False, False, False],
    ]
    assert result.ineligible_robust_mask[1, 3]
    assert result.shadow_pixel_count == 3
    assert result.robust_inside_rf_candidate_pixel_count == 1
    assert result.ineligible_robust_pixel_count == 1
    connected, isolated = result.alerts
    assert connected.pixel_count == 2
    assert connected.area_ha == 0.18
    assert connected.baseline_domain == CandidateBaselineDomain.MIXED
    assert connected.robust_known_onset_pixel_count == 1
    assert connected.robust_unknown_onset_pixel_count == 1
    assert connected.robust_onset_period_range == (4, 4)
    assert isolated.pixel_count == 1
    assert isolated.area_ha == 0.09
    assert isolated.robust_onset_period_range == (2, 2)
    assert connected.can_seed_primary_candidate is False
    assert connected.automatic_promotion_allowed is False
    assert np.array_equal(rf_candidates, original_candidates)


def test_rejects_invalid_shadow_onset_but_ignores_onset_inside_primary_candidate() -> None:
    shape = (1, 2)
    robust = np.ones(shape, dtype=np.bool_)
    rf_candidates = np.asarray([[True, False]])
    onset = np.asarray([[-9999.0, -9999.0]])

    with pytest.raises(ValueError, match="robust_shadow_onset_period_index_invalid"):
        build_robust_shadow_inventory(
            robust_persistent_mask=robust,
            robust_onset_period_index=onset,
            rf_candidate_mask=rf_candidates,
            automated_forest_mask=np.ones(shape, dtype=np.bool_),
            baseline_review_mask=np.zeros(shape, dtype=np.bool_),
            grid_spec=_grid(width=2, height=1),
        )

    result = build_robust_shadow_inventory(
        robust_persistent_mask=np.asarray([[True, False]]),
        robust_onset_period_index=onset,
        rf_candidate_mask=rf_candidates,
        automated_forest_mask=np.ones(shape, dtype=np.bool_),
        baseline_review_mask=np.zeros(shape, dtype=np.bool_),
        grid_spec=_grid(width=2, height=1),
    )
    assert result.alert_count == 0


def test_rejects_candidate_pixels_outside_eligible_baseline() -> None:
    shape = (1, 1)
    with pytest.raises(ValueError, match="robust_shadow_rf_candidate_outside_eligible_baseline"):
        build_robust_shadow_inventory(
            robust_persistent_mask=np.zeros(shape, dtype=np.bool_),
            robust_onset_period_index=np.full(shape, np.nan),
            rf_candidate_mask=np.ones(shape, dtype=np.bool_),
            automated_forest_mask=np.zeros(shape, dtype=np.bool_),
            baseline_review_mask=np.zeros(shape, dtype=np.bool_),
            grid_spec=_grid(width=1, height=1),
        )
