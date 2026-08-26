"""Pruebas del benchmark Hampel estacional, protegido y reversible."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

from deforestation_pipeline.disturbance_events import segment_persistent_disturbance_events
from deforestation_pipeline.hampel_benchmark import (
    ProtectedHampelConfig,
    apply_protected_hampel,
    load_hampel_benchmark_config,
    materialize_seasonal_hampel_benchmark,
    select_stable_controls,
)
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256

ROOT = Path(__file__).resolve().parents[1]


def _config(**changes: object) -> ProtectedHampelConfig:
    config = load_hampel_benchmark_config(ROOT / "configs" / "hampel-benchmark.yml")
    return config.model_copy(update=changes)


def test_isolated_spike_is_replaced_and_raw_value_is_preserved() -> None:
    result = apply_protected_hampel(
        np.asarray([1.0, 1.0, 1.0, 10.0, 1.0, 1.0, 1.0]),
        config=_config(),
    )

    point = result[3]
    assert point.raw_value == 10.0
    assert point.filtered_value == 1.0
    assert point.outlier_candidate is True
    assert point.replacement_applied is True
    assert point.reason_code == "isolated_spike_replaced"
    assert point.local_median == 1.0
    assert point.local_mad == 0.0
    assert point.window_support_count == 5


def test_persistent_step_is_never_modified() -> None:
    raw = np.asarray([1.0, 1.0, 1.0, 5.0, 5.0, 5.0, 5.0, 5.0])

    result = apply_protected_hampel(raw, config=_config())

    assert np.array_equal(
        np.asarray([point.filtered_value for point in result], dtype=np.float64),
        raw,
    )
    assert not any(point.replacement_applied for point in result)


def test_isolated_spike_on_persistent_step_is_replaced_but_step_remains() -> None:
    result = apply_protected_hampel(
        np.asarray([1.0, 1.0, 1.0, 5.0, 12.0, 5.0, 5.0, 5.0]),
        config=_config(),
    )

    assert result[4].replacement_applied is True
    assert result[4].filtered_value == 5.0
    assert [point.filtered_value for point in result[3:]] == [5.0, 5.0, 5.0, 5.0, 5.0]


def test_edge_candidate_is_flagged_without_replacement() -> None:
    result = apply_protected_hampel(
        np.asarray([10.0, 1.0, 1.0, 1.0, 1.0, 1.0]),
        config=_config(),
    )

    assert result[0].outlier_candidate is True
    assert result[0].replacement_applied is False
    assert result[0].filtered_value == 10.0
    assert result[0].reason_code == "edge_candidate_retained"


def test_nan_is_preserved_without_interpolation() -> None:
    result = apply_protected_hampel(
        np.asarray([1.0, 1.0, np.nan, 9.0, 1.0, 1.0, 1.0]),
        config=_config(),
    )

    assert result[2].raw_value is None
    assert result[2].filtered_value is None
    assert result[2].reason_code == "missing_preserved"


def test_short_series_is_retained_without_filtering() -> None:
    result = apply_protected_hampel(
        np.asarray([1.0, 10.0, 1.0]),
        config=_config(window_size=5),
    )

    assert not any(point.replacement_applied for point in result)
    assert {point.reason_code for point in result} == {"series_too_short"}


def test_hampel_is_deterministic() -> None:
    raw = np.asarray([0.2, 0.2, 0.9, 0.2, np.nan, 0.2, 0.2])
    config = _config()

    first = apply_protected_hampel(raw, config=config)
    second = apply_protected_hampel(raw.copy(), config=config)

    assert first == second


@pytest.mark.parametrize(
    ("field", "value"),
    (("window_size", 4), ("sigma_threshold", 0.0), ("stable_control_count", 0)),
)
def test_invalid_parameters_are_rejected(field: str, value: object) -> None:
    payload = load_hampel_benchmark_config(ROOT / "configs" / "hampel-benchmark.yml").model_dump(
        mode="python"
    )
    payload[field] = value

    with pytest.raises(ValueError):
        ProtectedHampelConfig.model_validate(payload)


def test_stable_control_selection_is_deterministic_spaced_and_strict() -> None:
    stable = np.ones((12, 12), dtype=np.bool_)
    stable[5, 5] = False

    first = select_stable_controls(
        stable,
        grid_sha256="a" * 64,
        requested_count=4,
        radius_pixels=1,
        minimum_center_distance_pixels=3,
    )
    second = select_stable_controls(
        stable.copy(),
        grid_sha256="a" * 64,
        requested_count=4,
        radius_pixels=1,
        minimum_center_distance_pixels=3,
    )

    assert first == second
    assert len(first) == 4
    assert all(control.pixel_count == 9 for control in first)
    assert all(control.unit_id.startswith("CTRL-") for control in first)
    assert all((5, 5) not in control.pixels for control in first)
    for index, left in enumerate(first):
        for right in first[index + 1 :]:
            distance = max(abs(left.row - right.row), abs(left.column - right.column))
            assert distance >= 3


def test_default_config_is_explicitly_seasonal_and_never_activated() -> None:
    config = load_hampel_benchmark_config(ROOT / "configs" / "hampel-benchmark.yml")

    assert config.schema_version == "1.0.0"
    assert config.temporal_granularity == "seasonal_composite_medians"
    assert config.methodological_equivalence_to_visec is False
    assert config.activation_allowed is False
    assert config.activated is False
    assert config.indices == ("NDVI", "EVI2", "NMDI", "LSWI", "NIRv", "kNDVI")
    json.dumps(config.model_dump(mode="json"), sort_keys=True)


def _grid(width: int = 25, height: int = 25) -> RasterGridSpec:
    identity: dict[str, Any] = {
        "target_crs": "EPSG:32721",
        "resolution_m": 30.0,
        "width": width,
        "height": height,
        "transform": (30.0, 0.0, 500000.0, 0.0, -30.0, 6200000.0),
        "bounds": (
            500000.0,
            6200000.0 - height * 30.0,
            500000.0 + width * 30.0,
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


def _write_raster(
    path: Path,
    values: np.ndarray[Any, Any],
    *,
    names: tuple[str, ...],
    grid: RasterGridSpec,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = values[np.newaxis] if values.ndim == 2 else values
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=grid.width,
        height=grid.height,
        count=data.shape[0],
        dtype="float32",
        crs=grid.target_crs,
        transform=Affine(*grid.transform),
        nodata=-9999.0,
    ) as dataset:
        dataset.write(data.astype(np.float32))
        dataset.descriptions = names


def _build_source_bundle(root: Path, input_path: Path) -> Path:
    bundle = root / "source-run"
    grid = _grid()
    evidence = bundle / "tiffs/evidence"
    constant = np.ones((grid.height, grid.width), dtype=np.float32)
    _write_raster(
        evidence / "forest_source_count_2020.tif",
        constant * 2,
        names=("forest_core_source_count_2020",),
        grid=grid,
    )
    _write_raster(
        evidence / "forest_consensus_2020.tif",
        constant,
        names=("forest_consensus_2020",),
        grid=grid,
    )
    _write_raster(
        evidence / "forest_disagreement_2020.tif",
        constant * 0,
        names=("forest_disagreement_2020",),
        grid=grid,
    )
    _write_raster(
        evidence / "forest_evidence_fraction_2020.tif",
        constant,
        names=("forest_evidence_fraction_2020",),
        grid=grid,
    )
    _write_raster(
        evidence / "rf_forest_vote_fraction_2020.tif",
        constant * 0.9,
        names=("rf_forest_vote_fraction_2020",),
        grid=grid,
    )
    _write_raster(
        evidence / "rf_forest_input_complete_2020.tif",
        constant,
        names=("rf_forest_input_complete_2020",),
        grid=grid,
    )
    state = constant.copy()
    state[2:4, 2:4] = 3
    summary = np.zeros((10, grid.height, grid.width), dtype=np.float32)
    summary[0] = state
    _write_raster(
        evidence / "disturbance_summary.tif",
        summary,
        names=(
            "disturbance_state_code",
            "convergence_reason_code",
            "first_anomalous_period_index",
            "maximum_disturbance_magnitude",
            "persistence_period_count",
            "disturbance_score",
            "available_detector_count",
            "agreeing_detector_count",
            "valid_post_cutoff_observation_count",
            "quality_flags_bitmask",
        ),
        grid=grid,
    )
    event = segment_persistent_disturbance_events(
        persistent_mask=state == 3,
        automated_forest_mask=np.ones_like(state, dtype=np.bool_),
        first_anomalous_period_index=summary[2],
        maximum_onset_period_difference=1,
        missing_onset_policy="separate_unknown_episode",
        grid_spec=grid,
        connectivity=8,
        area_threshold_ha=0.5,
    ).events[0]
    period_products: list[dict[str, object]] = []
    indices = ("NDVI", "EVI2", "NMDI", "LSWI", "NIRv", "kNDVI")
    for year in range(2020, 2026):
        for season in ("DJF", "MAM", "JJA", "SON"):
            period_id = f"{year}-{season}"
            relative = f"tiffs/seasonal/{period_id}/indices.tif"
            index_values = np.full((6, grid.height, grid.width), 0.4, dtype=np.float32)
            if year == 2022 and season == "MAM":
                index_values[0, 2:4, 2:4] = 1.2
            _write_raster(bundle / relative, index_values, names=indices, grid=grid)
            period_products.append(
                {
                    "period_id": period_id,
                    "season": season,
                    "season_year": year,
                    "indices_raster_path": relative,
                }
            )
    documents: dict[str, object] = {
        "json/temporal/seasonal/cube_spec.json": {
            "grid": grid.model_dump(mode="json"),
            "composite_method": "median_reflectance_then_indices",
        },
        "json/temporal/seasonal/cube_index.json": {"period_products": period_products},
        "json/configuration/resolved.json": {
            "forest_screening": {
                "schema_version": "1.1.0",
                "minimum_core_source_count": 2,
                "expected_tree_count": 500,
                "forest_vote_fraction_minimum": 0.8,
                "nonforest_vote_fraction_maximum": 0.2,
                "core_nonforest_max_evidence_fraction": 0.0,
                "forest_rule": (
                    "rf_complete_and_core_sufficient_and_unanimous_forest_and_vote_gte_threshold"
                ),
                "nonforest_rule": (
                    "rf_complete_and_core_sufficient_and_unanimous_nonforest_and_vote_lte_threshold"
                ),
                "temporal_signal_scope": (
                    "entire_aoi_primary_interpretation_automated_forest_only"
                ),
                "score_semantics": "uncalibrated_binary_tree_vote_fraction",
                "automatic_final_assessment_allowed": False,
            },
            "disturbance_events": {
                "connectivity": 8,
                "visec_operational_event_area_threshold_ha": 0.5,
                "maximum_onset_period_difference": 1,
                "missing_onset_policy": "separate_unknown_episode",
            },
        },
        "json/evidence/disturbance_events.json": {"events": [{"event_id": event.event_id}]},
        "json/input/ingestion.json": {
            "source_files": [
                {
                    "name": input_path.name,
                    "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                }
            ]
        },
        "json/run/summary.json": {"limitations": [], "final_assessment_generated": False},
    }
    for relative, payload in documents.items():
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload), encoding="utf-8")
    artifacts = []
    for path in sorted(item for item in bundle.rglob("*") if item.is_file()):
        relative = path.relative_to(bundle).as_posix()
        content = path.read_bytes()
        artifacts.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "media_type": "application/octet-stream",
            }
        )
    manifest = {
        "schema_version": "3.1.0",
        "analysis_id": "00000000-0000-5000-8000-000000000001",
        "run_id": "source-run",
        "establishment_id": "source",
        "scientific_parameters_hash": "a" * 64,
        "execution_config_hash": "b" * 64,
        "remote_data_accessed": True,
        "pixel_data_accessed": True,
        "datasets": [],
        "artifacts": artifacts,
    }
    manifest_path = bundle / "json/run/manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_derived_bundle_is_immutable_self_contained_and_manifested(tmp_path: Path) -> None:
    input_path = tmp_path / "costa-uru.geojson"
    input_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    source = _build_source_bundle(tmp_path, input_path)
    source_manifest_before = (source / "json/run/manifest.json").read_bytes()
    payload = load_hampel_benchmark_config(ROOT / "configs" / "hampel-benchmark.yml").model_dump(
        mode="python"
    )
    payload["stable_control_count"] = 2
    config = ProtectedHampelConfig.model_validate(payload)

    result = materialize_seasonal_hampel_benchmark(
        source_bundle=source,
        output_root=tmp_path / "outputs",
        input_geojson=input_path,
        establishment_id="hampel-test",
        config=config,
        created_at=datetime(2026, 8, 4, 22, 30, tzinfo=UTC),
    )

    assert (source / "json/run/manifest.json").read_bytes() == source_manifest_before
    assert {path.name for path in result.iterdir()} == {
        "figures",
        "json",
        "tables",
        "tiffs",
    }
    metadata = json.loads((result / "json/evidence/seasonal_hampel_benchmark.json").read_text())
    assert metadata["cohorts"]["persistent_event"]["unit_count"] == 1
    assert metadata["cohorts"]["stable_control"]["unit_count"] == 2
    assert metadata["activation"]["activated"] is False
    assert metadata["methodological_equivalence_to_visec"] is False
    source_metadata = metadata["source_bundle"]
    assert source_metadata["validated_source_artifact_count"] == len(
        json.loads((source / "json/run/manifest.json").read_text())["artifacts"]
    )
    assert source_metadata["reused_unchanged_artifact_count"] == (
        source_metadata["validated_source_artifact_count"] - 1
    )
    assert source_metadata["transformed_artifact_count"] == 1
    assert source_metadata["transformed_source_artifacts"] == ["json/run/summary.json"]
    assert source_metadata["all_final_artifacts_rehashed"] is True
    assert "all_source_artifacts_reused_and_rehashed" not in source_metadata
    source_artifacts = json.loads((source / "json/run/manifest.json").read_text())["artifacts"]
    for artifact in source_artifacts:
        path = artifact["path"]
        if path == "json/run/summary.json":
            assert (result / path).read_bytes() != (source / path).read_bytes()
        else:
            assert (result / path).read_bytes() == (source / path).read_bytes()
    manifest = json.loads((result / "json/run/manifest.json").read_text())
    assert manifest["hampel_activated"] is False
    assert manifest["schema_version"] == "3.2.0"
    for artifact in manifest["artifacts"]:
        content = (result / artifact["path"]).read_bytes()
        assert len(content) == artifact["size_bytes"]
        assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
