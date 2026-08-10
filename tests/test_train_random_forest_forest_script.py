from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "train_random_forest_forest.py"
SPEC = importlib.util.spec_from_file_location("train_random_forest_forest", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _manifest(features: list[str]) -> dict[str, Any]:
    payload = json.dumps(features, ensure_ascii=False, separators=(",", ":"))
    return {
        "canonical_feature_schema": {
            "predictor_columns": features,
            "predictor_columns_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }
    }


def test_manifest_contract_preserves_exact_feature_order_and_hash(tmp_path: Path) -> None:
    features = [f"f{i}" for i in range(56)]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest(features)), encoding="utf-8")

    contract = MODULE.load_feature_contract(path)

    assert contract.predictor_columns == tuple(features)
    assert (
        contract.predictor_columns_sha256
        == hashlib.sha256(
            json.dumps(features, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_manifest_contract_rejects_tampered_hash(tmp_path: Path) -> None:
    features = [f"f{i}" for i in range(56)]
    manifest = _manifest(features)
    manifest["canonical_feature_schema"]["predictor_columns_sha256"] = "0" * 64
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="hash"):
        MODULE.load_feature_contract(path)


def test_validate_dataset_rejects_sample_or_block_split_leakage() -> None:
    features = [f"f{i}" for i in range(56)]
    frame = pd.DataFrame(
        {
            **{feature: [0.1, 0.2, 0.3] for feature in features},
            "proxy_label": ["forest", "forest", "non_forest"],
            "split": ["train", "validation", "test"],
            "sample_id": ["same", "same", "other"],
            "block_id": ["a", "b", "c"],
            "sample_year": [2020, 2021, 2020],
        }
    )

    with pytest.raises(ValueError, match="sample_id"):
        MODULE.validate_dataset(frame, tuple(features))

    frame.loc[1, "sample_id"] = "new"
    frame.loc[1, "block_id"] = "a"
    with pytest.raises(ValueError, match="block_id"):
        MODULE.validate_dataset(frame, tuple(features))


def test_inverse_site_frequency_weights_equalize_each_site() -> None:
    sample_ids = pd.Series(["a", "a", "a", "b"], index=[10, 11, 12, 13])

    weights = MODULE.inverse_site_frequency_weights(sample_ids)

    assert weights.index.tolist() == [10, 11, 12, 13]
    assert weights.tolist() == pytest.approx([1 / 3, 1 / 3, 1 / 3, 1.0])
    assert weights.groupby(sample_ids).sum().to_dict() == pytest.approx({"a": 1.0, "b": 1.0})


def test_select_candidate_uses_validation_only_and_deterministic_tie_break() -> None:
    candidates = {
        "uniform": {
            "validation": {
                "balanced_accuracy": 0.90,
                "f1_forest": 0.91,
                "worst_year_f1_forest": 0.88,
            }
        },
        "inverse_site_frequency": {
            "validation": {
                "balanced_accuracy": 0.90,
                "f1_forest": 0.91,
                "worst_year_f1_forest": 0.88,
            }
        },
    }
    baseline = {
        "validation": {
            "balanced_accuracy": 0.89,
            "worst_year_f1_forest": 0.87,
        }
    }

    result = MODULE.select_candidate(candidates, baseline, max_regression=0.02)

    assert result["selected_variant"] == "inverse_site_frequency"
    assert result["selection_used_test"] is False
    assert result["gate_passed"] is True


def test_select_candidate_blocks_export_when_all_regress_materially() -> None:
    candidates = {
        "uniform": {
            "validation": {
                "balanced_accuracy": 0.80,
                "f1_forest": 0.82,
                "worst_year_f1_forest": 0.70,
            }
        },
        "inverse_site_frequency": {
            "validation": {
                "balanced_accuracy": 0.81,
                "f1_forest": 0.83,
                "worst_year_f1_forest": 0.71,
            }
        },
    }
    baseline = {
        "validation": {
            "balanced_accuracy": 0.90,
            "worst_year_f1_forest": 0.88,
        }
    }

    result = MODULE.select_candidate(candidates, baseline, max_regression=0.02)

    assert result["selected_variant"] is None
    assert result["gate_passed"] is False


def test_site_level_evaluation_requires_consistent_label() -> None:
    rows = pd.DataFrame(
        {
            "sample_id": ["a", "a"],
            "true_code": [0, 1],
            "forest_vote_fraction": [0.2, 0.8],
        }
    )

    with pytest.raises(ValueError, match="inconsistentes"):
        MODULE.site_level_predictions(rows)


def test_feature_matrix_must_be_finite() -> None:
    features = [f"f{i}" for i in range(56)]
    data = {feature: [0.1, 0.2, 0.3] for feature in features}
    data["f4"][1] = np.inf
    frame = pd.DataFrame(
        {
            **data,
            "proxy_label": ["forest", "forest", "non_forest"],
            "split": ["train", "validation", "test"],
            "sample_id": ["a", "b", "c"],
            "block_id": ["a", "b", "c"],
            "sample_year": [2020, 2020, 2020],
        }
    )

    with pytest.raises(ValueError, match="finitos"):
        MODULE.validate_dataset(frame, tuple(features))


def test_validate_dataset_rejects_feature_order_different_from_contract() -> None:
    features = [f"f{i}" for i in range(56)]
    reversed_features = list(reversed(features))
    frame = pd.DataFrame(
        {
            **{feature: [0.1, 0.2, 0.3] for feature in reversed_features},
            "proxy_label": ["forest", "forest", "non_forest"],
            "split": ["train", "validation", "test"],
            "sample_id": ["a", "b", "c"],
            "block_id": ["a", "b", "c"],
            "sample_year": [2020, 2020, 2020],
        }
    )

    with pytest.raises(ValueError, match="orden"):
        MODULE.validate_dataset(frame, tuple(features))
