"""Descarga, validación y render local del primer producto raster."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import Affine, from_origin
from shapely.geometry import Point, Polygon, box

from deforestation_pipeline.config import load_config
from deforestation_pipeline.hls_composite import HlsCompositeImages
from deforestation_pipeline.raster_products import (
    RasterDownloadError,
    estimate_direct_download,
    materialize_hls_raster_products,
    validate_direct_download,
    validate_geotiff_bytes,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_download_estimate_is_computed_in_the_explicit_target_grid() -> None:
    estimate = estimate_direct_download(
        aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
        target_crs="EPSG:6933",
        scale_m=30,
        band_count=6,
        bytes_per_sample=4,
    )

    assert estimate.target_crs == "EPSG:6933"
    assert estimate.width > 0
    assert estimate.height > 0
    assert estimate.band_count == 6
    assert estimate.estimated_uncompressed_bytes == (estimate.width * estimate.height * 6 * 4)


def test_download_guard_rejects_size_or_dimension_without_changing_resolution() -> None:
    estimate = estimate_direct_download(
        aoi_wgs84=box(-73.5, -55.0, -53.5, -21.5),
        target_crs="EPSG:6933",
        scale_m=30,
        band_count=8,
        bytes_per_sample=4,
    )

    with pytest.raises(RasterDownloadError) as captured:
        validate_direct_download(
            estimate,
            maximum_bytes=32_000_000,
            maximum_dimension=10_000,
        )

    assert "direct_download_limit_exceeded" in str(captured.value)
    assert estimate.scale_m == 30


class _DownloadImage:
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        self.download_parameters: dict[str, object] | None = None
        self.unmask_value: float | None = None

    def unmask(self, value: float) -> _DownloadImage:
        self.unmask_value = value
        return self

    def toFloat(self) -> _DownloadImage:
        return self

    def toInt16(self) -> _DownloadImage:
        return self

    def getDownloadURL(self, parameters: dict[str, object]) -> str:
        self.download_parameters = parameters
        return f"https://example.invalid/{self.identifier}"


def _geotiff_bytes(
    *,
    band_count: int,
    dtype: str,
    values: np.ndarray[Any, Any],
    crs: str = "EPSG:6933",
    transform_value: Affine | None = None,
) -> bytes:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=values.shape[2],
            height=values.shape[1],
            count=band_count,
            dtype=dtype,
            crs=crs,
            transform=transform_value or from_origin(-6_000_000, -3_000_000, 30, 30),
        ) as dataset:
            dataset.write(values)
        return bytes(memory.read())


def test_downloaded_geotiff_is_normalized_and_scientifically_validated() -> None:
    content = _geotiff_bytes(
        band_count=2,
        dtype="float32",
        values=np.array(
            [
                [[0.1, 0.2], [0.3, 0.4]],
                [[-0.5, 0.0], [0.5, 1.0]],
            ],
            dtype=np.float32,
        ),
    )

    normalized, validation = validate_geotiff_bytes(
        content,
        expected_crs="EPSG:6933",
        expected_scale_m=30,
        expected_band_names=("red", "NDVI"),
        nodata=-9999.0,
        artifact_path="rasters/test.tif",
    )

    assert normalized.startswith((b"II*\x00", b"MM\x00*"))
    assert validation.crs == "EPSG:6933"
    assert validation.band_names == ("red", "NDVI")
    assert validation.resolution_m == (30.0, 30.0)
    assert validation.minimum_by_band == pytest.approx((0.1, -0.5))
    assert validation.maximum_by_band == pytest.approx((0.4, 1.0))


def test_materialization_returns_geotiffs_and_local_pngs_without_signed_urls() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    reflectance = _DownloadImage("reflectance")
    indices = _DownloadImage("indices")
    count = _DownloadImage("count")
    product = HlsCompositeImages(
        reflectance=reflectance,
        indices=indices,
        valid_observation_count=count,
        metadata=cast(Any, None),
    )
    height = 6
    width = 8
    reflectance_values = np.stack(
        [
            np.full((height, width), value, dtype=np.float32)
            for value in (0.08, 0.10, 0.12, 0.30, 0.20, 0.15)
        ]
    )
    index_values = np.stack(
        [
            np.full((height, width), value, dtype=np.float32)
            for value in (0.5, 0.4, 0.3, 0.2, 0.1, 0.2, 0.15, 0.25)
        ]
    )
    count_values = np.full((1, height, width), 12, dtype=np.int16)
    responses = {
        "https://example.invalid/reflectance": _geotiff_bytes(
            band_count=6,
            dtype="float32",
            values=reflectance_values,
        ),
        "https://example.invalid/indices": _geotiff_bytes(
            band_count=8,
            dtype="float32",
            values=index_values,
        ),
        "https://example.invalid/count": _geotiff_bytes(
            band_count=1,
            dtype="int16",
            values=count_values,
        ),
    }

    result = materialize_hls_raster_products(
        product=product,
        aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
        output_config=config.output,
        target_crs="EPSG:6933",
        scale_m=30,
        fetch_bytes=lambda url, maximum_bytes: responses[url],
    )

    expected_rasters = {
        "rasters/hls_annual_reflectance.tif",
        "rasters/hls_annual_indices.tif",
        "rasters/hls_valid_observation_count.tif",
    }
    expected_figures = {
        "figures/rgb.png",
        "figures/valid_observations.png",
        "figures/indices_panel.png",
        *{f"figures/indices/{index.value}.png" for index in config.data.indices},
    }
    assert set(result.files) == expected_rasters | expected_figures
    assert all(result.files[path].startswith(b"\x89PNG") for path in expected_figures)
    assert len(result.validations) == 3
    assert len(result.estimates) == 3
    assert all("example.invalid" not in str(item) for item in result.metadata_payload().values())
    assert reflectance.unmask_value == -9999.0
    download_parameters = reflectance.download_parameters
    assert download_parameters is not None
    assert download_parameters == {
        "name": "hls_annual_reflectance",
        "bands": ["blue", "green", "red", "nir", "swir1", "swir2"],
        "region": download_parameters["region"],
        "scale": 30,
        "crs": "EPSG:6933",
        "format": "GEO_TIFF",
    }


@pytest.mark.parametrize(
    ("geometry", "target_crs", "expected_code"),
    [
        (Polygon(), "EPSG:6933", "aoi_invalid"),
        (Point(-63, -33), "EPSG:6933", "aoi_must_be_polygonal"),
        (box(-63, -33, -62.99, -32.99), "NO-ES-CRS", "target_crs_invalid"),
    ],
)
def test_download_estimate_rejects_invalid_geometry_or_crs(
    geometry: Any,
    target_crs: str,
    expected_code: str,
) -> None:
    with pytest.raises(RasterDownloadError, match=expected_code):
        estimate_direct_download(
            aoi_wgs84=geometry,
            target_crs=target_crs,
            scale_m=30,
            band_count=1,
            bytes_per_sample=4,
        )


@pytest.mark.parametrize(
    ("content", "expected_crs", "expected_scale", "band_names", "expected_code"),
    [
        (
            _geotiff_bytes(
                band_count=1,
                dtype="float32",
                values=np.ones((1, 2, 2), dtype=np.float32),
                crs="EPSG:4326",
            ),
            "EPSG:6933",
            30,
            ("red",),
            "geotiff_crs_mismatch",
        ),
        (
            _geotiff_bytes(
                band_count=1,
                dtype="float32",
                values=np.ones((1, 2, 2), dtype=np.float32),
            ),
            "EPSG:6933",
            30,
            ("red", "nir"),
            "geotiff_band_count_mismatch",
        ),
        (
            _geotiff_bytes(
                band_count=1,
                dtype="float32",
                values=np.ones((1, 2, 2), dtype=np.float32),
                transform_value=from_origin(-6_000_000, -3_000_000, 60, 60),
            ),
            "EPSG:6933",
            30,
            ("red",),
            "geotiff_resolution_mismatch",
        ),
        (
            b"no-es-un-tiff",
            "EPSG:6933",
            30,
            ("red",),
            "geotiff_unreadable",
        ),
    ],
)
def test_geotiff_contract_rejects_structural_mismatches(
    content: bytes,
    expected_crs: str,
    expected_scale: float,
    band_names: tuple[str, ...],
    expected_code: str,
) -> None:
    with pytest.raises(RasterDownloadError, match=expected_code):
        validate_geotiff_bytes(
            content,
            expected_crs=expected_crs,
            expected_scale_m=expected_scale,
            expected_band_names=band_names,
            nodata=-9999.0,
            artifact_path="rasters/invalid.tif",
        )


class _FailingDownloadImage(_DownloadImage):
    def __init__(self, message: str) -> None:
        super().__init__("failing")
        self.message = message

    def getDownloadURL(self, parameters: dict[str, object]) -> str:
        raise RuntimeError(self.message)


@pytest.mark.parametrize(
    ("remote_message", "expected_code"),
    [
        ("permission denied SECRET", "permission_denied"),
        ("quota exceeded", "quota_or_rate_limit"),
        ("request size too large", "remote_size_limit_exceeded"),
        ("deadline timeout", "timeout"),
        ("unexpected", "download_url_failed"),
    ],
)
def test_download_url_failures_are_classified_without_remote_text(
    remote_message: str,
    expected_code: str,
) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    product = HlsCompositeImages(
        reflectance=_FailingDownloadImage(remote_message),
        indices=_DownloadImage("indices"),
        valid_observation_count=_DownloadImage("count"),
        metadata=cast(Any, None),
    )

    with pytest.raises(RasterDownloadError) as captured:
        materialize_hls_raster_products(
            product=product,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            output_config=config.output,
            target_crs="EPSG:6933",
            scale_m=30,
            fetch_bytes=lambda url, maximum_bytes: b"",
        )

    assert captured.value.code == expected_code
    assert "SECRET" not in str(captured.value)
