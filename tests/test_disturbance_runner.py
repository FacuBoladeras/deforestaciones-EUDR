"""Integración local vigente de detección de perturbaciones y publicación atómica."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

import deforestation_pipeline.local_runner as runner
from deforestation_pipeline.ccdc_benchmark import CCDC_SCALAR_SUMMARY_BAND_NAMES
from deforestation_pipeline.change_detection import (
    DISTURBANCE_SUMMARY_BAND_NAMES,
    DisturbanceStateCode,
    disturbance_detection_output_paths,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.disturbance_materialization import (
    DisturbancePeriodRaster,
)
from deforestation_pipeline.forest_baseline import forest_baseline_output_paths
from deforestation_pipeline.forest_screening import build_forest_screening_domain
from deforestation_pipeline.hls_composite import HlsCompositeError
from deforestation_pipeline.hls_seasonal import HlsSeasonalRequest
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256
from deforestation_pipeline.temporal_cube import build_meteorological_south_window_plan

ROOT = Path(__file__).resolve().parents[1]


def _grid() -> RasterGridSpec:
    identity: dict[str, Any] = {
        "target_crs": "EPSG:32721",
        "resolution_m": 30.0,
        "width": 2,
        "height": 2,
        "transform": (30.0, 0.0, 500000.0, 0.0, -30.0, 6200000.0),
        "bounds": (500000.0, 6199940.0, 500060.0, 6200000.0),
        "alignment_strategy": "projected_crs_origin_outward_snap",
        "pixel_orientation": "north_up",
    }
    return RasterGridSpec(
        **identity,
        nodata=-9999.0,
        aoi_mask_required=True,
        grid_sha256=raster_grid_sha256(**identity),
    )


def _raster(values: np.ndarray, names: tuple[str, ...], grid: RasterGridSpec) -> bytes:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=grid.width,
            height=grid.height,
            count=len(names),
            dtype=str(values.dtype),
            crs=grid.target_crs,
            transform=rasterio.Affine(*grid.transform),
            nodata=grid.nodata,
        ) as dataset:
            dataset.write(values)
            dataset.descriptions = names
        return bytes(memory.read())


def _screening(grid: RasterGridSpec) -> object:
    config = load_config(ROOT / "configs" / "default.yml")
    shape = (grid.height, grid.width)
    return build_forest_screening_domain(
        source_count=np.full(shape, 2, dtype=np.int16),
        consensus_forest=np.ones(shape, dtype=np.int16),
        disagreement=np.zeros(shape, dtype=np.int16),
        evidence_fraction=np.ones(shape, dtype=np.float32),
        rf_vote_fraction=np.full(shape, 0.9, dtype=np.float32),
        rf_input_complete=np.ones(shape, dtype=np.int16),
        grid_spec=grid,
        config=config.forest_screening,
    )


def _disturbance_periods(grid: RasterGridSpec) -> tuple[DisturbancePeriodRaster, ...]:
    periods: list[DisturbancePeriodRaster] = []
    plan = build_meteorological_south_window_plan(
        start_year=2017,
        end_year=2021,
        generated_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    for window in plan.windows:
        reference_values = (0.68, 0.72, 0.69, 0.71)
        value = reference_values[window.season_year - 2017] if window.season_year <= 2020 else 0.2
        periods.append(
            DisturbancePeriodRaster(
                period_id=window.period_id,
                season=window.season.value,
                start_date=window.start_date,
                end_date_exclusive=window.end_date_exclusive,
                indices_raster=_raster(
                    np.full((4, 2, 2), value, dtype=np.float32),
                    ("NDVI", "NBR", "NDMI", "NIRv"),
                    grid,
                ),
                observation_count_raster=_raster(
                    np.full((1, 2, 2), 5, dtype=np.int16),
                    ("valid_observation_count",),
                    grid,
                ),
                published=window.season_year >= 2019,
            )
        )
    return tuple(periods)


def _successful_ccdc_scalar(grid: RasterGridSpec) -> bytes:
    values = np.zeros(
        (len(CCDC_SCALAR_SUMMARY_BAND_NAMES), grid.height, grid.width),
        dtype=np.float32,
    )
    values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index("ccdc_fit_succeeded")] = 1
    values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index("ccdc_has_post_cutoff_break")] = 1
    values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index("ccdc_first_break_fractional_year")] = 2021.25
    values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index("ccdc_change_pseudo_probability")] = 0.95
    values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index("ccdc_first_break_segment_observation_count")] = 10
    values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index("ccdc_total_valid_observation_count")] = 30
    for band_name in (
        "ccdc_NDVI_signed_magnitude",
        "ccdc_NBR_signed_magnitude",
        "ccdc_NDMI_signed_magnitude",
        "ccdc_NIRv_signed_magnitude",
    ):
        values[CCDC_SCALAR_SUMMARY_BAND_NAMES.index(band_name)] = -0.4
    return _raster(values, CCDC_SCALAR_SUMMARY_BAND_NAMES, grid)


@pytest.mark.parametrize(
    "ccdc_scalar_succeeds",
    (False, True),
    ids=("ccdc-unavailable", "ccdc-scalar-success"),
)
def test_range_pipeline_uses_internal_2017_support_and_publishes_five_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ccdc_scalar_succeeds: bool,
) -> None:
    vector = tmp_path / "territorio.geojson"
    vector.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-60.01, -31.01],
                            [-60.00, -31.01],
                            [-60.00, -31.00],
                            [-60.01, -31.00],
                            [-60.01, -31.01],
                        ]
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")
    seasonal_requests: list[HlsSeasonalRequest] = []

    monkeypatch.setattr(
        runner,
        "authenticate_earth_engine",
        lambda path: SimpleNamespace(module=object()),
    )
    grid = _grid()
    monkeypatch.setattr(runner, "derive_raster_grid_spec", lambda **kwargs: grid)

    def fake_seasonal(**kwargs: object) -> object:
        request = cast(HlsSeasonalRequest, kwargs["request"])
        grid = cast(RasterGridSpec, kwargs["grid_spec"])
        seasonal_requests.append(request)
        published = request.start_year == 2019
        return SimpleNamespace(
            files=(
                {
                    "json/temporal/seasonal/window_plan.json": b"{}\n",
                    "json/temporal/seasonal/cube_spec.json": b"{}\n",
                    "json/temporal/seasonal/cube_index.json": b"{}\n",
                    "json/temporal/seasonal/series_metadata.json": b"{}\n",
                }
                if published
                else {"support-must-not-be-published.txt": b"internal"}
            ),
            metadata=SimpleNamespace(
                start_year=request.start_year,
                end_year=request.end_year,
                period_ids=tuple(),
                grid_sha256=grid.grid_sha256,
                window_plan_sha256="a" * 64,
            ),
            cube_index=SimpleNamespace(cube_index_sha256="b" * 64),
            raster_items=tuple(),
        )

    monkeypatch.setattr(runner, "materialize_hls_seasonal_series", fake_seasonal)
    monkeypatch.setattr(
        runner,
        "build_forest_baseline_features",
        lambda **kwargs: SimpleNamespace(
            image=object(),
            metadata=SimpleNamespace(
                input_scene_count=10,
                model_dump=lambda mode: {"input_scene_count": 10},
            ),
        ),
    )
    monkeypatch.setattr(
        runner,
        "build_yearly_seasonal_feature_stack",
        lambda **kwargs: SimpleNamespace(image=object(), feature_columns=("p0",)),
    )
    baseline_metadata = SimpleNamespace(
        model_dump=lambda mode: {
            "score_semantics": "uncalibrated_core_evidence_fraction",
            "final_assessment_generated": False,
        }
    )
    monkeypatch.setattr(
        runner,
        "build_forest_baseline_images",
        lambda **kwargs: SimpleNamespace(metadata=baseline_metadata),
    )
    rf_metadata = SimpleNamespace(
        model_dump=lambda mode: {
            "score_semantics": "uncalibrated_binary_tree_vote_fraction",
            "calibrated_probability": False,
            "independent_evidence": False,
        }
    )
    monkeypatch.setattr(
        runner,
        "build_forest_rf_images",
        lambda **kwargs: SimpleNamespace(metadata=rf_metadata),
    )
    baseline_paths = forest_baseline_output_paths()
    monkeypatch.setattr(
        runner,
        "materialize_forest_baseline",
        lambda **kwargs: SimpleNamespace(
            files={path: b"internal" for path in baseline_paths.values()},
            grid_spec=kwargs["grid_spec"],
            screening_domain=_screening(cast(RasterGridSpec, kwargs["grid_spec"])),
        ),
    )
    monkeypatch.setattr(
        runner,
        "read_baseline_domain_inputs",
        lambda **kwargs: (
            np.full((2, 2), 2),
            np.ones((2, 2)),
            np.zeros((2, 2)),
            np.ones((2, 2)),
        ),
    )
    monkeypatch.setattr(
        runner,
        "_disturbance_period_rasters",
        lambda **kwargs: _disturbance_periods(grid),
    )

    if ccdc_scalar_succeeds:
        monkeypatch.setattr(
            runner,
            "build_ccdc_remote_benchmark",
            lambda **kwargs: SimpleNamespace(raw_array_image=object()),
        )
        monkeypatch.setattr(
            runner,
            "build_ccdc_scalar_summary_image",
            lambda **kwargs: object(),
        )
        monkeypatch.setattr(
            runner,
            "materialize_ee_image_to_grid",
            lambda **kwargs: SimpleNamespace(content=_successful_ccdc_scalar(grid)),
        )
    else:

        def ccdc_fails(**kwargs: object) -> object:
            raise HlsCompositeError("dense_20170101_20211201_source_collection_service_error")

        monkeypatch.setattr(runner, "build_ccdc_remote_benchmark", ccdc_fails)

    paths = disturbance_detection_output_paths()

    run_directory = runner.run_local_vector_pipeline(
        input_path=vector,
        output_root=tmp_path / "outputs",
        config_path=ROOT / "configs" / "default.yml",
        source_crs="EPSG:4326",
        establishment_id="step14",
        analysis_end_date=date(2026, 7, 29),
        created_at=datetime(2026, 7, 29, tzinfo=UTC),
        gee_credentials_path=credentials,
        generate_forest_baseline=True,
        hls_seasonal_request=HlsSeasonalRequest(start_year=2019, end_year=2021),
        generate_disturbance_detection=True,
    )

    assert [(request.start_year, request.end_year) for request in seasonal_requests] == [
        (2019, 2021),
        (2017, 2018),
    ]
    assert all((run_directory / path).is_file() for path in paths.values())
    assert not (run_directory / "support-must-not-be-published.txt").exists()
    summary = json.loads((run_directory / "json/run/summary.json").read_text())
    assert summary["stage"] == "disturbance_detection"
    assert summary["analysis_end_date"] == "2021-11-30"
    assert summary["requested_analysis_end_date"] == "2026-07-29"
    assert summary["disturbance_detection"]["executed"] is True
    event_summary = summary["disturbance_detection"]["persistent_events"]
    assert event_summary["event_count"] == 1
    assert event_summary["total_event_area_ha"] == 0.36
    assert event_summary["above_visec_area_reference_candidate_count"] == 0
    assert event_summary["above_visec_area_reference_candidate_area_ha"] == 0.0
    assert event_summary["automatic_final_assessment_generated"] is False
    assert event_summary["attribution_generated"] is False
    serialized_disturbance_summary = json.dumps(
        summary["disturbance_detection"],
        sort_keys=True,
    )
    assert all(serialized_disturbance_summary.count(path) == 1 for path in paths.values())
    disturbance_metadata = json.loads(
        (run_directory / paths["metadata"]).read_text(encoding="utf-8")
    )
    assert disturbance_metadata["ccdc"]["raw_array_downloaded"] is False
    with rasterio.open(run_directory / paths["summary_raster"]) as dataset:
        summary = dataset.read()
        nodata = dataset.nodata
    available = summary[DISTURBANCE_SUMMARY_BAND_NAMES.index("available_detector_count")]
    agreeing = summary[DISTURBANCE_SUMMARY_BAND_NAMES.index("agreeing_detector_count")]
    state = summary[DISTURBANCE_SUMMARY_BAND_NAMES.index("disturbance_state_code")]
    if ccdc_scalar_succeeds:
        assert disturbance_metadata["ccdc"]["status"] == "scalar_summary_materialized"
        assert disturbance_metadata["ccdc"]["scalar_summary_downloaded"] is True
        assert "failure_code" not in disturbance_metadata["ccdc"]
        assert np.all(available[available != nodata] == 2)
        assert np.all(agreeing[agreeing != nodata] == 2)
        assert np.all(state[state != nodata] == DisturbanceStateCode.PERSISTENT_CANDIDATE)
    else:
        assert disturbance_metadata["ccdc"]["status"] == "unavailable_global_failure"
        assert disturbance_metadata["ccdc"]["scalar_summary_downloaded"] is False
        assert disturbance_metadata["ccdc"]["failure_code"] == (
            "dense_20170101_20211201_source_collection_service_error"
        )
        assert np.all(available[available != nodata] == 1)
        assert not np.any(state[state != nodata] == DisturbanceStateCode.DETECTOR_DISAGREEMENT)
    manifest = json.loads((run_directory / "json/run/manifest.json").read_text())
    assert manifest["analysis_end_date"] == "2021-11-30"
    assert manifest["requested_analysis_end_date"] == "2026-07-29"
    disturbance_artifacts = tuple(
        artifact for artifact in manifest["artifacts"] if artifact["path"] in set(paths.values())
    )
    assert len(disturbance_artifacts) == 5
    assert {artifact["path"] for artifact in disturbance_artifacts} == set(paths.values())
    for artifact in disturbance_artifacts:
        assert (
            artifact["sha256"]
            == hashlib.sha256((run_directory / artifact["path"]).read_bytes()).hexdigest()
        )
    event_collection = json.loads(
        (run_directory / "json/evidence/disturbance_events.json").read_text()
    )
    assert event_collection["event_count"] == 1
    event_id = event_collection["events"][0]["event_id"]
    event_paths = {
        "tables/evidence/disturbance_events.csv",
        "json/evidence/disturbance_events.json",
        "json/evidence/disturbance_events.geojson",
        "figures/evidence/disturbance_events.png",
        f"figures/evidence/disturbance_event_{event_id.lower()}.png",
    }
    manifested = {artifact["path"]: artifact for artifact in manifest["artifacts"]}
    assert event_paths <= set(manifested)
    for path in event_paths:
        assert (
            manifested[path]["sha256"]
            == hashlib.sha256((run_directory / path).read_bytes()).hexdigest()
        )


def test_seasonal_hls_failure_aborts_before_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = tmp_path / "territorio.geojson"
    vector.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-60.01, -31.01],
                            [-60.00, -31.01],
                            [-60.00, -31.00],
                            [-60.01, -31.00],
                            [-60.01, -31.01],
                        ]
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")
    output_root = tmp_path / "outputs"
    monkeypatch.setattr(
        runner,
        "authenticate_earth_engine",
        lambda path: SimpleNamespace(module=object()),
    )

    def seasonal_fails(**kwargs: object) -> object:
        raise HlsCompositeError("2019_mam_source_collection_service_error")

    monkeypatch.setattr(runner, "materialize_hls_seasonal_series", seasonal_fails)

    with pytest.raises(
        HlsCompositeError,
        match="2019_mam_source_collection_service_error",
    ):
        runner.run_local_vector_pipeline(
            input_path=vector,
            output_root=output_root,
            config_path=ROOT / "configs" / "default.yml",
            source_crs="EPSG:4326",
            establishment_id="seasonal-failure",
            analysis_end_date=date(2026, 7, 29),
            created_at=datetime(2026, 7, 29, tzinfo=UTC),
            gee_credentials_path=credentials,
            hls_seasonal_request=HlsSeasonalRequest(start_year=2019, end_year=2021),
        )

    assert not output_root.exists() or not tuple(output_root.iterdir())
