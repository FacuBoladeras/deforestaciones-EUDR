from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image
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
from deforestation_pipeline.disturbance_materialization import (
    DisturbancePeriodRaster,
    ccdc_result_from_scalar_raster,
    materialize_disturbance_detection,
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


def test_materialization_emits_exactly_five_compact_artifacts() -> None:
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
        baseline_evidence_fraction=np.ones((2, 2), dtype=np.float32),
        spatial_footprint=np.ones((2, 2), dtype=np.bool_),
        ccdc_result=_ccdc((2, 2)),
        grid_spec=grid,
        config=config.disturbance_detection,
        scientific_parameters_hash="a" * 64,
        requested_start_year=2019,
        requested_end_year=2022,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
        png_dpi=72,
    )

    assert set(result.files) == {
        "json/evidence/disturbance_detection.json",
        "tiffs/evidence/disturbance_summary.tif",
        "tiffs/evidence/disturbance_diagnostics.tif",
        "figures/evidence/disturbance_detection.png",
        "tables/evidence/disturbance_period_summary.csv",
    }
    metadata = json.loads(result.files["json/evidence/disturbance_detection.json"])
    assert metadata["reference_support"]["actual_start_date"] == "2017-03-01"
    assert metadata["requested_range"] == {"start_year": 2019, "end_year": 2022}
    assert metadata["evaluable_range"]["start_date"] == "2021-03-01"
    assert metadata["evaluable_range"]["excluded_cutoff_straddling_period_ids"] == ["2021-DJF"]
    assert "2021-DJF" not in metadata["evaluable_range"]["period_ids"]
    assert metadata["final_assessment_generated"] is False
    assert metadata["attribution_generated"] is False
    assert metadata["ccdc"]["raw_array_downloaded"] is False
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

    Image.open(BytesIO(result.files["figures/evidence/disturbance_detection.png"])).verify()
    csv_text = result.files["tables/evidence/disturbance_period_summary.csv"].decode()
    assert "period_index,period_id,start_date,end_date_exclusive" in csv_text
    assert "2021-MAM" in csv_text


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
        baseline_evidence_fraction=np.ones((2, 2), dtype=np.float32),
        spatial_footprint=np.ones((2, 2), dtype=np.bool_),
        ccdc_result=unavailable_ccdc,
        grid_spec=grid,
        config=config.disturbance_detection,
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

    import pytest

    with pytest.raises(ValueError, match="grid_dimensions_mismatch"):
        materialize_disturbance_detection(
            periods=(period,),
            domain=domain,
            baseline_evidence_fraction=np.ones((2, 2), dtype=np.float32),
            spatial_footprint=np.ones((2, 3), dtype=np.bool_),
            ccdc_result=_ccdc((2, 2)),
            grid_spec=wrong_grid,
            config=config.disturbance_detection,
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
        baseline_evidence_fraction=np.ones((2, 2), dtype=np.float32),
        spatial_footprint=footprint,
        ccdc_result=_ccdc((2, 2)),
        grid_spec=grid,
        config=config.disturbance_detection,
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
