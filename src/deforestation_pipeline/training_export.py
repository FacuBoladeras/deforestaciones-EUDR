"""Manifiesto y frontera explícita para exportar muestras a Google Drive."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deforestation_pipeline.config import SpectralIndex
from deforestation_pipeline.seasonal_feature_stack import seasonal_feature_columns
from deforestation_pipeline.training_sampling import (
    AOI_ASSET_ID,
    AOI_FILTERS,
    LABEL_COLUMN,
    METADATA_COLUMNS,
    PROVINCIAL_GRID_PARTITIONS,
    SOURCE_ASSETS,
    SamplingQuotas,
    TrainingSamplingRequest,
    TrainingSchemaVersion,
    TrainingYear,
)

TRAINING_EXPORT_MANIFEST_SCHEMA_VERSION: Literal["1.1.0"] = "1.1.0"


class StrictExportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SamplingGridManifest(StrictExportModel):
    crs: str
    utm_zone: Literal[20, 21]
    scale_m: Literal[30]
    block_size_m: int = Field(gt=0)
    block_size_pixels: int = Field(gt=0)


class SourceAssetManifest(StrictExportModel):
    asset_id: str
    band: str
    semantics: str
    role: Literal["proxy_label_only"] = "proxy_label_only"


class AoiManifest(StrictExportModel):
    asset_id: Literal["FAO/GAUL/2015/level1"] = AOI_ASSET_ID
    filters: dict[str, str] = AOI_FILTERS


class TrainingExportManifest(StrictExportModel):
    """Contrato serializable exacto del CSV de entrenamiento P0."""

    schema_version: TrainingSchemaVersion = TRAINING_EXPORT_MANIFEST_SCHEMA_VERSION
    created_at: datetime
    year: TrainingYear
    seed: int = Field(ge=0)
    feature_columns: Annotated[tuple[str, ...], Field(min_length=68, max_length=68)]
    label_column: Literal["proxy_label"] = LABEL_COLUMN
    metadata_columns: tuple[str, ...] = METADATA_COLUMNS
    source_assets: dict[str, SourceAssetManifest]
    aoi: AoiManifest = AoiManifest()
    provincial_grid_partitions: tuple[
        Literal["EPSG:32720"],
        Literal["EPSG:32721"],
    ] = PROVINCIAL_GRID_PARTITIONS
    grid: SamplingGridManifest
    scale_m: Literal[30]
    quotas: SamplingQuotas
    label_semantics: Literal["proxy_not_eudr_forest_unanimous_votes_ambiguous_preserved"] = (
        "proxy_not_eudr_forest_unanimous_votes_ambiguous_preserved"
    )
    nodata_policy: Literal["drop_null_samples_no_interpolation"] = (
        "drop_null_samples_no_interpolation"
    )

    @model_validator(mode="after")
    def columns_are_disjoint_and_reconstructible(self) -> TrainingExportManifest:
        if self.schema_version == "1.0.0" and self.year != 2020:
            raise ValueError("los años posteriores a 2020 requieren schema_version 1.1.0")
        expected = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))
        if self.feature_columns != expected:
            raise ValueError("feature_columns no coincide con el stack estacional reconstruible")
        if set(self.feature_columns) & set(self.metadata_columns):
            raise ValueError("feature_columns y metadata_columns deben ser disjuntas")
        if self.label_column in self.feature_columns or self.label_column in self.metadata_columns:
            raise ValueError("label_column debe declararse por separado")
        return self

    @classmethod
    def from_sampling_request(
        cls,
        *,
        request: TrainingSamplingRequest,
        feature_columns: tuple[str, ...],
    ) -> TrainingExportManifest:
        assets = {
            name: SourceAssetManifest(
                asset_id=source.asset_id,
                band=source.band_for_year(request.year),
                semantics=source.semantics,
            )
            for name, source in SOURCE_ASSETS.items()
        }
        return cls(
            schema_version=request.schema_version,
            created_at=request.generated_at,
            year=request.year,
            seed=request.seed,
            feature_columns=feature_columns,
            source_assets=assets,
            grid=SamplingGridManifest(
                crs=request.grid_crs,
                utm_zone=request.utm_zone,
                scale_m=request.scale_m,
                block_size_m=request.block_size_m,
                block_size_pixels=request.block_size_pixels,
            ),
            scale_m=request.scale_m,
            quotas=request.quotas,
        )


class DriveExportRequest(StrictExportModel):
    """Parámetros no secretos para una única tarea CSV asíncrona."""

    description: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    folder: str = Field(min_length=1, max_length=255)
    file_name_prefix: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    selectors: Annotated[tuple[str, ...], Field(min_length=1)]
    file_format: Literal["CSV"] = "CSV"

    @model_validator(mode="after")
    def selectors_are_explicit_and_unique(self) -> DriveExportRequest:
        if len(self.selectors) != len(set(self.selectors)):
            raise ValueError("selectors no puede contener duplicados")
        expected = (
            *seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex)),
            LABEL_COLUMN,
            *METADATA_COLUMNS,
        )
        if self.selectors != expected:
            raise ValueError("selectors debe ser exactamente features + label + metadata")
        return self

    @property
    def label_column_missing(self) -> bool:
        return LABEL_COLUMN not in self.selectors


def drive_export_request_from_manifest(
    *,
    manifest: TrainingExportManifest,
    description: str,
    folder: str,
    file_name_prefix: str,
) -> DriveExportRequest:
    """Deriva el único orden permitido desde el manifiesto validado."""
    return DriveExportRequest(
        description=description,
        folder=folder,
        file_name_prefix=file_name_prefix,
        selectors=(
            *manifest.feature_columns,
            manifest.label_column,
            *manifest.metadata_columns,
        ),
    )


class DriveExportTaskRecord(StrictExportModel):
    task_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    description: str
    folder: str
    prefix: str
    started_at: datetime


def start_training_table_export(
    *,
    module: Any,
    collection: Any,
    request: DriveExportRequest,
) -> DriveExportTaskRecord:
    """Crea y arranca la tarea; ninguna otra función de este módulo tiene efectos remotos."""
    task = module.batch.Export.table.toDrive(
        collection=collection,
        description=request.description,
        folder=request.folder,
        fileNamePrefix=request.file_name_prefix,
        fileFormat=request.file_format,
        selectors=list(request.selectors),
    )
    task.start()
    task_id = getattr(task, "id", None)
    try:
        status = task.status()
    except Exception:
        status = {}
    state = status.get("state")
    if not isinstance(state, str) or not state:
        state = "UNKNOWN"
    if not isinstance(task_id, str) or not task_id:
        status_id = status.get("id")
        task_id = status_id if isinstance(status_id, str) and status_id else "unknown"
    return DriveExportTaskRecord(
        task_id=task_id,
        state=state,
        description=request.description,
        folder=request.folder,
        prefix=request.file_name_prefix,
        started_at=datetime.now(UTC),
    )
