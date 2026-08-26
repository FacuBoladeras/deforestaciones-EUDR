"""Descarga raster acotada, validación GeoTIFF y render PNG local."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Literal
from urllib.error import HTTPError, URLError
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
AllNodataBandPolicy = Literal[
    "reject",
    "allow_for_missing_period",
    "defer_to_band_groups",
]
DOWNLOAD_TRANSFER_MAX_ATTEMPTS = 2
DOWNLOAD_TRANSFER_RETRY_DELAY_SECONDS = 1.0
DOWNLOAD_TRANSFER_TIMEOUT_SECONDS = 180
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

    def __init__(
        self,
        code: str,
        *,
        artifact_path: str | None = None,
        product: str | None = None,
        band_index: int | None = None,
    ) -> None:
        self.code = code
        self.artifact_path = artifact_path
        self.product = product
        self.band_index = band_index
        artifact_label = artifact_path or "<unknown>"
        product_label = product or "<unknown>"
        band_label = str(band_index) if band_index is not None else "<not_applicable>"
        super().__init__(
            "Descarga raster fallida: "
            f"{code}; artifact_path={artifact_label}; "
            f"product={product_label}; band_index={band_label}"
        )

    def with_context(
        self,
        *,
        artifact_path: str,
        product: str,
        band_index: int | None = None,
    ) -> RasterDownloadError:
        """Completa contexto conocido sin incorporar texto remoto ni URLs."""
        return RasterDownloadError(
            self.code,
            artifact_path=self.artifact_path or artifact_path,
            product=self.product or product,
            band_index=self.band_index if self.band_index is not None else band_index,
        )


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
    minimum_by_band: tuple[float | None, ...]
    maximum_by_band: tuple[float | None, ...]
    valid_pixel_count_by_band: tuple[int, ...]
    all_nodata_band_names: tuple[str, ...]


class VisualizationRecord(BaseModel):
    """Parámetros efectivos de una figura derivada; nunca altera los datos."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    source_raster: str
    band_name: str
    title: str
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
    transport_strategy: str = "individual_geotiffs"
    transport_request_count: int = 5

    def metadata_payload(self) -> dict[str, object]:
        return {
            "download_method": "ee.Image.getDownloadURL",
            "signed_urls_persisted": False,
            "resolution_degraded": False,
            "transport_strategy": self.transport_strategy,
            "transport_request_count": self.transport_request_count,
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
class SingleRasterMaterialization:
    """Un GeoTIFF normalizado sobre la grilla contractual."""

    content: bytes
    estimate: DirectDownloadEstimate
    validation: RasterValidation


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
    product: str | None = None,
    all_nodata_band_policy: AllNodataBandPolicy = "reject",
) -> tuple[bytes, RasterValidation]:
    """Normaliza metadatos y reabre el GeoTIFF para verificar sus invariantes."""
    product_name = product or artifact_path
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
    except RasterDownloadError as error:
        raise error.with_context(
            artifact_path=artifact_path,
            product=product_name,
        ) from None
    except Exception:
        raise RasterDownloadError("geotiff_unreadable") from None

    validation = _inspect_normalized_geotiff(
        normalized_content,
        expected_grid=expected_grid,
        expected_band_names=expected_band_names,
        artifact_path=artifact_path,
        product=product_name,
        all_nodata_band_policy=all_nodata_band_policy,
    )
    return normalized_content, validation


def _gee_download_crs(target_crs: str) -> str:
    """Devuelve una representación que Earth Engine pueda interpretar.

    Earth Engine rechaza el alias ``EPSG:6933`` aunque acepta su definición
    WKT1 completa. Conservamos EPSG:6933 en el contrato local y sólo adaptamos
    el parámetro de transporte de ``getDownloadURL``.
    """
    if target_crs.upper() == "EPSG:6933":
        return CRS.from_user_input(target_crs).to_wkt(version="WKT1_GDAL")
    return target_crs


def materialize_ee_image_to_grid(
    *,
    image: Any,
    band_names: tuple[str, ...],
    artifact_path: str,
    download_name: str,
    output_config: OutputConfig,
    grid_spec: RasterGridSpec,
    output_type: Literal["float32", "int16"],
    fetch_bytes: FetchBytes | None = None,
    all_nodata_band_policy: AllNodataBandPolicy = "reject",
) -> SingleRasterMaterialization:
    """Descarga una imagen GEE arbitraria sin relajar grilla ni presupuesto."""
    product_name = download_name
    if not math.isclose(
        grid_spec.nodata,
        output_config.raster_nodata,
        abs_tol=1e-9,
    ):
        raise RasterDownloadError(
            "grid_nodata_mismatch",
            artifact_path=artifact_path,
            product=product_name,
        )
    try:
        estimate = validate_direct_download(
            estimate_fixed_grid_download(
                grid_spec=grid_spec,
                band_count=len(band_names),
                bytes_per_sample=4 if output_type == "float32" else 2,
            ),
            maximum_bytes=output_config.maximum_direct_download_bytes,
            maximum_dimension=output_config.maximum_direct_download_dimension,
        )
    except RasterDownloadError as error:
        raise error.with_context(
            artifact_path=artifact_path,
            product=product_name,
        ) from None
    prepared = image.toFloat() if output_type == "float32" else image.toInt16()
    prepared = prepared.unmask(output_config.raster_nodata, False)
    parameters = {
        "name": download_name,
        "bands": list(band_names),
        "crs": _gee_download_crs(grid_spec.target_crs),
        "crs_transform": list(grid_spec.transform),
        "dimensions": [grid_spec.width, grid_spec.height],
        "format": "GEO_TIFF",
    }
    try:
        url = prepared.getDownloadURL(parameters)
    except Exception as error:
        raise RasterDownloadError(
            _remote_download_error_code(error),
            artifact_path=artifact_path,
            product=product_name,
        ) from None
    if not isinstance(url, str) or urlparse(url).scheme != "https":
        raise RasterDownloadError(
            "download_url_invalid",
            artifact_path=artifact_path,
            product=product_name,
        )
    effective_fetch = fetch_bytes or _fetch_url_bytes
    try:
        raw_content = effective_fetch(url, output_config.maximum_direct_download_bytes)
    except RasterDownloadError as error:
        raise error.with_context(
            artifact_path=artifact_path,
            product=product_name,
        ) from None
    except Exception:
        raise RasterDownloadError(
            "download_transfer_failed",
            artifact_path=artifact_path,
            product=product_name,
        ) from None
    if len(raw_content) > output_config.maximum_direct_download_bytes:
        raise RasterDownloadError(
            "download_response_too_large",
            artifact_path=artifact_path,
            product=product_name,
        )
    normalized, validation = validate_geotiff_bytes(
        raw_content,
        expected_grid=grid_spec,
        expected_band_names=band_names,
        artifact_path=artifact_path,
        product=product_name,
        all_nodata_band_policy=all_nodata_band_policy,
    )
    return SingleRasterMaterialization(
        content=normalized,
        estimate=estimate,
        validation=validation,
    )


def extract_geotiff_band_group(
    content: bytes,
    *,
    band_names: tuple[str, ...],
    artifact_path: str,
    product: str,
    output_type: Literal["float32", "int16"],
    expected_grid: RasterGridSpec,
    all_nodata_band_policy: AllNodataBandPolicy = "reject",
) -> tuple[bytes, RasterValidation]:
    """Extrae bandas de un transporte multibanda sin conservar el contenedor remoto."""
    dtype = "float32" if output_type == "float32" else "int16"
    try:
        with MemoryFile(content) as source_memory:
            with source_memory.open() as source:
                descriptions = tuple(description or "" for description in source.descriptions)
                if len(descriptions) != len(set(descriptions)):
                    raise RasterDownloadError("geotiff_band_names_duplicated")
                missing = tuple(name for name in band_names if name not in descriptions)
                if missing:
                    raise RasterDownloadError("geotiff_band_names_mismatch")
                indexes = tuple(descriptions.index(name) + 1 for name in band_names)
                values = source.read(indexes).astype(dtype)
                profile = {
                    "driver": "GTiff",
                    "width": source.width,
                    "height": source.height,
                    "count": len(band_names),
                    "dtype": dtype,
                    "crs": source.crs,
                    "transform": source.transform,
                    "nodata": expected_grid.nodata,
                    "compress": "deflate",
                }
        with MemoryFile() as target_memory:
            with target_memory.open(**profile) as target:
                target.write(values)
                target.descriptions = band_names
            extracted = bytes(target_memory.read())
    except RasterDownloadError as error:
        raise error.with_context(artifact_path=artifact_path, product=product) from None
    except Exception:
        raise RasterDownloadError(
            "geotiff_band_extraction_failed",
            artifact_path=artifact_path,
            product=product,
        ) from None
    return validate_geotiff_bytes(
        extracted,
        expected_grid=expected_grid,
        expected_band_names=band_names,
        artifact_path=artifact_path,
        product=product,
        all_nodata_band_policy=all_nodata_band_policy,
    )


def materialize_hls_raster_products(
    *,
    product: HlsCompositeImages,
    aoi_wgs84: BaseGeometry,
    output_config: OutputConfig,
    grid_spec: RasterGridSpec,
    fetch_bytes: FetchBytes | None = None,
    artifact_kind: Literal["annual", "period"] = "annual",
) -> HlsRasterMaterialization:
    """Descarga un transporte multibanda, lo separa y genera PNG locales."""
    if not math.isclose(
        grid_spec.nodata,
        output_config.raster_nodata,
        abs_tol=1e-9,
    ):
        raise RasterDownloadError(
            "grid_nodata_mismatch",
            artifact_path="<hls_bundle>",
            product=f"hls_{artifact_kind}",
        )
    raster_specs: tuple[
        tuple[str, str, Any, tuple[str, ...], int, Literal["float32", "int16"]], ...
    ] = (
        (
            f"tiffs/hls_{artifact_kind}_reflectance.tif",
            f"hls_{artifact_kind}_reflectance",
            product.reflectance,
            REFLECTANCE_BAND_NAMES,
            4,
            "float32",
        ),
        (
            f"tiffs/hls_{artifact_kind}_indices.tif",
            f"hls_{artifact_kind}_indices",
            product.indices,
            tuple(item.index.value for item in output_config.index_visualization_ranges),
            4,
            "float32",
        ),
        (
            "tiffs/hls_valid_observation_count.tif",
            "hls_valid_observation_count",
            product.valid_observation_count,
            ("valid_observation_count",),
            2,
            "int16",
        ),
        (
            "tiffs/hls_valid_observation_count_l30.tif",
            "hls_valid_observation_count_l30",
            product.valid_observation_count_l30,
            ("valid_observation_count_l30",),
            2,
            "int16",
        ),
        (
            "tiffs/hls_valid_observation_count_s30.tif",
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
    transport_band_names = tuple(
        name for *_, names, _bytes, _type in raster_specs for name in names
    )
    transport_strategy = "single_multiband_geotiff"
    try:
        validate_direct_download(
            estimate_fixed_grid_download(
                grid_spec=grid_spec,
                band_count=len(transport_band_names),
                bytes_per_sample=4,
            ),
            maximum_bytes=output_config.maximum_direct_download_bytes,
            maximum_dimension=output_config.maximum_direct_download_dimension,
        )
    except RasterDownloadError as error:
        if error.code != "direct_download_limit_exceeded":
            raise
        transport_strategy = "individual_geotiff_fallback"

    if transport_strategy == "single_multiband_geotiff":
        transport_image = product.reflectance.addBands(product.indices)
        transport_image = transport_image.addBands(product.valid_observation_count)
        transport_image = transport_image.addBands(product.valid_observation_count_l30)
        transport_image = transport_image.addBands(product.valid_observation_count_s30)
        transport = materialize_ee_image_to_grid(
            image=transport_image,
            band_names=transport_band_names,
            artifact_path=f"internal/hls_{artifact_kind}_transport.tif",
            download_name=f"hls_{artifact_kind}_transport",
            output_config=output_config,
            grid_spec=grid_spec,
            output_type="float32",
            fetch_bytes=fetch_bytes,
            all_nodata_band_policy="defer_to_band_groups",
        )
        estimates.append(transport.estimate)
        for artifact_path, download_name, _image, band_names, _bytes, output_type in raster_specs:
            normalized, validation = extract_geotiff_band_group(
                transport.content,
                band_names=band_names,
                artifact_path=artifact_path,
                product=download_name,
                output_type=output_type,
                expected_grid=grid_spec,
                all_nodata_band_policy=(
                    "allow_for_missing_period"
                    if artifact_kind == "period" and output_type == "float32"
                    else "reject"
                ),
            )
            files[artifact_path] = normalized
            validations.append(validation)
    else:
        for artifact_path, download_name, image, band_names, _bytes, output_type in raster_specs:
            materialized = materialize_ee_image_to_grid(
                image=image,
                band_names=band_names,
                artifact_path=artifact_path,
                download_name=download_name,
                output_config=output_config,
                grid_spec=grid_spec,
                output_type=output_type,
                fetch_bytes=fetch_bytes,
                all_nodata_band_policy=(
                    "allow_for_missing_period"
                    if artifact_kind == "period" and output_type == "float32"
                    else "reject"
                ),
            )
            files[artifact_path] = materialized.content
            estimates.append(materialized.estimate)
            validations.append(materialized.validation)

    reflectance_path = f"tiffs/hls_{artifact_kind}_reflectance.tif"
    indices_path = f"tiffs/hls_{artifact_kind}_indices.tif"
    count_path = "tiffs/hls_valid_observation_count.tif"
    figures, visualization_records = _render_visualizations(
        reflectance_content=files[reflectance_path],
        indices_content=files[indices_path],
        count_content=files["tiffs/hls_valid_observation_count.tif"],
        reflectance_source_path=reflectance_path,
        indices_source_path=indices_path,
        count_source_path=count_path,
        composite_label=("anual" if artifact_kind == "annual" else "del período"),
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
        transport_strategy=transport_strategy,
        transport_request_count=len(estimates),
    )


def _inspect_normalized_geotiff(
    content: bytes,
    *,
    expected_grid: RasterGridSpec,
    expected_band_names: tuple[str, ...],
    artifact_path: str,
    product: str,
    all_nodata_band_policy: AllNodataBandPolicy,
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
                minimums: list[float | None] = []
                maximums: list[float | None] = []
                valid_counts: list[int] = []
                all_nodata_band_names: list[str] = []
                valid_masks = tuple(
                    np.isfinite(band)
                    & ~np.isclose(
                        band,
                        expected_grid.nodata,
                    )
                    for band in values
                )
                empty_band_indices = tuple(
                    index for index, valid in enumerate(valid_masks) if not np.any(valid)
                )
                if (
                    empty_band_indices
                    and all_nodata_band_policy != "defer_to_band_groups"
                    and (
                        all_nodata_band_policy == "reject"
                        or len(empty_band_indices) != len(expected_band_names)
                    )
                ):
                    raise RasterDownloadError(
                        "geotiff_band_has_no_valid_pixels",
                        artifact_path=artifact_path,
                        product=product,
                        band_index=empty_band_indices[0],
                    )
                for band_index, (band_name, band) in enumerate(
                    zip(expected_band_names, values, strict=True)
                ):
                    valid = valid_masks[band_index]
                    if not np.any(valid):
                        minimums.append(None)
                        maximums.append(None)
                        valid_counts.append(0)
                        all_nodata_band_names.append(band_name)
                        continue
                    minimums.append(float(np.min(band[valid])))
                    maximums.append(float(np.max(band[valid])))
                    valid_counts.append(int(np.count_nonzero(valid)))
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
                    valid_pixel_count_by_band=tuple(valid_counts),
                    all_nodata_band_names=tuple(all_nodata_band_names),
                )
    except RasterDownloadError as error:
        raise error.with_context(
            artifact_path=artifact_path,
            product=product,
        ) from None
    except Exception:
        raise RasterDownloadError(
            "geotiff_validation_failed",
            artifact_path=artifact_path,
            product=product,
        ) from None


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
    for attempt in range(1, DOWNLOAD_TRANSFER_MAX_ATTEMPTS + 1):
        try:
            with urlopen(
                request,
                timeout=DOWNLOAD_TRANSFER_TIMEOUT_SECONDS,
            ) as response:
                content = bytes(response.read(maximum_bytes + 1))
        except Exception as error:
            code = _download_transfer_error_code(error)
            if attempt < DOWNLOAD_TRANSFER_MAX_ATTEMPTS and _is_retryable_transfer_error(code):
                time.sleep(DOWNLOAD_TRANSFER_RETRY_DELAY_SECONDS)
                continue
            raise RasterDownloadError(code) from None
        if len(content) > maximum_bytes:
            raise RasterDownloadError("download_response_too_large")
        return content
    raise AssertionError("bucle de descarga sin resultado")


def _download_transfer_error_code(error: Exception) -> str:
    """Clasifica fallas sin persistir URL firmada ni texto remoto."""
    if isinstance(error, HTTPError):
        if error.code in {401, 403}:
            return "permission_denied"
        if error.code == 429:
            return "quota_or_rate_limit"
        if error.code in {408, 504}:
            return "timeout"
        if error.code == 413:
            return "remote_size_limit_exceeded"
        if 500 <= error.code <= 599:
            return "remote_server_error"
        return "download_transfer_failed"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, URLError) and isinstance(
        error.reason,
        TimeoutError,
    ):
        return "timeout"
    return "download_transfer_failed"


def _is_retryable_transfer_error(code: str) -> bool:
    return code in {
        "download_transfer_failed",
        "quota_or_rate_limit",
        "remote_server_error",
        "timeout",
    }


def _render_visualizations(
    *,
    reflectance_content: bytes,
    indices_content: bytes,
    count_content: bytes,
    reflectance_source_path: str,
    indices_source_path: str,
    count_source_path: str,
    composite_label: str,
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
    rgb_title = f"Composite {composite_label} HLS — RGB"
    files[rgb_path] = _render_rgb(
        reflectance,
        aoi_projected,
        display_min=output_config.rgb_min_reflectance,
        display_max=output_config.rgb_max_reflectance,
        dpi=output_config.png_dpi,
        title=rgb_title,
    )
    records.append(
        VisualizationRecord(
            path=rgb_path,
            source_raster=reflectance_source_path,
            band_name="red,green,blue",
            title=rgb_title,
            display_min=output_config.rgb_min_reflectance,
            display_max=output_config.rgb_max_reflectance,
            colormap="RGB",
            nodata_transparent=True,
            roi_overlay=True,
        )
    )

    visualization_ranges = output_config.index_visualization_range_map
    for index in (item.index for item in output_config.index_visualization_ranges):
        path = f"figures/index_{index.value}.png"
        display_min, display_max = visualization_ranges[index]
        colormap = INDEX_COLORMAPS[index]
        index_title = f"Composite HLS — {index.value}"
        files[path] = _render_single_band(
            indices,
            aoi_projected,
            band_name=index.value,
            title=index_title,
            display_min=display_min,
            display_max=display_max,
            colormap=colormap,
            dpi=output_config.png_dpi,
        )
        records.append(
            VisualizationRecord(
                path=path,
                source_raster=indices_source_path,
                band_name=index.value,
                title=index_title,
                display_min=display_min,
                display_max=display_max,
                colormap=colormap,
                nodata_transparent=True,
                roi_overlay=True,
            )
        )

    panel_path = "figures/indices_panel.png"
    panel_title = f"Índices del composite {composite_label} HLS"
    files[panel_path] = _render_index_panel(
        indices,
        aoi_projected,
        output_config=output_config,
        title=panel_title,
    )
    records.append(
        VisualizationRecord(
            path=panel_path,
            source_raster=indices_source_path,
            band_name="all_indices",
            title=panel_title,
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
    count_title = "Observaciones HLS válidas por píxel"
    files[count_path] = _render_single_band(
        count,
        aoi_projected,
        band_name="valid_observation_count",
        title=count_title,
        display_min=0.0,
        display_max=count_max,
        colormap="viridis",
        dpi=output_config.png_dpi,
    )
    records.append(
        VisualizationRecord(
            path=count_path,
            source_raster=count_source_path,
            band_name="valid_observation_count",
            title=count_title,
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
    title: str,
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
        title=title,
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
    title: str,
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
    figure.suptitle(title)
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
    if "projection" in message and "crs" in message and "pars" in message:
        return "remote_projection_invalid"
    if "permission" in message or "forbidden" in message:
        return "permission_denied"
    if "quota" in message or "rate" in message:
        return "quota_or_rate_limit"
    if "too large" in message or "request size" in message:
        return "remote_size_limit_exceeded"
    if "timeout" in message or "deadline" in message:
        return "timeout"
    return "download_url_failed"
