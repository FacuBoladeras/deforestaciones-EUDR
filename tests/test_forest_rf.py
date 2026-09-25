"""Integración auditable del RF P0 como evidencia forestal 2020."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from shapely.geometry import box

from deforestation_pipeline.config import (
    ForestRandomForestConfig,
    SpectralIndex,
    load_config,
    load_forest_model_config,
)
from deforestation_pipeline.forest_rf import (
    FOREST_RF_CLASS_BAND,
    FOREST_RF_INPUT_COMPLETE_BAND,
    FOREST_RF_VOTE_FRACTION_BAND,
    build_forest_rf_images,
    build_geemap_tree_classifier,
    canonicalize_forest_rf_feature_stack,
    forest_rf_output_band_names,
    forest_rf_predictor_band_names,
    forest_rf_predictor_columns_sha256,
    forest_rf_qa_band_names,
    select_forest_rf_predictors,
)
from deforestation_pipeline.seasonal_feature_stack import (
    SeasonalFeatureStack,
    seasonal_feature_columns,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeString:
    def __init__(self, value: str) -> None:
        self.value = value

    def replace(self, old: str, new: str, flags: str) -> str:
        assert flags == "g"
        return self.value.replace(old, new)


class FakeTreeList:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.values = values

    def map(self, mapper: Any) -> FakeTreeList:
        return FakeTreeList(tuple(mapper(value) for value in self.values))


class FakeFeatureCollection:
    def __init__(self, asset_id: str, trees: tuple[str, ...]) -> None:
        self.asset_id = asset_id
        self.trees = trees
        self.aggregated_property: str | None = None

    def aggregate_array(self, property_name: str) -> FakeTreeList:
        self.aggregated_property = property_name
        return FakeTreeList(self.trees)


class FakeClassifier:
    def __init__(self, trees: tuple[str, ...], mode: str = "CLASSIFICATION") -> None:
        self.trees = trees
        self.mode = mode

    def setOutputMode(self, mode: str) -> FakeClassifier:
        return FakeClassifier(self.trees, mode)


class FakeImage:
    def __init__(
        self,
        bands: tuple[str, ...],
        expression: str = "features",
        values: tuple[int, ...] | None = None,
    ) -> None:
        self.bands = bands
        self.expression = expression
        self.values = values or tuple(range(len(bands)))

    def select(self, bands: list[str], renamed: list[str] | None = None) -> FakeImage:
        positions = tuple(self.bands.index(band) for band in bands)
        output_bands = tuple(renamed) if renamed is not None else tuple(bands)
        return FakeImage(
            output_bands,
            f"select({','.join(bands)})",
            tuple(self.values[position] for position in positions),
        )

    def classify(self, classifier: FakeClassifier) -> FakeImage:
        return FakeImage(("classification",), f"classify[{classifier.mode}]({self.expression})")

    def arrayReduce(self, reducer: str, axes: list[int]) -> FakeImage:
        return FakeImage(self.bands, f"arrayReduce[{reducer},{axes}]({self.expression})")

    def arrayGet(self, position: list[int]) -> FakeImage:
        return FakeImage(self.bands, f"arrayGet[{position}]({self.expression})")

    def mask(self) -> FakeImage:
        return FakeImage(self.bands, f"mask({self.expression})")

    def reduce(self, reducer: str) -> FakeImage:
        return FakeImage(("reduced",), f"reduce[{reducer}]({self.expression})")

    def rename(self, band: str) -> FakeImage:
        return FakeImage((band,), self.expression)

    def toUint8(self) -> FakeImage:
        return FakeImage(self.bands, f"uint8({self.expression})")

    def toFloat(self) -> FakeImage:
        return FakeImage(self.bands, f"float({self.expression})")

    def unmask(self, value: int) -> FakeImage:
        return FakeImage(self.bands, f"unmask({self.expression},{value})")

    def updateMask(self, mask: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"updateMask({self.expression},{mask.expression})")

    def eq(self, value: int) -> FakeImage:
        return FakeImage(self.bands, f"eq({self.expression},{value})")

    def gt(self, value: int) -> FakeImage:
        return FakeImage(self.bands, f"gt({self.expression},{value})")

    def And(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"and({self.expression},{other.expression})")

    def clip(self, geometry: object) -> FakeImage:
        return FakeImage(self.bands, f"clip({self.expression},{geometry})")


class FakeImageFactory:
    @staticmethod
    def constant(value: int) -> FakeImage:
        return FakeImage(("constant",), f"constant({value})")


class FakeModule:
    def __init__(self, trees: tuple[str, ...] = ("a#b", "c#d")) -> None:
        self.collection: FakeFeatureCollection | None = None
        self.classifier_trees: tuple[str, ...] | None = None
        self._trees = trees
        self.Reducer = SimpleNamespace(mean=lambda: "mean", min=lambda: "min")
        self.Classifier = SimpleNamespace(decisionTreeEnsemble=self._classifier)
        self.Image = FakeImageFactory()

    def FeatureCollection(self, asset_id: str) -> FakeFeatureCollection:
        self.collection = FakeFeatureCollection(asset_id, self._trees)
        return self.collection

    @staticmethod
    def Geometry(value: object) -> str:
        return "aoi"

    @staticmethod
    def String(value: str) -> FakeString:
        return FakeString(value)

    def _classifier(self, trees: FakeTreeList) -> FakeClassifier:
        self.classifier_trees = trees.values
        return FakeClassifier(trees.values)


def _feature_stack(year: int = 2020) -> SeasonalFeatureStack:
    columns = seasonal_feature_columns(year=year, indices=tuple(SpectralIndex))
    return SeasonalFeatureStack(
        image=FakeImage(columns),
        feature_columns=columns,
        observation_year=year,
    )


def _multiyear_config() -> ForestRandomForestConfig:
    return load_forest_model_config(PROJECT_ROOT / "configs" / "rf-forest-entrerios-2020-2024.yml")


def test_rf_contract_has_exactly_56_predictors_in_deterministic_order() -> None:
    predictors = forest_rf_predictor_band_names()

    assert len(predictors) == 56
    assert len(set(predictors)) == 56
    assert predictors[:6] == (
        "2020_DJF_blue",
        "2020_DJF_green",
        "2020_DJF_red",
        "2020_DJF_nir",
        "2020_DJF_swir1",
        "2020_DJF_swir2",
    )
    assert predictors[-8:] == (
        "2020_SON_NDVI",
        "2020_SON_EVI2",
        "2020_SON_NBR",
        "2020_SON_NDMI",
        "2020_SON_NMDI",
        "2020_SON_LSWI",
        "2020_SON_NIRv",
        "2020_SON_kNDVI",
    )


def test_multiyear_canonicalization_preserves_values_order_and_feature_hash() -> None:
    config = _multiyear_config()
    source = _feature_stack(2024)

    canonical = canonicalize_forest_rf_feature_stack(
        feature_stack=source,
        observation_year=2024,
        config=config,
    )

    assert canonical.feature_columns == seasonal_feature_columns(
        year=2020, indices=tuple(SpectralIndex)
    )
    assert canonical.image.bands == canonical.feature_columns
    assert canonical.image.values == source.image.values
    assert canonical.observation_year == 2024
    assert forest_rf_predictor_columns_sha256() == (
        "35b5b2ba36bfccb07e798a27e354fb9fae0ba0819975986975b5c20c5fdd6d0f"
    )
    expected_payload = json.dumps(
        list(forest_rf_predictor_band_names()),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert hashlib.sha256(expected_payload.encode()).hexdigest() == (
        forest_rf_predictor_columns_sha256()
    )


@pytest.mark.parametrize(
    ("columns", "message"),
    [
        (
            seasonal_feature_columns(year=2024, indices=tuple(SpectralIndex))[:-1],
            "missing",
        ),
        (
            (
                *seasonal_feature_columns(year=2024, indices=tuple(SpectralIndex))[:-1],
                seasonal_feature_columns(year=2024, indices=tuple(SpectralIndex))[0],
            ),
            "duplicate",
        ),
        (
            (
                "2023_DJF_blue",
                *seasonal_feature_columns(year=2024, indices=tuple(SpectralIndex))[1:],
            ),
            "mixed_year",
        ),
    ],
)
def test_multiyear_canonicalization_rejects_schema_drift(
    columns: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        canonicalize_forest_rf_feature_stack(
            feature_stack=SeasonalFeatureStack(
                image=FakeImage(columns),
                feature_columns=columns,
                observation_year=2024,
            ),
            observation_year=2024,
            config=_multiyear_config(),
        )

    with pytest.raises(ValueError, match="fuera del rango"):
        canonicalize_forest_rf_feature_stack(
            feature_stack=_feature_stack(2020),
            observation_year=2025,
            config=_multiyear_config(),
        )


def test_rf_excludes_exactly_the_12_p0_qa_count_bands() -> None:
    all_p0 = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))
    predictors = forest_rf_predictor_band_names()
    qa = forest_rf_qa_band_names()

    assert len(all_p0) == 68
    assert len(qa) == 12
    assert set(predictors).isdisjoint(qa)
    assert tuple(column for column in all_p0 if column not in predictors) == qa
    assert all("valid_observation_count" in column for column in qa)


def test_geemap_table_is_reconstructed_as_a_tree_ensemble() -> None:
    module = FakeModule()
    config = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_model

    classifier = build_geemap_tree_classifier(module=module, config=config)

    assert isinstance(classifier, FakeClassifier)
    assert module.collection is not None
    assert module.collection.asset_id == config.asset_id
    assert module.collection.aggregated_property == "tree"
    assert module.classifier_trees == ("a\nb", "c\nd")


def test_rf_selects_only_exact_predictors_and_rejects_schema_drift() -> None:
    selected = select_forest_rf_predictors(_feature_stack())

    assert selected.bands == forest_rf_predictor_band_names()
    assert selected.expression.startswith("select(2020_DJF_blue")

    columns = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))
    invalid = SeasonalFeatureStack(
        image=FakeImage(columns[:-1]),
        feature_columns=columns[:-1],
    )
    with pytest.raises(ValueError, match=r"schema P0 inválido.*missing=.*SON_valid"):
        select_forest_rf_predictors(invalid)


def test_rf_outputs_class_raw_vote_fraction_and_scope_metadata() -> None:
    module = FakeModule()
    config = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_model

    result = build_forest_rf_images(
        module=module,
        config=config,
        feature_stack=_feature_stack(),
        aoi_wgs84=box(-58.24, -31.81, -58.19, -31.78),
        generated_at=datetime(2026, 8, 3, 15, tzinfo=UTC),
    )

    assert result.forest_class.bands == (FOREST_RF_CLASS_BAND,)
    assert "CLASSIFICATION" in result.forest_class.expression
    assert "updateMask(" in result.forest_class.expression
    assert result.forest_vote_fraction.bands == (FOREST_RF_VOTE_FRACTION_BAND,)
    assert "RAW" in result.forest_vote_fraction.expression
    assert "arrayReduce[mean" in result.forest_vote_fraction.expression
    assert "updateMask(" in result.forest_vote_fraction.expression
    assert result.input_complete.bands == (FOREST_RF_INPUT_COMPLETE_BAND,)
    assert "reduce[min]" in result.input_complete.expression
    assert "unmask(" in result.input_complete.expression
    assert "updateMask(" in result.input_complete.expression
    assert "clip(constant(1)" in result.input_complete.expression
    assert result.metadata.jurisdiction == "entre_rios"
    assert result.metadata.reference_year == 2020
    assert result.metadata.score_semantics == "uncalibrated_binary_tree_vote_fraction"
    assert result.metadata.calibrated_probability is False
    assert result.metadata.independent_evidence is False
    assert result.metadata.temporal_transfer_validated is False
    assert result.metadata.predictor_band_names == forest_rf_predictor_band_names()
    assert result.metadata.excluded_qa_band_names == forest_rf_qa_band_names()


def test_multiyear_outputs_are_year_named_and_keep_direct_classifier_class() -> None:
    result = build_forest_rf_images(
        module=FakeModule(),
        config=_multiyear_config(),
        feature_stack=_feature_stack(2024),
        observation_year=2024,
        aoi_wgs84=box(-58.24, -31.81, -58.19, -31.78),
        generated_at=datetime(2026, 8, 6, 15, tzinfo=UTC),
    )

    names = forest_rf_output_band_names(2024)
    assert result.forest_class.bands == (names["class"],)
    assert result.forest_vote_fraction.bands == (names["vote_fraction"],)
    assert result.input_complete.bands == (names["input_complete"],)
    assert "CLASSIFICATION" in result.forest_class.expression
    assert "RAW" not in result.forest_class.expression
    assert "RAW" in result.forest_vote_fraction.expression
    assert "valid_observation_count" in result.input_complete.expression
    assert result.metadata.observation_year == 2024
    assert result.metadata.predictor_slot_year == 2020
    assert result.metadata.temporal_transfer_validated is False


def test_rf_config_rejects_scope_or_serialization_drift() -> None:
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_model.model_dump()
    payload["reference_year"] = 2021

    with pytest.raises(ValidationError, match="reference_year"):
        ForestRandomForestConfig.model_validate(payload)

    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_model.model_dump()
    payload["asset_type"] = "native_classifier"
    with pytest.raises(ValidationError, match="asset_type"):
        ForestRandomForestConfig.model_validate(payload)
