"""Selección auditable de fuentes para la línea base forestal 2020."""

from __future__ import annotations

from pathlib import Path

import pytest

from deforestation_pipeline.catalog import (
    CatalogCompatibilityError,
    ForestEvidenceRole,
    ForestRuleKind,
    build_forest_baseline_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.yml"
CONFIG_PATH = PROJECT_ROOT / "configs" / "default.yml"


def test_catalog_declares_verified_forest_sources_and_roles() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
    plan = build_forest_baseline_source_plan(catalog, load_config(CONFIG_PATH))

    assert catalog.schema_version == "2.1.0"
    assert tuple(source.source_id for source in plan.core_sources) == (
        "jrc_gfc2020_v3",
        "esa_worldcover_2020_v100",
    )
    assert tuple(source.source_id for source in plan.supporting_sources) == (
        "hansen_gfc_2025_v1_13",
    )
    assert tuple(source.evidence_role for source in plan.core_sources) == (
        ForestEvidenceRole.CORE_FOREST_MAP,
        ForestEvidenceRole.CORE_LAND_COVER,
    )
    assert len({source.independence_group for source in plan.core_sources}) == 2
    assert plan.remote_data_accessed is False


def test_jrc_and_worldcover_use_explicit_categorical_rules() -> None:
    plan = build_forest_baseline_source_plan(
        load_source_catalog(CATALOG_PATH),
        load_config(CONFIG_PATH),
    )
    by_id = {source.source_id: source for source in plan.sources}

    assert by_id["jrc_gfc2020_v3"].rule.kind is ForestRuleKind.CATEGORICAL_VALUES
    assert by_id["jrc_gfc2020_v3"].rule.source_band == "Map"
    assert by_id["jrc_gfc2020_v3"].rule.forest_values == (1,)
    assert by_id["esa_worldcover_2020_v100"].rule.forest_values == (10, 95)


def test_hansen_is_supporting_evidence_not_a_core_2020_truth_map() -> None:
    plan = build_forest_baseline_source_plan(
        load_source_catalog(CATALOG_PATH),
        load_config(CONFIG_PATH),
    )
    hansen = plan.supporting_sources[0]

    assert hansen.rule.kind is ForestRuleKind.HANSEN_2020_RECONSTRUCTION
    assert hansen.rule.tree_cover_band == "treecover2000"
    assert hansen.rule.loss_year_band == "lossyear"
    assert hansen.rule.minimum_tree_cover_percent == 10
    assert hansen.rule.loss_year_cutoff_code == 20
    assert hansen.eligible_for_core_consensus is False
    assert any("regeneración" in limitation.lower() for limitation in hansen.limitations)


def test_forest_plan_rejects_non_independent_core_sources() -> None:
    catalog = load_source_catalog(CATALOG_PATH)
    payload = catalog.model_dump(mode="python")
    forest_sources = [
        source for source in payload["sources"] if source["family"] == "FOREST_BASELINE"
    ]
    forest_sources[1]["independence_group"] = forest_sources[0]["independence_group"]
    modified = type(catalog).model_validate(payload)

    with pytest.raises(CatalogCompatibilityError, match="independientes"):
        build_forest_baseline_source_plan(modified, load_config(CONFIG_PATH))


def test_mapbiomas_is_not_silently_used_without_stable_asset_and_temporal_audit() -> None:
    plan = build_forest_baseline_source_plan(
        load_source_catalog(CATALOG_PATH),
        load_config(CONFIG_PATH),
    )

    assert all("mapbiomas" not in source.source_id for source in plan.sources)
    assert "mapbiomas_chaco_deferred" in plan.deferred_source_ids
