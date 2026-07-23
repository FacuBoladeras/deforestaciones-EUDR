"""Medición reproducible de superficies vectoriales preparadas para análisis."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely.geometry import MultiPolygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from deforestation_pipeline.config import AreaCrsStrategy
from deforestation_pipeline.geometry import (
    SUPPORTED_AREA_GEOMETRY_TYPES,
    ValidatedGeometry,
)

EUDR_FOREST_AREA_THRESHOLD_HA = 0.5
DEFAULT_NUMERIC_TOLERANCE_HA = 1e-9
AUTO_EQUAL_AREA_CRS = CRS.from_epsg(6933)
WGS84 = CRS.from_epsg(4326)
SQUARE_METRES_PER_HECTARE = 10_000


class AreaThresholdRelation(StrEnum):
    """Relación con el umbral, sin colapsar igualdad en ``ABOVE``."""

    BELOW = "below"
    EQUAL = "equal"
    ABOVE = "above"


class AreaMeasurementError(ValueError):
    """Error de dominio que conserva todas las violaciones detectadas."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(violations)
        if not self.violations:
            raise ValueError("AreaMeasurementError requiere al menos una violación")
        super().__init__("Superficie no medible: " + "; ".join(self.violations))


@dataclass(frozen=True, slots=True)
class AreaMeasurement:
    """Resultado inmutable con metadatos suficientes para auditar el cálculo."""

    total_area_ha: float
    component_areas_ha: tuple[float, ...]
    calculation_crs: str
    strategy: AreaCrsStrategy
    threshold_relation: AreaThresholdRelation
    threshold_ha: float
    numeric_tolerance_ha: float
    method: str
    transform_always_xy: bool
    source_geometry_field: str
    utm_zone: int | None
    utm_hemisphere: str | None


def measure_area(
    validated_geometry: ValidatedGeometry,
    strategy: AreaCrsStrategy = AreaCrsStrategy.AUTO_EQUAL_AREA,
    *,
    numeric_tolerance_ha: float = DEFAULT_NUMERIC_TOLERANCE_HA,
) -> AreaMeasurement:
    """Mide ``analysis_geometry`` en un CRS proyectado explícito.

    ``numeric_tolerance_ha`` sólo absorbe ruido de coma flotante al clasificar
    la relación con el umbral. No representa incertidumbre de la geometría ni
    de su fuente.
    """
    geometry = validated_geometry.analysis_geometry
    violations = _area_geometry_violations(geometry)
    _validate_numeric_tolerance(numeric_tolerance_ha, violations)
    if violations:
        raise AreaMeasurementError(violations)

    calculation_crs, utm_zone, utm_hemisphere = _select_calculation_crs(
        geometry,
        strategy,
    )
    try:
        transformer = Transformer.from_crs(WGS84, calculation_crs, always_xy=True)
        projected = transform(transformer.transform, geometry)
    except ProjError as error:
        raise AreaMeasurementError([f"falló la transformación de CRS: {error}"]) from error

    projected_components = (
        tuple(projected.geoms) if isinstance(projected, MultiPolygon) else (projected,)
    )
    component_areas_ha = tuple(
        component.area / SQUARE_METRES_PER_HECTARE for component in projected_components
    )
    total_area_ha = sum(component_areas_ha)
    area_violations = _measured_area_violations(total_area_ha, component_areas_ha)
    if area_violations:
        raise AreaMeasurementError(area_violations)

    return AreaMeasurement(
        total_area_ha=total_area_ha,
        component_areas_ha=component_areas_ha,
        calculation_crs=_crs_identifier(calculation_crs),
        strategy=strategy,
        threshold_relation=classify_area_threshold(
            total_area_ha,
            numeric_tolerance_ha=numeric_tolerance_ha,
        ),
        threshold_ha=EUDR_FOREST_AREA_THRESHOLD_HA,
        numeric_tolerance_ha=numeric_tolerance_ha,
        method="planar_area_after_pyproj_transform",
        transform_always_xy=True,
        source_geometry_field="analysis_geometry",
        utm_zone=utm_zone,
        utm_hemisphere=utm_hemisphere,
    )


def classify_area_threshold(
    area_ha: float,
    *,
    threshold_ha: float = EUDR_FOREST_AREA_THRESHOLD_HA,
    numeric_tolerance_ha: float = DEFAULT_NUMERIC_TOLERANCE_HA,
) -> AreaThresholdRelation:
    """Compara hectáreas con un umbral conservando la relación de igualdad."""
    violations: list[str] = []
    if not isfinite(area_ha) or area_ha < 0:
        violations.append("la superficie debe ser finita y no negativa")
    if not isfinite(threshold_ha) or threshold_ha < 0:
        violations.append("el umbral debe ser finito y no negativo")
    _validate_numeric_tolerance(numeric_tolerance_ha, violations)
    if violations:
        raise AreaMeasurementError(violations)

    difference = area_ha - threshold_ha
    if abs(difference) <= numeric_tolerance_ha:
        return AreaThresholdRelation.EQUAL
    if difference < 0:
        return AreaThresholdRelation.BELOW
    return AreaThresholdRelation.ABOVE


def _area_geometry_violations(geometry: BaseGeometry) -> list[str]:
    violations: list[str] = []
    if geometry.geom_type not in SUPPORTED_AREA_GEOMETRY_TYPES:
        violations.append("analysis_geometry debe ser Polygon o MultiPolygon")
    if geometry.is_empty:
        violations.append("analysis_geometry no puede estar vacía")
    if not geometry.is_valid:
        violations.append("analysis_geometry debe tener topología válida")
    if not geometry.is_empty and not all(isfinite(value) for value in geometry.bounds):
        violations.append("analysis_geometry debe tener coordenadas finitas")
    return violations


def _validate_numeric_tolerance(
    numeric_tolerance_ha: float,
    violations: list[str],
) -> None:
    if not isfinite(numeric_tolerance_ha) or numeric_tolerance_ha < 0:
        violations.append("la tolerancia numérica debe ser finita y no negativa")


def _select_calculation_crs(
    geometry: BaseGeometry,
    strategy: AreaCrsStrategy,
) -> tuple[CRS, int | None, str | None]:
    if strategy is AreaCrsStrategy.AUTO_EQUAL_AREA:
        return AUTO_EQUAL_AREA_CRS, None, None
    if strategy is not AreaCrsStrategy.LOCAL_UTM:
        raise AreaMeasurementError([f"estrategia de CRS no soportada: {strategy}"])

    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    if min_lat < 0 < max_lat:
        raise AreaMeasurementError(["la geometría cruza el ecuador; use AUTO_EQUAL_AREA"])
    hemisphere = "south" if max_lat <= 0 else "north"
    candidate_zones = tuple(
        zone for zone in range(1, 61) if _utm_zone_covers_longitude_bounds(zone, min_lon, max_lon)
    )
    if len(candidate_zones) != 1:
        raise AreaMeasurementError(["la geometría cruza más de una zona UTM; use AUTO_EQUAL_AREA"])

    zone = candidate_zones[0]
    epsg = (32700 if hemisphere == "south" else 32600) + zone
    return CRS.from_epsg(epsg), zone, hemisphere


def _utm_zone_covers_longitude_bounds(
    zone: int,
    min_lon: float,
    max_lon: float,
) -> bool:
    west = -180 + (zone - 1) * 6
    east = west + 6
    return min_lon >= west and max_lon <= east


def _crs_identifier(crs: CRS) -> str:
    authority = crs.to_authority()
    return ":".join(authority) if authority is not None else crs.to_string()


def _measured_area_violations(
    total_area_ha: float,
    component_areas_ha: tuple[float, ...],
) -> tuple[str, ...]:
    violations: list[str] = []
    if not component_areas_ha:
        violations.append("la medición no produjo componentes")
    if not isfinite(total_area_ha) or total_area_ha < 0:
        violations.append("la superficie total calculada debe ser finita y no negativa")
    if any(not isfinite(area) or area < 0 for area in component_areas_ha):
        violations.append("las superficies por componente deben ser finitas y no negativas")
    return tuple(violations)
