"""Inferencia anual de RF forestales ingeridos como TABLE geemap en GEE."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.config import (
    FOREST_RF_PREDICTOR_COLUMNS_SHA256,
    ForestRandomForestConfig,
    SpectralIndex,
)
from deforestation_pipeline.seasonal_feature_stack import (
    SEASONAL_FEATURE_SEASONS,
    SEASONAL_QA_COUNT_BANDS,
    SeasonalFeatureStack,
    seasonal_feature_columns,
)

FOREST_RF_METADATA_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
FOREST_RF_MULTYEAR_METADATA_SCHEMA_VERSION: Literal["1.1.0"] = "1.1.0"
FOREST_RF_CLASS_BAND = "rf_forest_class_2020"
FOREST_RF_VOTE_FRACTION_BAND = "rf_forest_vote_fraction_2020"
FOREST_RF_INPUT_COMPLETE_BAND = "rf_forest_input_complete_2020"


class ForestRfMetadata(BaseModel):
    """Procedencia y límites explícitos de las salidas del RF."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0", "1.1.0"] = FOREST_RF_METADATA_SCHEMA_VERSION
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
    observation_year: int = 2020
    predictor_slot_year: Literal[2020] = 2020
    model_training_scope: Literal[
        "p0_2020",
        "multiyear_2020_2024_candidate",
    ] = "p0_2020"
    supported_observation_years: tuple[int, ...] = (2020,)
    predictor_columns_sha256: str = Field(
        default=FOREST_RF_PREDICTOR_COLUMNS_SHA256,
        pattern=r"^[a-f0-9]{64}$",
    )
    serialized_trees_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    joblib_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    inference_backend: Literal[
        "gee_decision_tree_ensemble",
        "local_sklearn_joblib",
    ] = "gee_decision_tree_ensemble"

    @model_validator(mode="after")
    def metadata_is_consistent(self) -> ForestRfMetadata:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        if self.predictor_band_names != forest_rf_predictor_band_names():
            raise ValueError("predictor_band_names no coincide con el contrato RF P0")
        if self.excluded_qa_band_names != forest_rf_qa_band_names():
            raise ValueError("excluded_qa_band_names no coincide con las bandas QA P0")
        if self.predictor_columns_sha256 != forest_rf_predictor_columns_sha256():
            raise ValueError("predictor_columns_sha256 no coincide con el orden RF")
        if self.observation_year not in self.supported_observation_years:
            raise ValueError("observation_year está fuera del rango soportado")
        if self.schema_version == "1.0.0" and (
            self.observation_year != 2020
            or self.supported_observation_years != (2020,)
            or self.model_training_scope != "p0_2020"
        ):
            raise ValueError("metadata RF 1.0.0 sólo admite P0 2020")
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


def forest_rf_predictor_columns_sha256() -> str:
    """Hash canónico del orden de los 56 slots consumidos por el RF."""
    payload = json.dumps(
        list(forest_rf_predictor_band_names()),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != FOREST_RF_PREDICTOR_COLUMNS_SHA256:
        raise RuntimeError("el hash de los predictores RF no coincide con el contrato")
    return digest


def forest_rf_qa_band_names() -> tuple[str, ...]:
    """Bandas P0 preservadas para QA, nunca enviadas al clasificador."""
    return tuple(
        f"2020_{season}_{suffix}"
        for season in SEASONAL_FEATURE_SEASONS
        for suffix in SEASONAL_QA_COUNT_BANDS
    )


def forest_rf_output_band_names(observation_year: int) -> dict[str, str]:
    """Nombres anuales estables sin confundir el año observado con los slots 2020."""
    if observation_year < 1900 or observation_year > 9999:
        raise ValueError("observation_year fuera de rango")
    return {
        "class": f"rf_forest_class_{observation_year}",
        "vote_fraction": f"rf_forest_vote_fraction_{observation_year}",
        "input_complete": f"rf_forest_input_complete_{observation_year}",
    }


def canonicalize_forest_rf_feature_stack(
    *,
    feature_stack: SeasonalFeatureStack,
    observation_year: int,
    config: ForestRandomForestConfig,
) -> SeasonalFeatureStack:
    """Renombra un stack anual homólogo a los slots 2020 sin tocar sus valores."""
    if observation_year not in config.supported_observation_years:
        raise ValueError(
            f"observation_year {observation_year} fuera del rango configurado "
            f"{config.supported_observation_years}"
        )
    if feature_stack.observation_year not in {None, observation_year}:
        raise ValueError("mixed_year: observation_year no coincide con el stack")

    actual = feature_stack.feature_columns
    expected_source = seasonal_feature_columns(
        year=observation_year,
        indices=tuple(SpectralIndex),
    )
    canonical = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))
    duplicates = tuple(sorted({name for name in actual if actual.count(name) > 1}))
    if duplicates:
        raise ValueError(f"duplicate={duplicates}")
    declared_years = {
        int(match.group(1))
        for name in actual
        if (match := re.match(r"^(\d{4})_", name)) is not None
    }
    if declared_years != {observation_year}:
        raise ValueError(
            f"mixed_year: expected={observation_year}, observed={tuple(sorted(declared_years))}"
        )
    if actual != expected_source:
        missing = tuple(name for name in expected_source if name not in actual)
        unexpected = tuple(name for name in actual if name not in expected_source)
        order_mismatch = not missing and not unexpected
        raise ValueError(
            "schema anual RF inválido: "
            f"missing={missing}, unexpected={unexpected}, order_mismatch={order_mismatch}"
        )
    image = feature_stack.image.select(list(actual), list(canonical))
    return SeasonalFeatureStack(
        image=image,
        feature_columns=canonical,
        observation_year=observation_year,
        periods=feature_stack.periods,
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
    observation_year: int = 2020,
    aoi_wgs84: BaseGeometry,
    generated_at: datetime,
) -> ForestRfImages:
    """Clasifica un año y deriva la fracción media de votos binarios en modo RAW."""
    canonical_stack = canonicalize_forest_rf_feature_stack(
        feature_stack=feature_stack,
        observation_year=observation_year,
        config=config,
    )
    predictors = select_forest_rf_predictors(canonical_stack)
    remote_aoi = module.Geometry(mapping(aoi_wgs84))
    aoi_mask = module.Image.constant(1).clip(remote_aoi)
    required_observation_counts = canonical_stack.image.select(
        [f"2020_{season}_valid_observation_count" for season in SEASONAL_FEATURE_SEASONS]
    )
    required_support = required_observation_counts.gt(0).reduce(module.Reducer.min())
    input_complete = (
        predictors.mask()
        .reduce(module.Reducer.min())
        .And(required_support)
        .unmask(0)
        .updateMask(aoi_mask)
        .rename(forest_rf_output_band_names(observation_year)["input_complete"])
        .toUint8()
    )
    complete_input_mask = input_complete.eq(1)
    classifier = build_geemap_tree_classifier(module=module, config=config)
    forest_class = (
        predictors.classify(classifier)
        .updateMask(complete_input_mask)
        .rename(forest_rf_output_band_names(observation_year)["class"])
        .toUint8()
    )
    raw_votes = predictors.classify(classifier.setOutputMode("RAW"))
    forest_vote_fraction = (
        raw_votes.arrayReduce(module.Reducer.mean(), [0])
        .arrayGet([0])
        .updateMask(complete_input_mask)
        .rename(forest_rf_output_band_names(observation_year)["vote_fraction"])
        .toFloat()
    )
    return ForestRfImages(
        forest_class=forest_class,
        forest_vote_fraction=forest_vote_fraction,
        input_complete=input_complete,
        metadata=ForestRfMetadata(
            schema_version=(
                FOREST_RF_METADATA_SCHEMA_VERSION
                if config.schema_version == "1.0.0"
                else FOREST_RF_MULTYEAR_METADATA_SCHEMA_VERSION
            ),
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
            observation_year=observation_year,
            predictor_slot_year=config.predictor_slot_year,
            model_training_scope=config.model_training_scope,
            supported_observation_years=config.supported_observation_years,
            predictor_columns_sha256=config.predictor_columns_sha256,
            serialized_trees_sha256=config.serialized_trees_sha256,
            joblib_sha256=config.joblib_sha256,
            inference_backend=config.inference_backend,
        ),
    )
