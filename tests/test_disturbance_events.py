from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from shapely.geometry import shape as shapely_shape

from deforestation_pipeline.config import load_config
from deforestation_pipeline.disturbance_events import (
    EVENT_ONSET_FIGURE_LABEL,
    DisturbanceEventCollection,
    _same_season_comparison_indices,
    materialize_persistent_disturbance_events,
    segment_persistent_disturbance_events,
)
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256

ROOT = Path(__file__).resolve().parents[1]


def test_same_season_comparison_excludes_cutoff_straddling_period() -> None:
    pre_index, post_index = _same_season_comparison_indices(
        onset_axis_index=2,
        all_period_seasons=("DJF", "DJF", "DJF"),
        all_period_start_dates=(
            date(2019, 12, 1),
            date(2020, 12, 1),
            date(2021, 12, 1),
        ),
        all_period_end_dates_exclusive=(
            date(2020, 3, 1),
            date(2021, 3, 1),
            date(2022, 3, 1),
        ),
    )

    assert (pre_index, post_index) == (0, 2)


def _grid(*, width: int = 6, height: int = 5) -> RasterGridSpec:
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


def test_segmentation_uses_eight_neighbors_and_only_automated_forest() -> None:
    grid = _grid()
    persistent = np.zeros((5, 6), dtype=np.bool_)
    persistent[0, 0] = True
    persistent[1, 1] = True  # diagonal: mismo evento con 8 vecinos
    persistent[2, 4] = True
    automated_forest = np.ones((5, 6), dtype=np.bool_)
    automated_forest[2, 4] = False

    result = segment_persistent_disturbance_events(
        persistent_mask=persistent,
        automated_forest_mask=automated_forest,
        grid_spec=grid,
        connectivity=8,
        area_threshold_ha=0.5,
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.pixel_count == 2
    assert event.area_ha == 0.18
    assert event.area_threshold_met is False
    assert result.outside_primary_domain_pixel_count == 1
    assert event.event_id.startswith("PDE-")


def test_event_ids_are_stable_and_independent_of_input_memory_layout() -> None:
    grid = _grid()
    persistent = np.zeros((5, 6), dtype=np.bool_)
    persistent[0, 5] = True
    persistent[4, 0:2] = True
    automated_forest = np.ones((5, 6), dtype=np.bool_)

    first = segment_persistent_disturbance_events(
        persistent_mask=persistent,
        automated_forest_mask=automated_forest,
        grid_spec=grid,
        connectivity=8,
        area_threshold_ha=0.5,
    )
    second = segment_persistent_disturbance_events(
        persistent_mask=np.asfortranarray(persistent),
        automated_forest_mask=np.asfortranarray(automated_forest),
        grid_spec=grid,
        connectivity=8,
        area_threshold_ha=0.5,
    )

    assert [event.event_id for event in first.events] == [event.event_id for event in second.events]
    assert [event.pixel_count for event in first.events] == [2, 1]


def test_materialization_preserves_small_events_and_caps_detail_figures() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    grid = _grid(width=9, height=7)
    persistent = np.zeros((7, 9), dtype=np.bool_)
    persistent[0, 0] = True
    persistent[1, 1] = True  # mismo evento por conectividad 8; polígonos sólo se tocan
    persistent[2, 4:6] = True
    persistent[5:7, 6:9] = True
    automated_forest = np.ones((7, 9), dtype=np.bool_)
    first_period = np.full((7, 9), np.nan, dtype=np.float64)
    first_period[persistent] = 1
    magnitudes = np.zeros((7, 9), dtype=np.float64)
    magnitudes[persistent] = 4.0
    scores = np.zeros((7, 9), dtype=np.float64)
    scores[persistent] = 0.7
    reasons = np.zeros((7, 9), dtype=np.float64)
    reasons[persistent] = 9
    available = np.ones((7, 9), dtype=np.float64)
    agreeing = np.ones((7, 9), dtype=np.float64)
    ccdc_offsets = np.full((7, 9), np.nan, dtype=np.float64)
    ccdc_offsets[5:7, 6:9] = 120
    flags = np.zeros((7, 9), dtype=np.float64)
    period_ids = ("2021-MAM", "2021-JJA", "2021-SON")
    starts = (date(2021, 3, 1), date(2021, 6, 1), date(2021, 9, 1))
    ends = (date(2021, 6, 1), date(2021, 9, 1), date(2021, 12, 1))
    index_names = ("NDVI", "NBR", "NDMI", "NIRv")
    cube = np.full((6, 4, 7, 9), 0.7, dtype=np.float64)
    cube[4:, :, persistent] = 0.2

    result = materialize_persistent_disturbance_events(
        persistent_mask=persistent,
        automated_forest_mask=automated_forest,
        first_anomalous_period_index=first_period,
        maximum_disturbance_magnitude=magnitudes,
        disturbance_score=scores,
        convergence_reason_code=reasons,
        available_detector_count=available,
        agreeing_detector_count=agreeing,
        ccdc_break_day_offset=ccdc_offsets,
        quality_flags_bitmask=flags,
        post_period_ids=period_ids,
        post_period_start_dates=starts,
        post_period_end_dates_exclusive=ends,
        all_period_ids=("2020-MAM", "2020-JJA", "2020-SON", *period_ids),
        all_period_seasons=("MAM", "JJA", "SON", "MAM", "JJA", "SON"),
        all_period_start_dates=(
            date(2020, 3, 1),
            date(2020, 6, 1),
            date(2020, 9, 1),
            *starts,
        ),
        all_period_end_dates_exclusive=(
            date(2020, 6, 1),
            date(2020, 9, 1),
            date(2020, 12, 1),
            *ends,
        ),
        index_names=index_names,
        index_cube=cube,
        grid_spec=grid,
        config=config.disturbance_events.model_copy(update={"detail_figure_limit": 2}),
        generated_at=datetime(2026, 8, 4, tzinfo=UTC),
        scientific_parameters_hash="a" * 64,
        png_dpi=72,
    )

    assert set(result.files) >= {
        "tables/evidence/disturbance_events.csv",
        "json/evidence/disturbance_events.json",
        "json/evidence/disturbance_events.geojson",
        "figures/evidence/disturbance_events.png",
    }
    detail_paths = sorted(
        path for path in result.files if path.startswith("figures/evidence/disturbance_event_")
    )
    assert len(detail_paths) == 2
    rows = list(
        csv.DictReader(StringIO(result.files["tables/evidence/disturbance_events.csv"].decode()))
    )
    assert len(rows) == 3
    assert {row["area_threshold_met"] for row in rows} == {"false", "true"}
    payload = json.loads(result.files["json/evidence/disturbance_events.json"])
    validated = DisturbanceEventCollection.model_validate(payload)
    assert validated.event_count == 3
    assert validated.below_area_threshold_event_count == 2
    assert validated.detail_figure_count == 2
    assert validated.area_crs == "EPSG:32721"
    assert validated.pixel_area_ha == 0.09
    assert validated.automatic_final_assessment_generated is False
    assert validated.attribution_generated is False
    geojson = json.loads(result.files["json/evidence/disturbance_events.geojson"])
    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) == 3
    assert all(shapely_shape(feature["geometry"]).is_valid for feature in geojson["features"])
    two_pixel_geometry_types = sorted(
        feature["geometry"]["type"]
        for feature in geojson["features"]
        if feature["properties"]["pixel_count"] == 2
    )
    assert two_pixel_geometry_types == ["MultiPolygon", "Polygon"]
    assert EVENT_ONSET_FIGURE_LABEL == "primera ventana anómala robusta estimada"
    for path in ("figures/evidence/disturbance_events.png", *detail_paths):
        image = Image.open(BytesIO(result.files[path]))
        image.verify()


def test_empty_event_collection_is_materialized_without_detail_figures() -> None:
    config = load_config(ROOT / "configs" / "default.yml")
    grid = _grid()
    shape = (grid.height, grid.width)
    result = materialize_persistent_disturbance_events(
        persistent_mask=np.zeros(shape, dtype=np.bool_),
        automated_forest_mask=np.ones(shape, dtype=np.bool_),
        first_anomalous_period_index=np.full(shape, np.nan),
        maximum_disturbance_magnitude=np.full(shape, np.nan),
        disturbance_score=np.full(shape, np.nan),
        convergence_reason_code=np.zeros(shape),
        available_detector_count=np.zeros(shape),
        agreeing_detector_count=np.zeros(shape),
        ccdc_break_day_offset=np.full(shape, np.nan),
        quality_flags_bitmask=np.zeros(shape),
        post_period_ids=("2021-MAM",),
        post_period_start_dates=(date(2021, 3, 1),),
        post_period_end_dates_exclusive=(date(2021, 6, 1),),
        all_period_ids=("2020-MAM", "2021-MAM"),
        all_period_seasons=("MAM", "MAM"),
        all_period_start_dates=(date(2020, 3, 1), date(2021, 3, 1)),
        all_period_end_dates_exclusive=(date(2020, 6, 1), date(2021, 6, 1)),
        index_names=("NDVI", "NBR", "NDMI", "NIRv"),
        index_cube=np.full((2, 4, *shape), 0.7),
        grid_spec=grid,
        config=config.disturbance_events,
        generated_at=datetime(2026, 8, 4, tzinfo=UTC),
        scientific_parameters_hash="a" * 64,
        png_dpi=72,
    )

    assert not any("disturbance_event_" in path for path in result.files)
    payload = json.loads(result.files["json/evidence/disturbance_events.json"])
    assert payload["event_count"] == 0
    assert payload["events"] == []
