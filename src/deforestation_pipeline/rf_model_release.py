"""Preflight reproducible del release local del Random Forest multianual."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib


@dataclass(frozen=True, slots=True)
class VerifiedRfModelRelease:
    """Paths e identidad comprobados antes de usar un joblib confiable."""

    registry_path: Path
    joblib_path: Path
    registry: Mapping[str, Any]
    bundle: Mapping[str, Any] | None


def verify_local_rf_model_release(
    config: Any,
    *,
    project_root: Path,
    load_bundle: bool,
) -> VerifiedRfModelRelease:
    """Valida registro, binario y metadata sin aceptar paths fuera del proyecto."""
    if getattr(config, "schema_version", None) != "1.1.0":
        raise ValueError("model registry sólo aplica al candidato RF 1.1.0")
    root = project_root.resolve()
    registry_path = _project_file(root, getattr(config, "model_registry_path", None), "registry")
    registry_bytes = registry_path.read_bytes()
    if _sha256(registry_bytes) != getattr(config, "model_registry_sha256", None):
        raise ValueError("model registry sha256 no coincide con la configuración")
    try:
        payload = json.loads(registry_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("model registry no es JSON UTF-8 válido") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0.0":
        raise ValueError("model registry debe usar schema_version 1.0.0")

    expected_pairs = (
        ("asset_id", getattr(config, "asset_id", None)),
        ("expected_tree_count", getattr(config, "expected_tree_count", None)),
        ("predictor_columns_sha256", getattr(config, "predictor_columns_sha256", None)),
    )
    for key, expected in expected_pairs:
        if payload.get(key) != expected:
            raise ValueError(f"model registry {key} no coincide con la configuración")

    joblib_record = _mapping(payload, "joblib")
    joblib_path = _project_file(root, joblib_record.get("path"), "joblib")
    joblib_bytes = joblib_path.read_bytes()
    joblib_sha = _sha256(joblib_bytes)
    if (
        joblib_sha != joblib_record.get("sha256")
        or joblib_sha != getattr(config, "joblib_sha256", None)
        or len(joblib_bytes) != joblib_record.get("size_bytes")
        or joblib_record.get("path") != getattr(config, "local_joblib_path", None)
    ):
        raise ValueError("joblib local no coincide con el release registrado")

    serialized = _mapping(payload, "serialized_trees")
    if serialized.get("file_sha256") != getattr(config, "serialized_trees_sha256", None):
        raise ValueError("registry serialized tree file sha256 no coincide")
    if serialized.get("multiset_sha256") != getattr(
        config, "serialized_tree_multiset_sha256", None
    ):
        raise ValueError("registry serialized tree multiset sha256 no coincide")

    bundle: Mapping[str, Any] | None = None
    if load_bundle:
        loaded = joblib.load(joblib_path)
        if not isinstance(loaded, Mapping):
            raise ValueError("joblib local no contiene un bundle mapeable")
        metadata = _mapping(loaded, "metadata")
        checks = (
            (
                "feature_columns_sha256",
                payload.get("predictor_columns_sha256"),
            ),
            ("dataset_sha256", _mapping(payload, "training_dataset").get("sha256")),
            (
                "training_manifest_sha256",
                _mapping(payload, "training_manifest").get("sha256"),
            ),
            ("random_seed", _mapping(payload, "reproduction").get("random_seed")),
        )
        for key, expected in checks:
            if metadata.get(key) != expected:
                label = "dataset" if key == "dataset_sha256" else key
                raise ValueError(f"joblib {label} no coincide con model registry")
        model = loaded.get("model")
        if getattr(model, "n_estimators", None) != payload.get("expected_tree_count"):
            raise ValueError("joblib tree count no coincide con model registry")
        bundle = loaded

    return VerifiedRfModelRelease(
        registry_path=registry_path,
        joblib_path=joblib_path,
        registry=payload,
        bundle=bundle,
    )


def _project_file(root: Path, configured: object, label: str) -> Path:
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError(f"{label} path no fue configurado")
    relative = Path(configured)
    if relative.is_absolute():
        raise ValueError(f"{label} path debe ser relativo al proyecto")
    resolved = (root / relative).resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise ValueError(f"{label} file no existe dentro del proyecto")
    return resolved


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"model registry {key} debe ser un objeto")
    return value


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
