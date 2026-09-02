from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from PIL import Image
from pydantic import ValidationError
from rasterio.io import MemoryFile

from deforestation_pipeline.ccdc_benchmark import CCDC_SCALAR_SUMMARY_BAND_NAMES
from deforestation_pipeline.change_detection import (
    DISTURBANCE_DIAGNOSTIC_BAND_NAMES,
    DISTURBANCE_SUMMARY_BAND_NAMES,
    CcdcBenchmarkResult,
    DisturbanceStateCode,
    build_forest_evaluation_domain,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.disturbance_evidence import DisturbanceEvidenceMetadata
from deforestation_pipeline.disturbance_materialization import (
    ROBUST_ANOMALY_VISUALIZATION_LABEL,
    DisturbanceCcdcProvenance,
    DisturbancePeriodRaster,
    _render_qa,
    _robust_anomaly_visualization,
    _screening_statement_fields,
    ccdc_result_from_scalar_raster,
    materialize_disturbance_detection,
)
from deforestation_pipeline.forest_screening import (
    ForestScreeningDomain,
    build_forest_screening_domain,
)
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256
from deforestation_pipeline.temporal_cube import build_meteorological_south_window_plan

ROOT = Path(__file__).resolve().parents[1]


def _grid() -> RasterGridSpec:
    identity: dict[str, Any] = {
        "target_crs": "EPSG:32721",
        "resolution_m": 30.0,
        "width": 2,
        "height": 2,
        "transform": (30.0, 0.0, 500000.0, 0.0, -30.0, 6200000.0),
        "bounds": (500000.0, 6199940.0, 500060.0, 6200000.0),
        "alignment_strategy": "projected_crs_origin_outward_snap",
        "pixel_orientation": "north_up",
    }
    return RasterGridSpec(
        **identity,
        nodata=-9999.0,
        aoi_mask_required=True,
        grid_sha256=raster_grid_sha256(**identity),
    )


def _irregular_grid() -> RasterGridSpec:
    identity: dict[str, Any] = {
        "target_crs": "EPSG:32721",
        "resolution_m": 30.0,
        "width": 4,
        "height": 4,
        "transform": (30.0, 0.0, 500000.0, 0.0, -30.0, 6200000.0),
        "bounds": (500000.0, 6199880.0, 500120.0, 6200000.0),
        "alignment_strategy": "projected_crs_origin_outward_snap",
        "pixel_orientation": "north_up",
    }
    return RasterGridSpec(
        **identity,
        nodata=-9999.0,
        aoi_mask_required=True,
        grid_sha256=raster_grid_sha256(**identity),
    )


def _visualization_grid() -> RasterGridSpec:
    identity: dict[str, Any] = {
        "target_crs": "EPSG:32721",
        "resolution_m": 30.0,
        "width": 11,
        "height": 10,
        "transform": (30.0, 0.0, 500000.0, 0.0, -30.0, 6200000.0),
        "bounds": (500000.0, 6199700.0, 500330.0, 6200000.0),
        "alignment_strategy": "projected_crs_origin_outward_snap",
        "pixel_orientation": "north_up",
    }
    return RasterGridSpec(
        **identity,
        nodata=-9999.0,
        aoi_mask_required=True,
        grid_sha256=raster_grid_sha256(**identity),
    )


def _raster(values: np.ndarray, names: tuple[str, ...], grid: RasterGridSpec) -> bytes:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=grid.width,
            height=grid.height,
            count=len(names),
            dtype=str(values.dtype),
            crs=grid.target_crs,
            transform=rasterio.Affine(*grid.transform),
            nodata=grid.nodata,
        ) as dataset:
            dataset.write(values)
            dataset.descriptions = names
        return bytes(memory.read())


def _ccdc(domain_shape: tuple[int, int]) -> CcdcBenchmarkResult:
    false = np.zeros(domain_shape, dtype=np.bool_)
    true = np.ones(domain_shape, dtype=np.bool_)
    nan = np.full(domain_shape, np.nan, dtype=np.float64)
    return CcdcBenchmarkResult(
        fit_eligible=true,
        fit_succeeded=true,
        has_post_cutoff_break=false,
        first_post_cutoff_break_segment_index=np.full(domain_shape, -1, dtype=np.int32),
        first_post_cutoff_break_fractional_year=nan,
        ccdc_breakpoint_pseudo_probability=nan.copy(),
        raw_signed_magnitude=np.full((4, *domain_shape), np.nan, dtype=np.float64),
        decrease_positive_magnitude=np.full((4, *domain_shape), np.nan, dtype=np.float64),
        first_break_segment_observation_count=np.zeros(domain_shape, dtype=np.uint16),
        total_valid_observation_count=np.full(domain_shape, 30, dtype=np.uint16),
        quality_flags_bitmask=np.zeros(domain_shape, dtype=np.uint16),
    )


def _screening(
    grid: RasterGridSpec,
    footprint: np.ndarray | None = None,
) -> ForestScreeningDomain:
    config = load_config(ROOT / "configs" / "default.yml")
    shape = (grid.height, grid.width)
    aoi = np.ones(shape, dtype=np.bool_) if footprint is None else footprint
    source_count = np.full(shape, np.nan, dtype=np.float64)
    source_count[aoi] = 2
    return build_forest_screening_domain(
        source_count=source_count,
        consensus_forest=np.ones(shape, dtype=np.int16),
        disagreement=np.zeros(shape, dtype=np.int16),
        evidence_fraction=np.ones(shape, dtype=np.float32),
        rf_vote_fraction=np.full(shape, 0.9, dtype=np.float32),
        rf_input_complete=np.ones(shape, dtype=np.int16),
        grid_spec=grid,
        config=config.forest_screening,
    )


def _ccdc_with_compatible_breaks(
    positive_pixels: np.ndarray,
) -> CcdcBenchmarkResult:
    spatial_shape = positive_pixels.shape
    first_break = np.full(spatial_shape, np.nan, dtype=np.float64)
    first_break[positive_pixels] = 2021.25
    pseudo_probability = np.full(spatial_shape, np.nan, dtype=np.float64)
    pseudo_probability[positive_pixels] = 0.95
    raw_magnitude = np.full((4, *spatial_shape), np.nan, dtype=np.float64)
    raw_magnitude[:, positive_pixels] = -0.4
    first_segment = np.full(spatial_shape, -1, dtype=np.int32)
    first_segment[positive_pixels] = 0
    first_observations = np.zeros(spatial_shape, dtype=np.uint16)
    first_observations[positive_pixels] = 10
    return CcdcBenchmarkResult(
        fit_eligible=np.ones(spatial_shape, dtype=np.bool_),
        fit_succeeded=np.ones(spatial_shape, dtype=np.bool_),
        has_post_cutoff_break=positive_pixels.copy(),
        first_post_cutoff_break_segment_index=first_segment,
        first_post_cutoff_break_fractional_year=first_break,
        ccdc_breakpoint_pseudo_probability=pseudo_probability,
        raw_signed_magnitude=raw_magnitude,
        decrease_positive_magnitude=-raw_magnitude,
        first_break_segment_observation_count=first_observations,
        total_valid_observation_count=np.full(spatial_shape, 30, dtype=np.uint16),
        quality_flags_bitmask=np.zeros(spatial_shape, dtype=np.uint16),
    )


def test_materialization_emits_six_core_disturbance_artifacts() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    grid = _grid()
    index_names = ("NDVI", "NBR", "NDMI", "NIRv")
    periods: list[DisturbancePeriodRaster] = []
    plan = build_meteorological_south_window_plan(
        start_year=2017,
        end_year=2022,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    for window in plan.windows:
        baseline = 0.7 if window.season_year <= 2020 else 0.2
        values = np.full((4, 2, 2), baseline, dtype=np.float32)
        periods.append(
            DisturbancePeriodRaster(
                period_id=window.period_id,
                season=window.season.value,
                start_date=window.start_date,
                end_date_exclusive=window.end_date_exclusive,
                indices_raster=_raster(values, index_names, grid),
                observation_count_raster=_raster(
                    np.full((1, 2, 2), 5, dtype=np.int16),
                    ("valid_observation_count",),
                    grid,
                ),
                published=window.season_year >= 2019,
            )
        )
    domain = build_forest_evaluation_domain(
        source_count=np.full((2, 2), 2, dtype=np.int16),
        consensus_forest=np.ones((2, 2), dtype=np.int16),
        disagreement=np.zeros((2, 2), dtype=np.int16),
        config=config.disturbance_detection,
    )
    result = materialize_disturbance_detection(
        periods=tuple(periods),
        domain=domain,
        screening_domain=_screening(grid),
        baseline_evidence_fraction=np.ones((2, 2), dtype=np.float32),
        spatial_footprint=np.ones((2, 2), dtype=np.bool_),
        ccdc_result=_ccdc((2, 2)),
        ccdc_provenance=DisturbanceCcdcProvenance(
            status="scalar_summary_materialized",
        ),
        grid_spec=grid,
        config=config.disturbance_detection,
        event_config=config.disturbance_events,
        scientific_parameters_hash="a" * 64,
        requested_start_year=2019,
        requested_end_year=2022,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        png_dpi=72,
    )

    assert set(result.files) >= {
        "json/evidence/disturbance_detection.json",
        "tiffs/evidence/disturbance_summary.tif",
        "tiffs/evidence/disturbance_diagnostics.tif",
        "tiffs/evidence/disturbance_robust_state.tif",
        "figures/evidence/disturbance_detection.png",
        "tables/evidence/disturbance_period_summary.csv",
        "tables/evidence/disturbance_events.csv",
        "json/evidence/disturbance_events.json",
        "json/evidence/disturbance_events.geojson",
        "figures/evidence/disturbance_events.png",
    }
    metadata = json.loads(result.files["json/evidence/disturbance_detection.json"])
    validated_metadata = DisturbanceEvidenceMetadata.model_validate(metadata)
    assert validated_metadata.schema_version == "1.3.0"
    assert metadata["reference_support"]["actual_start_date"] == "2017-03-01"
    assert metadata["requested_range"] == {"start_year": 2019, "end_year": 2022}
    assert metadata["evaluable_range"]["start_date"] == "2021-03-01"
    assert metadata["evaluable_range"]["excluded_cutoff_straddling_period_ids"] == ["2021-DJF"]
    assert "2021-DJF" not in metadata["evaluable_range"]["period_ids"]
    assert metadata["screening"]["primary_interpretation_domain"] == "automated_forest"
    assert (
        metadata["screening"]["temporal_signals_by_domain"]["automated_forest"][
            "persistent_candidate_pixel_count"
        ]
        == 0
    )
    assert metadata["screening"]["automatic_statement_generated"] is True
    assert metadata["screening"]["automatic_statement"] == (
        "sin cambio detectado en el bosque automático de alta confianza "
        "entre 2021-03-01 y 2022-11-30"
    )
    assert metadata["screening"]["final_assessment_generated"] is False
    assert metadata["final_assessment_generated"] is False
    assert metadata["attribution_generated"] is False
    assert metadata["ccdc"]["raw_array_downloaded"] is False
    assert metadata["ccdc"]["status"] == "scalar_summary_materialized"
    assert metadata["ccdc"]["scalar_summary_downloaded"] is True
    assert "failure_code" not in metadata["ccdc"]
    assert "probability" not in json.dumps(metadata).lower()
    assert "deforest" not in json.dumps(metadata).lower()

    with MemoryFile(result.files["tiffs/evidence/disturbance_summary.tif"]) as memory:
        with memory.open() as dataset:
            assert dataset.descriptions == DISTURBANCE_SUMMARY_BAND_NAMES
            assert set(dataset.dtypes) == {"float32"}
            assert dataset.tags()["integer_semantics_preserved"] == "true"
            assert dataset.tags()["grid_sha256"] == grid.grid_sha256
    with MemoryFile(result.files["tiffs/evidence/disturbance_diagnostics.tif"]) as memory:
        with memory.open() as dataset:
            assert dataset.descriptions == DISTURBANCE_DIAGNOSTIC_BAND_NAMES
            recovery = dataset.read()[DISTURBANCE_DIAGNOSTIC_BAND_NAMES.index("recovery_indicator")]
            assert np.all(recovery == grid.nodata)
            assert dataset.tags()["recovery_indicator"] == "reserved_nodata"
            assert dataset.tags()["grid_sha256"] == grid.grid_sha256
    with MemoryFile(result.files["tiffs/evidence/disturbance_robust_state.tif"]) as memory:
        with memory.open() as dataset:
            assert dataset.descriptions == ("robust_state_code",)
            assert dataset.tags()["ccdc_used_as_veto"] == "false"
            assert dataset.tags()["grid_sha256"] == grid.grid_sha256

    Image.open(BytesIO(result.files["figures/evidence/disturbance_detection.png"])).verify()
    csv_text = result.files["tables/evidence/disturbance_period_summary.csv"].decode()
    assert "period_index,period_id,start_date,end_date_exclusive" in csv_text
    assert "2021-MAM" in csv_text

    invalid_metadata = dict(metadata)
    invalid_metadata["final_assessment_generated"] = True
    with pytest.raises(ValidationError, match="final_assessment_generated"):
        DisturbanceEvidenceMetadata.model_validate(invalid_metadata)

    missing_status = dict(metadata)
    missing_status["ccdc"] = dict(metadata["ccdc"])
    missing_status["ccdc"].pop("status")
    with pytest.raises(ValidationError, match="status"):
        DisturbanceEvidenceMetadata.model_validate(missing_status)


def test_all_nodata_post_period_remains_not_evaluable_not_stable() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    grid = _grid()
    index_names = ("NDVI", "NBR", "NDMI", "NIRv")
    plan = build_meteorological_south_window_plan(
        start_year=2017,
        end_year=2022,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    periods: list[DisturbancePeriodRaster] = []
    for window in plan.windows:
        values = np.full((4, 2, 2), 0.7, dtype=np.float32)
        count = np.full((1, 2, 2), 5, dtype=np.int16)
        if window.start_date >= config.disturbance_detection.analysis_start_date:
            values[:] = grid.nodata
            count[:] = 0
        periods.append(
            DisturbancePeriodRaster(
                period_id=window.period_id,
                season=window.season.value,
                start_date=window.start_date,
                end_date_exclusive=window.end_date_exclusive,
                indices_raster=_raster(values, index_names, grid),
                observation_count_raster=_raster(
                    count,
                    ("valid_observation_count",),
                    grid,
                ),
                published=window.season_year >= 2019,
            )
        )
    domain = build_forest_evaluation_domain(
        source_count=np.full((2, 2), 2, dtype=np.int16),
        consensus_forest=np.ones((2, 2), dtype=np.int16),
        disagreement=np.zeros((2, 2), dtype=np.int16),
        config=config.disturbance_detection,
    )
    unavailable_ccdc = replace(
        _ccdc((2, 2)),
        fit_eligible=np.zeros((2, 2), dtype=np.bool_),
        fit_succeeded=np.zeros((2, 2), dtype=np.bool_),
        total_valid_observation_count=np.zeros((2, 2), dtype=np.uint16),
    )

    result = materialize_disturbance_detection(
        periods=tuple(periods),
        domain=domain,
        screening_domain=_screening(grid),
        baseline_evidence_fraction=np.ones((2, 2), dtype=np.float32),
        spatial_footprint=np.ones((2, 2), dtype=np.bool_),
        ccdc_result=unavailable_ccdc,
        ccdc_provenance=DisturbanceCcdcProvenance(
            status="unavailable_global_failure",
            failure_code="synthetic_ccdc_service_error",
        ),
        grid_spec=grid,
        config=config.disturbance_detection,
        event_config=config.disturbance_events,
        scientific_parameters_hash="a" * 64,
        requested_start_year=2019,
        requested_end_year=2022,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        png_dpi=72,
    )

    with MemoryFile(result.files["tiffs/evidence/disturbance_summary.tif"]) as memory:
        with memory.open() as dataset:
            summary = dataset.read()
            state = summary[0]
            valid_counts = summary[7]
    assert np.all(state == DisturbanceStateCode.NOT_EVALUABLE)
    assert np.all(valid_counts == 0)
    metadata = json.loads(result.files["json/evidence/disturbance_detection.json"])
    assert metadata["ccdc"] == {
        "change_metric_semantics": "algorithmic_breakpoint_score_not_calibrated",
        "failure_code": "synthetic_ccdc_service_error",
        "raw_array_downloaded": False,
        "scalar_summary_downloaded": False,
        "source_product": "HLSL30",
        "status": "unavailable_global_failure",
    }


def test_materialization_rejects_a_grid_mismatch_before_publishing() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    grid = _grid()
    wrong_grid = grid.model_copy(update={"width": 3})
    period = DisturbancePeriodRaster(
        period_id="2019-MAM",
        season="MAM",
        start_date=date(2019, 3, 1),
        end_date_exclusive=date(2019, 6, 1),
        indices_raster=_raster(
            np.ones((4, 2, 2), dtype=np.float32),
            ("NDVI", "NBR", "NDMI", "NIRv"),
            grid,
        ),
        observation_count_raster=_raster(
            np.ones((1, 2, 2), dtype=np.int16),
            ("valid_observation_count",),
            grid,
        ),
        published=True,
    )
    domain = build_forest_evaluation_domain(
        source_count=np.full((2, 2), 2, dtype=np.int16),
        consensus_forest=np.ones((2, 2), dtype=np.int16),
        disagreement=np.zeros((2, 2), dtype=np.int16),
        config=config.disturbance_detection,
    )

    with pytest.raises(ValueError, match="grid_dimensions_mismatch"):
        materialize_disturbance_detection(
            periods=(period,),
            domain=domain,
            screening_domain=_screening(wrong_grid),
            baseline_evidence_fraction=np.ones((2, 2), dtype=np.float32),
            spatial_footprint=np.ones((2, 3), dtype=np.bool_),
            ccdc_result=_ccdc((2, 2)),
            ccdc_provenance=DisturbanceCcdcProvenance(
                status="scalar_summary_materialized",
            ),
            grid_spec=wrong_grid,
            config=config.disturbance_detection,
            event_config=config.disturbance_events,
            scientific_parameters_hash="a" * 64,
            requested_start_year=2019,
            requested_end_year=2019,
            generated_at=datetime(2026, 7, 29, tzinfo=UTC),
            png_dpi=72,
        )


def test_materialization_preserves_nodata_outside_the_aoi_footprint() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    grid = _grid()
    index_names = ("NDVI", "NBR", "NDMI", "NIRv")
    periods: list[DisturbancePeriodRaster] = []
    plan = build_meteorological_south_window_plan(
        start_year=2017,
        end_year=2021,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    for window in plan.windows:
        periods.append(
            DisturbancePeriodRaster(
                period_id=window.period_id,
                season=window.season.value,
                start_date=window.start_date,
                end_date_exclusive=window.end_date_exclusive,
                indices_raster=_raster(
                    np.full((4, 2, 2), 0.7, dtype=np.float32),
                    index_names,
                    grid,
                ),
                observation_count_raster=_raster(
                    np.full((1, 2, 2), 5, dtype=np.int16),
                    ("valid_observation_count",),
                    grid,
                ),
                published=window.season_year >= 2019,
            )
        )
    footprint = np.array([[True, False], [True, True]], dtype=np.bool_)
    domain = build_forest_evaluation_domain(
        source_count=np.full((2, 2), 2, dtype=np.int16),
        consensus_forest=np.ones((2, 2), dtype=np.int16),
        disagreement=np.zeros((2, 2), dtype=np.int16),
        config=config.disturbance_detection,
    )

    result = materialize_disturbance_detection(
        periods=tuple(periods),
        domain=domain,
        screening_domain=_screening(grid, footprint),
        baseline_evidence_fraction=np.ones((2, 2), dtype=np.float32),
        spatial_footprint=footprint,
        ccdc_result=_ccdc((2, 2)),
        ccdc_provenance=DisturbanceCcdcProvenance(
            status="scalar_summary_materialized",
        ),
        grid_spec=grid,
        config=config.disturbance_detection,
        event_config=config.disturbance_events,
        scientific_parameters_hash="a" * 64,
        requested_start_year=2019,
        requested_end_year=2021,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        png_dpi=72,
    )

    for path in (
        "tiffs/evidence/disturbance_summary.tif",
        "tiffs/evidence/disturbance_diagnostics.tif",
    ):
        with MemoryFile(result.files[path]) as memory:
            with memory.open() as dataset:
                values = dataset.read()
                assert np.all(values[:, 0, 1] == grid.nodata)
                assert dataset.tags()["aoi_footprint_applied"] == "true"

    metadata = json.loads(result.files["json/evidence/disturbance_detection.json"])
    assert metadata["rasters"]["aoi_footprint_applied"] is True
    assert sum(metadata["statistics"]["state_code"].values()) == 3
    rows = tuple(
        csv.DictReader(
            StringIO(result.files["tables/evidence/disturbance_period_summary.csv"].decode("utf-8"))
        )
    )
    assert {int(row["valid_pixel_count"]) for row in rows} == {3}
    assert {int(row["signal_pixel_count"]) for row in rows} == {0}


def test_positive_irregular_aoi_with_hole_limits_all_published_evidence() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    grid = _irregular_grid()
    index_names = ("NDVI", "NBR", "NDMI", "NIRv")
    footprint = np.array(
        [
            [True, True, False, False],
            [True, False, True, False],
            [True, True, True, False],
            [False, True, True, True],
        ],
        dtype=np.bool_,
    )
    expected_candidates = np.zeros((4, 4), dtype=np.bool_)
    expected_candidates[0, 0] = True
    expected_candidates[1, 2] = True
    expected_candidates[2, 1] = True
    positive_inputs = expected_candidates.copy()
    positive_inputs[0, 3] = True
    positive_inputs[1, 1] = True  # hueco deliberadamente positivo en los insumos
    reference_values = {2017: 0.68, 2018: 0.72, 2019: 0.69, 2020: 0.71}
    periods: list[DisturbancePeriodRaster] = []
    plan = build_meteorological_south_window_plan(
        start_year=2017,
        end_year=2022,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    for window in plan.windows:
        values = np.full(
            (4, grid.height, grid.width),
            reference_values.get(window.season_year, 0.70),
            dtype=np.float32,
        )
        if window.season_year >= 2021:
            values[:, positive_inputs] = 0.20
        periods.append(
            DisturbancePeriodRaster(
                period_id=window.period_id,
                season=window.season.value,
                start_date=window.start_date,
                end_date_exclusive=window.end_date_exclusive,
                indices_raster=_raster(values, index_names, grid),
                observation_count_raster=_raster(
                    np.full((1, grid.height, grid.width), 5, dtype=np.int16),
                    ("valid_observation_count",),
                    grid,
                ),
                published=window.season_year >= 2019,
            )
        )
    domain = build_forest_evaluation_domain(
        source_count=np.full((grid.height, grid.width), 2, dtype=np.int16),
        consensus_forest=np.ones((grid.height, grid.width), dtype=np.int16),
        disagreement=np.zeros((grid.height, grid.width), dtype=np.int16),
        config=config.disturbance_detection,
    )

    result = materialize_disturbance_detection(
        periods=tuple(periods),
        domain=domain,
        screening_domain=_screening(grid, footprint),
        baseline_evidence_fraction=np.ones(
            (grid.height, grid.width),
            dtype=np.float32,
        ),
        spatial_footprint=footprint,
        ccdc_result=_ccdc_with_compatible_breaks(positive_inputs),
        ccdc_provenance=DisturbanceCcdcProvenance(
            status="scalar_summary_materialized",
        ),
        grid_spec=grid,
        config=config.disturbance_detection,
        event_config=config.disturbance_events,
        scientific_parameters_hash="a" * 64,
        requested_start_year=2019,
        requested_end_year=2022,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        png_dpi=72,
    )

    with MemoryFile(result.files["tiffs/evidence/disturbance_summary.tif"]) as memory:
        with memory.open() as dataset:
            summary = dataset.read()
            state = summary[DISTURBANCE_SUMMARY_BAND_NAMES.index("disturbance_state_code")]
            assert np.array_equal(
                state == DisturbanceStateCode.PERSISTENT_CANDIDATE,
                expected_candidates,
            )
            assert np.all(summary[:, ~footprint] == grid.nodata)

    with MemoryFile(result.files["tiffs/evidence/disturbance_diagnostics.tif"]) as memory:
        with memory.open() as dataset:
            assert np.all(dataset.read()[:, ~footprint] == grid.nodata)

    metadata = json.loads(result.files["json/evidence/disturbance_detection.json"])
    state_counts = metadata["statistics"]["state_code"]
    assert state_counts["PERSISTENT_CANDIDATE"] == int(expected_candidates.sum())
    assert sum(state_counts.values()) == int(footprint.sum())
    assert metadata["screening"]["automatic_statement_generated"] is False
    assert "automatic_statement" not in metadata["screening"]

    rows = tuple(
        csv.DictReader(
            StringIO(result.files["tables/evidence/disturbance_period_summary.csv"].decode("utf-8"))
        )
    )
    assert {int(row["valid_pixel_count"]) for row in rows} == {int(footprint.sum())}
    fully_post_cutoff = tuple(
        row for row in rows if date.fromisoformat(row["start_date"]) >= date(2021, 1, 1)
    )
    assert fully_post_cutoff
    assert {int(row["signal_pixel_count"]) for row in fully_post_cutoff} == {
        int(expected_candidates.sum())
    }
    Image.open(BytesIO(result.files["figures/evidence/disturbance_detection.png"])).verify()


def test_statement_scope_is_automated_forest_when_only_nonforest_has_signals() -> None:
    zero_counts = {
        "any_robust_signal_pixel_count": 0,
        "ccdc_break_pixel_count": 0,
        "persistent_candidate_pixel_count": 0,
        "transient_signal_pixel_count": 0,
        "detector_disagreement_pixel_count": 0,
    }
    signal_counts = {
        "automated_forest": dict(zero_counts),
        "automated_nonforest": {
            **zero_counts,
            "any_robust_signal_pixel_count": 1,
        },
        "review_required": dict(zero_counts),
        "insufficient_data": dict(zero_counts),
    }

    result = _screening_statement_fields(
        signal_counts=signal_counts,
        period_start_date=date(2021, 3, 1),
        period_end_date=date(2025, 11, 30),
    )

    assert result["automatic_statement_generated"] is True
    assert result["automatic_statement_scope"] == "automated_forest_high_confidence"
    assert result["automatic_statement"] == (
        "sin cambio detectado en el bosque automático de alta confianza "
        "entre 2021-03-01 y 2025-11-30"
    )


def test_robust_visualization_caps_extreme_outlier_and_masks_footprint() -> None:
    grid = _visualization_grid()
    robust_z = np.linspace(0.1, 10.0, grid.width * grid.height).reshape(
        grid.height,
        grid.width,
    )
    robust_z[0, 0] = 305_630.0
    footprint = np.ones(robust_z.shape, dtype=np.bool_)
    footprint[4, 5] = False

    panel, upper_limit = _robust_anomaly_visualization(
        robust_z,
        spatial_footprint=footprint,
    )

    assert upper_limit < float(np.nanmax(robust_z)) * 0.01
    np.testing.assert_array_equal(np.ma.getmaskarray(panel), ~footprint)
    assert "p99" in ROBUST_ANOMALY_VISUALIZATION_LABEL
    png = _render_qa(
        state=np.zeros(robust_z.shape, dtype=np.uint8),
        reason=np.zeros(robust_z.shape, dtype=np.uint8),
        robust_z=robust_z,
        ccdc_magnitude=np.zeros(robust_z.shape, dtype=np.float64),
        spatial_footprint=footprint,
        grid_spec=grid,
        dpi=72,
    )
    Image.open(BytesIO(png)).verify()


def test_ccdc_scalar_without_break_remains_a_successful_stable_fit() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    grid = _grid()
    domain = build_forest_evaluation_domain(
        source_count=np.full((2, 2), 2, dtype=np.int16),
        consensus_forest=np.ones((2, 2), dtype=np.int16),
        disagreement=np.zeros((2, 2), dtype=np.int16),
        config=config.disturbance_detection,
    )
    values = np.zeros((len(CCDC_SCALAR_SUMMARY_BAND_NAMES), 2, 2), dtype=np.float32)
    values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index("ccdc_fit_succeeded")] = 1
    values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index("ccdc_total_valid_observation_count")] = 30

    result = ccdc_result_from_scalar_raster(
        content=_raster(values, CCDC_SCALAR_SUMMARY_BAND_NAMES, grid),
        domain=domain,
        grid_spec=grid,
        config=config.disturbance_detection,
    )

    assert result.fit_succeeded.all()
    assert not result.has_post_cutoff_break.any()
    assert np.isnan(result.first_post_cutoff_break_fractional_year).all()
    assert np.isnan(result.raw_signed_magnitude).all()
