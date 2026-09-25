"""Recolector espacial por evento para evidencia agrícola temporal Dynamic World."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Annotated, Any, Final, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np
import rasterio
import yaml
from numpy.typing import NDArray
from pydantic import Field, field_validator, model_validator
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.warp import Resampling, reproject
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from deforestation_pipeline.agricultural_visualization import (
    render_annual_dynamic_world_land_cover,
    render_monthly_agricultural_evidence,
    select_monthly_evidence_windows,
)
from deforestation_pipeline.catalog import (
    AgriculturalCatalogSource,
    AgriculturalClassRule,
    build_agricultural_evidence_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import ArtifactProfile, OutputConfig, load_license_registry
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.local_runner import _publish_bundle
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import (
    FetchBytes,
    RasterDownloadError,
    materialize_ee_image_to_grid,
    validate_geotiff_bytes,
)
from deforestation_pipeline.schemas import NonEmptyString, RasterGridSpec, StrictModel

AGRICULTURAL_COLLECTION_SCHEMA_VERSION: Final = "1.1.0"
AGRICULTURAL_COLLECTION_BUNDLE_SCHEMA_VERSION: Final = "1.7.0"
DYNAMIC_WORLD_RASTER_BANDS: Final = (
    "valid_observation_count",
    "qualifying_crop_count",
    "mean_crop_probability",
    "crop_observation_fraction",
)
DYNAMIC_WORLD_ANNUAL_LABEL_BAND: Final = "dynamic_world_label_mode"
DYNAMIC_WORLD_ANNUAL_NODATA: Final = 255
TEMPORAL_COUNT_CUBE_NODATA: Final = 65535
# Each of the two chunk transfers uses four uncompressed bytes per pixel/month:
# two uint16 count bands, or one float32 probability band.  The configured
# budget applies independently to each HTTP raster transfer.
REMOTE_CHUNK_BYTES_PER_PIXEL_PER_WINDOW: Final = 4
_EVENT_ARTIFACT = "json/evidence/disturbance_events.geojson"
_SAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9_-]+")


class AgriculturalCollectorConfig(StrictModel):
    """Parámetros acotados del recolector, separados del gate de persistencia."""

    schema_version: Literal["1.2.0", "1.3.0", "1.4.0", "1.5.0"]
    source_id: Literal["dynamic_world_v1"]
    target_crs: Literal["EPSG:6933"]
    resolution_m: Literal[10]
    raster_nodata: float
    temporal_cadence: Literal["calendar_month"]
    agricultural_observation_start_date: date = date(2021, 1, 1)
    annual_land_cover_start_year: Annotated[int, Field(ge=2020, le=2100)] = 2020
    annual_land_cover_composite: Literal["mode_of_top1_label"] = "mode_of_top1_label"
    minimum_top1_probability: Annotated[float, Field(gt=0.5, le=1, strict=True)]
    minimum_valid_observations_per_pixel: Annotated[int, Field(ge=1, le=31, strict=True)]
    minimum_crop_observation_fraction: Annotated[float, Field(gt=0, le=1, strict=True)]
    maximum_events_per_batch: Annotated[int, Field(ge=1, le=100, strict=True)]
    maximum_windows_per_event: Annotated[int, Field(ge=1, le=72, strict=True)]
    maximum_remote_acquisitions: Annotated[int, Field(ge=1, le=500, strict=True)]
    maximum_shared_grid_pixels: Annotated[int, Field(ge=1, le=5_000_000, strict=True)]
    maximum_remote_chunk_uncompressed_bytes: Annotated[
        int, Field(ge=1_000_000, le=31_000_000, strict=True)
    ] = 24_000_000
    resampling: Literal["nearest"]
    event_footprint_method: Literal["exact_polygon_pixel_intersection_fraction"]

    @field_validator("raster_nodata")
    @classmethod
    def nodata_is_fixed(cls, value: float) -> float:
        if value != -9999.0:
            raise ValueError("raster_nodata debe ser -9999")
        return value

    @field_validator("agricultural_observation_start_date")
    @classmethod
    def observation_starts_after_eudr_cutoff(cls, value: date) -> date:
        if value != date(2021, 1, 1):
            raise ValueError("agricultural_observation_start_date debe ser 2021-01-01")
        return value


class AgriculturalCollectionRow(StrictModel):
    """Área de una clase para un evento, fuente y ventana mensual."""

    event_id: NonEmptyString
    window_id: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]
    start_date: date
    end_date_exclusive: date
    source_id: Literal["dynamic_world_v1"]
    class_name: Literal["crop", "unknown", "nodata"]
    area_ha: Annotated[float, Field(ge=0)]
    event_area_ha: Annotated[float, Field(gt=0)]
    event_area_exact_ha: Annotated[float, Field(gt=0)]
    grid_error_area_ha: float
    matched_scene_count: Annotated[int, Field(ge=0, strict=True)]
    mean_crop_probability: Annotated[float, Field(ge=0, le=1)] | None
    mean_crop_observation_fraction: Annotated[float, Field(ge=0, le=1)] | None
    raster_path: NonEmptyString
    valid_count_band: Annotated[int, Field(ge=1, strict=True)] | None = None
    qualifying_count_band: Annotated[int, Field(ge=1, strict=True)] | None = None
    event_footprint_path: NonEmptyString
    grid_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    persistence_evaluated: Literal[False]

    @model_validator(mode="after")
    def temporal_window_is_nonempty(self) -> AgriculturalCollectionRow:
        if self.end_date_exclusive <= self.start_date:
            raise ValueError("agricultural collection row requiere una ventana no vacía")
        if (self.valid_count_band is None) != (self.qualifying_count_band is None):
            raise ValueError("las bandas del cubo temporal deben declararse juntas")
        if (
            self.valid_count_band is not None
            and self.qualifying_count_band != self.valid_count_band + 1
        ):
            raise ValueError("las bandas valid/qualifying del cubo deben ser consecutivas")
        return self


class AgriculturalCollectionEvent(StrictModel):
    """Estado de recolección de un evento, incluso cuando falta onset."""

    event_id: NonEmptyString
    collection_status: Literal["collected", "insufficient_onset", "insufficient_post_onset_window"]
    estimated_onset_window_end: date | None
    window_count: Annotated[int, Field(ge=0, strict=True)]
    quality_flags: list[NonEmptyString]
    event_area_exact_ha: Annotated[float, Field(gt=0)] | None
    event_area_raster_ha: Annotated[float, Field(gt=0)] | None
    grid_error_area_ha: float | None
    grid: RasterGridSpec | None

    @model_validator(mode="after")
    def status_matches_spatial_materialization(self) -> AgriculturalCollectionEvent:
        spatial = (
            self.event_area_exact_ha,
            self.event_area_raster_ha,
            self.grid_error_area_ha,
            self.grid,
        )
        if self.collection_status == "collected":
            if self.window_count < 1 or any(value is None for value in spatial):
                raise ValueError("collected requiere ventanas, áreas y grilla")
        elif self.window_count != 0 or any(value is not None for value in spatial):
            raise ValueError("un evento no recolectado no puede declarar grilla o áreas")
        return self


class AgriculturalCollectionDocument(StrictModel):
    """Contrato del JSON crudo previo al gate de persistencia."""

    schema_version: Literal["1.0.0", "1.1.0"]
    analysis_id: UUID
    establishment_id: NonEmptyString
    created_at: datetime
    analysis_end_date: date
    status: Literal["collected", "insufficient_data"]
    attribution_generated: Literal[False]
    persistence_evaluated: Literal[False]
    events: list[AgriculturalCollectionEvent]
    rows: list[AgriculturalCollectionRow]

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at debe incluir zona horaria")
        return value

    @model_validator(mode="after")
    def rows_balance_events(self) -> AgriculturalCollectionDocument:
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("events contiene event_id duplicados")
        if not {row.event_id for row in self.rows}.issubset(event_ids):
            raise ValueError("rows referencia eventos ausentes")
        groups: dict[tuple[str, str], list[AgriculturalCollectionRow]] = {}
        for row in self.rows:
            groups.setdefault((row.event_id, row.window_id), []).append(row)
        for grouped_rows in groups.values():
            if {row.class_name for row in grouped_rows} != {"crop", "unknown", "nodata"}:
                raise ValueError("cada ventana debe conservar crop, unknown y nodata")
            if len(grouped_rows) != 3:
                raise ValueError("cada ventana debe contener exactamente tres clases")
            event_area = grouped_rows[0].event_area_ha
            if any(not math.isclose(row.event_area_ha, event_area) for row in grouped_rows):
                raise ValueError("event_area_ha difiere dentro de una ventana")
            if not math.isclose(sum(row.area_ha for row in grouped_rows), event_area, abs_tol=1e-6):
                raise ValueError("las clases no cierran el área del evento")
        expected_status = "collected" if self.rows else "insufficient_data"
        if self.status != expected_status:
            raise ValueError("status no coincide con las filas recolectadas")
        return self


@dataclass(frozen=True, slots=True)
class DynamicWorldWindowQuery:
    """Una consulta remota limitada a un evento y un mes poscambio."""

    event_id: str
    window_id: str
    start_date: date
    end_date_exclusive: date
    geometry_wgs84: BaseGeometry
    grid: RasterGridSpec
    source: AgriculturalCatalogSource
    minimum_top1_probability: float


@dataclass(frozen=True, slots=True)
class DynamicWorldAnnualLandCoverQuery:
    """Consulta anual descriptiva acotada al dominio candidato RF-first."""

    year: int
    start_date: date
    end_date_exclusive: date
    geometry_wgs84: BaseGeometry
    grid: RasterGridSpec
    source: AgriculturalCatalogSource


@dataclass(frozen=True, slots=True)
class CollectedAnnualLandCoverRaster:
    """Composición anual categórica recuperada sin persistir URL firmada."""

    content: bytes | None
    matched_scene_count: int
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class CollectedWindowRaster:
    """Raster mensual recuperado sin persistir URLs firmadas."""

    content: bytes | None
    matched_scene_count: int
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if self.matched_scene_count < 0:
            raise ValueError("matched_scene_count no puede ser negativo")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at debe incluir zona horaria")
        if self.matched_scene_count > 0 and self.content is None:
            raise ValueError("una consulta con escenas requiere contenido raster")


@dataclass(frozen=True, slots=True)
class DynamicWorldSharedEvent:
    """Evento incluido en una adquisición mensual compartida."""

    event_id: str
    geometry_wgs84: BaseGeometry


@dataclass(frozen=True, slots=True)
class DynamicWorldSharedWindowQuery:
    """Una adquisición remota mensual reutilizada por varios eventos."""

    window_id: str
    start_date: date
    end_date_exclusive: date
    geometry_wgs84: BaseGeometry
    grid: RasterGridSpec
    events: tuple[DynamicWorldSharedEvent, ...]
    event_batches: tuple[tuple[DynamicWorldSharedEvent, ...], ...]
    source: AgriculturalCatalogSource
    minimum_top1_probability: float

    def __post_init__(self) -> None:
        event_ids = tuple(event.event_id for event in self.events)
        batched_ids = tuple(event.event_id for batch in self.event_batches for event in batch)
        if not event_ids or len(event_ids) != len(set(event_ids)):
            raise ValueError("shared_window_events_empty_or_duplicate")
        if not self.event_batches or any(not batch for batch in self.event_batches):
            raise ValueError("shared_window_event_batches_empty")
        if batched_ids != event_ids:
            raise ValueError("shared_window_event_batches_must_partition_events")


@dataclass(frozen=True, slots=True)
class CollectedSharedWindowRaster:
    """Raster mensual común y conteos exactos por evento."""

    content: bytes | None
    matched_scene_counts: Mapping[str, int]
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if not self.matched_scene_counts or any(
            not event_id or count < 0 for event_id, count in self.matched_scene_counts.items()
        ):
            raise ValueError("matched_scene_counts compartidos son inválidos")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at debe incluir zona horaria")
        if any(count > 0 for count in self.matched_scene_counts.values()) and self.content is None:
            raise ValueError("una adquisición compartida con escenas requiere contenido raster")


@dataclass(frozen=True, slots=True)
class DynamicWorldSharedTemporalWindow:
    """Mes y candidatos participantes dentro de un chunk remoto."""

    window_id: str
    start_date: date
    end_date_exclusive: date
    events: tuple[DynamicWorldSharedEvent, ...]
    event_batches: tuple[tuple[DynamicWorldSharedEvent, ...], ...]

    def __post_init__(self) -> None:
        event_ids = tuple(event.event_id for event in self.events)
        batched_ids = tuple(event.event_id for batch in self.event_batches for event in batch)
        if not event_ids or len(event_ids) != len(set(event_ids)):
            raise ValueError("shared_temporal_window_events_empty_or_duplicate")
        if batched_ids != event_ids:
            raise ValueError("shared_temporal_window_batches_must_partition_events")


@dataclass(frozen=True, slots=True)
class DynamicWorldSharedChunkQuery:
    """Chunk temporal sobre una única grilla espacial compartida."""

    chunk_id: str
    geometry_wgs84: BaseGeometry
    grid: RasterGridSpec
    windows: tuple[DynamicWorldSharedTemporalWindow, ...]
    source: AgriculturalCatalogSource
    minimum_top1_probability: float

    def __post_init__(self) -> None:
        window_ids = tuple(window.window_id for window in self.windows)
        if not window_ids or len(window_ids) != len(set(window_ids)):
            raise ValueError("shared_chunk_windows_empty_or_duplicate")


@dataclass(frozen=True, slots=True)
class CollectedSharedChunkRasters:
    """Dos transportes tipados de un chunk y conteos de escenas por ventana."""

    counts_content: bytes | None
    probability_content: bytes | None
    matched_scene_counts: Mapping[str, Mapping[str, int]]
    transported_window_ids: tuple[str, ...]
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at debe incluir zona horaria")
        if (self.counts_content is None) != (self.probability_content is None):
            raise ValueError("los dos transportes del chunk deben coexistir")
        if bool(self.transported_window_ids) != (self.counts_content is not None):
            raise ValueError("transported_window_ids no coincide con los transportes")
        if any(
            not window_id
            or not counts
            or any(not event_id or count < 0 for event_id, count in counts.items())
            for window_id, counts in self.matched_scene_counts.items()
        ):
            raise ValueError("matched_scene_counts del chunk son inválidos")


RasterProvider = Callable[[DynamicWorldSharedChunkQuery], CollectedSharedChunkRasters]
AnnualLandCoverProvider = Callable[
    [DynamicWorldAnnualLandCoverQuery], CollectedAnnualLandCoverRaster
]


class AgriculturalCollectorRemoteError(RuntimeError):
    """Fallo remoto sanitizado que no propaga URLs ni credenciales."""


@dataclass(frozen=True, slots=True)
class _EventSource:
    event_id: str
    geometry: BaseGeometry
    feature: Mapping[str, Any]
    onset_end: date | None


@dataclass(frozen=True, slots=True)
class _Footprint:
    fractions: NDArray[np.float64]
    content: bytes
    exact_area_ha: float
    raster_area_ha: float
    error_area_ha: float


def load_agricultural_collector_config(path: Path) -> AgriculturalCollectorConfig:
    """Carga la configuración estricta sin acceder a fuentes remotas."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("agricultural_collector_config_root_must_be_mapping")
    return AgriculturalCollectorConfig.model_validate(payload)


def materialize_agricultural_evidence_collection(
    *,
    source_event_bundle: Path,
    output_root: Path,
    establishment_id: str,
    analysis_end_date: date,
    config_path: Path,
    catalog_path: Path,
    licenses_path: Path,
    created_at: datetime,
    raster_provider: RasterProvider,
    annual_land_cover_provider: AnnualLandCoverProvider | None = None,
    artifact_profile: ArtifactProfile = ArtifactProfile.LEAN,
) -> Path:
    """Consulta meses post-corte y publica evidencia espacial cruda por candidato."""
    artifact_profile = ArtifactProfile(artifact_profile)
    if not establishment_id.strip():
        raise ValueError("agricultural_collector_establishment_id_empty")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("agricultural_collector_created_at_requires_timezone")
    config = load_agricultural_collector_config(config_path)
    plan = build_agricultural_evidence_source_plan(load_source_catalog(catalog_path))
    source = plan.candidate_source
    if source.source_id != config.source_id:
        raise ValueError("agricultural_collector_source_config_mismatch")
    license_record = _license_for_source(source, licenses_path)
    source_bundle, source_manifest, event_collection = _load_event_bundle(source_event_bundle)
    events = _parse_events(event_collection)

    event_windows = {
        event.event_id: (
            _monthly_windows_from_observation_start(
                observation_start=config.agricultural_observation_start_date,
                analysis_end=analysis_end_date,
            )
            if config.schema_version == "1.5.0"
            else _monthly_windows(event.onset_end, analysis_end_date)
        )
        for event in events
    }
    if any(len(windows) > config.maximum_windows_per_event for windows in event_windows.values()):
        raise ValueError("agricultural_collector_window_budget_exceeded")
    query_count = sum(len(windows) for windows in event_windows.values())

    identity = {
        "source_manifest_sha256": _sha256_file(source_bundle / "json/run/manifest.json"),
        "config_sha256": _sha256_file(config_path),
        "catalog_sha256": _sha256_file(catalog_path),
        "licenses_sha256": _sha256_file(licenses_path),
        "analysis_end_date": analysis_end_date.isoformat(),
        "establishment_id": establishment_id,
    }
    analysis_id = uuid5(
        NAMESPACE_URL,
        "agricultural-collector-v6-post-cutoff-candidate-coverage-and-annual-land-cover:"
        + json.dumps(identity, sort_keys=True, separators=(",", ":")),
    )
    files: dict[str, bytes] = {}
    rows: list[dict[str, Any]] = []
    output_features: list[dict[str, Any]] = []
    query_records: list[dict[str, Any]] = []
    acquisition_records: list[dict[str, Any]] = []
    event_summaries: list[dict[str, Any]] = []
    visualizations: list[dict[str, Any]] = []
    annual_land_cover_records: list[dict[str, Any]] = []
    spatial: dict[str, tuple[RasterGridSpec, _Footprint, str, str]] = {}

    for event in events:
        windows = event_windows[event.event_id]
        if not windows:
            continue
        grid = derive_raster_grid_spec(
            aoi_wgs84=event.geometry,
            target_crs=config.target_crs,
            resolution_m=float(config.resolution_m),
            nodata=config.raster_nodata,
        )
        footprint = _event_footprint(event.geometry, grid)
        event_root = f"tiffs/evidence/agricultural/{_safe_id(event.event_id)}"
        footprint_path = f"{event_root}/event_footprint_fraction.tif"
        files[footprint_path] = footprint.content
        spatial[event.event_id] = (grid, footprint, event_root, footprint_path)

    active_events = tuple(event for event in events if event.event_id in spatial)
    annual_years = tuple(range(config.annual_land_cover_start_year, analysis_end_date.year + 1))
    spatial_groups = _plan_spatial_groups(
        active_events,
        target_crs=config.target_crs,
        resolution_m=float(config.resolution_m),
        nodata=config.raster_nodata,
        maximum_grid_pixels=config.maximum_shared_grid_pixels,
    )
    chunk_plan: list[
        tuple[int, RasterGridSpec, tuple[tuple[str, date, date, tuple[_EventSource, ...]], ...]]
    ] = []
    for group_index, (group_events, group_grid) in enumerate(spatial_groups, start=1):
        group_acquisitions = _shared_acquisition_plan(group_events, event_windows)
        for chunk in _plan_temporal_chunks(
            group_acquisitions,
            grid=group_grid,
            maximum_uncompressed_bytes=config.maximum_remote_chunk_uncompressed_bytes,
        ):
            chunk_plan.append((group_index, group_grid, chunk))
    remote_chunk_acquisition_count = len(chunk_plan)
    annual_request_count = (
        len(annual_years) if annual_land_cover_provider is not None and active_events else 0
    )
    planned_http_raster_transfer_count = remote_chunk_acquisition_count * 2 + annual_request_count
    if planned_http_raster_transfer_count > config.maximum_remote_acquisitions:
        raise ValueError("agricultural_collector_total_query_budget_exceeded")
    annual_grid: RasterGridSpec | None = None
    if active_events and annual_land_cover_provider is not None and annual_years:
        annual_geometry = unary_union([event.geometry for event in active_events])
        annual_grid = derive_raster_grid_spec(
            aoi_wgs84=annual_geometry,
            target_crs=config.target_crs,
            resolution_m=float(config.resolution_m),
            nodata=float(DYNAMIC_WORLD_ANNUAL_NODATA),
        )
        if annual_grid.width * annual_grid.height > config.maximum_shared_grid_pixels:
            raise ValueError("agricultural_collector_annual_land_cover_grid_budget_exceeded")
        annual_contents: dict[int, tuple[str, bytes]] = {}
        for year in annual_years:
            start_date = date(year, 1, 1)
            end_date_exclusive = min(date(year + 1, 1, 1), analysis_end_date + timedelta(days=1))
            if end_date_exclusive <= start_date:
                continue
            annual_query = DynamicWorldAnnualLandCoverQuery(
                year=year,
                start_date=start_date,
                end_date_exclusive=end_date_exclusive,
                geometry_wgs84=annual_geometry,
                grid=annual_grid,
                source=source,
            )
            collected_annual = annual_land_cover_provider(annual_query)
            raster_path = (
                f"tiffs/evidence/agricultural/annual_land_cover/dynamic_world_land_cover_{year}.tif"
            )
            record: dict[str, Any] = {
                "year": year,
                "start_date": start_date.isoformat(),
                "end_date_exclusive": end_date_exclusive.isoformat(),
                "matched_scene_count": collected_annual.matched_scene_count,
                "retrieved_at": collected_annual.retrieved_at.isoformat(),
                "raster_path": None,
                "raster_sha256": None,
            }
            if collected_annual.content is not None:
                normalized, validation = validate_geotiff_bytes(
                    collected_annual.content,
                    expected_grid=annual_grid,
                    expected_band_names=(DYNAMIC_WORLD_ANNUAL_LABEL_BAND,),
                    artifact_path=raster_path,
                    product="dynamic_world_annual_land_cover_mode",
                    all_nodata_band_policy="allow_for_missing_period",
                )
                _require_raster_dtype(normalized, "uint8")
                _require_dynamic_world_label_values(normalized)
                files[raster_path] = normalized
                annual_contents[year] = (raster_path, normalized)
                record.update(
                    raster_path=raster_path,
                    raster_sha256=hashlib.sha256(normalized).hexdigest(),
                    raster_validation=validation.model_dump(mode="json"),
                )
            annual_land_cover_records.append(record)
        annual_visualization = render_annual_dynamic_world_land_cover(
            raster_contents=annual_contents,
            source_label=f"Dynamic World V1 · {source.collection_id}",
        )
        if annual_visualization is not None:
            files[annual_visualization.path] = annual_visualization.content
            visualizations.append(annual_visualization.record)
    for chunk_index, (group_index, shared_grid, planned_windows) in enumerate(chunk_plan, start=1):
        temporal_windows: list[DynamicWorldSharedTemporalWindow] = []
        for window_id, start_date, end_date_exclusive, participants in planned_windows:
            shared_events = tuple(
                DynamicWorldSharedEvent(event.event_id, event.geometry) for event in participants
            )
            temporal_windows.append(
                DynamicWorldSharedTemporalWindow(
                    window_id=window_id,
                    start_date=start_date,
                    end_date_exclusive=end_date_exclusive,
                    events=shared_events,
                    event_batches=_event_batches(
                        shared_events,
                        maximum_events_per_batch=config.maximum_events_per_batch,
                    ),
                )
            )
        chunk_id = f"g{group_index:03d}-c{chunk_index:03d}"
        query = DynamicWorldSharedChunkQuery(
            chunk_id=chunk_id,
            geometry_wgs84=unary_union(
                [event.geometry_wgs84 for window in temporal_windows for event in window.events]
            ),
            grid=shared_grid,
            windows=tuple(temporal_windows),
            source=source,
            minimum_top1_probability=config.minimum_top1_probability,
        )
        collected = raster_provider(query)
        expected_counts = {
            window.window_id: {event.event_id for event in window.events}
            for window in temporal_windows
        }
        if set(collected.matched_scene_counts) != set(expected_counts) or any(
            set(collected.matched_scene_counts[window_id]) != participant_ids
            for window_id, participant_ids in expected_counts.items()
        ):
            raise ValueError("agricultural_collector_shared_scene_counts_mismatch")
        expected_transported = tuple(
            window.window_id
            for window in temporal_windows
            if any(collected.matched_scene_counts[window.window_id].values())
        )
        if collected.transported_window_ids != expected_transported:
            raise ValueError("agricultural_collector_chunk_transported_windows_mismatch")

        counts_path = f"tiffs/evidence/agricultural/_shared/{chunk_id}_counts_uint16.tif"
        probability_path = f"tiffs/evidence/agricultural/_shared/{chunk_id}_probability.tif"
        normalized_counts: bytes | None = None
        normalized_probability: bytes | None = None
        count_validation: Any = None
        probability_validation: Any = None
        if expected_transported:
            assert collected.counts_content is not None
            assert collected.probability_content is not None
            count_names = _chunk_count_band_names(expected_transported)
            probability_names = _chunk_probability_band_names(expected_transported)
            normalized_counts, count_validation = validate_geotiff_bytes(
                collected.counts_content,
                expected_grid=_grid_with_nodata(shared_grid, TEMPORAL_COUNT_CUBE_NODATA),
                expected_band_names=count_names,
                artifact_path=counts_path,
                product="dynamic_world_shared_temporal_counts",
                all_nodata_band_policy="allow_for_missing_period",
            )
            normalized_probability, probability_validation = validate_geotiff_bytes(
                collected.probability_content,
                expected_grid=shared_grid,
                expected_band_names=probability_names,
                artifact_path=probability_path,
                product="dynamic_world_shared_temporal_probability",
                all_nodata_band_policy="allow_for_missing_period",
            )
            _require_raster_dtype(normalized_counts, "uint16")
            _require_raster_dtype(normalized_probability, "float32")
            if artifact_profile == ArtifactProfile.DEBUG:
                files[counts_path] = normalized_counts
                files[probability_path] = normalized_probability

        active_window_count = len(expected_transported)
        legacy_bytes = shared_grid.width * shared_grid.height * active_window_count * 16
        new_bytes = shared_grid.width * shared_grid.height * active_window_count * 8
        acquisition_records.append(
            {
                "acquisition_id": f"dynamic-world-{chunk_id}",
                "chunk_id": chunk_id,
                "spatial_group_id": f"g{group_index:03d}",
                "window_ids": [window.window_id for window in temporal_windows],
                "transported_window_ids": list(expected_transported),
                "windows": [
                    {
                        "window_id": window.window_id,
                        "start_date": window.start_date.isoformat(),
                        "end_date_exclusive": window.end_date_exclusive.isoformat(),
                        "event_ids": [event.event_id for event in window.events],
                        "matched_scene_counts": dict(
                            collected.matched_scene_counts[window.window_id]
                        ),
                        "event_batches": [
                            {
                                "batch_id": (
                                    f"dynamic-world-{chunk_id}-{window.window_id}-"
                                    f"batch-{batch_index:03d}"
                                ),
                                "event_ids": [event.event_id for event in batch],
                                "event_count": len(batch),
                            }
                            for batch_index, batch in enumerate(window.event_batches, start=1)
                        ],
                    }
                    for window in temporal_windows
                ],
                "event_count": len(
                    {event.event_id for window in temporal_windows for event in window.events}
                ),
                "batch_count": sum(len(window.event_batches) for window in temporal_windows),
                "retrieved_at": collected.retrieved_at.isoformat(),
                "grid_sha256": shared_grid.grid_sha256,
                "counts_raster_path": (
                    counts_path
                    if expected_transported and artifact_profile == ArtifactProfile.DEBUG
                    else None
                ),
                "probability_raster_path": (
                    probability_path
                    if expected_transported and artifact_profile == ArtifactProfile.DEBUG
                    else None
                ),
                "rasters_retained": bool(
                    expected_transported and artifact_profile == ArtifactProfile.DEBUG
                ),
                "counts_raster_sha256": (
                    hashlib.sha256(normalized_counts).hexdigest()
                    if normalized_counts is not None
                    else None
                ),
                "probability_raster_sha256": (
                    hashlib.sha256(normalized_probability).hexdigest()
                    if normalized_probability is not None
                    else None
                ),
                "counts_raster_validation": (
                    count_validation.model_dump(mode="json")
                    if count_validation is not None
                    else None
                ),
                "probability_raster_validation": (
                    probability_validation.model_dump(mode="json")
                    if probability_validation is not None
                    else None
                ),
                "http_raster_transfer_count": 2 if expected_transported else 0,
                "estimated_uncompressed_bytes_legacy": legacy_bytes,
                "estimated_uncompressed_bytes_new": new_bytes,
            }
        )
        for window in temporal_windows:
            window_id = window.window_id
            start_date = window.start_date
            end_date_exclusive = window.end_date_exclusive
            for shared_event in window.events:
                event = next(
                    item for item in active_events if item.event_id == shared_event.event_id
                )
                grid, footprint, event_root, footprint_path = spatial[event.event_id]
                matched_scene_count = collected.matched_scene_counts[window_id][event.event_id]
                raster_path = f"{event_root}/{window_id}_dynamic_world.tif"
                raw = (
                    _extract_shared_chunk_window_to_event_grid(
                        counts_content=normalized_counts,
                        probability_content=normalized_probability,
                        window_id=window_id,
                        event_grid=grid,
                    )
                    if window_id in expected_transported
                    else _empty_dynamic_world_raster(grid)
                )
                normalized, validation = validate_geotiff_bytes(
                    raw,
                    expected_grid=grid,
                    expected_band_names=DYNAMIC_WORLD_RASTER_BANDS,
                    artifact_path=raster_path,
                    product="dynamic_world_monthly_crop_support",
                    all_nodata_band_policy="allow_for_missing_period",
                )
                files[raster_path] = normalized
                window_rows = _summarize_window(
                    content=normalized,
                    footprint=footprint,
                    config=config,
                    event=event,
                    window_id=window_id,
                    start_date=start_date,
                    end_date_exclusive=end_date_exclusive,
                    matched_scene_count=matched_scene_count,
                    raster_path=raster_path,
                    footprint_path=footprint_path,
                    grid=grid,
                )
                rows.extend(window_rows)
                query_records.append(
                    {
                        "event_id": event.event_id,
                        "window_id": window_id,
                        "start_date": start_date.isoformat(),
                        "end_date_exclusive": end_date_exclusive.isoformat(),
                        "matched_scene_count": matched_scene_count,
                        "retrieved_at": collected.retrieved_at.isoformat(),
                        "shared_acquisition_id": f"dynamic-world-{chunk_id}",
                        "shared_counts_raster_path": (
                            counts_path
                            if expected_transported and artifact_profile == ArtifactProfile.DEBUG
                            else None
                        ),
                        "shared_probability_raster_path": (
                            probability_path
                            if expected_transported and artifact_profile == ArtifactProfile.DEBUG
                            else None
                        ),
                        "grid_sha256": grid.grid_sha256,
                        "raster_path": raster_path,
                        "raster_sha256": hashlib.sha256(normalized).hexdigest(),
                        "raster_validation": validation.model_dump(mode="json"),
                    }
                )

    for event in events:
        windows = event_windows[event.event_id]
        if config.schema_version != "1.5.0" and event.onset_end is None:
            summary = _event_summary(
                event,
                status="insufficient_onset",
                windows=0,
                quality_flags=["estimated_onset_window_end_missing"],
            )
        elif not windows:
            summary = _event_summary(
                event,
                status="insufficient_post_onset_window",
                windows=0,
                quality_flags=[
                    "analysis_end_before_agricultural_observation_start"
                    if config.schema_version == "1.5.0"
                    else "analysis_end_not_after_estimated_onset"
                ],
            )
        else:
            grid, footprint, _, _ = spatial[event.event_id]
            summary = _event_summary(
                event,
                status="collected",
                windows=len(windows),
                quality_flags=[],
                footprint=footprint,
                grid=grid,
            )
        event_summaries.append(summary)
        output_features.append(_output_feature(event, summary))

    for event in events:
        event_rows = [row for row in rows if row["event_id"] == event.event_id]
        if not event_rows:
            continue
        footprint_path = str(event_rows[0]["event_footprint_path"])
        raster_paths = sorted({str(row["raster_path"]) for row in event_rows})
        visualization = render_monthly_agricultural_evidence(
            event_id=event.event_id,
            rows=event_rows,
            raster_contents={path: files[path] for path in raster_paths},
            footprint_content=files[footprint_path],
            footprint_path=footprint_path,
            source_label=f"Dynamic World V1 · {source.collection_id}",
        )
        if visualization is not None:
            files[visualization.path] = visualization.content
            visualizations.append(visualization.record)

    if artifact_profile == ArtifactProfile.LEAN:
        retained_monthly_paths: set[str] = set()
        for event in events:
            event_rows = [row for row in rows if row["event_id"] == event.event_id]
            if not event_rows:
                continue
            selected_windows = set(select_monthly_evidence_windows(event_rows))
            retained_monthly_paths.update(
                str(row["raster_path"])
                for row in event_rows
                if str(row["window_id"]) in selected_windows
            )
            cube_path = (
                f"tiffs/evidence/agricultural/{_safe_id(event.event_id)}/temporal_counts_uint16.tif"
            )
            cube_content, band_pairs = _compact_temporal_count_cube(
                event_rows=event_rows,
                raster_contents=files,
            )
            files[cube_path] = cube_content
            for row in event_rows:
                valid_band, qualifying_band = band_pairs[str(row["window_id"])]
                row["raster_path"] = cube_path
                row["valid_count_band"] = valid_band
                row["qualifying_count_band"] = qualifying_band
            for query_record in query_records:
                if query_record["event_id"] != event.event_id:
                    continue
                original_path = str(query_record["raster_path"])
                query_record["raster_retained"] = original_path in retained_monthly_paths
                query_record["raster_path"] = (
                    original_path if original_path in retained_monthly_paths else None
                )
                query_record["temporal_cube_path"] = cube_path
                valid_band, qualifying_band = band_pairs[str(query_record["window_id"])]
                query_record["valid_count_band"] = valid_band
                query_record["qualifying_count_band"] = qualifying_band
        monthly_prefix = "tiffs/evidence/agricultural/"
        for path in tuple(files):
            if (
                path.startswith(monthly_prefix)
                and path.endswith("_dynamic_world.tif")
                and "/_shared/" not in path
                and path not in retained_monthly_paths
            ):
                del files[path]

    source_identity = {
        "source_id": source.source_id,
        "provider": source.provider,
        "collection": source.collection_id,
        "version": source.version,
        "license": source.license,
        "access_date": license_record.access_date.isoformat(),
        "attribution": license_record.attribution,
        "restrictions": list(license_record.restrictions),
        "evidence_role": source.evidence_role,
        "sensor_family": source.sensor_family,
        "upstream_dataset_ids": list(source.upstream_dataset_ids),
        "spatial_resolution_m": source.spatial_resolution_m,
        "availability_start": source.availability_start.isoformat(),
        "availability_end": None,
    }
    payload = {
        "schema_version": AGRICULTURAL_COLLECTION_SCHEMA_VERSION,
        "analysis_id": str(analysis_id),
        "establishment_id": establishment_id,
        "created_at": created_at.isoformat(),
        "analysis_end_date": analysis_end_date.isoformat(),
        "status": "collected" if query_count else "insufficient_data",
        "attribution_generated": False,
        "persistence_evaluated": False,
        "events": event_summaries,
        "rows": rows,
    }
    payload = AgriculturalCollectionDocument.model_validate(payload).model_dump(mode="json")
    metadata = {
        "schema_version": AGRICULTURAL_COLLECTION_SCHEMA_VERSION,
        "analysis_id": str(analysis_id),
        "method": "dynamic_world_spatial_temporal_chunk_collector_v5",
        "source": source_identity,
        "configuration": config.model_dump(mode="json"),
        "spatial_method": {
            "target_crs": config.target_crs,
            "resolution_m": config.resolution_m,
            "resampling": config.resampling,
            "event_footprint": config.event_footprint_method,
            "area_method": "sum_pixel_area_times_exact_event_footprint_fraction",
            "area_unit": "hectare",
            "shared_acquisition_grids": [
                {
                    "spatial_group_id": f"g{index:03d}",
                    "event_ids": [event.event_id for event in group],
                    "grid": grid.model_dump(mode="json"),
                }
                for index, (group, grid) in enumerate(spatial_groups, start=1)
            ],
            "event_raster_derivation": "aligned_nearest_window_from_typed_temporal_chunk",
            "lean_temporal_storage": (
                "uint16_valid_and_qualifying_count_cube_per_event"
                if artifact_profile == ArtifactProfile.LEAN
                else "monthly_float32_event_rasters"
            ),
        },
        "temporal_method": {
            "cadence": config.temporal_cadence,
            "window_start": config.agricultural_observation_start_date.isoformat(),
            "window_start_semantics": "first_day_after_eudr_cutoff",
            "analysis_end_inclusive": analysis_end_date.isoformat(),
            "persistence_evaluated": False,
        },
        "annual_land_cover": {
            "domain": "union_of_rf_first_candidate_footprints",
            "composite": config.annual_land_cover_composite,
            "start_year": config.annual_land_cover_start_year,
            "end_date_inclusive": analysis_end_date.isoformat(),
            "label_band": DYNAMIC_WORLD_ANNUAL_LABEL_BAND,
            "nodata": DYNAMIC_WORLD_ANNUAL_NODATA,
            "grid": annual_grid.model_dump(mode="json") if annual_grid is not None else None,
            "rasters": annual_land_cover_records,
            "interpretation": "descriptive_land_cover_context_not_conversion_evidence",
            "reference": "Brown et al. 2022; doi:10.1038/s41597-022-01307-4",
        },
        "query_count": len(query_records),
        "queries": query_records,
        "remote_acquisition_count": len(annual_land_cover_records)
        + sum(
            int(acquisition["http_raster_transfer_count"]) for acquisition in acquisition_records
        ),
        "remote_chunk_acquisition_count": len(acquisition_records),
        "planned_max_http_raster_transfer_count": planned_http_raster_transfer_count,
        "http_raster_transfer_count": sum(
            int(acquisition["http_raster_transfer_count"]) for acquisition in acquisition_records
        ),
        "estimated_uncompressed_bytes_legacy": sum(
            int(acquisition["estimated_uncompressed_bytes_legacy"])
            for acquisition in acquisition_records
        ),
        "estimated_uncompressed_bytes_new": sum(
            int(acquisition["estimated_uncompressed_bytes_new"])
            for acquisition in acquisition_records
        ),
        "estimated_legacy_http_raster_transfer_count": sum(
            len(cast(list[str], acquisition["transported_window_ids"]))
            for acquisition in acquisition_records
        ),
        "event_batch_count": sum(
            int(acquisition["batch_count"]) for acquisition in acquisition_records
        ),
        "event_window_reuse_ratio": (
            len(query_records) / len(acquisition_records) if acquisition_records else 0.0
        ),
        "remote_query_budget_semantics": "maximum_planned_http_raster_transfers",
        "logical_query_semantics": "event_window_products",
        "event_batch_budget_semantics": ("maximum_events_per_shared_month_scene_count_batch"),
        "artifact_profile": artifact_profile.value,
        "shared_acquisitions": acquisition_records,
        "signed_urls_persisted": False,
        "visualizations": visualizations,
        "limitations": [
            *source.limitations,
            "The collector reports monthly spatial support but does not yet evaluate "
            "multi-month persistence.",
            "Unknown valid cover is kept separate from nodata and is not agricultural evidence.",
        ],
    }
    files.update(
        {
            "tables/evidence/agricultural_evidence.csv": _rows_csv(rows),
            "json/evidence/agricultural_evidence.geojson": _json_bytes(
                {"type": "FeatureCollection", "features": output_features}
            ),
            "json/evidence/agricultural_evidence.json": _json_bytes(payload),
            "json/evidence/agricultural_evidence_metadata.json": _json_bytes(metadata),
            "json/run/summary.json": _json_bytes({**payload, "rows": []}),
        }
    )
    source_event_identity = {
        "analysis_id": source_manifest.get("analysis_id"),
        "created_at": source_manifest.get("created_at"),
        "input_sha256": source_manifest.get("input_sha256"),
        "manifest_sha256": identity["source_manifest_sha256"],
        "bundle_name": source_bundle.name,
        "event_artifact_path": _EVENT_ARTIFACT,
        "event_artifact_sha256": _sha256_file(source_bundle / _EVENT_ARTIFACT),
    }
    run_name = (
        f"{_safe_id(establishment_id)}-agriculture-v2__"
        f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}__{str(analysis_id)[:8]}"
    )
    run_directory = output_root.resolve() / run_name
    _publish_bundle(
        run_directory=run_directory,
        files=files,
        manifest_metadata={
            "schema_version": AGRICULTURAL_COLLECTION_BUNDLE_SCHEMA_VERSION,
            "analysis_id": str(analysis_id),
            "establishment_id": establishment_id,
            "created_at": created_at.isoformat(),
            "analysis_end_date": analysis_end_date.isoformat(),
            "input_sha256": source_manifest.get("input_sha256"),
            "source_event_bundle": source_event_identity,
            "config_sha256": identity["config_sha256"],
            "catalog_sha256": identity["catalog_sha256"],
            "licenses_sha256": identity["licenses_sha256"],
            "query_count": len(query_records),
            "remote_acquisition_count": metadata["remote_acquisition_count"],
            "remote_chunk_acquisition_count": len(acquisition_records),
            "planned_max_http_raster_transfer_count": planned_http_raster_transfer_count,
            "http_raster_transfer_count": metadata["http_raster_transfer_count"],
            "estimated_uncompressed_bytes_legacy": metadata["estimated_uncompressed_bytes_legacy"],
            "estimated_uncompressed_bytes_new": metadata["estimated_uncompressed_bytes_new"],
            "event_batch_count": sum(
                int(acquisition["batch_count"]) for acquisition in acquisition_records
            ),
            "artifact_profile": artifact_profile.value,
            "signed_urls_persisted": False,
            "persistence_evaluated": False,
            "visualizations": visualizations,
        },
        remote_data_accessed=bool(acquisition_records),
        datasets=(source_identity,),
    )
    return run_directory


@dataclass(frozen=True, slots=True)
class DynamicWorldGeeRasterProvider:
    """Provider productivo único para métricas mensuales y contexto anual."""

    session: GeeSession
    output_config: OutputConfig
    fetch_bytes: FetchBytes | None = None

    def __call__(self, query: DynamicWorldSharedChunkQuery) -> CollectedSharedChunkRasters:
        return collect_dynamic_world_shared_chunk(
            query=query,
            session=self.session,
            output_config=self.output_config,
            fetch_bytes=self.fetch_bytes,
        )

    def collect_annual_land_cover(
        self, query: DynamicWorldAnnualLandCoverQuery
    ) -> CollectedAnnualLandCoverRaster:
        return collect_dynamic_world_annual_land_cover(
            query=query,
            session=self.session,
            output_config=self.output_config,
            fetch_bytes=self.fetch_bytes,
        )


def make_dynamic_world_raster_provider(
    *,
    session: GeeSession,
    output_config: OutputConfig,
    fetch_bytes: FetchBytes | None = None,
) -> RasterProvider:
    """Construye el provider productivo GEE sin exponer la sesión en artefactos."""

    return DynamicWorldGeeRasterProvider(
        session=session,
        output_config=output_config,
        fetch_bytes=fetch_bytes,
    )


def collect_dynamic_world_annual_land_cover(
    *,
    query: DynamicWorldAnnualLandCoverQuery,
    session: GeeSession,
    output_config: OutputConfig,
    fetch_bytes: FetchBytes | None = None,
    retrieved_at: datetime | None = None,
) -> CollectedAnnualLandCoverRaster:
    """Descarga la moda anual de ``label`` siguiendo el compuesto oficial documentado."""
    module = session.module
    timestamp = retrieved_at or datetime.now(UTC)
    try:
        remote_geometry = module.Geometry(mapping(query.geometry_wgs84))
        collection = (
            module.ImageCollection(query.source.collection_id)
            .filterBounds(remote_geometry)
            .filterDate(query.start_date.isoformat(), query.end_date_exclusive.isoformat())
        )
        matched_scene_count = int(collection.size().getInfo())
        if matched_scene_count == 0:
            return CollectedAnnualLandCoverRaster(None, 0, timestamp)
        image = (
            collection.select("label")
            .mode()
            .rename(DYNAMIC_WORLD_ANNUAL_LABEL_BAND)
            .clip(remote_geometry)
        )
        artifact_path = (
            "tiffs/evidence/agricultural/annual_land_cover/"
            f"dynamic_world_land_cover_{query.year}.tif"
        )
        materialized = materialize_ee_image_to_grid(
            image=image,
            band_names=(DYNAMIC_WORLD_ANNUAL_LABEL_BAND,),
            artifact_path=artifact_path,
            download_name=f"dynamic_world_annual_land_cover_{query.year}",
            output_config=output_config,
            grid_spec=query.grid,
            output_type="uint8",
            nodata_override=float(DYNAMIC_WORLD_ANNUAL_NODATA),
            fetch_bytes=fetch_bytes,
            all_nodata_band_policy="allow_for_missing_period",
        )
        return CollectedAnnualLandCoverRaster(
            content=materialized.content,
            matched_scene_count=matched_scene_count,
            retrieved_at=timestamp,
        )
    except (RasterDownloadError, AgriculturalCollectorRemoteError):
        raise
    except Exception as exc:
        raise AgriculturalCollectorRemoteError(
            "dynamic_world_annual_land_cover_query_failed"
        ) from exc


def collect_dynamic_world_shared_chunk(
    *,
    query: DynamicWorldSharedChunkQuery,
    session: GeeSession,
    output_config: OutputConfig,
    fetch_bytes: FetchBytes | None = None,
    retrieved_at: datetime | None = None,
) -> CollectedSharedChunkRasters:
    """Descarga conteos uint16 y probabilidades float32 para varios meses."""
    module = session.module
    timestamp = retrieved_at or datetime.now(UTC)
    matched_scene_counts: dict[str, dict[str, int]] = {}
    transported_windows: list[str] = []
    counts_image: Any | None = None
    probability_image: Any | None = None
    try:
        eligible_rule = _automatic_gate_eligible_rule(query.source)
        remote_geometry = module.Geometry(mapping(query.geometry_wgs84))
        for window in query.windows:
            collection = (
                module.ImageCollection(query.source.collection_id)
                .filterBounds(remote_geometry)
                .filterDate(window.start_date.isoformat(), window.end_date_exclusive.isoformat())
            )
            window_counts: dict[str, int] = {}
            for batch in window.event_batches:
                batch_counts = _shared_event_scene_counts(
                    module=module,
                    collection=collection,
                    events=batch,
                )
                if set(window_counts).intersection(batch_counts):
                    raise AgriculturalCollectorRemoteError(
                        "dynamic_world_shared_scene_counts_duplicate"
                    )
                window_counts.update(batch_counts)
            if set(window_counts) != {event.event_id for event in window.events}:
                raise AgriculturalCollectorRemoteError(
                    "dynamic_world_shared_scene_counts_incomplete"
                )
            matched_scene_counts[window.window_id] = window_counts
            if not any(window_counts.values()):
                continue
            valid_count, qualifying_count, mean_probability = _dynamic_world_metric_components(
                module=module,
                collection=collection,
                minimum_top1_probability=query.minimum_top1_probability,
                probability_band=eligible_rule.source_probability_band,
                label_value=eligible_rule.source_label_value,
            )
            count_pair = valid_count.rename(f"{window.window_id}_valid_observation_count").addBands(
                qualifying_count.rename(f"{window.window_id}_qualifying_crop_count")
            )
            probability = mean_probability.rename(f"{window.window_id}_mean_crop_probability")
            counts_image = count_pair if counts_image is None else counts_image.addBands(count_pair)
            probability_image = (
                probability
                if probability_image is None
                else probability_image.addBands(probability)
            )
            transported_windows.append(window.window_id)
        if not transported_windows:
            return CollectedSharedChunkRasters(
                counts_content=None,
                probability_content=None,
                matched_scene_counts=matched_scene_counts,
                transported_window_ids=(),
                retrieved_at=timestamp,
            )
        count_names = _chunk_count_band_names(transported_windows)
        probability_names = _chunk_probability_band_names(transported_windows)
        counts_path = f"tiffs/evidence/agricultural/_shared/{query.chunk_id}_counts_uint16.tif"
        probability_path = f"tiffs/evidence/agricultural/_shared/{query.chunk_id}_probability.tif"
        counts = materialize_ee_image_to_grid(
            image=counts_image,
            band_names=count_names,
            artifact_path=counts_path,
            download_name=f"dynamic_world_{query.chunk_id}_counts",
            output_config=output_config,
            grid_spec=query.grid,
            output_type="uint16",
            nodata_override=float(TEMPORAL_COUNT_CUBE_NODATA),
            fetch_bytes=fetch_bytes,
            all_nodata_band_policy="allow_for_missing_period",
        )
        probability = materialize_ee_image_to_grid(
            image=probability_image,
            band_names=probability_names,
            artifact_path=probability_path,
            download_name=f"dynamic_world_{query.chunk_id}_probability",
            output_config=output_config,
            grid_spec=query.grid,
            output_type="float32",
            fetch_bytes=fetch_bytes,
            all_nodata_band_policy="allow_for_missing_period",
        )
        return CollectedSharedChunkRasters(
            counts_content=counts.content,
            probability_content=probability.content,
            matched_scene_counts=matched_scene_counts,
            transported_window_ids=tuple(transported_windows),
            retrieved_at=timestamp,
        )
    except (RasterDownloadError, AgriculturalCollectorRemoteError):
        raise
    except Exception as exc:
        raise AgriculturalCollectorRemoteError("dynamic_world_shared_chunk_query_failed") from exc


def collect_dynamic_world_shared_window(
    *,
    query: DynamicWorldSharedWindowQuery,
    session: GeeSession,
    output_config: OutputConfig,
    fetch_bytes: FetchBytes | None = None,
    retrieved_at: datetime | None = None,
) -> CollectedSharedWindowRaster:
    """Descarga una vez el mes y obtiene conteos por evento en lotes acotados."""
    module = session.module
    timestamp = retrieved_at or datetime.now(UTC)
    try:
        eligible_rule = _automatic_gate_eligible_rule(query.source)
        remote_geometry = module.Geometry(mapping(query.geometry_wgs84))
        collection = (
            module.ImageCollection(query.source.collection_id)
            .filterBounds(remote_geometry)
            .filterDate(query.start_date.isoformat(), query.end_date_exclusive.isoformat())
        )
        matched_scene_counts: dict[str, int] = {}
        for batch in query.event_batches:
            batch_counts = _shared_event_scene_counts(
                module=module,
                collection=collection,
                events=batch,
            )
            if set(matched_scene_counts).intersection(batch_counts):
                raise AgriculturalCollectorRemoteError(
                    "dynamic_world_shared_scene_counts_duplicate"
                )
            matched_scene_counts.update(batch_counts)
        if set(matched_scene_counts) != {event.event_id for event in query.events}:
            raise AgriculturalCollectorRemoteError("dynamic_world_shared_scene_counts_incomplete")
        if not any(matched_scene_counts.values()):
            return CollectedSharedWindowRaster(
                content=None,
                matched_scene_counts=matched_scene_counts,
                retrieved_at=timestamp,
            )
        image = _dynamic_world_metrics_image(
            module=module,
            collection=collection,
            minimum_top1_probability=query.minimum_top1_probability,
            probability_band=eligible_rule.source_probability_band,
            label_value=eligible_rule.source_label_value,
        )
        artifact_path = f"tiffs/evidence/agricultural/_shared/{query.window_id}_dynamic_world.tif"
        materialized = materialize_ee_image_to_grid(
            image=image,
            band_names=DYNAMIC_WORLD_RASTER_BANDS,
            artifact_path=artifact_path,
            download_name=f"dynamic_world_shared_{query.window_id}",
            output_config=output_config,
            grid_spec=query.grid,
            output_type="float32",
            fetch_bytes=fetch_bytes,
        )
        return CollectedSharedWindowRaster(
            content=materialized.content,
            matched_scene_counts=matched_scene_counts,
            retrieved_at=timestamp,
        )
    except (RasterDownloadError, AgriculturalCollectorRemoteError):
        raise
    except Exception as exc:
        raise AgriculturalCollectorRemoteError("dynamic_world_shared_window_query_failed") from exc


def _shared_event_scene_counts(
    *,
    module: Any,
    collection: Any,
    events: Sequence[DynamicWorldSharedEvent],
) -> dict[str, int]:
    features = module.FeatureCollection(
        [
            module.Feature(
                module.Geometry(mapping(event.geometry_wgs84)),
                {"event_id": event.event_id},
            )
            for event in events
        ]
    )

    def attach_count(feature: Any) -> Any:
        candidate = module.Feature(feature)
        return candidate.set(
            "matched_scene_count",
            collection.filterBounds(candidate.geometry()).size(),
        )

    evaluated = features.map(attach_count).getInfo()
    raw_features = evaluated.get("features") if isinstance(evaluated, dict) else None
    if not isinstance(raw_features, list):
        raise AgriculturalCollectorRemoteError("dynamic_world_shared_scene_counts_invalid")
    counts: dict[str, int] = {}
    for raw in raw_features:
        properties = raw.get("properties") if isinstance(raw, dict) else None
        event_id = properties.get("event_id") if isinstance(properties, dict) else None
        count = properties.get("matched_scene_count") if isinstance(properties, dict) else None
        if not isinstance(event_id, str) or not isinstance(count, int) or count < 0:
            raise AgriculturalCollectorRemoteError("dynamic_world_shared_scene_counts_invalid")
        if event_id in counts:
            raise AgriculturalCollectorRemoteError("dynamic_world_shared_scene_counts_duplicate")
        counts[event_id] = count
    if set(counts) != {event.event_id for event in events}:
        raise AgriculturalCollectorRemoteError("dynamic_world_shared_scene_counts_incomplete")
    return counts


def _dynamic_world_metrics_image(
    *,
    module: Any,
    collection: Any,
    minimum_top1_probability: float,
    probability_band: str,
    label_value: int,
) -> Any:
    valid_count, qualifying_count, mean_probability = _dynamic_world_metric_components(
        module=module,
        collection=collection,
        minimum_top1_probability=minimum_top1_probability,
        probability_band=probability_band,
        label_value=label_value,
    )
    observation_fraction = qualifying_count.divide(valid_count).rename(
        DYNAMIC_WORLD_RASTER_BANDS[3]
    )
    return (
        valid_count.addBands(qualifying_count)
        .addBands(mean_probability)
        .addBands(observation_fraction)
        .updateMask(valid_count.gt(0))
        .toFloat()
    )


def _dynamic_world_metric_components(
    *,
    module: Any,
    collection: Any,
    minimum_top1_probability: float,
    probability_band: str,
    label_value: int,
) -> tuple[Any, Any, Any]:
    valid_count = collection.select(probability_band).count().rename(DYNAMIC_WORLD_RASTER_BANDS[0])

    def qualifying_crop(image: Any) -> Any:
        candidate = module.Image(image)
        return (
            candidate.select("label")
            .eq(label_value)
            .And(candidate.select(probability_band).gte(minimum_top1_probability))
            .rename("qualifying_crop")
        )

    qualifying_count = collection.map(qualifying_crop).sum().rename(DYNAMIC_WORLD_RASTER_BANDS[1])
    mean_probability = (
        collection.select(probability_band).mean().rename(DYNAMIC_WORLD_RASTER_BANDS[2])
    )
    return valid_count, qualifying_count, mean_probability


def collect_dynamic_world_window(
    *,
    query: DynamicWorldWindowQuery,
    session: GeeSession,
    output_config: OutputConfig,
    fetch_bytes: FetchBytes | None = None,
    retrieved_at: datetime | None = None,
) -> CollectedWindowRaster:
    """Consulta una colección mensual y descarga cuatro métricas reproducibles."""
    module = session.module
    timestamp = retrieved_at or datetime.now(UTC)
    try:
        eligible_rule = _automatic_gate_eligible_rule(query.source)
        remote_geometry = module.Geometry(mapping(query.geometry_wgs84))
        collection = (
            module.ImageCollection(query.source.collection_id)
            .filterBounds(remote_geometry)
            .filterDate(query.start_date.isoformat(), query.end_date_exclusive.isoformat())
        )
        matched_scene_count = int(collection.size().getInfo())
        if matched_scene_count == 0:
            return CollectedWindowRaster(
                content=None,
                matched_scene_count=0,
                retrieved_at=timestamp,
            )

        image = _dynamic_world_metrics_image(
            module=module,
            collection=collection,
            minimum_top1_probability=query.minimum_top1_probability,
            probability_band=eligible_rule.source_probability_band,
            label_value=eligible_rule.source_label_value,
        )
        artifact_path = (
            f"tiffs/evidence/agricultural/{_safe_id(query.event_id)}/"
            f"{query.window_id}_dynamic_world.tif"
        )
        materialized = materialize_ee_image_to_grid(
            image=image,
            band_names=DYNAMIC_WORLD_RASTER_BANDS,
            artifact_path=artifact_path,
            download_name=f"dynamic_world_{_safe_id(query.event_id)}_{query.window_id}",
            output_config=output_config,
            grid_spec=query.grid,
            output_type="float32",
            fetch_bytes=fetch_bytes,
        )
        return CollectedWindowRaster(
            content=materialized.content,
            matched_scene_count=matched_scene_count,
            retrieved_at=timestamp,
        )
    except (RasterDownloadError, AgriculturalCollectorRemoteError):
        raise
    except Exception as exc:
        raise AgriculturalCollectorRemoteError("dynamic_world_window_query_failed") from exc


def _automatic_gate_eligible_rule(source: AgriculturalCatalogSource) -> AgriculturalClassRule:
    eligible = tuple(rule for rule in source.class_rules if rule.automatic_gate_eligible)
    if len(eligible) != 1:
        raise ValueError("agricultural_catalog_automatic_gate_rule_missing_or_ambiguous")
    return eligible[0]


def _license_for_source(source: AgriculturalCatalogSource, licenses_path: Path) -> Any:
    registry = load_license_registry(licenses_path)
    matches = [item for item in registry.datasets if item.dataset_id == source.source_id]
    if len(matches) != 1:
        raise ValueError("agricultural_collector_license_record_missing_or_duplicate")
    record = matches[0]
    if (
        record.provider != source.provider
        or record.collection != source.collection_id
        or record.version != source.version
        or record.license != source.license
    ):
        raise ValueError("agricultural_collector_catalog_license_mismatch")
    return record


def _load_event_bundle(bundle: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    resolved = bundle.resolve()
    manifest_path = resolved / "json/run/manifest.json"
    if not resolved.is_dir() or not manifest_path.is_file():
        raise ValueError("source_event_bundle_manifest_missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), list):
        raise ValueError("source_event_bundle_manifest_invalid")
    manifested: dict[str, Path] = {}
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict):
            raise ValueError("source_event_bundle_artifact_invalid")
        relative_text = artifact.get("path")
        if not isinstance(relative_text, str):
            raise ValueError("source_event_bundle_artifact_path_invalid")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts or relative_text in manifested:
            raise ValueError("source_event_bundle_artifact_path_unsafe_or_duplicate")
        path = (resolved / relative).resolve()
        if not path.is_relative_to(resolved) or not path.is_file():
            raise ValueError("source_event_bundle_artifact_missing")
        if path.stat().st_size != artifact.get("size_bytes"):
            raise ValueError("source_artifact_size_mismatch")
        if _sha256_file(path) != artifact.get("sha256"):
            raise ValueError("source_artifact_sha256_mismatch")
        manifested[relative_text] = path
    if _EVENT_ARTIFACT not in manifested:
        raise ValueError("source_event_geojson_not_manifested")
    collection = json.loads(manifested[_EVENT_ARTIFACT].read_text(encoding="utf-8"))
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValueError("source_event_geojson_invalid")
    return resolved, manifest, collection


def _parse_events(collection: Mapping[str, Any]) -> tuple[_EventSource, ...]:
    features = collection.get("features")
    if not isinstance(features, list):
        raise ValueError("source_event_features_invalid")
    events: list[_EventSource] = []
    identifiers: set[str] = set()
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise ValueError("source_event_feature_invalid")
        properties = cast(dict[str, Any], feature["properties"])
        event_id = str(properties.get("event_id", "")).strip()
        if not event_id or event_id in identifiers:
            raise ValueError("source_event_id_empty_or_duplicate")
        geometry_payload = feature.get("geometry")
        if not isinstance(geometry_payload, dict):
            raise ValueError("source_event_geometry_missing")
        geometry = shape(geometry_payload)
        if geometry.geom_type not in {"Polygon", "MultiPolygon"} or not geometry.is_valid:
            raise ValueError("source_event_geometry_invalid")
        if geometry.is_empty or not _bounds_are_wgs84(geometry.bounds):
            raise ValueError("source_event_geometry_outside_wgs84")
        onset_raw = properties.get("estimated_onset_window_end")
        onset = date.fromisoformat(onset_raw) if isinstance(onset_raw, str) else None
        identifiers.add(event_id)
        events.append(
            _EventSource(
                event_id=event_id,
                geometry=geometry,
                feature=feature,
                onset_end=onset,
            )
        )
    return tuple(events)


def _monthly_windows(
    onset_end: date | None,
    analysis_end: date,
) -> tuple[tuple[str, date, date], ...]:
    if onset_end is None or analysis_end <= onset_end:
        return ()
    cursor = onset_end + timedelta(days=1)
    final_exclusive = analysis_end + timedelta(days=1)
    windows: list[tuple[str, date, date]] = []
    while cursor < final_exclusive:
        next_month = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
        end = min(next_month, final_exclusive)
        windows.append((cursor.strftime("%Y-%m"), cursor, end))
        cursor = end
    return tuple(windows)


def _monthly_windows_from_observation_start(
    *,
    observation_start: date,
    analysis_end: date,
) -> tuple[tuple[str, date, date], ...]:
    """Genera meses desde el inicio poscorte, sin depender del onset RF estimado."""
    if analysis_end < observation_start:
        return ()
    cursor = observation_start
    final_exclusive = analysis_end + timedelta(days=1)
    windows: list[tuple[str, date, date]] = []
    while cursor < final_exclusive:
        next_month = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
        end = min(next_month, final_exclusive)
        windows.append((cursor.strftime("%Y-%m"), cursor, end))
        cursor = end
    return tuple(windows)


def _shared_acquisition_plan(
    events: Sequence[_EventSource],
    event_windows: Mapping[str, tuple[tuple[str, date, date], ...]],
) -> tuple[tuple[str, date, date, tuple[_EventSource, ...]], ...]:
    grouped: dict[tuple[str, date, date], list[_EventSource]] = {}
    for event in events:
        for window_id, start_date, end_date_exclusive in event_windows[event.event_id]:
            grouped.setdefault((window_id, start_date, end_date_exclusive), []).append(event)
    return tuple(
        (
            window_id,
            start_date,
            end_date_exclusive,
            tuple(sorted(participants, key=lambda event: event.event_id)),
        )
        for (window_id, start_date, end_date_exclusive), participants in sorted(
            grouped.items(), key=lambda item: (item[0][1], item[0][2], item[0][0])
        )
    )


def _event_batches(
    events: Sequence[DynamicWorldSharedEvent],
    *,
    maximum_events_per_batch: int,
) -> tuple[tuple[DynamicWorldSharedEvent, ...], ...]:
    """Particiona inventarios completos en lotes determinísticos sin descartarlos."""
    if maximum_events_per_batch < 1:
        raise ValueError("maximum_events_per_batch_must_be_positive")
    ordered = tuple(sorted(events, key=lambda event: event.event_id))
    return tuple(
        ordered[offset : offset + maximum_events_per_batch]
        for offset in range(0, len(ordered), maximum_events_per_batch)
    )


def _chunk_count_band_names(window_ids: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        name
        for window_id in window_ids
        for name in (
            f"{window_id}_valid_observation_count",
            f"{window_id}_qualifying_crop_count",
        )
    )


def _chunk_probability_band_names(window_ids: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"{window_id}_mean_crop_probability" for window_id in window_ids)


def _grid_with_nodata(grid: RasterGridSpec, nodata: float) -> RasterGridSpec:
    payload = grid.model_dump(mode="json")
    payload["nodata"] = nodata
    return RasterGridSpec.model_validate(payload)


def _require_raster_dtype(content: bytes, expected_dtype: str) -> None:
    with MemoryFile(content) as memory, memory.open() as dataset:
        if set(dataset.dtypes) != {expected_dtype}:
            raise ValueError("agricultural_chunk_transport_dtype_mismatch")


def _require_dynamic_world_label_values(content: bytes) -> None:
    with MemoryFile(content) as memory, memory.open() as dataset:
        values = dataset.read(indexes=(1,))[0]
        valid = values[values != DYNAMIC_WORLD_ANNUAL_NODATA]
        if valid.size and (int(valid.min()) < 0 or int(valid.max()) > 8):
            raise ValueError("dynamic_world_annual_label_values_invalid")


def _plan_temporal_chunks[ChunkItem](
    windows: Sequence[ChunkItem],
    *,
    grid: RasterGridSpec,
    maximum_uncompressed_bytes: int,
) -> tuple[tuple[ChunkItem, ...], ...]:
    """Agrupa meses sin superar el presupuesto de ninguna transferencia.

    Conteos (2 x uint16) y probabilidad (1 x float32) requieren ambos cuatro
    bytes por píxel y mes. Se descargan por separado para conservar sus dtypes.
    """
    bytes_per_window = grid.width * grid.height * REMOTE_CHUNK_BYTES_PER_PIXEL_PER_WINDOW
    windows_per_chunk = maximum_uncompressed_bytes // bytes_per_window
    if windows and windows_per_chunk < 1:
        raise ValueError("agricultural_collector_chunk_grid_budget_exceeded")
    return tuple(
        tuple(windows[offset : offset + windows_per_chunk])
        for offset in range(0, len(windows), windows_per_chunk)
    )


def _plan_spatial_groups(
    events: Sequence[_EventSource],
    *,
    target_crs: str,
    resolution_m: float,
    nodata: float,
    maximum_grid_pixels: int,
) -> tuple[tuple[tuple[_EventSource, ...], RasterGridSpec], ...]:
    """Divide envolventes dispersas recursivamente sin partir eventos."""

    def group_grid(group: Sequence[_EventSource]) -> RasterGridSpec:
        return derive_raster_grid_spec(
            aoi_wgs84=unary_union([event.geometry for event in group]),
            target_crs=target_crs,
            resolution_m=resolution_m,
            nodata=nodata,
        )

    def split(
        group: tuple[_EventSource, ...],
    ) -> list[tuple[tuple[_EventSource, ...], RasterGridSpec]]:
        grid = group_grid(group)
        if grid.width * grid.height <= maximum_grid_pixels:
            return [(tuple(sorted(group, key=lambda item: item.event_id)), grid)]
        if len(group) == 1:
            raise ValueError("agricultural_collector_shared_grid_budget_exceeded")
        centroids = [(event, event.geometry.centroid) for event in group]
        x_span = max(point.x for _, point in centroids) - min(point.x for _, point in centroids)
        y_span = max(point.y for _, point in centroids) - min(point.y for _, point in centroids)
        if x_span >= y_span:
            ordered = tuple(
                sorted(group, key=lambda item: (item.geometry.centroid.x, item.event_id))
            )
        else:
            ordered = tuple(
                sorted(group, key=lambda item: (item.geometry.centroid.y, item.event_id))
            )
        midpoint = len(ordered) // 2
        return [*split(ordered[:midpoint]), *split(ordered[midpoint:])]

    if not events:
        return ()
    groups = split(tuple(events))
    return tuple(sorted(groups, key=lambda item: tuple(event.event_id for event in item[0])))


def _extract_shared_chunk_window_to_event_grid(
    *,
    counts_content: bytes | None,
    probability_content: bytes | None,
    window_id: str,
    event_grid: RasterGridSpec,
) -> bytes:
    """Reconstruye el raster mensual sin transportar la fracción redundante."""
    if counts_content is None or probability_content is None:
        raise ValueError("agricultural_chunk_transport_missing")
    destination = np.full(
        (3, event_grid.height, event_grid.width),
        event_grid.nodata,
        dtype=np.float32,
    )
    count_names = _chunk_count_band_names((window_id,))
    probability_name = _chunk_probability_band_names((window_id,))[0]
    with MemoryFile(counts_content) as memory, memory.open() as source:
        descriptions = tuple(description or "" for description in source.descriptions)
        for destination_index, name in enumerate(count_names):
            source_index = descriptions.index(name) + 1
            reproject(
                source=rasterio.band(source, source_index),
                destination=destination[destination_index],
                src_transform=source.transform,
                src_crs=source.crs,
                src_nodata=TEMPORAL_COUNT_CUBE_NODATA,
                dst_transform=rasterio.Affine(*event_grid.transform),
                dst_crs=event_grid.target_crs,
                dst_nodata=event_grid.nodata,
                resampling=Resampling.nearest,
            )
    with MemoryFile(probability_content) as memory, memory.open() as source:
        descriptions = tuple(description or "" for description in source.descriptions)
        source_index = descriptions.index(probability_name) + 1
        reproject(
            source=rasterio.band(source, source_index),
            destination=destination[2],
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=rasterio.Affine(*event_grid.transform),
            dst_crs=event_grid.target_crs,
            dst_nodata=event_grid.nodata,
            resampling=Resampling.nearest,
        )
    valid = (
        np.isfinite(destination[0])
        & np.isfinite(destination[1])
        & np.isfinite(destination[2])
        & ~np.isclose(destination[0], event_grid.nodata)
        & ~np.isclose(destination[1], event_grid.nodata)
        & ~np.isclose(destination[2], event_grid.nodata)
        & (destination[0] > 0)
    )
    values = np.full(
        (len(DYNAMIC_WORLD_RASTER_BANDS), event_grid.height, event_grid.width),
        event_grid.nodata,
        dtype=np.float32,
    )
    values[:3, valid] = destination[:, valid]
    values[3, valid] = destination[1, valid] / destination[0, valid]
    profile = _raster_profile(event_grid, count=len(DYNAMIC_WORLD_RASTER_BANDS))
    with MemoryFile() as target_memory:
        with target_memory.open(**profile) as target:
            target.write(values)
            target.descriptions = DYNAMIC_WORLD_RASTER_BANDS
        return bytes(target_memory.read())


def _extract_shared_raster_to_event_grid(
    shared_content: bytes,
    event_grid: RasterGridSpec,
) -> bytes:
    destination = np.full(
        (len(DYNAMIC_WORLD_RASTER_BANDS), event_grid.height, event_grid.width),
        event_grid.nodata,
        dtype=np.float32,
    )
    with MemoryFile(shared_content) as source_memory:
        with source_memory.open() as source:
            for band_index in range(1, len(DYNAMIC_WORLD_RASTER_BANDS) + 1):
                reproject(
                    source=rasterio.band(source, band_index),
                    destination=destination[band_index - 1],
                    src_transform=source.transform,
                    src_crs=source.crs,
                    src_nodata=source.nodata,
                    dst_transform=rasterio.Affine(*event_grid.transform),
                    dst_crs=event_grid.target_crs,
                    dst_nodata=event_grid.nodata,
                    resampling=Resampling.nearest,
                )
    profile = _raster_profile(event_grid, count=len(DYNAMIC_WORLD_RASTER_BANDS))
    with MemoryFile() as target_memory:
        with target_memory.open(**profile) as target:
            target.write(destination)
            target.descriptions = DYNAMIC_WORLD_RASTER_BANDS
        return bytes(target_memory.read())


def _compact_temporal_count_cube(
    *,
    event_rows: Sequence[Mapping[str, Any]],
    raster_contents: Mapping[str, bytes],
) -> tuple[bytes, dict[str, tuple[int, int]]]:
    """Empaqueta conteos mensuales sin pérdida para el gate de persistencia."""
    windows = sorted({str(row["window_id"]) for row in event_rows})
    if not windows:
        raise ValueError("agricultural_temporal_cube_requires_windows")
    representatives = {
        window: next(row for row in event_rows if str(row["window_id"]) == window)
        for window in windows
    }
    arrays: list[NDArray[np.uint16]] = []
    descriptions: list[str] = []
    profile: dict[str, Any] | None = None
    band_pairs: dict[str, tuple[int, int]] = {}
    for index, window in enumerate(windows):
        path = str(representatives[window]["raster_path"])
        content = raster_contents.get(path)
        if content is None:
            raise ValueError("agricultural_temporal_cube_source_raster_missing")
        with MemoryFile(content) as memory, memory.open() as dataset:
            current = {
                "width": dataset.width,
                "height": dataset.height,
                "crs": dataset.crs,
                "transform": dataset.transform,
            }
            if profile is None:
                profile = current
            elif current != profile:
                raise ValueError("agricultural_temporal_cube_grid_mismatch")
            source = dataset.read((1, 2)).astype("float64")
            source_nodata = dataset.nodata
        nodata = ~np.isfinite(source)
        if source_nodata is not None:
            nodata |= source == source_nodata
        valid_values = source[~nodata]
        if (
            np.any(valid_values < 0)
            or np.any(valid_values >= TEMPORAL_COUNT_CUBE_NODATA)
            or np.any(valid_values != np.rint(valid_values))
        ):
            raise ValueError("agricultural_temporal_cube_counts_invalid")
        compact = np.full(source.shape, TEMPORAL_COUNT_CUBE_NODATA, dtype="uint16")
        compact[~nodata] = np.rint(source[~nodata]).astype("uint16")
        arrays.extend((compact[0], compact[1]))
        descriptions.extend(
            (f"{window}_valid_observation_count", f"{window}_qualifying_crop_count")
        )
        band_pairs[window] = (index * 2 + 1, index * 2 + 2)
    assert profile is not None
    output_profile = {
        "driver": "GTiff",
        "width": profile["width"],
        "height": profile["height"],
        "count": len(arrays),
        "dtype": "uint16",
        "crs": profile["crs"],
        "transform": profile["transform"],
        "nodata": TEMPORAL_COUNT_CUBE_NODATA,
        "compress": "deflate",
        "predictor": 2,
    }
    with MemoryFile() as memory:
        with memory.open(**output_profile) as dataset:
            dataset.write(np.stack(arrays))
            dataset.descriptions = tuple(descriptions)
        return bytes(memory.read()), band_pairs


def _event_footprint(geometry_wgs84: BaseGeometry, grid: RasterGridSpec) -> _Footprint:
    transformer = Transformer.from_crs("EPSG:4326", grid.target_crs, always_xy=True)
    projected = transform(transformer.transform, geometry_wgs84)
    fractions = np.zeros((grid.height, grid.width), dtype=np.float64)
    pixel_area_m2 = grid.resolution_m * grid.resolution_m
    affine = rasterio.Affine(*grid.transform)
    for row in range(grid.height):
        for column in range(grid.width):
            left, top = affine * (column, row)
            right, bottom = affine * (column + 1, row + 1)
            intersection_area = projected.intersection(box(left, bottom, right, top)).area
            fractions[row, column] = min(1.0, max(0.0, intersection_area / pixel_area_m2))
    exact_area_ha = projected.area / 10_000.0
    raster_area_ha = float(np.sum(fractions) * pixel_area_m2 / 10_000.0)
    content = _write_single_band_raster(
        values=fractions.astype(np.float32),
        grid=grid,
        band_name="event_footprint_fraction",
        nodata=grid.nodata,
    )
    fractions.setflags(write=False)
    return _Footprint(
        fractions=fractions,
        content=content,
        exact_area_ha=exact_area_ha,
        raster_area_ha=raster_area_ha,
        error_area_ha=raster_area_ha - exact_area_ha,
    )


def _summarize_window(
    *,
    content: bytes,
    footprint: _Footprint,
    config: AgriculturalCollectorConfig,
    event: _EventSource,
    window_id: str,
    start_date: date,
    end_date_exclusive: date,
    matched_scene_count: int,
    raster_path: str,
    footprint_path: str,
    grid: RasterGridSpec,
) -> list[dict[str, Any]]:
    with MemoryFile(content) as memory:
        with memory.open() as dataset:
            values = dataset.read().astype(np.float64)
            nodata = float(dataset.nodata)
    valid_count = values[0]
    qualifying_count = values[1]
    mean_probability = values[2]
    crop_fraction = values[3]
    _validate_dynamic_world_values(values, nodata, matched_scene_count)
    finite = np.all(np.isfinite(values), axis=0) & ~np.any(np.isclose(values, nodata), axis=0)
    valid = finite & (valid_count >= config.minimum_valid_observations_per_pixel)
    crop = (
        valid & (qualifying_count > 0) & (crop_fraction >= config.minimum_crop_observation_fraction)
    )
    unknown = valid & ~crop
    nodata_mask = ~valid
    weights = footprint.fractions * (grid.resolution_m * grid.resolution_m / 10_000.0)
    areas = {
        "crop": float(np.sum(weights[crop])),
        "unknown": float(np.sum(weights[unknown])),
        "nodata": float(np.sum(weights[nodata_mask])),
    }
    weighted_valid = footprint.fractions > 0
    mean_crop_probability = _weighted_mean(mean_probability, weights, valid & weighted_valid)
    mean_crop_observation_fraction = _weighted_mean(crop_fraction, weights, valid & weighted_valid)
    base = {
        "event_id": event.event_id,
        "window_id": window_id,
        "start_date": start_date.isoformat(),
        "end_date_exclusive": end_date_exclusive.isoformat(),
        "source_id": config.source_id,
        "matched_scene_count": matched_scene_count,
        "event_area_ha": round(footprint.raster_area_ha, 8),
        "event_area_exact_ha": round(footprint.exact_area_ha, 8),
        "grid_error_area_ha": round(footprint.error_area_ha, 12),
        "mean_crop_probability": mean_crop_probability,
        "mean_crop_observation_fraction": mean_crop_observation_fraction,
        "raster_path": raster_path,
        "event_footprint_path": footprint_path,
        "grid_sha256": grid.grid_sha256,
        "persistence_evaluated": False,
    }
    return [
        {**base, "class_name": class_name, "area_ha": round(area, 8)}
        for class_name, area in areas.items()
    ]


def _validate_dynamic_world_values(
    values: NDArray[np.float64], nodata: float, matched_scene_count: int
) -> None:
    band_nodata = ~np.isfinite(values) | np.isclose(values, nodata)
    if not all(np.array_equal(band_nodata[0], band_nodata[index]) for index in range(1, 4)):
        raise ValueError("dynamic_world_raster_values_invalid:band_masks_differ")
    valid_pixels = ~band_nodata[0]
    if not np.any(valid_pixels):
        return
    valid_count = values[0][valid_pixels]
    qualifying_count = values[1][valid_pixels]
    mean_probability = values[2][valid_pixels]
    crop_fraction = values[3][valid_pixels]
    invalid = (
        matched_scene_count == 0
        or np.any(valid_count <= 0)
        or np.any(valid_count > matched_scene_count)
        or np.any(qualifying_count < 0)
        or np.any(qualifying_count > valid_count)
        or np.any(~np.isclose(valid_count, np.rint(valid_count), atol=1e-6))
        or np.any(~np.isclose(qualifying_count, np.rint(qualifying_count), atol=1e-6))
        or np.any((mean_probability < 0) | (mean_probability > 1))
        or np.any((crop_fraction < 0) | (crop_fraction > 1))
        or np.any(~np.isclose(crop_fraction, qualifying_count / valid_count, atol=1e-5))
    )
    if invalid:
        raise ValueError("dynamic_world_raster_values_invalid")


def _event_summary(
    event: _EventSource,
    *,
    status: Literal["collected", "insufficient_onset", "insufficient_post_onset_window"],
    windows: int,
    quality_flags: list[str],
    footprint: _Footprint | None = None,
    grid: RasterGridSpec | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "collection_status": status,
        "estimated_onset_window_end": (
            event.onset_end.isoformat() if event.onset_end is not None else None
        ),
        "window_count": windows,
        "quality_flags": quality_flags,
        "event_area_exact_ha": footprint.exact_area_ha if footprint is not None else None,
        "event_area_raster_ha": footprint.raster_area_ha if footprint is not None else None,
        "grid_error_area_ha": footprint.error_area_ha if footprint is not None else None,
        "grid": grid.model_dump(mode="json") if grid is not None else None,
    }


def _output_feature(event: _EventSource, summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": mapping(event.geometry),
        "properties": dict(summary),
    }


def _empty_dynamic_world_raster(grid: RasterGridSpec) -> bytes:
    values = np.full(
        (len(DYNAMIC_WORLD_RASTER_BANDS), grid.height, grid.width),
        grid.nodata,
        dtype=np.float32,
    )
    profile = _raster_profile(grid, count=len(DYNAMIC_WORLD_RASTER_BANDS))
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            dataset.write(values)
            dataset.descriptions = DYNAMIC_WORLD_RASTER_BANDS
        return bytes(memory.read())


def _write_single_band_raster(
    *, values: NDArray[np.float32], grid: RasterGridSpec, band_name: str, nodata: float
) -> bytes:
    profile = _raster_profile(grid, count=1)
    profile["nodata"] = nodata
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            dataset.write(values, 1)
            dataset.set_band_description(1, band_name)
        return bytes(memory.read())


def _raster_profile(grid: RasterGridSpec, *, count: int) -> dict[str, Any]:
    return {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": count,
        "dtype": "float32",
        "crs": grid.target_crs,
        "transform": rasterio.Affine(*grid.transform),
        "nodata": grid.nodata,
        "compress": "deflate",
    }


def _weighted_mean(
    values: NDArray[np.float64],
    weights: NDArray[np.float64],
    mask: NDArray[np.bool_],
) -> float | None:
    denominator = float(np.sum(weights[mask]))
    if denominator <= 0:
        return None
    return float(np.sum(values[mask] * weights[mask]) / denominator)


def _rows_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    fields = (
        "event_id",
        "window_id",
        "start_date",
        "end_date_exclusive",
        "source_id",
        "class_name",
        "area_ha",
        "event_area_ha",
        "event_area_exact_ha",
        "grid_error_area_ha",
        "matched_scene_count",
        "mean_crop_probability",
        "mean_crop_observation_fraction",
        "raster_path",
        "valid_count_band",
        "qualifying_count_band",
        "event_footprint_path",
        "grid_sha256",
        "persistence_evaluated",
    )
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _safe_id(value: str) -> str:
    normalized = _SAFE_IDENTIFIER.sub("-", value.strip()).strip("-_")
    if not normalized:
        raise ValueError("agricultural_collector_identifier_unsafe")
    return normalized[:100]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounds_are_wgs84(bounds: tuple[float, float, float, float]) -> bool:
    return (
        all(math.isfinite(value) for value in bounds)
        and -180 <= bounds[0] <= bounds[2] <= 180
        and -90 <= bounds[1] <= bounds[3] <= 90
    )


def agricultural_collection_json_schema() -> dict[str, Any]:
    """Devuelve el JSON Schema canónico de la recolección cruda."""
    schema = AgriculturalCollectionDocument.model_json_schema(mode="serialization")
    schema["$id"] = "urn:deforestation-pipeline:agricultural-collection:1.1.0"
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = AGRICULTURAL_COLLECTION_SCHEMA_VERSION
    return schema
