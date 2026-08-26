"""Pruebas del corte vertical local y sus artefactos auditables."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import geopandas as gpd
import pytest
from shapely.geometry import box

import deforestation_pipeline.local_runner as local_runner_module
from deforestation_pipeline.config import ForestRandomForestConfig, SpectralIndex, load_config
from deforestation_pipeline.forest_baseline import forest_baseline_output_paths
from deforestation_pipeline.forest_rf_temporal import forest_rf_temporal_output_paths
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
from deforestation_pipeline.hls_seasonal import HlsSeasonalRequest
from deforestation_pipeline.local_runner import (
    _inclusive_period_end_date,
    _observation_years_analysis_end_date,
    _seasonal_request_analysis_end_date,
    main,
    run_local_vector_pipeline,
)
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import HlsRasterMaterialization
from deforestation_pipeline.schemas import RasterGridSpec
from deforestation_pipeline.vector_ingestion import VectorIngestionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yml"
CANDIDATE_FOREST_MODEL_CONFIG = PROJECT_ROOT / "configs" / "rf-forest-entrerios-2020-2024.yml"
FIXED_NOW = datetime(2026, 7, 23, 18, 30, tzinfo=UTC)


def test_seasonal_authoritative_end_uses_closed_djf_and_son_boundaries() -> None:
    assert _inclusive_period_end_date(date(2025, 3, 1)) == date(2025, 2, 28)
    assert _inclusive_period_end_date(date(2025, 12, 1)) == date(2025, 11, 30)
    assert _seasonal_request_analysis_end_date(
        HlsSeasonalRequest(start_year=2020, end_year=2025)
    ) == date(2025, 11, 30)
    assert _observation_years_analysis_end_date((2020, 2021, 2022, 2023, 2024)) == date(
        2024, 11, 30
    )


def _windows_access_denied() -> PermissionError:
    error = PermissionError(13, "directorio bloqueado temporalmente")
    error.winerror = 5
    return error


def test_atomic_publish_retries_transient_windows_access_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".temporary-run"
    target = tmp_path / "published-run"
    attempts: list[tuple[Path, Path]] = []
    delays: list[float] = []

    def fake_rename(path: Path, destination: Path) -> Path:
        attempts.append((path, destination))
        if len(attempts) < 3:
            raise _windows_access_denied()
        return destination

    monkeypatch.setattr(Path, "rename", fake_rename)

    local_runner_module._rename_directory_with_retry(source, target, sleep=delays.append)

    assert attempts == [(source, target), (source, target), (source, target)]
    assert delays == [0.1, 0.2]


def test_atomic_publish_does_not_retry_unrelated_rename_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".temporary-run"
    target = tmp_path / "published-run"
    delays: list[float] = []

    def fail_immediately(path: Path, destination: Path) -> Path:
        raise FileExistsError("destination already exists")

    monkeypatch.setattr(Path, "rename", fail_immediately)

    with pytest.raises(FileExistsError, match="destination already exists"):
        local_runner_module._rename_directory_with_retry(source, target, sleep=delays.append)

    assert delays == []


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
        "json/geometry/analysis.geojson",
        "json/geometry/area.json",
        "json/run/environment.json",
        "json/geometry/normalized_wgs84.geojson",
        "json/geometry/original_interpreted.json",
        "json/geometry/validation.json",
        "json/input/converted.geojson",
        "json/input/ingestion.json",
        "json/run/manifest.json",
        "json/configuration/resolved.json",
        "json/run/summary.json",
        "json/configuration/source_plan.json",
        "source/establecimiento.geojson",
    }
    actual_files = {
        path.relative_to(run_directory).as_posix()
        for path in run_directory.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files
    assert (run_directory / "source/establecimiento.geojson").read_bytes() == original_bytes
    converted = json.loads(
        (run_directory / "json/input/converted.geojson").read_text(encoding="utf-8")
    )
    assert converted["type"] == "Feature"
    assert converted["geometry"]["type"] == "Polygon"

    summary = json.loads((run_directory / "json/run/summary.json").read_text(encoding="utf-8"))
    assert summary["establishment_id"] == "test-establishment"
    assert summary["stage"] == "spatial_preparation"
    assert summary["final_assessment_generated"] is False
    assert summary["area"]["total_area_ha"] > 0
    assert summary["area"]["calculation_crs"] == "EPSG:6933"
    assert summary["catalog"]["products"] == ["HLSL30", "HLSS30"]
    assert summary["catalog"]["remote_data_accessed"] is False
    assert summary["input"]["format"] == "GeoJSON"
    assert summary["input"]["feature_count"] == 1
    assert summary["input"]["converted_path"] == "json/input/converted.geojson"
    assert summary["limitations"]

    source_plan = json.loads(
        (run_directory / "json/configuration/source_plan.json").read_text(encoding="utf-8")
    )
    assert source_plan["sensor_family"] == "HLS"
    assert [source["collection_id"] for source in source_plan["sources"]] == [
        "NASA/HLS/HLSL30/v002",
        "NASA/HLS/HLSS30/v002",
    ]
    assert source_plan["remote_data_accessed"] is False

    manifest = json.loads((run_directory / "json/run/manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_self_excluded"] is True
    assert manifest["remote_data_accessed"] is False
    assert manifest["datasets"] == []
    manifested_paths = {artifact["path"] for artifact in manifest["artifacts"]}
    assert manifested_paths == expected_files - {"json/run/manifest.json"}
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

    original_names = {path.name for path in (run_directory / "source").iterdir()}
    assert {"territorio.shp", "territorio.shx", "territorio.dbf", "territorio.prj"} <= (
        original_names
    )
    summary = json.loads((run_directory / "json/run/summary.json").read_text(encoding="utf-8"))
    assert summary["input"]["format"] == "ESRI Shapefile"
    assert summary["input"]["source_crs"] == "EPSG:4326"
    assert summary["input"]["source_crs_origin"] == "embedded"
    assert (run_directory / "json/input/converted.geojson").is_file()


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
    assert (run_directory / "json/run/manifest.json").is_file()


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
    assert captured["generate_forest_baseline"] is True
    assert captured["generate_disturbance_detection"] is False
    assert captured["hls_seasonal_request"] is None
    assert captured["vector_layer"] == "parcelas"
    assert captured["dissolve_all"] is True
    assert Path(capsys.readouterr().out.strip()) == expected_directory


def test_cli_routes_explicit_rf_candidate_mode_without_promoting_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "territorio.geojson"
    input_path.write_bytes(b"runner-mocked")
    captured: dict[str, object] = {}
    expected_directory = tmp_path / "rf-run"

    def fake_run(**kwargs: object) -> Path:
        captured.update(kwargs)
        return expected_directory

    monkeypatch.setattr(local_runner_module, "run_local_vector_pipeline", fake_run)

    result = main(
        [
            str(input_path),
            "--rf-annual-deltas",
            "--forest-model-config",
            str(CANDIDATE_FOREST_MODEL_CONFIG),
            "--analysis-end-date",
            "2024-12-31",
        ]
    )

    assert result == 0
    assert captured["generate_rf_deltas"] is True
    assert captured["forest_model_config_path"] == CANDIDATE_FOREST_MODEL_CONFIG
    assert captured["generate_forest_baseline"] is False
    assert captured["generate_disturbance_detection"] is False
    assert captured["gee_credentials_path"] == local_runner_module.DEFAULT_GEE_CREDENTIALS_PATH
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

    request = cast(HlsSeasonalRequest, captured["hls_seasonal_request"])
    assert result == 0
    assert request.start_year == 2019
    assert request.end_year == 2024
    assert captured["generate_disturbance_detection"] is True
    assert captured["gee_query"] is None
    assert captured["generate_hls_composite"] is False
    assert captured["generate_forest_baseline"] is True
    assert Path(capsys.readouterr().out.strip()) == expected_directory


def test_full_pipeline_accepts_seasonal_mode_for_a_closed_year_range(
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
            "--seasonal",
            "--hls-start-year",
            "2021",
            "--hls-end-year",
            "2022",
        ]
    )

    request = cast(HlsSeasonalRequest, captured["hls_seasonal_request"])
    assert result == 0
    assert request.start_year == 2021
    assert request.end_year == 2022
    assert captured["gee_query"] is None
    assert captured["generate_forest_baseline"] is True
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
        (run_directory / "json/gee/scene_metadata.json").read_text(encoding="utf-8")
    )
    assert query_result["metadata_only"] is True
    assert "pixels" not in query_result
    summary = json.loads((run_directory / "json/run/summary.json").read_text(encoding="utf-8"))
    assert summary["remote_data_accessed"] is True
    assert summary["gee"]["metadata_only"] is True
    manifest = json.loads((run_directory / "json/run/manifest.json").read_text(encoding="utf-8"))
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
        "tiffs/hls_annual_reflectance.tif": b"TIFF-reflectance",
        "tiffs/hls_annual_indices.tif": b"TIFF-indices",
        "tiffs/hls_valid_observation_count.tif": b"TIFF-count",
        "tiffs/hls_valid_observation_count_l30.tif": b"TIFF-count-l30",
        "tiffs/hls_valid_observation_count_s30.tif": b"TIFF-count-s30",
        "figures/rgb.png": b"\x89PNG-rgb",
        "figures/index_NDVI.png": b"\x89PNG-ndvi",
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
    published_rasters = {
        "tiffs/annual/2023/reflectance.tif": b"TIFF-reflectance",
        "tiffs/annual/2023/indices.tif": b"TIFF-indices",
        "tiffs/annual/2023/observation_count_total.tif": b"TIFF-count",
        "tiffs/annual/2023/observation_count_l30.tif": b"TIFF-count-l30",
        "tiffs/annual/2023/observation_count_s30.tif": b"TIFF-count-s30",
        "figures/annual/2023/rgb.png": b"\x89PNG-rgb",
        "figures/annual/2023/index_NDVI.png": b"\x89PNG-ndvi",
        "figures/annual/2023/indices_panel.png": b"\x89PNG-panel",
        "figures/annual/2023/observation_count.png": b"\x89PNG-count",
    }
    for relative_path, expected_content in published_rasters.items():
        assert (run_directory / relative_path).read_bytes() == expected_content
    top_level_directories = {path.name for path in run_directory.iterdir() if path.is_dir()}
    assert top_level_directories == {"source", "json", "figures", "tiffs"}
    assert (run_directory / "json/temporal/annual/2023/input_inventory.json").is_file()
    assert (run_directory / "json/temporal/annual/2023/composite_metadata.json").is_file()

    summary = json.loads((run_directory / "json/run/summary.json").read_text(encoding="utf-8"))
    assert summary["stage"] == "annual_hls_composite"
    assert summary["remote_data_accessed"] is True
    assert summary["pixel_data_accessed"] is True
    assert summary["final_assessment_generated"] is False
    assert summary["gee"]["metadata_only"] is False
    assert "deforestación" in " ".join(summary["limitations"]).lower()

    manifest = json.loads((run_directory / "json/run/manifest.json").read_text(encoding="utf-8"))
    media_types = {artifact["path"]: artifact["media_type"] for artifact in manifest["artifacts"]}
    assert media_types["tiffs/annual/2023/indices.tif"] == "image/tiff"
    assert media_types["figures/annual/2023/rgb.png"] == "image/png"
    assert all(
        "metadata only" not in " ".join(dataset["restrictions"]).lower()
        for dataset in manifest["datasets"]
    )


def test_local_runner_publishes_seasonal_cube_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    _write_feature(input_path)
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        local_runner_module,
        "authenticate_earth_engine",
        lambda path: SimpleNamespace(module=object()),
    )

    def fake_seasonal(**kwargs: object) -> object:
        grid = cast(RasterGridSpec, kwargs["grid_spec"])
        request = cast(HlsSeasonalRequest, kwargs["request"])
        return SimpleNamespace(
            files={
                "json/temporal/seasonal/window_plan.json": b"{}\n",
                "json/temporal/seasonal/cube_spec.json": b"{}\n",
                "json/temporal/seasonal/cube_index.json": b"{}\n",
                "json/temporal/seasonal/series_metadata.json": b"{}\n",
                "json/temporal/seasonal/coverage.json": b"{}\n",
                "tables/seasonal/summary.csv": b"period_id\n",
                "figures/seasonal/qa/index_timeseries.png": b"PNG",
                "figures/seasonal/qa/observation_coverage.png": b"PNG",
                "figures/seasonal/qa/rgb_timeline.png": b"PNG",
                "tiffs/seasonal/2022-DJF/reflectance.tif": b"TIFF",
                "figures/seasonal/2022-DJF/rgb.png": b"PNG",
            },
            metadata=SimpleNamespace(
                start_year=request.start_year,
                end_year=request.end_year,
                period_ids=("2022-DJF", "2022-MAM", "2022-JJA", "2022-SON"),
                grid_sha256=grid.grid_sha256,
                window_plan_sha256="a" * 64,
            ),
            cube_index=SimpleNamespace(cube_index_sha256="b" * 64),
        )

    monkeypatch.setattr(
        local_runner_module,
        "materialize_hls_seasonal_series",
        fake_seasonal,
    )

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="seasonal-test",
        analysis_end_date=date(2026, 7, 28),
        created_at=datetime(2026, 7, 28, 18, tzinfo=UTC),
        gee_credentials_path=credentials_path,
        hls_seasonal_request=HlsSeasonalRequest(start_year=2022, end_year=2022),
    )

    assert (run_directory / "json/temporal/seasonal/cube_index.json").is_file()
    assert (run_directory / "tiffs/seasonal/2022-DJF/reflectance.tif").is_file()
    summary = json.loads((run_directory / "json/run/summary.json").read_text(encoding="utf-8"))
    assert summary["stage"] == "seasonal_temporal_cube"
    assert summary["remote_data_accessed"] is True
    assert summary["pixel_data_accessed"] is True
    assert summary["gee"]["period_count"] == 4
    assert summary["gee"]["cube_index_sha256"] == "b" * 64
    assert summary["gee"]["rgb_timeline_path"] == "figures/seasonal/qa/rgb_timeline.png"


def test_local_runner_publishes_compact_forest_baseline_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    _write_feature(input_path)
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        local_runner_module,
        "authenticate_earth_engine",
        lambda path: SimpleNamespace(module=object()),
    )
    feature_metadata = SimpleNamespace(
        input_scene_count=50,
        model_dump=lambda mode: {"input_scene_count": 50},
    )
    monkeypatch.setattr(
        local_runner_module,
        "build_forest_baseline_features",
        lambda **kwargs: SimpleNamespace(image=object(), metadata=feature_metadata),
    )
    rf_stack = SimpleNamespace(image=object(), feature_columns=("p0",))
    monkeypatch.setattr(
        local_runner_module,
        "build_yearly_seasonal_feature_stack",
        lambda **kwargs: rf_stack,
    )
    product_metadata = SimpleNamespace(
        model_dump=lambda mode: {
            "score_semantics": "uncalibrated_core_evidence_fraction",
            "calibrated_probability": False,
            "final_assessment_generated": False,
        }
    )
    monkeypatch.setattr(
        local_runner_module,
        "build_forest_baseline_images",
        lambda **kwargs: SimpleNamespace(metadata=product_metadata),
    )
    rf_metadata = SimpleNamespace(
        model_dump=lambda mode: {
            "asset_id": "projects/example/assets/models/rf_forest_2020",
            "score_semantics": "uncalibrated_binary_tree_vote_fraction",
            "calibrated_probability": False,
            "independent_evidence": False,
        }
    )
    rf_images = SimpleNamespace(metadata=rf_metadata)
    monkeypatch.setattr(
        local_runner_module,
        "build_forest_rf_images",
        lambda **kwargs: rf_images,
    )
    paths = forest_baseline_output_paths()
    captured_materialization: dict[str, object] = {}

    def fake_materialize(**kwargs: object) -> object:
        captured_materialization.update(kwargs)
        grid = cast(RasterGridSpec, kwargs["grid_spec"])
        return SimpleNamespace(
            files={
                path: (
                    b"\x89PNG"
                    if path.endswith(".png")
                    else b"TIFF"
                    if path.endswith(".tif")
                    else b"{}\n"
                )
                for path in paths.values()
            },
            grid_spec=grid,
            screening_domain=SimpleNamespace(
                metrics=SimpleNamespace(
                    model_dump=lambda mode: {
                        "automated_evaluable_area_ha": 1.0,
                        "automated_forest_area_ha": 1.0,
                        "review_required_area_ha": 0.0,
                        "insufficient_data_area_ha": 0.0,
                    }
                )
            ),
        )

    monkeypatch.setattr(
        local_runner_module,
        "materialize_forest_baseline",
        fake_materialize,
    )

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="baseline-test",
        analysis_end_date=date(2026, 7, 29),
        created_at=datetime(2026, 7, 29, 18, tzinfo=UTC),
        gee_credentials_path=credentials_path,
        generate_forest_baseline=True,
    )

    assert all((run_directory / path).is_file() for path in paths.values())
    summary = json.loads((run_directory / "json/run/summary.json").read_text(encoding="utf-8"))
    assert summary["stage"] == "forest_baseline_2020"
    assert summary["forest_baseline"]["score_semantics"] == ("uncalibrated_core_evidence_fraction")
    assert summary["forest_baseline"]["random_forest"]["score_semantics"] == (
        "uncalibrated_binary_tree_vote_fraction"
    )
    assert captured_materialization["rf_images"] is rf_images
    assert summary["forest_baseline"]["metadata_path"] == paths["metadata"]
    assert summary["final_assessment_generated"] is False
    assert any(dataset["dataset_id"] == "jrc_gfc2020_v3" for dataset in summary["datasets"])


def test_local_runner_uses_same_2020_stack_for_opt_in_model_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "establecimiento.geojson"
    _write_feature(input_path)
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        local_runner_module,
        "authenticate_earth_engine",
        lambda path: SimpleNamespace(module=object()),
    )
    stacks: dict[int, object] = {}

    def fake_build_stack(**kwargs: object) -> object:
        year = cast(int, kwargs["year"])
        stack = SimpleNamespace(observation_year=year, periods=())
        stacks[year] = stack
        return stack

    rf_calls: list[dict[str, object]] = []

    def fake_build_rf(**kwargs: object) -> object:
        rf_calls.append(dict(kwargs))
        config = cast(ForestRandomForestConfig, kwargs["config"])
        return SimpleNamespace(
            forest_class=object(),
            forest_vote_fraction=object(),
            input_complete=object(),
            metadata=SimpleNamespace(
                model_dump=lambda mode: {
                    "asset_id": config.asset_id,
                    "observation_year": kwargs["observation_year"],
                    "temporal_transfer_validated": False,
                }
            ),
        )

    paths = forest_rf_temporal_output_paths(years=(2020, 2021, 2022, 2023, 2024))

    def fake_materialize(**kwargs: object) -> object:
        grid = cast(RasterGridSpec, kwargs["grid_spec"])
        return SimpleNamespace(
            files={path: b"{}\n" for path in paths.values()},
            grid_spec=grid,
            yearly_summaries=({"year": 2020}, {"year": 2021}),
            model_comparison_2020=SimpleNamespace(summary={"same_input_complete_mask": True}),
        )

    monkeypatch.setattr(
        local_runner_module,
        "build_yearly_seasonal_feature_stack",
        fake_build_stack,
    )
    monkeypatch.setattr(local_runner_module, "build_forest_rf_images", fake_build_rf)
    monkeypatch.setattr(
        local_runner_module,
        "materialize_forest_rf_temporal",
        fake_materialize,
        raising=False,
    )

    run_directory = run_local_vector_pipeline(
        input_path=input_path,
        output_root=tmp_path / "outputs",
        config_path=DEFAULT_CONFIG,
        forest_model_config_path=CANDIDATE_FOREST_MODEL_CONFIG,
        source_crs="EPSG:4326",
        establishment_id="rf-deltas-test",
        analysis_end_date=date(2024, 12, 31),
        created_at=datetime(2026, 8, 6, 18, tzinfo=UTC),
        gee_credentials_path=credentials_path,
        generate_rf_deltas=True,
    )

    assert tuple(stacks) == (2020, 2021, 2022, 2023, 2024)
    candidate_2020 = next(
        call
        for call in rf_calls
        if cast(ForestRandomForestConfig, call["config"]).schema_version == "1.1.0"
        and call["observation_year"] == 2020
    )
    p0_2020 = next(
        call
        for call in rf_calls
        if cast(ForestRandomForestConfig, call["config"]).schema_version == "1.0.0"
    )
    assert candidate_2020["feature_stack"] is p0_2020["feature_stack"] is stacks[2020]
    assert all((run_directory / path).is_file() for path in paths.values())
    summary = json.loads((run_directory / "json/run/summary.json").read_text(encoding="utf-8"))
    assert summary["schema_version"] == "3.5.0"
    assert summary["analysis_end_date"] == "2024-11-30"
    assert summary["requested_analysis_end_date"] == "2024-12-31"
    assert summary["status"] == "review_required"
    assert summary["stage"] == "forest_rf_deltas_2020_2024"
    assert summary["forest_rf_deltas"]["comparison_2020"]["same_input_complete_mask"] is True
    assert summary["forest_rf_deltas"]["temporal_transfer_validated"] is False
    assert summary["forest_rf_deltas"]["scientific_status"] == "review_required"
    default_model = json.loads(
        (run_directory / "json/configuration/resolved.json").read_text(encoding="utf-8")
    )["forest_model"]
    resolved = json.loads(
        (run_directory / "json/configuration/resolved.json").read_text(encoding="utf-8")
    )
    assert resolved["analysis"]["analysis_end_date"] == "2024-11-30"
    assert default_model["asset_id"].endswith("/rf_forest_multiyear_2020_2024_v1")
    assert load_config(DEFAULT_CONFIG).forest_model.asset_id.endswith("/rf_forest_2020")
