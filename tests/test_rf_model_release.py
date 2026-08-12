from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import joblib
import pytest

from deforestation_pipeline.rf_model_release import verify_local_rf_model_release


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[SimpleNamespace, Path]:
    joblib_path = tmp_path / "models" / "model.joblib"
    joblib_path.parent.mkdir()
    bundle = {
        "model": SimpleNamespace(n_estimators=500),
        "feature_columns": ["f1"],
        "metadata": {
            "feature_columns_sha256": "a" * 64,
            "dataset_sha256": "b" * 64,
            "training_manifest_sha256": "c" * 64,
            "random_seed": 42,
        },
    }
    joblib.dump(bundle, joblib_path)
    registry_path = tmp_path / "data" / "models" / "release.json"
    registry_path.parent.mkdir(parents=True)
    registry = {
        "schema_version": "1.0.0",
        "model_name": "candidate",
        "asset_id": "projects/p/assets/models/candidate",
        "expected_tree_count": 500,
        "predictor_columns_sha256": "a" * 64,
        "joblib": {
            "path": "models/model.joblib",
            "size_bytes": joblib_path.stat().st_size,
            "sha256": _sha256(joblib_path),
            "availability": "external_or_retrained_not_committed",
        },
        "training_dataset": {"sha256": "b" * 64},
        "training_manifest": {"sha256": "c" * 64},
        "serialized_trees": {
            "file_sha256": "d" * 64,
            "multiset_sha256": "e" * 64,
        },
        "reproduction": {"random_seed": 42, "command": "python train.py"},
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    config = SimpleNamespace(
        schema_version="1.1.0",
        asset_id=registry["asset_id"],
        expected_tree_count=500,
        predictor_columns_sha256="a" * 64,
        serialized_trees_sha256="d" * 64,
        serialized_tree_multiset_sha256="e" * 64,
        joblib_sha256=_sha256(joblib_path),
        local_joblib_path="models/model.joblib",
        model_registry_path="data/models/release.json",
        model_registry_sha256=_sha256(registry_path),
    )
    return config, registry_path


def test_release_preflight_verifies_registry_artifact_and_bundle_metadata(tmp_path: Path) -> None:
    config, _ = _fixture(tmp_path)

    verified = verify_local_rf_model_release(config, project_root=tmp_path, load_bundle=True)

    assert verified.joblib_path == (tmp_path / "models" / "model.joblib")
    assert verified.registry["model_name"] == "candidate"
    assert verified.bundle is not None


@pytest.mark.parametrize("tamper", ["registry", "joblib", "bundle_metadata"])
def test_release_preflight_fails_closed_on_tampering(tmp_path: Path, tamper: str) -> None:
    config, registry_path = _fixture(tmp_path)
    if tamper == "registry":
        registry_path.write_text("{}", encoding="utf-8")
    elif tamper == "joblib":
        (tmp_path / "models" / "model.joblib").write_bytes(b"tampered")
    else:
        bundle = joblib.load(tmp_path / "models" / "model.joblib")
        bundle["metadata"]["dataset_sha256"] = "f" * 64
        joblib.dump(bundle, tmp_path / "models" / "model.joblib")
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["joblib"]["sha256"] = _sha256(tmp_path / "models" / "model.joblib")
        registry["joblib"]["size_bytes"] = (tmp_path / "models" / "model.joblib").stat().st_size
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        config.joblib_sha256 = registry["joblib"]["sha256"]
        config.model_registry_sha256 = _sha256(registry_path)

    with pytest.raises(ValueError, match=r"registry|joblib|dataset"):
        verify_local_rf_model_release(config, project_root=tmp_path, load_bundle=True)


def test_committed_candidate_registry_matches_configuration_and_local_artifact() -> None:
    from deforestation_pipeline.config import load_forest_model_config

    project_root = Path(__file__).parents[1]
    config = load_forest_model_config(project_root / "configs/rf-forest-entrerios-2020-2024.yml")

    verified = verify_local_rf_model_release(config, project_root=project_root, load_bundle=False)

    assert verified.registry["asset_id"] == config.asset_id
    assert verified.registry["joblib"]["sha256"] == config.joblib_sha256
