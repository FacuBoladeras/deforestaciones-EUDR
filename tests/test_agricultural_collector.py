from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from shapely.geometry import shape
from shapely.ops import unary_union

from deforestation_pipeline.agricultural_collector import (
    DYNAMIC_WORLD_ANNUAL_LABEL_BAND,
    DYNAMIC_WORLD_RASTER_BANDS,
    CollectedAnnualLandCoverRaster,
    CollectedSharedChunkRasters,
    DynamicWorldAnnualLandCoverQuery,
    DynamicWorldSharedChunkQuery,
    DynamicWorldSharedEvent,
    DynamicWorldSharedTemporalWindow,
    DynamicWorldSharedWindowQuery,
    DynamicWorldWindowQuery,
    _monthly_windows,
    _monthly_windows_from_observation_start,
    _parse_events,
    _shared_acquisition_plan,
    collect_dynamic_world_shared_chunk,
    collect_dynamic_world_shared_window,
    collect_dynamic_world_window,
    load_agricultural_collector_config,
    materialize_agricultural_evidence_collection,
)
from deforestation_pipeline.catalog import (
    build_agricultural_evidence_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import ArtifactProfile, load_config
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import RasterDownloadError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREATED = datetime(2026, 8, 11, 15, tzinfo=UTC)
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_agricultural_collector.py"


def _event_feature(
    *,
    event_id: str = "PDE-1",
    onset: bool = True,
    longitude_offset: float = 0.0,
    area_ha: float = 0.5,
    area_threshold_met: bool = True,
) -> dict[str, Any]:
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
        "properties": {
            "event_id": event_id,
            "event_type": "persistent_disturbance_event",
            "estimated_onset_window_end": "2022-05-31" if onset else None,
            "area_ha": area_ha,
            "area_threshold_met": area_threshold_met,
        },
    }


def test_agricultural_windows_start_after_cutoff_even_when_rf_onset_is_later() -> None:
    windows = _monthly_windows_from_observation_start(
        observation_start=date(2021, 1, 1),
        analysis_end=date(2021, 3, 31),
    )
    assert windows == (
        ("2021-01", date(2021, 1, 1), date(2021, 2, 1)),
        ("2021-02", date(2021, 2, 1), date(2021, 3, 1)),
        ("2021-03", date(2021, 3, 1), date(2021, 4, 1)),
    )


def test_production_collector_queries_from_cutoff_not_rf_onset(tmp_path: Path) -> None:
    queries: list[DynamicWorldSharedChunkQuery] = []

    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        queries.append(query)
        return _chunk_result(query)

    _run(
        tmp_path,
        provider,
        analysis_end_date=date(2021, 3, 31),
        post_cutoff_windows=True,
    )

    assert [window.window_id for query in queries for window in query.windows] == [
        "2021-01",
        "2021-02",
        "2021-03",
    ]


def test_production_collector_does_not_require_rf_onset(tmp_path: Path) -> None:
    output = _run(
        tmp_path,
        _chunk_result,
        features=[_event_feature(onset=False)],
        analysis_end_date=date(2021, 1, 31),
        post_cutoff_windows=True,
    )

    document = json.loads((output / "json/evidence/agricultural_evidence.json").read_text("utf-8"))
    assert document["events"][0]["collection_status"] == "collected"
    assert document["events"][0]["estimated_onset_window_end"] is None


def _event_bundle(tmp_path: Path, features: list[dict[str, Any]]) -> Path:
    bundle = tmp_path / "events"
    relative = "json/evidence/disturbance_events.geojson"
    artifact = bundle / relative
    artifact.parent.mkdir(parents=True)
    content = json.dumps({"type": "FeatureCollection", "features": features}).encode()
    artifact.write_bytes(content)
    manifest = {
        "schema_version": "test",
        "analysis_id": "11111111-1111-5111-8111-111111111111",
        "created_at": CREATED.isoformat(),
        "input_sha256": "c" * 64,
        "artifacts": [
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    manifest_path = bundle / "json/run/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def _raster_bytes(
    query: DynamicWorldWindowQuery | DynamicWorldSharedWindowQuery, *, mode: str
) -> bytes:
    height, width = query.grid.height, query.grid.width
    nodata = query.grid.nodata
    values = np.full((4, height, width), nodata, dtype=np.float32)
    if mode == "crop":
        values[0] = 4.0
        values[1] = 4.0
        values[2] = 0.85
        values[3] = 1.0
    elif mode == "unknown":
        values[0] = 4.0
        values[1] = 0.0
        values[2] = 0.25
        values[3] = 0.0
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 4,
        "dtype": "float32",
        "crs": query.grid.target_crs,
        "transform": rasterio.Affine(*query.grid.transform),
        "nodata": nodata,
    }
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            dataset.write(values)
            dataset.descriptions = DYNAMIC_WORLD_RASTER_BANDS
        return bytes(memory.read())


def _chunk_result(
    query: DynamicWorldSharedChunkQuery,
    *,
    modes: dict[str, str] | None = None,
) -> CollectedSharedChunkRasters:
    effective_modes = modes or {window.window_id: "crop" for window in query.windows}
    transported = tuple(
        window.window_id
        for window in query.windows
        if effective_modes.get(window.window_id, "crop") != "nodata"
    )
    matched = {
        window.window_id: {
            event.event_id: (0 if effective_modes.get(window.window_id, "crop") == "nodata" else 4)
            for event in window.events
        }
        for window in query.windows
    }
    if not transported:
        return CollectedSharedChunkRasters(None, None, matched, (), CREATED)
    height, width = query.grid.height, query.grid.width
    count_values = np.full((len(transported) * 2, height, width), 65535, dtype=np.uint16)
    probabilities = np.full((len(transported), height, width), query.grid.nodata, dtype=np.float32)
    count_names: list[str] = []
    probability_names: list[str] = []
    for index, window_id in enumerate(transported):
        mode = effective_modes.get(window_id, "crop")
        count_values[index * 2] = 4
        count_values[index * 2 + 1] = 4 if mode == "crop" else 0
        probabilities[index] = 0.85 if mode == "crop" else 0.25
        count_names.extend(
            (
                f"{window_id}_valid_observation_count",
                f"{window_id}_qualifying_crop_count",
            )
        )
        probability_names.append(f"{window_id}_mean_crop_probability")

    def encode(values: np.ndarray, *, nodata: float, descriptions: list[str]) -> bytes:
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": len(descriptions),
            "dtype": str(values.dtype),
            "crs": query.grid.target_crs,
            "transform": rasterio.Affine(*query.grid.transform),
            "nodata": nodata,
        }
        with MemoryFile() as memory:
            with memory.open(**profile) as dataset:
                dataset.write(values)
                dataset.descriptions = tuple(descriptions)
            return bytes(memory.read())

    return CollectedSharedChunkRasters(
        counts_content=encode(count_values, nodata=65535, descriptions=count_names),
        probability_content=encode(
            probabilities, nodata=query.grid.nodata, descriptions=probability_names
        ),
        matched_scene_counts=matched,
        transported_window_ids=transported,
        retrieved_at=CREATED,
    )


def _run(
    tmp_path: Path,
    provider: Any,
    *,
    features: list[dict[str, Any]] | None = None,
    artifact_profile: ArtifactProfile = ArtifactProfile.LEAN,
    analysis_end_date: date = date(2022, 7, 31),
    annual_land_cover_provider: Any | None = None,
    post_cutoff_windows: bool = False,
) -> Path:
    config_path = PROJECT_ROOT / "configs/agricultural-collector.yml"
    if not post_cutoff_windows:
        legacy_config = config_path.read_text(encoding="utf-8").replace(
            'schema_version: "1.5.0"', 'schema_version: "1.4.0"'
        )
        config_path = tmp_path / "agricultural-collector-v1.4.yml"
        config_path.write_text(legacy_config, encoding="utf-8")
    return materialize_agricultural_evidence_collection(
        source_event_bundle=_event_bundle(tmp_path, features or [_event_feature()]),
        output_root=tmp_path / "out",
        establishment_id="synthetic-field",
        analysis_end_date=analysis_end_date,
        config_path=config_path,
        catalog_path=PROJECT_ROOT / "data/catalog.yml",
        licenses_path=PROJECT_ROOT / "data/licenses.yml",
        created_at=CREATED,
        raster_provider=provider,
        annual_land_cover_provider=annual_land_cover_provider,
        artifact_profile=artifact_profile,
    )


def test_production_collector_config_is_versioned_and_bounded() -> None:
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")

    assert config.schema_version == "1.5.0"
    assert config.source_id == "dynamic_world_v1"
    assert config.target_crs == "EPSG:6933"
    assert config.resolution_m == 10
    assert config.temporal_cadence == "calendar_month"
    assert config.agricultural_observation_start_date == date(2021, 1, 1)
    assert config.maximum_events_per_batch == 100
    assert "maximum_events" not in type(config).model_fields
    assert config.maximum_windows_per_event <= 72
    assert config.maximum_remote_acquisitions == 500
    assert config.maximum_shared_grid_pixels == 1_000_000
    assert config.maximum_remote_chunk_uncompressed_bytes == 24_000_000
    assert config.minimum_top1_probability == pytest.approx(0.6)
    assert config.annual_land_cover_start_year == 2020
    assert config.annual_land_cover_composite == "mode_of_top1_label"


def test_collector_materializes_annual_dynamic_world_mode_series(tmp_path: Path) -> None:
    annual_queries: list[DynamicWorldAnnualLandCoverQuery] = []

    def annual_provider(
        query: DynamicWorldAnnualLandCoverQuery,
    ) -> CollectedAnnualLandCoverRaster:
        annual_queries.append(query)
        values = np.full((1, query.grid.height, query.grid.width), query.year % 9, dtype=np.uint8)
        profile = {
            "driver": "GTiff",
            "width": query.grid.width,
            "height": query.grid.height,
            "count": 1,
            "dtype": "uint8",
            "crs": query.grid.target_crs,
            "transform": rasterio.Affine(*query.grid.transform),
            "nodata": 255,
        }
        with MemoryFile() as memory:
            with memory.open(**profile) as dataset:
                dataset.write(values)
                dataset.descriptions = (DYNAMIC_WORLD_ANNUAL_LABEL_BAND,)
            content = bytes(memory.read())
        return CollectedAnnualLandCoverRaster(
            content=content,
            matched_scene_count=12,
            retrieved_at=CREATED,
        )

    output = _run(
        tmp_path,
        lambda query: _chunk_result(query),
        analysis_end_date=date(2022, 7, 31),
        annual_land_cover_provider=annual_provider,
    )

    assert [query.year for query in annual_queries] == [2020, 2021, 2022]
    annual_tiffs = sorted(output.glob("tiffs/evidence/agricultural/annual_land_cover/*.tif"))
    assert [path.name for path in annual_tiffs] == [
        "dynamic_world_land_cover_2020.tif",
        "dynamic_world_land_cover_2021.tif",
        "dynamic_world_land_cover_2022.tif",
    ]
    with rasterio.open(annual_tiffs[0]) as dataset:
        assert dataset.count == 1
        assert dataset.dtypes == ("uint8",)
        assert dataset.nodata == 255
        assert dataset.descriptions == (DYNAMIC_WORLD_ANNUAL_LABEL_BAND,)
    assert (output / "figures/evidence/dynamic_world_annual_land_cover_2020_2022.png").is_file()
    metadata = json.loads(
        (output / "json/evidence/agricultural_evidence_metadata.json").read_text("utf-8")
    )
    assert metadata["annual_land_cover"]["composite"] == "mode_of_top1_label"
    assert [item["year"] for item in metadata["annual_land_cover"]["rasters"]] == [
        2020,
        2021,
        2022,
    ]


def test_collector_queries_only_post_onset_months_and_materializes_auditable_bundle(
    tmp_path: Path,
) -> None:
    queries: list[DynamicWorldSharedChunkQuery] = []

    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        queries.append(query)
        return _chunk_result(query, modes={"2022-06": "crop", "2022-07": "nodata"})

    output = _run(tmp_path, provider)

    assert [
        (window.start_date, window.end_date_exclusive)
        for item in queries
        for window in item.windows
    ] == [
        (date(2022, 6, 1), date(2022, 7, 1)),
        (date(2022, 7, 1), date(2022, 8, 1)),
    ]
    csv_path = output / "tables/evidence/agricultural_evidence.csv"
    rows = list(csv.DictReader(StringIO(csv_path.read_text(encoding="utf-8"))))
    assert len(rows) == 6
    by_key = {(row["window_id"], row["class_name"]): row for row in rows}
    june_crop = float(by_key[("2022-06", "crop")]["area_ha"])
    june_unknown = float(by_key[("2022-06", "unknown")]["area_ha"])
    june_nodata = float(by_key[("2022-06", "nodata")]["area_ha"])
    july_nodata = float(by_key[("2022-07", "nodata")]["area_ha"])
    event_area = float(by_key[("2022-06", "crop")]["event_area_ha"])
    assert june_crop == pytest.approx(event_area, abs=1e-6)
    assert june_unknown == pytest.approx(0.0, abs=1e-6)
    assert june_nodata == pytest.approx(0.0, abs=1e-6)
    assert july_nodata == pytest.approx(event_area, abs=1e-6)
    assert all(
        sum(
            float(by_key[(window, class_name)]["area_ha"])
            for class_name in ("crop", "unknown", "nodata")
        )
        == pytest.approx(event_area, abs=1e-6)
        for window in ("2022-06", "2022-07")
    )

    metadata = json.loads(
        (output / "json/evidence/agricultural_evidence_metadata.json").read_text("utf-8")
    )
    assert metadata["source"]["source_id"] == "dynamic_world_v1"
    assert metadata["source"]["evidence_role"] == "candidate_independent"
    assert metadata["spatial_method"]["event_footprint"] == (
        "exact_polygon_pixel_intersection_fraction"
    )
    assert metadata["spatial_method"]["resampling"] == "nearest"
    assert metadata["query_count"] == 2
    assert metadata["remote_acquisition_count"] == 2
    assert metadata["remote_chunk_acquisition_count"] == 1
    assert metadata["http_raster_transfer_count"] == 2
    assert (
        metadata["estimated_uncompressed_bytes_new"] * 2
        == metadata["estimated_uncompressed_bytes_legacy"]
    )
    assert metadata["signed_urls_persisted"] is False

    geojson = json.loads(
        (output / "json/evidence/agricultural_evidence.geojson").read_text("utf-8")
    )
    assert len(geojson["features"]) == 1
    assert geojson["features"][0]["properties"]["collection_status"] == "collected"
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    assert manifest["schema_version"] == "1.7.0"
    assert manifest["source_event_bundle"]["input_sha256"] == "c" * 64
    assert manifest["remote_data_accessed"] is True
    assert all((output / item["path"]).is_file() for item in manifest["artifacts"])
    assert any(
        item["path"].endswith("event_footprint_fraction.tif") for item in manifest["artifacts"]
    )
    assert sum(item["path"].endswith("dynamic_world.tif") for item in manifest["artifacts"]) == 2
    assert not any(
        item["path"].startswith("tiffs/evidence/agricultural/_shared/")
        for item in manifest["artifacts"]
    )
    assert metadata["artifact_profile"] == "lean"
    assert all(
        acquisition["rasters_retained"] is False for acquisition in metadata["shared_acquisitions"]
    )
    pngs = [
        item
        for item in manifest["artifacts"]
        if item["path"] == "figures/evidence/agricultural_monthly_evidence_PDE-1.png"
    ]
    assert len(pngs) == 1
    assert manifest["visualizations"][0]["sha256"] == pngs[0]["sha256"]
    assert manifest["visualizations"][0]["selected_window_ids"] == ["2022-06", "2022-07"]
    assert manifest["visualizations"][0]["source_rasters"]


def test_lean_collector_compacts_monthly_counts_without_losing_temporal_rows(
    tmp_path: Path,
) -> None:
    transported_sizes: list[int] = []

    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        result = _chunk_result(query)
        transported_sizes.extend(
            len(content)
            for content in (result.counts_content, result.probability_content)
            if content is not None
        )
        return result

    output = _run(
        tmp_path,
        provider,
        analysis_end_date=date(2022, 12, 31),
    )
    payload = json.loads((output / "json/evidence/agricultural_evidence.json").read_text("utf-8"))
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    metadata = json.loads(
        (output / "json/evidence/agricultural_evidence_metadata.json").read_text("utf-8")
    )
    cube_paths = {row["raster_path"] for row in payload["rows"]}

    assert payload["schema_version"] == "1.1.0"
    assert cube_paths == {"tiffs/evidence/agricultural/PDE-1/temporal_counts_uint16.tif"}
    assert {row["valid_count_band"] for row in payload["rows"]} == set(range(1, 15, 2))
    assert {row["qualifying_count_band"] for row in payload["rows"]} == set(range(2, 16, 2))
    assert sum(item["path"].endswith("_dynamic_world.tif") for item in manifest["artifacts"]) <= 4
    cube = output / next(iter(cube_paths))
    with rasterio.open(cube) as dataset:
        assert dataset.count == 14
        assert dataset.dtypes == ("uint16",) * 14
        assert dataset.nodata == 65535
    retained_temporal_bytes = sum(
        item["size_bytes"]
        for item in manifest["artifacts"]
        if item["path"].endswith("_dynamic_world.tif")
        or item["path"].endswith("temporal_counts_uint16.tif")
    )
    assert retained_temporal_bytes < sum(transported_sizes)
    assert metadata["estimated_legacy_http_raster_transfer_count"] == 7
    assert metadata["http_raster_transfer_count"] == 2
    assert (
        metadata["estimated_uncompressed_bytes_new"] * 2
        == metadata["estimated_uncompressed_bytes_legacy"]
    )


def test_collector_shares_remote_monthly_acquisitions_across_events(
    tmp_path: Path,
) -> None:
    acquisitions: list[DynamicWorldSharedChunkQuery] = []

    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        acquisitions.append(query)
        return _chunk_result(query)

    output = _run(
        tmp_path,
        provider,
        features=[
            _event_feature(event_id="PDE-1"),
            _event_feature(event_id="PDE-2", longitude_offset=0.003),
        ],
    )

    assert [[window.window_id for window in query.windows] for query in acquisitions] == [
        ["2022-06", "2022-07"]
    ]
    assert all(
        {event.event_id for window in query.windows for event in window.events}
        == {"PDE-1", "PDE-2"}
        for query in acquisitions
    )
    metadata = json.loads(
        (output / "json/evidence/agricultural_evidence_metadata.json").read_text("utf-8")
    )
    assert metadata["query_count"] == 4
    assert metadata["remote_acquisition_count"] == 2
    assert metadata["remote_chunk_acquisition_count"] == 1
    assert metadata["event_window_reuse_ratio"] == pytest.approx(4.0)
    assert len(metadata["shared_acquisitions"]) == 1
    payload = json.loads((output / "json/evidence/agricultural_evidence.json").read_text("utf-8"))
    assert metadata["spatial_method"]["shared_acquisition_grids"][0]["grid"]["width"] > max(
        event["grid"]["width"] for event in payload["events"]
    )
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    assert manifest["query_count"] == 4
    assert manifest["remote_acquisition_count"] == 2
    assert manifest["remote_chunk_acquisition_count"] == 1
    assert not any(
        item["path"].startswith("tiffs/evidence/agricultural/_shared/")
        for item in manifest["artifacts"]
    )
    assert sum(item["path"].endswith("dynamic_world.tif") for item in manifest["artifacts"]) == 4


def test_debug_profile_retains_shared_agricultural_transport_rasters(tmp_path: Path) -> None:
    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        return _chunk_result(query)

    output = _run(tmp_path, provider, artifact_profile=ArtifactProfile.DEBUG)
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    metadata = json.loads(
        (output / "json/evidence/agricultural_evidence_metadata.json").read_text("utf-8")
    )

    assert (
        sum(
            item["path"].startswith("tiffs/evidence/agricultural/_shared/")
            for item in manifest["artifacts"]
        )
        == 2
    )
    assert metadata["artifact_profile"] == "debug"
    assert all(
        acquisition["rasters_retained"] is True for acquisition in metadata["shared_acquisitions"]
    )


def test_shared_plan_budgets_unique_months_not_event_month_products() -> None:
    features = [_event_feature(event_id=f"PDE-{index:03d}") for index in range(100)]
    events = _parse_events({"type": "FeatureCollection", "features": features})
    windows = {
        event.event_id: _monthly_windows(event.onset_end, date(2025, 12, 31)) for event in events
    }

    plan = _shared_acquisition_plan(events, windows)

    assert sum(len(items) for items in windows.values()) == 4_300
    assert len(plan) == 43
    assert all(len(participants) == 100 for *_, participants in plan)


def test_collector_batches_122_candidates_without_duplicate_monthly_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    acquisitions: list[DynamicWorldSharedChunkQuery] = []

    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        acquisitions.append(query)
        return _chunk_result(query)

    monkeypatch.setattr(
        collector_module,
        "render_monthly_agricultural_evidence",
        lambda **_: None,
    )
    features = [
        _event_feature(
            event_id=f"PDE-{index:03d}",
            area_ha=0.1 if index % 2 else 0.8,
            area_threshold_met=index % 2 == 0,
        )
        for index in reversed(range(122))
    ]

    output = _run(tmp_path, provider, features=features)

    assert [[window.window_id for window in query.windows] for query in acquisitions] == [
        ["2022-06", "2022-07"]
    ]
    assert all(len(window.events) == 122 for window in acquisitions[0].windows)
    assert [len(batch) for batch in acquisitions[0].windows[0].event_batches] == [100, 22]
    assert [
        event.event_id for batch in acquisitions[0].windows[0].event_batches for event in batch
    ] == [f"PDE-{index:03d}" for index in range(122)]
    payload = json.loads((output / "json/evidence/agricultural_evidence.json").read_text("utf-8"))
    assert len(payload["events"]) == 122
    assert {event["event_id"] for event in payload["events"]} == {
        f"PDE-{index:03d}" for index in range(122)
    }
    assert {
        event["event_id"]
        for event in payload["events"]
        if int(event["event_id"].removeprefix("PDE-")) % 2
    } == {f"PDE-{index:03d}" for index in range(1, 122, 2)}
    assert len(payload["rows"]) == 122 * 2 * 3
    assert all(event["event_area_exact_ha"] > 0 for event in payload["events"])
    assert sum(row["class_name"] == "nodata" for row in payload["rows"]) == 122 * 2
    metadata = json.loads(
        (output / "json/evidence/agricultural_evidence_metadata.json").read_text("utf-8")
    )
    assert metadata["method"] == "dynamic_world_spatial_temporal_chunk_collector_v5"
    assert metadata["query_count"] == 244
    assert metadata["remote_acquisition_count"] == 2
    assert metadata["remote_chunk_acquisition_count"] == 1
    assert metadata["event_batch_count"] == 4
    assert metadata["event_batch_budget_semantics"] == (
        "maximum_events_per_shared_month_scene_count_batch"
    )
    assert all(acquisition["batch_count"] == 4 for acquisition in metadata["shared_acquisitions"])
    assert all(
        [batch["event_count"] for batch in window["event_batches"]] == [100, 22]
        for acquisition in metadata["shared_acquisitions"]
        for window in acquisition["windows"]
    )
    assert len({row["raster_path"] for row in metadata["queries"]}) == 244
    assert all(len(row["grid_sha256"]) == 64 for row in metadata["queries"])
    assert all(len(row["raster_sha256"]) == 64 for row in metadata["queries"])
    assert len({item["acquisition_id"] for item in metadata["shared_acquisitions"]}) == 1
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    assert manifest["schema_version"] == "1.7.0"
    assert manifest["event_batch_count"] == 4
    assert manifest["remote_acquisition_count"] == 2
    assert manifest["remote_chunk_acquisition_count"] == 1
    assert manifest["source_event_bundle"]["manifest_sha256"]
    assert manifest["source_event_bundle"]["event_artifact_sha256"]


def test_collector_preserves_missing_onset_without_querying(tmp_path: Path) -> None:
    calls: list[DynamicWorldSharedChunkQuery] = []

    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        calls.append(query)
        raise AssertionError("no debe consultar un evento sin onset")

    output = _run(tmp_path, provider, features=[_event_feature(onset=False)])

    assert calls == []
    payload = json.loads((output / "json/evidence/agricultural_evidence.json").read_text("utf-8"))
    assert payload["events"][0]["collection_status"] == "insufficient_onset"
    assert payload["events"][0]["quality_flags"] == ["estimated_onset_window_end_missing"]
    assert payload["rows"] == []


def test_collector_rejects_provider_raster_on_wrong_grid(tmp_path: Path) -> None:
    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        result = _chunk_result(query)
        assert result.counts_content is not None
        content = result.counts_content
        with MemoryFile(content) as source_memory:
            with source_memory.open() as source:
                values = source.read()
                profile = source.profile.copy()
        profile["transform"] = rasterio.Affine.translation(10, 0) * profile["transform"]
        with MemoryFile() as target_memory:
            with target_memory.open(**profile) as target:
                target.write(values)
            broken = bytes(target_memory.read())
        return CollectedSharedChunkRasters(
            counts_content=broken,
            probability_content=result.probability_content,
            matched_scene_counts=result.matched_scene_counts,
            transported_window_ids=result.transported_window_ids,
            retrieved_at=result.retrieved_at,
        )

    with pytest.raises(RasterDownloadError, match="grid_transform_mismatch"):
        _run(tmp_path, provider)


def test_collector_rejects_impossible_dynamic_world_counts(tmp_path: Path) -> None:
    def provider(query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        result = _chunk_result(query)
        assert result.counts_content is not None
        height, width = query.grid.height, query.grid.width
        values = np.zeros((len(query.windows) * 2, height, width), dtype=np.uint16)
        values[::2] = 2
        values[1::2] = 3
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": len(query.windows) * 2,
            "dtype": "uint16",
            "crs": query.grid.target_crs,
            "transform": rasterio.Affine(*query.grid.transform),
            "nodata": 65535,
        }
        with MemoryFile() as memory:
            with memory.open(**profile) as dataset:
                dataset.write(values)
                dataset.descriptions = tuple(
                    name
                    for window in query.windows
                    for name in (
                        f"{window.window_id}_valid_observation_count",
                        f"{window.window_id}_qualifying_crop_count",
                    )
                )
            content = bytes(memory.read())
        return CollectedSharedChunkRasters(
            counts_content=content,
            probability_content=result.probability_content,
            matched_scene_counts={
                window.window_id: {event.event_id: 2 for event in window.events}
                for window in query.windows
            },
            transported_window_ids=result.transported_window_ids,
            retrieved_at=result.retrieved_at,
        )

    with pytest.raises(ValueError, match="dynamic_world_raster_values_invalid"):
        _run(tmp_path, provider)


def test_collector_rejects_tampered_source_manifest_artifact(tmp_path: Path) -> None:
    bundle = _event_bundle(tmp_path, [_event_feature()])
    (bundle / "json/evidence/disturbance_events.geojson").write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match=r"source_artifact_(size|sha256)_mismatch"):
        materialize_agricultural_evidence_collection(
            source_event_bundle=bundle,
            output_root=tmp_path / "out",
            establishment_id="synthetic-field",
            analysis_end_date=date(2022, 7, 31),
            config_path=PROJECT_ROOT / "configs/agricultural-collector.yml",
            catalog_path=PROJECT_ROOT / "data/catalog.yml",
            licenses_path=PROJECT_ROOT / "data/licenses.yml",
            created_at=CREATED,
            raster_provider=lambda _: pytest.fail("no debe consultar"),
        )


class _Scalar:
    def __init__(self, value: int) -> None:
        self.value = value

    def getInfo(self) -> int:
        return self.value


class _Image:
    def __init__(self, expression: str) -> None:
        self.expression = expression

    def select(self, band: str) -> _Image:
        return _Image(f"{self.expression}.select({band})")

    def eq(self, value: object) -> _Image:
        return _Image(f"{self.expression}.eq({value})")

    def gte(self, value: object) -> _Image:
        return _Image(f"{self.expression}.gte({value})")

    def And(self, other: _Image) -> _Image:
        return _Image(f"{self.expression}.And({other.expression})")

    def rename(self, name: str) -> _Image:
        return _Image(f"{self.expression}.rename({name})")

    def divide(self, other: _Image) -> _Image:
        return _Image(f"{self.expression}.divide({other.expression})")

    def addBands(self, other: _Image) -> _Image:
        return _Image(f"{self.expression}.addBands({other.expression})")

    def updateMask(self, other: _Image) -> _Image:
        return _Image(f"{self.expression}.updateMask({other.expression})")

    def gt(self, value: object) -> _Image:
        return _Image(f"{self.expression}.gt({value})")

    def toFloat(self) -> _Image:
        return _Image(f"{self.expression}.toFloat()")


class _Collection:
    def __init__(self, module: _FakeEe, expression: str) -> None:
        self.module = module
        self.expression = expression

    def filterBounds(self, geometry: object) -> _Collection:
        self.module.bounds = geometry
        return self

    def filterDate(self, start: str, end: str) -> _Collection:
        self.module.date_filter = (start, end)
        return self

    def size(self) -> _Scalar:
        return _Scalar(self.module.scene_count)

    def select(self, band: str) -> _Collection:
        return _Collection(self.module, f"{self.expression}.select({band})")

    def count(self) -> _Image:
        return _Image(f"{self.expression}.count()")

    def mean(self) -> _Image:
        return _Image(f"{self.expression}.mean()")

    def map(self, function: Any) -> _Collection:
        self.module.mapped_expression = function(_Image("scene")).expression
        return _Collection(self.module, f"{self.expression}.map(qualifying_crop)")

    def sum(self) -> _Image:
        return _Image(f"{self.expression}.sum()")


class _Feature:
    def __init__(self, geometry: object, properties: dict[str, Any]) -> None:
        self._geometry = geometry
        self.properties = dict(properties)

    def geometry(self) -> object:
        return self._geometry

    def set(self, name: str, value: object) -> _Feature:
        resolved = value.value if isinstance(value, _Scalar) else value
        return _Feature(self._geometry, {**self.properties, name: resolved})


class _FeatureCollection:
    def __init__(self, features: list[_Feature]) -> None:
        self.features = features

    def map(self, function: Any) -> _FeatureCollection:
        return _FeatureCollection([function(feature) for feature in self.features])

    def getInfo(self) -> dict[str, object]:
        return {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": feature.properties} for feature in self.features
            ],
        }


class _FakeEe:
    def __init__(self, scene_count: int = 3) -> None:
        self.scene_count = scene_count
        self.collection_id: str | None = None
        self.bounds: object | None = None
        self.date_filter: tuple[str, str] | None = None
        self.mapped_expression: str | None = None
        self.feature_collection_sizes: list[int] = []

    def Geometry(self, payload: object) -> object:
        return payload

    def ImageCollection(self, collection_id: str) -> _Collection:
        self.collection_id = collection_id
        return _Collection(self, collection_id)

    def Image(self, image: _Image) -> _Image:
        return image

    def Feature(self, geometry: object, properties: dict[str, Any] | None = None) -> _Feature:
        if isinstance(geometry, _Feature):
            return geometry
        assert properties is not None
        return _Feature(geometry, properties)

    def FeatureCollection(self, features: list[_Feature]) -> _FeatureCollection:
        self.feature_collection_sizes.append(len(features))
        return _FeatureCollection(features)


def _dynamic_world_query() -> DynamicWorldWindowQuery:
    feature = _event_feature()
    geometry = shape(feature["geometry"])
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")
    source = build_agricultural_evidence_source_plan(
        load_source_catalog(PROJECT_ROOT / "data/catalog.yml")
    ).candidate_source
    grid = derive_raster_grid_spec(
        aoi_wgs84=geometry,
        target_crs=config.target_crs,
        resolution_m=float(config.resolution_m),
        nodata=config.raster_nodata,
    )
    return DynamicWorldWindowQuery(
        event_id="PDE-1",
        window_id="2022-06",
        start_date=date(2022, 6, 1),
        end_date_exclusive=date(2022, 7, 1),
        geometry_wgs84=geometry,
        grid=grid,
        source=source,
        minimum_top1_probability=0.6,
    )


def _dynamic_world_shared_query() -> DynamicWorldSharedWindowQuery:
    first = shape(_event_feature(event_id="PDE-1")["geometry"])
    second = shape(_event_feature(event_id="PDE-2")["geometry"])
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")
    source = build_agricultural_evidence_source_plan(
        load_source_catalog(PROJECT_ROOT / "data/catalog.yml")
    ).candidate_source
    union = unary_union([first, second])
    events = (
        DynamicWorldSharedEvent("PDE-1", first),
        DynamicWorldSharedEvent("PDE-2", second),
    )
    return DynamicWorldSharedWindowQuery(
        window_id="2022-06",
        start_date=date(2022, 6, 1),
        end_date_exclusive=date(2022, 7, 1),
        geometry_wgs84=union,
        grid=derive_raster_grid_spec(
            aoi_wgs84=union,
            target_crs=config.target_crs,
            resolution_m=float(config.resolution_m),
            nodata=config.raster_nodata,
        ),
        events=events,
        event_batches=(events,),
        source=source,
        minimum_top1_probability=0.6,
    )


def _dynamic_world_chunk_query() -> DynamicWorldSharedChunkQuery:
    base = _dynamic_world_shared_query()
    first = DynamicWorldSharedTemporalWindow(
        window_id="2022-06",
        start_date=date(2022, 6, 1),
        end_date_exclusive=date(2022, 7, 1),
        events=base.events,
        event_batches=base.event_batches,
    )
    second = DynamicWorldSharedTemporalWindow(
        window_id="2022-07",
        start_date=date(2022, 7, 1),
        end_date_exclusive=date(2022, 8, 1),
        events=base.events,
        event_batches=base.event_batches,
    )
    return DynamicWorldSharedChunkQuery(
        chunk_id="g001-c001",
        geometry_wgs84=base.geometry_wgs84,
        grid=base.grid,
        windows=(first, second),
        source=base.source,
        minimum_top1_probability=base.minimum_top1_probability,
    )


def test_gee_provider_filters_event_and_builds_top1_crop_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    module = _FakeEe()
    captured: dict[str, Any] = {}

    def materialize(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return type("Result", (), {"content": b"normalized"})()

    monkeypatch.setattr(collector_module, "materialize_ee_image_to_grid", materialize)
    result = collect_dynamic_world_window(
        query=_dynamic_world_query(),
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )

    assert result.content == b"normalized"
    assert result.matched_scene_count == 3
    assert module.collection_id == "GOOGLE/DYNAMICWORLD/V1"
    assert module.bounds is not None
    assert module.date_filter == ("2022-06-01", "2022-07-01")
    assert module.mapped_expression == (
        "scene.select(label).eq(4).And(scene.select(crops).gte(0.6)).rename(qualifying_crop)"
    )
    assert captured["band_names"] == DYNAMIC_WORLD_RASTER_BANDS
    assert captured["grid_spec"].grid_sha256 == _dynamic_world_query().grid.grid_sha256
    assert cast(_Image, captured["image"]).expression.endswith(".toFloat()")


def test_gee_shared_provider_downloads_month_once_and_counts_each_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    module = _FakeEe()
    captured: list[dict[str, Any]] = []

    def materialize(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return type("Result", (), {"content": b"normalized"})()

    monkeypatch.setattr(collector_module, "materialize_ee_image_to_grid", materialize)
    result = collect_dynamic_world_shared_window(
        query=_dynamic_world_shared_query(),
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )

    assert result.content == b"normalized"
    assert result.matched_scene_counts == {"PDE-1": 3, "PDE-2": 3}
    assert len(captured) == 1
    assert captured[0]["artifact_path"] == (
        "tiffs/evidence/agricultural/_shared/2022-06_dynamic_world.tif"
    )
    assert captured[0]["grid_spec"].grid_sha256 == _dynamic_world_shared_query().grid.grid_sha256
    assert module.feature_collection_sizes == [2]


def test_gee_shared_provider_batches_scene_counts_but_downloads_month_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    base = _dynamic_world_shared_query()
    geometry = base.events[0].geometry_wgs84
    events = tuple(
        DynamicWorldSharedEvent(event_id=f"PDE-{index:03d}", geometry_wgs84=geometry)
        for index in range(122)
    )
    query = DynamicWorldSharedWindowQuery(
        window_id=base.window_id,
        start_date=base.start_date,
        end_date_exclusive=base.end_date_exclusive,
        geometry_wgs84=base.geometry_wgs84,
        grid=base.grid,
        events=events,
        event_batches=(events[:100], events[100:]),
        source=base.source,
        minimum_top1_probability=base.minimum_top1_probability,
    )
    module = _FakeEe()
    downloads: list[dict[str, Any]] = []

    def materialize(**kwargs: Any) -> Any:
        downloads.append(kwargs)
        return type("Result", (), {"content": b"normalized"})()

    monkeypatch.setattr(collector_module, "materialize_ee_image_to_grid", materialize)

    result = collect_dynamic_world_shared_window(
        query=query,
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )

    assert len(result.matched_scene_counts) == 122
    assert set(result.matched_scene_counts.values()) == {3}
    assert module.feature_collection_sizes == [100, 22]
    assert len(downloads) == 1


def test_gee_chunk_provider_uses_two_typed_transfers_for_multiple_months(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    module = _FakeEe()
    downloads: list[dict[str, Any]] = []

    def materialize(**kwargs: Any) -> Any:
        downloads.append(kwargs)
        return type("Result", (), {"content": b"normalized"})()

    monkeypatch.setattr(collector_module, "materialize_ee_image_to_grid", materialize)
    result = collect_dynamic_world_shared_chunk(
        query=_dynamic_world_chunk_query(),
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )

    assert result.transported_window_ids == ("2022-06", "2022-07")
    assert result.matched_scene_counts == {
        "2022-06": {"PDE-1": 3, "PDE-2": 3},
        "2022-07": {"PDE-1": 3, "PDE-2": 3},
    }
    assert [download["output_type"] for download in downloads] == ["uint16", "float32"]
    assert downloads[0]["nodata_override"] == 65535.0
    assert len(downloads[0]["band_names"]) == 4
    assert len(downloads[1]["band_names"]) == 2
    assert module.feature_collection_sizes == [2, 2]


def test_gee_providers_resolve_the_automatic_gate_rule_from_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    base = _dynamic_world_shared_query()
    eligible = base.source.class_rules[1].model_copy(
        update={"output_land_use": "crop", "automatic_gate_eligible": True}
    )
    source = base.source.model_copy(update={"class_rules": (eligible,)})
    shared_query = replace(base, source=source)
    chunk_query = replace(_dynamic_world_chunk_query(), source=source)
    window_query = replace(_dynamic_world_query(), source=source)
    module = _FakeEe()
    monkeypatch.setattr(
        collector_module,
        "materialize_ee_image_to_grid",
        lambda **_: type("Result", (), {"content": b"normalized"})(),
    )

    collect_dynamic_world_shared_window(
        query=shared_query,
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )
    assert module.mapped_expression == (
        "scene.select(label).eq(6).And(scene.select(built).gte(0.6)).rename(qualifying_crop)"
    )

    collect_dynamic_world_window(
        query=window_query,
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )
    assert module.mapped_expression == (
        "scene.select(label).eq(6).And(scene.select(built).gte(0.6)).rename(qualifying_crop)"
    )

    collect_dynamic_world_shared_chunk(
        query=chunk_query,
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )
    assert module.mapped_expression == (
        "scene.select(label).eq(6).And(scene.select(built).gte(0.6)).rename(qualifying_crop)"
    )


def test_preflight_budgets_two_raster_transfers_per_temporal_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")
    monkeypatch.setattr(
        collector_module,
        "load_agricultural_collector_config",
        lambda _: config.model_copy(update={"maximum_remote_acquisitions": 1}),
    )

    with pytest.raises(ValueError, match="agricultural_collector_total_query_budget_exceeded"):
        _run(tmp_path, lambda _: pytest.fail("el preflight debe rechazar antes del provider"))


def test_gee_chunk_provider_skips_both_transfers_when_all_months_have_zero_scenes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    module = _FakeEe(scene_count=0)
    monkeypatch.setattr(
        collector_module,
        "materialize_ee_image_to_grid",
        lambda **_: pytest.fail("no debe descargar chunks sin escenas"),
    )

    result = collect_dynamic_world_shared_chunk(
        query=_dynamic_world_chunk_query(),
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )

    assert result.counts_content is None
    assert result.probability_content is None
    assert result.transported_window_ids == ()


def test_gee_provider_skips_download_when_month_has_no_scenes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    module = _FakeEe(scene_count=0)
    monkeypatch.setattr(
        collector_module,
        "materialize_ee_image_to_grid",
        lambda **_: pytest.fail("no debe descargar una colección vacía"),
    )
    result = collect_dynamic_world_window(
        query=_dynamic_world_query(),
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )

    assert result.content is None
    assert result.matched_scene_count == 0


def test_collector_cli_builds_authenticated_provider_and_forwards_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("run_agricultural_collector_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    event_bundle = tmp_path / "events"
    event_bundle.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")
    session = object()
    provider = object()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(script, "authenticate_earth_engine", lambda _: session)
    monkeypatch.setattr(
        script,
        "make_dynamic_world_raster_provider",
        lambda **kwargs: provider if kwargs["session"] is session else pytest.fail("bad session"),
    )

    def materialize(**kwargs: Any) -> Path:
        captured.update(kwargs)
        return tmp_path / "published"

    monkeypatch.setattr(script, "materialize_agricultural_evidence_collection", materialize)
    exit_code = script.main(
        [
            str(event_bundle),
            "--establishment-id",
            "field",
            "--analysis-end-date",
            "2025-12-31",
            "--credentials",
            str(credentials),
        ]
    )

    assert exit_code == 0
    assert captured["source_event_bundle"] == event_bundle
    assert captured["establishment_id"] == "field"
    assert captured["analysis_end_date"] == date(2025, 12, 31)
    assert captured["raster_provider"] is provider
