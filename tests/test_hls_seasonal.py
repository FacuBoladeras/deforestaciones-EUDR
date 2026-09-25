"""Orquestación estacional HLS sobre contratos temporales explícitos."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from shapely.geometry import box

import deforestation_pipeline.hls_seasonal as hls_seasonal
from deforestation_pipeline.catalog import (
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import SpectralIndex, load_config
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import HlsTemporalCompositeRequest
from deforestation_pipeline.hls_seasonal import (
    HlsSeasonalError,
    HlsSeasonalRequest,
    TemporalCubeIndex,
    build_hls_seasonal_composites,
    hls_seasonal_metadata_json_schema,
    materialize_hls_seasonal_series,
    temporal_cube_index_json_schema,
)
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import HlsRasterMaterialization
from deforestation_pipeline.temporal_cube import (
    build_meteorological_south_window_plan,
    build_temporal_cube_spec,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 7, 28, 15, tzinfo=UTC)
AOI = box(-63.0, -33.0, -62.999, -32.999)
GRID = derive_raster_grid_spec(
    aoi_wgs84=AOI,
    target_crs="EPSG:32720",
    resolution_m=30,
    nodata=-9999,
)


def _inputs() -> tuple[Any, Any]:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    source_plan = build_benchmark_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    return config, source_plan


def test_seasonal_builder_calls_generic_compositor_in_plan_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, source_plan = _inputs()
    window_plan = build_meteorological_south_window_plan(
        start_year=2022,
        end_year=2022,
        generated_at=FIXED_NOW,
    )
    captured: list[HlsTemporalCompositeRequest] = []
    captured_period_ids: list[str] = []

    def fake_build(**kwargs: object) -> object:
        request = cast(HlsTemporalCompositeRequest, kwargs["request"])
        captured.append(request)
        captured_period_ids.append(cast(str, kwargs["period_id"]))
        return SimpleNamespace(
            metadata=SimpleNamespace(
                start_date=request.start_date,
                end_date_exclusive=request.end_date_exclusive,
            )
        )

    monkeypatch.setattr(hls_seasonal, "build_hls_composite", fake_build)

    result = build_hls_seasonal_composites(
        session=GeeSession(module=object()),
        source_plan=source_plan,
        data_config=config.data,
        aoi_wgs84=AOI,
        grid_spec=GRID,
        window_plan=window_plan,
        generated_at=FIXED_NOW,
    )

    assert tuple(item.window.period_id for item in result.items) == (
        "2022-DJF",
        "2022-MAM",
        "2022-JJA",
        "2022-SON",
    )
    assert tuple(request.start_date for request in captured) == tuple(
        window.start_date for window in window_plan.windows
    )
    assert tuple(captured_period_ids) == (
        "2022-DJF",
        "2022-MAM",
        "2022-JJA",
        "2022-SON",
    )
    assert all(request.target_crs == GRID.target_crs for request in captured)
    assert result.cube_spec.window_plan_sha256 == window_plan.plan_sha256
    assert result.cube_spec.spectral_indices == config.data.indices
    assert result.stage == "seasonal_composites_ready"
    assert result.final_assessment_generated is False


def test_seasonal_builder_rejects_grid_and_index_contract_mismatches() -> None:
    config, source_plan = _inputs()
    window_plan = build_meteorological_south_window_plan(
        start_year=2022,
        end_year=2022,
        generated_at=FIXED_NOW,
    )
    wrong_grid = GRID.model_copy(update={"resolution_m": 10})

    with pytest.raises(HlsSeasonalError, match="grid_resolution_mismatch"):
        build_hls_seasonal_composites(
            session=GeeSession(module=object()),
            source_plan=source_plan,
            data_config=config.data,
            aoi_wgs84=AOI,
            grid_spec=wrong_grid,
            window_plan=window_plan,
            generated_at=FIXED_NOW,
        )

    duplicated_indices = config.data.model_copy(
        update={"indices": (SpectralIndex.NDVI, SpectralIndex.NDVI)}
    )
    with pytest.raises(HlsSeasonalError, match="spectral_indices_not_unique"):
        build_hls_seasonal_composites(
            session=GeeSession(module=object()),
            source_plan=source_plan,
            data_config=duplicated_indices,
            aoi_wgs84=AOI,
            grid_spec=GRID,
            window_plan=window_plan,
            generated_at=FIXED_NOW,
        )


def test_seasonal_builder_rejects_composite_outside_requested_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, source_plan = _inputs()
    window_plan = build_meteorological_south_window_plan(
        start_year=2022,
        end_year=2022,
        generated_at=FIXED_NOW,
    )

    def fake_build(**kwargs: object) -> object:
        request = cast(HlsTemporalCompositeRequest, kwargs["request"])
        return SimpleNamespace(
            metadata=SimpleNamespace(
                start_date=request.start_date,
                end_date_exclusive=request.start_date,
            )
        )

    monkeypatch.setattr(hls_seasonal, "build_hls_composite", fake_build)

    with pytest.raises(HlsSeasonalError, match="composite_window_mismatch"):
        build_hls_seasonal_composites(
            session=GeeSession(module=object()),
            source_plan=source_plan,
            data_config=config.data,
            aoi_wgs84=AOI,
            grid_spec=GRID,
            window_plan=window_plan,
            generated_at=FIXED_NOW,
        )


def test_seasonal_request_builds_closed_window_plan() -> None:
    request = HlsSeasonalRequest(start_year=2021, end_year=2022)

    plan = request.build_window_plan(generated_at=FIXED_NOW)

    assert len(plan.windows) == 8
    assert plan.windows[0].period_id == "2021-DJF"
    assert plan.windows[-1].period_id == "2022-SON"


class _FakeMetadata:
    def __init__(self, start: object, end: object) -> None:
        self.start_date = start
        self.end_date_exclusive = end
        self.input_scene_count = 2
        self.sources = (
            SimpleNamespace(product="HLSL30", scene_count=1),
            SimpleNamespace(product="HLSS30", scene_count=1),
        )

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "start_date": str(self.start_date),
            "end_date_exclusive": str(self.end_date_exclusive),
            "input_scene_count": 2,
            "sources": [{"product": "HLSL30"}, {"product": "HLSS30"}],
        }


def test_materialization_publishes_flat_period_assets_and_virtual_cube(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, source_plan = _inputs()
    request = HlsSeasonalRequest(start_year=2022, end_year=2022)
    window_plan = request.build_window_plan(generated_at=FIXED_NOW)
    cube_spec = build_temporal_cube_spec(
        grid_spec=GRID,
        window_plan=window_plan,
        spectral_indices=config.data.indices,
        created_at=FIXED_NOW,
    )
    seasonal_items = tuple(
        hls_seasonal.HlsSeasonalCompositeItem(
            window=window,
            composite=cast(
                Any,
                SimpleNamespace(
                    metadata=_FakeMetadata(window.start_date, window.end_date_exclusive)
                ),
            ),
        )
        for window in window_plan.windows
    )
    monkeypatch.setattr(
        hls_seasonal,
        "build_hls_seasonal_composites",
        lambda **kwargs: hls_seasonal.HlsSeasonalCompositeSeries(
            cube_spec=cube_spec,
            items=seasonal_items,
        ),
    )

    def fake_rasters(**kwargs: object) -> HlsRasterMaterialization:
        assert kwargs["artifact_kind"] == "period"
        return HlsRasterMaterialization(
            files={
                "tiffs/hls_period_reflectance.tif": b"reflectance",
                "tiffs/hls_period_indices.tif": b"indices",
                "tiffs/hls_valid_observation_count.tif": b"total",
                "tiffs/hls_valid_observation_count_l30.tif": b"l30",
                "tiffs/hls_valid_observation_count_s30.tif": b"s30",
                "figures/rgb.png": b"rgb",
            },
            estimates=(),
            validations=(),
            visualizations=(),
            grid_spec=GRID,
        )

    monkeypatch.setattr(hls_seasonal, "materialize_hls_raster_products", fake_rasters)
    captured_qa: dict[str, object] = {}

    def fake_qa(**kwargs: object) -> object:
        captured_qa.update(kwargs)
        return SimpleNamespace(
            files={
                "json/temporal/seasonal/coverage.json": b"{}\n",
                "tables/seasonal/summary.csv": b"period_id\n",
                "figures/seasonal/qa/index_timeseries.png": b"PNG",
                "figures/seasonal/qa/observation_coverage.png": b"PNG",
                "figures/seasonal/qa/rgb_timeline.png": b"PNG",
            },
            coverage=SimpleNamespace(),
            summaries=(),
        )

    monkeypatch.setattr(hls_seasonal, "analyze_seasonal_rasters", fake_qa)

    result = materialize_hls_seasonal_series(
        session=GeeSession(module=object()),
        source_plan=source_plan,
        data_config=config.data,
        output_config=config.output,
        aoi_wgs84=AOI,
        grid_spec=GRID,
        request=request,
        generated_at=FIXED_NOW,
    )

    assert result.metadata.period_ids == (
        "2022-DJF",
        "2022-MAM",
        "2022-JJA",
        "2022-SON",
    )
    assert "tiffs/seasonal/2022-DJF/reflectance.tif" in result.files
    assert "figures/seasonal/2022-SON/rgb.png" in result.files
    assert "json/temporal/seasonal/2022-MAM/composite_metadata.json" in result.files
    assert "json/temporal/seasonal/window_plan.json" in result.files
    assert "json/temporal/seasonal/cube_spec.json" in result.files
    assert "json/temporal/seasonal/cube_index.json" in result.files
    assert "json/temporal/seasonal/coverage.json" in result.files
    assert "tables/seasonal/summary.csv" in result.files
    assert "figures/seasonal/qa/rgb_timeline.png" in result.files
    cube_index = json.loads(result.files["json/temporal/seasonal/cube_index.json"])
    assert cube_index["storage_model"] == "virtual_geotiff_collection"
    assert cube_index["period_axis"] == list(result.metadata.period_ids)
    assert cube_index["reflectance_dimensions"] == [
        "time",
        "reflectance_band",
        "y",
        "x",
    ]
    assert len(cube_index["period_products"]) == 4
    assert cube_index["period_products"][0]["reflectance_raster_sha256"] == (
        "6a601860836ffb9140ccbe88aa62d1cb4efa2c1736be499391495761a343e54c"
    )
    assert not any(path.endswith("_cube.tif") for path in result.files)

    tampered = dict(cube_index)
    tampered["cube_index_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="cube_index_sha256"):
        TemporalCubeIndex.model_validate(tampered)
    assert len(cast(tuple[object, ...], captured_qa["periods"])) == 4


def test_materialization_rejects_total_budget_before_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, source_plan = _inputs()
    called = False

    def unexpected_remote(**kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("no debe consultar GEE")

    monkeypatch.setattr(hls_seasonal, "build_hls_seasonal_composites", unexpected_remote)
    tiny_budget = config.output.model_copy(update={"maximum_series_download_bytes": 1})

    with pytest.raises(HlsSeasonalError, match="seasonal_download_budget_exceeded"):
        materialize_hls_seasonal_series(
            session=GeeSession(module=object()),
            source_plan=source_plan,
            data_config=config.data,
            output_config=tiny_budget,
            aoi_wgs84=AOI,
            grid_spec=GRID,
            request=HlsSeasonalRequest(start_year=2021, end_year=2022),
            generated_at=FIXED_NOW,
        )
    assert called is False


def test_materialization_rejects_nodata_mismatch_before_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, source_plan = _inputs()
    called = False

    def unexpected_remote(**kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("no debe consultar GEE")

    monkeypatch.setattr(hls_seasonal, "build_hls_seasonal_composites", unexpected_remote)
    wrong_grid = GRID.model_copy(update={"nodata": -32768.0})

    with pytest.raises(HlsSeasonalError, match="grid_nodata_mismatch"):
        materialize_hls_seasonal_series(
            session=GeeSession(module=object()),
            source_plan=source_plan,
            data_config=config.data,
            output_config=config.output,
            aoi_wgs84=AOI,
            grid_spec=wrong_grid,
            request=HlsSeasonalRequest(start_year=2022, end_year=2022),
            generated_at=FIXED_NOW,
        )
    assert called is False


@pytest.mark.parametrize(
    ("relative_path", "factory"),
    (
        (
            "data/schemas/hls-seasonal-metadata-v2.0.0.json",
            hls_seasonal_metadata_json_schema,
        ),
        (
            "data/schemas/temporal-cube-index-v2.0.0.json",
            temporal_cube_index_json_schema,
        ),
    ),
)
def test_committed_seasonal_schemas_match_models(
    relative_path: str,
    factory: Callable[[], dict[str, object]],
) -> None:
    committed = json.loads((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    assert committed == factory()
