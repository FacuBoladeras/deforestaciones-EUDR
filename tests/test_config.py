"""Pruebas de carga y reproducibilidad de configuración."""

from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from deforestation_pipeline.config import (
    ConfigurationError,
    PipelineConfig,
    ResolvedPipelineConfig,
    execution_config_hash,
    load_config,
    load_license_registry,
    parameters_hash,
    resolve_run_config,
    scientific_parameters_hash,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_config_is_valid_and_deterministic() -> None:
    """La configuración versionada debe cargar y producir un SHA-256 estable."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    assert config.analysis.cutoff_date == date(2020, 12, 31)
    assert config.spatial.forest_definition_min_area_ha == 0.5
    assert config.spatial.preserve_subthreshold_events is True
    assert config.spatial.raster_crs_strategy == "local_utm"
    assert len(parameters_hash(config)) == 64


def test_parameters_hash_matches_golden_for_controlled_configuration() -> None:
    """El digest se calcula sobre el contenido validado, no sobre un YAML dado."""
    config = PipelineConfig.model_validate(
        {
            "schema_version": "1.2.0",
            "analysis": {
                "cutoff_date": "2020-12-31",
                "benchmark_start_date": "2019-01-01",
                "analysis_end_date": "2021-01-01",
                "random_seed": 7,
            },
            "spatial": {
                "interchange_crs": "EPSG:4326",
                "area_crs_strategy": "auto_equal_area",
                "raster_crs_strategy": "local_utm",
                "forest_definition_min_area_ha": 0.5,
                "minimum_coordinate_decimals": 6,
                "preserve_subthreshold_events": True,
            },
            "data": {
                "benchmark_sensor": "HLS",
                "target_resolution_m": 30,
                "composition_interval": "annual",
                "composition_reducer": "median",
                "source_native_reflectance_scale_factor": 0.0001,
                "source_native_reflectance_offset": 0.0,
                "earth_engine_reflectance_multiplier": 1.0,
                "earth_engine_reflectance_offset": 0.0,
                "mask_high_aerosol": True,
                "preserve_water": True,
                "indices": ["NDVI"],
            },
            "output": {
                "directory": "outputs",
                "formats": ["json", "png", "geotiff"],
                "raster_nodata": -9999.0,
                "maximum_direct_download_bytes": 32_000_000,
                "maximum_direct_download_dimension": 10_000,
                "maximum_series_download_bytes": 256_000_000,
                "rgb_min_reflectance": 0.01,
                "rgb_max_reflectance": 0.18,
                "index_visualization_ranges": [{"index": "NDVI", "minimum": -1.0, "maximum": 1.0}],
                "png_dpi": 150,
            },
        }
    )

    assert parameters_hash(config) == (
        "8ac73e287477ffc6b3f9c87a3f0b87884f34f9ec4f4fdb70b8c4c97fe8c90947"
    )


def test_parameters_hash_ignores_yaml_key_order_and_surface_format(
    tmp_path: Path,
) -> None:
    """YAML equivalente produce el mismo modelo validado y el mismo digest."""
    first = tmp_path / "first.yml"
    second = tmp_path / "second.yml"
    first.write_text(
        "\n".join(
            [
                'schema_version: "1.2.0"',
                "analysis:",
                "  cutoff_date: 2020-12-31",
                "  benchmark_start_date: 2019-01-01",
                "  analysis_end_date: 2021-01-01",
                "  random_seed: 7",
                "spatial:",
                "  interchange_crs: EPSG:4326",
                "  area_crs_strategy: auto_equal_area",
                "  raster_crs_strategy: local_utm",
                "  forest_definition_min_area_ha: 0.5",
                "  minimum_coordinate_decimals: 6",
                "  preserve_subthreshold_events: true",
                "data:",
                "  benchmark_sensor: HLS",
                "  target_resolution_m: 30",
                "  composition_interval: annual",
                "  composition_reducer: median",
                "  source_native_reflectance_scale_factor: 0.0001",
                "  source_native_reflectance_offset: 0.0",
                "  earth_engine_reflectance_multiplier: 1.0",
                "  earth_engine_reflectance_offset: 0.0",
                "  mask_high_aerosol: true",
                "  preserve_water: true",
                "  indices: [NDVI, EVI2]",
                "output:",
                "  directory: outputs",
                "  formats: [json, geojson, png, geotiff]",
                "  raster_nodata: -9999.0",
                "  maximum_direct_download_bytes: 32000000",
                "  maximum_direct_download_dimension: 10000",
                "  maximum_series_download_bytes: 256000000",
                "  rgb_min_reflectance: 0.01",
                "  rgb_max_reflectance: 0.18",
                "  index_visualization_ranges:",
                "    - {index: NDVI, minimum: -1.0, maximum: 1.0}",
                "    - {index: EVI2, minimum: -1.0, maximum: 1.0}",
                "  png_dpi: 150",
                "",
            ]
        ),
        encoding="utf-8",
    )
    second.write_text(
        "\n".join(
            [
                "# Same semantic configuration, deliberately reordered and formatted.",
                (
                    "output: {png_dpi: 150, index_visualization_ranges: "
                    "[{maximum: 1.0, minimum: -1.0, index: NDVI}, "
                    "{minimum: -1.0, index: EVI2, maximum: 1.0}], "
                    "rgb_max_reflectance: 0.18, rgb_min_reflectance: 0.01, "
                    "maximum_direct_download_dimension: 10000, "
                    "maximum_direct_download_bytes: 32000000, "
                    "maximum_series_download_bytes: 256000000, "
                    "raster_nodata: -9999.0, formats: [json, geojson, png, geotiff], "
                    "directory: outputs}"
                ),
                (
                    "data: {indices: [NDVI, EVI2], preserve_water: true, "
                    "mask_high_aerosol: true, earth_engine_reflectance_offset: 0.0, "
                    "earth_engine_reflectance_multiplier: 1.0, "
                    "source_native_reflectance_offset: 0.0, "
                    "source_native_reflectance_scale_factor: 0.0001, "
                    "composition_reducer: median, "
                    "composition_interval: annual, target_resolution_m: 30, "
                    "benchmark_sensor: HLS}"
                ),
                (
                    "spatial: {preserve_subthreshold_events: true, "
                    "minimum_coordinate_decimals: 6, forest_definition_min_area_ha: 0.5, "
                    "raster_crs_strategy: local_utm, area_crs_strategy: auto_equal_area, "
                    "interchange_crs: EPSG:4326}"
                ),
                (
                    "analysis: {random_seed: 7, analysis_end_date: 2021-01-01, "
                    "benchmark_start_date: 2019-01-01, cutoff_date: 2020-12-31}"
                ),
                'schema_version: "1.2.0"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert parameters_hash(load_config(first)) == parameters_hash(load_config(second))


def test_parameters_hash_changes_when_a_valid_parameter_changes() -> None:
    """Cambiar un parámetro válido modifica la identidad vigente."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    baseline = PipelineConfig.model_validate(payload)
    payload["analysis"]["random_seed"] = 43
    changed = PipelineConfig.model_validate(payload)

    assert parameters_hash(changed) != parameters_hash(baseline)


def test_parameters_hash_currently_includes_output_directory() -> None:
    """Caracteriza que la ruta operativa forma parte del digest actual."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    baseline = PipelineConfig.model_validate(payload)
    payload["output"]["directory"] = "other-output"
    changed = PipelineConfig.model_validate(payload)

    assert parameters_hash(changed) != parameters_hash(baseline)


def test_parameters_hash_currently_treats_list_order_as_significant() -> None:
    """Caracteriza que el orden de índices y formatos forma parte del digest."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    baseline = PipelineConfig.model_validate(payload)
    payload["data"]["indices"] = list(reversed(payload["data"]["indices"]))
    changed = PipelineConfig.model_validate(payload)

    assert parameters_hash(changed) != parameters_hash(baseline)


def test_indices_are_deeply_immutable_and_cannot_change_the_hash() -> None:
    """Los índices validados no pueden mutarse luego de calcular su identidad."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    digest = parameters_hash(config)

    with pytest.raises(TypeError):
        cast(Any, config.data.indices)[0] = "EVI2"

    assert parameters_hash(config) == digest


def test_output_formats_are_deeply_immutable_and_cannot_change_the_hash() -> None:
    """Los formatos validados no pueden mutarse luego de calcular su identidad."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    digest = parameters_hash(config)

    with pytest.raises(TypeError):
        cast(Any, config.output.formats)[0] = "csv"

    assert parameters_hash(config) == digest


def test_resolve_run_config_replaces_a_missing_analysis_end_date() -> None:
    """Una corrida recibe siempre una fecha final concreta y explícita."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    resolved = resolve_run_config(config, date(2024, 12, 31))

    assert isinstance(resolved, ResolvedPipelineConfig)
    assert resolved.analysis.analysis_end_date == date(2024, 12, 31)


def test_resolve_run_config_preserves_an_explicit_matching_end_date() -> None:
    """Una fecha declarada se conserva cuando coincide con la de la corrida."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload["analysis"]["analysis_end_date"] = date(2024, 12, 31)
    config = PipelineConfig.model_validate(payload)

    resolved = resolve_run_config(config, date(2024, 12, 31))

    assert resolved.analysis.analysis_end_date == date(2024, 12, 31)


def test_resolve_run_config_rejects_a_conflicting_explicit_end_date() -> None:
    """La fecha efectiva no puede contradecir un YAML ya fijado."""
    payload = load_config(PROJECT_ROOT / "configs" / "default.yml").model_dump()
    payload["analysis"]["analysis_end_date"] = date(2024, 12, 31)
    config = PipelineConfig.model_validate(payload)

    with pytest.raises(ValueError, match="analysis_end_date"):
        resolve_run_config(config, date(2025, 1, 1))


def test_resolve_run_config_does_not_mutate_the_source_config() -> None:
    """Resolver una corrida devuelve otro modelo y conserva el config de entrada."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    resolved = resolve_run_config(config, date(2024, 12, 31))

    assert config.analysis.analysis_end_date is None
    assert resolved is not config


def test_scientific_hash_ignores_operational_output_changes() -> None:
    """La identidad científica excluye el destino y formatos de salida."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    baseline = resolve_run_config(config, date(2024, 12, 31))
    payload = baseline.model_dump()
    payload["output"]["directory"] = "other-output"
    changed = ResolvedPipelineConfig.model_validate(payload)

    assert scientific_parameters_hash(changed) == scientific_parameters_hash(baseline)


def test_execution_hash_includes_operational_output_changes() -> None:
    """La identidad de ejecución incluye el destino y formatos de salida."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    baseline = resolve_run_config(config, date(2024, 12, 31))
    payload = baseline.model_dump()
    payload["output"]["directory"] = "other-output"
    changed = ResolvedPipelineConfig.model_validate(payload)

    assert execution_config_hash(changed) != execution_config_hash(baseline)


def test_resolved_end_date_changes_scientific_and_execution_identities() -> None:
    """El período efectivo forma parte de ambas identidades de la corrida."""
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    first = resolve_run_config(config, date(2024, 12, 31))
    second = resolve_run_config(config, date(2025, 1, 1))

    assert scientific_parameters_hash(first) != scientific_parameters_hash(second)
    assert execution_config_hash(first) != execution_config_hash(second)


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


def test_license_registry_records_both_hls_products_used_by_step_10() -> None:
    """El primer uso de píxeles debe registrar condiciones y atribución."""
    registry = load_license_registry(PROJECT_ROOT / "data" / "licenses.yml")

    assert registry.schema_version == "1.0.0"
    assert tuple(dataset.dataset_id for dataset in registry.datasets) == (
        "hls_l30_v2",
        "hls_s30_v2",
    )
    assert all(dataset.provider == "NASA LP DAAC" for dataset in registry.datasets)
    assert all("modified" in dataset.attribution.lower() for dataset in registry.datasets)
    assert all(dataset.restrictions for dataset in registry.datasets)


def test_yaml_root_must_be_a_mapping(tmp_path: Path) -> None:
    """Un documento escalar no puede validarse como configuración."""
    invalid_config = tmp_path / "invalid.yml"
    invalid_config.write_text("- item\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="debe ser un objeto YAML"):
        load_config(invalid_config)
