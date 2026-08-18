"""Adaptador entre GeoJSON HTTP y las reglas GIS existentes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from deforestation_api.models import ApiFeature, ApiGeometry
from deforestation_pipeline.area import measure_area
from deforestation_pipeline.geometry import GeometryValidationError, validate_geometry
from deforestation_pipeline.schemas import GeoJSONGeometry


class GeometryComplexityError(ValueError):
    """La cantidad de vértices supera el límite operativo."""


@dataclass(frozen=True, slots=True)
class GeometryAssessment:
    geometry_type: str
    repair_performed: bool
    area_ha: float
    warnings: tuple[str, ...]


def load_jurisdiction_boundary(path: Path) -> BaseGeometry:
    if not path.is_file():
        raise FileNotFoundError(f"jurisdiction_boundary_missing:{path.name}")
    document = json.loads(path.read_text(encoding="utf-8"))
    document_type = document.get("type")
    if document_type == "FeatureCollection":
        geometries = [shape(feature["geometry"]) for feature in document.get("features", [])]
        boundary = unary_union(geometries)
    elif document_type == "Feature":
        boundary = shape(document["geometry"])
    else:
        boundary = shape(document)
    if (
        boundary.is_empty
        or not boundary.is_valid
        or boundary.geom_type
        not in {
            "Polygon",
            "MultiPolygon",
        }
    ):
        raise ValueError("jurisdiction_boundary_invalid")
    return boundary


def assess_geometry(
    document: ApiGeometry | ApiFeature,
    *,
    jurisdiction_boundary: BaseGeometry,
    max_vertices: int,
) -> GeometryAssessment:
    geometry = document.geometry if isinstance(document, ApiFeature) else document
    if count_vertices(geometry.coordinates) > max_vertices:
        raise GeometryComplexityError("geometry_vertex_limit_exceeded")
    domain_geometry = GeoJSONGeometry(type=geometry.type, coordinates=geometry.coordinates)
    validated = validate_geometry(
        domain_geometry,
        "EPSG:4326",
        jurisdiction_boundary_wgs84=jurisdiction_boundary,
    )
    area = measure_area(validated)
    return GeometryAssessment(
        geometry_type=validated.analysis_geometry.geom_type,
        repair_performed=validated.repair_performed,
        area_ha=area.total_area_ha,
        warnings=validated.warnings,
    )


def canonical_feature(document: ApiGeometry | ApiFeature) -> dict[str, Any]:
    if isinstance(document, ApiFeature):
        return document.model_dump(mode="json")
    return {"type": "Feature", "properties": {}, "geometry": document.model_dump(mode="json")}


def count_vertices(coordinates: object) -> int:
    if not isinstance(coordinates, list):
        return 0
    if len(coordinates) >= 2 and all(isinstance(value, (int, float)) for value in coordinates):
        return 1
    return sum(count_vertices(item) for item in coordinates)


__all__ = [
    "GeometryComplexityError",
    "GeometryValidationError",
    "assess_geometry",
    "canonical_feature",
    "load_jurisdiction_boundary",
]
