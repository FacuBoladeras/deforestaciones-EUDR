"""Exportación explícita y auditable del dataset de entrenamiento."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import ValidationError

from deforestation_pipeline.config import SpectralIndex
from deforestation_pipeline.seasonal_feature_stack import seasonal_feature_columns
from deforestation_pipeline.training_export import (
    DriveExportRequest,
    TrainingExportManifest,
    drive_export_request_from_manifest,
    start_training_table_export,
)
from deforestation_pipeline.training_sampling import (
    LABEL_COLUMN,
    METADATA_COLUMNS,
    SamplingQuotas,
    TrainingSamplingRequest,
)


class FakeTask:
    id = "task-123"

    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def status(self) -> dict[str, str]:
        return {"state": "READY"}


class FakeTableExport:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None
        self.task = FakeTask()

    def toDrive(self, **kwargs: object) -> FakeTask:
        self.kwargs = kwargs
        return self.task


def _sampling_request(
    *,
    year: Literal[2020, 2021, 2022, 2023, 2024] = 2020,
    schema_version: Literal["1.0.0", "1.1.0"] = "1.0.0",
) -> TrainingSamplingRequest:
    return TrainingSamplingRequest(
        schema_version=schema_version,
        year=year,
        seed=73,
        grid_crs="EPSG:32720",
        utm_zone=20,
        scale_m=30,
        block_size_m=3000,
        quotas=SamplingQuotas(forest=900, non_forest=700, ambiguous=400),
        generated_at=datetime(2026, 7, 30, 18, tzinfo=UTC),
    )


def test_manifest_separates_features_label_and_metadata() -> None:
    request = _sampling_request()
    features = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))

    manifest = TrainingExportManifest.from_sampling_request(
        request=request,
        feature_columns=features,
    )
    payload = manifest.model_dump(mode="json")

    assert payload["feature_columns"] == list(features)
    assert payload["label_column"] == LABEL_COLUMN
    assert payload["metadata_columns"] == list(METADATA_COLUMNS)
    assert payload["year"] == 2020
    assert payload["schema_version"] == "1.0.0"
    assert payload["quotas"] == {
        "forest": 900,
        "non_forest": 700,
        "ambiguous": 400,
    }
    assert payload["grid"]["utm_zone"] == 20
    assert payload["provincial_grid_partitions"] == ["EPSG:32720", "EPSG:32721"]
    assert payload["aoi"]["asset_id"] == "FAO/GAUL/2015/level1"
    assert "mapbiomas" in payload["source_assets"]
    assert not set(features) & set(payload["metadata_columns"])


def test_manifest_uses_the_same_predictor_schema_for_a_2024_observation() -> None:
    request = _sampling_request(year=2024, schema_version="1.1.0")
    features = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))

    manifest = TrainingExportManifest.from_sampling_request(
        request=request,
        feature_columns=features,
    )

    assert manifest.year == 2024
    assert manifest.schema_version == "1.1.0"
    assert manifest.feature_columns == features
    assert manifest.source_assets["mapbiomas"].band == "classification_2024"
    assert manifest.source_assets["jrc"].band == "Map"
    assert "sample_year" in manifest.source_assets["hansen"].semantics

    legacy_payload = manifest.model_dump()
    legacy_payload["schema_version"] = "1.0.0"
    with pytest.raises(ValidationError, match=r"schema_version 1\.1\.0"):
        TrainingExportManifest.model_validate(legacy_payload)


def test_drive_export_starts_only_through_explicit_function_with_selectors() -> None:
    table_export = FakeTableExport()
    fake_module = SimpleNamespace(batch=SimpleNamespace(Export=SimpleNamespace(table=table_export)))
    collection = object()
    manifest = TrainingExportManifest.from_sampling_request(
        request=_sampling_request(),
        feature_columns=seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex)),
    )
    request = drive_export_request_from_manifest(
        manifest=manifest,
        description="entre_rios_training_2020_p0",
        folder="deforestation-pipeline",
        file_name_prefix="entre_rios_training_2020_p0",
    )
    selectors = (*manifest.feature_columns, LABEL_COLUMN, *METADATA_COLUMNS)

    result = start_training_table_export(
        module=fake_module,
        collection=collection,
        request=request,
    )

    assert table_export.task.started is True
    assert table_export.kwargs == {
        "collection": collection,
        "description": request.description,
        "folder": request.folder,
        "fileNamePrefix": request.file_name_prefix,
        "fileFormat": "CSV",
        "selectors": list(selectors),
    }
    assert result.task_id == "task-123"
    assert result.state == "READY"
    assert result.folder == request.folder
    assert result.prefix == request.file_name_prefix


def test_drive_request_rejects_missing_or_extra_model_features() -> None:
    manifest = TrainingExportManifest.from_sampling_request(
        request=_sampling_request(),
        feature_columns=seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex)),
    )
    selectors = (*manifest.feature_columns[:-1], LABEL_COLUMN, *METADATA_COLUMNS, "unexpected")

    with pytest.raises(ValidationError, match="features \\+ label \\+ metadata"):
        DriveExportRequest(
            description="entre_rios_training_2020_p0",
            folder="deforestation-pipeline",
            file_name_prefix="entre_rios_training_2020_p0",
            selectors=selectors,
        )


def test_task_id_is_preserved_when_initial_status_query_fails() -> None:
    class FailingStatusTask(FakeTask):
        id = "task-status-failed"

        def status(self) -> dict[str, str]:
            raise RuntimeError("SECRET remote details")

    table_export = FakeTableExport()
    table_export.task = FailingStatusTask()
    fake_module = SimpleNamespace(batch=SimpleNamespace(Export=SimpleNamespace(table=table_export)))
    manifest = TrainingExportManifest.from_sampling_request(
        request=_sampling_request(),
        feature_columns=seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex)),
    )
    request = drive_export_request_from_manifest(
        manifest=manifest,
        description="entre_rios_training_2020_p0",
        folder="deforestation-pipeline",
        file_name_prefix="entre_rios_training_2020_p0",
    )

    result = start_training_table_export(
        module=fake_module,
        collection=object(),
        request=request,
    )

    assert result.task_id == "task-status-failed"
    assert result.state == "UNKNOWN"
