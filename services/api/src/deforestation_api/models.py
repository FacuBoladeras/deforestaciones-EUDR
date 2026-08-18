"""Contratos HTTP versionados de la API interna."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApiGeometry(StrictModel):
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: Annotated[list[Any], Field(min_length=1)]


class ApiFeature(StrictModel):
    type: Literal["Feature"]
    properties: dict[str, Any] = Field(default_factory=dict)
    geometry: ApiGeometry


class GeometryDocument(RootModel[ApiGeometry | ApiFeature]):
    pass


class GeometryValidationResponse(StrictModel):
    geometry_type: Literal["Polygon", "MultiPolygon"]
    source_crs: Literal["EPSG:4326"] = "EPSG:4326"
    valid: Literal[True] = True
    repair_required: bool
    area_ha_estimate: float
    warnings: list[str]


class CreateAnalysisRequest(StrictModel):
    establishment_id: Annotated[str, Field(min_length=1, max_length=200)]
    geometry: ApiGeometry | ApiFeature
    declared_land_use: Literal["unknown", "managed_forest_plantation"] = "unknown"
    declared_context_source: Annotated[str, Field(min_length=1, max_length=200)] = "user_declared"


class CreateAnalysisResponse(StrictModel):
    analysis_id: UUID
    status: str
    status_url: str
    created_at: datetime


class AnalysisStatusResponse(StrictModel):
    analysis_id: UUID
    status: str
    stage: str
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
    attempt: int
    safe_error_code: str | None


class EventPageResponse(StrictModel):
    items: list[dict[str, Any]]
    page: int
    page_size: int
    total: int
    next_page: int | None


class AssetItem(StrictModel):
    asset_id: str
    category: str
    component: str | None
    event_id: str | None
    media_type: str
    size_bytes: int
    sha256: str
    download_url: str


class AssetPageResponse(StrictModel):
    items: list[AssetItem]
    page: int
    page_size: int
    total: int
    next_page: int | None


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    service: Literal["deforestation-api"] = "deforestation-api"
    version: str


class ErrorBody(StrictModel):
    code: str
    message: str
    details: list[str] = Field(default_factory=list)


class ErrorResponse(StrictModel):
    error: ErrorBody
