"""Pruebas de carga y reproducibilidad de configuración."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from deforestation_pipeline.config import (
    ConfigurationError,
    PipelineConfig,
    load_config,
    load_license_registry,
    parameters_hash,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_config_is_valid_and_deterministic() -> None:
    """La configuración versionada debe cargar y producir un SHA-256 estable."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    assert config.analysis.cutoff_date == date(2020, 12, 31)
    assert config.spatial.forest_definition_min_area_ha == 0.5
    assert config.spatial.preserve_subthreshold_events is True
    assert len(parameters_hash(config)) == 64
    assert parameters_hash(config) == parameters_hash(config)


def test_config_rejects_a_modified_cutoff_date() -> None:
    """La fecha de corte no puede usarse como hiperparámetro."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload["analysis"]["cutoff_date"] = date(2021, 1, 1)

    with pytest.raises(ValidationError, match="cutoff_date debe ser 2020-12-31"):
        PipelineConfig.model_validate(payload)


def test_config_rejects_a_modified_forest_area_threshold() -> None:
    """El umbral de superficie forestal no puede calibrarse con el modelo."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload["spatial"]["forest_definition_min_area_ha"] = 0.6

    with pytest.raises(ValidationError, match=r"debe ser 0\.5"):
        PipelineConfig.model_validate(payload)


def test_license_registry_starts_empty() -> None:
    """No deben declararse licencias de fuentes aún no utilizadas."""
    registry = load_license_registry(PROJECT_ROOT / "data" / "licenses.yml")

    assert registry.schema_version == "1.0.0"
    assert registry.datasets == []


def test_yaml_root_must_be_a_mapping(tmp_path: Path) -> None:
    """Un documento escalar no puede validarse como configuración."""
    invalid_config = tmp_path / "invalid.yml"
    invalid_config.write_text("- item\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="debe ser un objeto YAML"):
        load_config(invalid_config)
