"""Pruebas del corte vertical local y sus artefactos auditables."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

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
from deforestation_pipeline.local_runner import (
    LocalVectorInputError,
    main,
    run_local_vector_pipeline,
)
from deforestation_pipeline.raster_products import HlsRasterMaterialization

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
        "input/source.geojson",
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
    assert (run_directory / "input/source.geojson").read_bytes() == original_bytes

    summary = json.loads((run_directory / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["establishment_id"] == "test-establishment"
    assert summary["stage"] == "spatial_preparation"
    assert summary["final_assessment_generated"] is False
    assert summary["area"]["total_area_ha"] > 0
    assert summary["area"]["calculation_crs"] == "EPSG:6933"
    assert summary["catalog"]["products"] == ["HLSL30", "HLSS30"]
    assert summary["catalog"]["remote_data_accessed"] is False
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


def test_feature_collection_must_contain_exactly_one_feature(tmp_path: Path) -> None:
    input_path = tmp_path / "multiple.geojson"
    input_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {"type": "Point", "coordinates": [-63, -33]},
                    },
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {"type": "Point", "coordinates": [-62, -32]},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LocalVectorInputError, match="exactamente una Feature"):
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
        metadata=composite_metadata,
    )
    raster_files = {
        "rasters/hls_annual_reflectance.tif": b"TIFF-reflectance",
        "rasters/hls_annual_indices.tif": b"TIFF-indices",
        "rasters/hls_valid_observation_count.tif": b"TIFF-count",
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
    assert captured_materialization["target_crs"] == "EPSG:32720"
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
