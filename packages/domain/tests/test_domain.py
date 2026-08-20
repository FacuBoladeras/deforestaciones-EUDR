"""Contrato liviano compartido por API y pipeline científico."""

from __future__ import annotations

import pytest

from deforestation_domain.area import (
    AreaCrsStrategy,
    AreaMeasurementError,
    measure_area,
    select_projected_crs,
)
from deforestation_domain.geometry import GeometryValidationError, validate_geometry
from deforestation_domain.schemas import GeoJSONGeometry


def test_validates_and_measures_polygon_without_scientific_pipeline() -> None:
    geometry = GeoJSONGeometry(
        type="Polygon",
        coordinates=[
            [
                [-60.3, -32.1],
                [-60.2, -32.1],
                [-60.2, -32.0],
                [-60.3, -32.0],
                [-60.3, -32.1],
            ]
        ],
    )

    validated = validate_geometry(geometry, "EPSG:4326")
    measured = measure_area(validated)

    assert measured.total_area_ha > 0
    assert measured.strategy is AreaCrsStrategy.AUTO_EQUAL_AREA
    assert validated.analysis_geometry.geom_type == "Polygon"
    assert select_projected_crs(validated, AreaCrsStrategy.AUTO_EQUAL_AREA) == "EPSG:6933"


def test_domain_errors_require_at_least_one_violation() -> None:
    with pytest.raises(ValueError, match="al menos una violación"):
        AreaMeasurementError([])
    with pytest.raises(ValueError, match="al menos una violación"):
        GeometryValidationError([])
