"""Serie HLS multianual, QA local y productos temporales del Paso 11."""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from pydantic import ValidationError
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from shapely.geometry import box

import deforestation_pipeline.hls_series as hls_series_module
from deforestation_pipeline.catalog import BenchmarkSourcePlan
from deforestation_pipeline.config import SpectralIndex, load_config
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import (
    HlsCompositeImages,
    HlsCompositeInputScene,
    HlsCompositeMetadata,
    HlsCompositeRequest,
    HlsCompositeSourceInventory,
    HlsIndexFormulaRecord,
)
from deforestation_pipeline.hls_series import (
    HlsSeriesError,
    HlsSeriesRequest,
    hls_series_coverage_json_schema,
    hls_series_metadata_json_schema,
    materialize_hls_annual_series,
)
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import HlsRasterMaterialization
from deforestation_pipeline.spectral_indices import INDEX_FORMULAS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 7, 27, 15, tzinfo=UTC)
AOI = box(-63.0, -33.0, -62.999, -32.999)
GRID = derive_raster_grid_spec(
    aoi_wgs84=AOI,
    target_crs="EPSG:32720",
    resolution_m=30,
    nodata=-9999,
)


def _geotiff(
    values: np.ndarray[Any, Any],
    *,
    band_names: tuple[str, ...],
    dtype: str,
) -> bytes:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=GRID.width,
            height=GRID.height,
            count=len(band_names),
            dtype=dtype,
            crs=GRID.target_crs,
            transform=Affine(*GRID.transform),
            nodata=GRID.nodata,
        ) as dataset:
            dataset.write(values)
            dataset.descriptions = band_names
        return bytes(memory.read())


def _metadata(year: int, indices: tuple[SpectralIndex, ...]) -> HlsCompositeMetadata:
    inventories = tuple(
        HlsCompositeSourceInventory(
            source_id=f"hls_{product.lower()}_v2",
            product=product,
            collection_id=f"NASA/HLS/{product}/v002",
            spatial_filter_strategy=("filterBounds" if product == "HLSL30" else "MGRS_TILE_ID"),
            mgrs_tile_ids=("20HNJ",),
            scene_count=1,
            scenes=(
                HlsCompositeInputScene(
                    image_id=f"T20HNJ_{year}0601T140000_{product}",
                    acquired_at=datetime(year, 6, 1, 14, tzinfo=UTC),
                    cloud_coverage_pct=10,
                ),
            ),
        )
        for product in ("HLSL30", "HLSS30")
    )
    return HlsCompositeMetadata(
        generated_at=FIXED_NOW,
        start_date=date(year, 1, 1),
        end_date_exclusive=date(year + 1, 1, 1),
        period_days=(date(year + 1, 1, 1) - date(year, 1, 1)).days,
        reducer="median",
        target_crs=GRID.target_crs,
        scale_m=30,
        source_native_reflectance_scale_factor=0.0001,
        source_native_reflectance_offset=0,
        earth_engine_reflectance_multiplier=1,
        earth_engine_reflectance_offset=0,
        earth_engine_pixel_value_semantics="surface_reflectance_already_scaled",
        reflectance_band_names=("blue", "green", "red", "nir", "swir1", "swir2"),
        requested_indices=indices,
        index_formulas=tuple(
            HlsIndexFormulaRecord(index=index, formula=INDEX_FORMULAS[index]) for index in indices
        ),
        indices_derived_from="annual_median_reflectance",
        masked_fmask_classes=(
            "fill",
            "cloud",
            "adjacent_to_cloud_or_shadow",
            "cloud_shadow",
            "snow_or_ice",
            "high_aerosol",
        ),
        water_preserved=True,
        complete_scene_inventory=True,
        input_scene_count=2,
        sources=inventories,
        ndmi_lswi_equivalent=(SpectralIndex.NDMI in indices and SpectralIndex.LSWI in indices),
    )


def _annual_product(year: int, indices: tuple[SpectralIndex, ...]) -> HlsCompositeImages:
    return HlsCompositeImages(
        reflectance=object(),
        indices=object(),
        valid_observation_count=object(),
        valid_observation_count_l30=object(),
        valid_observation_count_s30=object(),
        metadata=_metadata(year, indices),
    )


def _annual_materialization(
    year: int,
    indices: tuple[SpectralIndex, ...],
) -> HlsRasterMaterialization:
    reflectance = np.stack(
        [
            np.full((GRID.height, GRID.width), value, dtype=np.float32)
            for value in (0.08, 0.10, 0.12, 0.30, 0.20, 0.15)
        ]
    )
    index_values = np.stack(
        [
            np.full(
                (GRID.height, GRID.width),
                0.1 + position * 0.03 + (year - 2020) * 0.02,
                dtype=np.float32,
            )
            for position, _ in enumerate(indices)
        ]
    )
    total_count = 10 if year == 2020 else 0
    files = {
        "tiffs/hls_annual_reflectance.tif": _geotiff(
            reflectance,
            band_names=("blue", "green", "red", "nir", "swir1", "swir2"),
            dtype="float32",
        ),
        "tiffs/hls_annual_indices.tif": _geotiff(
            index_values,
            band_names=tuple(index.value for index in indices),
            dtype="float32",
        ),
        "tiffs/hls_valid_observation_count.tif": _geotiff(
            np.full((1, GRID.height, GRID.width), total_count, dtype=np.int16),
            band_names=("valid_observation_count",),
            dtype="int16",
        ),
        "tiffs/hls_valid_observation_count_l30.tif": _geotiff(
            np.full(
                (1, GRID.height, GRID.width),
                6 if year == 2020 else 0,
                dtype=np.int16,
            ),
            band_names=("valid_observation_count_l30",),
            dtype="int16",
        ),
        "tiffs/hls_valid_observation_count_s30.tif": _geotiff(
            np.full(
                (1, GRID.height, GRID.width),
                4 if year == 2020 else 0,
                dtype=np.int16,
            ),
            band_names=("valid_observation_count_s30",),
            dtype="int16",
        ),
        "figures/rgb.png": b"\x89PNG-annual-rgb",
    }
    return HlsRasterMaterialization(
        files=files,
        estimates=(),
        validations=(),
        visualizations=(),
        grid_spec=GRID,
    )


def test_series_request_is_an_inclusive_closed_year_range() -> None:
    request = HlsSeriesRequest(start_year=2019, end_year=2024)

    assert request.years == (2019, 2020, 2021, 2022, 2023, 2024)

    with pytest.raises(ValidationError, match="start_year"):
        HlsSeriesRequest(start_year=2024, end_year=2023)
    with pytest.raises(ValidationError, match="2015"):
        HlsSeriesRequest(start_year=2014, end_year=2020)


def test_series_materialization_prefixes_years_and_generates_qa_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    built_years: list[int] = []

    def fake_build(**kwargs: object) -> HlsCompositeImages:
        request = cast(HlsCompositeRequest, kwargs["request"])
        year = request.start_date.year
        built_years.append(year)
        return _annual_product(year, config.data.indices)

    def fake_materialize(**kwargs: object) -> HlsRasterMaterialization:
        product = cast(HlsCompositeImages, kwargs["product"])
        year = product.metadata.start_date.year
        assert kwargs["grid_spec"] == GRID
        return _annual_materialization(year, config.data.indices)

    monkeypatch.setattr(hls_series_module, "build_hls_annual_composite", fake_build)
    monkeypatch.setattr(
        hls_series_module,
        "materialize_hls_raster_products",
        fake_materialize,
    )

    result = materialize_hls_annual_series(
        session=cast(GeeSession, object()),
        plan=cast(BenchmarkSourcePlan, object()),
        data_config=config.data,
        output_config=config.output,
        aoi_wgs84=AOI,
        grid_spec=GRID,
        request=HlsSeriesRequest(start_year=2020, end_year=2021),
        generated_at=FIXED_NOW,
    )

    assert built_years == [2020, 2021]
    assert result.metadata.years == (2020, 2021)
    assert result.metadata.grid_sha256 == GRID.grid_sha256
    assert result.metadata.final_assessment_generated is False
    assert result.coverage[0].coverage_state == "complete_observed_coverage"
    assert result.coverage[0].valid_pixel_fraction_total == 1
    assert result.coverage[0].total_count.median == 10
    assert result.coverage[1].coverage_state == "no_observed_coverage"
    assert result.coverage[1].valid_pixel_fraction_total == 0
    assert "no_total_valid_observations" in result.coverage[1].quality_flags

    expected_paths = {
        "json/temporal/annual/grid.json",
        "json/temporal/annual/series_metadata.json",
        "json/temporal/annual/coverage.json",
        "tables/annual/summary.csv",
        "figures/annual/qa/rgb_panel.png",
        "figures/annual/qa/index_timeseries.png",
        "figures/annual/qa/observation_coverage.png",
    }
    for year in (2020, 2021):
        expected_paths.update(
            {
                f"json/temporal/annual/{year}/composite_metadata.json",
                f"json/temporal/annual/{year}/input_inventory.json",
                f"tiffs/annual/{year}/reflectance.tif",
                f"tiffs/annual/{year}/indices.tif",
                f"tiffs/annual/{year}/observation_count_total.tif",
                f"tiffs/annual/{year}/observation_count_l30.tif",
                f"tiffs/annual/{year}/observation_count_s30.tif",
                f"figures/annual/{year}/rgb.png",
            }
        )
    assert set(result.files) == expected_paths
    assert all(
        result.files[path].startswith(b"\x89PNG")
        for path in (
            "figures/annual/qa/rgb_panel.png",
            "figures/annual/qa/index_timeseries.png",
            "figures/annual/qa/observation_coverage.png",
        )
    )

    grid_payload = json.loads(result.files["json/temporal/annual/grid.json"])
    assert grid_payload["grid_sha256"] == GRID.grid_sha256
    coverage_payload = json.loads(result.files["json/temporal/annual/coverage.json"])
    assert [item["year"] for item in coverage_payload["years"]] == [2020, 2021]
    rows = list(csv.DictReader(StringIO(result.files["tables/annual/summary.csv"].decode("utf-8"))))
    assert len(rows) == 2 * (6 + len(config.data.indices))
    assert {row["variable_kind"] for row in rows} == {"reflectance", "index"}


def test_series_preflight_rejects_total_budget_before_remote_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    tiny_budget = config.output.model_copy(update={"maximum_series_download_bytes": 1})
    called = False

    def forbidden_build(**kwargs: object) -> HlsCompositeImages:
        nonlocal called
        called = True
        raise AssertionError("no debe construir composites después de fallar el preflight")

    monkeypatch.setattr(hls_series_module, "build_hls_annual_composite", forbidden_build)

    with pytest.raises(HlsSeriesError, match="series_download_budget_exceeded"):
        materialize_hls_annual_series(
            session=cast(GeeSession, object()),
            plan=cast(BenchmarkSourcePlan, object()),
            data_config=config.data,
            output_config=tiny_budget,
            aoi_wgs84=AOI,
            grid_spec=GRID,
            request=HlsSeriesRequest(start_year=2020, end_year=2021),
            generated_at=FIXED_NOW,
        )

    assert called is False


def test_series_rejects_an_open_calendar_year_before_remote_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    called = False

    def forbidden_build(**kwargs: object) -> HlsCompositeImages:
        nonlocal called
        called = True
        raise AssertionError("no debe construir un año calendario todavía abierto")

    monkeypatch.setattr(hls_series_module, "build_hls_annual_composite", forbidden_build)

    with pytest.raises(HlsSeriesError, match="series_requires_closed_calendar_years"):
        materialize_hls_annual_series(
            session=cast(GeeSession, object()),
            plan=cast(BenchmarkSourcePlan, object()),
            data_config=config.data,
            output_config=config.output,
            aoi_wgs84=AOI,
            grid_spec=GRID,
            request=HlsSeriesRequest(start_year=2025, end_year=2026),
            generated_at=FIXED_NOW,
        )

    assert called is False


@pytest.mark.parametrize(
    ("relative_path", "schema_factory"),
    (
        (
            "data/schemas/hls-series-metadata-v3.0.0.json",
            hls_series_metadata_json_schema,
        ),
        (
            "data/schemas/hls-series-coverage-v1.0.0.json",
            hls_series_coverage_json_schema,
        ),
    ),
)
def test_committed_hls_series_schemas_match_pydantic_models(
    relative_path: str,
    schema_factory: Any,
) -> None:
    committed = json.loads((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))

    assert committed == schema_factory()
