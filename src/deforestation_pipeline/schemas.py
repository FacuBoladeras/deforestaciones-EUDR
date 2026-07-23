"""Contratos de entrada, eventos, procedencia y salida."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EUDR_CUTOFF_DATE = date(2020, 12, 31)
SUMMARY_SCHEMA_VERSION = "1.0.0"
SUMMARY_SCHEMA_ID = "urn:deforestation-pipeline:analysis-summary:1.0.0"

NonEmptyString = Annotated[str, Field(min_length=1)]
NonNegativeHectares = Annotated[float, Field(ge=0)]
Probability = Annotated[float, Field(ge=0, le=1)]
Sha256Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class StrictModel(BaseModel):
    """Base inmutable que impide campos silenciosos fuera del contrato."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class GeometrySource(StrEnum):
    """Procedencia declarada de una geometría."""

    DECLARED = "declared"
    RENSPA = "renspa"
    OTHER = "other"


class AssessmentStatus(StrEnum):
    """Estados públicos permitidos por el contrato de salida 1.0.0."""

    LOW_RISK = "low_risk"
    REVIEW_REQUIRED = "review_required"
    CONVERSION_LIKELY = "conversion_likely"
    INSUFFICIENT_DATA = "insufficient_data"


class PostChangeLandUse(StrEnum):
    """Coberturas posteriores consideradas por la etapa de atribución."""

    FOREST_OR_REGENERATION = "forest_or_regeneration"
    PASTURE = "pasture"
    CROPLAND = "cropland"
    PERSISTENT_BARE_SOIL = "persistent_bare_soil"
    INFRASTRUCTURE = "infrastructure"
    WATER = "water"
    FIRE_OR_TEMPORARY_DISTURBANCE = "fire_or_temporary_disturbance"
    UNKNOWN = "unknown"


class GeoJSONGeometry(StrictModel):
    """Estructura GeoJSON admitida antes de la validación espacial."""

    type: Literal["Point", "Polygon", "MultiPolygon"]
    coordinates: Annotated[list[Any], Field(min_length=1)]


class EstablishmentInput(StrictModel):
    """Entrada mínima y versionada de un establecimiento."""

    establishment_id: NonEmptyString
    geometry: GeoJSONGeometry
    geometry_crs: Annotated[str, Field(pattern=r"^EPSG:\d+$")]
    geometry_source: GeometrySource
    geometry_version: NonEmptyString
    geometry_registered_at: datetime
    province: str | None = None
    ecoregion: str | None = None

    @field_validator("geometry_registered_at")
    @classmethod
    def geometry_date_has_timezone(cls, value: datetime) -> datetime:
        """Exige una fecha auditable e inequívoca."""
        return _require_timezone(value, "geometry_registered_at")


class DatasetRecord(StrictModel):
    """Procedencia y condiciones de uso de un dataset efectivamente utilizado."""

    dataset_id: NonEmptyString
    provider: NonEmptyString
    collection: NonEmptyString
    version: NonEmptyString
    license: NonEmptyString
    access_date: date
    attribution: NonEmptyString
    restrictions: list[str]


class LicenseRegistry(StrictModel):
    """Registro versionado de licencias verificadas."""

    schema_version: Literal["1.0.0"]
    datasets: list[DatasetRecord]

    @field_validator("datasets")
    @classmethod
    def dataset_ids_are_unique(cls, value: list[DatasetRecord]) -> list[DatasetRecord]:
        """Evita referencias ambiguas en el manifiesto."""
        identifiers = [dataset.dataset_id for dataset in value]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("datasets contiene dataset_id repetidos")
        return value


class ChangeEvent(StrictModel):
    """Evento de cambio separado de una conclusión legal o humana."""

    event_id: UUID
    start_date: date
    end_date: date | None
    detected_change_area_ha: NonNegativeHectares
    likely_conversion_area_ha: NonNegativeHectares
    change_magnitude: float
    persistence: Probability
    sensors: Annotated[list[NonEmptyString], Field(min_length=1)]
    post_change_land_use: PostChangeLandUse
    confidence: Probability
    quality_flags: list[str]
    reason_codes: list[str]

    @model_validator(mode="after")
    def event_is_consistent(self) -> ChangeEvent:
        """Valida temporalidad y relación entre superficies."""
        if self.start_date <= EUDR_CUTOFF_DATE:
            raise ValueError("start_date debe ser posterior a 2020-12-31")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date no puede ser anterior a start_date")
        if self.likely_conversion_area_ha > self.detected_change_area_ha:
            raise ValueError("likely_conversion_area_ha no puede superar detected_change_area_ha")
        return self


class AnalysisSummary(StrictModel):
    """Contrato 1.0.0 del resumen por establecimiento."""

    establishment_id: NonEmptyString
    analysis_id: UUID
    analysis_version: NonEmptyString
    cutoff_date: date = EUDR_CUTOFF_DATE
    analysis_end_date: date
    geometry_source: GeometrySource
    forest_area_2020_ha: NonNegativeHectares
    detected_change_area_ha: NonNegativeHectares
    likely_conversion_area_ha: NonNegativeHectares
    status: AssessmentStatus
    confidence: Probability
    quality_flags: list[str]
    events: list[ChangeEvent]
    datasets: list[DatasetRecord]
    parameters_hash: Sha256Digest
    created_at: datetime

    @field_validator("cutoff_date")
    @classmethod
    def cutoff_date_is_fixed(cls, value: date) -> date:
        """Impide cambiar la fecha ambiental sin alterar el código de dominio."""
        if value != EUDR_CUTOFF_DATE:
            raise ValueError("cutoff_date debe ser 2020-12-31")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        """Exige una marca temporal inequívoca."""
        return _require_timezone(value, "created_at")

    @model_validator(mode="after")
    def summary_is_consistent(self) -> AnalysisSummary:
        """Valida fechas y superficies agregadas."""
        if self.analysis_end_date <= self.cutoff_date:
            raise ValueError("analysis_end_date debe ser posterior a cutoff_date")
        if self.likely_conversion_area_ha > self.detected_change_area_ha:
            raise ValueError("likely_conversion_area_ha no puede superar detected_change_area_ha")
        return self


def analysis_summary_json_schema() -> dict[str, Any]:
    """Devuelve el JSON Schema canónico del resumen 1.0.0."""
    schema = AnalysisSummary.model_json_schema(mode="serialization")
    schema["$id"] = SUMMARY_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = SUMMARY_SCHEMA_VERSION
    return schema


def _require_timezone(value: datetime, field_name: str) -> datetime:
    """Valida que una fecha y hora incluya desplazamiento UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} debe incluir zona horaria")
    return value
