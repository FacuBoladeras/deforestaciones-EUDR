"""Entrypoint reproducible y sin efectos implícitos para el muestreo P0."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from shapely.geometry import box, mapping

from deforestation_pipeline.config import SpectralIndex
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.seasonal_feature_stack import (
    SeasonalFeatureStack,
    seasonal_feature_columns,
)
from deforestation_pipeline.training_export import DriveExportTaskRecord
from deforestation_pipeline.training_p0 import (
    TrainingP0Error,
    load_training_p0_config,
    run_training_p0,
    split_entre_rios_at_meridian,
)
from deforestation_pipeline.training_sampling import (
    ProxyLabelStack,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_config(
    path: Path,
    output_directory: Path,
    *,
    year: int = 2020,
    schema_version: str = "1.0.0",
    preflight_count_mode: str = "synchronous",
) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": schema_version,
                "pipeline_config_path": str(PROJECT_ROOT / "configs" / "default.yml"),
                "source_catalog_path": str(PROJECT_ROOT / "data" / "catalog.yml"),
                "manifest_directory": str(output_directory),
                "earth_engine_project": "ee-facuboladerasgee",
                "year": year,
                "preflight_count_mode": preflight_count_mode,
                "seed": 42,
                "scale_m": 30,
                "block_size_m": 3000,
                "boundary_longitude": -60.0,
                "boundary_buffer_m": 3000,
                "provincial_quotas": {
                    "forest": 800,
                    "non_forest": 800,
                    "ambiguous": 400,
                },
                "partitions": [
                    {
                        "utm_zone": 20,
                        "grid_crs": "EPSG:32720",
                        "quotas": {
                            "forest": 400,
                            "non_forest": 400,
                            "ambiguous": 200,
                        },
                    },
                    {
                        "utm_zone": 21,
                        "grid_crs": "EPSG:32721",
                        "quotas": {
                            "forest": 400,
                            "non_forest": 400,
                            "ambiguous": 200,
                        },
                    },
                ],
                "drive": {
                    "folder": "deforestation-pipeline-training-p0",
                    "file_name_prefix": f"entre_rios_training_{year}_p0",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_training_p0_config_parses_exact_budget_and_rejects_unaligned_blocks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "training.yml"
    _write_config(path, tmp_path / "manifests")

    config = load_training_p0_config(path)

    assert config.year == 2020
    assert config.seed == 42
    assert config.scale_m == 30
    assert config.block_size_m == 3000
    assert config.earth_engine_project == "ee-facuboladerasgee"
    assert config.provincial_quotas.total == 2000
    assert tuple(partition.quotas.total for partition in config.partitions) == (1000, 1000)

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["block_size_m"] = 3010
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="múltiplo"):
        load_training_p0_config(path)


def test_training_p0_config_accepts_2024_and_rejects_unlabelled_2025(
    tmp_path: Path,
) -> None:
    path = tmp_path / "training.yml"
    _write_config(path, tmp_path / "manifests", year=2024, schema_version="1.1.0")

    config = load_training_p0_config(path)
    assert config.year == 2024
    assert config.schema_version == "1.1.0"

    _write_config(path, tmp_path / "manifests", year=2024, schema_version="1.0.0")
    with pytest.raises(ValidationError, match=r"schema_version 1\.1\.0"):
        load_training_p0_config(path)

    _write_config(
        path,
        tmp_path / "manifests",
        year=2020,
        schema_version="1.0.0",
        preflight_count_mode="deferred_to_export_validation",
    )
    with pytest.raises(ValidationError, match=r"schema_version 1\.1\.0"):
        load_training_p0_config(path)

    _write_config(path, tmp_path / "manifests", year=2025, schema_version="1.1.0")
    with pytest.raises(ValidationError):
        load_training_p0_config(path)


def test_meridian_split_has_no_area_overlap_and_excludes_versioned_buffer() -> None:
    aoi = box(-61.0, -33.0, -59.0, -31.0)

    partitions = split_entre_rios_at_meridian(
        aoi,
        boundary_longitude=-60.0,
        boundary_buffer_m=3000,
    )

    west, east = partitions
    assert west.utm_zone == 20
    assert east.utm_zone == 21
    assert west.raw_aoi.bounds[2] == pytest.approx(-60.0)
    assert east.raw_aoi.bounds[0] == pytest.approx(-60.0)
    assert west.raw_aoi.intersection(east.raw_aoi).area == pytest.approx(0.0)
    assert west.sampling_aoi.intersection(east.sampling_aoi).area == pytest.approx(0.0)
    assert west.excluded_boundary_area_ha > 0
    assert east.excluded_boundary_area_ha > 0


class _Info:
    def __init__(self, value: object) -> None:
        self.value = value

    def getInfo(self) -> object:
        return self.value


class _Samples:
    def __init__(self, histogram: dict[str, int]) -> None:
        self.histogram = histogram

    def size(self) -> _Info:
        return _Info(sum(self.histogram.values()))

    def aggregate_histogram(self, _: str) -> _Info:
        return _Info(self.histogram)


class _RemoteAoi:
    def __init__(self, geometry: object) -> None:
        self.geometry_payload = geometry

    def geometry(self) -> _Info:
        return _Info(self.geometry_payload)


def _patch_remote_builders(
    monkeypatch: pytest.MonkeyPatch,
    *,
    histogram: dict[str, int] | None = None,
) -> None:
    from deforestation_pipeline import training_p0

    province = mapping(box(-61.0, -33.0, -59.0, -31.0))
    monkeypatch.setattr(
        training_p0,
        "authenticate_earth_engine",
        lambda *_args, **_kwargs: GeeSession(module=SimpleNamespace()),
    )
    monkeypatch.setattr(
        training_p0,
        "build_entre_rios_aoi",
        lambda _module: _RemoteAoi(province),
    )
    features = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))
    monkeypatch.setattr(
        training_p0,
        "build_seasonal_feature_stack",
        lambda **_kwargs: SeasonalFeatureStack(image=object(), feature_columns=features),
    )
    monkeypatch.setattr(
        training_p0,
        "build_multisource_proxy_label",
        lambda **_kwargs: ProxyLabelStack(image=object()),
    )
    actual_histogram = histogram or {
        "forest": 400,
        "non_forest": 400,
        "ambiguous": 200,
    }
    monkeypatch.setattr(
        training_p0,
        "build_stratified_training_samples",
        lambda **_kwargs: _Samples(actual_histogram),
    )


def test_dry_run_writes_sanitized_manifest_without_starting_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deforestation_pipeline import training_p0

    config_path = tmp_path / "training.yml"
    output = tmp_path / "manifests"
    _write_config(config_path, output)
    _patch_remote_builders(monkeypatch)
    starts: list[object] = []
    monkeypatch.setattr(
        training_p0,
        "start_training_table_export",
        lambda **kwargs: starts.append(kwargs),
    )
    credentials_path = tmp_path / "super-secret-credentials.json"

    result = run_training_p0(
        config_path=config_path,
        credentials_path=credentials_path,
        start_export=False,
        generated_at=datetime(2026, 7, 30, 20, tzinfo=UTC),
    )

    assert starts == []
    assert result.status == "dry_run_completed"
    assert result.manifest_path.exists()
    content = result.manifest_path.read_text(encoding="utf-8")
    assert str(credentials_path) not in content
    payload = json.loads(content)
    assert payload["status"] == "dry_run_completed"
    assert payload["schema_version"] == "1.0.0"
    assert "preflight_count_mode" not in payload
    assert len(payload["partitions"]) == 2
    assert "count_validation_status" not in payload["partitions"][0]
    assert payload["partitions"][0]["actual_counts"]["total"] == 1000
    assert payload["partitions"][0]["grid"]["grid_sha256"]
    assert len(payload["partitions"][0]["grid"]["transform"]) == 6
    assert len(payload["partitions"][0]["grid"]["bounds"]) == 4
    assert payload["boundary_buffer_m"] == 3000
    assert payload["tasks"] == []


def test_user_oauth_needs_no_service_account_file_and_manifest_has_only_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deforestation_pipeline import training_p0

    config_path = tmp_path / "training.yml"
    output = tmp_path / "manifests"
    _write_config(config_path, output)
    _patch_remote_builders(monkeypatch)
    user_projects: list[str] = []
    monkeypatch.setattr(
        training_p0,
        "authenticate_earth_engine",
        lambda *_args, **_kwargs: pytest.fail("service account no debe usarse"),
    )

    def fake_user_oauth(*, project: str) -> GeeSession:
        user_projects.append(project)
        return GeeSession(module=SimpleNamespace())

    monkeypatch.setattr(
        training_p0,
        "authenticate_earth_engine_user_oauth",
        fake_user_oauth,
    )

    result = run_training_p0(
        config_path=config_path,
        credentials_path=None,
        auth_mode="user_oauth",
        start_export=False,
        generated_at=datetime(2026, 7, 30, 20, tzinfo=UTC),
    )

    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert user_projects == ["ee-facuboladerasgee"]
    assert payload["auth_mode"] == "user_oauth"
    serialized = json.dumps(payload)
    assert "principal" not in serialized
    assert "token" not in serialized
    assert "client_email" not in serialized


def test_start_mode_persists_preflight_before_start_and_then_task_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deforestation_pipeline import training_p0

    config_path = tmp_path / "training.yml"
    output = tmp_path / "manifests"
    _write_config(config_path, output)
    _patch_remote_builders(monkeypatch)
    observed_preflight: list[str] = []

    def fake_start(**kwargs: Any) -> DriveExportTaskRecord:
        manifest_path = next(output.glob("*.json"))
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        observed_preflight.append(payload["status"])
        request = kwargs["request"]
        return DriveExportTaskRecord(
            task_id=f"task-{len(observed_preflight)}",
            state="READY",
            description=request.description,
            folder=request.folder,
            prefix=request.file_name_prefix,
            started_at=datetime(2026, 7, 30, 20, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(training_p0, "start_training_table_export", fake_start)

    result = run_training_p0(
        config_path=config_path,
        credentials_path=tmp_path / "credentials.json",
        start_export=True,
        generated_at=datetime(2026, 7, 30, 20, tzinfo=UTC),
    )

    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert observed_preflight == ["preflight_ready", "export_starting"]
    assert result.status == "export_started"
    assert [task["task_id"] for task in payload["tasks"]] == ["task-1", "task-2"]


def test_deferred_count_mode_starts_exports_without_synchronous_getinfo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deforestation_pipeline import training_p0

    config_path = tmp_path / "training.yml"
    output = tmp_path / "manifests"
    _write_config(
        config_path,
        output,
        year=2024,
        schema_version="1.1.0",
        preflight_count_mode="deferred_to_export_validation",
    )
    _patch_remote_builders(monkeypatch)
    monkeypatch.setattr(
        training_p0,
        "_sample_counts",
        lambda _samples: pytest.fail("no debe ejecutar getInfo síncrono"),
    )
    started: list[str] = []

    def fake_start(**kwargs: Any) -> DriveExportTaskRecord:
        request = kwargs["request"]
        started.append(request.description)
        return DriveExportTaskRecord(
            task_id=f"task-{len(started)}",
            state="READY",
            description=request.description,
            folder=request.folder,
            prefix=request.file_name_prefix,
            started_at=datetime(2026, 8, 5, tzinfo=UTC),
        )

    monkeypatch.setattr(training_p0, "start_training_table_export", fake_start)

    result = run_training_p0(
        config_path=config_path,
        credentials_path=None,
        auth_mode="user_oauth",
        start_export=True,
        generated_at=datetime(2026, 8, 5, tzinfo=UTC),
    )

    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert result.status == "export_started"
    assert len(started) == 2
    assert payload["schema_version"] == "1.1.0"
    assert payload["preflight_count_mode"] == "deferred_to_export_validation"
    assert all(
        partition["actual_counts"] is None
        and partition["count_validation_status"] == "deferred_to_export_validation"
        for partition in payload["partitions"]
    )


def test_start_is_blocked_when_drop_nulls_reduces_a_partition_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deforestation_pipeline import training_p0

    config_path = tmp_path / "training.yml"
    output = tmp_path / "manifests"
    _write_config(config_path, output)
    _patch_remote_builders(
        monkeypatch,
        histogram={"forest": 399, "non_forest": 400, "ambiguous": 200},
    )
    starts: list[object] = []
    monkeypatch.setattr(
        training_p0,
        "start_training_table_export",
        lambda **kwargs: starts.append(kwargs),
    )

    with pytest.raises(TrainingP0Error, match="preflight_count_mismatch"):
        run_training_p0(
            config_path=config_path,
            credentials_path=tmp_path / "credentials.json",
            start_export=True,
            generated_at=datetime(2026, 7, 30, 20, tzinfo=UTC),
        )

    assert starts == []
    payload = json.loads(next(output.glob("*.json")).read_text(encoding="utf-8"))
    assert payload["status"] == "preflight_failed"
