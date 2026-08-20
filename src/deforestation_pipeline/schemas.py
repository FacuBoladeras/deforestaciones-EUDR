"""Contratos de entrada, eventos, procedencia y salida."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from math import isclose, isfinite
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pyproj import CRS
from pyproj.exceptions import CRSError

from deforestation_domain.schemas import GeoJSONGeometry as GeoJSONGeometry
from deforestation_domain.schemas import StrictModel as StrictModel

EUDR_CUTOFF_DATE = date(2020, 12, 31)
SUMMARY_SCHEMA_VERSION = "1.0.0"
SUMMARY_SCHEMA_ID = "urn:deforestation-pipeline:analysis-summary:1.0.0"
RASTER_GRID_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
RASTER_GRID_SCHEMA_ID = "urn:deforestation-pipeline:raster-grid:1.0.0"

NonEmptyString = Annotated[str, Field(min_length=1)]
NonNegativeHectares = Annotated[float, Field(ge=0)]
Probability = Annotated[float, Field(ge=0, le=1)]
Sha256Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
AffineTransform = tuple[float, float, float, float, float, float]
RasterBounds = tuple[float, float, float, float]
RasterGridAlignment = Literal["projected_crs_origin_outward_snap"]
RasterPixelOrientation = Literal["north_up"]


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


class RasterGridSpec(StrictModel):
    """Contrato espacial inmutable para comparar productos píxel a píxel."""

    schema_version: Literal["1.0.0"] = RASTER_GRID_SCHEMA_VERSION
    target_crs: NonEmptyString
    resolution_m: Annotated[float, Field(gt=0)]
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]
    transform: AffineTransform
    bounds: RasterBounds
    alignment_strategy: RasterGridAlignment
    pixel_orientation: RasterPixelOrientation
    nodata: float
    aoi_mask_required: Literal[True]
    grid_sha256: Sha256Digest

    @field_validator("target_crs")
    @classmethod
    def target_crs_is_projected_in_metres(cls, value: str) -> str:
        """Normaliza el CRS y excluye sistemas no proyectados o no métricos."""
        try:
            crs = CRS.from_user_input(value)
        except CRSError:
            raise ValueError("target_crs debe ser un CRS reconocible") from None
        if not crs.is_projected:
            raise ValueError("target_crs debe ser proyectado")
        axis_units = {axis.unit_name.lower() for axis in crs.axis_info if axis.unit_name}
        if not axis_units or not axis_units <= {"metre", "meter"}:
            raise ValueError("target_crs debe usar metros")
        authority = crs.to_authority()
        return ":".join(authority) if authority is not None else crs.to_string()

    @model_validator(mode="after")
    def grid_is_internally_consistent(self) -> RasterGridSpec:
        """Impide deserializar una identidad espacial parcial o adulterada."""
        numeric_values = (
            self.resolution_m,
            self.nodata,
            *self.transform,
            *self.bounds,
        )
        if not all(isfinite(value) for value in numeric_values):
            raise ValueError("la grilla sólo admite valores numéricos finitos")

        x_scale, x_shear, x_origin, y_shear, y_scale, y_origin = self.transform
        if not (
            isclose(x_scale, self.resolution_m, abs_tol=1e-9)
            and isclose(y_scale, -self.resolution_m, abs_tol=1e-9)
            and isclose(x_shear, 0.0, abs_tol=1e-12)
            and isclose(y_shear, 0.0, abs_tol=1e-12)
        ):
            raise ValueError("transform no representa una grilla norte-arriba a resolution_m")

        left, bottom, right, top = self.bounds
        if not left < right or not bottom < top:
            raise ValueError("bounds debe seguir el orden left,bottom,right,top")
        expected_bounds = (
            x_origin,
            y_origin + y_scale * self.height,
            x_origin + x_scale * self.width,
            y_origin,
        )
        if not all(
            isclose(actual, expected, abs_tol=1e-8)
            for actual, expected in zip(self.bounds, expected_bounds, strict=True)
        ):
            raise ValueError("bounds no coinciden con transform, width y height")

        expected_hash = raster_grid_sha256(
            target_crs=self.target_crs,
            resolution_m=self.resolution_m,
            width=self.width,
            height=self.height,
            transform=self.transform,
            bounds=self.bounds,
            alignment_strategy=self.alignment_strategy,
            pixel_orientation=self.pixel_orientation,
        )
        if self.grid_sha256 != expected_hash:
            raise ValueError("grid_sha256 no coincide con la identidad espacial")
        return self


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


def raster_grid_json_schema() -> dict[str, Any]:
    """Devuelve el JSON Schema canónico de la grilla raster 1.0.0."""
    schema = RasterGridSpec.model_json_schema(mode="serialization")
    schema["$id"] = RASTER_GRID_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = RASTER_GRID_SCHEMA_VERSION
    return schema


def raster_grid_sha256(
    *,
    target_crs: str,
    resolution_m: float,
    width: int,
    height: int,
    transform: AffineTransform,
    bounds: RasterBounds,
    alignment_strategy: RasterGridAlignment,
    pixel_orientation: RasterPixelOrientation,
) -> str:
    """Identifica sólo la grilla espacial, sin mezclar codificación ni máscara."""
    payload = {
        "alignment_strategy": alignment_strategy,
        "bounds": bounds,
        "height": height,
        "pixel_orientation": pixel_orientation,
        "resolution_m": resolution_m,
        "target_crs": target_crs,
        "transform": transform,
        "width": width,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_timezone(value: datetime, field_name: str) -> datetime:
    """Valida que una fecha y hora incluya desplazamiento UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} debe incluir zona horaria")
    return value
