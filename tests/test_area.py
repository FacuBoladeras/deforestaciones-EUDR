"""Pruebas de medición vectorial reproducible de superficies."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from math import isfinite
from pathlib import Path

import pytest
from pyproj import Geod
from shapely.geometry import Polygon

from deforestation_pipeline.area import (
    EUDR_FOREST_AREA_THRESHOLD_HA,
    AreaMeasurementError,
    AreaThresholdRelation,
    classify_area_threshold,
    measure_area,
)
from deforestation_pipeline.config import AreaCrsStrategy, load_config
from deforestation_pipeline.geometry import ValidatedGeometry, validate_geometry
from deforestation_pipeline.schemas import GeoJSONGeometry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _validated_polygon(rings: list[list[list[float]]]) -> ValidatedGeometry:
    geometry = GeoJSONGeometry(type="Polygon", coordinates=rings)
    return validate_geometry(geometry, "EPSG:4326")


def _validated_multipolygon(
    polygons: list[list[list[list[float]]]],
) -> ValidatedGeometry:
    geometry = GeoJSONGeometry(type="MultiPolygon", coordinates=polygons)
    return validate_geometry(geometry, "EPSG:4326")


def test_auto_equal_area_matches_independent_geodesic_area() -> None:
    validated = _validated_polygon(
        [[[-63.0, -33.0], [-62.99, -33.0], [-62.99, -32.99], [-63.0, -32.99], [-63.0, -33.0]]]
    )

    result = measure_area(validated, AreaCrsStrategy.AUTO_EQUAL_AREA)
    geodesic_area_m2, _ = Geod(ellps="WGS84").geometry_area_perimeter(validated.analysis_geometry)
    expected_ha = abs(geodesic_area_m2) / 10_000

    assert result.total_area_ha == pytest.approx(expected_ha, rel=2e-6)
    assert result.calculation_crs == "EPSG:6933"
    assert result.strategy is AreaCrsStrategy.AUTO_EQUAL_AREA
    assert result.transform_always_xy is True
    assert result.source_geometry_field == "analysis_geometry"


def test_measure_area_accepts_strategy_loaded_from_pipeline_config() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    validated = _validated_polygon(
        [[[-63.0, -33.0], [-62.99, -33.0], [-62.99, -32.99], [-63.0, -32.99], [-63.0, -33.0]]]
    )

    result = measure_area(validated, config.spatial.area_crs_strategy)

    assert result.strategy is config.spatial.area_crs_strategy
    assert result.calculation_crs == "EPSG:6933"


def test_multipolygon_reports_total_and_each_component() -> None:
    validated = _validated_multipolygon(
        [
            [[[-63.0, -33.0], [-62.99, -33.0], [-62.99, -32.99], [-63.0, -32.99], [-63.0, -33.0]]],
            [[[-62.9, -33.0], [-62.88, -33.0], [-62.88, -32.99], [-62.9, -32.99], [-62.9, -33.0]]],
        ]
    )

    result = measure_area(validated)

    assert len(result.component_areas_ha) == 2
    assert result.total_area_ha == pytest.approx(sum(result.component_areas_ha), rel=1e-12)
    assert result.component_areas_ha[1] > result.component_areas_ha[0]


def test_polygon_hole_is_subtracted_from_area() -> None:
    shell = [
        [-63.0, -33.0],
        [-62.98, -33.0],
        [-62.98, -32.98],
        [-63.0, -32.98],
        [-63.0, -33.0],
    ]
    hole = [
        [-62.995, -32.995],
        [-62.985, -32.995],
        [-62.985, -32.985],
        [-62.995, -32.985],
        [-62.995, -32.995],
    ]
    with_hole = _validated_polygon([shell, hole])
    shell_only = _validated_polygon([shell])

    result_with_hole = measure_area(with_hole)
    result_shell_only = measure_area(shell_only)

    assert result_with_hole.total_area_ha < result_shell_only.total_area_ha


def test_repaired_geometry_is_measured_without_replacing_original() -> None:
    validated = _validated_polygon(
        [[[-63.0, -33.0], [-62.0, -32.0], [-63.0, -32.0], [-62.0, -33.0], [-63.0, -33.0]]]
    )
    original_wkt = validated.original_interpreted.wkt

    result = measure_area(validated)

    assert validated.repair_performed is True
    assert validated.original_interpreted.wkt == original_wkt
    assert validated.original_interpreted.is_valid is False
    assert result.total_area_ha > 0
    assert len(result.component_areas_ha) == 2


def test_point_is_rejected_as_non_areal() -> None:
    point = validate_geometry(
        GeoJSONGeometry(type="Point", coordinates=[-63.0, -33.0]),
        "EPSG:4326",
    )

    with pytest.raises(AreaMeasurementError, match="Polygon o MultiPolygon"):
        measure_area(point)


def test_cross_utm_zone_auto_works_but_local_utm_rejects() -> None:
    crossing = _validated_polygon(
        [[[-60.1, -33.0], [-59.9, -33.0], [-59.9, -32.9], [-60.1, -32.9], [-60.1, -33.0]]]
    )

    automatic = measure_area(crossing, AreaCrsStrategy.AUTO_EQUAL_AREA)

    assert automatic.total_area_ha > 0
    with pytest.raises(AreaMeasurementError, match="AUTO_EQUAL_AREA"):
        measure_area(crossing, AreaCrsStrategy.LOCAL_UTM)


def test_local_utm_selects_southern_zone_deterministically() -> None:
    validated = _validated_polygon(
        [[[-63.1, -33.1], [-63.0, -33.1], [-63.0, -33.0], [-63.1, -33.0], [-63.1, -33.1]]]
    )

    result = measure_area(validated, AreaCrsStrategy.LOCAL_UTM)

    assert result.calculation_crs == "EPSG:32720"
    assert result.utm_zone == 20
    assert result.utm_hemisphere == "south"


def test_local_utm_rejects_equator_crossing_without_hidden_fallback() -> None:
    crossing = _validated_polygon(
        [[[-63.1, -0.1], [-63.0, -0.1], [-63.0, 0.1], [-63.1, 0.1], [-63.1, -0.1]]]
    )

    with pytest.raises(AreaMeasurementError, match=r"ecuador.*AUTO_EQUAL_AREA"):
        measure_area(crossing, AreaCrsStrategy.LOCAL_UTM)


@pytest.mark.parametrize(
    ("area_ha", "expected"),
    [
        (0.49, AreaThresholdRelation.BELOW),
        (EUDR_FOREST_AREA_THRESHOLD_HA, AreaThresholdRelation.EQUAL),
        (0.51, AreaThresholdRelation.ABOVE),
    ],
)
def test_threshold_relation_preserves_strictly_greater_definition(
    area_ha: float,
    expected: AreaThresholdRelation,
) -> None:
    assert classify_area_threshold(area_ha) is expected


def test_threshold_numeric_tolerance_is_explicit_not_scientific_uncertainty() -> None:
    assert (
        classify_area_threshold(
            EUDR_FOREST_AREA_THRESHOLD_HA + 5e-10,
            numeric_tolerance_ha=1e-9,
        )
        is AreaThresholdRelation.EQUAL
    )
    assert (
        classify_area_threshold(
            EUDR_FOREST_AREA_THRESHOLD_HA + 5e-10,
            numeric_tolerance_ha=0,
        )
        is AreaThresholdRelation.ABOVE
    )


def test_results_are_finite_non_negative_and_immutable() -> None:
    validated = _validated_polygon(
        [[[-63.0, -33.0], [-62.99, -33.0], [-62.99, -32.99], [-63.0, -32.99], [-63.0, -33.0]]]
    )
    result = measure_area(validated)

    assert isfinite(result.total_area_ha)
    assert result.total_area_ha >= 0
    assert all(isfinite(area) and area >= 0 for area in result.component_areas_ha)
    with pytest.raises(FrozenInstanceError):
        result.total_area_ha = 0  # type: ignore[misc]


def test_classification_rejects_non_finite_or_negative_area() -> None:
    with pytest.raises(AreaMeasurementError, match="finita y no negativa"):
        classify_area_threshold(float("nan"))
    with pytest.raises(AreaMeasurementError, match="finita y no negativa"):
        classify_area_threshold(-0.1)


def test_area_uses_analysis_geometry_for_an_explicit_validated_result() -> None:
    validated = _validated_polygon(
        [[[-63.0, -33.0], [-62.99, -33.0], [-62.99, -32.99], [-63.0, -32.99], [-63.0, -33.0]]]
    )
    assert isinstance(validated.analysis_geometry, Polygon)

    result = measure_area(validated)

    assert result.source_geometry_field == "analysis_geometry"
