"""Materialización compacta y QA de la línea base forestal."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from shapely.geometry import box

from deforestation_pipeline.catalog import (
    build_forest_baseline_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.forest_baseline import forest_baseline_output_paths
from deforestation_pipeline.forest_baseline_features import (
    FOREST_BASELINE_FEATURE_BAND_NAMES,
    ForestBaselineFeatureMetadata,
)
from deforestation_pipeline.forest_baseline_materialization import (
    materialize_forest_baseline,
)
from deforestation_pipeline.forest_baseline_products import (
    ForestBaselineImages,
    ForestBaselineProductMetadata,
)
from deforestation_pipeline.forest_rf import (
    ForestRfImages,
    ForestRfMetadata,
    forest_rf_predictor_band_names,
    forest_rf_qa_band_names,
)
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import RasterDownloadError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_AOI = box(-59.01, -33.01, -59.0, -33.0)
TEST_GRID = derive_raster_grid_spec(
    aoi_wgs84=TEST_AOI,
    target_crs="EPSG:32721",
    resolution_m=30,
    nodata=-9999,
)


class DownloadImage:
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier

    def unmask(self, value: float, same_footprint: bool) -> DownloadImage:
        return self

    def toFloat(self) -> DownloadImage:
        return self

    def toInt16(self) -> DownloadImage:
        return self

    def getDownloadURL(self, parameters: dict[str, object]) -> str:
        return f"https://example.invalid/{self.identifier}"


def _rf_images(generated_at: datetime) -> ForestRfImages:
    return ForestRfImages(
        forest_class=DownloadImage("rf-class"),
        forest_vote_fraction=DownloadImage("rf-vote-fraction"),
        input_complete=DownloadImage("rf-input-complete"),
        metadata=ForestRfMetadata(
            generated_at=generated_at,
            asset_id="projects/ee-facuboladerasgee/assets/models/rf_forest_2020",
            asset_type="geemap_tree_feature_collection",
            tree_property="tree",
            expected_tree_count=500,
            jurisdiction="entre_rios",
            reference_year=2020,
            non_forest_class_code=0,
            forest_class_code=1,
            predictor_band_names=forest_rf_predictor_band_names(),
            excluded_qa_band_names=forest_rf_qa_band_names(),
            training_label_semantics="multisource_unanimity_proxy_v1",
            validation_reference=("held_out_spatial_proxy_labels_not_independent_ground_truth"),
            score_semantics="uncalibrated_binary_tree_vote_fraction",
            evidence_role="learned_supporting_evidence",
        ),
    )


def _tiff_bytes(*, band_count: int, dtype: str, value: float) -> bytes:
    values = np.full(
        (band_count, TEST_GRID.height, TEST_GRID.width),
        value,
        dtype=np.float32 if dtype == "float32" else np.int16,
    )
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=TEST_GRID.width,
            height=TEST_GRID.height,
            count=band_count,
            dtype=dtype,
            crs=TEST_GRID.target_crs,
            transform=Affine(*TEST_GRID.transform),
        ) as dataset:
            dataset.write(values)
        return bytes(memory.read())


def test_materialization_publishes_one_flat_evidence_domain() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    source_plan = build_forest_baseline_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    generated_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    images = ForestBaselineImages(
        source_evidence=DownloadImage("source-evidence"),
        evidence_fraction=DownloadImage("fraction"),
        consensus=DownloadImage("consensus"),
        disagreement=DownloadImage("disagreement"),
        source_count=DownloadImage("source-count"),
        features=DownloadImage("features"),
        metadata=ForestBaselineProductMetadata(
            generated_at=generated_at,
            reference_date=date(2020, 12, 31),
            core_source_ids=("jrc_gfc2020_v3", "esa_worldcover_2020_v100"),
            supporting_source_ids=("hansen_gfc_2025_v1_13",),
            deferred_source_ids=("mapbiomas_chaco_deferred",),
            score_semantics="uncalibrated_core_evidence_fraction",
            consensus_rule="all_core_sources_forest_minimum_two",
            resampling_method="nearest_on_declared_target_grid",
            input_scene_count=50,
        ),
    )
    feature_metadata = ForestBaselineFeatureMetadata(
        generated_at=generated_at,
        reference_date=date(2020, 12, 31),
        feature_start_date=date(2019, 1, 1),
        feature_end_date_exclusive=date(2021, 1, 1),
        calendar_years=(2019, 2020),
        input_scene_count=50,
        feature_band_names=FOREST_BASELINE_FEATURE_BAND_NAMES,
        aggregation_method="annual_medians_then_multiannual_mean_and_range",
    )
    responses = {
        "https://example.invalid/source-evidence": _tiff_bytes(
            band_count=3,
            dtype="int16",
            value=1,
        ),
        "https://example.invalid/fraction": _tiff_bytes(
            band_count=1,
            dtype="float32",
            value=1,
        ),
        "https://example.invalid/consensus": _tiff_bytes(
            band_count=1,
            dtype="int16",
            value=1,
        ),
        "https://example.invalid/disagreement": _tiff_bytes(
            band_count=1,
            dtype="int16",
            value=0,
        ),
        "https://example.invalid/source-count": _tiff_bytes(
            band_count=1,
            dtype="int16",
            value=2,
        ),
        "https://example.invalid/features": _tiff_bytes(
            band_count=len(FOREST_BASELINE_FEATURE_BAND_NAMES),
            dtype="float32",
            value=0.5,
        ),
        "https://example.invalid/rf-class": _tiff_bytes(
            band_count=1,
            dtype="int16",
            value=1,
        ),
        "https://example.invalid/rf-vote-fraction": _tiff_bytes(
            band_count=1,
            dtype="float32",
            value=0.9,
        ),
        "https://example.invalid/rf-input-complete": _tiff_bytes(
            band_count=1,
            dtype="int16",
            value=1,
        ),
    }

    result = materialize_forest_baseline(
        images=images,
        rf_images=_rf_images(generated_at),
        feature_metadata=feature_metadata,
        source_plan=source_plan,
        screening_config=config.forest_screening,
        output_config=config.output,
        grid_spec=TEST_GRID,
        fetch_bytes=lambda url, maximum_bytes: responses[url],
    )

    paths = forest_baseline_output_paths()
    assert set(result.files) == set(paths.values())
    assert result.files[paths["qa_figure"]].startswith(b"\x89PNG")
    assert all(len(Path(path).parts) == 3 for path in result.files)
    metadata = json.loads(result.files[paths["metadata"]])
    assert metadata["score_semantics"] == "uncalibrated_core_evidence_fraction"
    assert metadata["calibrated_probability"] is False
    assert metadata["final_assessment_generated"] is False
    assert metadata["grid"]["grid_sha256"] == TEST_GRID.grid_sha256
    assert len(metadata["raster_validations"]) == 9
    assert metadata["screening"]["metrics"]["automated_evaluable_fraction"] == 1.0
    assert metadata["screening"]["metrics"]["automated_forest_fraction"] == 1.0
    assert metadata["screening"]["final_assessment_generated"] is False
    assert metadata["screening"]["automatic_statement_scope"] == (
        "automated_forest_high_confidence"
    )
    assert metadata["screening"]["automatic_statement_policy"] == (
        "only_when_no_signal_in_automated_forest"
    )
    assert result.screening_domain.metrics.automated_evaluable_fraction == 1.0
    assert metadata["random_forest"]["score_semantics"] == (
        "uncalibrated_binary_tree_vote_fraction"
    )
    assert metadata["random_forest"]["calibrated_probability"] is False
    assert metadata["random_forest"]["independent_evidence"] is False
    assert metadata["random_forest"]["assets"] == {
        "class": paths["rf_class"],
        "input_complete": paths["rf_input_complete"],
        "vote_fraction": paths["rf_vote_fraction"],
    }
    assert "signed" not in json.dumps(metadata).lower()


def test_materialization_identifies_the_failing_forest_product() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    source_plan = build_forest_baseline_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    generated_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    images = ForestBaselineImages(
        source_evidence=DownloadImage("source-evidence"),
        evidence_fraction=DownloadImage("fraction"),
        consensus=DownloadImage("consensus"),
        disagreement=DownloadImage("disagreement"),
        source_count=DownloadImage("source-count"),
        features=DownloadImage("features"),
        metadata=ForestBaselineProductMetadata(
            generated_at=generated_at,
            reference_date=date(2020, 12, 31),
            core_source_ids=("jrc_gfc2020_v3", "esa_worldcover_2020_v100"),
            supporting_source_ids=("hansen_gfc_2025_v1_13",),
            deferred_source_ids=("mapbiomas_chaco_deferred",),
            score_semantics="uncalibrated_core_evidence_fraction",
            consensus_rule="all_core_sources_forest_minimum_two",
            resampling_method="nearest_on_declared_target_grid",
            input_scene_count=50,
        ),
    )
    feature_metadata = ForestBaselineFeatureMetadata(
        generated_at=generated_at,
        reference_date=date(2020, 12, 31),
        feature_start_date=date(2019, 1, 1),
        feature_end_date_exclusive=date(2021, 1, 1),
        calendar_years=(2019, 2020),
        input_scene_count=50,
        feature_band_names=FOREST_BASELINE_FEATURE_BAND_NAMES,
        aggregation_method="annual_medians_then_multiannual_mean_and_range",
    )

    with pytest.raises(
        RasterDownloadError,
        match="forest_baseline_features:geotiff_band_has_no_valid_pixels",
    ):
        materialize_forest_baseline(
            images=images,
            rf_images=_rf_images(generated_at),
            feature_metadata=feature_metadata,
            source_plan=source_plan,
            screening_config=config.forest_screening,
            output_config=config.output,
            grid_spec=TEST_GRID,
            fetch_bytes=lambda url, maximum_bytes: _tiff_bytes(
                band_count=len(FOREST_BASELINE_FEATURE_BAND_NAMES),
                dtype="float32",
                value=-9999,
            ),
        )


def test_baseline_paths_keep_screening_inside_flat_evidence_domains() -> None:
    paths = forest_baseline_output_paths()
    forbidden = {"model", "detector", "index", "applicability", "screening"}

    assert len(paths.values()) == len(set(paths.values()))
    assert paths["automated_evaluable"] == "tiffs/evidence/automated_evaluable_2020.tif"
    assert paths["review_required"] == "tiffs/evidence/review_required_2020.tif"
    assert paths["insufficient_data"] == "tiffs/evidence/insufficient_data_2020.tif"
    assert paths["screening_figure"] == "figures/evidence/forest_screening_2020.png"
    assert not any(forbidden & set(Path(path).parts) for path in paths.values())
