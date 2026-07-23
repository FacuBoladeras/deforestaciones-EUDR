"""Configuración versionada y determinística del pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deforestation_pipeline.schemas import EUDR_CUTOFF_DATE, LicenseRegistry

AtLeastSix = Annotated[int, Field(ge=6)]
PositiveHectares = Annotated[float, Field(gt=0)]


class ConfigurationError(ValueError):
    """Error legible al cargar un archivo de configuración."""


class StrictConfigModel(BaseModel):
    """Base inmutable que rechaza parámetros desconocidos."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AreaCrsStrategy(StrEnum):
    """Estrategias admitidas para seleccionar un CRS de superficie."""

    AUTO_EQUAL_AREA = "auto_equal_area"
    LOCAL_UTM = "local_utm"


class SpectralIndex(StrEnum):
    """Índices ópticos candidatos para el benchmark inicial."""

    NDVI = "NDVI"
    EVI2 = "EVI2"
    NBR = "NBR"
    NDMI = "NDMI"
    NMDI = "NMDI"
    LSWI = "LSWI"
    NIRV = "NIRv"
    KNDVI = "kNDVI"


class OutputFormat(StrEnum):
    """Formatos mínimos de evidencia de la primera prueba."""

    JSON = "json"
    GEOJSON = "geojson"
    CSV = "csv"
    PNG = "png"


class AnalysisConfig(StrictConfigModel):
    """Período y semilla de una ejecución."""

    cutoff_date: date
    benchmark_start_date: date
    analysis_end_date: date | None
    random_seed: int

    @field_validator("cutoff_date")
    @classmethod
    def cutoff_date_is_fixed(cls, value: date) -> date:
        """Impide convertir la fecha regulatoria en un hiperparámetro."""
        if value != EUDR_CUTOFF_DATE:
            raise ValueError("cutoff_date debe ser 2020-12-31")
        return value

    @model_validator(mode="after")
    def dates_are_ordered(self) -> AnalysisConfig:
        """Valida el orden temporal sin resolver todavía la fecha final nula."""
        if self.benchmark_start_date >= self.cutoff_date:
            raise ValueError("benchmark_start_date debe ser anterior a cutoff_date")
        if self.analysis_end_date is not None and self.analysis_end_date <= self.cutoff_date:
            raise ValueError("analysis_end_date debe ser posterior a cutoff_date")
        return self


class SpatialConfig(StrictConfigModel):
    """Reglas espaciales declarativas previas al procesamiento."""

    interchange_crs: Literal["EPSG:4326"]
    area_crs_strategy: AreaCrsStrategy
    forest_definition_min_area_ha: PositiveHectares
    minimum_coordinate_decimals: AtLeastSix
    preserve_subthreshold_events: Literal[True]

    @field_validator("forest_definition_min_area_ha")
    @classmethod
    def forest_area_threshold_is_fixed(cls, value: float) -> float:
        """Impide ajustar el umbral regulatorio para modificar métricas."""
        if value != 0.5:
            raise ValueError("forest_definition_min_area_ha debe ser 0.5")
        return value


class DataConfig(StrictConfigModel):
    """Selección reproducible de sensor, resolución e índices."""

    benchmark_sensor: Literal["HLS"]
    target_resolution_m: Literal[30]
    composition_interval: Literal["monthly"]
    indices: Annotated[tuple[SpectralIndex, ...], Field(min_length=1)]

    @field_validator("indices")
    @classmethod
    def indices_are_unique(cls, value: tuple[SpectralIndex, ...]) -> tuple[SpectralIndex, ...]:
        """Evita calcular o registrar dos veces la misma variable."""
        if len(value) != len(set(value)):
            raise ValueError("indices no puede contener valores repetidos")
        return value


class OutputConfig(StrictConfigModel):
    """Destino y formatos de la primera prueba."""

    directory: Path
    formats: Annotated[tuple[OutputFormat, ...], Field(min_length=1)]

    @field_validator("formats")
    @classmethod
    def formats_are_unique(cls, value: tuple[OutputFormat, ...]) -> tuple[OutputFormat, ...]:
        """Evita declarar salidas duplicadas."""
        if len(value) != len(set(value)):
            raise ValueError("formats no puede contener valores repetidos")
        return value


class PipelineConfig(StrictConfigModel):
    """Configuración raíz del pipeline."""

    schema_version: Literal["1.0.0"]
    analysis: AnalysisConfig
    spatial: SpatialConfig
    data: DataConfig
    output: OutputConfig


class ResolvedAnalysisConfig(AnalysisConfig):
    """Período de una corrida cuya fecha final ya fue fijada explícitamente."""

    analysis_end_date: date


class ResolvedPipelineConfig(PipelineConfig):
    """Configuración inmutable lista para identificar y ejecutar una corrida."""

    analysis: ResolvedAnalysisConfig


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Carga un documento YAML cuyo nodo raíz debe ser un objeto."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(f"No se pudo leer {path}: {error}") from error

    try:
        document: object = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ConfigurationError(f"YAML inválido en {path}: {error}") from error

    if not isinstance(document, dict):
        raise ConfigurationError(f"El nodo raíz de {path} debe ser un objeto YAML")
    return document


def load_config(path: Path) -> PipelineConfig:
    """Carga y valida una configuración del pipeline."""
    return PipelineConfig.model_validate(_load_yaml_mapping(path))


def load_license_registry(path: Path) -> LicenseRegistry:
    """Carga y valida el registro auditable de licencias."""
    return LicenseRegistry.model_validate(_load_yaml_mapping(path))


def resolve_run_config(config: PipelineConfig, analysis_end_date: date) -> ResolvedPipelineConfig:
    """Fija la fecha final de una corrida sin mutar su configuración de origen."""
    declared_end_date = config.analysis.analysis_end_date
    if declared_end_date is not None and declared_end_date != analysis_end_date:
        raise ValueError("analysis_end_date declarada no coincide con la fecha final de la corrida")

    payload = config.model_dump(mode="python")
    payload["analysis"]["analysis_end_date"] = analysis_end_date
    return ResolvedPipelineConfig.model_validate(payload)


def _canonical_hash(payload: object) -> str:
    """Calcula SHA-256 sobre una representación JSON canónica."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parameters_hash(config: PipelineConfig) -> str:
    """Calcula SHA-256 sobre la representación JSON canónica de la configuración."""
    return _canonical_hash(config.model_dump(mode="json"))


def scientific_parameters_hash(config: ResolvedPipelineConfig) -> str:
    """Identifica los parámetros metodológicos de una corrida ya resuelta."""
    payload = config.model_dump(mode="json")
    scientific_payload = {
        field: payload[field] for field in ("schema_version", "analysis", "spatial", "data")
    }
    return _canonical_hash(scientific_payload)


def execution_config_hash(config: ResolvedPipelineConfig) -> str:
    """Identifica la configuración completa, incluidos detalles operativos."""
    return _canonical_hash(config.model_dump(mode="json"))
