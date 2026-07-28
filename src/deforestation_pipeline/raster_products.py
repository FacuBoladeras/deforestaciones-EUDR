"""Descarga raster acotada, validación GeoTIFF y render PNG local."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import matplotlib
import numpy as np
import rasterio
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field
from pyproj import CRS, Transformer
from rasterio.io import MemoryFile
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from deforestation_pipeline.config import OutputConfig, SpectralIndex
from deforestation_pipeline.hls_composite import (
    REFLECTANCE_BAND_NAMES,
    HlsCompositeImages,
)
from deforestation_pipeline.schemas import RasterGridSpec

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

FetchBytes = Callable[[str, int], bytes]
INDEX_COLORMAPS: dict[SpectralIndex, str] = {
    SpectralIndex.NDVI: "RdYlGn",
    SpectralIndex.EVI2: "RdYlGn",
    SpectralIndex.NBR: "BrBG",
    SpectralIndex.NDMI: "BrBG",
    SpectralIndex.NMDI: "BrBG",
    SpectralIndex.LSWI: "BrBG",
    SpectralIndex.NIRV: "YlGn",
    SpectralIndex.KNDVI: "YlGn",
}


class RasterDownloadError(RuntimeError):
    """Fallo legible que nunca persiste una URL firmada."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Descarga raster fallida: {code}")


class DirectDownloadEstimate(BaseModel):
    """Estimación conservadora sobre la grilla pedida, antes de llamar a GEE."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_crs: str
    scale_m: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    band_count: int = Field(gt=0)
    bytes_per_sample: int = Field(gt=0)
    estimated_uncompressed_bytes: int = Field(gt=0)
    grid_sha256: str | None = None


class RasterValidation(BaseModel):
    """Propiedades leídas nuevamente desde el GeoTIFF normalizado."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_path: str
    crs: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    band_count: int = Field(gt=0)
    band_names: tuple[str, ...]
    data_types: tuple[str, ...]
    nodata: float
    resolution_m: tuple[float, float]
    transform: tuple[float, float, float, float, float, float]
    bounds: tuple[float, float, float, float]
    grid_sha256: str
    minimum_by_band: tuple[float, ...]
    maximum_by_band: tuple[float, ...]


class VisualizationRecord(BaseModel):
    """Parámetros efectivos de una figura derivada; nunca altera los datos."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    source_raster: str
    band_name: str
    display_min: float | None
    display_max: float | None
    colormap: str
    nodata_transparent: bool
    roi_overlay: bool


@dataclass(frozen=True, slots=True)
class HlsRasterMaterialization:
    """Archivos binarios y su evidencia estructural lista para el bundle."""

    files: Mapping[str, bytes]
    estimates: tuple[DirectDownloadEstimate, ...]
    validations: tuple[RasterValidation, ...]
    visualizations: tuple[VisualizationRecord, ...]
    grid_spec: RasterGridSpec

    def metadata_payload(self) -> dict[str, object]:
        return {
            "download_method": "ee.Image.getDownloadURL",
            "signed_urls_persisted": False,
            "resolution_degraded": False,
            "grid": self.grid_spec.model_dump(mode="json"),
            "estimates": [estimate.model_dump(mode="json") for estimate in self.estimates],
            "raster_validations": [
                validation.model_dump(mode="json") for validation in self.validations
            ],
            "visualizations": [
                visualization.model_dump(mode="json") for visualization in self.visualizations
            ],
        }


@dataclass(frozen=True, slots=True)
class _RasterData:
    values: NDArray[np.float64]
    mask: NDArray[np.bool_]
    band_names: tuple[str, ...]
    crs: str
    transform: Any
    bounds: tuple[float, float, float, float]
    nodata: float


def estimate_direct_download(
    *,
    aoi_wgs84: BaseGeometry,
    target_crs: str,
    scale_m: float,
    band_count: int,
    bytes_per_sample: int,
) -> DirectDownloadEstimate:
    """Estima dimensiones sin modificar AOI, resolución ni cantidad de bandas."""
    if aoi_wgs84.is_empty or not aoi_wgs84.is_valid:
        raise RasterDownloadError("aoi_invalid")
    if aoi_wgs84.geom_type not in {"Polygon", "MultiPolygon"}:
        raise RasterDownloadError("aoi_must_be_polygonal")
    try:
        destination_crs = CRS.from_user_input(target_crs)
        transformer = Transformer.from_crs("EPSG:4326", destination_crs, always_xy=True)
        projected = transform(transformer.transform, aoi_wgs84)
    except Exception:
        raise RasterDownloadError("target_crs_invalid") from None
    min_x, min_y, max_x, max_y = projected.bounds
    width = max(1, math.ceil((max_x - min_x) / scale_m))
    height = max(1, math.ceil((max_y - min_y) / scale_m))
    estimated_bytes = width * height * band_count * bytes_per_sample
    return DirectDownloadEstimate(
        target_crs=destination_crs.to_string(),
        scale_m=scale_m,
        width=width,
        height=height,
        band_count=band_count,
        bytes_per_sample=bytes_per_sample,
        estimated_uncompressed_bytes=estimated_bytes,
    )


def estimate_fixed_grid_download(
    *,
    grid_spec: RasterGridSpec,
    band_count: int,
    bytes_per_sample: int,
) -> DirectDownloadEstimate:
    """Estima bytes desde la grilla contractual, sin recalcular dimensiones."""
    return DirectDownloadEstimate(
        target_crs=grid_spec.target_crs,
        scale_m=grid_spec.resolution_m,
        width=grid_spec.width,
        height=grid_spec.height,
        band_count=band_count,
        bytes_per_sample=bytes_per_sample,
        estimated_uncompressed_bytes=(
            grid_spec.width * grid_spec.height * band_count * bytes_per_sample
        ),
        grid_sha256=grid_spec.grid_sha256,
    )


def validate_direct_download(
    estimate: DirectDownloadEstimate,
    *,
    maximum_bytes: int,
    maximum_dimension: int,
) -> DirectDownloadEstimate:
    """Falla de forma explícita; nunca degrada resolución para eludir límites."""
    if (
        estimate.estimated_uncompressed_bytes > maximum_bytes
        or estimate.width > maximum_dimension
        or estimate.height > maximum_dimension
    ):
        raise RasterDownloadError("direct_download_limit_exceeded")
    return estimate


def validate_geotiff_bytes(
    content: bytes,
    *,
    expected_grid: RasterGridSpec,
    expected_band_names: tuple[str, ...],
    artifact_path: str,
) -> tuple[bytes, RasterValidation]:
    """Normaliza metadatos y reabre el GeoTIFF para verificar sus invariantes."""
    try:
        with MemoryFile(content) as source_memory:
            with source_memory.open() as source:
                if source.crs is None:
                    raise RasterDownloadError("geotiff_missing_crs")
                expected_raster_crs = rasterio.crs.CRS.from_user_input(expected_grid.target_crs)
                if source.crs != expected_raster_crs:
                    raise RasterDownloadError("geotiff_crs_mismatch")
                if source.count != len(expected_band_names):
                    raise RasterDownloadError("geotiff_band_count_mismatch")
                if source.width <= 0 or source.height <= 0:
                    raise RasterDownloadError("geotiff_empty_grid")
                if source.width != expected_grid.width or source.height != expected_grid.height:
                    raise RasterDownloadError("geotiff_grid_dimensions_mismatch")
                source_transform = _affine_transform_values(source.transform)
                if not _numeric_tuples_match(
                    source_transform,
                    expected_grid.transform,
                    absolute_tolerance=1e-8,
                ):
                    raise RasterDownloadError("geotiff_grid_transform_mismatch")
                source_bounds = (
                    float(source.bounds.left),
                    float(source.bounds.bottom),
                    float(source.bounds.right),
                    float(source.bounds.top),
                )
                if not _numeric_tuples_match(
                    source_bounds,
                    expected_grid.bounds,
                    absolute_tolerance=1e-8,
                ):
                    raise RasterDownloadError("geotiff_grid_bounds_mismatch")
                values = source.read()
                profile = source.profile.copy()

        profile.update(
            driver="GTiff",
            count=len(expected_band_names),
            nodata=expected_grid.nodata,
            compress="deflate",
        )
        with MemoryFile() as normalized_memory:
            with normalized_memory.open(**profile) as normalized:
                normalized.write(values)
                normalized.descriptions = expected_band_names
            normalized_content = bytes(normalized_memory.read())
    except RasterDownloadError:
        raise
    except Exception:
        raise RasterDownloadError("geotiff_unreadable") from None

    validation = _inspect_normalized_geotiff(
        normalized_content,
        expected_grid=expected_grid,
        expected_band_names=expected_band_names,
        artifact_path=artifact_path,
    )
    return normalized_content, validation


def materialize_hls_raster_products(
    *,
    product: HlsCompositeImages,
    aoi_wgs84: BaseGeometry,
    output_config: OutputConfig,
    grid_spec: RasterGridSpec,
    fetch_bytes: FetchBytes | None = None,
) -> HlsRasterMaterialization:
    """Descarga cinco GeoTIFF sobre una grilla exacta y genera PNG locales."""
    if not math.isclose(
        grid_spec.nodata,
        output_config.raster_nodata,
        abs_tol=1e-9,
    ):
        raise RasterDownloadError("grid_nodata_mismatch")
    effective_fetch = fetch_bytes or _fetch_url_bytes
    raster_specs = (
        (
            "rasters/hls_annual_reflectance.tif",
            "hls_annual_reflectance",
            product.reflectance,
            REFLECTANCE_BAND_NAMES,
            4,
            "float32",
        ),
        (
            "rasters/hls_annual_indices.tif",
            "hls_annual_indices",
            product.indices,
            tuple(item.index.value for item in output_config.index_visualization_ranges),
            4,
            "float32",
        ),
        (
            "rasters/hls_valid_observation_count.tif",
            "hls_valid_observation_count",
            product.valid_observation_count,
            ("valid_observation_count",),
            2,
            "int16",
        ),
        (
            "rasters/hls_valid_observation_count_l30.tif",
            "hls_valid_observation_count_l30",
            product.valid_observation_count_l30,
            ("valid_observation_count_l30",),
            2,
            "int16",
        ),
        (
            "rasters/hls_valid_observation_count_s30.tif",
            "hls_valid_observation_count_s30",
            product.valid_observation_count_s30,
            ("valid_observation_count_s30",),
            2,
            "int16",
        ),
    )
    files: dict[str, bytes] = {}
    estimates: list[DirectDownloadEstimate] = []
    validations: list[RasterValidation] = []

    for (
        artifact_path,
        download_name,
        image,
        band_names,
        bytes_per_sample,
        output_type,
    ) in raster_specs:
        estimate = validate_direct_download(
            estimate_fixed_grid_download(
                grid_spec=grid_spec,
                band_count=len(band_names),
                bytes_per_sample=bytes_per_sample,
            ),
            maximum_bytes=output_config.maximum_direct_download_bytes,
            maximum_dimension=output_config.maximum_direct_download_dimension,
        )
        estimates.append(estimate)
        prepared = image.unmask(output_config.raster_nodata)
        prepared = prepared.toFloat() if output_type == "float32" else prepared.toInt16()
        parameters = {
            "name": download_name,
            "bands": list(band_names),
            "crs": grid_spec.target_crs,
            "crs_transform": list(grid_spec.transform),
            "dimensions": [grid_spec.width, grid_spec.height],
            "format": "GEO_TIFF",
        }
        try:
            url = prepared.getDownloadURL(parameters)
        except Exception as error:
            raise RasterDownloadError(_remote_download_error_code(error)) from None
        if not isinstance(url, str) or urlparse(url).scheme != "https":
            raise RasterDownloadError("download_url_invalid")
        try:
            raw_content = effective_fetch(
                url,
                output_config.maximum_direct_download_bytes,
            )
        except RasterDownloadError:
            raise
        except Exception:
            raise RasterDownloadError("download_transfer_failed") from None
        if len(raw_content) > output_config.maximum_direct_download_bytes:
            raise RasterDownloadError("download_response_too_large")
        normalized, validation = validate_geotiff_bytes(
            raw_content,
            expected_grid=grid_spec,
            expected_band_names=band_names,
            artifact_path=artifact_path,
        )
        files[artifact_path] = normalized
        validations.append(validation)

    figures, visualization_records = _render_visualizations(
        reflectance_content=files["rasters/hls_annual_reflectance.tif"],
        indices_content=files["rasters/hls_annual_indices.tif"],
        count_content=files["rasters/hls_valid_observation_count.tif"],
        aoi_wgs84=aoi_wgs84,
        output_config=output_config,
    )
    files.update(figures)
    return HlsRasterMaterialization(
        files=files,
        estimates=tuple(estimates),
        validations=tuple(validations),
        visualizations=visualization_records,
        grid_spec=grid_spec,
    )


def _inspect_normalized_geotiff(
    content: bytes,
    *,
    expected_grid: RasterGridSpec,
    expected_band_names: tuple[str, ...],
    artifact_path: str,
) -> RasterValidation:
    try:
        with MemoryFile(content) as memory:
            with memory.open() as dataset:
                descriptions = tuple(description or "" for description in dataset.descriptions)
                if descriptions != expected_band_names:
                    raise RasterDownloadError("geotiff_band_names_mismatch")
                if dataset.crs is None or dataset.crs != rasterio.crs.CRS.from_user_input(
                    expected_grid.target_crs
                ):
                    raise RasterDownloadError("geotiff_crs_mismatch")
                if dataset.nodata is None or not math.isclose(
                    float(dataset.nodata),
                    expected_grid.nodata,
                    abs_tol=1e-6,
                ):
                    raise RasterDownloadError("geotiff_nodata_mismatch")
                if dataset.width != expected_grid.width or dataset.height != expected_grid.height:
                    raise RasterDownloadError("geotiff_grid_dimensions_mismatch")
                transform_values = _affine_transform_values(dataset.transform)
                if not _numeric_tuples_match(
                    transform_values,
                    expected_grid.transform,
                    absolute_tolerance=1e-8,
                ):
                    raise RasterDownloadError("geotiff_grid_transform_mismatch")
                bounds = dataset.bounds
                bounds_values = (
                    float(bounds.left),
                    float(bounds.bottom),
                    float(bounds.right),
                    float(bounds.top),
                )
                if not _numeric_tuples_match(
                    bounds_values,
                    expected_grid.bounds,
                    absolute_tolerance=1e-8,
                ):
                    raise RasterDownloadError("geotiff_grid_bounds_mismatch")
                resolution = (
                    abs(float(dataset.transform.a)),
                    abs(float(dataset.transform.e)),
                )
                if not all(
                    math.isclose(value, expected_grid.resolution_m, abs_tol=1e-6)
                    for value in resolution
                ):
                    raise RasterDownloadError("geotiff_resolution_mismatch")
                values = dataset.read().astype(np.float64, copy=False)
                minimums: list[float] = []
                maximums: list[float] = []
                for band in values:
                    valid = np.isfinite(band) & ~np.isclose(
                        band,
                        expected_grid.nodata,
                    )
                    if not np.any(valid):
                        raise RasterDownloadError("geotiff_band_has_no_valid_pixels")
                    minimums.append(float(np.min(band[valid])))
                    maximums.append(float(np.max(band[valid])))
                return RasterValidation(
                    artifact_path=artifact_path,
                    crs=dataset.crs.to_string(),
                    width=dataset.width,
                    height=dataset.height,
                    band_count=dataset.count,
                    band_names=descriptions,
                    data_types=tuple(dataset.dtypes),
                    nodata=float(dataset.nodata),
                    resolution_m=resolution,
                    transform=transform_values,
                    bounds=bounds_values,
                    grid_sha256=expected_grid.grid_sha256,
                    minimum_by_band=tuple(minimums),
                    maximum_by_band=tuple(maximums),
                )
    except RasterDownloadError:
        raise
    except Exception:
        raise RasterDownloadError("geotiff_validation_failed") from None


def _affine_transform_values(
    affine: Any,
) -> tuple[float, float, float, float, float, float]:
    return (
        float(affine.a),
        float(affine.b),
        float(affine.c),
        float(affine.d),
        float(affine.e),
        float(affine.f),
    )


def _numeric_tuples_match(
    first: tuple[float, ...],
    second: tuple[float, ...],
    *,
    absolute_tolerance: float,
) -> bool:
    return len(first) == len(second) and all(
        math.isclose(left, right, abs_tol=absolute_tolerance)
        for left, right in zip(first, second, strict=True)
    )


def _fetch_url_bytes(url: str, maximum_bytes: int) -> bytes:
    request = Request(url, headers={"User-Agent": "deforestation-pipeline/0.1"})
    try:
        with urlopen(request, timeout=180) as response:
            content = bytes(response.read(maximum_bytes + 1))
    except Exception:
        raise RasterDownloadError("download_transfer_failed") from None
    if len(content) > maximum_bytes:
        raise RasterDownloadError("download_response_too_large")
    return content


def _render_visualizations(
    *,
    reflectance_content: bytes,
    indices_content: bytes,
    count_content: bytes,
    aoi_wgs84: BaseGeometry,
    output_config: OutputConfig,
) -> tuple[dict[str, bytes], tuple[VisualizationRecord, ...]]:
    reflectance = _load_raster_data(reflectance_content)
    indices = _load_raster_data(indices_content)
    count = _load_raster_data(count_content)
    _assert_same_grid((reflectance, indices, count))
    aoi_projected = _project_geometry(aoi_wgs84, reflectance.crs)
    files: dict[str, bytes] = {}
    records: list[VisualizationRecord] = []

    rgb_path = "figures/rgb.png"
    files[rgb_path] = _render_rgb(
        reflectance,
        aoi_projected,
        display_min=output_config.rgb_min_reflectance,
        display_max=output_config.rgb_max_reflectance,
        dpi=output_config.png_dpi,
    )
    records.append(
        VisualizationRecord(
            path=rgb_path,
            source_raster="rasters/hls_annual_reflectance.tif",
            band_name="red,green,blue",
            display_min=output_config.rgb_min_reflectance,
            display_max=output_config.rgb_max_reflectance,
            colormap="RGB",
            nodata_transparent=True,
            roi_overlay=True,
        )
    )

    visualization_ranges = output_config.index_visualization_range_map
    for index in (item.index for item in output_config.index_visualization_ranges):
        path = f"figures/indices/{index.value}.png"
        display_min, display_max = visualization_ranges[index]
        colormap = INDEX_COLORMAPS[index]
        files[path] = _render_single_band(
            indices,
            aoi_projected,
            band_name=index.value,
            title=f"Composite anual HLS — {index.value}",
            display_min=display_min,
            display_max=display_max,
            colormap=colormap,
            dpi=output_config.png_dpi,
        )
        records.append(
            VisualizationRecord(
                path=path,
                source_raster="rasters/hls_annual_indices.tif",
                band_name=index.value,
                display_min=display_min,
                display_max=display_max,
                colormap=colormap,
                nodata_transparent=True,
                roi_overlay=True,
            )
        )

    panel_path = "figures/indices_panel.png"
    files[panel_path] = _render_index_panel(
        indices,
        aoi_projected,
        output_config=output_config,
    )
    records.append(
        VisualizationRecord(
            path=panel_path,
            source_raster="rasters/hls_annual_indices.tif",
            band_name="all_indices",
            display_min=None,
            display_max=None,
            colormap="per_index",
            nodata_transparent=True,
            roi_overlay=True,
        )
    )

    count_values = count.values[0][~count.mask[0]]
    count_max = max(1.0, float(np.max(count_values)))
    count_path = "figures/valid_observations.png"
    files[count_path] = _render_single_band(
        count,
        aoi_projected,
        band_name="valid_observation_count",
        title="Observaciones HLS válidas por píxel",
        display_min=0.0,
        display_max=count_max,
        colormap="viridis",
        dpi=output_config.png_dpi,
    )
    records.append(
        VisualizationRecord(
            path=count_path,
            source_raster="rasters/hls_valid_observation_count.tif",
            band_name="valid_observation_count",
            display_min=0.0,
            display_max=count_max,
            colormap="viridis",
            nodata_transparent=True,
            roi_overlay=True,
        )
    )
    return files, tuple(records)


def _load_raster_data(content: bytes) -> _RasterData:
    with MemoryFile(content) as memory:
        with memory.open() as dataset:
            if dataset.crs is None or dataset.nodata is None:
                raise RasterDownloadError("render_requires_crs_and_nodata")
            values = dataset.read().astype(np.float64, copy=False)
            nodata = float(dataset.nodata)
            mask = ~np.isfinite(values) | np.isclose(values, nodata)
            bounds = dataset.bounds
            return _RasterData(
                values=values,
                mask=mask,
                band_names=tuple(description or "" for description in dataset.descriptions),
                crs=dataset.crs.to_string(),
                transform=dataset.transform,
                bounds=(
                    float(bounds.left),
                    float(bounds.bottom),
                    float(bounds.right),
                    float(bounds.top),
                ),
                nodata=nodata,
            )


def _assert_same_grid(rasters: tuple[_RasterData, ...]) -> None:
    reference = rasters[0]
    reference_shape = reference.values.shape[1:]
    for raster in rasters[1:]:
        if (
            raster.crs != reference.crs
            or raster.transform != reference.transform
            or raster.values.shape[1:] != reference_shape
            or raster.bounds != reference.bounds
        ):
            raise RasterDownloadError("raster_grids_do_not_match")


def _project_geometry(geometry: BaseGeometry, target_crs: str) -> BaseGeometry:
    try:
        transformer = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)
        return transform(transformer.transform, geometry)
    except Exception:
        raise RasterDownloadError("roi_overlay_projection_failed") from None


def _render_rgb(
    raster: _RasterData,
    aoi_projected: BaseGeometry,
    *,
    display_min: float,
    display_max: float,
    dpi: int,
) -> bytes:
    indices = {name: position for position, name in enumerate(raster.band_names)}
    try:
        selected = [indices[name] for name in ("red", "green", "blue")]
    except KeyError:
        raise RasterDownloadError("rgb_bands_missing") from None
    rgb = np.moveaxis(raster.values[selected], 0, -1)
    invalid = np.any(raster.mask[selected], axis=0)
    scaled = np.clip((rgb - display_min) / (display_max - display_min), 0, 1)
    rgba = np.concatenate(
        (scaled, np.where(invalid, 0.0, 1.0)[..., np.newaxis]),
        axis=2,
    )
    figure, axis = plt.subplots(figsize=(8, 7), constrained_layout=True)
    axis.imshow(rgba, extent=_image_extent(raster), origin="upper")
    _decorate_axis(
        axis,
        raster,
        aoi_projected,
        title="Composite anual HLS — RGB",
    )
    return _figure_bytes(figure, dpi)


def _render_single_band(
    raster: _RasterData,
    aoi_projected: BaseGeometry,
    *,
    band_name: str,
    title: str,
    display_min: float,
    display_max: float,
    colormap: str,
    dpi: int,
) -> bytes:
    try:
        band_index = raster.band_names.index(band_name)
    except ValueError:
        raise RasterDownloadError("visualization_band_missing") from None
    values = np.ma.array(raster.values[band_index], mask=raster.mask[band_index])
    color_map = matplotlib.colormaps[colormap].with_extremes(bad=(0, 0, 0, 0))
    figure, axis = plt.subplots(figsize=(8, 7), constrained_layout=True)
    image = axis.imshow(
        values,
        extent=_image_extent(raster),
        origin="upper",
        cmap=color_map,
        vmin=display_min,
        vmax=display_max,
    )
    figure.colorbar(image, ax=axis, shrink=0.8, label=band_name)
    _decorate_axis(axis, raster, aoi_projected, title=title)
    return _figure_bytes(figure, dpi)


def _render_index_panel(
    raster: _RasterData,
    aoi_projected: BaseGeometry,
    *,
    output_config: OutputConfig,
) -> bytes:
    ranges = output_config.index_visualization_range_map
    items = output_config.index_visualization_ranges
    columns = 4
    rows = math.ceil(len(items) / columns)
    figure, axes = plt.subplots(
        rows,
        columns,
        figsize=(16, 4 * rows),
        constrained_layout=True,
        squeeze=False,
    )
    flat_axes = list(axes.flat)
    for axis, item in zip(flat_axes, items, strict=False):
        band_index = raster.band_names.index(item.index.value)
        values = np.ma.array(raster.values[band_index], mask=raster.mask[band_index])
        color_map = matplotlib.colormaps[INDEX_COLORMAPS[item.index]].with_extremes(
            bad=(0, 0, 0, 0)
        )
        display_min, display_max = ranges[item.index]
        image = axis.imshow(
            values,
            extent=_image_extent(raster),
            origin="upper",
            cmap=color_map,
            vmin=display_min,
            vmax=display_max,
        )
        figure.colorbar(image, ax=axis, shrink=0.65)
        _decorate_axis(
            axis,
            raster,
            aoi_projected,
            title=item.index.value,
            compact=True,
        )
    for axis in flat_axes[len(items) :]:
        axis.set_visible(False)
    figure.suptitle("Índices del composite anual HLS")
    return _figure_bytes(figure, output_config.png_dpi)


def _decorate_axis(
    axis: Axes,
    raster: _RasterData,
    aoi_projected: BaseGeometry,
    *,
    title: str,
    compact: bool = False,
) -> None:
    _plot_geometry_outline(axis, aoi_projected)
    left, right, bottom, top = _image_extent(raster)
    axis.set_xlim(left, right)
    axis.set_ylim(bottom, top)
    axis.set_aspect("equal")
    axis.set_title(title)
    if compact:
        axis.set_xticks([])
        axis.set_yticks([])
    else:
        axis.set_xlabel(f"X ({raster.crs})")
        axis.set_ylabel(f"Y ({raster.crs})")


def _plot_geometry_outline(axis: Axes, geometry: BaseGeometry) -> None:
    if isinstance(geometry, MultiPolygon):
        polygons = tuple(geometry.geoms)
    elif isinstance(geometry, Polygon):
        polygons = (geometry,)
    else:
        raise RasterDownloadError("roi_overlay_requires_polygonal_geometry")
    for polygon in polygons:
        x_values, y_values = polygon.exterior.xy
        axis.plot(x_values, y_values, color="black", linewidth=1.0)


def _image_extent(raster: _RasterData) -> tuple[float, float, float, float]:
    left, bottom, right, top = raster.bounds
    return left, right, bottom, top


def _figure_bytes(figure: Figure, dpi: int) -> bytes:
    buffer = BytesIO()
    try:
        figure.savefig(
            buffer,
            format="png",
            dpi=dpi,
            metadata={"Software": "deforestation-pipeline"},
        )
        return buffer.getvalue()
    finally:
        plt.close(figure)


def _remote_download_error_code(error: Exception) -> str:
    message = str(error).lower()
    if "permission" in message or "forbidden" in message:
        return "permission_denied"
    if "quota" in message or "rate" in message:
        return "quota_or_rate_limit"
    if "too large" in message or "request size" in message:
        return "remote_size_limit_exceeded"
    if "timeout" in message or "deadline" in message:
        return "timeout"
    return "download_url_failed"
