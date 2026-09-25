from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode
from deforestation_pipeline.rf_seeded_candidate_domain import (
    RF_SEEDED_CANDIDATE_DOMAIN_SCHEMA_VERSION,
    CandidateBaselineDomain,
    segment_rf_seeded_candidate_domain,
)
from deforestation_pipeline.rf_terminal_persistence import (
    RFTerminalPersistenceResult,
    classify_rf_terminal_persistence,
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


def _persistence(
    trajectories: list[list[ForestTransitionCode]], *, width: int, height: int
) -> RFTerminalPersistenceResult:
    cube = np.asarray(trajectories, dtype=np.uint8).T.reshape(4, height, width)
    return classify_rf_terminal_persistence(
        transitions=cube,
        years=(2021, 2022, 2023, 2024),
        minimum_consecutive_loss_years=2,
    )


def test_segments_only_terminal_rf_loss_with_eight_connectivity_and_no_area_filter() -> None:
    forest = ForestTransitionCode.STABLE_FOREST
    loss = ForestTransitionCode.CANDIDATE_FOREST_LOSS
    trajectories = [[forest, forest, forest, forest] for _ in range(10)]
    trajectories[0] = [loss, loss, loss, loss]
    trajectories[4] = [forest, forest, loss, loss]
    trajectories[6] = [forest, loss, loss, loss]
    persistence = _persistence(trajectories, width=5, height=2)
    automated = np.zeros((2, 5), dtype=np.bool_)
    automated[0, 0] = True
    automated[0, 4] = True
    review = np.zeros((2, 5), dtype=np.bool_)
    review[1, 1] = True

    result = segment_rf_seeded_candidate_domain(
        persistence=persistence,
        automated_forest_mask=automated,
        baseline_review_mask=review,
        grid_spec=_grid(width=5, height=2),
    )

    assert result.schema_version == RF_SEEDED_CANDIDATE_DOMAIN_SCHEMA_VERSION
    assert result.candidate_count == 2
    assert result.candidate_mask.tolist() == [
        [True, False, False, False, True],
        [False, True, False, False, False],
    ]
    connected, isolated = result.candidates
    assert connected.pixel_count == 2
    assert connected.area_ha == 0.18
    assert connected.baseline_domain == CandidateBaselineDomain.MIXED
    assert connected.automated_forest_pixel_count == 1
    assert connected.baseline_review_pixel_count == 1
    assert connected.first_loss_year_range == (2021, 2022)
    assert connected.terminal_consecutive_loss_year_range == (3, 4)
    assert isolated.pixel_count == 1
    assert isolated.area_ha == 0.09
    assert isolated.baseline_domain == CandidateBaselineDomain.AUTOMATED_FOREST
    assert isolated.first_loss_year_range == (2023, 2023)


def test_excludes_recovered_insufficient_and_outside_baseline_without_hiding_them() -> None:
    forest = ForestTransitionCode.STABLE_FOREST
    loss = ForestTransitionCode.CANDIDATE_FOREST_LOSS
    unknown = ForestTransitionCode.INSUFFICIENT_DATA
    persistence = _persistence(
        [
            [loss, loss, forest, forest],
            [forest, loss, loss, unknown],
            [forest, forest, loss, loss],
            [forest, forest, loss, loss],
        ],
        width=4,
        height=1,
    )
    automated = np.asarray([[True, True, False, True]])

    result = segment_rf_seeded_candidate_domain(
        persistence=persistence,
        automated_forest_mask=automated,
        baseline_review_mask=np.zeros_like(automated),
        grid_spec=_grid(width=4, height=1),
    )

    assert result.candidate_count == 1
    assert result.candidate_mask.tolist() == [[False, False, False, True]]
    assert result.ineligible_terminal_loss_mask.tolist() == [[False, False, True, False]]
    assert result.ineligible_terminal_loss_pixel_count == 1
    assert not result.candidate_mask[0, 0]
    assert not result.candidate_mask[0, 1]


def test_rejects_overlapping_or_misaligned_baseline_domains() -> None:
    forest = ForestTransitionCode.STABLE_FOREST
    persistence = _persistence([[forest, forest, forest, forest]], width=1, height=1)
    grid = _grid(width=1, height=1)

    with pytest.raises(ValueError, match="rf_seeded_candidate_baseline_domains_overlap"):
        segment_rf_seeded_candidate_domain(
            persistence=persistence,
            automated_forest_mask=np.ones((1, 1), dtype=np.bool_),
            baseline_review_mask=np.ones((1, 1), dtype=np.bool_),
            grid_spec=grid,
        )

    with pytest.raises(ValueError, match="rf_seeded_candidate_automated_forest_mask_shape"):
        segment_rf_seeded_candidate_domain(
            persistence=persistence,
            automated_forest_mask=np.ones((1, 2), dtype=np.bool_),
            baseline_review_mask=np.zeros((1, 1), dtype=np.bool_),
            grid_spec=grid,
        )
