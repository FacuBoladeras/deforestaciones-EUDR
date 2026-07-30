"""Contrato científico de la línea base forestal al 31 de diciembre de 2020."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deforestation_pipeline.artifact_layout import (
    evidence_figure,
    evidence_json,
    evidence_tiff,
)
from deforestation_pipeline.schemas import EUDR_CUTOFF_DATE

FOREST_BASELINE_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
FOREST_BASELINE_SCHEMA_ID = "urn:deforestation-pipeline:forest-baseline:1.0.0"


class StrictForestBaselineModel(BaseModel):
    """Base inmutable que rechaza parámetros metodológicos desconocidos."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ForestDefinitionConfig(StrictForestBaselineModel):
    """Umbrales de dominio que no pueden calibrarse para mejorar métricas."""

    minimum_area_ha: Annotated[float, Field(gt=0)]
    minimum_tree_height_m: Annotated[float, Field(gt=0)]
    minimum_canopy_cover_percent: Annotated[float, Field(gt=0, le=100)]
    exclude_predominantly_agricultural_or_urban: Literal[True]

    @model_validator(mode="after")
    def thresholds_match_domain_definition(self) -> ForestDefinitionConfig:
        if self.minimum_area_ha != 0.5:
            raise ValueError("minimum_area_ha debe ser 0.5")
        if self.minimum_tree_height_m != 5.0:
            raise ValueError("minimum_tree_height_m debe ser 5")
        if self.minimum_canopy_cover_percent != 10.0:
            raise ValueError("minimum_canopy_cover_percent debe ser 10")
        return self


class ForestBaselineConfig(StrictForestBaselineModel):
    """Especificación metodológica previa a consultar o clasificar datos."""

    schema_version: Literal["1.0.0"] = FOREST_BASELINE_SCHEMA_VERSION
    reference_date: date
    feature_start_date: date
    feature_end_date_exclusive: date
    benchmark_resolution_m: Literal[30]
    minimum_independent_sources: Annotated[int, Field(ge=2)]
    forest_definition: ForestDefinitionConfig
    preserve_source_evidence: Literal[True]
    preserve_disagreement: Literal[True]
    post_cutoff_observations_allowed: Literal[False]
    long_gap_interpolation_allowed: Literal[False]

    @field_validator("reference_date")
    @classmethod
    def reference_date_is_fixed(cls, value: date) -> date:
        if value != EUDR_CUTOFF_DATE:
            raise ValueError("reference_date debe ser 2020-12-31")
        return value

    @model_validator(mode="after")
    def temporal_scope_prevents_future_leakage(self) -> ForestBaselineConfig:
        expected_end = self.reference_date + timedelta(days=1)
        if self.feature_end_date_exclusive != expected_end:
            raise ValueError("feature_end_date_exclusive debe ser 2021-01-01")
        if (self.feature_start_date.month, self.feature_start_date.day) != (1, 1):
            raise ValueError("feature_start_date debe comenzar el 1 de enero")
        if self.feature_start_date >= self.reference_date:
            raise ValueError("feature_start_date debe ser anterior a reference_date")
        return self


def forest_baseline_output_paths() -> dict[str, str]:
    """Define un conjunto compacto y estable de artefactos, sin crear directorios."""
    return {
        "metadata": evidence_json("forest_baseline_2020.json"),
        "qa_figure": evidence_figure("forest_baseline_2020.png"),
        "evidence_fraction": evidence_tiff("forest_evidence_fraction_2020.tif"),
        "consensus": evidence_tiff("forest_consensus_2020.tif"),
        "disagreement": evidence_tiff("forest_disagreement_2020.tif"),
        "source_count": evidence_tiff("forest_source_count_2020.tif"),
        "source_evidence": evidence_tiff("forest_source_evidence_2020.tif"),
        "features": evidence_tiff("forest_features_2020.tif"),
    }


def forest_baseline_json_schema() -> dict[str, Any]:
    """Devuelve el JSON Schema canónico del contrato metodológico 1.0.0."""
    schema = ForestBaselineConfig.model_json_schema(mode="serialization")
    schema["$id"] = FOREST_BASELINE_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = FOREST_BASELINE_SCHEMA_VERSION
    return schema
