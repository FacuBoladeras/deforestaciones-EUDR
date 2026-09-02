"""Visualizaciones vectoriales orientadas a lectura humana."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from itertools import pairwise

from reportlab.graphics.shapes import Circle, Drawing, Line, Path, Rect, String
from reportlab.lib import colors

from deforestation_reporting.basemap import MapBounds, OSMBasemap, OSMFeature
from deforestation_reporting.models import BaselineSummary, EventSummary, GeometryRings

_BLUE = colors.HexColor("#17365D")
_GREEN = colors.HexColor("#2F6B4F")
_PALE_GREEN = colors.HexColor("#DCEBDD")
_AMBER = colors.HexColor("#D97706")
_PALE_AMBER = colors.HexColor("#FEF3C7")
_RED = colors.HexColor("#B91C1C")
_GRAY = colors.HexColor("#9CA3AF")
_LIGHT_GRAY = colors.HexColor("#F3F4F6")
_GRID = colors.HexColor("#D1D5DB")
_TEXT = colors.HexColor("#1F2937")
_MUTED = colors.HexColor("#6B7280")
_FONT = "ReportBody"
_FONT_BOLD = "ReportBody-Bold"
CoordinateTransform = Callable[[float, float], tuple[float, float]]


def baseline_area_chart(summary: BaselineSummary, *, width: float, height: float) -> Drawing:
    """Compara áreas raster sin mezclar el denominador vectorial declarado."""
    drawing = Drawing(width, height)
    drawing.add(
        String(
            0,
            height - 15,
            "Clasificación automática de la superficie raster",
            fontName=_FONT_BOLD,
            fontSize=10,
            fillColor=_BLUE,
        )
    )
    x = 0.0
    y = height - 50
    bar_width = width
    bar_height = 28
    total = summary.raster_aoi_area_ha
    categories = (
        ("Bosque automático", summary.automated_forest_area_ha, _GREEN),
        ("No bosque automático", summary.automated_nonforest_area_ha, colors.HexColor("#CBD5E1")),
        ("Revisión requerida", summary.review_required_area_ha, _AMBER),
        ("Sin datos", summary.insufficient_data_area_ha, _GRAY),
    )
    for _label, value, color in categories:
        segment = 0.0 if total == 0 else bar_width * value / total
        if segment > 0:
            drawing.add(
                Rect(
                    x,
                    y,
                    segment,
                    bar_height,
                    fillColor=color,
                    strokeColor=colors.white,
                    strokeWidth=0.5,
                )
            )
        x += segment
    drawing.add(
        Rect(0, y, bar_width, bar_height, fillColor=None, strokeColor=_BLUE, strokeWidth=0.7)
    )
    legend_y = y - 24
    for index, (label, value, color) in enumerate(categories):
        column = index % 2
        row = index // 2
        item_x = column * width / 2
        item_y = legend_y - row * 22
        drawing.add(Rect(item_x, item_y, 10, 10, fillColor=color, strokeColor=None))
        drawing.add(
            String(
                item_x + 15,
                item_y + 1,
                f"{label}: {_decimal_es(value)} ha",
                fontName=_FONT,
                fontSize=7.4,
                fillColor=_TEXT,
            )
        )
    return drawing


def events_overview_map(
    events: tuple[EventSummary, ...],
    *,
    establishment_geometry: GeometryRings = (),
    label_candidate_ids: frozenset[str] | None = None,
    width: float,
    height: float,
) -> Drawing:
    """Ubica los eventos dentro del perímetro declarado con ayudas cartográficas."""
    drawing = Drawing(width, height)
    event_rings = [ring for event in events for ring in event.event_geometry]
    rings = list(establishment_geometry) or event_rings
    if not rings:
        drawing.add(
            String(
                width / 2,
                height / 2,
                "Geometría espacial no disponible",
                textAnchor="middle",
                fontName=_FONT,
                fontSize=9,
                fillColor=_GRAY,
            )
        )
        return drawing
    map_box = (42.0, 39.0, width - 54.0, height - 50.0)
    transform = _draw_reference_map(
        drawing,
        rings,
        map_box=map_box,
        fill=colors.HexColor("#F8FAFC"),
        stroke=_BLUE,
    )
    if establishment_geometry:
        for ring in establishment_geometry:
            drawing.add(
                _ring_path(
                    ring,
                    transform,
                    fill=colors.HexColor("#F8FAFC"),
                    stroke=_BLUE,
                    stroke_width=1.1,
                )
            )
    placed_labels: list[tuple[float, float]] = []
    for event in events:
        color = _event_color(event)
        for ring in event.event_geometry:
            drawing.add(_ring_path(ring, transform, fill=color, stroke=_TEXT, stroke_width=0.7))
        centroid = _centroid(event.event_geometry)
        should_label = label_candidate_ids is None or event.candidate_id in label_candidate_ids
        if centroid is not None and should_label:
            x, y = transform(*centroid)
            label_x, label_y = _non_overlapping_label_position(
                x,
                y,
                placed_labels,
                width=width,
                height=height,
            )
            placed_labels.append((label_x, label_y))
            if (label_x, label_y) != (x, y):
                drawing.add(Line(x, y, label_x, label_y, strokeColor=_TEXT, strokeWidth=0.5))
            drawing.add(
                Circle(
                    label_x,
                    label_y,
                    8,
                    fillColor=colors.white,
                    strokeColor=_TEXT,
                    strokeWidth=0.7,
                )
            )
            drawing.add(
                String(
                    label_x,
                    label_y - 2.5,
                    ("E" if event.record_type == "conversion_likely_event" else "C")
                    + str(event.ordinal),
                    textAnchor="middle",
                    fontName=_FONT_BOLD,
                    fontSize=6.2,
                    fillColor=_TEXT,
                )
            )
    _map_legend(drawing, width, height, events)
    return drawing


def forest_trajectory_chart(event: EventSummary, *, width: float, height: float) -> Drawing:
    """Muestra la fracción anual clasificada como bosque dentro del evento."""
    drawing = Drawing(width, height)
    points = event.forest_trajectory
    if not points:
        drawing.add(
            String(
                width / 2,
                height / 2,
                "Trayectoria anual no disponible",
                textAnchor="middle",
                fontName=_FONT,
                fontSize=9,
                fillColor=_GRAY,
            )
        )
        return drawing
    left, right, bottom, top = 42.0, width - 18.0, 28.0, height - 24.0
    for percent in (0, 25, 50, 75, 100):
        y = bottom + (top - bottom) * percent / 100
        drawing.add(Line(left, y, right, y, strokeColor=_GRID, strokeWidth=0.4))
        drawing.add(
            String(
                left - 8,
                y - 2,
                f"{percent}%",
                textAnchor="end",
                fontName=_FONT,
                fontSize=6.4,
                fillColor=_TEXT,
            )
        )
    count = len(points)
    coordinates: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        x = left if count == 1 else left + (right - left) * index / (count - 1)
        y = bottom + (top - bottom) * point.forest_fraction
        coordinates.append((x, y))
        drawing.add(
            String(
                x,
                10,
                str(point.year),
                textAnchor="middle",
                fontName=_FONT,
                fontSize=6.6,
                fillColor=_TEXT,
            )
        )
    for first, second in pairwise(coordinates):
        drawing.add(Line(*first, *second, strokeColor=_GREEN, strokeWidth=2.2))
    for (x, y), point in zip(coordinates, points, strict=True):
        drawing.add(Circle(x, y, 3.2, fillColor=_GREEN, strokeColor=colors.white, strokeWidth=0.6))
        drawing.add(
            String(
                x,
                min(top + 4, y + 8),
                f"{point.forest_fraction * 100:.0f}%",
                textAnchor="middle",
                fontName=_FONT_BOLD,
                fontSize=6.4,
                fillColor=_GREEN,
            )
        )
    drawing.add(
        String(
            left,
            height - 10,
            "Fracción del evento clasificada como bosque",
            fontName=_FONT_BOLD,
            fontSize=8.5,
            fillColor=_BLUE,
        )
    )
    return drawing


def agricultural_support_map(
    event: EventSummary,
    *,
    establishment_geometry: GeometryRings = (),
    basemap: OSMBasemap | None = None,
    width: float,
    height: float,
) -> Drawing:
    """Distingue soporte agrícola y ubica el evento dentro del establecimiento."""
    drawing = Drawing(width, height)
    map_geometry = event.candidate_geometry or event.event_geometry
    if not map_geometry:
        drawing.add(
            String(
                width / 2,
                height / 2,
                "Geometría espacial no disponible",
                textAnchor="middle",
                fontName=_FONT,
                fontSize=9,
                fillColor=_GRAY,
            )
        )
        return drawing
    map_box = (42.0, 38.0, width - 54.0, height - 49.0)
    transform = _draw_reference_map(
        drawing,
        map_geometry,
        map_box=map_box,
        fill=colors.HexColor("#F8FAFC"),
        stroke=_TEXT,
        basemap=basemap,
    )
    for ring in map_geometry:
        drawing.add(_ring_path(ring, transform, fill=_LIGHT_GRAY, stroke=_TEXT, stroke_width=1.0))
    if event.record_type == "conversion_likely_event":
        for ring in event.event_geometry:
            drawing.add(_ring_path(ring, transform, fill=_RED, stroke=_TEXT, stroke_width=0.8))
    for ring in event.agricultural_geometry:
        drawing.add(_ring_path(ring, transform, fill=_AMBER, stroke=_RED, stroke_width=0.6))
    _event_locator_inset(drawing, event, establishment_geometry, width=width, height=height)
    drawing.add(Rect(10, 10, 10, 10, fillColor=_LIGHT_GRAY, strokeColor=_TEXT, strokeWidth=0.6))
    drawing.add(
        String(25, 12, "Perturbación candidata", fontName=_FONT, fontSize=7, fillColor=_TEXT)
    )
    persistence_unavailable = "agricultural_persistence_unavailable" in event.quality_flags
    occurrence_contract = event.agricultural_evidence_schema_version in {"2.0.0", "2.1.0"}
    if event.agricultural_geometry:
        drawing.add(Rect(112, 10, 10, 10, fillColor=_AMBER, strokeColor=_RED, strokeWidth=0.6))
        label = (
            "Evidencia agrícola post-evento"
            if occurrence_contract
            else "Soporte agrícola persistente (legado v1)"
        )
    elif persistence_unavailable:
        drawing.add(
            Rect(112, 10, 10, 10, fillColor=colors.white, strokeColor=_GRAY, strokeWidth=0.6)
        )
        label = "Evidencia agrícola no evaluada"
    else:
        drawing.add(
            Rect(112, 10, 10, 10, fillColor=colors.white, strokeColor=_GRAY, strokeWidth=0.6)
        )
        drawing.add(Line(112, 10, 122, 20, strokeColor=_GRAY, strokeWidth=0.7))
        drawing.add(Line(112, 20, 122, 10, strokeColor=_GRAY, strokeWidth=0.7))
        label = (
            "No se delimitó evidencia agrícola post-evento"
            if occurrence_contract
            else "No se delimitó soporte agrícola persistente (legado v1)"
        )
    drawing.add(String(127, 12, label, fontName=_FONT, fontSize=7, fillColor=_TEXT))
    return drawing


def agricultural_map_bounds(rings: GeometryRings, *, width: float, height: float) -> MapBounds:
    """Calcula la extensión exacta que comparten el contexto OSM y los vectores."""
    map_box = (42.0, 38.0, width - 54.0, height - 49.0)
    return _fit_bounds(
        _bounds(rings),
        aspect=map_box[2] / map_box[3],
        padding_fraction=0.07,
    )


def sensitivity_chart(event: EventSummary, *, width: float, height: float) -> Drawing:
    """Resume la sensibilidad como rango, sin fingir un intervalo estadístico."""
    drawing = Drawing(width, height)
    minimum = event.sensitivity_min_area_ha
    maximum = event.sensitivity_max_area_ha
    primary = event.persistent_agricultural_area_ha
    if primary is None or minimum is None or maximum is None or maximum <= 0:
        drawing.add(
            String(
                0,
                height / 2,
                "Sin rango de sensibilidad aplicable",
                fontName=_FONT,
                fontSize=8,
                fillColor=_GRAY,
            )
        )
        return drawing
    left, right, y = 30.0, width - 30.0, height / 2
    scale_max = max(maximum, primary) * 1.1

    def to_x(value: float) -> float:
        return left + (right - left) * value / scale_max

    drawing.add(Line(left, y, right, y, strokeColor=_GRID, strokeWidth=5))
    drawing.add(Line(to_x(minimum), y, to_x(maximum), y, strokeColor=_AMBER, strokeWidth=7))
    drawing.add(
        Circle(to_x(primary), y, 4, fillColor=_BLUE, strokeColor=colors.white, strokeWidth=0.7)
    )
    drawing.add(
        String(
            to_x(minimum),
            y + 10,
            f"{_decimal_es(minimum)} ha",
            textAnchor="middle",
            fontName=_FONT,
            fontSize=6.8,
            fillColor=_TEXT,
        )
    )
    drawing.add(
        String(
            to_x(maximum),
            y + 10,
            f"{_decimal_es(maximum)} ha",
            textAnchor="middle",
            fontName=_FONT,
            fontSize=6.8,
            fillColor=_TEXT,
        )
    )
    drawing.add(
        String(
            to_x(primary),
            y - 17,
            f"Valor principal: {_decimal_es(primary)} ha",
            textAnchor="middle",
            fontName=_FONT_BOLD,
            fontSize=7,
            fillColor=_BLUE,
        )
    )
    return drawing


def _draw_reference_map(
    drawing: Drawing,
    rings: Iterable[tuple[tuple[float, float], ...]],
    *,
    map_box: tuple[float, float, float, float],
    fill: colors.Color,
    stroke: colors.Color,
    basemap: OSMBasemap | None = None,
) -> CoordinateTransform:
    x, y, box_width, box_height = map_box
    bounds = (
        basemap.bounds
        if basemap is not None
        else _fit_bounds(_bounds(rings), aspect=box_width / box_height, padding_fraction=0.07)
    )
    minimum_x, minimum_y, maximum_x, maximum_y = bounds
    drawing.add(
        Rect(
            x,
            y,
            box_width,
            box_height,
            fillColor=fill,
            strokeColor=stroke,
            strokeWidth=0.7,
        )
    )
    transform = _transform_bounds(bounds, map_box=map_box)
    if basemap is not None:
        _draw_osm_basemap(drawing, basemap, transform=transform, map_box=map_box)
    for fraction in (0.0, 0.5, 1.0):
        grid_x = x + box_width * fraction
        grid_y = y + box_height * fraction
        drawing.add(Line(grid_x, y, grid_x, y + box_height, strokeColor=_GRID, strokeWidth=0.35))
        drawing.add(Line(x, grid_y, x + box_width, grid_y, strokeColor=_GRID, strokeWidth=0.35))
        longitude = minimum_x + (maximum_x - minimum_x) * fraction
        latitude = minimum_y + (maximum_y - minimum_y) * fraction
        drawing.add(
            String(
                grid_x,
                y - 11,
                _format_longitude(longitude),
                textAnchor="middle",
                fontName=_FONT,
                fontSize=5.8,
                fillColor=_MUTED,
            )
        )
        drawing.add(
            String(
                x - 5,
                grid_y - 2,
                _format_latitude(latitude),
                textAnchor="end",
                fontName=_FONT,
                fontSize=5.8,
                fillColor=_MUTED,
            )
        )
    _north_arrow(drawing, x=x + box_width - 15, y=y + box_height - 15)
    _scale_bar(drawing, bounds=bounds, map_box=map_box)
    return transform


def _draw_osm_basemap(
    drawing: Drawing,
    basemap: OSMBasemap,
    *,
    transform: CoordinateTransform,
    map_box: tuple[float, float, float, float],
) -> None:
    x, y, box_width, box_height = map_box
    if not basemap.available:
        drawing.add(
            String(
                x + box_width / 2,
                y + box_height / 2,
                "Contexto OSM no disponible",
                textAnchor="middle",
                fontName=_FONT,
                fontSize=6.5,
                fillColor=_MUTED,
            )
        )
        return
    minimum_x, minimum_y, maximum_x, maximum_y = basemap.bounds

    def bounded_transform(longitude: float, latitude: float) -> tuple[float, float]:
        return transform(
            min(max(longitude, minimum_x), maximum_x),
            min(max(latitude, minimum_y), maximum_y),
        )

    for feature in basemap.features:
        drawing.add(_osm_feature_path(feature, bounded_transform, opacity=basemap.opacity))
    drawing.add(
        String(
            x + box_width - 4,
            y + 4,
            basemap.attribution,
            textAnchor="end",
            fontName=_FONT,
            fontSize=4.6,
            fillColor=_MUTED,
        )
    )


def _osm_feature_path(
    feature: OSMFeature,
    transform: CoordinateTransform,
    *,
    opacity: float,
) -> Path:
    path = Path()
    first_x, first_y = transform(*feature.geometry[0])
    path.moveTo(first_x, first_y)
    for longitude, latitude in feature.geometry[1:]:
        point_x, point_y = transform(longitude, latitude)
        path.lineTo(point_x, point_y)

    if feature.kind in {"building", "landuse", "water"}:
        path.closePath()
    stroke, fill, stroke_width = _osm_style(feature.kind, opacity=opacity)
    path.strokeColor = stroke
    path.strokeWidth = stroke_width
    path.fillColor = fill
    return path


def _osm_style(kind: str, *, opacity: float) -> tuple[colors.Color, colors.Color | None, float]:
    if kind == "road":
        return _faded_color("#64748B", opacity), None, 1.1
    if kind == "waterway":
        return _faded_color("#3B82F6", opacity), None, 0.9
    if kind == "water":
        return _faded_color("#60A5FA", opacity), _faded_color("#BFDBFE", opacity), 0.5
    if kind == "building":
        return _faded_color("#94A3B8", opacity), _faded_color("#CBD5E1", opacity), 0.45
    return _faded_color("#86A789", opacity), _faded_color("#DCEBDD", opacity), 0.35


def _faded_color(hex_color: str, opacity: float) -> colors.Color:
    color = colors.HexColor(hex_color)
    return colors.Color(
        1 - (1 - color.red) * opacity,
        1 - (1 - color.green) * opacity,
        1 - (1 - color.blue) * opacity,
    )


def _event_locator_inset(
    drawing: Drawing,
    event: EventSummary,
    establishment_geometry: GeometryRings,
    *,
    width: float,
    height: float,
) -> None:
    if not establishment_geometry:
        return
    inset_width, inset_height = 118.0, 88.0
    x, y = 55.0, height - inset_height - 12.0
    drawing.add(
        Rect(
            x,
            y,
            inset_width,
            inset_height,
            fillColor=colors.white,
            strokeColor=_BLUE,
            strokeWidth=0.7,
        )
    )
    drawing.add(
        String(
            x + 6,
            y + inset_height - 11,
            "Ubicación en el establecimiento",
            fontName=_FONT_BOLD,
            fontSize=5.8,
            fillColor=_BLUE,
        )
    )
    inset_box = (x + 8, y + 7, inset_width - 16, inset_height - 23)
    bounds = _fit_bounds(
        _bounds(establishment_geometry),
        aspect=inset_box[2] / inset_box[3],
        padding_fraction=0.08,
    )
    transform = _transform_bounds(bounds, map_box=inset_box)
    for ring in establishment_geometry:
        drawing.add(
            _ring_path(
                ring,
                transform,
                fill=colors.HexColor("#F8FAFC"),
                stroke=_BLUE,
                stroke_width=0.7,
            )
        )
    for ring in event.event_geometry:
        drawing.add(
            _ring_path(
                ring,
                transform,
                fill=_PALE_AMBER,
                stroke=_RED,
                stroke_width=0.8,
            )
        )


def _north_arrow(drawing: Drawing, *, x: float, y: float) -> None:
    drawing.add(
        String(
            x,
            y + 3,
            "N",
            textAnchor="middle",
            fontName=_FONT_BOLD,
            fontSize=7,
            fillColor=_TEXT,
        )
    )
    drawing.add(Line(x, y - 19, x, y - 2, strokeColor=_TEXT, strokeWidth=1.1))
    drawing.add(Line(x, y - 2, x - 4, y - 8, strokeColor=_TEXT, strokeWidth=1.1))
    drawing.add(Line(x, y - 2, x + 4, y - 8, strokeColor=_TEXT, strokeWidth=1.1))


def _scale_bar(
    drawing: Drawing,
    *,
    bounds: tuple[float, float, float, float],
    map_box: tuple[float, float, float, float],
) -> None:
    minimum_x, minimum_y, maximum_x, maximum_y = bounds
    x, y, box_width, _box_height = map_box
    midpoint_latitude = (minimum_y + maximum_y) / 2
    meters_per_longitude_degree = max(1.0, 111_320.0 * math.cos(math.radians(midpoint_latitude)))
    horizontal_metres = (maximum_x - minimum_x) * meters_per_longitude_degree
    target = horizontal_metres * 0.22
    scale_metres = _nice_distance(target)
    if scale_metres <= 0 or horizontal_metres <= 0:
        return
    bar_width = box_width * scale_metres / horizontal_metres
    bar_x, bar_y = x + 10, y + 11
    drawing.add(Line(bar_x, bar_y, bar_x + bar_width, bar_y, strokeColor=_TEXT, strokeWidth=2.1))
    drawing.add(Line(bar_x, bar_y - 2, bar_x, bar_y + 2, strokeColor=_TEXT, strokeWidth=0.8))
    drawing.add(
        Line(
            bar_x + bar_width,
            bar_y - 2,
            bar_x + bar_width,
            bar_y + 2,
            strokeColor=_TEXT,
            strokeWidth=0.8,
        )
    )
    label = f"{scale_metres / 1000:g} km" if scale_metres >= 1000 else f"{scale_metres:g} m"
    drawing.add(
        String(
            bar_x + bar_width / 2,
            bar_y + 5,
            label,
            textAnchor="middle",
            fontName=_FONT,
            fontSize=5.8,
            fillColor=_TEXT,
        )
    )


def _nice_distance(value: float) -> float:
    if value <= 0:
        return 0.0
    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude
    factor = 1 if normalized < 2 else 2 if normalized < 5 else 5
    return float(factor * magnitude)


def _map_legend(
    drawing: Drawing,
    width: float,
    height: float,
    events: tuple[EventSummary, ...],
) -> None:
    items: list[tuple[str, colors.Color]] = []
    if any(event.record_type == "conversion_likely_event" for event in events):
        items.append(("Evidencia compatible con conversión", _RED))
    if any(
        event.record_type == "disturbance_candidate" and event.human_review_required
        for event in events
    ):
        items.append(("Candidato que requiere revisión", _AMBER))
    if any(
        event.record_type == "disturbance_candidate" and not event.human_review_required
        for event in events
    ):
        items.append(("Candidato observado", _GRAY))
    if not items:
        return
    drawing.add(
        Rect(
            1,
            1,
            width - 2,
            25,
            fillColor=colors.white,
            strokeColor=None,
        )
    )
    y = 11.0
    for index, (label, color) in enumerate(items):
        x = 10.0 + index * width / len(items)
        drawing.add(Rect(x, y - 2, 9, 9, fillColor=color, strokeColor=None))
        drawing.add(String(x + 13, y, label, fontName=_FONT, fontSize=6.5, fillColor=_TEXT))


def _non_overlapping_label_position(
    x: float,
    y: float,
    placed: list[tuple[float, float]],
    *,
    width: float,
    height: float,
) -> tuple[float, float]:
    offsets = (
        (0.0, 0.0),
        (0.0, 26.0),
        (26.0, 0.0),
        (-26.0, 0.0),
        (0.0, -26.0),
        (26.0, 26.0),
        (-26.0, 26.0),
        (26.0, -26.0),
        (-26.0, -26.0),
        (0.0, 52.0),
    )
    for offset_x, offset_y in offsets:
        candidate = (
            min(max(x + offset_x, 10.0), width - 10.0),
            min(max(y + offset_y, 38.0), height - 10.0),
        )
        if all(
            (candidate[0] - other_x) ** 2 + (candidate[1] - other_y) ** 2 >= 24**2
            for other_x, other_y in placed
        ):
            return candidate
    return min(max(x, 10.0), width - 10.0), min(max(y, 38.0), height - 10.0)


def _event_color(event: EventSummary) -> colors.Color:
    if event.record_type == "conversion_likely_event":
        return _RED
    if event.human_review_required:
        return _AMBER
    return _GRAY


def _transform(
    rings: Iterable[tuple[tuple[float, float], ...]], *, width: float, height: float, padding: float
) -> CoordinateTransform:
    map_box = (padding, padding, width - 2 * padding, height - 2 * padding)
    bounds = _fit_bounds(_bounds(rings), aspect=map_box[2] / map_box[3], padding_fraction=0)
    return _transform_bounds(bounds, map_box=map_box)


def _bounds(
    rings: Iterable[tuple[tuple[float, float], ...]],
) -> tuple[float, float, float, float]:
    points = [point for ring in rings for point in ring]
    if not points:
        raise ValueError("geometry_empty")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _fit_bounds(
    bounds: tuple[float, float, float, float],
    *,
    aspect: float,
    padding_fraction: float,
) -> tuple[float, float, float, float]:
    minimum_x, minimum_y, maximum_x, maximum_y = bounds
    span_x = max(maximum_x - minimum_x, 1e-9)
    span_y = max(maximum_y - minimum_y, 1e-9)
    minimum_x -= span_x * padding_fraction
    maximum_x += span_x * padding_fraction
    minimum_y -= span_y * padding_fraction
    maximum_y += span_y * padding_fraction
    span_x = maximum_x - minimum_x
    span_y = maximum_y - minimum_y
    current_aspect = span_x / span_y
    if current_aspect < aspect:
        extra = (span_y * aspect - span_x) / 2
        minimum_x -= extra
        maximum_x += extra
    elif current_aspect > aspect:
        extra = (span_x / aspect - span_y) / 2
        minimum_y -= extra
        maximum_y += extra
    return minimum_x, minimum_y, maximum_x, maximum_y


def _transform_bounds(
    bounds: tuple[float, float, float, float],
    *,
    map_box: tuple[float, float, float, float],
) -> CoordinateTransform:
    minimum_x, minimum_y, maximum_x, maximum_y = bounds
    x, y, width, height = map_box
    span_x = max(maximum_x - minimum_x, 1e-12)
    span_y = max(maximum_y - minimum_y, 1e-12)

    def project(longitude: float, latitude: float) -> tuple[float, float]:
        return (
            x + (longitude - minimum_x) * width / span_x,
            y + (latitude - minimum_y) * height / span_y,
        )

    return project


def _format_longitude(value: float) -> str:
    hemisphere = "O" if value < 0 else "E"
    return f"{abs(value):.4f}° {hemisphere}"


def _format_latitude(value: float) -> str:
    hemisphere = "S" if value < 0 else "N"
    return f"{abs(value):.4f}° {hemisphere}"


def _ring_path(
    ring: tuple[tuple[float, float], ...],
    transform: CoordinateTransform,
    *,
    fill: colors.Color,
    stroke: colors.Color,
    stroke_width: float,
) -> Path:
    path = Path()
    for index, point in enumerate(ring):
        x, y = transform(*point)
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closePath()
    path.fillColor = fill
    path.strokeColor = stroke
    path.strokeWidth = stroke_width
    return path


def _centroid(rings: GeometryRings) -> tuple[float, float] | None:
    points = [point for ring in rings for point in ring]
    if not points:
        return None
    return sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(
        points
    )


def _decimal_es(value: float) -> str:
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
