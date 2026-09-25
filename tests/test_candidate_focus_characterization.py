"""Caracterización del dominio candidato antes del refactor RF-first.

Estos tests describen el comportamiento vigente, incluso donde la política
futura deberá cambiarlo. No expresan por sí solos la semántica científica
objetivo.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from deforestation_pipeline.agricultural_evidence import (
    AgriculturalEvidenceObservation,
    EvaluatedAgriculturalEvidence,
    IndependenceAssessment,
)
from deforestation_pipeline.disturbance_candidate_fusion import (
    CandidateSource,
    fuse_disturbance_candidates,
)
from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode
from deforestation_pipeline.post_change_attribution import (
    AgriculturalAttributionSupport,
    AttributionContext,
    EventTrajectory,
    attribute_post_change_event,
)
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256


def _grid(*, width: int) -> RasterGridSpec:
    identity: dict[str, Any] = {
        "target_crs": "EPSG:32721",
        "resolution_m": 30.0,
        "width": width,
        "height": 1,
        "transform": (30.0, 0.0, 500000.0, 0.0, -30.0, 6200000.0),
        "bounds": (500000.0, 6199970.0, 500000.0 + 30.0 * width, 6200000.0),
        "alignment_strategy": "projected_crs_origin_outward_snap",
        "pixel_orientation": "north_up",
    }
    return RasterGridSpec(
        **identity,
        nodata=-9999.0,
        aoi_mask_required=True,
        grid_sha256=raster_grid_sha256(**identity),
    )


def test_current_inventory_is_union_of_robust_and_ever_consecutive_rf_loss() -> None:
    """Congela conteos, áreas y procedencia del inventario anterior al refactor."""
    loss = int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)
    stable = int(ForestTransitionCode.STABLE_FOREST)
    robust = np.asarray([[True, False, False, False, True, False, False]])
    transitions = np.asarray(
        [
            [[stable, stable, loss, stable, loss, stable, stable]],
            [[stable, stable, loss, stable, loss, stable, stable]],
            [[stable, stable, stable, stable, loss, stable, stable]],
            [[stable, stable, stable, stable, loss, stable, stable]],
        ],
        dtype=np.int16,
    )

    result = fuse_disturbance_candidates(
        robust_persistent_mask=robust,
        robust_onset_period_index=np.asarray([[1.0, np.nan, np.nan, np.nan, 2.0, np.nan, np.nan]]),
        rf_transition_cube=transitions,
        rf_years=(2021, 2022, 2023, 2024),
        ccdc_break_mask=np.asarray([[False, False, False, False, False, False, True]]),
        automated_forest_mask=np.ones_like(robust),
        baseline_review_mask=np.zeros_like(robust),
        grid_spec=_grid(width=7),
    )

    assert result.candidate_count == 3
    assert sum(candidate.area_ha for candidate in result.candidates) == 0.27
    assert {candidate.sources for candidate in result.candidates} == {
        (CandidateSource.ROBUST,),
        (CandidateSource.RF_TEMPORAL,),
        (CandidateSource.ROBUST, CandidateSource.RF_TEMPORAL),
    }
    assert np.flatnonzero(result.fused_candidate_mask).tolist() == [0, 2, 4]
    assert np.flatnonzero(result.rf_persistent_loss_mask).tolist() == [2, 4]

    rf_only = next(
        candidate
        for candidate in result.candidates
        if candidate.sources == (CandidateSource.RF_TEMPORAL,)
    )
    assert rf_only.rf_persistent_loss_year_range == (2021, 2022)


def test_source_aware_attribution_blocks_rf_only_automatic_promotion() -> None:
    """RF-only permanece candidato aunque los demás gates resulten favorables."""
    event_id = "RFC-1"
    observation = AgriculturalEvidenceObservation.model_construct(
        evidence_id="AGR-RFC-1",
        event_id=event_id,
        land_use="crop",
    )
    evidence = EvaluatedAgriculturalEvidence.model_construct(
        observation=observation,
        assessment=IndependenceAssessment(
            level="methodological",
            decision_eligible=True,
            reason_codes=["characterization_fixture"],
            policy_id="characterization-policy-v1",
            policy_sha256="b" * 64,
        ),
    )

    result = attribute_post_change_event(
        event={
            "event_id": event_id,
            "event_type": "persistent_disturbance_event",
            "primary_interpretation_domain": "automated_forest",
            "area_ha": 2.0,
            "sources": [CandidateSource.RF_TEMPORAL.value],
            "automatic_promotion_allowed": False,
        },
        trajectory=EventTrajectory(
            years=(2020, 2021, 2022, 2023, 2024),
            forest_fraction=(1.0, 1.0, 0.1, 0.1, 0.1),
            mean_hard_vote_fraction=(1.0, 1.0, 0.1, 0.1, 0.1),
            candidate_loss_fraction=(0.0, 0.0, 0.8, 0.8, 0.8),
            evaluable_pixel_count=(20, 20, 20, 20, 20),
        ),
        context=AttributionContext(agricultural_use_evidence={event_id: (evidence,)}),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=1.4,
            conjunctive_likely_area_ha=1.2,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
        ),
    )

    assert result["record_type"] == "disturbance_candidate"
    assert result["automatic_status"] == "review_required"
    assert result["gates"]["candidate_provenance_eligible_for_automatic_promotion"] is False
    assert result["gates"]["all_conversion_likely_gates"] is False
    assert "rf_only_candidate_not_eligible_for_automatic_promotion" in result["reason_codes"]
