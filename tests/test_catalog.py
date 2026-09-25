"""Pruebas del catálogo local de fuentes y su compatibilidad científica."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from deforestation_pipeline.catalog import (
    AgriculturalCatalogSource,
    BandRole,
    CatalogCompatibilityError,
    ReferenceLabelCatalogSource,
    SourceAccessStatus,
    SourceCatalog,
    build_agricultural_evidence_source_plan,
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.yml"
CONFIG_PATH = PROJECT_ROOT / "configs" / "default.yml"


def test_hls_v2_catalog_records_both_constituent_products() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
    hls_sources = tuple(source for source in catalog.sources if source.family == "HLS")

    assert catalog.schema_version == "2.1.0"
    assert tuple(source.product for source in hls_sources) == ("HLSL30", "HLSS30")
    assert tuple(source.collection_id for source in hls_sources) == (
        "NASA/HLS/HLSL30/v002",
        "NASA/HLS/HLSS30/v002",
    )
    assert all(source.version == "2.0" for source in hls_sources)
    assert all(source.spatial_resolution_m == 30 for source in hls_sources)
    assert all(
        source.access_status is SourceAccessStatus.CATALOGED_NOT_ACCESSED for source in hls_sources
    )
    assert all(source.catalog_checked_at == date(2026, 7, 24) for source in hls_sources)


def test_mapbiomas_reference_label_is_parsed_without_entering_baseline_consensus() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
    source = next(
        source for source in catalog.sources if isinstance(source, ReferenceLabelCatalogSource)
    )

    assert source.source_id == "mapbiomas_argentina_collection2"
    assert source.p0_reference_band == "classification_2020"
    assert source.forest_values == (3, 4, 6)
    assert source.eligible_for_core_consensus is False


def test_dynamic_world_is_cataloged_as_temporal_crop_candidate_with_conservative_classes() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
    source = next(
        source for source in catalog.sources if isinstance(source, AgriculturalCatalogSource)
    )

    assert source.source_id == "dynamic_world_v1"
    assert source.collection_id == "GOOGLE/DYNAMICWORLD/V1"
    assert source.spatial_resolution_m == 10
    assert source.availability_start == date(2015, 6, 27)
    assert source.availability_end is None
    assert source.license == "CC-BY-4.0"
    by_land_use = {rule.output_land_use: rule for rule in source.class_rules}
    assert by_land_use["crop"].source_probability_band == "crops"
    assert by_land_use["crop"].source_label_value == 4
    assert by_land_use["crop"].automatic_gate_eligible is True
    assert by_land_use["other"].automatic_gate_eligible is False
    assert source.sensor_family == "sentinel2"


def test_agricultural_source_plan_preserves_candidate_and_corroboration_roles() -> None:
    plan = build_agricultural_evidence_source_plan(load_source_catalog(CATALOG_PATH))

    assert plan.candidate_source.source_id == "dynamic_world_v1"
    assert plan.corroborative_sources[0].source_id == "mapbiomas_argentina_collection2"
    assert plan.target_resolution_m == 10
    assert plan.remote_data_accessed is False


def test_hls_common_band_roles_are_mapped_per_product() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
    hls_sources = tuple(source for source in catalog.sources if source.family == "HLS")
    by_product = {source.product: source for source in hls_sources}

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
    assert all(source.qa_mask_bits.cloud == 1 for source in hls_sources)
    assert all(source.qa_mask_bits.cloud_shadow == 3 for source in hls_sources)
    assert all(source.qa_mask_bits.snow_or_ice == 4 for source in hls_sources)


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
