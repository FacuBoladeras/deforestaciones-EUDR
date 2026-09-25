"""Dominio conservador para screening automatizado de la línea base 2020."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from deforestation_pipeline.config import load_config
from deforestation_pipeline.forest_screening import (
    ScreeningDomainCode,
    build_forest_screening_domain,
    build_screening_temporal_domain,
)
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256

ROOT = Path(__file__).resolve().parents[1]


def _grid() -> RasterGridSpec:
    identity: dict[str, Any] = {
        "target_crs": "EPSG:32721",
        "resolution_m": 20.0,
        "width": 5,
        "height": 1,
        "transform": (20.0, 0.0, 500000.0, 0.0, -20.0, 6200000.0),
        "bounds": (500000.0, 6199980.0, 500100.0, 6200000.0),
        "alignment_strategy": "projected_crs_origin_outward_snap",
        "pixel_orientation": "north_up",
    }
    return RasterGridSpec(
        **identity,
        nodata=-9999.0,
        aoi_mask_required=True,
        grid_sha256=raster_grid_sha256(**identity),
    )


def test_screening_partitions_aoi_with_conservative_fixed_rules() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    domain = build_forest_screening_domain(
        source_count=np.array([[2, 2, 2, 1, np.nan]]),
        consensus_forest=np.array([[1, 0, 0, 0, np.nan]]),
        disagreement=np.array([[0, 0, 1, 0, np.nan]]),
        evidence_fraction=np.array([[1.0, 0.0, 0.5, 0.0, np.nan]]),
        rf_vote_fraction=np.array([[0.90, 0.10, 0.50, 0.10, np.nan]]),
        rf_input_complete=np.array([[1, 1, 1, 1, np.nan]]),
        grid_spec=_grid(),
        config=config.forest_screening,
    )

    np.testing.assert_array_equal(domain.aoi_footprint, [[True, True, True, True, False]])
    np.testing.assert_array_equal(domain.automated_forest, [[True, False, False, False, False]])
    np.testing.assert_array_equal(
        domain.automated_nonforest,
        [[False, True, False, False, False]],
    )
    np.testing.assert_array_equal(
        domain.automated_evaluable,
        [[True, True, False, False, False]],
    )
    np.testing.assert_array_equal(domain.review_required, [[False, False, True, False, False]])
    np.testing.assert_array_equal(domain.insufficient_data, [[False, False, False, True, False]])
    np.testing.assert_array_equal(
        domain.domain_code,
        [
            [
                ScreeningDomainCode.AUTOMATED_FOREST,
                ScreeningDomainCode.AUTOMATED_NON_FOREST,
                ScreeningDomainCode.REVIEW_REQUIRED,
                ScreeningDomainCode.INSUFFICIENT_DATA,
                ScreeningDomainCode.OUTSIDE_AOI,
            ]
        ],
    )


def test_screening_area_uses_grid_transform_and_is_exhaustive() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    domain = build_forest_screening_domain(
        source_count=np.array([[2, 2, 2, 1, np.nan]]),
        consensus_forest=np.array([[1, 0, 0, 0, np.nan]]),
        disagreement=np.array([[0, 0, 1, 0, np.nan]]),
        evidence_fraction=np.array([[1.0, 0.0, 0.5, 0.0, np.nan]]),
        rf_vote_fraction=np.array([[0.90, 0.10, 0.50, 0.10, np.nan]]),
        rf_input_complete=np.array([[1, 1, 1, 1, np.nan]]),
        grid_spec=_grid(),
        config=config.forest_screening,
    )

    metrics = domain.metrics.model_dump(mode="json")
    assert metrics["pixel_area_ha"] == 0.04
    assert metrics["aoi_raster_area_ha"] == 0.16
    assert metrics["automated_evaluable_area_ha"] == 0.08
    assert metrics["automated_forest_area_ha"] == 0.04
    assert metrics["review_required_area_ha"] == 0.04
    assert metrics["insufficient_data_area_ha"] == 0.04
    assert metrics["automated_evaluable_fraction"] == 0.5
    assert metrics["review_required_fraction"] == 0.25
    assert metrics["insufficient_data_fraction"] == 0.25


def test_temporal_detection_preserves_signals_across_the_entire_aoi() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    screening = build_forest_screening_domain(
        source_count=np.array([[2, 2, 2, 1, np.nan]]),
        consensus_forest=np.array([[1, 0, 0, 0, np.nan]]),
        disagreement=np.array([[0, 0, 1, 0, np.nan]]),
        evidence_fraction=np.array([[1.0, 0.0, 0.5, 0.0, np.nan]]),
        rf_vote_fraction=np.array([[0.90, 0.10, 0.50, 0.10, np.nan]]),
        rf_input_complete=np.array([[1, 1, 1, 1, np.nan]]),
        grid_spec=_grid(),
        config=config.forest_screening,
    )

    temporal = build_screening_temporal_domain(screening)

    np.testing.assert_array_equal(temporal.evaluable, screening.aoi_footprint)
    np.testing.assert_array_equal(
        temporal.baseline_uncertain,
        screening.aoi_footprint & ~screening.automated_forest,
    )


def test_default_screening_config_is_versioned_and_not_tunable_as_probability() -> None:
    config = load_config(ROOT / "configs" / "default.yml")

    assert config.schema_version == "1.13.0"
    assert config.forest_screening.schema_version == "1.1.0"
    assert config.forest_screening.forest_vote_fraction_minimum == 0.8
    assert config.forest_screening.nonforest_vote_fraction_maximum == 0.2
    assert config.forest_screening.score_semantics == "uncalibrated_binary_tree_vote_fraction"
    assert config.forest_screening.automatic_final_assessment_allowed is False


def test_vote_fraction_is_quantized_to_the_500_tree_support_before_thresholds() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    domain = build_forest_screening_domain(
        source_count=np.array([[2, 2, 2, 2]], dtype=np.int16),
        consensus_forest=np.array([[0, 0, 1, 1]], dtype=np.int16),
        disagreement=np.zeros((1, 4), dtype=np.int16),
        evidence_fraction=np.array([[0.0, 0.0, 1.0, 1.0]]),
        rf_vote_fraction=np.array(
            [[0.20000000298023224, 0.202, 0.7999999821186066, 0.798]],
            dtype=np.float64,
        ),
        rf_input_complete=np.ones((1, 4), dtype=np.int16),
        grid_spec=_grid().model_copy(
            update={
                "width": 4,
                "bounds": (500000.0, 6199980.0, 500080.0, 6200000.0),
            }
        ),
        config=config.forest_screening,
    )

    np.testing.assert_array_equal(domain.automated_nonforest, [[True, False, False, False]])
    np.testing.assert_array_equal(domain.automated_forest, [[False, False, True, False]])
