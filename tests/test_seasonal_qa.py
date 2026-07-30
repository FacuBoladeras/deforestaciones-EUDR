"""QA científico del cubo estacional materializado."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from shapely.geometry import box

from deforestation_pipeline.config import SpectralIndex, load_config
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.seasonal_qa import (
    SeasonalQaError,
    SeasonalQaInput,
    analyze_seasonal_rasters,
    seasonal_coverage_json_schema,
)
from deforestation_pipeline.temporal_cube import SeasonCode, TemporalWindow

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AOI = box(-63.0, -33.0, -62.998, -32.998)
GRID = derive_raster_grid_spec(
    aoi_wgs84=AOI,
    target_crs="EPSG:32720",
    resolution_m=30,
    nodata=-9999,
)
WINDOW = TemporalWindow(
    period_id="2022-DJF",
    season_year=2022,
    season=SeasonCode.DJF,
    start_date=date(2021, 12, 1),
    end_date_exclusive=date(2022, 3, 1),
    expected_days=90,
    crosses_calendar_year=True,
)
MAM_WINDOW = TemporalWindow(
    period_id="2022-MAM",
    season_year=2022,
    season=SeasonCode.MAM,
    start_date=date(2022, 3, 1),
    end_date_exclusive=date(2022, 6, 1),
    expected_days=92,
    crosses_calendar_year=False,
)


def _raster_bytes(
    values: np.ndarray,
    band_names: tuple[str, ...],
) -> bytes:
    profile = {
        "driver": "GTiff",
        "height": GRID.height,
        "width": GRID.width,
        "count": len(band_names),
        "dtype": values.dtype,
        "crs": GRID.target_crs,
        "transform": Affine(*GRID.transform),
        "nodata": GRID.nodata,
    }
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            dataset.write(values)
            dataset.descriptions = band_names
        return bytes(memory.read())


def _qa_input(
    *,
    window: TemporalWindow = WINDOW,
    mismatch: bool = False,
    sensor_nodata: bool = False,
    empty_coverage: bool = False,
) -> SeasonalQaInput:
    height, width = GRID.height, GRID.width
    total = np.full((1, height, width), 3, dtype=np.int16)
    row, column = height // 2, width // 2
    total[0, row, column] = 0
    l30 = np.full((1, height, width), 2, dtype=np.int16)
    s30 = np.full((1, height, width), 1, dtype=np.int16)
    l30[0, row, column] = 0
    s30[0, row, column] = 0
    if mismatch:
        s30[0, row, min(column + 1, width - 1)] = 2
    if sensor_nodata:
        sensor_column = min(column + 1, width - 1)
        total[0, row, sensor_column] = 2
        l30[0, row, sensor_column] = 2
        s30[0, row, sensor_column] = int(GRID.nodata)
    reflectance = np.stack(
        [
            np.full((height, width), value, dtype=np.float32)
            for value in (0.08, 0.10, 0.12, 0.30, 0.20, 0.15)
        ]
    )
    indices = np.stack([np.full((height, width), value, dtype=np.float32) for value in (0.5, 0.4)])
    if empty_coverage:
        total[:] = 0
        l30[:] = 0
        s30[:] = 0
        reflectance[:] = GRID.nodata
        indices[:] = GRID.nodata
    return SeasonalQaInput(
        window=window,
        input_scene_count=5,
        l30_scene_count=3,
        s30_scene_count=2,
        reflectance_bytes=_raster_bytes(
            reflectance,
            ("blue", "green", "red", "nir", "swir1", "swir2"),
        ),
        indices_bytes=_raster_bytes(indices, ("NDVI", "NBR")),
        total_count_bytes=_raster_bytes(total, ("valid_observation_count",)),
        l30_count_bytes=_raster_bytes(l30, ("valid_observation_count_l30",)),
        s30_count_bytes=_raster_bytes(s30, ("valid_observation_count_s30",)),
    )


def test_seasonal_qa_computes_coverage_summaries_and_figures() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    result = analyze_seasonal_rasters(
        periods=(_qa_input(),),
        aoi_wgs84=AOI,
        grid_spec=GRID,
        spectral_indices=(SpectralIndex.NDVI, SpectralIndex.NBR),
        output_config=config.output,
        window_plan_sha256="a" * 64,
        generated_at=datetime(2026, 7, 28, 18, tzinfo=UTC),
    )

    coverage = result.coverage.periods[0]
    assert coverage.period_id == "2022-DJF"
    assert coverage.roi_pixel_count > 1
    assert coverage.observed_pixel_count_total == coverage.roi_pixel_count - 1
    assert coverage.coverage_state == "partial_observed_coverage"
    assert coverage.quality_flags == ("partial_observed_coverage",)
    assert coverage.total_count.median == 3
    assert len(result.summaries) == 8
    assert {item.variable for item in result.summaries} == {
        "blue",
        "green",
        "red",
        "nir",
        "swir1",
        "swir2",
        "NDVI",
        "NBR",
    }
    assert set(result.files) == {
        "json/temporal/seasonal/coverage.json",
        "tables/seasonal/summary.csv",
        "figures/seasonal/qa/index_timeseries.png",
        "figures/seasonal/qa/observation_coverage.png",
        "figures/seasonal/qa/rgb_timeline.png",
    }
    assert result.files["figures/seasonal/qa/index_timeseries.png"].startswith(b"\x89PNG")
    assert result.files["figures/seasonal/qa/rgb_timeline.png"].startswith(b"\x89PNG")


def test_rgb_timeline_and_qa_are_ordered_from_t0_to_tx() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    result = analyze_seasonal_rasters(
        periods=(
            _qa_input(window=MAM_WINDOW),
            _qa_input(window=WINDOW),
        ),
        aoi_wgs84=AOI,
        grid_spec=GRID,
        spectral_indices=(SpectralIndex.NDVI, SpectralIndex.NBR),
        output_config=config.output,
        window_plan_sha256="a" * 64,
        generated_at=datetime(2026, 7, 28, 18, tzinfo=UTC),
    )

    assert tuple(item.period_id for item in result.coverage.periods) == (
        "2022-DJF",
        "2022-MAM",
    )


def test_seasonal_qa_rejects_sensor_count_inconsistency() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    with pytest.raises(SeasonalQaError, match="sensor_count_sum_mismatch"):
        analyze_seasonal_rasters(
            periods=(_qa_input(mismatch=True),),
            aoi_wgs84=AOI,
            grid_spec=GRID,
            spectral_indices=(SpectralIndex.NDVI, SpectralIndex.NBR),
            output_config=config.output,
            window_plan_sha256="a" * 64,
            generated_at=datetime(2026, 7, 28, 18, tzinfo=UTC),
        )


def test_seasonal_qa_interprets_masked_sensor_count_as_zero_observations() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    result = analyze_seasonal_rasters(
        periods=(_qa_input(sensor_nodata=True),),
        aoi_wgs84=AOI,
        grid_spec=GRID,
        spectral_indices=(SpectralIndex.NDVI, SpectralIndex.NBR),
        output_config=config.output,
        window_plan_sha256="a" * 64,
        generated_at=datetime(2026, 7, 28, 18, tzinfo=UTC),
    )

    coverage = result.coverage.periods[0]
    assert coverage.s30_count.minimum == 0
    assert coverage.total_count.minimum == 0


def test_empty_season_is_auditable_insufficient_data_not_stable_signal() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    result = analyze_seasonal_rasters(
        periods=(_qa_input(empty_coverage=True),),
        aoi_wgs84=AOI,
        grid_spec=GRID,
        spectral_indices=(SpectralIndex.NDVI, SpectralIndex.NBR),
        output_config=config.output,
        window_plan_sha256="a" * 64,
        generated_at=datetime(2026, 7, 28, 18, tzinfo=UTC),
    )

    coverage = result.coverage.periods[0]
    assert coverage.coverage_state == "no_observed_coverage"
    assert coverage.observed_pixel_count_total == 0
    assert coverage.total_count.minimum == 0
    assert coverage.quality_flags == (
        "no_observed_coverage",
        "no_l30_observed_coverage",
        "no_s30_observed_coverage",
        "insufficient_data",
    )
    assert all(item.valid_pixel_count == 0 for item in result.summaries)
    assert all(item.median is None for item in result.summaries)


def test_committed_seasonal_coverage_schema_matches_model() -> None:
    committed = json.loads(
        (PROJECT_ROOT / "data/schemas/hls-seasonal-coverage-v1.0.0.json").read_text(
            encoding="utf-8"
        )
    )
    assert committed == seasonal_coverage_json_schema()
