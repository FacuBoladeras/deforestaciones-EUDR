"""Deltas forestales RF contra 2020 sin atribución ni conclusión EUDR."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.figure import Figure
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from deforestation_pipeline.config import load_config, load_forest_model_config
from deforestation_pipeline.forest_rf import forest_rf_predictor_band_names
from deforestation_pipeline.forest_rf_temporal import (
    FOREST_RF_TEMPORAL_SCHEMA_VERSION,
    ForestModelComparison,
    ForestTransitionCode,
    classify_forest_transition,
    compare_forest_models_2020,
    compute_vote_delta,
    forest_rf_per_pixel_qa_band_names,
    forest_rf_temporal_output_paths,
    materialize_forest_rf_temporal,
    predict_forest_rf_locally,
    summarize_forest_year,
)
from deforestation_pipeline.raster_products import RasterDownloadError
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256
from deforestation_pipeline.seasonal_feature_stack import (
    SeasonalFeatureStack,
    seasonal_feature_columns,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _grid() -> RasterGridSpec:
    transform = (30.0, 0.0, 500_000.0, 0.0, -30.0, 6_500_000.0)
    bounds = (500_000.0, 6_499_940.0, 500_090.0, 6_500_000.0)
    return RasterGridSpec(
        target_crs="EPSG:32721",
        resolution_m=30.0,
        width=3,
        height=2,
        transform=transform,
        bounds=bounds,
        alignment_strategy="projected_crs_origin_outward_snap",
        pixel_orientation="north_up",
        nodata=-9999,
        aoi_mask_required=True,
        grid_sha256=raster_grid_sha256(
            target_crs="EPSG:32721",
            resolution_m=30.0,
            width=3,
            height=2,
            transform=transform,
            bounds=bounds,
            alignment_strategy="projected_crs_origin_outward_snap",
            pixel_orientation="north_up",
        ),
    )


def test_transition_truth_table_and_common_support_are_explicit() -> None:
    reference = np.array([[0, 0, 1], [1, 1, 0]], dtype=np.int16)
    current = np.array([[0, 1, 0], [1, 0, 1]], dtype=np.int16)
    common = np.array([[True, True, True], [True, False, False]])
    aoi = np.array([[True, True, True], [True, True, False]])

    transition = classify_forest_transition(
        reference_class=reference,
        current_class=current,
        common_support=common,
        aoi_footprint=aoi,
        nodata=-9999,
    )

    assert transition.tolist() == [
        [
            int(ForestTransitionCode.STABLE_NONFOREST),
            int(ForestTransitionCode.CANDIDATE_FOREST_GAIN),
            int(ForestTransitionCode.CANDIDATE_FOREST_LOSS),
        ],
        [
            int(ForestTransitionCode.STABLE_FOREST),
            int(ForestTransitionCode.INSUFFICIENT_DATA),
            -9999,
        ],
    ]


def test_vote_delta_is_bounded_and_never_computed_outside_common_support() -> None:
    reference = np.array([[0.1, 0.2, 0.9], [0.8, 0.7, -9999]], dtype=np.float32)
    current = np.array([[0.2, 0.9, 0.1], [0.8, -9999, -9999]], dtype=np.float32)
    common = np.array([[True, True, True], [True, False, False]])

    delta = compute_vote_delta(
        reference_vote_fraction=reference,
        current_vote_fraction=current,
        common_support=common,
        nodata=-9999,
    )

    assert delta[0].tolist() == pytest.approx([0.1, 0.7, -0.8])
    assert delta[1].tolist() == pytest.approx([0.0, -9999, -9999])
    assert np.all(delta[common] >= -1)
    assert np.all(delta[common] <= 1)

    invalid = current.copy()
    invalid[0, 0] = 1.01
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        compute_vote_delta(
            reference_vote_fraction=reference,
            current_vote_fraction=invalid,
            common_support=common,
            nodata=-9999,
        )


def test_year_summary_preserves_counts_metric_area_categories_and_nodata() -> None:
    grid = _grid()
    transition = np.array(
        [
            [1, 2, 3],
            [4, 0, -9999],
        ],
        dtype=np.int16,
    )
    delta = np.array([[0.0, 0.6, -0.7], [0.0, -9999, -9999]], dtype=np.float32)
    common = np.array([[True, True, True], [True, False, False]])
    aoi = np.array([[True, True, True], [True, True, False]])

    summary = summarize_forest_year(
        year=2024,
        transition=transition,
        vote_delta=delta,
        common_support=common,
        aoi_footprint=aoi,
        grid_spec=grid,
    )

    assert summary["evaluable_pixel_count"] == 4
    assert summary["evaluable_area_ha"] == pytest.approx(0.36)
    assert summary["aoi_pixel_count"] == 5
    assert summary["categories"]["stable_nonforest"] == {
        "code": 1,
        "pixel_count": 1,
        "area_ha": pytest.approx(0.09),
    }
    assert summary["categories"]["insufficient_data"]["pixel_count"] == 1
    assert summary["vote_delta"]["minimum"] == pytest.approx(-0.7)
    assert summary["vote_delta"]["maximum"] == pytest.approx(0.6)


def test_p0_candidate_comparison_requires_identical_stack_support() -> None:
    grid = _grid()
    aoi = np.ones((2, 3), dtype=bool)
    support = np.array([[1, 1, 1], [1, 0, 0]], dtype=np.uint8)
    p0_class = np.array([[0, 1, 1], [0, -9999, -9999]], dtype=np.int16)
    candidate_class = np.array([[0, 0, 1], [1, -9999, -9999]], dtype=np.int16)
    p0_vote = np.array([[0.1, 0.8, 0.9], [0.2, -9999, -9999]], dtype=np.float32)
    candidate_vote = np.array([[0.2, 0.4, 0.95], [0.7, -9999, -9999]], dtype=np.float32)

    comparison = compare_forest_models_2020(
        p0_class=p0_class,
        candidate_class=candidate_class,
        p0_vote_fraction=p0_vote,
        candidate_vote_fraction=candidate_vote,
        p0_input_complete=support,
        candidate_input_complete=support.copy(),
        aoi_footprint=aoi,
        grid_spec=grid,
    )

    assert comparison.summary["same_input_complete_mask"] is True
    assert comparison.summary["evaluable_pixel_count"] == 4
    assert comparison.summary["class_disagreement_pixel_count"] == 2
    assert comparison.summary["class_disagreement_area_ha"] == pytest.approx(0.18)
    assert comparison.class_disagreement[support == 1].tolist() == [0, 1, 0, 1]
    assert comparison.vote_delta[support == 1].tolist() == pytest.approx([0.1, -0.4, 0.05, 0.5])

    mismatched = support.copy()
    mismatched[0, 0] = 0
    with pytest.raises(ValueError, match="mismo stack y soporte"):
        compare_forest_models_2020(
            p0_class=p0_class,
            candidate_class=candidate_class,
            p0_vote_fraction=p0_vote,
            candidate_vote_fraction=candidate_vote,
            p0_input_complete=mismatched,
            candidate_input_complete=support,
            aoi_footprint=aoi,
            grid_spec=grid,
        )


def test_temporal_output_contract_has_only_existing_bundle_roots_and_versions() -> None:
    paths = forest_rf_temporal_output_paths(years=(2020, 2021, 2022, 2023, 2024))

    assert FOREST_RF_TEMPORAL_SCHEMA_VERSION == "1.2.0"
    assert len(paths) == len(set(paths.values()))
    assert paths["class_2024"] == "tiffs/evidence/rf_forest_class_2024.tif"
    assert paths["delta_2024"] == "tiffs/evidence/rf_forest_vote_delta_2024_vs_2020.tif"
    assert paths["transition_2024"] == ("tiffs/evidence/rf_forest_transition_2024_vs_2020.tif")
    assert paths["predictors_2024"] == "tiffs/evidence/rf_predictor_stack_2024.tif"
    assert paths["observation_qa_2024"] == ("tiffs/evidence/rf_observation_qa_2024.tif")
    assert paths["summary_csv"].startswith("tables/evidence/")
    assert paths["summary_json"].startswith("json/evidence/")
    assert paths["overview_figure"].startswith("figures/evidence/")
    assert {Path(path).parts[0] for path in paths.values()} <= {
        "figures",
        "json",
        "tables",
        "tiffs",
    }


def test_per_pixel_qa_contract_has_four_seasons_and_three_sensor_counts() -> None:
    names = forest_rf_per_pixel_qa_band_names(2024)

    assert len(names) == 12
    assert names == (
        "2024_DJF_valid_observation_count",
        "2024_DJF_valid_observation_count_l30",
        "2024_DJF_valid_observation_count_s30",
        "2024_MAM_valid_observation_count",
        "2024_MAM_valid_observation_count_l30",
        "2024_MAM_valid_observation_count_s30",
        "2024_JJA_valid_observation_count",
        "2024_JJA_valid_observation_count_l30",
        "2024_JJA_valid_observation_count_s30",
        "2024_SON_valid_observation_count",
        "2024_SON_valid_observation_count_l30",
        "2024_SON_valid_observation_count_s30",
    )


def test_overview_figure_has_complete_discrete_legends_north_and_metric_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.forest_rf_temporal as temporal

    years = (2020, 2021)
    classes: dict[int, dict[str, np.ndarray]] = {
        year: {
            "class": np.asarray([[0, 1, -9999], [1, 0, -9999]], dtype=np.int16),
            "vote": np.zeros((2, 3), dtype=np.float32),
            "complete": np.ones((2, 3), dtype=np.int16),
        }
        for year in years
    }
    transitions = {
        2020: np.asarray([[1, 4, -9999], [4, 1, -9999]], dtype=np.int16),
        2021: np.asarray([[1, 2, -9999], [3, 4, -9999]], dtype=np.int16),
    }
    comparison = ForestModelComparison(
        class_disagreement=np.asarray([[0, 1, -9999], [0, 0, -9999]], dtype=np.int16),
        vote_delta=np.zeros((2, 3), dtype=np.float32),
        summary={},
    )
    captured: dict[str, object] = {}

    def capture(figure: Figure, dpi: int) -> bytes:
        captured["figure"] = figure
        captured["dpi"] = dpi
        return b"PNG"

    monkeypatch.setattr(temporal, "_figure_bytes", capture)

    content = temporal._render_overview(
        years=years,
        candidate_arrays=classes,
        transitions=transitions,
        comparison=comparison,
        dpi=150,
        nodata=-9999,
        grid_spec=_grid(),
    )

    assert content == b"PNG"
    figure = cast(Figure, captured["figure"])
    legend_labels: set[str] = set()
    for axis in figure.axes:
        legend = axis.get_legend()
        if legend is not None:
            legend_labels.update(text.get_text() for text in legend.get_texts())
    assert {
        "No bosque",
        "Bosque",
        "Datos insuficientes",
        "No bosque estable",
        "Ganancia forestal candidata",
        "Pérdida forestal candidata",
        "Bosque estable",
    } <= legend_labels
    annotation_text = {text.get_text() for axis in figure.axes for text in axis.texts}
    assert "N" in annotation_text
    assert any(label.endswith(" m") or label.endswith(" km") for label in annotation_text)
    plt.close(figure)


@pytest.mark.filterwarnings("error:Setting the shape on a NumPy array has been deprecated")
@pytest.mark.parametrize(
    "fallback_error_code",
    [
        None,
        "direct_download_limit_exceeded",
        "download_url_failed",
        "remote_server_error",
    ],
)
def test_temporal_materialization_preserves_12_band_qa_with_hash_and_grid(
    monkeypatch: pytest.MonkeyPatch,
    fallback_error_code: str | None,
) -> None:
    import deforestation_pipeline.forest_rf_temporal as temporal

    grid = _grid()
    pipeline_config = load_config(PROJECT_ROOT / "configs/default.yml")
    candidate_config = load_forest_model_config(
        PROJECT_ROOT / "configs/rf-forest-entrerios-2020-2024.yml"
    )

    class FakeImage:
        def __init__(self) -> None:
            self.selections: list[tuple[str, ...]] = []

        def select(self, names: list[str]) -> FakeImage:
            selected = tuple(names)
            self.selections.append(selected)
            return self

        def addBands(self, other: object) -> FakeImage:
            del other
            return self

    feature_images = {year: FakeImage() for year in candidate_config.supported_observation_years}
    stacks = {
        year: SeasonalFeatureStack(
            image=feature_images[year],
            feature_columns=seasonal_feature_columns(
                year=year,
                indices=pipeline_config.data.indices,
            ),
            observation_year=year,
            periods=(),
        )
        for year in candidate_config.supported_observation_years
    }
    fake_metadata = SimpleNamespace(model_dump=lambda mode: {"temporal_transfer_validated": False})
    images = {
        year: SimpleNamespace(
            forest_class=FakeImage(),
            forest_vote_fraction=FakeImage(),
            input_complete=FakeImage(),
            metadata=fake_metadata,
        )
        for year in candidate_config.supported_observation_years
    }
    p0_images = SimpleNamespace(
        forest_class=FakeImage(),
        forest_vote_fraction=FakeImage(),
        input_complete=FakeImage(),
        metadata=fake_metadata,
    )

    def raster_bytes(values: np.ndarray, names: tuple[str, ...], dtype: str) -> bytes:
        cube = values if values.ndim == 3 else values[np.newaxis, ...]
        with MemoryFile() as memory:
            with memory.open(
                driver="GTiff",
                width=grid.width,
                height=grid.height,
                count=cube.shape[0],
                dtype=dtype,
                crs=grid.target_crs,
                transform=Affine(*grid.transform),
                nodata=grid.nodata,
            ) as dataset:
                dataset.write(cube.astype(dtype))
                for index, name in enumerate(names, start=1):
                    dataset.set_band_description(index, name)
            return bytes(memory.read())

    download_calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_download(**kwargs: object) -> object:
        artifact_path = str(kwargs["artifact_path"])
        band_names = tuple(cast(list[str] | tuple[str, ...], kwargs["band_names"]))
        output_type = str(kwargs["output_type"])
        download_calls.append((artifact_path, band_names))
        if fallback_error_code is not None and len(band_names) in {69, 3}:
            raise RasterDownloadError(fallback_error_code)
        if len(band_names) == 69:
            values = np.concatenate(
                (
                    np.full((56, grid.height, grid.width), 0.2, dtype=np.float32),
                    np.stack(
                        [
                            np.full((grid.height, grid.width), index, dtype=np.float32)
                            for index in range(12)
                        ]
                    ),
                    np.ones((1, grid.height, grid.width), dtype=np.float32),
                )
            )
        elif len(band_names) == 56:
            values = np.full((56, grid.height, grid.width), 0.2, dtype=np.float32)
        elif len(band_names) == 12:
            values = np.stack(
                [np.full((grid.height, grid.width), index, dtype=np.int16) for index in range(12)]
            )
        elif len(band_names) == 3:
            values = np.stack(
                (
                    np.zeros((grid.height, grid.width), dtype=np.float32),
                    np.full((grid.height, grid.width), 0.2, dtype=np.float32),
                    np.ones((grid.height, grid.width), dtype=np.float32),
                )
            )
        elif "vote" in artifact_path:
            values = np.full((grid.height, grid.width), 0.2, dtype=np.float32)
        elif "class" in artifact_path:
            values = np.zeros((grid.height, grid.width), dtype=np.int16)
        else:
            values = np.ones((grid.height, grid.width), dtype=np.int16)
        dtype = "float32" if output_type == "float32" else "int16"
        content = raster_bytes(values, band_names, dtype)
        record = SimpleNamespace(
            model_dump=lambda mode: {
                "artifact_path": artifact_path,
                "band_count": len(band_names),
                "grid_sha256": grid.grid_sha256,
            }
        )
        return SimpleNamespace(content=content, estimate=record, validation=record)

    class FakeModel:
        classes_ = np.asarray([0, 1])
        feature_names_in_ = np.asarray(forest_rf_predictor_band_names(), dtype=object)
        estimators_ = tuple(
            SimpleNamespace(
                predict=lambda values, vote=vote: np.full(values.shape[0], vote, dtype=np.int16)
            )
            for vote in (0, 0, 0, 0, 1)
        )

        def predict(self, values: Any) -> np.ndarray:
            return np.zeros(values.shape[0], dtype=np.int16)

    monkeypatch.setattr(temporal, "materialize_ee_image_to_grid", fake_download)
    monkeypatch.setattr(temporal, "canonicalize_forest_rf_feature_stack", lambda **kwargs: object())
    monkeypatch.setattr(temporal, "select_forest_rf_predictors", lambda image: FakeImage())
    monkeypatch.setattr(
        temporal,
        "_load_local_model_bundle",
        lambda config: {
            "model": FakeModel(),
            "feature_columns": list(forest_rf_predictor_band_names()),
            "label_to_int": {"non_forest": 0, "forest": 1},
            "positive_class": "forest",
        },
    )

    result = materialize_forest_rf_temporal(
        yearly_images=images,  # type: ignore[arg-type]
        yearly_feature_stacks=stacks,
        p0_images=p0_images,  # type: ignore[arg-type]
        config=candidate_config,
        output_config=pipeline_config.output,
        grid_spec=grid,
        generated_at=datetime(2026, 8, 6, tzinfo=UTC),
    )

    paths = forest_rf_temporal_output_paths(years=candidate_config.supported_observation_years)
    metadata = json.loads(result.files[paths["metadata"]])
    if fallback_error_code is not None:
        assert [len(names) for _, names in download_calls] == [
            69,
            56,
            12,
            1,
            69,
            56,
            12,
            1,
            69,
            56,
            12,
            1,
            69,
            56,
            12,
            1,
            69,
            56,
            12,
            1,
            3,
            1,
            1,
            1,
        ]
        assert metadata["raster_materialization"]["transport_request_count"] == 18
        assert metadata["raster_materialization"]["transport_strategy"] == (
            "adaptive_multiband_with_individual_fallback"
        )
        assert metadata["raster_materialization"]["transport_fallback_codes"] == [
            fallback_error_code
        ]
    else:
        assert [len(names) for _, names in download_calls] == [69, 69, 69, 69, 69, 3]
        assert metadata["raster_materialization"]["transport_request_count"] == 6
        assert metadata["raster_materialization"]["transport_strategy"] == (
            "single_multiband_geotiff_per_year"
        )
        assert metadata["raster_materialization"]["transport_fallback_codes"] == []
    for year in candidate_config.supported_observation_years:
        path = paths[f"observation_qa_{year}"]
        with MemoryFile(result.files[path]) as memory:
            with memory.open() as dataset:
                assert dataset.count == 12
                assert tuple(dataset.descriptions) == forest_rf_per_pixel_qa_band_names(year)
                assert dataset.nodata == grid.nodata
                assert dataset.crs.to_string() == grid.target_crs
        qa = metadata["per_pixel_observation_qa"]["years"][str(year)]
        assert qa["grid_sha256"] == grid.grid_sha256
        assert qa["band_count"] == 12
        assert len(qa["sha256"]) == 64
    assert metadata["gee_candidate_graph_diagnostic"]["point_reduce_region_succeeded"] is True
    assert metadata["scientific_status"] == "review_required"
    assert (
        metadata["gee_candidate_graph_diagnostic"]["precise_limiting_component_isolated"] is False
    )


def test_local_candidate_inference_uses_direct_predict_and_hard_tree_votes_on_support() -> None:
    from deforestation_pipeline.forest_rf import forest_rf_predictor_band_names

    class FakeModel:
        classes_ = np.asarray([0, 1])

        def __init__(self, feature_names: tuple[str, ...]) -> None:
            self.predict_rows = 0
            self.proba_rows = 0
            self.feature_names_in_ = np.asarray(feature_names)
            self.estimators_ = (
                _FakeTree(np.asarray([0, 1, 1], dtype=np.int16)),
                _FakeTree(np.asarray([0, 1, 0], dtype=np.int16)),
                _FakeTree(np.asarray([1, 1, 0], dtype=np.int16)),
            )

        def predict(self, values: Any) -> np.ndarray:
            self.predict_rows = values.shape[0]
            first_column = np.asarray(values.iloc[:, 0].to_numpy(), dtype=np.float64)
            return (first_column >= 0.5).astype(np.int16)

        def predict_proba(self, values: Any) -> np.ndarray:
            self.proba_rows = values.shape[0]
            forest = np.full(values.shape[0], 0.99, dtype=np.float64)
            return np.column_stack((1 - forest, forest))

    class _FakeTree:
        def __init__(self, predictions: np.ndarray) -> None:
            self.predictions = predictions

        def predict(self, values: Any) -> np.ndarray:
            assert isinstance(values, np.ndarray)
            return self.predictions[: values.shape[0]]

    names = forest_rf_predictor_band_names()
    model = FakeModel(names)
    cube = np.zeros((56, 2, 3), dtype=np.float32)
    cube[0] = np.asarray([[0.2, 0.8, 0.7], [0.1, 0.6, 0.9]], dtype=np.float32)
    complete = np.asarray([[1, 1, 0], [1, 0, -9999]], dtype=np.int16)
    aoi = complete != -9999

    result = predict_forest_rf_locally(
        predictor_cube=cube,
        predictor_band_names=names,
        input_complete=complete,
        aoi_footprint=aoi,
        model_bundle={
            "model": model,
            "feature_columns": list(names),
            "label_to_int": {"non_forest": 0, "forest": 1},
            "positive_class": "forest",
        },
        nodata=-9999,
    )

    assert model.predict_rows == 3
    assert model.proba_rows == 0
    assert result.forest_class.tolist() == [[0, 1, -9999], [0, -9999, -9999]]
    assert result.vote_fraction[0, 0] == pytest.approx(1 / 3)
    assert result.vote_fraction[0, 1] == pytest.approx(1.0)
    assert result.vote_fraction[1, 0] == pytest.approx(1 / 3)
    assert np.all(result.vote_fraction[complete != 1] == -9999)


def test_local_candidate_rejects_non_binary_or_empty_tree_ensemble() -> None:
    from deforestation_pipeline.forest_rf import forest_rf_predictor_band_names

    class InvalidTree:
        def predict(self, values: Any) -> np.ndarray:
            return np.full(values.shape[0], 2, dtype=np.int16)

    class InvalidModel:
        classes_ = np.asarray([0, 1])
        feature_names_in_ = np.asarray(forest_rf_predictor_band_names())
        estimators_ = (InvalidTree(),)

        def predict(self, values: Any) -> np.ndarray:
            return np.zeros(values.shape[0], dtype=np.int16)

    with pytest.raises(ValueError, match="votos binarios"):
        predict_forest_rf_locally(
            predictor_cube=np.zeros((56, 1, 1), dtype=np.float32),
            predictor_band_names=forest_rf_predictor_band_names(),
            input_complete=np.ones((1, 1), dtype=np.int16),
            aoi_footprint=np.ones((1, 1), dtype=bool),
            model_bundle={
                "model": InvalidModel(),
                "feature_columns": list(forest_rf_predictor_band_names()),
                "label_to_int": {"non_forest": 0, "forest": 1},
                "positive_class": "forest",
            },
            nodata=-9999,
        )
