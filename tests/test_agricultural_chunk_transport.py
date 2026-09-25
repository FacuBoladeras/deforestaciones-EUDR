from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from shapely.geometry import shape

from deforestation_pipeline.agricultural_collector import (
    REMOTE_CHUNK_BYTES_PER_PIXEL_PER_WINDOW,
    _EventSource,
    _extract_shared_chunk_window_to_event_grid,
    _monthly_windows,
    _plan_spatial_groups,
    _plan_temporal_chunks,
    load_agricultural_collector_config,
)
from deforestation_pipeline.raster_grid import derive_raster_grid_spec

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _event_feature(*, event_id: str = "PDE-1", longitude_offset: float = 0.0) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-60.0000 + longitude_offset, -31.0000],
                    [-59.9990 + longitude_offset, -31.0000],
                    [-59.9990 + longitude_offset, -30.9992],
                    [-60.0000 + longitude_offset, -31.0000],
                ]
            ],
        },
        "properties": {"event_id": event_id},
    }


def test_temporal_chunk_plan_stays_below_the_versioned_transport_budget() -> None:
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")
    geometry = shape(_event_feature()["geometry"])
    grid = derive_raster_grid_spec(
        aoi_wgs84=geometry,
        target_crs=config.target_crs,
        resolution_m=float(config.resolution_m),
        nodata=config.raster_nodata,
    )
    windows = _monthly_windows(date(2021, 12, 31), date(2022, 12, 31))

    chunks = _plan_temporal_chunks(
        windows,
        grid=grid,
        maximum_uncompressed_bytes=config.maximum_remote_chunk_uncompressed_bytes,
    )

    assert [window for chunk in chunks for window in chunk] == list(windows)
    assert all(chunk for chunk in chunks)
    assert all(
        grid.width * grid.height * len(chunk) * REMOTE_CHUNK_BYTES_PER_PIXEL_PER_WINDOW
        <= config.maximum_remote_chunk_uncompressed_bytes
        for chunk in chunks
    )


def test_temporal_chunk_plan_uses_six_months_for_one_million_pixels() -> None:
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")
    geometry = shape(_event_feature()["geometry"])
    grid = derive_raster_grid_spec(
        aoi_wgs84=geometry,
        target_crs=config.target_crs,
        resolution_m=float(config.resolution_m),
        nodata=config.raster_nodata,
    ).model_copy(update={"width": 1000, "height": 1000})
    windows = _monthly_windows(date(2021, 12, 31), date(2022, 12, 31))

    chunks = _plan_temporal_chunks(
        windows,
        grid=grid,
        maximum_uncompressed_bytes=24_000_000,
    )

    assert [len(chunk) for chunk in chunks] == [6, 6]


def test_spatial_grouping_is_deterministic_and_never_drops_or_duplicates_events() -> None:
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")
    events = tuple(
        _EventSource(
            event_id=f"PDE-{index:03d}",
            geometry=shape(
                _event_feature(event_id=f"PDE-{index:03d}", longitude_offset=index * 0.03)[
                    "geometry"
                ]
            ),
            feature={},
            onset_end=date(2022, 5, 31),
        )
        for index in reversed(range(7))
    )

    groups = _plan_spatial_groups(
        events,
        target_crs=config.target_crs,
        resolution_m=float(config.resolution_m),
        nodata=config.raster_nodata,
        maximum_grid_pixels=20_000,
    )

    flattened = [event.event_id for group, _ in groups for event in group]
    assert flattened == sorted(flattened)
    assert len(flattened) == len(set(flattened)) == len(events)
    assert all(grid.width * grid.height <= 20_000 for _, grid in groups)


def test_chunk_transport_reconstructs_crop_fraction_from_integer_counts_exactly() -> None:
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")
    geometry = shape(_event_feature()["geometry"])
    grid = derive_raster_grid_spec(
        aoi_wgs84=geometry,
        target_crs=config.target_crs,
        resolution_m=float(config.resolution_m),
        nodata=config.raster_nodata,
    )

    def encode(values: np.ndarray, descriptions: tuple[str, ...], nodata: float) -> bytes:
        with MemoryFile() as memory:
            with memory.open(
                driver="GTiff",
                width=grid.width,
                height=grid.height,
                count=len(descriptions),
                dtype=str(values.dtype),
                crs=grid.target_crs,
                transform=rasterio.Affine(*grid.transform),
                nodata=nodata,
            ) as dataset:
                dataset.write(values)
                dataset.descriptions = descriptions
            return bytes(memory.read())

    counts = np.stack(
        (
            np.full((grid.height, grid.width), 3, dtype=np.uint16),
            np.full((grid.height, grid.width), 1, dtype=np.uint16),
        )
    )
    probability = np.full((1, grid.height, grid.width), 0.7, dtype=np.float32)
    reconstructed = _extract_shared_chunk_window_to_event_grid(
        counts_content=encode(
            counts,
            (
                "2022-06_valid_observation_count",
                "2022-06_qualifying_crop_count",
            ),
            65535,
        ),
        probability_content=encode(probability, ("2022-06_mean_crop_probability",), grid.nodata),
        window_id="2022-06",
        event_grid=grid,
    )

    with MemoryFile(reconstructed) as memory, memory.open() as dataset:
        fraction = dataset.read(indexes=(4,))[0]
    np.testing.assert_allclose(fraction, np.float32(1 / 3), atol=1e-7, rtol=0)
