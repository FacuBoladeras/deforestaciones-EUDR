"""Contrato del Paso 10 antes de descargar el primer raster real."""

from pathlib import Path

from deforestation_pipeline.catalog import BandRole, load_source_catalog
from deforestation_pipeline.config import OutputFormat, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_config_declares_the_annual_hls_raster_experiment() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    assert config.schema_version == "1.1.0"
    assert config.data.composition_interval == "annual"
    assert config.data.composition_reducer == "median"
    assert config.data.source_native_reflectance_scale_factor == 0.0001
    assert config.data.source_native_reflectance_offset == 0.0
    assert config.data.earth_engine_reflectance_multiplier == 1.0
    assert config.data.earth_engine_reflectance_offset == 0.0
    assert config.data.mask_high_aerosol is True
    assert config.data.preserve_water is True
    assert config.spatial.raster_crs_strategy == "local_utm"
    assert OutputFormat.GEOTIFF in config.output.formats
    assert config.output.raster_nodata == -9999.0
    assert config.output.maximum_direct_download_bytes == 32_000_000
    assert config.output.maximum_direct_download_dimension == 10_000
    assert config.output.rgb_min_reflectance < config.output.rgb_max_reflectance


def test_hls_catalog_exposes_every_band_needed_for_rgb_and_indices() -> None:
    catalog = load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml")
    by_product = {source.product: source for source in catalog.sources}

    assert by_product["HLSL30"].band_map == {
        BandRole.BLUE: "B2",
        BandRole.GREEN: "B3",
        BandRole.RED: "B4",
        BandRole.NIR: "B5",
        BandRole.SWIR1: "B6",
        BandRole.SWIR2: "B7",
        BandRole.QA: "Fmask",
    }
    assert by_product["HLSS30"].band_map == {
        BandRole.BLUE: "B2",
        BandRole.GREEN: "B3",
        BandRole.RED: "B4",
        BandRole.NIR: "B8A",
        BandRole.SWIR1: "B11",
        BandRole.SWIR2: "B12",
        BandRole.QA: "Fmask",
    }
