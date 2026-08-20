"""Validación GIS local separada de los contratos de transporte."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely import make_valid
from shapely.geometry import Point
from shapely.geometry import shape as shapely_shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.validation import explain_validity

from deforestation_domain.schemas import GeoJSONGeometry

__all__ = [
    "SUPPORTED_AREA_GEOMETRY_TYPES",
    "SUPPORTED_GEOMETRY_TYPES",
    "GeometryQualityCode",
    "GeometryValidationError",
    "ValidatedGeometry",
    "validate_geometry",
    "validate_point_polygon_correspondence",
]

WGS84 = CRS.from_epsg(4326)
SUPPORTED_GEOMETRY_TYPES = frozenset({"Point", "Polygon", "MultiPolygon"})
SUPPORTED_AREA_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})


class GeometryQualityCode(StrEnum):
    """Banderas auditables producidas durante la preparación espacial."""

    REPROJECTED_TO_WGS84 = "reprojected_to_wgs84"
    GEOMETRY_REPAIRED = "geometry_repaired"
    JURISDICTION_CHECKED = "jurisdiction_checked"
    JURISDICTION_NOT_EVALUATED = "jurisdiction_not_evaluated"


class GeometryValidationError(ValueError):
    """Error de dominio que conserva todas las violaciones detectadas."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(violations)
        if not self.violations:
            raise ValueError("GeometryValidationError requiere al menos una violación")
        super().__init__("Geometría inválida: " + "; ".join(self.violations))


@dataclass(frozen=True, slots=True)
class ValidatedGeometry:
    """Resultado inmutable que nunca reemplaza silenciosamente el original."""

    original_interpreted: BaseGeometry
    normalized_wgs84: BaseGeometry
    analysis_geometry: BaseGeometry
    source_crs: CRS
    warnings: tuple[str, ...]
    quality_flags: tuple[GeometryQualityCode, ...]
    repair_performed: bool


def validate_geometry(
    geometry: GeoJSONGeometry,
    source_crs: str,
    *,
    jurisdiction_boundary_wgs84: BaseGeometry | None = None,
) -> ValidatedGeometry:
    """Interpreta, normaliza y valida una geometría sin calcular superficie.

    La frontera jurisdiccional, cuando se provee, debe ser un polígono explícito
    y válido en EPSG:4326. Omitirla no produce una afirmación de pertenencia.
    """
    violations: list[str] = []
    warnings: list[str] = []
    quality_flags: list[GeometryQualityCode] = []

    if geometry.type not in SUPPORTED_GEOMETRY_TYPES:
        violations.append(f"tipo no soportado: {geometry.type}")
    if not geometry.coordinates:
        violations.append("geometría vacía")
    raw_coordinate_violations = _raw_coordinate_violations(geometry)
    violations.extend(raw_coordinate_violations)

    parsed_crs: CRS | None = None
    try:
        parsed_crs = CRS.from_user_input(source_crs)
    except CRSError:
        violations.append(f"CRS no reconocido: {source_crs}")

    original = None if raw_coordinate_violations else _interpret_geometry(geometry, violations)
    if violations or original is None or parsed_crs is None:
        raise GeometryValidationError(violations)

    normalized = _reproject_to_wgs84(original, parsed_crs)
    if parsed_crs != WGS84:
        quality_flags.append(GeometryQualityCode.REPROJECTED_TO_WGS84)

    if not _has_valid_wgs84_bounds(normalized):
        violations.append("coordenadas WGS84 fuera de rango")
        raise GeometryValidationError(violations)

    analysis = normalized
    repaired = False
    if not normalized.is_valid:
        reason = explain_validity(normalized)
        candidate = make_valid(normalized)
        if (
            candidate.is_empty
            or candidate.geom_type not in SUPPORTED_GEOMETRY_TYPES
            or not candidate.is_valid
        ):
            violations.append(
                f"reparación no utilizable ({reason}); resultado={candidate.geom_type}"
            )
            raise GeometryValidationError(violations)
        analysis = candidate
        repaired = True
        warnings.append(f"geometría reparada con make_valid: {reason}")
        quality_flags.append(GeometryQualityCode.GEOMETRY_REPAIRED)

    if jurisdiction_boundary_wgs84 is None:
        warnings.append("pertenencia jurisdiccional no evaluada: no se proporcionó frontera")
        quality_flags.append(GeometryQualityCode.JURISDICTION_NOT_EVALUATED)
    else:
        boundary_violations = _jurisdiction_boundary_violations(jurisdiction_boundary_wgs84)
        if boundary_violations:
            raise GeometryValidationError(boundary_violations)
        if not jurisdiction_boundary_wgs84.covers(analysis):
            raise GeometryValidationError(
                ["geometría fuera de la jurisdicción explícita proporcionada"]
            )
        quality_flags.append(GeometryQualityCode.JURISDICTION_CHECKED)

    return ValidatedGeometry(
        original_interpreted=original,
        normalized_wgs84=normalized,
        analysis_geometry=analysis,
        source_crs=parsed_crs,
        warnings=tuple(warnings),
        quality_flags=tuple(quality_flags),
        repair_performed=repaired,
    )


def validate_point_polygon_correspondence(
    point_wgs84: BaseGeometry,
    polygon_wgs84: BaseGeometry,
) -> None:
    """Exige que un punto esté cubierto por su polígono, incluido el borde."""
    violations: list[str] = []
    if not isinstance(point_wgs84, Point):
        violations.append("la geometría puntual debe ser Point")
    if polygon_wgs84.geom_type not in SUPPORTED_AREA_GEOMETRY_TYPES:
        violations.append("la geometría productiva debe ser Polygon o MultiPolygon")
    if point_wgs84.is_empty or polygon_wgs84.is_empty:
        violations.append("punto y polígono deben ser no vacíos")
    if not point_wgs84.is_valid or not polygon_wgs84.is_valid:
        violations.append("punto y polígono deben tener topología válida")
    if not _has_valid_wgs84_bounds(point_wgs84) or not _has_valid_wgs84_bounds(polygon_wgs84):
        violations.append("punto y polígono deben usar coordenadas WGS84 válidas")

    if violations:
        raise GeometryValidationError(violations)
    if not polygon_wgs84.covers(point_wgs84):
        raise GeometryValidationError(["punto fuera del polígono productivo"])


def _interpret_geometry(
    geometry: GeoJSONGeometry,
    violations: list[str],
) -> BaseGeometry | None:
    if violations and (geometry.type not in SUPPORTED_GEOMETRY_TYPES or not geometry.coordinates):
        return None
    payload: dict[str, Any] = {
        "type": geometry.type,
        "coordinates": geometry.coordinates,
    }
    try:
        interpreted = shapely_shape(payload)
    except (TypeError, ValueError, IndexError) as error:
        violations.append(f"geometría no interpretable: {error}")
        return None
    if interpreted.is_empty:
        violations.append("geometría vacía")
    if interpreted.geom_type not in SUPPORTED_GEOMETRY_TYPES:
        violations.append(f"tipo no soportado: {interpreted.geom_type}")
    return interpreted


def _raw_coordinate_violations(geometry: GeoJSONGeometry) -> tuple[str, ...]:
    """Detecta anillos abiertos antes de que Shapely los cierre implícitamente."""
    if geometry.type not in SUPPORTED_AREA_GEOMETRY_TYPES:
        return ()

    polygons: list[Any]
    if geometry.type == "Polygon":
        polygons = [geometry.coordinates]
    else:
        polygons = geometry.coordinates

    violations: list[str] = []
    for polygon_index, polygon in enumerate(polygons):
        if not isinstance(polygon, list):
            continue
        for ring_index, ring in enumerate(polygon):
            if isinstance(ring, list) and ring and ring[0] != ring[-1]:
                violations.append(
                    "anillo no cerrado en coordenadas GeoJSON "
                    f"(polígono {polygon_index}, anillo {ring_index})"
                )
    return tuple(violations)


def _reproject_to_wgs84(geometry: BaseGeometry, source_crs: CRS) -> BaseGeometry:
    if source_crs == WGS84:
        return geometry
    transformer = Transformer.from_crs(source_crs, WGS84, always_xy=True)
    return transform(transformer.transform, geometry)


def _has_valid_wgs84_bounds(geometry: BaseGeometry) -> bool:
    if geometry.is_empty:
        return False
    min_x, min_y, max_x, max_y = geometry.bounds
    return (
        all(isfinite(value) for value in (min_x, min_y, max_x, max_y))
        and -180 <= min_x <= 180
        and -180 <= max_x <= 180
        and -90 <= min_y <= 90
        and -90 <= max_y <= 90
    )


def _jurisdiction_boundary_violations(boundary: BaseGeometry) -> tuple[str, ...]:
    violations: list[str] = []
    if boundary.geom_type not in SUPPORTED_AREA_GEOMETRY_TYPES:
        violations.append("frontera jurisdiccional debe ser Polygon o MultiPolygon")
    if boundary.is_empty:
        violations.append("frontera jurisdiccional vacía")
    if not boundary.is_valid:
        violations.append(f"frontera jurisdiccional inválida: {explain_validity(boundary)}")
    if not _has_valid_wgs84_bounds(boundary):
        violations.append("frontera jurisdiccional fuera del rango WGS84")
    return tuple(violations)
