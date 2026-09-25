"""Compatibilidad para el dominio geoespacial extraído."""

from deforestation_domain.geometry import (
    SUPPORTED_AREA_GEOMETRY_TYPES,
    SUPPORTED_GEOMETRY_TYPES,
    GeometryQualityCode,
    GeometryValidationError,
    ValidatedGeometry,
    validate_geometry,
    validate_point_polygon_correspondence,
)

__all__ = [
    "SUPPORTED_AREA_GEOMETRY_TYPES",
    "SUPPORTED_GEOMETRY_TYPES",
    "GeometryQualityCode",
    "GeometryValidationError",
    "ValidatedGeometry",
    "validate_geometry",
    "validate_point_polygon_correspondence",
]
