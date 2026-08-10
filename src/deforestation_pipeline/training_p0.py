"""Runner mínimo, reproducible y sin exportaciones implícitas para el P0 provincial."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import Transformer
from shapely.geometry import LineString, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from deforestation_pipeline.catalog import (
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.gee import (
    authenticate_earth_engine,
    authenticate_earth_engine_user_oauth,
)
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.training_export import (
    DriveExportTaskRecord,
    TrainingExportManifest,
    drive_export_request_from_manifest,
    start_training_table_export,
)
from deforestation_pipeline.training_sampling import (
    LABEL_COLUMN,
    SamplingQuotas,
    TrainingSamplingRequest,
    TrainingSchemaVersion,
    TrainingYear,
    build_entre_rios_aoi,
    build_multisource_proxy_label,
    build_seasonal_feature_stack,
    build_stratified_training_samples,
)


class TrainingP0Error(RuntimeError):
    """Fallo sanitizado del preflight o arranque P0."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Training P0 fallido: {code}")


class StrictP0Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


AuthMode = Literal["service_account", "user_oauth"]
TRAINING_P0_SCHEMA_VERSION: Literal["1.1.0"] = "1.1.0"


class TrainingP0PartitionConfig(StrictP0Model):
    utm_zone: Literal[20, 21]
    grid_crs: Literal["EPSG:32720", "EPSG:32721"]
    quotas: SamplingQuotas

    @model_validator(mode="after")
    def crs_matches_zone(self) -> TrainingP0PartitionConfig:
        if self.grid_crs != f"EPSG:{32700 + self.utm_zone}":
            raise ValueError("grid_crs no coincide con utm_zone")
        return self


class TrainingP0DriveConfig(StrictP0Model):
    folder: str = Field(min_length=1)
    file_name_prefix: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")


class TrainingP0Config(StrictP0Model):
    schema_version: TrainingSchemaVersion
    pipeline_config_path: Path
    source_catalog_path: Path
    manifest_directory: Path
    earth_engine_project: Literal["ee-facuboladerasgee"]
    year: TrainingYear
    preflight_count_mode: Literal[
        "synchronous",
        "deferred_to_export_validation",
    ] = "synchronous"
    seed: Annotated[int, Field(ge=0)]
    scale_m: Literal[30]
    block_size_m: Annotated[int, Field(gt=0)]
    boundary_longitude: float
    boundary_buffer_m: Annotated[int, Field(gt=0)]
    provincial_quotas: SamplingQuotas
    partitions: Annotated[
        tuple[TrainingP0PartitionConfig, ...],
        Field(min_length=2, max_length=2),
    ]
    drive: TrainingP0DriveConfig

    @model_validator(mode="after")
    def provincial_contract_is_consistent(self) -> TrainingP0Config:
        if self.schema_version == "1.0.0" and (
            self.year != 2020 or self.preflight_count_mode != "synchronous"
        ):
            raise ValueError(
                "años posteriores a 2020 o conteos diferidos requieren schema_version 1.1.0"
            )
        if self.block_size_m % self.scale_m:
            raise ValueError("block_size_m debe ser múltiplo de scale_m")
        if self.boundary_longitude != -60.0:
            raise ValueError("boundary_longitude debe ser -60.0")
        if tuple(item.utm_zone for item in self.partitions) != (20, 21):
            raise ValueError("partitions debe estar ordenado por zonas 20 y 21")
        summed = SamplingQuotas(
            forest=sum(item.quotas.forest for item in self.partitions),
            non_forest=sum(item.quotas.non_forest for item in self.partitions),
            ambiguous=sum(item.quotas.ambiguous for item in self.partitions),
        )
        if summed != self.provincial_quotas:
            raise ValueError("las cuotas de partición no suman provincial_quotas")
        if not 2_000 <= summed.total <= 5_000:
            raise ValueError("el total provincial P0 debe estar entre 2000 y 5000")
        return self


@dataclass(frozen=True, slots=True)
class TrainingP0PartitionAoi:
    utm_zone: Literal[20, 21]
    grid_crs: Literal["EPSG:32720", "EPSG:32721"]
    raw_aoi: BaseGeometry
    sampling_aoi: BaseGeometry
    excluded_boundary_area_ha: float


@dataclass(frozen=True, slots=True)
class TrainingP0RunResult:
    status: Literal[
        "dry_run_completed",
        "preflight_ready",
        "export_starting",
        "export_started",
        "preflight_failed",
    ]
    manifest_path: Path
    tasks: tuple[DriveExportTaskRecord, ...]


def load_training_p0_config(path: Path) -> TrainingP0Config:
    """Carga YAML local y delega todas las invariantes al contrato Pydantic."""
    try:
        payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise TrainingP0Error("configuration_unreadable") from error
    return TrainingP0Config.model_validate(payload)


def split_entre_rios_at_meridian(
    aoi_wgs84: BaseGeometry,
    *,
    boundary_longitude: float,
    boundary_buffer_m: float,
) -> tuple[TrainingP0PartitionAoi, TrainingP0PartitionAoi]:
    """Parte en -60° y excluye un buffer métrico a ambos lados de la costura."""
    if (
        aoi_wgs84.is_empty
        or not aoi_wgs84.is_valid
        or aoi_wgs84.geom_type not in {"Polygon", "MultiPolygon"}
    ):
        raise TrainingP0Error("aoi_invalid")
    min_lon, min_lat, max_lon, max_lat = aoi_wgs84.bounds
    if not min_lon < boundary_longitude < max_lon:
        raise TrainingP0Error("boundary_outside_aoi")
    raw_aois = (
        aoi_wgs84.intersection(box(-180, -90, boundary_longitude, 90)),
        aoi_wgs84.intersection(box(boundary_longitude, -90, 180, 90)),
    )
    definitions: tuple[
        tuple[Literal[20], Literal["EPSG:32720"]],
        tuple[Literal[21], Literal["EPSG:32721"]],
    ] = ((20, "EPSG:32720"), (21, "EPSG:32721"))
    results: list[TrainingP0PartitionAoi] = []
    for raw_aoi, (zone, crs) in zip(raw_aois, definitions, strict=True):
        forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        inverse = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        projected = transform(forward.transform, raw_aoi)
        boundary_line = transform(
            forward.transform,
            LineString(
                (
                    (boundary_longitude, min_lat - 1),
                    (boundary_longitude, max_lat + 1),
                )
            ),
        )
        sampling_projected = projected.difference(boundary_line.buffer(boundary_buffer_m))
        sampling = transform(inverse.transform, sampling_projected)
        excluded_area_ha = max(0.0, (projected.area - sampling_projected.area) / 10_000)
        results.append(
            TrainingP0PartitionAoi(
                utm_zone=cast(Literal[20, 21], zone),
                grid_crs=cast(Literal["EPSG:32720", "EPSG:32721"], crs),
                raw_aoi=raw_aoi,
                sampling_aoi=sampling,
                excluded_boundary_area_ha=excluded_area_ha,
            )
        )
    return results[0], results[1]


def run_training_p0(
    *,
    config_path: Path,
    credentials_path: Path | None,
    auth_mode: AuthMode = "service_account",
    start_export: bool,
    generated_at: datetime | None = None,
) -> TrainingP0RunResult:
    """Ejecuta preflight; sólo arranca Drive después de persistir evidencia local."""
    generation_time = generated_at or datetime.now(UTC)
    if generation_time.tzinfo is None or generation_time.utcoffset() is None:
        raise TrainingP0Error("generated_at_requires_timezone")
    config = load_training_p0_config(config_path)
    pipeline_config = load_config(config.pipeline_config_path)
    source_plan = build_benchmark_source_plan(
        load_source_catalog(config.source_catalog_path),
        pipeline_config,
    )
    if auth_mode == "service_account":
        if credentials_path is None:
            raise TrainingP0Error("service_account_credentials_required")
        session = authenticate_earth_engine(credentials_path)
    else:
        session = authenticate_earth_engine_user_oauth(
            project=config.earth_engine_project,
        )
    geometry_payload = build_entre_rios_aoi(session.module).geometry().getInfo()
    if not isinstance(geometry_payload, dict):
        raise TrainingP0Error("remote_aoi_invalid")
    province = shape(geometry_payload)
    partition_aois = split_entre_rios_at_meridian(
        province,
        boundary_longitude=config.boundary_longitude,
        boundary_buffer_m=config.boundary_buffer_m,
    )
    manifest_path = _manifest_path(config.manifest_directory, generation_time)
    partition_records: list[dict[str, object]] = []
    export_inputs: list[tuple[Any, Any]] = []
    mismatch = False
    for partition_config, partition_aoi in zip(
        config.partitions,
        partition_aois,
        strict=True,
    ):
        request = TrainingSamplingRequest(
            schema_version=config.schema_version,
            year=config.year,
            seed=config.seed,
            grid_crs=partition_config.grid_crs,
            utm_zone=partition_config.utm_zone,
            scale_m=config.scale_m,
            block_size_m=config.block_size_m,
            quotas=partition_config.quotas,
            generated_at=generation_time,
        )
        grid = derive_raster_grid_spec(
            aoi_wgs84=partition_aoi.sampling_aoi,
            target_crs=partition_config.grid_crs,
            resolution_m=config.scale_m,
            nodata=pipeline_config.output.raster_nodata,
        )
        features = build_seasonal_feature_stack(
            session=session,
            source_plan=source_plan,
            data_config=pipeline_config.data,
            aoi_wgs84=partition_aoi.sampling_aoi,
            grid_spec=grid,
            request=request,
        )
        proxy = build_multisource_proxy_label(
            module=session.module,
            year=config.year,
            aoi_wgs84=partition_aoi.sampling_aoi,
        )
        samples = build_stratified_training_samples(
            module=session.module,
            feature_image=features.image,
            proxy_image=proxy.image,
            aoi_wgs84=partition_aoi.sampling_aoi,
            request=request,
        )
        expected = {
            "forest": partition_config.quotas.forest,
            "non_forest": partition_config.quotas.non_forest,
            "ambiguous": partition_config.quotas.ambiguous,
            "total": partition_config.quotas.total,
        }
        counts = _sample_counts(samples) if config.preflight_count_mode == "synchronous" else None
        mismatch = mismatch or (counts is not None and counts != expected)
        export_manifest = TrainingExportManifest.from_sampling_request(
            request=request,
            feature_columns=features.feature_columns,
        )
        drive_request = drive_export_request_from_manifest(
            manifest=export_manifest,
            description=f"{config.drive.file_name_prefix}_utm{partition_config.utm_zone}",
            folder=config.drive.folder,
            file_name_prefix=(f"{config.drive.file_name_prefix}_utm{partition_config.utm_zone}"),
        )
        partition_record: dict[str, object] = {
            "utm_zone": partition_config.utm_zone,
            "raw_aoi_bounds": list(partition_aoi.raw_aoi.bounds),
            "sampling_aoi_bounds": list(partition_aoi.sampling_aoi.bounds),
            "excluded_boundary_area_ha": partition_aoi.excluded_boundary_area_ha,
            "expected_counts": expected,
            "actual_counts": counts,
            "grid": grid.model_dump(mode="json"),
            "export_manifest": export_manifest.model_dump(mode="json"),
        }
        if config.schema_version == TRAINING_P0_SCHEMA_VERSION:
            partition_record["count_validation_status"] = (
                "passed"
                if counts == expected
                else "deferred_to_export_validation"
                if counts is None
                else "failed"
            )
        partition_records.append(partition_record)
        export_inputs.append((samples, drive_request))

    base_payload: dict[str, object] = {
        "schema_version": config.schema_version,
        "generated_at": generation_time.isoformat(),
        "year": config.year,
        "seed": config.seed,
        "auth_mode": auth_mode,
        "earth_engine_project": config.earth_engine_project,
        "boundary_longitude": config.boundary_longitude,
        "boundary_buffer_m": config.boundary_buffer_m,
        "provincial_quotas": config.provincial_quotas.model_dump(mode="json"),
        "partitions": partition_records,
        "tasks": [],
    }
    if config.schema_version == TRAINING_P0_SCHEMA_VERSION:
        base_payload["preflight_count_mode"] = config.preflight_count_mode
    if mismatch:
        base_payload["status"] = "preflight_failed"
        _write_manifest(manifest_path, base_payload)
        raise TrainingP0Error("preflight_count_mismatch")
    if not start_export:
        base_payload["status"] = "dry_run_completed"
        _write_manifest(manifest_path, base_payload)
        return TrainingP0RunResult(
            status="dry_run_completed",
            manifest_path=manifest_path,
            tasks=(),
        )

    base_payload["status"] = "preflight_ready"
    _write_manifest(manifest_path, base_payload)
    tasks: list[DriveExportTaskRecord] = []
    for index, (samples, drive_request) in enumerate(export_inputs):
        if index:
            base_payload["status"] = "export_starting"
            base_payload["tasks"] = [task.model_dump(mode="json") for task in tasks]
            _write_manifest(manifest_path, base_payload)
        task = start_training_table_export(
            module=session.module,
            collection=samples,
            request=drive_request,
        )
        tasks.append(task)
    base_payload["status"] = "export_started"
    base_payload["tasks"] = [task.model_dump(mode="json") for task in tasks]
    _write_manifest(manifest_path, base_payload)
    return TrainingP0RunResult(
        status="export_started",
        manifest_path=manifest_path,
        tasks=tuple(tasks),
    )


def _sample_counts(samples: Any) -> dict[str, int]:
    total_raw = samples.size().getInfo()
    histogram_raw = samples.aggregate_histogram(LABEL_COLUMN).getInfo()
    if not isinstance(total_raw, int) or not isinstance(histogram_raw, dict):
        raise TrainingP0Error("preflight_counts_invalid")
    counts: dict[str, int] = {}
    for label in ("forest", "non_forest", "ambiguous"):
        value = histogram_raw.get(label, 0)
        if not isinstance(value, int):
            raise TrainingP0Error("preflight_counts_invalid")
        counts[label] = value
    counts["total"] = total_raw
    return counts


def _manifest_path(directory: Path, generated_at: datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"training-p0-{generated_at:%Y%m%dT%H%M%SZ}.json"


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
