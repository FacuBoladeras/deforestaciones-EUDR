from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_api_image_contract_is_minimal_and_non_root() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.api").read_text(encoding="utf-8")

    assert "uv sync --locked --no-dev --no-editable --package deforestation-api" in dockerfile
    assert "src/deforestation_pipeline" not in dockerfile
    assert "packages/reporting/src" not in dockerfile
    assert "services/worker/src" not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "DEFORESTATION_RUNTIME_ROOT=/opt/deforestation/runtime" in dockerfile
    assert "DEFORESTATION_API_PRIVATE_ROOT=/var/lib/deforestation/private" in dockerfile
    assert "data/boundaries/argentina_operational.geojson" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "http://127.0.0.1:8000/health" in dockerfile
    assert 'CMD ["uvicorn", "deforestation_api.app:create_app", "--factory"' in dockerfile
    assert '"--host", "0.0.0.0"' in dockerfile
    assert "DEFORESTATION_CODE_REVISION=${VCS_REF}" in dockerfile
    assert 'test "${#VCS_REF}" -eq 40' in dockerfile


def test_api_image_versions_are_explicit_and_never_use_latest() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.api").read_text(encoding="utf-8")

    assert (
        "ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:"
        "2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
    ) in dockerfile
    assert (
        "ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.12@sha256:"
        "72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec"
    ) in dockerfile
    assert ":latest" not in dockerfile


def test_docker_context_excludes_private_and_regenerable_state() -> None:
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = {line.strip() for line in dockerignore if line.strip() and not line.startswith("#")}

    assert "**/credentials.json" in patterns
    assert "**/.env*" in patterns
    assert "!**/.env.example" in patterns
    assert "outputs/" in patterns
    assert "output/" in patterns
    assert "tmp/" in patterns
    assert ".venv/" in patterns
    assert "**/node_modules/" in patterns
    assert "**/*.geojson" in patterns
    assert "!data/boundaries/argentina_operational.geojson" in patterns
    assert "data/models/**/*.joblib" in patterns
    assert "data/models/" not in patterns


def test_worker_image_contract_contains_science_without_api() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")

    assert "uv sync --locked --no-dev --no-editable --package deforestation-worker" in dockerfile
    assert "COPY src src" in dockerfile
    assert "COPY packages/reporting/src packages/reporting/src" in dockerfile
    assert "COPY services/worker/src services/worker/src" in dockerfile
    assert "services/api/src" not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "DEFORESTATION_RUNTIME_ROOT=/opt/deforestation/runtime" in dockerfile
    assert "DEFORESTATION_API_PRIVATE_ROOT=/var/lib/deforestation/private" in dockerfile
    assert "DEFORESTATION_ANALYSIS_OUTPUT_ROOT=/var/lib/deforestation/runs" in dockerfile
    assert "DEFORESTATION_GEE_CREDENTIALS=/run/secrets/gee-credentials.json" in dockerfile
    assert "DEFORESTATION_RF_MODEL_ARTIFACT=/opt/deforestation/models/" in dockerfile
    assert "DEFORESTATION_CODE_REVISION=${VCS_REF}" in dockerfile
    assert 'test "${#VCS_REF}" -eq 40' in dockerfile
    assert "COPY configs /opt/deforestation/runtime/configs" in dockerfile
    assert "COPY data/schemas /opt/deforestation/runtime/data/schemas" in dockerfile
    assert "data/models/rf_forest_multiyear_2020_2024_v1.json" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert 'CMD ["deforestation-worker", "--healthcheck"]' in dockerfile


def test_worker_image_uses_the_same_pinned_toolchain_as_api() -> None:
    api = (PROJECT_ROOT / "Dockerfile.api").read_text(encoding="utf-8")
    worker = (PROJECT_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")

    pinned_args = [line for line in api.splitlines() if line.startswith("ARG ")][:2]
    assert pinned_args
    assert all(argument in worker for argument in pinned_args)
    assert ":latest" not in worker
