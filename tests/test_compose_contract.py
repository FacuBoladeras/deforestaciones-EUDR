from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")),
    )


def test_compose_separates_api_and_worker_with_shared_private_storage() -> None:
    services = _compose()["services"]

    assert set(services) == {"api", "worker"}
    assert services["api"]["build"]["dockerfile"] == "Dockerfile.api"
    assert services["worker"]["build"]["dockerfile"] == "Dockerfile.worker"
    assert services["api"]["build"]["args"]["VCS_REF"].startswith("${VCS_REF:?")
    assert services["worker"]["build"]["args"]["VCS_REF"].startswith("${VCS_REF:?")
    assert services["api"]["user"] == services["worker"]["user"] == "10001:10001"

    api_storage = services["api"]["volumes"][0]
    worker_storage = services["worker"]["volumes"][0]
    assert api_storage == worker_storage
    assert api_storage["type"] == "bind"
    assert api_storage["source"].startswith("${DEFORESTATION_PRIVATE_ROOT:?")
    assert api_storage["target"] == "/var/lib/deforestation"
    assert api_storage["bind"]["create_host_path"] is False


def test_compose_mounts_worker_credentials_and_model_read_only() -> None:
    compose = _compose()
    api = compose["services"]["api"]
    worker = compose["services"]["worker"]

    assert "secrets" not in api
    assert worker["secrets"] == [{"source": "gee_credentials", "target": "gee-credentials.json"}]
    assert compose["secrets"]["gee_credentials"]["file"].startswith(
        "${DEFORESTATION_GEE_CREDENTIALS:?"
    )
    model = worker["volumes"][1]
    assert model["source"].startswith("${DEFORESTATION_RF_MODEL_ARTIFACT:?")
    assert model["target"].endswith("random_forest_forest_multiyear_2020_2024.joblib")
    assert model["read_only"] is True
    assert model["bind"]["create_host_path"] is False
    assert worker["depends_on"]["api"]["condition"] == "service_healthy"


def test_compose_applies_local_security_and_resource_limits() -> None:
    for service in _compose()["services"].values():
        assert service["read_only"] is True
        assert service["init"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cpus"]
        assert service["mem_limit"]
        assert service["pids_limit"] > 0
        assert any("noexec" in mount and "size=" in mount for mount in service["tmpfs"])
        assert service["logging"]["options"] == {"max-file": "5", "max-size": "10m"}
        assert service.get("privileged") is not True
        assert service.get("network_mode") != "host"

    api = _compose()["services"]["api"]
    assert api["ports"] == [{"target": 8000, "published": "8000", "host_ip": "127.0.0.1"}]


def test_compose_example_contains_paths_not_secret_values() -> None:
    example = (PROJECT_ROOT / "compose.env.example").read_text(encoding="utf-8")
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "VCS_REF=replace-with-full-commit-sha" in example
    assert "DEFORESTATION_GEE_CREDENTIALS=./credentials.json" in example
    assert "DEFORESTATION_RF_MODEL_ARTIFACT=./outputs/models/" in example
    assert "private_key" not in example
    assert "compose.env" in gitignore
    assert "compose.env" in dockerignore
