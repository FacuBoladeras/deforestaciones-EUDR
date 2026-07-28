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


class CompositionInterval(StrEnum):
    """Intervalos temporales admitidos para composiciones ópticas."""

    MONTHLY = "monthly"
    ANNUAL = "annual"


class CompositionReducer(StrEnum):
    """Reductores explícitos para no ocultar cómo se sintetiza el período."""

    MEDIAN = "median"


class OutputFormat(StrEnum):
    """Formatos mínimos de evidencia de la primera prueba."""

    JSON = "json"
    GEOJSON = "geojson"
    CSV = "csv"
    PNG = "png"
    GEOTIFF = "geotiff"


class IndexVisualizationRange(StrictConfigModel):
    """Escala fija de presentación para un índice; no altera el raster científico."""

    index: SpectralIndex
    minimum: float
    maximum: float

    @model_validator(mode="after")
    def bounds_are_ordered(self) -> IndexVisualizationRange:
        if self.minimum >= self.maximum:
            raise ValueError("minimum debe ser menor que maximum")
        return self


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
    raster_crs_strategy: AreaCrsStrategy
    forest_definition_min_area_ha: PositiveHectares
    minimum_coordinate_decimals: AtLeastSix
    preserve_subthreshold_events: Literal[True]

    @field_validator("raster_crs_strategy")
    @classmethod
    def raster_grid_uses_local_utm(
        cls,
        value: AreaCrsStrategy,
    ) -> AreaCrsStrategy:
        if value is not AreaCrsStrategy.LOCAL_UTM:
            raise ValueError("raster_crs_strategy debe ser local_utm en el Paso 10")
        return value

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
    composition_interval: CompositionInterval
    composition_reducer: CompositionReducer
    source_native_reflectance_scale_factor: float
    source_native_reflectance_offset: float
    earth_engine_reflectance_multiplier: float
    earth_engine_reflectance_offset: float
    mask_high_aerosol: Literal[True]
    preserve_water: Literal[True]
    indices: Annotated[tuple[SpectralIndex, ...], Field(min_length=1)]

    @field_validator("indices")
    @classmethod
    def indices_are_unique(cls, value: tuple[SpectralIndex, ...]) -> tuple[SpectralIndex, ...]:
        """Evita calcular o registrar dos veces la misma variable."""
        if len(value) != len(set(value)):
            raise ValueError("indices no puede contener valores repetidos")
        return value

    @model_validator(mode="after")
    def hls_scaling_is_the_documented_v2_scaling(self) -> DataConfig:
        if self.source_native_reflectance_scale_factor != 0.0001:
            raise ValueError("source_native_reflectance_scale_factor debe ser 0.0001 para HLS v2")
        if self.source_native_reflectance_offset != 0.0:
            raise ValueError("source_native_reflectance_offset debe ser 0 para HLS v2")
        if self.earth_engine_reflectance_multiplier != 1.0:
            raise ValueError(
                "earth_engine_reflectance_multiplier debe ser 1 porque GEE ya entrega "
                "reflectancia escalada"
            )
        if self.earth_engine_reflectance_offset != 0.0:
            raise ValueError("earth_engine_reflectance_offset debe ser 0")
        return self


class OutputConfig(StrictConfigModel):
    """Destino y formatos de la primera prueba."""

    directory: Path
    formats: Annotated[tuple[OutputFormat, ...], Field(min_length=1)]
    raster_nodata: float
    maximum_direct_download_bytes: Annotated[int, Field(gt=0, le=32_000_000)]
    maximum_direct_download_dimension: Annotated[int, Field(gt=0, le=10_000)]
    maximum_series_download_bytes: Annotated[int, Field(gt=0, le=512_000_000)]
    rgb_min_reflectance: float
    rgb_max_reflectance: float
    index_visualization_ranges: Annotated[
        tuple[IndexVisualizationRange, ...],
        Field(min_length=1),
    ]
    png_dpi: Annotated[int, Field(ge=72, le=600)]

    @field_validator("formats")
    @classmethod
    def formats_are_unique(cls, value: tuple[OutputFormat, ...]) -> tuple[OutputFormat, ...]:
        """Evita declarar salidas duplicadas."""
        if len(value) != len(set(value)):
            raise ValueError("formats no puede contener valores repetidos")
        return value

    @model_validator(mode="after")
    def visualization_parameters_are_consistent(self) -> OutputConfig:
        if self.raster_nodata != -9999.0:
            raise ValueError("raster_nodata debe ser -9999")
        if self.rgb_min_reflectance >= self.rgb_max_reflectance:
            raise ValueError("rgb_min_reflectance debe ser menor que rgb_max_reflectance")
        range_indices = tuple(item.index for item in self.index_visualization_ranges)
        if len(range_indices) != len(set(range_indices)):
            raise ValueError("index_visualization_ranges contiene índices repetidos")
        return self

    @property
    def index_visualization_range_map(
        self,
    ) -> dict[SpectralIndex, tuple[float, float]]:
        """Materializa una copia por índice para el render local."""
        return {
            item.index: (item.minimum, item.maximum) for item in self.index_visualization_ranges
        }


class PipelineConfig(StrictConfigModel):
    """Configuración raíz del pipeline."""

    schema_version: Literal["1.2.0"]
    analysis: AnalysisConfig
    spatial: SpatialConfig
    data: DataConfig
    output: OutputConfig

    @model_validator(mode="after")
    def every_requested_index_has_a_visualization_range(self) -> PipelineConfig:
        configured = tuple(item.index for item in self.output.index_visualization_ranges)
        if set(configured) != set(self.data.indices):
            raise ValueError(
                "index_visualization_ranges debe cubrir exactamente los índices solicitados"
            )
        return self


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
