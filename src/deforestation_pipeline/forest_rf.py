"""Inferencia del RF P0 de bosque 2020 ingerido como TABLE geemap en GEE."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.config import ForestRandomForestConfig, SpectralIndex
from deforestation_pipeline.seasonal_feature_stack import (
    SEASONAL_FEATURE_SEASONS,
    SEASONAL_QA_COUNT_BANDS,
    SeasonalFeatureStack,
    seasonal_feature_columns,
)

FOREST_RF_METADATA_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
FOREST_RF_CLASS_BAND = "rf_forest_class_2020"
FOREST_RF_VOTE_FRACTION_BAND = "rf_forest_vote_fraction_2020"
FOREST_RF_INPUT_COMPLETE_BAND = "rf_forest_input_complete_2020"


class ForestRfMetadata(BaseModel):
    """Procedencia y límites explícitos de las salidas del RF."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = FOREST_RF_METADATA_SCHEMA_VERSION
    generated_at: datetime
    asset_id: str
    asset_type: Literal["geemap_tree_feature_collection"]
    tree_property: Literal["tree"]
    expected_tree_count: Literal[500]
    jurisdiction: Literal["entre_rios"]
    reference_year: Literal[2020]
    non_forest_class_code: Literal[0]
    forest_class_code: Literal[1]
    predictor_band_names: Annotated[tuple[str, ...], Field(min_length=56, max_length=56)]
    excluded_qa_band_names: Annotated[tuple[str, ...], Field(min_length=12, max_length=12)]
    training_label_semantics: Literal["multisource_unanimity_proxy_v1"]
    validation_reference: Literal["held_out_spatial_proxy_labels_not_independent_ground_truth"]
    score_semantics: Literal["uncalibrated_binary_tree_vote_fraction"]
    evidence_role: Literal["learned_supporting_evidence"]
    calibrated_probability: Literal[False] = False
    independent_evidence: Literal[False] = False
    temporal_transfer_validated: Literal[False] = False
    final_assessment_generated: Literal[False] = False

    @model_validator(mode="after")
    def metadata_is_consistent(self) -> ForestRfMetadata:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        if self.predictor_band_names != forest_rf_predictor_band_names():
            raise ValueError("predictor_band_names no coincide con el contrato RF P0")
        if self.excluded_qa_band_names != forest_rf_qa_band_names():
            raise ValueError("excluded_qa_band_names no coincide con las bandas QA P0")
        return self


@dataclass(frozen=True, slots=True)
class ForestRfImages:
    """Imágenes GEE perezosas de estado forestal; no son una conclusión EUDR."""

    forest_class: Any = field(repr=False, compare=False)
    forest_vote_fraction: Any = field(repr=False, compare=False)
    input_complete: Any = field(repr=False, compare=False)
    metadata: ForestRfMetadata


def forest_rf_predictor_band_names() -> tuple[str, ...]:
    """Devuelve el esquema exacto y ordenado de 56 predictores del asset P0."""
    all_columns = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))
    qa_columns = set(forest_rf_qa_band_names())
    predictors = tuple(column for column in all_columns if column not in qa_columns)
    if len(predictors) != 56:
        raise RuntimeError("el contrato RF P0 debe contener exactamente 56 predictores")
    return predictors


def forest_rf_qa_band_names() -> tuple[str, ...]:
    """Bandas P0 preservadas para QA, nunca enviadas al clasificador."""
    return tuple(
        f"2020_{season}_{suffix}"
        for season in SEASONAL_FEATURE_SEASONS
        for suffix in SEASONAL_QA_COUNT_BANDS
    )


def select_forest_rf_predictors(feature_stack: SeasonalFeatureStack) -> Any:
    """Valida el stack P0 completo y selecciona los 56 inputs por nombre y orden."""
    expected = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))
    actual = feature_stack.feature_columns
    if actual != expected:
        missing = tuple(column for column in expected if column not in actual)
        unexpected = tuple(column for column in actual if column not in expected)
        order_mismatch = not missing and not unexpected
        raise ValueError(
            "schema P0 inválido: "
            f"missing={missing}, unexpected={unexpected}, order_mismatch={order_mismatch}"
        )
    return feature_stack.image.select(list(forest_rf_predictor_band_names()))


def build_geemap_tree_classifier(*, module: Any, config: ForestRandomForestConfig) -> Any:
    """Reconstruye el ensemble desde árboles serializados por geemap en una TABLE."""
    serialized_trees = module.FeatureCollection(config.asset_id).aggregate_array(
        config.tree_property
    )
    trees = serialized_trees.map(lambda value: module.String(value).replace("#", "\n", "g"))
    return module.Classifier.decisionTreeEnsemble(trees)


def build_forest_rf_images(
    *,
    module: Any,
    config: ForestRandomForestConfig,
    feature_stack: SeasonalFeatureStack,
    aoi_wgs84: BaseGeometry,
    generated_at: datetime,
) -> ForestRfImages:
    """Clasifica 2020 y deriva la fracción media de votos binarios en modo RAW."""
    predictors = select_forest_rf_predictors(feature_stack)
    remote_aoi = module.Geometry(mapping(aoi_wgs84))
    aoi_mask = module.Image.constant(1).clip(remote_aoi)
    input_complete = (
        predictors.mask()
        .reduce(module.Reducer.min())
        .unmask(0)
        .updateMask(aoi_mask)
        .rename(FOREST_RF_INPUT_COMPLETE_BAND)
        .toUint8()
    )
    complete_input_mask = input_complete.eq(1)
    classifier = build_geemap_tree_classifier(module=module, config=config)
    forest_class = (
        predictors.classify(classifier)
        .updateMask(complete_input_mask)
        .rename(FOREST_RF_CLASS_BAND)
        .toUint8()
    )
    raw_votes = predictors.classify(classifier.setOutputMode("RAW"))
    forest_vote_fraction = (
        raw_votes.arrayReduce(module.Reducer.mean(), [0])
        .arrayGet([0])
        .updateMask(complete_input_mask)
        .rename(FOREST_RF_VOTE_FRACTION_BAND)
        .toFloat()
    )
    return ForestRfImages(
        forest_class=forest_class,
        forest_vote_fraction=forest_vote_fraction,
        input_complete=input_complete,
        metadata=ForestRfMetadata(
            generated_at=generated_at,
            asset_id=config.asset_id,
            asset_type=config.asset_type,
            tree_property=config.tree_property,
            expected_tree_count=config.expected_tree_count,
            jurisdiction=config.jurisdiction,
            reference_year=config.reference_year,
            non_forest_class_code=config.non_forest_class_code,
            forest_class_code=config.forest_class_code,
            predictor_band_names=forest_rf_predictor_band_names(),
            excluded_qa_band_names=forest_rf_qa_band_names(),
            training_label_semantics=config.training_label_semantics,
            validation_reference=("held_out_spatial_proxy_labels_not_independent_ground_truth"),
            score_semantics=config.score_semantics,
            evidence_role=config.evidence_role,
            calibrated_probability=config.calibrated_probability,
            independent_evidence=config.independent_evidence,
            temporal_transfer_validated=config.temporal_transfer_validated,
        ),
    )
