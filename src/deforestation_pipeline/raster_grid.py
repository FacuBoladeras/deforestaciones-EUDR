"""Derivación local y determinística de la grilla raster temporal."""

from __future__ import annotations

from collections.abc import Iterable
from math import ceil, floor, isfinite

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from deforestation_pipeline.schemas import (
    AffineTransform,
    RasterBounds,
    RasterGridAlignment,
    RasterGridSpec,
    RasterPixelOrientation,
    raster_grid_sha256,
)

SUPPORTED_GRID_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})
GRID_ALIGNMENT_STRATEGY: RasterGridAlignment = "projected_crs_origin_outward_snap"
GRID_PIXEL_ORIENTATION: RasterPixelOrientation = "north_up"


class RasterGridError(ValueError):
    """Error de dominio que conserva todas las violaciones de la grilla."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(violations)
        if not self.violations:
            raise ValueError("RasterGridError requiere al menos una violación")
        super().__init__("Grilla raster inválida: " + "; ".join(self.violations))


def derive_raster_grid_spec(
    *,
    aoi_wgs84: BaseGeometry,
    target_crs: str,
    resolution_m: float,
    nodata: float,
) -> RasterGridSpec:
    """Ajusta la envolvente del AOI hacia afuera sobre el origen del CRS."""
    violations = _input_violations(
        aoi_wgs84=aoi_wgs84,
        target_crs=target_crs,
        resolution_m=resolution_m,
        nodata=nodata,
    )
    if violations:
        raise RasterGridError(violations)

    destination_crs = CRS.from_user_input(target_crs)
    try:
        transformer = Transformer.from_crs(
            "EPSG:4326",
            destination_crs,
            always_xy=True,
        )
        projected = transform(transformer.transform, aoi_wgs84)
    except (CRSError, ProjError, ValueError) as error:
        raise RasterGridError([f"falló la transformación al target_crs: {error}"]) from error

    if projected.is_empty or not all(isfinite(value) for value in projected.bounds):
        raise RasterGridError(["la proyección del AOI no produjo bounds finitos"])

    min_x, min_y, max_x, max_y = projected.bounds
    left = floor(min_x / resolution_m) * resolution_m
    bottom = floor(min_y / resolution_m) * resolution_m
    right = ceil(max_x / resolution_m) * resolution_m
    top = ceil(max_y / resolution_m) * resolution_m
    width = round((right - left) / resolution_m)
    height = round((top - bottom) / resolution_m)
    if width <= 0 or height <= 0:
        raise RasterGridError(["el AOI no produjo dimensiones raster positivas"])

    normalized_crs = _crs_identifier(destination_crs)
    affine: AffineTransform = (
        float(resolution_m),
        0.0,
        float(left),
        0.0,
        float(-resolution_m),
        float(top),
    )
    bounds: RasterBounds = (
        float(left),
        float(bottom),
        float(right),
        float(top),
    )
    grid_hash = raster_grid_sha256(
        target_crs=normalized_crs,
        resolution_m=float(resolution_m),
        width=width,
        height=height,
        transform=affine,
        bounds=bounds,
        alignment_strategy=GRID_ALIGNMENT_STRATEGY,
        pixel_orientation=GRID_PIXEL_ORIENTATION,
    )
    return RasterGridSpec(
        target_crs=normalized_crs,
        resolution_m=float(resolution_m),
        width=width,
        height=height,
        transform=affine,
        bounds=bounds,
        alignment_strategy=GRID_ALIGNMENT_STRATEGY,
        pixel_orientation=GRID_PIXEL_ORIENTATION,
        nodata=float(nodata),
        aoi_mask_required=True,
        grid_sha256=grid_hash,
    )


def _input_violations(
    *,
    aoi_wgs84: BaseGeometry,
    target_crs: str,
    resolution_m: float,
    nodata: float,
) -> tuple[str, ...]:
    violations: list[str] = []
    if aoi_wgs84.geom_type not in SUPPORTED_GRID_GEOMETRY_TYPES:
        violations.append("aoi_wgs84 debe ser Polygon o MultiPolygon")
    if aoi_wgs84.is_empty:
        violations.append("aoi_wgs84 no puede estar vacío")
    if not aoi_wgs84.is_valid:
        violations.append("aoi_wgs84 debe tener topología válida")
    if not aoi_wgs84.is_empty:
        bounds = aoi_wgs84.bounds
        if not all(isfinite(value) for value in bounds):
            violations.append("aoi_wgs84 debe tener coordenadas finitas")
        elif not (
            -180 <= bounds[0] <= 180
            and -90 <= bounds[1] <= 90
            and -180 <= bounds[2] <= 180
            and -90 <= bounds[3] <= 90
        ):
            violations.append("aoi_wgs84 debe estar dentro del rango EPSG:4326")

    try:
        parsed_crs = CRS.from_user_input(target_crs)
    except CRSError:
        parsed_crs = None
        violations.append("target_crs debe ser un CRS reconocible")
    if parsed_crs is not None:
        if not parsed_crs.is_projected:
            violations.append("target_crs debe ser proyectado")
        axis_units = {axis.unit_name.lower() for axis in parsed_crs.axis_info if axis.unit_name}
        if not axis_units or not axis_units <= {"metre", "meter"}:
            violations.append("target_crs debe usar metros")

    if not isfinite(resolution_m) or resolution_m <= 0:
        violations.append("resolution_m debe ser finita y positiva")
    if not isfinite(nodata):
        violations.append("nodata debe ser finito")
    return tuple(violations)


def _crs_identifier(crs: CRS) -> str:
    authority = crs.to_authority()
    return ":".join(authority) if authority is not None else crs.to_string()
