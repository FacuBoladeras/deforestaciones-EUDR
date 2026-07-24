"""Pruebas del catálogo local de fuentes y su compatibilidad científica."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from deforestation_pipeline.catalog import (
    BandRole,
    CatalogCompatibilityError,
    SourceAccessStatus,
    SourceCatalog,
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.yml"
CONFIG_PATH = PROJECT_ROOT / "configs" / "default.yml"


def test_hls_v2_catalog_records_both_constituent_products() -> None:
    catalog = load_source_catalog(CATALOG_PATH)

    assert catalog.schema_version == "1.0.0"
    assert tuple(source.product for source in catalog.sources) == ("HLSL30", "HLSS30")
    assert tuple(source.collection_id for source in catalog.sources) == (
        "NASA/HLS/HLSL30/v002",
        "NASA/HLS/HLSS30/v002",
    )
    assert all(source.family == "HLS" for source in catalog.sources)
    assert all(source.version == "2.0" for source in catalog.sources)
    assert all(source.spatial_resolution_m == 30 for source in catalog.sources)
    assert all(
        source.access_status is SourceAccessStatus.CATALOGED_NOT_ACCESSED
        for source in catalog.sources
    )
    assert all(source.catalog_checked_at == date(2026, 7, 24) for source in catalog.sources)


def test_hls_common_band_roles_are_mapped_per_product() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
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
    assert all(source.qa_mask_bits.cloud == 1 for source in catalog.sources)
    assert all(source.qa_mask_bits.cloud_shadow == 3 for source in catalog.sources)
    assert all(source.qa_mask_bits.snow_or_ice == 4 for source in catalog.sources)


def test_benchmark_plan_matches_resolved_configuration_without_remote_access() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
    config = load_config(CONFIG_PATH)

    plan = build_benchmark_source_plan(catalog, config)

    assert plan.sensor_family == "HLS"
    assert plan.target_resolution_m == 30
    assert tuple(source.product for source in plan.sources) == ("HLSL30", "HLSS30")
    assert plan.required_band_roles == (
        BandRole.BLUE,
        BandRole.GREEN,
        BandRole.RED,
        BandRole.NIR,
        BandRole.SWIR1,
        BandRole.SWIR2,
        BandRole.QA,
    )
    assert tuple(index.value for index in plan.requested_indices) == tuple(
        index.value for index in config.data.indices
    )
    assert plan.remote_data_accessed is False


def test_catalog_rejects_duplicate_source_and_collection_identifiers() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
    payload = catalog.model_dump(mode="python")
    payload["sources"][1]["source_id"] = payload["sources"][0]["source_id"]
    payload["sources"][1]["collection_id"] = payload["sources"][0]["collection_id"]

    with pytest.raises(ValueError, match=r"source_id.*collection_id"):
        SourceCatalog.model_validate(payload)


def test_benchmark_plan_rejects_missing_required_band_role(tmp_path: Path) -> None:
    payload = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["sources"][0]["bands"] = [
        binding for binding in payload["sources"][0]["bands"] if binding["role"] != "swir2"
    ]
    invalid_catalog_path = tmp_path / "catalog.yml"
    invalid_catalog_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    catalog = load_source_catalog(invalid_catalog_path)

    with pytest.raises(CatalogCompatibilityError, match=r"HLSL30.*swir2"):
        build_benchmark_source_plan(catalog, load_config(CONFIG_PATH))
