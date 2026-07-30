"""Contratos metodológicos de la línea base forestal del Paso 13.0."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from deforestation_pipeline.artifact_layout import (
    evidence_figure,
    evidence_json,
    evidence_tiff,
)
from deforestation_pipeline.config import (
    PipelineConfig,
    load_config,
    resolve_run_config,
    scientific_parameters_hash,
)
from deforestation_pipeline.forest_baseline import (
    ForestBaselineConfig,
    forest_baseline_json_schema,
    forest_baseline_output_paths,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_config_declares_auditable_forest_baseline_contract() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    assert config.schema_version == "1.8.0"
    assert config.forest_baseline.reference_date == date(2020, 12, 31)
    assert config.forest_baseline.feature_end_date_exclusive == date(2021, 1, 1)
    assert config.forest_baseline.benchmark_resolution_m == 30
    assert config.forest_baseline.minimum_independent_sources == 2
    assert config.forest_baseline.preserve_source_evidence is True
    assert config.forest_baseline.preserve_disagreement is True
    assert config.forest_baseline.post_cutoff_observations_allowed is False
    assert config.forest_baseline.long_gap_interpolation_allowed is False


def test_baseline_rejects_temporal_leakage_after_cutoff() -> None:
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_baseline.model_dump()
    payload["feature_end_date_exclusive"] = date(2021, 1, 2)

    with pytest.raises(ValidationError, match="feature_end_date_exclusive debe ser 2021-01-01"):
        ForestBaselineConfig.model_validate(payload)


def test_baseline_rejects_feature_period_without_history_before_cutoff() -> None:
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_baseline.model_dump()
    payload["feature_start_date"] = date(2021, 1, 1)

    with pytest.raises(ValidationError, match="feature_start_date debe ser anterior"):
        ForestBaselineConfig.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("minimum_area_ha", 0.6, "minimum_area_ha debe ser 0.5"),
        ("minimum_tree_height_m", 4.0, "minimum_tree_height_m debe ser 5"),
        (
            "minimum_canopy_cover_percent",
            15.0,
            "minimum_canopy_cover_percent debe ser 10",
        ),
    ],
)
def test_forest_definition_thresholds_are_domain_constants(
    field: str,
    value: float,
    message: str,
) -> None:
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_baseline.model_dump()
    payload["forest_definition"][field] = value

    with pytest.raises(ValidationError, match=message):
        ForestBaselineConfig.model_validate(payload)


def test_baseline_requires_multiple_independent_sources() -> None:
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").forest_baseline.model_dump()
    payload["minimum_independent_sources"] = 1

    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        ForestBaselineConfig.model_validate(payload)


def test_baseline_parameters_participate_in_scientific_identity() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    baseline = resolve_run_config(config, date(2024, 12, 31))
    payload = baseline.model_dump()
    payload["forest_baseline"]["feature_start_date"] = date(2018, 1, 1)
    changed = type(baseline).model_validate(payload)

    assert scientific_parameters_hash(changed) != scientific_parameters_hash(baseline)


def test_forest_baseline_outputs_reuse_one_flat_evidence_domain_per_type() -> None:
    paths = forest_baseline_output_paths()

    assert paths == {
        "metadata": "json/evidence/forest_baseline_2020.json",
        "qa_figure": "figures/evidence/forest_baseline_2020.png",
        "evidence_fraction": "tiffs/evidence/forest_evidence_fraction_2020.tif",
        "consensus": "tiffs/evidence/forest_consensus_2020.tif",
        "disagreement": "tiffs/evidence/forest_disagreement_2020.tif",
        "source_count": "tiffs/evidence/forest_source_count_2020.tif",
        "source_evidence": "tiffs/evidence/forest_source_evidence_2020.tif",
        "features": "tiffs/evidence/forest_features_2020.tif",
    }
    assert evidence_json("forest_baseline_2020.json") == paths["metadata"]
    assert evidence_figure("forest_baseline_2020.png") == paths["qa_figure"]
    assert evidence_tiff("forest_evidence_fraction_2020.tif") == paths["evidence_fraction"]
    assert all(len(Path(path).parts) == 3 for path in paths.values())


def test_committed_forest_baseline_schema_matches_model() -> None:
    path = PROJECT_ROOT / "data" / "schemas" / "forest-baseline-v1.0.0.json"
    committed = json.loads(path.read_text(encoding="utf-8"))

    assert committed == forest_baseline_json_schema()


def test_pipeline_rejects_unknown_or_missing_baseline_contract() -> None:
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload.pop("forest_baseline")

    with pytest.raises(ValidationError, match="forest_baseline"):
        PipelineConfig.model_validate(payload)
