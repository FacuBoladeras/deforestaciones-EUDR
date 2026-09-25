from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import shape
from shapely.ops import unary_union

from deforestation_pipeline.agricultural_evidence import (
    AgriculturalEvidenceObservation,
    EvaluatedAgriculturalEvidence,
    IndependenceAssessment,
    canonical_payload_sha256,
    load_agricultural_evidence_policy,
)
from deforestation_pipeline.post_change_attribution import (
    AgriculturalAttributionSupport,
    AttributionContext,
    AttributionRules,
    EventTrajectory,
    _normalize_disjoint_event_geometries,
    _normalize_likely_event_geometries,
    _validate_attribution_payload_v5,
    _validate_disjoint_event_geometries,
    attribute_post_change_event,
    materialize_post_change_attribution,
)


def _evaluated_evidence(*, land_use: str, decision_eligible: bool) -> EvaluatedAgriculturalEvidence:
    observation = AgriculturalEvidenceObservation.model_construct(
        evidence_id="AGR-PDE-1",
        event_id="PDE-1",
        land_use=land_use,
    )
    assessment = IndependenceAssessment(
        level="methodological" if decision_eligible else "ineligible",
        decision_eligible=decision_eligible,
        reason_codes=["test_policy_assessment"],
        policy_id="test-policy-v1",
        policy_sha256="b" * 64,
    )
    return EvaluatedAgriculturalEvidence.model_construct(
        observation=observation,
        assessment=assessment,
    )


def _event() -> dict[str, object]:
    return {
        "event_id": "PDE-1",
        "event_type": "persistent_disturbance_event",
        "primary_interpretation_domain": "automated_forest",
        "area_ha": 2.0,
        "area_threshold_met": True,
        "estimated_onset_period_id": "2022-MAM",
    }


def _trajectory(values: tuple[float, ...]) -> EventTrajectory:
    return EventTrajectory(
        years=(2020, 2021, 2022, 2023, 2024),
        forest_fraction=values,
        mean_hard_vote_fraction=values,
        candidate_loss_fraction=(0.0, 0.0, 0.8, 0.8, 0.8),
        evaluable_pixel_count=(20, 20, 20, 20, 20),
    )


def test_persistent_rf_loss_without_post_use_evidence_stays_unknown() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(),
    )

    assert result["post_change_use"] == "unknown"
    assert result["automatic_status"] == "review_required"
    assert result["record_type"] == "disturbance_candidate"
    assert result["interpretation_level"] == "candidate_episode"
    assert result["interpretation_status"] == "persistent_unattributed"
    assert result["candidate_id"] == "PDE-1"
    assert result["event_id"] is None
    assert "agricultural_use_evidence_missing" in result["reason_codes"]
    assert result["human_review_required"] is True
    assert result["conversion_confirmed"] is False


def test_policy_eligible_crop_evidence_can_reach_conversion_likely_when_all_gates_pass() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=1.4,
            conjunctive_likely_area_ha=1.2,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
        ),
    )

    assert result["post_change_use"] == "agriculture_likely"
    assert result["automatic_status"] == "conversion_likely"
    assert result["record_type"] == "conversion_likely_event"
    assert result["interpretation_level"] == "event"
    assert result["interpretation_status"] == "conversion_likely"
    assert result["candidate_id"] == "PDE-1"
    assert result["event_id"] == "PDE-1"
    assert result["gates"]["all_conversion_likely_gates"] is True
    assert result["conversion_confirmed"] is False
    assert result["persistent_agricultural_area_ha"] == 1.4
    assert result["likely_conversion_area_ha"] == 1.2
    assert "all_conjunctive_conversion_gates_passed" in result["reasons_for"]


def test_candidate_agricultural_coverage_below_five_percent_blocks_attribution() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=0.098,
            conjunctive_likely_area_ha=0.098,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
            candidate_agricultural_coverage_fraction=0.049,
            candidate_agricultural_coverage_threshold=0.05,
            candidate_agricultural_coverage_gate_met=False,
        ),
    )

    assert result["post_change_use"] == "unknown"
    assert result["gates"]["candidate_agricultural_coverage_at_least_threshold"] is False
    assert result["candidate_agricultural_coverage_fraction"] == 0.049
    assert "candidate_agricultural_coverage_below_threshold" in result["reason_codes"]


def test_rf_seeded_candidate_with_only_ccdc_support_stays_review_required() -> None:
    event = {
        **_event(),
        "segmentation_source": "rf_terminal_persistent_forest_loss",
        "support_sources": ["ccdc_break_support"],
    }
    result = attribute_post_change_event(
        event=event,
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
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
    assert result["candidate_provenance_assessment"]["ccdc_support_present"] is True
    assert result["candidate_provenance_assessment"]["robust_support_present"] is False
    assert "ccdc_support_does_not_unlock_automatic_promotion" in result["reason_codes"]


def test_rf_seeded_candidate_with_robust_support_can_reach_conversion_likely() -> None:
    event = {
        **_event(),
        "segmentation_source": "rf_terminal_persistent_forest_loss",
        "support_sources": ["robust_persistent_support", "ccdc_break_support"],
    }
    result = attribute_post_change_event(
        event=event,
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=1.4,
            conjunctive_likely_area_ha=1.2,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
        ),
    )

    assert result["record_type"] == "conversion_likely_event"
    assert result["automatic_status"] == "conversion_likely"
    assert result["gates"]["candidate_provenance_eligible_for_automatic_promotion"] is True
    assert result["candidate_provenance_assessment"] == {
        "policy": "rf_seeded_source_aware_v1",
        "segmentation_source": "rf_terminal_persistent_forest_loss",
        "support_sources": ["robust_persistent_support", "ccdc_break_support"],
        "robust_support_present": True,
        "ccdc_support_present": True,
        "eligible_for_automatic_promotion": True,
        "shared_hls_support_is_independent": False,
    }


def test_mixed_baseline_candidate_keeps_automatic_forest_subset_eligible() -> None:
    event = {
        **_event(),
        "primary_interpretation_domain": "baseline_review",
        "baseline_domain": "mixed",
        "automated_forest_pixel_count": 8,
        "baseline_review_pixel_count": 12,
        "segmentation_source": "rf_terminal_persistent_forest_loss",
        "support_sources": ["robust_persistent_support"],
    }
    result = attribute_post_change_event(
        event=event,
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=1.4,
            conjunctive_likely_area_ha=1.2,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
            candidate_agricultural_coverage_fraction=0.7,
            candidate_agricultural_coverage_threshold=0.05,
            candidate_agricultural_coverage_gate_met=True,
        ),
    )

    assert result["gates"]["forest_at_cutoff"] is True
    assert result["record_type"] == "conversion_likely_event"


def test_visec_publication_gate_uses_strict_conjunctive_area_not_candidate_footprint() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=1.4,
            conjunctive_likely_area_ha=0.5,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
        ),
    )

    assert result["candidate_area_ha"] == 2.0
    assert result["conjunctive_conversion_evidence_area_ha"] == 0.5
    assert result["post_change_use"] == "agriculture_likely"
    assert result["record_type"] == "disturbance_candidate"
    assert result["interpretation_status"] == "subthreshold_conversion_evidence"
    assert result["automatic_status"] == "review_required"
    assert result["event_id"] is None
    assert result["likely_conversion_area_ha"] == 0.0
    assert result["gates"]["visec_operational_area_strictly_greater_than_threshold"] is False
    assert result["gates"]["all_scientific_conversion_evidence_gates"] is True
    assert "visec_operational_event_area_not_exceeded" in result["reason_codes"]


def test_visec_publication_gate_promotes_conjunctive_area_strictly_above_half_hectare() -> None:
    result = attribute_post_change_event(
        event={**_event(), "area_ha": 0.51, "area_threshold_met": False},
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="pasture", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=0.7,
            conjunctive_likely_area_ha=0.500001,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="d" * 64,
        ),
    )

    assert result["record_type"] == "conversion_likely_event"
    assert result["likely_conversion_area_ha"] == 0.500001
    assert result["gates"]["visec_operational_area_strictly_greater_than_threshold"] is True
    assert result["conversion_confirmed"] is False


def test_visec_operational_area_reference_is_not_a_tunable_model_threshold() -> None:
    with np.testing.assert_raises_regex(
        ValueError,
        "visec_operational_event_area_threshold_ha debe ser 0.5",
    ):
        AttributionRules(visec_operational_event_area_threshold_ha=0.6)


def test_alternative_explanations_report_only_evidence_sources_actually_evaluated() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 0.9, 0.1, 0.4, 0.8)),
        context=AttributionContext(),
    )

    assert result["alternative_explanation_assessment"] == {
        "supported_explanations": ["forest_recovery_after_trough"],
        "evaluated_sources": ["rf_annual_recovery_trajectory"],
        "unevaluated_explanation_types": [
            "fire",
            "flood",
            "drought",
            "field_or_high_resolution_evidence",
        ],
    }


def test_policy_evidence_without_materialized_spatial_conjunction_stays_review_required() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
    )

    assert result["automatic_status"] == "review_required"
    assert result["likely_conversion_area_ha"] == 0.0
    assert "persistent_agricultural_spatial_support_missing" in result["reasons_against"]


def test_recovery_zeroes_likely_area_even_with_agricultural_spatial_support() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 0.9, 0.1, 0.4, 0.8)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=1.4,
            conjunctive_likely_area_ha=1.2,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
        ),
    )

    assert result["post_change_use"] == "forest_recovery"
    assert result["record_type"] == "disturbance_candidate"
    assert result["interpretation_status"] == "temporary_or_recovered"
    assert result["automatic_status"] == "low_risk"
    assert result["human_review_required"] is False
    assert result["likely_conversion_area_ha"] == 0.0
    assert "strong_alternative_explanation_present" in result["reasons_against"]


def test_overlapping_event_geometries_are_rejected_to_prevent_double_counting() -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }
    features = [
        {"type": "Feature", "geometry": geometry, "properties": {"event_id": event_id}}
        for event_id in ("PDE-1", "PDE-2")
    ]

    try:
        _validate_disjoint_event_geometries(features)
    except ValueError as exc:
        assert "overlap" in str(exc)
    else:
        raise AssertionError("overlapping event geometries accepted")


def test_subpixel_topology_sliver_is_removed_without_losing_candidates_or_area() -> None:
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-59.5, -31.1],
                        [-59.4999, -31.1],
                        [-59.4999, -31.0999],
                        [-59.5, -31.0999],
                        [-59.5, -31.1],
                    ]
                ],
            },
            "properties": {"event_id": "PDE-EARLY"},
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-59.49990005, -31.1],
                        [-59.4998, -31.1],
                        [-59.4998, -31.0999],
                        [-59.49990005, -31.0999],
                        [-59.49990005, -31.1],
                    ]
                ],
            },
            "properties": {"event_id": "PDE-LATE"},
        },
    ]
    original_geometries = [shape(cast(dict[str, Any], feature["geometry"])) for feature in features]
    assert original_geometries[0].intersection(original_geometries[1]).area > 0

    normalized = _normalize_disjoint_event_geometries(features)

    normalized_geometries = [shape(feature["geometry"]) for feature in normalized]
    assert len(normalized) == 2
    assert [feature["properties"]["event_id"] for feature in normalized] == [
        "PDE-EARLY",
        "PDE-LATE",
    ]
    assert normalized_geometries[0].intersection(normalized_geometries[1]).area == 0
    assert normalized[0]["properties"].get("candidate_topology_overlap_trimmed_ha", 0.0) == 0.0
    assert normalized[1]["properties"]["candidate_topology_overlap_trimmed_ha"] > 0.0
    assert math.isclose(
        unary_union(normalized_geometries).area,
        unary_union(original_geometries).area,
        abs_tol=1e-15,
    )
    assert (
        shape(cast(dict[str, Any], features[1]["geometry"]))
        .intersection(shape(cast(dict[str, Any], features[0]["geometry"])))
        .area
        > 0
    )
    assert "candidate_topology_overlap_trimmed_ha" not in features[1]["properties"]


def test_real_nativo_numeric_overlap_is_not_classified_as_substantive() -> None:
    """Regresión del par que bloqueó la atribución del smoke frontend."""
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    [
                        [
                            [-59.49521174, -31.05767872],
                            [-59.49520468, -31.05740828],
                            [-59.49489054, -31.05741436],
                            [-59.4948976, -31.0576848],
                            [-59.49521174, -31.05767872],
                        ]
                    ],
                    [
                        [
                            [-59.49519761, -31.05713785],
                            [-59.49520468, -31.05740828],
                            [-59.49551881, -31.0574022],
                            [-59.49551175, -31.05713177],
                            [-59.49519761, -31.05713785],
                        ]
                    ],
                ],
            },
            "properties": {"event_id": "PDE-6CBC6EEC9E47"},
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-59.49518349, -31.05659697],
                        [-59.49519055, -31.05686741],
                        [-59.49550469, -31.05686133],
                        [-59.49551175, -31.05713177],
                        [-59.49488348, -31.05714393],
                        [-59.49486936, -31.05660305],
                        [-59.49518349, -31.05659697],
                    ]
                ],
            },
            "properties": {"event_id": "PDE-25F4AB6C9F41"},
        },
    ]

    # La validación clasifica por superficie equivalente, no por grados².
    _validate_disjoint_event_geometries(features)
    normalized = _normalize_disjoint_event_geometries(features)
    geometries = [shape(feature["geometry"]) for feature in normalized]
    assert geometries[0].intersection(geometries[1]).area == 0.0
    assert (
        sum(
            float(feature["properties"].get("candidate_topology_overlap_trimmed_ha", 0.0))
            for feature in normalized
        )
        > 0.0
    )


def test_substantive_overlap_is_not_normalized_away() -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [[-59.5, -31.1], [-59.4, -31.1], [-59.4, -31], [-59.5, -31], [-59.5, -31.1]]
        ],
    }
    features = [
        {"type": "Feature", "geometry": geometry, "properties": {"event_id": event_id}}
        for event_id in ("PDE-1", "PDE-2")
    ]

    with np.testing.assert_raises_regex(ValueError, "event geometries overlap:PDE-1:PDE-2"):
        _normalize_disjoint_event_geometries(features)


def test_likely_event_normalization_keeps_area_ledger_and_candidate_geometry() -> None:
    candidate = {
        "type": "Polygon",
        "coordinates": [
            [[-59.6, -31.2], [-59.3, -31.2], [-59.3, -30.9], [-59.6, -30.9], [-59.6, -31.2]]
        ],
    }
    first = {
        "type": "Polygon",
        "coordinates": [
            [
                [-59.5, -31.1],
                [-59.4999, -31.1],
                [-59.4999, -31.0999],
                [-59.5, -31.0999],
                [-59.5, -31.1],
            ]
        ],
    }
    second = {
        "type": "Polygon",
        "coordinates": [
            [
                [-59.49990005, -31.1],
                [-59.4998, -31.1],
                [-59.4998, -31.0999],
                [-59.49990005, -31.0999],
                [-59.49990005, -31.1],
            ]
        ],
    }
    records = [
        {
            "event_id": event_id,
            "record_type": "conversion_likely_event",
            "candidate_geometry": candidate,
            "conjunctive_conversion_evidence_geometry": geometry,
            "likely_conversion_geometry": geometry,
            "likely_conversion_area_ha": area,
        }
        for event_id, geometry, area in (("PDE-1", first, 0.7), ("PDE-2", second, 0.8))
    ]

    normalized = _normalize_likely_event_geometries(records)

    geometries = [shape(record["likely_conversion_geometry"]) for record in normalized]
    assert geometries[0].intersection(geometries[1]).area == 0
    assert [record["likely_conversion_area_ha"] for record in normalized] == [0.7, 0.8]
    assert [record["candidate_geometry"] for record in normalized] == [candidate, candidate]
    assert all(
        record["conjunctive_conversion_evidence_geometry"] == record["likely_conversion_geometry"]
        for record in normalized
    )


def test_duplicate_event_ids_are_rejected_even_when_geometries_do_not_overlap() -> None:
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[offset, 0], [offset + 1, 0], [offset + 1, 1], [offset, 1], [offset, 0]]
                ],
            },
            "properties": {"event_id": "PDE-1"},
        }
        for offset in (0, 2)
    ]

    with np.testing.assert_raises_regex(ValueError, "event_id duplicado:PDE-1"):
        _validate_disjoint_event_geometries(features)


def test_declared_plantation_plus_loss_and_recovery_is_managed_harvest_not_conversion() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 0.9, 0.1, 0.4, 0.8)),
        context=AttributionContext(
            declared_land_use="managed_forest_plantation",
            declared_context_source="user_declared",
        ),
    )

    assert result["post_change_use"] == "managed_harvest"
    assert result["automatic_status"] == "review_required"
    assert result["gates"]["strong_alternative_explanation_absent"] is False
    assert "declared_managed_forest_context" in result["reason_codes"]


def test_recovery_without_managed_context_is_forest_recovery() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 0.9, 0.1, 0.4, 0.8)),
        context=AttributionContext(),
    )

    assert result["post_change_use"] == "forest_recovery"
    assert result["automatic_status"] == "low_risk"
    assert result["interpretation_status"] == "temporary_or_recovered"
    assert result["human_review_required"] is False
    assert "forest_recovery_after_trough" in result["reason_codes"]


def test_nonpersistent_signal_remains_candidate_without_forcing_review() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.4, 0.4, 0.4)),
        context=AttributionContext(),
    )

    assert result["record_type"] == "disturbance_candidate"
    assert result["interpretation_level"] == "candidate_episode"
    assert result["interpretation_status"] == "candidate_only"
    assert result["automatic_status"] == "low_risk"
    assert result["human_review_required"] is False
    assert result["event_id"] is None


def test_policy_ineligible_agricultural_claim_cannot_unlock_conversion_likely() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="pasture", decision_eligible=False),)
            }
        ),
    )

    assert result["post_change_use"] == "unknown"
    assert result["automatic_status"] == "review_required"
    assert "agricultural_use_evidence_not_policy_eligible" in result["reason_codes"]


def _write_source_bundle(
    root: Path,
    *,
    input_sha: str,
    rf: bool,
    rf_forests: tuple[int, int, int, int, int] = (1, 1, 0, 0, 1),
) -> Path:
    bundle = root / ("rf" if rf else "events")
    artifacts: list[dict[str, object]] = []

    def register(relative: str, content: bytes) -> None:
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        artifacts.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    if rf:
        register(
            "json/evidence/rf_forest_deltas_metadata.json",
            json.dumps({"score_semantics": "uncalibrated_binary_tree_vote_fraction"}).encode(
                "utf-8"
            ),
        )
        transform = from_origin(0, 2, 1, 1)
        for year, forest in zip(range(2020, 2025), rf_forests, strict=True):
            for kind, value, dtype, nodata in (
                ("class", forest, "int16", -9999),
                ("vote_fraction", float(forest), "float32", -9999.0),
            ):
                relative = f"tiffs/evidence/rf_forest_{kind}_{year}.tif"
                path = bundle / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with rasterio.open(
                    path,
                    "w",
                    driver="GTiff",
                    width=2,
                    height=2,
                    count=1,
                    dtype=dtype,
                    crs="EPSG:4326",
                    transform=transform,
                    nodata=nodata,
                ) as dataset:
                    dataset.write(np.full((1, 2, 2), value, dtype=dtype))
                content = path.read_bytes()
                artifacts.append(
                    {
                        "path": relative,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            if year > 2020:
                relative = f"tiffs/evidence/rf_forest_transition_{year}_vs_2020.tif"
                path = bundle / relative
                with rasterio.open(
                    path,
                    "w",
                    driver="GTiff",
                    width=2,
                    height=2,
                    count=1,
                    dtype="int16",
                    crs="EPSG:4326",
                    transform=transform,
                    nodata=-9999,
                ) as dataset:
                    dataset.write(np.full((1, 2, 2), 4 if forest else 3, dtype="int16"))
                content = path.read_bytes()
                artifacts.append(
                    {
                        "path": relative,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
    else:
        feature_collection = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 1], [1, 1], [1, 2], [0, 2], [0, 1]]],
                    },
                    "properties": _event(),
                }
            ],
        }
        register(
            "json/evidence/disturbance_events.geojson",
            json.dumps(feature_collection).encode("utf-8"),
        )
    manifest = {
        "schema_version": "test",
        "analysis_id": (
            "11111111-1111-5111-8111-111111111111" if rf else "22222222-2222-5222-8222-222222222222"
        ),
        "created_at": "2026-08-10T12:00:00+00:00",
        "input_sha256": input_sha,
        "artifacts": artifacts,
    }
    path = bundle / "json/run/manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def _write_persistence_bundle(
    root: Path,
    *,
    input_sha: str,
    source_event_manifest_sha: str | None = None,
    source_event_artifact_sha: str | None = None,
    evidence_set_id: str = "33333333-3333-5333-8333-333333333333",
    declared_support_fraction: float = 1.0,
    support_values: np.ndarray | None = None,
    contract_version: str = "1.0.0",
) -> Path:
    bundle = root / "agriculture"
    artifacts: list[dict[str, object]] = []

    def register(relative: str, content: bytes) -> None:
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        artifacts.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)
    left, bottom = transformer.transform(0, 1)
    right, top = transformer.transform(1, 2)
    raster_path = "tiffs/evidence/agricultural_persistence/PDE-1_persistent_crop_support.tif"
    path = bundle / raster_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:6933",
        transform=from_origin(left, top, (right - left) / 2, (top - bottom) / 2),
        nodata=-9999.0,
    ) as dataset:
        values = (
            np.ones((2, 2), dtype="float32")
            if support_values is None
            else np.asarray(support_values, dtype="float32")
        )
        dataset.write(values[np.newaxis, ...])
    raster_content = path.read_bytes()
    raster_sha = hashlib.sha256(raster_content).hexdigest()
    artifacts.append({"path": raster_path, "size_bytes": len(raster_content), "sha256": raster_sha})
    source = {
        "dataset_id": "dynamic_world_v1",
        "provider": "Google / World Resources Institute",
        "collection": "GOOGLE/DYNAMICWORLD/V1",
        "version": "1",
        "asset": raster_path,
        "license": "CC-BY-4.0",
        "access_date": "2026-08-11",
        "attribution": "Contains modified Dynamic World data",
        "restrictions": [],
    }
    pixel_area_ha = ((right - left) / 2) * ((top - bottom) / 2) / 10000
    event_area = pixel_area_ha * 4
    declared_persistent_area = float(np.sum(values, dtype=np.float64) * pixel_area_ha)
    evidence: dict[str, Any] = {
        "schema_version": "1.0.0",
        "evidence_set_id": evidence_set_id,
        "created_at": "2026-08-11T15:00:00+00:00",
        "observations": [
            {
                "evidence_id": "AGR-PERSIST-PDE-1",
                "event_id": "PDE-1",
                "land_use": "crop",
                "source": source,
                "source_descriptor_sha256": canonical_payload_sha256(source),
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 1], [1, 1], [1, 2], [0, 2], [0, 1]]],
                    "crs": "EPSG:4326",
                },
                "spatial_support": {
                    "source_resolution_m": 10.0,
                    "event_area_ha": event_area,
                    "valid_area_ha": event_area,
                    "attributed_area_ha": declared_persistent_area * declared_support_fraction,
                    "event_coverage_fraction": 1.0,
                    "attributed_event_fraction": (
                        declared_persistent_area / event_area * declared_support_fraction
                    ),
                    "resampling_method": "none_native_equal_area_grid",
                },
                "temporal_support": {
                    "estimated_onset_window_end": "2022-05-31",
                    "post_change_window_start": "2022-06-01",
                    "post_change_window_end": "2022-12-31",
                    "effective_observation_date": "2022-12-31",
                    "valid_observation_count": 7,
                    "qualifying_observation_count": 2,
                    "minimum_consecutive_qualifying_periods": 2,
                    "observed_consecutive_qualifying_periods": 1,
                    "minimum_qualifying_periods": 2,
                    "observed_qualifying_periods": 2,
                    "minimum_observation_duration_days": 180,
                    "observed_duration_days": 214,
                    "persistence_basis": "seasonal_nonconsecutive_periods",
                    "persistence_satisfied": True,
                    "long_gap_interpolation_used": False,
                    "persistence_rule": "2_of_3_over_180_days_nonconsecutive",
                },
                "quality": {
                    "confidence": None,
                    "confidence_semantics": "deterministic_threshold_support_not_probability",
                    "quality_flags": [],
                    "discarded_observation_reasons": [],
                    "nodata_area_ha": 0.0,
                    "uncertainty_notes": ["test_fixture"],
                },
                "artifact_sha256": raster_sha,
            }
        ],
    }
    persistence_event = {
        "event_id": "PDE-1",
        "persistence_status": "persistent_crop_support",
        "estimated_onset_window_end": "2022-05-31",
        "post_change_window_start": "2022-06-01",
        "post_change_window_end": "2022-12-31",
        "observed_duration_days": 214,
        "valid_period_count": 7,
        "qualifying_period_count": 2,
        "observed_consecutive_qualifying_periods": 1,
        "gap_period_count": 0,
        "maximum_consecutive_gap_periods": 0,
        "long_gap_interpolation_used": False,
        "temporal_disagreement_present": True,
        "cross_source_disagreement_state": "single_source_not_assessed",
        "forest_recovery_assessed": False,
        "event_area_ha": event_area,
        "persistent_crop_area_ha": declared_persistent_area * declared_support_fraction,
        "persistent_event_fraction": (
            declared_persistent_area / event_area * declared_support_fraction
        ),
        "support_raster_path": raster_path,
        "support_raster_sha256": raster_sha,
        "sensitivity": [],
        "per_period_support": [],
        "quality_flags": ["test_fixture"],
    }
    source_collection_bundle: dict[str, Any] = {
        "analysis_id": "44444444-4444-5444-8444-444444444444",
        "input_sha256": input_sha,
        "manifest_sha256": "d" * 64,
        "bundle_name": "raw",
    }
    if source_event_manifest_sha is not None:
        source_collection_bundle["source_event_bundle"] = {
            "analysis_id": "22222222-2222-5222-8222-222222222222",
            "input_sha256": input_sha,
            "manifest_sha256": source_event_manifest_sha,
            "bundle_name": "events",
            "event_artifact_path": "json/evidence/disturbance_events.geojson",
            "event_artifact_sha256": source_event_artifact_sha,
        }
    persistence: dict[str, Any] = {
        "schema_version": "1.0.0",
        "analysis_id": "33333333-3333-5333-8333-333333333333",
        "establishment_id": "field",
        "created_at": "2026-08-11T15:00:00+00:00",
        "source_collection_bundle": source_collection_bundle,
        "persistence_rule": "2_of_3_over_180_days_nonconsecutive",
        "primary_crop_observation_fraction": 0.5,
        "events": [persistence_event],
    }
    if contract_version == "2.0.0":
        evidence["schema_version"] = "2.0.0"
        evidence["observations"][0]["evidence_id"] = "AGR-OCCURRENCE-PDE-1"
        evidence["observations"][0]["temporal_support"] = {
            "estimated_onset_window_end": "2022-05-31",
            "post_change_window_start": "2022-06-01",
            "post_change_window_end": "2022-12-31",
            "effective_observation_date": "2022-06-30",
            "valid_period_count": 7,
            "qualifying_period_count": 1,
            "evidence_basis": "any_qualifying_post_event_period",
            "evidence_satisfied": True,
            "signal_strength": "weak",
            "long_gap_interpolation_used": False,
            "evidence_rule": "1_qualifying_post_event_period",
        }
        persistence["schema_version"] = "2.0.0"
        persistence["persistence_rule"] = "1_qualifying_post_event_period"
        persistence_event.update(
            {
                "persistence_status": "agricultural_support_detected",
                "signal_strength": "weak",
                "effective_observation_date": "2022-06-30",
                "strength_raster_path": raster_path,
                "strength_raster_sha256": raster_sha,
                "signal_strength_areas": [
                    {
                        "signal_strength": "weak",
                        "qualifying_period_minimum": 1,
                        "qualifying_period_maximum": 1,
                        "area_ha": declared_persistent_area * declared_support_fraction,
                        "event_fraction": (
                            declared_persistent_area / event_area * declared_support_fraction
                        ),
                    }
                ],
            }
        )
    register("json/evidence/agricultural_evidence_persistent.json", json.dumps(evidence).encode())
    register("json/evidence/agricultural_persistence.json", json.dumps(persistence).encode())
    manifest = {
        "schema_version": "1.0.0",
        "analysis_id": persistence["analysis_id"],
        "created_at": persistence["created_at"],
        "input_sha256": input_sha,
        "artifacts": artifacts,
    }
    manifest_path = bundle / "json/run/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_materializer_overlays_events_with_rf_trajectory_and_publishes_manifest(
    tmp_path: Path,
) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=True)

    output = materialize_post_change_attribution(
        source_event_bundle=events,
        source_rf_bundle=rf,
        output_root=tmp_path / "out",
        establishment_id="field",
        context=AttributionContext(
            declared_land_use="managed_forest_plantation",
            declared_context_source="user_declared",
        ),
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    payload = json.loads(
        (output / "json/evidence/post_change_attribution.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "review_required"
    assert payload["event_count"] == 0
    assert payload["candidate_count"] == 1
    assert payload["events"] == []
    assert payload["candidates"][0]["post_change_use"] == "managed_harvest"
    assert payload["candidates"][0]["conversion_confirmed"] is False
    assert payload["records"] == payload["candidates"]
    manifest = json.loads((output / "json/run/manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["artifacts"]) == 6
    assert all((output / item["path"]).is_file() for item in manifest["artifacts"])
    assert manifest["source_bundles"]["events"]["input_sha256"] == "a" * 64
    assert len(manifest["code"]["module_sha256"]) == 64
    assert len(manifest["code"]["schema_sha256"]) == 64


def test_materializer_rejects_bundles_from_different_input_geometry(tmp_path: Path) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(tmp_path, input_sha="b" * 64, rf=True)

    try:
        materialize_post_change_attribution(
            source_event_bundle=events,
            source_rf_bundle=rf,
            output_root=tmp_path / "out",
            establishment_id="field",
            context=AttributionContext(),
            created_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
    except ValueError as exc:
        assert "input_sha256" in str(exc)
    else:
        raise AssertionError("se esperaba rechazo de bundles incompatibles")


def test_persistent_bundle_materializes_conjunctive_area_and_reversible_sources(
    tmp_path: Path,
) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha="b" * 64,
        source_event_artifact_sha=hashlib.sha256(
            (events / "json/evidence/disturbance_events.geojson").read_bytes()
        ).hexdigest(),
    )

    output = materialize_post_change_attribution(
        source_event_bundle=events,
        source_rf_bundle=rf,
        source_agricultural_bundle=agriculture,
        agricultural_policy=load_agricultural_evidence_policy(
            Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
        ),
        output_root=tmp_path / "out",
        establishment_id="field",
        context=AttributionContext(),
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    payload = json.loads((output / "json/evidence/post_change_attribution.json").read_text("utf-8"))
    event = payload["events"][0]
    assert payload["event_count"] == 1
    assert payload["candidate_count"] == 0
    assert payload["candidates"] == []
    assert payload["records"] == payload["events"]
    assert event["automatic_status"] == "conversion_likely"
    assert event["likely_conversion_area_ha"] > 0
    assert payload["likely_conversion_area_ha"] == event["likely_conversion_area_ha"]
    assert event["likely_conversion_area_ha"] <= event["persistent_agricultural_area_ha"]
    conjunction = output / event["agricultural_spatial_support"]["conjunctive_support_raster_path"]
    assert conjunction.is_file()
    figure = output / "figures/evidence/likely_conversion_conjunction_PDE-1.png"
    assert figure.is_file()
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    assert manifest["source_bundles"]["agricultural_persistence"]["manifest_sha256"]
    assert manifest["visualizations"][0]["path"] == figure.relative_to(output).as_posix()
    assert (
        manifest["visualizations"][0]["sha256"] == hashlib.sha256(figure.read_bytes()).hexdigest()
    )


def test_v2_single_occurrence_bundle_is_consumed_without_confirming_conversion(
    tmp_path: Path,
) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha="b" * 64,
        source_event_artifact_sha=hashlib.sha256(
            (events / "json/evidence/disturbance_events.geojson").read_bytes()
        ).hexdigest(),
        contract_version="2.0.0",
    )

    output = materialize_post_change_attribution(
        source_event_bundle=events,
        source_rf_bundle=rf,
        source_agricultural_bundle=agriculture,
        agricultural_policy=load_agricultural_evidence_policy(
            Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
        ),
        output_root=tmp_path / "out",
        establishment_id="field",
        context=AttributionContext(),
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    payload = json.loads((output / "json/evidence/post_change_attribution.json").read_text("utf-8"))
    assert payload["events"][0]["automatic_status"] == "conversion_likely"
    assert payload["events"][0]["conversion_confirmed"] is False
    temporal = payload["events"][0]["agricultural_use_evidence"][0]["observation"][
        "temporal_support"
    ]
    assert temporal["qualifying_period_count"] == 1
    assert temporal["signal_strength"] == "weak"


def test_probable_event_geometry_is_materialized_conjunction_and_candidate_is_preserved(
    tmp_path: Path,
) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha="b" * 64,
        source_event_artifact_sha=hashlib.sha256(
            (events / "json/evidence/disturbance_events.geojson").read_bytes()
        ).hexdigest(),
        support_values=np.asarray([[1.0, 0.0], [0.0, 0.0]], dtype="float32"),
    )

    output = materialize_post_change_attribution(
        source_event_bundle=events,
        source_rf_bundle=rf,
        source_agricultural_bundle=agriculture,
        agricultural_policy=load_agricultural_evidence_policy(
            Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
        ),
        output_root=tmp_path / "out",
        establishment_id="field",
        context=AttributionContext(),
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    payload = json.loads((output / "json/evidence/post_change_attribution.json").read_text("utf-8"))
    feature_collection = json.loads(
        (output / "json/evidence/post_change_attribution.geojson").read_text("utf-8")
    )
    event = payload["events"][0]
    event_feature = feature_collection["features"][0]
    csv_row = next(
        csv.DictReader(
            (output / "tables/evidence/post_change_attribution.csv").read_text("utf-8").splitlines()
        )
    )
    assert event["candidate_geometry"]["coordinates"] == [[[0, 1], [1, 1], [1, 2], [0, 2], [0, 1]]]
    assert event["likely_conversion_geometry"] == event_feature["geometry"]
    assert shape(event_feature["geometry"]).area < shape(event["candidate_geometry"]).area
    assert event["likely_conversion_area_ha"] == event["conjunctive_conversion_evidence_area_ha"]
    assert float(csv_row["candidate_area_ha"]) == event["candidate_area_ha"]
    assert (
        float(csv_row["conjunctive_conversion_evidence_area_ha"])
        == event["conjunctive_conversion_evidence_area_ha"]
    )


def test_persistent_bundle_must_derive_from_exact_current_event_artifact(tmp_path: Path) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha=hashlib.sha256(
            (events / "json/run/manifest.json").read_bytes()
        ).hexdigest(),
        source_event_artifact_sha="b" * 64,
    )

    with np.testing.assert_raises_regex(
        ValueError,
        "agricultural source event artifact does not match current event artifact",
    ):
        materialize_post_change_attribution(
            source_event_bundle=events,
            source_rf_bundle=rf,
            source_agricultural_bundle=agriculture,
            agricultural_policy=load_agricultural_evidence_policy(
                Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
            ),
            output_root=tmp_path / "out",
            establishment_id="field",
            context=AttributionContext(),
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
        )


def test_persistence_and_evidence_documents_must_share_analysis_identity(tmp_path: Path) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    event_manifest_sha = hashlib.sha256(
        (events / "json/run/manifest.json").read_bytes()
    ).hexdigest()
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha=event_manifest_sha,
        source_event_artifact_sha=hashlib.sha256(
            (events / "json/evidence/disturbance_events.geojson").read_bytes()
        ).hexdigest(),
        evidence_set_id="55555555-5555-5555-8555-555555555555",
    )

    with np.testing.assert_raises_regex(
        ValueError,
        "persistence and evidence analysis identity mismatch",
    ):
        materialize_post_change_attribution(
            source_event_bundle=events,
            source_rf_bundle=rf,
            source_agricultural_bundle=agriculture,
            agricultural_policy=load_agricultural_evidence_policy(
                Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
            ),
            output_root=tmp_path / "out",
            establishment_id="field",
            context=AttributionContext(),
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
        )


def test_persistent_raster_area_must_match_declared_contract_area(tmp_path: Path) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha=hashlib.sha256(
            (events / "json/run/manifest.json").read_bytes()
        ).hexdigest(),
        source_event_artifact_sha=hashlib.sha256(
            (events / "json/evidence/disturbance_events.geojson").read_bytes()
        ).hexdigest(),
        declared_support_fraction=0.5,
    )

    with np.testing.assert_raises_regex(
        ValueError,
        "persistent raster area does not match persistence contract",
    ):
        materialize_post_change_attribution(
            source_event_bundle=events,
            source_rf_bundle=rf,
            source_agricultural_bundle=agriculture,
            agricultural_policy=load_agricultural_evidence_policy(
                Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
            ),
            output_root=tmp_path / "out",
            establishment_id="field",
            context=AttributionContext(),
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
        )


def _v5_record(*, event: bool) -> dict[str, Any]:
    polygon = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
    }
    return {
        "schema_version": "5.0.0",
        "candidate_id": "PDE-1",
        "event_id": "EVT-1" if event else None,
        "record_type": "conversion_likely_event" if event else "disturbance_candidate",
        "interpretation_level": "event" if event else "candidate_episode",
        "interpretation_status": "conversion_likely" if event else "candidate_only",
        "post_change_use": "agriculture_likely" if event else "unknown",
        "automatic_status": "conversion_likely" if event else "review_required",
        "conversion_confirmed": False,
        "human_review_required": not event,
        "gates": {
            "forest_at_cutoff": event,
            "post_cutoff_loss": event,
            "persistent_change": event,
            "agricultural_or_livestock_post_use": event,
            "defensible_area_and_geometry": event,
            "strong_alternative_explanation_absent": event,
            "visec_operational_area_strictly_greater_than_0_5_ha": event,
        },
        "reason_codes": [],
        "reasons_for": [],
        "reasons_against": [],
        "candidate_area_ha": 1.0,
        "persistent_agricultural_area_ha": 0.6 if event else 0.0,
        "conjunctive_conversion_evidence_area_ha": 0.6 if event else 0.0,
        "likely_conversion_area_ha": 0.6 if event else 0.0,
        "candidate_geometry": polygon,
        "conjunctive_conversion_evidence_geometry": polygon if event else None,
        "likely_conversion_geometry": polygon if event else None,
        "alternative_explanation_assessment": {},
        "trajectory": {},
        "context": {},
        "quality_flags": [],
    }


def _v5_payload() -> dict[str, Any]:
    event = _v5_record(event=True)
    candidate = _v5_record(event=False) | {"candidate_id": "PDE-2"}
    return {
        "schema_version": "5.0.0",
        "analysis_id": "12345678-1234-5678-9234-567812345678",
        "establishment_id": "field",
        "created_at": "2026-08-24T00:00:00+00:00",
        "status": "conversion_likely",
        "automatic_final_assessment_generated": False,
        "conversion_confirmed_count": 0,
        "conversion_likely_count": 1,
        "observed_candidate_count": 2,
        "candidate_count": 1,
        "event_count": 1,
        "likely_conversion_area_ha": 0.6,
        "counts_by_post_change_use": {"agriculture_likely": 1, "unknown": 1},
        "counts_by_interpretation_status": {"conversion_likely": 1, "candidate_only": 1},
        "records": [event, candidate],
        "candidates": [candidate],
        "events": [event],
    }


def test_attribution_v5_runtime_validates_partition_and_geometries() -> None:
    _validate_attribution_payload_v5(_v5_payload())


def test_attribution_v5_runtime_rejects_inconsistent_partition() -> None:
    payload = _v5_payload()
    payload["events"] = []

    with np.testing.assert_raises_regex(ValueError, "attribution_v5_partition_mismatch"):
        _validate_attribution_payload_v5(payload)


def test_attribution_v5_runtime_rejects_event_without_probable_geometry() -> None:
    payload = _v5_payload()
    payload["records"][0]["likely_conversion_geometry"] = None
    payload["events"][0]["likely_conversion_geometry"] = None

    with np.testing.assert_raises_regex(ValueError, "attribution_v5_event_geometry_invalid"):
        _validate_attribution_payload_v5(payload)
