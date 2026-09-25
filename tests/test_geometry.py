"""Pruebas de la frontera GIS local para geometrías sintéticas."""

from __future__ import annotations

from typing import Any

import pytest
from pyproj import Transformer
from shapely.geometry import Point, Polygon, mapping

from deforestation_pipeline.geometry import (
    GeometryQualityCode,
    GeometryValidationError,
    validate_geometry,
    validate_point_polygon_correspondence,
)
from deforestation_pipeline.schemas import GeoJSONGeometry


def _geometry(geometry_type: str, coordinates: list[Any]) -> GeoJSONGeometry:
    """Permite probar defensas del dominio aun si se omitió Pydantic."""
    return GeoJSONGeometry.model_construct(type=geometry_type, coordinates=coordinates)


def _polygon_geometry(coordinates: list[list[list[float]]]) -> GeoJSONGeometry:
    return GeoJSONGeometry(type="Polygon", coordinates=coordinates)


def _synthetic_boundary() -> Polygon:
    return Polygon([(-65.0, -35.0), (-60.0, -35.0), (-60.0, -30.0), (-65.0, -30.0)])


def test_rejects_empty_geometry() -> None:
    geometry = _geometry("Polygon", [])

    with pytest.raises(GeometryValidationError, match="geometría vacía"):
        validate_geometry(geometry, "EPSG:4326")


@pytest.mark.parametrize(
    "geometry",
    [
        _geometry(
            "Polygon",
            [[[-63.0, -33.0], [-62.0, -33.0], [-62.0, -32.0]]],
        ),
        _geometry(
            "MultiPolygon",
            [[[[-63.0, -33.0], [-62.0, -33.0], [-62.0, -32.0]]]],
        ),
    ],
)
def test_rejects_unclosed_raw_geojson_rings(geometry: GeoJSONGeometry) -> None:
    """Shapely no debe cerrar silenciosamente un anillo declarado abierto."""
    with pytest.raises(GeometryValidationError, match="anillo no cerrado"):
        validate_geometry(geometry, "EPSG:4326")


def test_rejects_unsupported_geometry_type() -> None:
    geometry = _geometry("LineString", [[-63.0, -33.0], [-62.0, -32.0]])

    with pytest.raises(GeometryValidationError, match="tipo no soportado"):
        validate_geometry(geometry, "EPSG:4326")


def test_rejects_unknown_crs() -> None:
    geometry = GeoJSONGeometry(type="Point", coordinates=[-63.0, -33.0])

    with pytest.raises(GeometryValidationError, match="CRS no reconocido"):
        validate_geometry(geometry, "EPSG:999999")


def test_reprojects_deterministically_to_wgs84_with_xy_axis_order() -> None:
    forward = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x, y = forward.transform(-63.0, -33.0)
    geometry = GeoJSONGeometry(type="Point", coordinates=[x, y])

    first = validate_geometry(geometry, "EPSG:3857")
    second = validate_geometry(geometry, "EPSG:3857")

    assert first.normalized_wgs84.equals_exact(Point(-63.0, -33.0), tolerance=1e-9)
    assert first.normalized_wgs84.equals_exact(second.normalized_wgs84, tolerance=0)
    assert first.source_crs.to_epsg() == 3857
    assert GeometryQualityCode.REPROJECTED_TO_WGS84 in first.quality_flags


@pytest.mark.parametrize(
    "coordinates",
    [
        [181.0, -33.0],
        [-63.0, 91.0],
        [float("inf"), -33.0],
    ],
)
def test_rejects_wgs84_coordinates_outside_valid_range(coordinates: list[float]) -> None:
    geometry = GeoJSONGeometry(type="Point", coordinates=coordinates)

    with pytest.raises(GeometryValidationError, match="coordenadas WGS84 fuera de rango"):
        validate_geometry(geometry, "EPSG:4326")


def test_accepts_valid_polygon_without_repair() -> None:
    geometry = _polygon_geometry([[[-63.0, -33.0], [-62.0, -33.0], [-62.0, -32.0], [-63.0, -33.0]]])

    result = validate_geometry(
        geometry,
        "EPSG:4326",
        jurisdiction_boundary_wgs84=_synthetic_boundary(),
    )

    assert result.original_interpreted.is_valid
    assert result.normalized_wgs84.is_valid
    assert result.analysis_geometry.is_valid
    assert result.repair_performed is False
    assert result.warnings == ()


def test_repairs_bow_tie_without_discarding_original() -> None:
    geometry = _polygon_geometry(
        [[[-63.0, -33.0], [-62.0, -32.0], [-63.0, -32.0], [-62.0, -33.0], [-63.0, -33.0]]]
    )

    result = validate_geometry(geometry, "EPSG:4326")

    assert result.original_interpreted.is_valid is False
    assert result.normalized_wgs84.is_valid is False
    assert result.analysis_geometry.is_valid
    assert result.analysis_geometry.geom_type == "MultiPolygon"
    assert result.repair_performed is True
    assert GeometryQualityCode.GEOMETRY_REPAIRED in result.quality_flags
    assert result.warnings


def test_rejects_repair_result_that_is_not_usable() -> None:
    geometry = _polygon_geometry([[[-63.0, -33.0], [-62.0, -33.0], [-61.0, -33.0], [-63.0, -33.0]]])

    with pytest.raises(GeometryValidationError, match="reparación no utilizable"):
        validate_geometry(geometry, "EPSG:4326")


def test_jurisdiction_boundary_accepts_covered_geometry_and_rejects_external() -> None:
    boundary = _synthetic_boundary()
    inside = GeoJSONGeometry(type="Point", coordinates=[-63.0, -33.0])
    outside = GeoJSONGeometry(type="Point", coordinates=[-58.0, -33.0])

    accepted = validate_geometry(
        inside,
        "EPSG:4326",
        jurisdiction_boundary_wgs84=boundary,
    )

    assert GeometryQualityCode.JURISDICTION_CHECKED in accepted.quality_flags
    with pytest.raises(GeometryValidationError, match="fuera de la jurisdicción"):
        validate_geometry(
            outside,
            "EPSG:4326",
            jurisdiction_boundary_wgs84=boundary,
        )


def test_without_boundary_marks_jurisdiction_as_not_evaluated() -> None:
    geometry = GeoJSONGeometry(type="Point", coordinates=[-63.0, -33.0])

    result = validate_geometry(geometry, "EPSG:4326")

    assert GeometryQualityCode.JURISDICTION_NOT_EVALUATED in result.quality_flags
    assert any("no evaluada" in warning for warning in result.warnings)


def test_rejects_invalid_jurisdiction_boundary() -> None:
    invalid_boundary = Polygon([(-65.0, -35.0), (-60.0, -30.0), (-65.0, -30.0), (-60.0, -35.0)])
    geometry = GeoJSONGeometry(type="Point", coordinates=[-63.0, -33.0])

    with pytest.raises(GeometryValidationError, match="frontera jurisdiccional inválida"):
        validate_geometry(
            geometry,
            "EPSG:4326",
            jurisdiction_boundary_wgs84=invalid_boundary,
        )


def test_point_polygon_correspondence_uses_covers_for_boundary_points() -> None:
    polygon = _synthetic_boundary()

    validate_point_polygon_correspondence(Point(-65.0, -33.0), polygon)

    with pytest.raises(GeometryValidationError, match="punto fuera del polígono"):
        validate_point_polygon_correspondence(Point(-58.0, -33.0), polygon)


def test_result_is_immutable_and_preserves_original_coordinates() -> None:
    geometry = GeoJSONGeometry(type="Point", coordinates=[-63.0, -33.0])
    result = validate_geometry(geometry, "EPSG:4326")

    assert mapping(result.original_interpreted)["coordinates"] == (-63.0, -33.0)
    with pytest.raises((AttributeError, TypeError)):
        result.repair_performed = True  # type: ignore[misc]
