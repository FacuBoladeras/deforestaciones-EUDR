"""Pruebas del corte vertical local y sus artefactos auditables."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import geopandas as gpd
import pytest
from shapely.geometry import box

import deforestation_pipeline.local_runner as local_runner_module
from deforestation_pipeline.config import SpectralIndex
from deforestation_pipeline.gee import (
    CollectionMetadataResult,
    GeeMetadataQuery,
    GeeMetadataQueryResult,
    SceneMetadata,
)
from deforestation_pipeline.hls_composite import (
    HlsCompositeImages,
    HlsCompositeInputScene,
    HlsCompositeMetadata,
    HlsCompositeRequest,
    HlsCompositeSourceInventory,
    HlsIndexFormulaRecord,
)
from deforestation_pipeline.hls_series import (
    HlsSeriesMaterialization,
    HlsSeriesMetadata,
    HlsSeriesRequest,
    HlsSeriesYearProductRecord,
)
from deforestation_pipeline.local_runner import (
    main,
    run_local_vector_pipeline,
)
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import HlsRasterMaterialization
from deforestation_pipeline.schemas import RasterGridSpec
from deforestation_pipeline.vector_ingestion import VectorIngestionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yml"
FIXED_NOW = datetime(2026, 7, 23, 18, 30, tzinfo=UTC)


def _write_feature(path: Path) -> bytes:
    payload = {
        "type": "Feature",
        "properties": {"name": "caso sintético"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-63.0, -33.0],
                    [-62.99, -33.0],
                    [-62.99, -32.99],
                    [-63.0, -32.99],
                    [-63.0, -33.0],
                ]
            ],
        },
    }
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(content)
    return content


def test_local_run_writes_complete_auditable_bundle(tmp_path: Path) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    original_bytes = _write_feature(input_path)

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="test-establishment",
        analysis_end_date=date(2026, 7, 23),
        created_at=FIXED_NOW,
    )

    expected_files = {
        "catalog/source_plan.json",
        "config/resolved_config.json",
        "environment.json",
        "geometry/analysis_geometry.geojson",
        "geometry/normalized_wgs84.geojson",
        "geometry/original_interpreted.json",
        "geometry/validation.json",
        "input/converted.geojson",
        "input/ingestion.json",
        "input/original/establecimiento.geojson",
        "manifest.json",
        "measurements/area.json",
        "run_summary.json",
    }
    actual_files = {
        path.relative_to(run_directory).as_posix()
        for path in run_directory.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files
    assert (run_directory / "input/original/establecimiento.geojson").read_bytes() == original_bytes
    converted = json.loads((run_directory / "input/converted.geojson").read_text(encoding="utf-8"))
    assert converted["type"] == "Feature"
    assert converted["geometry"]["type"] == "Polygon"

    summary = json.loads((run_directory / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["establishment_id"] == "test-establishment"
    assert summary["stage"] == "spatial_preparation"
    assert summary["final_assessment_generated"] is False
    assert summary["area"]["total_area_ha"] > 0
    assert summary["area"]["calculation_crs"] == "EPSG:6933"
    assert summary["catalog"]["products"] == ["HLSL30", "HLSS30"]
    assert summary["catalog"]["remote_data_accessed"] is False
    assert summary["input"]["format"] == "GeoJSON"
    assert summary["input"]["feature_count"] == 1
    assert summary["input"]["converted_path"] == "input/converted.geojson"
    assert summary["limitations"]

    source_plan = json.loads(
        (run_directory / "catalog/source_plan.json").read_text(encoding="utf-8")
    )
    assert source_plan["sensor_family"] == "HLS"
    assert [source["collection_id"] for source in source_plan["sources"]] == [
        "NASA/HLS/HLSL30/v002",
        "NASA/HLS/HLSS30/v002",
    ]
    assert source_plan["remote_data_accessed"] is False

    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_self_excluded"] is True
    assert manifest["remote_data_accessed"] is False
    assert manifest["datasets"] == []
    manifested_paths = {artifact["path"] for artifact in manifest["artifacts"]}
    assert manifested_paths == expected_files - {"manifest.json"}
    for artifact in manifest["artifacts"]:
        artifact_path = run_directory / artifact["path"]
        assert artifact["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        assert artifact["size_bytes"] == artifact_path.stat().st_size


def test_runner_converts_a_shapefile_and_preserves_its_sidecars(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "territorio.shp"
    gpd.GeoDataFrame(
        {"name": ["territorio"]},
        geometry=[box(-63, -33, -62.99, -32.99)],
        crs="EPSG:4326",
    ).to_file(input_path, driver="ESRI Shapefile", engine="pyogrio")

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs=None,
        establishment_id="shapefile-test",
        analysis_end_date=date(2026, 7, 23),
        created_at=FIXED_NOW,
    )

    original_names = {path.name for path in (run_directory / "input/original").iterdir()}
    assert {"territorio.shp", "territorio.shx", "territorio.dbf", "territorio.prj"} <= (
        original_names
    )
    summary = json.loads((run_directory / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["input"]["format"] == "ESRI Shapefile"
    assert summary["input"]["source_crs"] == "EPSG:4326"
    assert summary["input"]["source_crs_origin"] == "embedded"
    assert (run_directory / "input/converted.geojson").is_file()


def test_runner_requires_explicit_dissolve_for_multiple_features(tmp_path: Path) -> None:
    input_path = tmp_path / "multiple.geojson"
    input_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-63.0, -33.0],
                                    [-62.995, -33.0],
                                    [-62.995, -32.99],
                                    [-63.0, -32.99],
                                    [-63.0, -33.0],
                                ]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-62.995, -33.0],
                                    [-62.99, -33.0],
                                    [-62.99, -32.99],
                                    [-62.995, -32.99],
                                    [-62.995, -33.0],
                                ]
                            ],
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(VectorIngestionError, match="--dissolve-all"):
        run_local_vector_pipeline(
            input_path=input_path,
            output_root=tmp_path / "outputs",
            config_path=DEFAULT_CONFIG,
            source_crs="EPSG:4326",
            establishment_id="multiple",
            analysis_end_date=date(2026, 7, 23),
            created_at=FIXED_NOW,
        )

    assert not (tmp_path / "outputs").exists()


def test_existing_run_directory_is_never_overwritten(tmp_path: Path) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    _write_feature(input_path)
    run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="test-establishment",
        analysis_end_date=date(2026, 7, 23),
        created_at=FIXED_NOW,
    )

    with pytest.raises(FileExistsError, match="no se sobrescribe"):
        run_local_vector_pipeline(
            input_path=input_path,
            output_root=tmp_path / "outputs",
            config_path=DEFAULT_CONFIG,
            source_crs="EPSG:4326",
            establishment_id="test-establishment",
            analysis_end_date=date(2026, 7, 23),
            created_at=FIXED_NOW,
        )


def test_cli_runs_sample_and_prints_created_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            str(PROJECT_ROOT / "data" / "samples" / "local_test_polygon.geojson"),
            "--output-root",
            str(tmp_path),
            "--establishment-id",
            "cli-test",
            "--analysis-end-date",
            "2026-07-23",
        ]
    )

    run_directory = Path(capsys.readouterr().out.strip())
    assert result == 0
    assert run_directory.parent == tmp_path.resolve()
    assert (run_directory / "manifest.json").is_file()


def test_cli_can_list_the_vector_drivers_available_in_its_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(["--list-vector-formats"])

    output = capsys.readouterr().out
    assert result == 0
    assert "GeoJSON" in output
    assert "ESRI Shapefile" in output
    assert "GPKG" in output


def test_full_pipeline_single_year_remains_backward_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "territorio.gpkg"
    input_path.write_bytes(b"not-read-because-runner-is-mocked")
    captured: dict[str, object] = {}
    expected_directory = tmp_path / "run"

    def fake_run(**kwargs: object) -> Path:
        captured.update(kwargs)
        return expected_directory

    monkeypatch.setattr(local_runner_module, "run_local_vector_pipeline", fake_run)

    result = main(
        [
            str(input_path),
            "--full-pipeline",
            "--hls-year",
            "2023",
            "--layer",
            "parcelas",
            "--dissolve-all",
        ]
    )

    query = cast(GeeMetadataQuery, captured["gee_query"])
    assert result == 0
    assert query.start_date == date(2023, 1, 1)
    assert query.end_date == date(2024, 1, 1)
    assert captured["generate_hls_composite"] is True
    assert captured["hls_series_request"] is None
    assert captured["vector_layer"] == "parcelas"
    assert captured["dissolve_all"] is True
    assert Path(capsys.readouterr().out.strip()) == expected_directory


def test_full_pipeline_accepts_an_inclusive_closed_year_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "territorio.gpkg"
    input_path.write_bytes(b"not-read-because-runner-is-mocked")
    captured: dict[str, object] = {}
    expected_directory = tmp_path / "run"

    def fake_run(**kwargs: object) -> Path:
        captured.update(kwargs)
        return expected_directory

    monkeypatch.setattr(local_runner_module, "run_local_vector_pipeline", fake_run)

    result = main(
        [
            str(input_path),
            "--full-pipeline",
            "--hls-start-year",
            "2019",
            "--hls-end-year",
            "2024",
        ]
    )

    request = cast(HlsSeriesRequest, captured["hls_series_request"])
    assert result == 0
    assert request.years == (2019, 2020, 2021, 2022, 2023, 2024)
    assert captured["gee_query"] is None
    assert captured["generate_hls_composite"] is False
    assert Path(capsys.readouterr().out.strip()) == expected_directory


def test_full_pipeline_requires_a_year_or_complete_range(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        main([str(tmp_path / "territorio.geojson"), "--full-pipeline"])

    assert captured.value.code == 2
    assert "--full-pipeline requiere --hls-year o el rango completo" in (capsys.readouterr().err)


def test_opt_in_gee_query_is_added_to_bundle_without_downloading_pixels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    _write_feature(input_path)
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    remote_result = GeeMetadataQueryResult(
        queried_at=FIXED_NOW,
        start_date=date(2021, 1, 1),
        end_date_exclusive=date(2021, 1, 5),
        max_scenes_per_source=10,
        aoi_sha256="a" * 64,
        collections=(
            CollectionMetadataResult(
                source_id="hls_l30_v2",
                product="HLSL30",
                collection_id="NASA/HLS/HLSL30/v002",
                matched_scene_count=1,
                returned_scene_count=1,
                truncated=False,
                scenes=(
                    SceneMetadata(
                        image_id="L30-A",
                        acquired_at=datetime(2021, 1, 2, tzinfo=UTC),
                        cloud_coverage_pct=12.0,
                    ),
                ),
            ),
            CollectionMetadataResult(
                source_id="hls_s30_v2",
                product="HLSS30",
                collection_id="NASA/HLS/HLSS30/v002",
                matched_scene_count=0,
                returned_scene_count=0,
                truncated=False,
                scenes=(),
            ),
        ),
    )
    monkeypatch.setattr(
        local_runner_module,
        "authenticate_earth_engine",
        lambda path: object(),
    )
    monkeypatch.setattr(
        local_runner_module,
        "query_hls_scene_metadata",
        lambda **kwargs: remote_result,
    )

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="gee-test",
        analysis_end_date=date(2026, 7, 24),
        created_at=FIXED_NOW,
        gee_query=GeeMetadataQuery(
            start_date=date(2021, 1, 1),
            end_date=date(2021, 1, 5),
            max_scenes_per_source=10,
        ),
        gee_credentials_path=credentials_path,
    )

    query_result = json.loads(
        (run_directory / "gee/scene_metadata.json").read_text(encoding="utf-8")
    )
    assert query_result["metadata_only"] is True
    assert "pixels" not in query_result
    summary = json.loads((run_directory / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["remote_data_accessed"] is True
    assert summary["gee"]["metadata_only"] is True
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["remote_data_accessed"] is True
    assert len(manifest["datasets"]) == 2
    assert {dataset["collection"] for dataset in manifest["datasets"]} == {
        "NASA/HLS/HLSL30/v002",
        "NASA/HLS/HLSS30/v002",
    }


def test_annual_hls_composite_adds_rasters_pngs_and_non_final_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    _write_feature(input_path)
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    remote_result = GeeMetadataQueryResult(
        queried_at=FIXED_NOW,
        start_date=date(2023, 1, 1),
        end_date_exclusive=date(2024, 1, 1),
        max_scenes_per_source=10,
        aoi_sha256="a" * 64,
        collections=(
            CollectionMetadataResult(
                source_id="hls_l30_v2",
                product="HLSL30",
                collection_id="NASA/HLS/HLSL30/v002",
                matched_scene_count=1,
                returned_scene_count=1,
                truncated=False,
                scenes=(
                    SceneMetadata(
                        image_id="L30-A",
                        acquired_at=datetime(2023, 6, 1, tzinfo=UTC),
                        cloud_coverage_pct=10.0,
                    ),
                ),
            ),
            CollectionMetadataResult(
                source_id="hls_s30_v2",
                product="HLSS30",
                collection_id="NASA/HLS/HLSS30/v002",
                matched_scene_count=0,
                returned_scene_count=0,
                truncated=False,
                scenes=(),
            ),
        ),
    )
    composite_metadata = HlsCompositeMetadata(
        generated_at=FIXED_NOW,
        start_date=date(2023, 1, 1),
        end_date_exclusive=date(2024, 1, 1),
        period_days=365,
        reducer="median",
        target_crs="EPSG:32720",
        scale_m=30,
        source_native_reflectance_scale_factor=0.0001,
        source_native_reflectance_offset=0.0,
        earth_engine_reflectance_multiplier=1.0,
        earth_engine_reflectance_offset=0.0,
        earth_engine_pixel_value_semantics="surface_reflectance_already_scaled",
        reflectance_band_names=("blue", "green", "red", "nir", "swir1", "swir2"),
        requested_indices=(SpectralIndex.NDVI,),
        index_formulas=(
            HlsIndexFormulaRecord(
                index=SpectralIndex.NDVI,
                formula="(NIR - RED) / (NIR + RED)",
            ),
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
        input_scene_count=1,
        sources=(
            HlsCompositeSourceInventory(
                source_id="hls_l30_v2",
                product="HLSL30",
                collection_id="NASA/HLS/HLSL30/v002",
                spatial_filter_strategy="filterBounds",
                mgrs_tile_ids=("20HNJ",),
                scene_count=1,
                scenes=(
                    HlsCompositeInputScene(
                        image_id="L30-A",
                        acquired_at=datetime(2023, 6, 1, tzinfo=UTC),
                        cloud_coverage_pct=10.0,
                    ),
                ),
            ),
        ),
        ndmi_lswi_equivalent=False,
    )
    composite = HlsCompositeImages(
        reflectance=object(),
        indices=object(),
        valid_observation_count=object(),
        valid_observation_count_l30=object(),
        valid_observation_count_s30=object(),
        metadata=composite_metadata,
    )
    raster_files = {
        "rasters/hls_annual_reflectance.tif": b"TIFF-reflectance",
        "rasters/hls_annual_indices.tif": b"TIFF-indices",
        "rasters/hls_valid_observation_count.tif": b"TIFF-count",
        "rasters/hls_valid_observation_count_l30.tif": b"TIFF-count-l30",
        "rasters/hls_valid_observation_count_s30.tif": b"TIFF-count-s30",
        "figures/rgb.png": b"\x89PNG-rgb",
        "figures/indices/NDVI.png": b"\x89PNG-ndvi",
        "figures/indices_panel.png": b"\x89PNG-panel",
        "figures/valid_observations.png": b"\x89PNG-count",
    }
    materialization = HlsRasterMaterialization(
        files=raster_files,
        estimates=(),
        validations=(),
        visualizations=(),
        grid_spec=derive_raster_grid_spec(
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            target_crs="EPSG:32720",
            resolution_m=30,
            nodata=-9999,
        ),
    )
    captured_build: dict[str, object] = {}
    captured_materialization: dict[str, object] = {}

    monkeypatch.setattr(
        local_runner_module,
        "authenticate_earth_engine",
        lambda path: object(),
    )
    monkeypatch.setattr(
        local_runner_module,
        "query_hls_scene_metadata",
        lambda **kwargs: remote_result,
    )

    def fake_build(**kwargs: object) -> HlsCompositeImages:
        captured_build.update(kwargs)
        return composite

    def fake_materialize(**kwargs: object) -> HlsRasterMaterialization:
        captured_materialization.update(kwargs)
        return materialization

    monkeypatch.setattr(
        local_runner_module,
        "build_hls_annual_composite",
        fake_build,
    )
    monkeypatch.setattr(
        local_runner_module,
        "materialize_hls_raster_products",
        fake_materialize,
    )

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="hls-raster-test",
        analysis_end_date=date(2026, 7, 24),
        created_at=FIXED_NOW,
        gee_query=GeeMetadataQuery(
            start_date=date(2023, 1, 1),
            end_date=date(2024, 1, 1),
            max_scenes_per_source=10,
        ),
        gee_credentials_path=credentials_path,
        generate_hls_composite=True,
    )

    assert captured_build["generated_at"] == FIXED_NOW
    assert cast(HlsCompositeRequest, captured_build["request"]).target_crs == "EPSG:32720"
    grid_spec = captured_materialization["grid_spec"]
    assert grid_spec == materialization.grid_spec
    for relative_path, expected_content in raster_files.items():
        assert (run_directory / relative_path).read_bytes() == expected_content
    assert (run_directory / "gee/composite_input_inventory.json").is_file()
    assert (run_directory / "gee/composite_metadata.json").is_file()

    summary = json.loads((run_directory / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["stage"] == "annual_hls_composite"
    assert summary["remote_data_accessed"] is True
    assert summary["pixel_data_accessed"] is True
    assert summary["final_assessment_generated"] is False
    assert summary["gee"]["metadata_only"] is False
    assert "deforestación" in " ".join(summary["limitations"]).lower()

    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    media_types = {artifact["path"]: artifact["media_type"] for artifact in manifest["artifacts"]}
    assert media_types["rasters/hls_annual_indices.tif"] == "image/tiff"
    assert media_types["figures/rgb.png"] == "image/png"
    assert all(
        "metadata only" not in " ".join(dataset["restrictions"]).lower()
        for dataset in manifest["datasets"]
    )


def test_annual_hls_series_is_published_with_fixed_grid_and_no_final_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    _write_feature(input_path)
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        local_runner_module,
        "authenticate_earth_engine",
        lambda path: object(),
    )

    def fake_series(**kwargs: object) -> HlsSeriesMaterialization:
        captured.update(kwargs)
        grid = cast(RasterGridSpec, kwargs["grid_spec"])
        request = cast(HlsSeriesRequest, kwargs["request"])
        records = tuple(
            HlsSeriesYearProductRecord(
                year=year,
                composite_metadata_path=f"temporal/years/{year}/composite_metadata.json",
                composite_input_inventory_path=(
                    f"temporal/years/{year}/composite_input_inventory.json"
                ),
                reflectance_raster_path=(
                    f"temporal/years/{year}/rasters/hls_annual_reflectance.tif"
                ),
                indices_raster_path=(f"temporal/years/{year}/rasters/hls_annual_indices.tif"),
                valid_observation_count_raster_path=(
                    f"temporal/years/{year}/rasters/hls_valid_observation_count.tif"
                ),
                l30_observation_count_raster_path=(
                    f"temporal/years/{year}/rasters/hls_valid_observation_count_l30.tif"
                ),
                s30_observation_count_raster_path=(
                    f"temporal/years/{year}/rasters/hls_valid_observation_count_s30.tif"
                ),
            )
            for year in request.years
        )
        metadata = HlsSeriesMetadata(
            generated_at=FIXED_NOW,
            start_year=request.start_year,
            end_year=request.end_year,
            years=request.years,
            grid_sha256=grid.grid_sha256,
            source_products=("HLSL30", "HLSS30"),
            annual_composite_method="median_reflectance_then_indices",
            total_estimated_uncompressed_bytes=1,
            year_products=records,
        )
        files = {
            "temporal/grid.json": b'{"schema_version":"1.0.0"}\n',
            "temporal/coverage.json": b'{"years":[]}\n',
            "temporal/series_metadata.json": (
                json.dumps(metadata.model_dump(mode="json")).encode()
            ),
            "tables/hls_annual_summary.csv": b"year,variable\n2020,NDVI\n",
            "figures/temporal/annual_rgb_panel.png": b"\x89PNG-rgb",
            "figures/temporal/index_timeseries.png": b"\x89PNG-index",
            "figures/temporal/observation_coverage.png": b"\x89PNG-coverage",
        }
        return HlsSeriesMaterialization(
            files=files,
            metadata=metadata,
            coverage=(),
            summaries=(),
            annual_items=(),
            grid_spec=grid,
            estimates=(),
        )

    monkeypatch.setattr(
        local_runner_module,
        "materialize_hls_annual_series",
        fake_series,
    )

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="hls-series-test",
        analysis_end_date=date(2026, 7, 24),
        created_at=FIXED_NOW,
        gee_credentials_path=credentials_path,
        hls_series_request=HlsSeriesRequest(start_year=2020, end_year=2021),
    )

    request = cast(HlsSeriesRequest, captured["request"])
    assert request.years == (2020, 2021)
    assert (run_directory / "temporal/grid.json").is_file()
    assert (run_directory / "tables/hls_annual_summary.csv").is_file()
    summary = json.loads((run_directory / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["stage"] == "annual_hls_time_series"
    assert summary["remote_data_accessed"] is True
    assert summary["pixel_data_accessed"] is True
    assert summary["final_assessment_generated"] is False
    assert summary["gee"]["years"] == [2020, 2021]
    captured_grid = cast(RasterGridSpec, captured["grid_spec"])
    assert summary["gee"]["grid_sha256"] == captured_grid.grid_sha256
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    media_types = {item["path"]: item["media_type"] for item in manifest["artifacts"]}
    assert media_types["tables/hls_annual_summary.csv"] == "text/csv"
