from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode
from deforestation_pipeline.rf_candidate_evidence_association import (
    RF_CANDIDATE_EVIDENCE_ASSOCIATION_SCHEMA_VERSION,
    CandidateSupportSource,
    associate_rf_candidate_evidence,
)
from deforestation_pipeline.rf_seeded_candidate_domain import (
    RFSeededCandidateDomainResult,
    segment_rf_seeded_candidate_domain,
)
from deforestation_pipeline.rf_terminal_persistence import classify_rf_terminal_persistence
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


def _domain(*, grid: RasterGridSpec) -> RFSeededCandidateDomainResult:
    forest = ForestTransitionCode.STABLE_FOREST
    loss = ForestTransitionCode.CANDIDATE_FOREST_LOSS
    trajectories = [[forest, forest, forest, forest] for _ in range(grid.width * grid.height)]
    trajectories[0] = [loss, loss, loss, loss]
    trajectories[1] = [forest, loss, loss, loss]
    trajectories[4] = [forest, forest, loss, loss]
    persistence = classify_rf_terminal_persistence(
        transitions=np.asarray(trajectories, dtype=np.uint8).T.reshape(4, grid.height, grid.width),
        years=(2021, 2022, 2023, 2024),
        minimum_consecutive_loss_years=2,
    )
    return segment_rf_seeded_candidate_domain(
        persistence=persistence,
        automated_forest_mask=np.ones((grid.height, grid.width), dtype=np.bool_),
        baseline_review_mask=np.zeros((grid.height, grid.width), dtype=np.bool_),
        grid_spec=grid,
    )


def test_associates_robust_and_ccdc_inside_rf_candidates_without_changing_footprints() -> None:
    grid = _grid(width=5, height=1)
    domain = _domain(grid=grid)
    original_candidate_mask = domain.candidate_mask.copy()
    robust = np.asarray([[True, False, False, True, False]])
    robust_onset = np.asarray([[2.0, np.nan, np.nan, 7.0, np.nan]])
    ccdc = np.asarray([[False, True, False, True, False]])
    ccdc_offset = np.asarray([[np.nan, 120.0, np.nan, 300.0, np.nan]])

    result = associate_rf_candidate_evidence(
        candidate_domain=domain,
        robust_persistent_mask=robust,
        robust_onset_period_index=robust_onset,
        ccdc_break_mask=ccdc,
        ccdc_break_day_offset=ccdc_offset,
        grid_spec=grid,
    )

    assert result.schema_version == RF_CANDIDATE_EVIDENCE_ASSOCIATION_SCHEMA_VERSION
    assert result.candidate_count == domain.candidate_count == 2
    assert tuple(record.candidate_id for record in result.records) == tuple(
        candidate.candidate_id for candidate in domain.candidates
    )
    connected, isolated = result.records
    assert connected.candidate_pixel_count == 2
    assert connected.rf_seed_pixel_count == 2
    assert connected.robust_support_pixel_count == 1
    assert connected.robust_support_fraction == 0.5
    assert connected.robust_support_area_ha == 0.09
    assert connected.robust_onset_period_range == (2, 2)
    assert connected.ccdc_support_pixel_count == 1
    assert connected.ccdc_support_fraction == 0.5
    assert connected.ccdc_support_area_ha == 0.09
    assert connected.ccdc_break_day_offset_range == (120, 120)
    assert connected.support_sources == (
        CandidateSupportSource.ROBUST,
        CandidateSupportSource.CCDC,
    )
    assert isolated.robust_support_pixel_count == 0
    assert isolated.ccdc_support_pixel_count == 0
    assert isolated.support_sources == ()
    assert isolated.ccdc_role == "shared_hls_support_or_date_never_veto"
    assert isolated.geometry_expansion_pixel_count == 0
    assert isolated.automatic_promotion_allowed is False
    assert result.robust_support_inside_candidate_mask.tolist() == [
        [True, False, False, False, False]
    ]
    assert result.ccdc_support_inside_candidate_mask.tolist() == [
        [False, True, False, False, False]
    ]
    assert result.ccdc_outside_candidate_mask.tolist() == [[False, False, False, True, False]]
    assert np.array_equal(domain.candidate_mask, original_candidate_mask)


def test_ccdc_outside_rf_domain_never_creates_or_vetoes_candidate() -> None:
    grid = _grid(width=5, height=1)
    domain = _domain(grid=grid)
    result = associate_rf_candidate_evidence(
        candidate_domain=domain,
        robust_persistent_mask=np.zeros((1, 5), dtype=np.bool_),
        robust_onset_period_index=np.full((1, 5), np.nan),
        ccdc_break_mask=np.asarray([[False, False, True, True, False]]),
        ccdc_break_day_offset=np.asarray([[np.nan, np.nan, 200.0, 300.0, np.nan]]),
        grid_spec=grid,
    )

    assert result.candidate_count == 2
    assert all(record.ccdc_support_pixel_count == 0 for record in result.records)
    assert result.ccdc_outside_candidate_pixel_count == 2
    assert np.array_equal(result.candidate_mask, domain.candidate_mask)


def test_rejects_invalid_support_metadata_and_inconsistent_candidate_domain() -> None:
    grid = _grid(width=5, height=1)
    domain = _domain(grid=grid)
    shape = (1, 5)
    with pytest.raises(ValueError, match="rf_candidate_evidence_ccdc_date_without_support"):
        associate_rf_candidate_evidence(
            candidate_domain=domain,
            robust_persistent_mask=np.zeros(shape, dtype=np.bool_),
            robust_onset_period_index=np.full(shape, np.nan),
            ccdc_break_mask=np.zeros(shape, dtype=np.bool_),
            ccdc_break_day_offset=np.asarray([[100.0, np.nan, np.nan, np.nan, np.nan]]),
            grid_spec=grid,
        )

    with pytest.raises(ValueError, match="rf_candidate_evidence_robust_onset_invalid"):
        associate_rf_candidate_evidence(
            candidate_domain=domain,
            robust_persistent_mask=np.asarray([[True, False, False, False, False]]),
            robust_onset_period_index=np.asarray([[-1.0, np.nan, np.nan, np.nan, np.nan]]),
            ccdc_break_mask=np.zeros(shape, dtype=np.bool_),
            ccdc_break_day_offset=np.full(shape, np.nan),
            grid_spec=grid,
        )
