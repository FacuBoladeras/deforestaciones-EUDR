"""Descarga, validación y render local del primer producto raster."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import Affine, from_origin
from shapely.geometry import Point, Polygon, box

from deforestation_pipeline.config import load_config
from deforestation_pipeline.hls_composite import HlsCompositeImages
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import (
    RasterDownloadError,
    estimate_direct_download,
    estimate_fixed_grid_download,
    materialize_ee_image_to_grid,
    materialize_hls_raster_products,
    validate_direct_download,
    validate_geotiff_bytes,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_AOI = box(-63.0, -33.0, -62.999, -32.999)
TEST_GRID = derive_raster_grid_spec(
    aoi_wgs84=TEST_AOI,
    target_crs="EPSG:32720",
    resolution_m=30,
    nodata=-9999,
)
EQUAL_AREA_TEST_GRID = derive_raster_grid_spec(
    aoi_wgs84=TEST_AOI,
    target_crs="EPSG:6933",
    resolution_m=10,
    nodata=-9999,
)


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


def test_fixed_grid_estimate_uses_exact_contract_dimensions() -> None:
    estimate = estimate_fixed_grid_download(
        grid_spec=TEST_GRID,
        band_count=6,
        bytes_per_sample=4,
    )

    assert estimate.target_crs == TEST_GRID.target_crs
    assert estimate.width == TEST_GRID.width
    assert estimate.height == TEST_GRID.height
    assert estimate.grid_sha256 == TEST_GRID.grid_sha256
    assert estimate.estimated_uncompressed_bytes == (TEST_GRID.width * TEST_GRID.height * 6 * 4)


class _DownloadImage:
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        self.download_parameters: dict[str, object] | None = None
        self.unmask_value: float | None = None
        self.unmask_same_footprint: bool | None = None
        self.operations: list[str] = []

    def unmask(self, value: float, same_footprint: bool) -> _DownloadImage:
        self.unmask_value = value
        self.unmask_same_footprint = same_footprint
        self.operations.append(f"unmask:{value}:{same_footprint}")
        return self

    def toFloat(self) -> _DownloadImage:
        self.operations.append("toFloat")
        return self

    def toInt16(self) -> _DownloadImage:
        self.operations.append("toInt16")
        return self

    def getDownloadURL(self, parameters: dict[str, object]) -> str:
        self.download_parameters = parameters
        return f"https://example.invalid/{self.identifier}"


class _EarthEngineProjectionCheckingImage(_DownloadImage):
    """Reproduce el rechazo real de EE al alias EPSG:6933."""

    def getDownloadURL(self, parameters: dict[str, object]) -> str:
        self.download_parameters = parameters
        if parameters["crs"] == "EPSG:6933":
            raise RuntimeError("Projection: The CRS of a map projection could not be parsed.")
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


def test_materializes_generic_ee_image_on_exact_grid() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    image = _DownloadImage("baseline-features")
    content = _geotiff_bytes(
        band_count=2,
        dtype="float32",
        values=np.full((2, TEST_GRID.height, TEST_GRID.width), 0.5, dtype=np.float32),
        crs=TEST_GRID.target_crs,
        transform_value=Affine(*TEST_GRID.transform),
    )

    result = materialize_ee_image_to_grid(
        image=image,
        band_names=("forest_feature_a", "forest_feature_b"),
        artifact_path="tiffs/evidence/forest_features_2020.tif",
        download_name="forest_features_2020",
        output_config=config.output,
        grid_spec=TEST_GRID,
        output_type="float32",
        fetch_bytes=lambda url, maximum_bytes: content,
    )

    assert result.validation.band_names == ("forest_feature_a", "forest_feature_b")
    assert result.validation.grid_sha256 == TEST_GRID.grid_sha256
    assert result.estimate.band_count == 2
    assert image.unmask_value == -9999.0
    assert image.unmask_same_footprint is False
    assert image.download_parameters == {
        "name": "forest_features_2020",
        "bands": ["forest_feature_a", "forest_feature_b"],
        "crs": TEST_GRID.target_crs,
        "crs_transform": list(TEST_GRID.transform),
        "dimensions": [TEST_GRID.width, TEST_GRID.height],
        "format": "GEO_TIFF",
    }


def test_equal_area_epsg_6933_is_sent_as_gee_parseable_wkt() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    image = _EarthEngineProjectionCheckingImage("dynamic-world")
    content = _geotiff_bytes(
        band_count=1,
        dtype="float32",
        values=np.ones(
            (1, EQUAL_AREA_TEST_GRID.height, EQUAL_AREA_TEST_GRID.width), dtype=np.float32
        ),
        crs=EQUAL_AREA_TEST_GRID.target_crs,
        transform_value=Affine(*EQUAL_AREA_TEST_GRID.transform),
    )

    materialize_ee_image_to_grid(
        image=image,
        band_names=("crop",),
        artifact_path="tiffs/evidence/agricultural/event/2021-09_dynamic_world.tif",
        download_name="dynamic_world_event_2021-09",
        output_config=config.output,
        grid_spec=EQUAL_AREA_TEST_GRID,
        output_type="float32",
        fetch_bytes=lambda _url, _maximum_bytes: content,
    )

    assert image.download_parameters is not None
    requested_crs = image.download_parameters["crs"]
    assert isinstance(requested_crs, str)
    assert requested_crs.startswith("PROJCS[")
    assert 'AUTHORITY["EPSG","6933"]' in requested_crs


def test_downloaded_geotiff_is_normalized_and_scientifically_validated() -> None:
    values = np.full((2, TEST_GRID.height, TEST_GRID.width), 0.1, dtype=np.float32)
    values[0, 0, 0] = 0.4
    values[1, 0, 0] = -0.5
    values[1, -1, -1] = 1.0
    content = _geotiff_bytes(
        band_count=2,
        dtype="float32",
        values=values,
        crs=TEST_GRID.target_crs,
        transform_value=Affine(*TEST_GRID.transform),
    )

    normalized, validation = validate_geotiff_bytes(
        content,
        expected_grid=TEST_GRID,
        expected_band_names=("red", "NDVI"),
        artifact_path="tiffs/test.tif",
    )

    assert normalized.startswith((b"II*\x00", b"MM\x00*"))
    assert validation.crs == TEST_GRID.target_crs
    assert validation.grid_sha256 == TEST_GRID.grid_sha256
    assert validation.transform == pytest.approx(TEST_GRID.transform)
    assert validation.width == TEST_GRID.width
    assert validation.height == TEST_GRID.height
    assert validation.band_names == ("red", "NDVI")
    assert validation.resolution_m == (30.0, 30.0)
    assert validation.minimum_by_band == pytest.approx((0.1, -0.5))
    assert validation.maximum_by_band == pytest.approx((0.4, 1.0))
    assert validation.valid_pixel_count_by_band == (
        TEST_GRID.width * TEST_GRID.height,
        TEST_GRID.width * TEST_GRID.height,
    )
    assert validation.all_nodata_band_names == ()


def test_strict_product_rejects_all_nodata_with_auditable_context() -> None:
    values = np.full(
        (1, TEST_GRID.height, TEST_GRID.width),
        TEST_GRID.nodata,
        dtype=np.float32,
    )
    content = _geotiff_bytes(
        band_count=1,
        dtype="float32",
        values=values,
        crs=TEST_GRID.target_crs,
        transform_value=Affine(*TEST_GRID.transform),
    )

    with pytest.raises(RasterDownloadError) as captured:
        validate_geotiff_bytes(
            content,
            expected_grid=TEST_GRID,
            expected_band_names=("red",),
            artifact_path="tiffs/hls_annual_reflectance.tif",
            product="hls_annual_reflectance",
        )

    assert captured.value.code == "geotiff_band_has_no_valid_pixels"
    assert captured.value.artifact_path == "tiffs/hls_annual_reflectance.tif"
    assert captured.value.product == "hls_annual_reflectance"
    assert captured.value.band_index == 0
    assert "artifact_path=tiffs/hls_annual_reflectance.tif" in str(captured.value)
    assert "product=hls_annual_reflectance" in str(captured.value)
    assert "band_index=0" in str(captured.value)


def test_period_materialization_accepts_empty_spectral_bands_and_zero_counts() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    product = HlsCompositeImages(
        reflectance=_DownloadImage("reflectance"),
        indices=_DownloadImage("indices"),
        valid_observation_count=_DownloadImage("count"),
        valid_observation_count_l30=_DownloadImage("count_l30"),
        valid_observation_count_s30=_DownloadImage("count_s30"),
        metadata=cast(Any, None),
    )
    height, width = TEST_GRID.height, TEST_GRID.width
    all_nodata_reflectance = np.full(
        (6, height, width),
        TEST_GRID.nodata,
        dtype=np.float32,
    )
    all_nodata_indices = np.full(
        (8, height, width),
        TEST_GRID.nodata,
        dtype=np.float32,
    )
    zero_count_with_nodata_exterior = np.zeros((1, height, width), dtype=np.int16)
    zero_count_with_nodata_exterior[0, 0, 0] = int(TEST_GRID.nodata)
    responses = {
        "https://example.invalid/reflectance": _geotiff_bytes(
            band_count=6,
            dtype="float32",
            values=all_nodata_reflectance,
            crs=TEST_GRID.target_crs,
            transform_value=Affine(*TEST_GRID.transform),
        ),
        "https://example.invalid/indices": _geotiff_bytes(
            band_count=8,
            dtype="float32",
            values=all_nodata_indices,
            crs=TEST_GRID.target_crs,
            transform_value=Affine(*TEST_GRID.transform),
        ),
        **{
            f"https://example.invalid/{name}": _geotiff_bytes(
                band_count=1,
                dtype="int16",
                values=zero_count_with_nodata_exterior,
                crs=TEST_GRID.target_crs,
                transform_value=Affine(*TEST_GRID.transform),
            )
            for name in ("count", "count_l30", "count_s30")
        },
    }

    result = materialize_hls_raster_products(
        product=product,
        aoi_wgs84=TEST_AOI,
        output_config=config.output,
        grid_spec=TEST_GRID,
        fetch_bytes=lambda url, maximum_bytes: responses[url],
        artifact_kind="period",
    )

    reflectance = next(
        item
        for item in result.validations
        if item.artifact_path == "tiffs/hls_period_reflectance.tif"
    )
    indices = next(
        item for item in result.validations if item.artifact_path == "tiffs/hls_period_indices.tif"
    )
    assert reflectance.valid_pixel_count_by_band == (0,) * 6
    assert reflectance.minimum_by_band == (None,) * 6
    assert reflectance.all_nodata_band_names == (
        "blue",
        "green",
        "red",
        "nir",
        "swir1",
        "swir2",
    )
    assert indices.valid_pixel_count_by_band == (0,) * 8
    assert len(indices.all_nodata_band_names) == 8
    total_count = next(
        item
        for item in result.validations
        if item.artifact_path == "tiffs/hls_valid_observation_count.tif"
    )
    assert total_count.minimum_by_band == (0.0,)
    assert total_count.maximum_by_band == (0.0,)
    assert total_count.valid_pixel_count_by_band == (height * width - 1,)
    assert total_count.all_nodata_band_names == ()
    with MemoryFile(result.files["tiffs/hls_valid_observation_count.tif"]) as memory:
        with memory.open() as dataset:
            values = dataset.read()[0]
            assert values[0, 0] == TEST_GRID.nodata
            assert np.all(values[1:] == 0)


def test_annual_materialization_does_not_publish_empty_required_composite() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    product = HlsCompositeImages(
        reflectance=_DownloadImage("reflectance"),
        indices=_DownloadImage("indices"),
        valid_observation_count=_DownloadImage("count"),
        valid_observation_count_l30=_DownloadImage("count_l30"),
        valid_observation_count_s30=_DownloadImage("count_s30"),
        metadata=cast(Any, None),
    )
    content = _geotiff_bytes(
        band_count=6,
        dtype="float32",
        values=np.full(
            (6, TEST_GRID.height, TEST_GRID.width),
            TEST_GRID.nodata,
            dtype=np.float32,
        ),
        crs=TEST_GRID.target_crs,
        transform_value=Affine(*TEST_GRID.transform),
    )

    with pytest.raises(RasterDownloadError) as captured:
        materialize_hls_raster_products(
            product=product,
            aoi_wgs84=TEST_AOI,
            output_config=config.output,
            grid_spec=TEST_GRID,
            fetch_bytes=lambda url, maximum_bytes: content,
            artifact_kind="annual",
        )

    assert captured.value.code == "geotiff_band_has_no_valid_pixels"
    assert captured.value.artifact_path == "tiffs/hls_annual_reflectance.tif"
    assert captured.value.product == "hls_annual_reflectance"
    assert captured.value.band_index == 0


def test_period_policy_rejects_a_single_empty_band_in_otherwise_valid_product() -> None:
    values = np.full(
        (2, TEST_GRID.height, TEST_GRID.width),
        0.25,
        dtype=np.float32,
    )
    values[1] = TEST_GRID.nodata
    content = _geotiff_bytes(
        band_count=2,
        dtype="float32",
        values=values,
        crs=TEST_GRID.target_crs,
        transform_value=Affine(*TEST_GRID.transform),
    )

    with pytest.raises(RasterDownloadError) as captured:
        validate_geotiff_bytes(
            content,
            expected_grid=TEST_GRID,
            expected_band_names=("red", "nir"),
            artifact_path="tiffs/hls_period_reflectance.tif",
            product="hls_period_reflectance",
            all_nodata_band_policy="allow_for_missing_period",
        )

    assert captured.value.code == "geotiff_band_has_no_valid_pixels"
    assert captured.value.band_index == 1


@pytest.mark.parametrize("artifact_kind", ["annual", "period"])
def test_materialization_returns_geotiffs_and_local_pngs_without_signed_urls(
    artifact_kind: Literal["annual", "period"],
) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    reflectance = _DownloadImage("reflectance")
    indices = _DownloadImage("indices")
    count = _DownloadImage("count")
    count_l30 = _DownloadImage("count_l30")
    count_s30 = _DownloadImage("count_s30")
    product = HlsCompositeImages(
        reflectance=reflectance,
        indices=indices,
        valid_observation_count=count,
        valid_observation_count_l30=count_l30,
        valid_observation_count_s30=count_s30,
        metadata=cast(Any, None),
    )
    height = TEST_GRID.height
    width = TEST_GRID.width
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
            crs=TEST_GRID.target_crs,
            transform_value=Affine(*TEST_GRID.transform),
        ),
        "https://example.invalid/indices": _geotiff_bytes(
            band_count=8,
            dtype="float32",
            values=index_values,
            crs=TEST_GRID.target_crs,
            transform_value=Affine(*TEST_GRID.transform),
        ),
        "https://example.invalid/count": _geotiff_bytes(
            band_count=1,
            dtype="int16",
            values=count_values,
            crs=TEST_GRID.target_crs,
            transform_value=Affine(*TEST_GRID.transform),
        ),
        "https://example.invalid/count_l30": _geotiff_bytes(
            band_count=1,
            dtype="int16",
            values=np.full((1, height, width), 7, dtype=np.int16),
            crs=TEST_GRID.target_crs,
            transform_value=Affine(*TEST_GRID.transform),
        ),
        "https://example.invalid/count_s30": _geotiff_bytes(
            band_count=1,
            dtype="int16",
            values=np.full((1, height, width), 5, dtype=np.int16),
            crs=TEST_GRID.target_crs,
            transform_value=Affine(*TEST_GRID.transform),
        ),
    }

    result = materialize_hls_raster_products(
        product=product,
        aoi_wgs84=TEST_AOI,
        output_config=config.output,
        grid_spec=TEST_GRID,
        fetch_bytes=lambda url, maximum_bytes: responses[url],
        artifact_kind=artifact_kind,
    )

    expected_rasters = {
        f"tiffs/hls_{artifact_kind}_reflectance.tif",
        f"tiffs/hls_{artifact_kind}_indices.tif",
        "tiffs/hls_valid_observation_count.tif",
        "tiffs/hls_valid_observation_count_l30.tif",
        "tiffs/hls_valid_observation_count_s30.tif",
    }
    expected_figures = {
        "figures/rgb.png",
        "figures/valid_observations.png",
        "figures/indices_panel.png",
        *{f"figures/index_{index.value}.png" for index in config.data.indices},
    }
    assert set(result.files) == expected_rasters | expected_figures
    assert all(result.files[path].startswith(b"\x89PNG") for path in expected_figures)
    assert len(result.validations) == 5
    assert len(result.estimates) == 5
    assert result.grid_spec == TEST_GRID
    assert all(item.grid_sha256 == TEST_GRID.grid_sha256 for item in result.validations)
    visualization_sources = {item.source_raster for item in result.visualizations}
    assert f"tiffs/hls_{artifact_kind}_reflectance.tif" in visualization_sources
    assert f"tiffs/hls_{artifact_kind}_indices.tif" in visualization_sources
    expected_label = "anual" if artifact_kind == "annual" else "del período"
    rgb_record = next(item for item in result.visualizations if item.path == "figures/rgb.png")
    panel_record = next(
        item for item in result.visualizations if item.path == "figures/indices_panel.png"
    )
    assert expected_label in rgb_record.title
    assert expected_label in panel_record.title
    assert all("example.invalid" not in str(item) for item in result.metadata_payload().values())
    assert reflectance.unmask_value == -9999.0
    assert count.operations.index("toInt16") < count.operations.index("unmask:-9999.0:False")
    download_parameters = reflectance.download_parameters
    assert download_parameters is not None
    assert download_parameters == {
        "name": f"hls_{artifact_kind}_reflectance",
        "bands": ["blue", "green", "red", "nir", "swir1", "swir2"],
        "crs": TEST_GRID.target_crs,
        "crs_transform": list(TEST_GRID.transform),
        "dimensions": [TEST_GRID.width, TEST_GRID.height],
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
    ("content", "band_names", "expected_code"),
    [
        (
            _geotiff_bytes(
                band_count=1,
                dtype="float32",
                values=np.ones(
                    (1, TEST_GRID.height, TEST_GRID.width),
                    dtype=np.float32,
                ),
                crs="EPSG:4326",
                transform_value=Affine(*TEST_GRID.transform),
            ),
            ("red",),
            "geotiff_crs_mismatch",
        ),
        (
            _geotiff_bytes(
                band_count=1,
                dtype="float32",
                values=np.ones(
                    (1, TEST_GRID.height, TEST_GRID.width),
                    dtype=np.float32,
                ),
                crs=TEST_GRID.target_crs,
                transform_value=Affine(*TEST_GRID.transform),
            ),
            ("red", "nir"),
            "geotiff_band_count_mismatch",
        ),
        (
            _geotiff_bytes(
                band_count=1,
                dtype="float32",
                values=np.ones(
                    (1, TEST_GRID.height, TEST_GRID.width),
                    dtype=np.float32,
                ),
                crs=TEST_GRID.target_crs,
                transform_value=Affine(
                    TEST_GRID.resolution_m,
                    0,
                    TEST_GRID.transform[2] + TEST_GRID.resolution_m,
                    0,
                    -TEST_GRID.resolution_m,
                    TEST_GRID.transform[5],
                ),
            ),
            ("red",),
            "geotiff_grid_transform_mismatch",
        ),
        (
            _geotiff_bytes(
                band_count=1,
                dtype="float32",
                values=np.ones(
                    (1, TEST_GRID.height + 1, TEST_GRID.width),
                    dtype=np.float32,
                ),
                crs=TEST_GRID.target_crs,
                transform_value=Affine(*TEST_GRID.transform),
            ),
            ("red",),
            "geotiff_grid_dimensions_mismatch",
        ),
        (
            b"no-es-un-tiff",
            ("red",),
            "geotiff_unreadable",
        ),
    ],
)
def test_geotiff_contract_rejects_structural_mismatches(
    content: bytes,
    band_names: tuple[str, ...],
    expected_code: str,
) -> None:
    with pytest.raises(RasterDownloadError, match=expected_code):
        validate_geotiff_bytes(
            content,
            expected_grid=TEST_GRID,
            expected_band_names=band_names,
            artifact_path="tiffs/invalid.tif",
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
        (
            "Projection: The CRS of a map projection could not be parsed.",
            "remote_projection_invalid",
        ),
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
        valid_observation_count_l30=_DownloadImage("count_l30"),
        valid_observation_count_s30=_DownloadImage("count_s30"),
        metadata=cast(Any, None),
    )

    with pytest.raises(RasterDownloadError) as captured:
        materialize_hls_raster_products(
            product=product,
            aoi_wgs84=TEST_AOI,
            output_config=config.output,
            grid_spec=TEST_GRID,
            fetch_bytes=lambda url, maximum_bytes: b"",
        )

    assert captured.value.code == expected_code
    assert captured.value.artifact_path == "tiffs/hls_annual_reflectance.tif"
    assert captured.value.product == "hls_annual_reflectance"
    assert captured.value.band_index is None
    assert "band_index=<not_applicable>" in str(captured.value)
    assert "SECRET" not in str(captured.value)
