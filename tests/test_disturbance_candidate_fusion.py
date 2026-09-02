from __future__ import annotations

from typing import Any

import numpy as np

from deforestation_pipeline.disturbance_candidate_fusion import (
    CANDIDATE_FUSION_SCHEMA_VERSION,
    CandidateResolution,
    CandidateSource,
    fuse_disturbance_candidates,
)
from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256


def _grid(*, width: int, height: int = 1) -> RasterGridSpec:
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


def _transitions(years: list[list[int]]) -> np.ndarray:
    return np.asarray(years, dtype=np.int16).reshape(len(years), 1, -1)


def test_connected_patch_with_heterogeneous_onset_stays_one_spatial_candidate() -> None:
    robust = np.ones((1, 5), dtype=np.bool_)

    result = fuse_disturbance_candidates(
        robust_persistent_mask=robust,
        robust_onset_period_index=np.asarray([[0.0, 0.0, 2.0, 2.0, 4.0]]),
        rf_transition_cube=_transitions(
            [
                [1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1],
            ]
        ),
        rf_years=(2021, 2022),
        ccdc_break_mask=np.zeros_like(robust),
        automated_forest_mask=np.ones_like(robust),
        baseline_review_mask=np.zeros_like(robust),
        grid_spec=_grid(width=5),
    )

    assert result.schema_version == CANDIDATE_FUSION_SCHEMA_VERSION
    assert result.candidate_count == 1
    candidate = result.candidates[0]
    assert candidate.pixel_count == 5
    assert candidate.area_ha == 0.45
    assert candidate.robust_onset_period_range == (0, 4)
    assert [
        (episode.onset_period_index, episode.pixel_count) for episode in candidate.subepisodes
    ] == [
        (0, 2),
        (2, 2),
        (4, 1),
    ]


def test_robust_and_persistent_rf_masks_are_unioned_without_double_counting() -> None:
    robust = np.asarray([[True, True, False, False]])
    loss = int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)
    stable = int(ForestTransitionCode.STABLE_FOREST)

    result = fuse_disturbance_candidates(
        robust_persistent_mask=robust,
        robust_onset_period_index=np.asarray([[1.0, 1.0, np.nan, np.nan]]),
        rf_transition_cube=_transitions(
            [
                [stable, loss, loss, loss],
                [stable, loss, loss, loss],
            ]
        ),
        rf_years=(2021, 2022),
        ccdc_break_mask=np.asarray([[False, False, False, True]]),
        automated_forest_mask=np.ones_like(robust),
        baseline_review_mask=np.zeros_like(robust),
        grid_spec=_grid(width=4),
        ccdc_break_day_offset=np.asarray([[np.nan, np.nan, np.nan, 120.0]]),
    )

    assert result.candidate_count == 1
    candidate = result.candidates[0]
    assert candidate.pixel_count == 4
    assert candidate.area_ha == 0.36
    assert candidate.sources == (CandidateSource.ROBUST, CandidateSource.RF_TEMPORAL)
    assert candidate.robust_pixel_count == 2
    assert candidate.rf_persistent_loss_pixel_count == 3
    assert candidate.robust_rf_overlap_pixel_count == 1
    assert candidate.ccdc_support_pixel_count == 1
    assert candidate.ccdc_break_day_offset_range == (120, 120)
    assert candidate.automatic_promotion_allowed is False
    assert np.count_nonzero(result.fused_candidate_mask) == 4


def test_rf_requires_consecutive_persistent_loss_and_ccdc_never_creates_candidate() -> None:
    shape = (1, 3)
    loss = int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)
    stable = int(ForestTransitionCode.STABLE_FOREST)

    result = fuse_disturbance_candidates(
        robust_persistent_mask=np.zeros(shape, dtype=np.bool_),
        robust_onset_period_index=np.full(shape, np.nan),
        rf_transition_cube=_transitions(
            [
                [loss, stable, stable],
                [stable, loss, stable],
                [loss, stable, stable],
            ]
        ),
        rf_years=(2021, 2022, 2023),
        ccdc_break_mask=np.ones(shape, dtype=np.bool_),
        automated_forest_mask=np.ones(shape, dtype=np.bool_),
        baseline_review_mask=np.zeros(shape, dtype=np.bool_),
        grid_spec=_grid(width=3),
    )

    assert result.candidate_count == 0
    assert not result.fused_candidate_mask.any()


def test_rf_only_candidate_in_baseline_review_is_preserved_for_review() -> None:
    shape = (1, 2)
    loss = int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)
    stable = int(ForestTransitionCode.STABLE_FOREST)

    result = fuse_disturbance_candidates(
        robust_persistent_mask=np.zeros(shape, dtype=np.bool_),
        robust_onset_period_index=np.full(shape, np.nan),
        rf_transition_cube=_transitions(
            [
                [loss, stable],
                [loss, stable],
            ]
        ),
        rf_years=(2021, 2022),
        ccdc_break_mask=np.zeros(shape, dtype=np.bool_),
        automated_forest_mask=np.asarray([[False, True]]),
        baseline_review_mask=np.asarray([[True, False]]),
        grid_spec=_grid(width=2),
    )

    assert result.candidate_count == 1
    candidate = result.candidates[0]
    assert candidate.sources == (CandidateSource.RF_TEMPORAL,)
    assert candidate.resolution == CandidateResolution.REVIEW_REQUIRED
    assert candidate.baseline_review_pixel_count == 1
    assert candidate.automatic_promotion_allowed is False
    assert candidate.rf_evidence_role == "learned_supporting_evidence"


def test_onset_nodata_is_ignored_outside_eligible_robust_candidates() -> None:
    robust = np.asarray([[True, False, True]])
    stable = int(ForestTransitionCode.STABLE_FOREST)

    result = fuse_disturbance_candidates(
        robust_persistent_mask=robust,
        robust_onset_period_index=np.asarray([[1.0, -9999.0, -9999.0]]),
        rf_transition_cube=_transitions(
            [
                [stable, stable, stable],
                [stable, stable, stable],
            ]
        ),
        rf_years=(2021, 2022),
        ccdc_break_mask=np.zeros_like(robust),
        automated_forest_mask=np.asarray([[True, True, False]]),
        baseline_review_mask=np.zeros_like(robust),
        grid_spec=_grid(width=3),
    )

    assert result.candidate_count == 1
    assert result.candidates[0].robust_onset_period_range == (1, 1)


def test_negative_finite_onset_inside_eligible_robust_candidate_is_rejected() -> None:
    robust = np.asarray([[True]])
    stable = int(ForestTransitionCode.STABLE_FOREST)

    with np.testing.assert_raises_regex(
        ValueError,
        "candidate_fusion_robust_onset_period_index_invalid",
    ):
        fuse_disturbance_candidates(
            robust_persistent_mask=robust,
            robust_onset_period_index=np.asarray([[-9999.0]]),
            rf_transition_cube=_transitions([[stable], [stable]]),
            rf_years=(2021, 2022),
            ccdc_break_mask=np.zeros_like(robust),
            automated_forest_mask=np.ones_like(robust),
            baseline_review_mask=np.zeros_like(robust),
            grid_spec=_grid(width=1),
        )


def test_rf_support_is_rejected_outside_validated_temporal_transfer_scope() -> None:
    shape = (1, 1)

    with np.testing.assert_raises_regex(ValueError, "candidate_fusion_rf_years_out_of_scope"):
        fuse_disturbance_candidates(
            robust_persistent_mask=np.zeros(shape, dtype=np.bool_),
            robust_onset_period_index=np.full(shape, np.nan),
            rf_transition_cube=_transitions(
                [
                    [int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)],
                    [int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)],
                ]
            ),
            rf_years=(2024, 2025),
            ccdc_break_mask=np.zeros(shape, dtype=np.bool_),
            automated_forest_mask=np.ones(shape, dtype=np.bool_),
            baseline_review_mask=np.zeros(shape, dtype=np.bool_),
            grid_spec=_grid(width=1),
        )


def test_candidate_id_is_stable_when_provenance_grows_without_footprint_change() -> None:
    shape = (1, 2)
    loss = int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)
    stable = int(ForestTransitionCode.STABLE_FOREST)
    robust_only = fuse_disturbance_candidates(
        robust_persistent_mask=np.ones(shape, dtype=np.bool_),
        robust_onset_period_index=np.asarray([[1.0, 1.0]]),
        rf_transition_cube=_transitions([[stable, stable], [stable, stable]]),
        rf_years=(2021, 2022),
        ccdc_break_mask=np.zeros(shape, dtype=np.bool_),
        automated_forest_mask=np.ones(shape, dtype=np.bool_),
        baseline_review_mask=np.zeros(shape, dtype=np.bool_),
        grid_spec=_grid(width=2),
    )
    robust_and_rf = fuse_disturbance_candidates(
        robust_persistent_mask=np.ones(shape, dtype=np.bool_),
        robust_onset_period_index=np.asarray([[1.0, 1.0]]),
        rf_transition_cube=_transitions([[loss, loss], [loss, loss]]),
        rf_years=(2021, 2022),
        ccdc_break_mask=np.zeros(shape, dtype=np.bool_),
        automated_forest_mask=np.ones(shape, dtype=np.bool_),
        baseline_review_mask=np.zeros(shape, dtype=np.bool_),
        grid_spec=_grid(width=2),
    )

    assert robust_only.candidates[0].pixels == robust_and_rf.candidates[0].pixels
    assert robust_only.candidates[0].candidate_id == robust_and_rf.candidates[0].candidate_id
    assert robust_only.candidates[0].sources != robust_and_rf.candidates[0].sources
