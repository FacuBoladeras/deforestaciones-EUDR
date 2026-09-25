"""Compatibilidad para las reglas de superficie extraídas al dominio."""

from deforestation_domain.area import (
    DEFAULT_NUMERIC_TOLERANCE_HA,
    EUDR_FOREST_AREA_THRESHOLD_HA,
    AreaMeasurement,
    AreaMeasurementError,
    AreaThresholdRelation,
    classify_area_threshold,
    measure_area,
    select_projected_crs,
)

__all__ = [
    "DEFAULT_NUMERIC_TOLERANCE_HA",
    "EUDR_FOREST_AREA_THRESHOLD_HA",
    "AreaMeasurement",
    "AreaMeasurementError",
    "AreaThresholdRelation",
    "classify_area_threshold",
    "measure_area",
    "select_projected_crs",
]
