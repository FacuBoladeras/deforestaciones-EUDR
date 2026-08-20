"""Contratos mínimos de geometría sin dependencias del pipeline científico."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base inmutable que impide campos silenciosos fuera del contrato."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class GeoJSONGeometry(StrictModel):
    """Estructura GeoJSON admitida antes de la validación espacial."""

    type: Literal["Point", "Polygon", "MultiPolygon"]
    coordinates: Annotated[list[Any], Field(min_length=1)]
