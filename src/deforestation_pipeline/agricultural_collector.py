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
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from deforestation_pipeline.agricultural_visualization import (
    render_monthly_agricultural_evidence,
)
from deforestation_pipeline.catalog import (
    AgriculturalCatalogSource,
    build_agricultural_evidence_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import OutputConfig, load_license_registry
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

AGRICULTURAL_COLLECTION_SCHEMA_VERSION: Final = "1.0.0"
DYNAMIC_WORLD_RASTER_BANDS: Final = (
    "valid_observation_count",
    "qualifying_crop_count",
    "mean_crop_probability",
    "crop_observation_fraction",
)
_EVENT_ARTIFACT = "json/evidence/disturbance_events.geojson"
_SAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9_-]+")


class AgriculturalCollectorConfig(StrictModel):
    """Parámetros acotados del recolector, separados del gate de persistencia."""

    schema_version: Literal["1.0.0"]
    source_id: Literal["dynamic_world_v1"]
    target_crs: Literal["EPSG:6933"]
    resolution_m: Literal[10]
    raster_nodata: float
    temporal_cadence: Literal["calendar_month"]
    minimum_top1_probability: Annotated[float, Field(gt=0.5, le=1, strict=True)]
    minimum_valid_observations_per_pixel: Annotated[int, Field(ge=1, le=31, strict=True)]
    minimum_crop_observation_fraction: Annotated[float, Field(gt=0, le=1, strict=True)]
    maximum_events: Annotated[int, Field(ge=1, le=100, strict=True)]
    maximum_windows_per_event: Annotated[int, Field(ge=1, le=72, strict=True)]
    maximum_total_queries: Annotated[int, Field(ge=1, le=500, strict=True)]
    resampling: Literal["nearest"]
    event_footprint_method: Literal["exact_polygon_pixel_intersection_fraction"]

    @field_validator("raster_nodata")
    @classmethod
    def nodata_is_fixed(cls, value: float) -> float:
        if value != -9999.0:
            raise ValueError("raster_nodata debe ser -9999")
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
    event_footprint_path: NonEmptyString
    grid_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    persistence_evaluated: Literal[False]

    @model_validator(mode="after")
    def temporal_window_is_nonempty(self) -> AgriculturalCollectionRow:
        if self.end_date_exclusive <= self.start_date:
            raise ValueError("agricultural collection row requiere una ventana no vacía")
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

    schema_version: Literal["1.0.0"]
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


RasterProvider = Callable[[DynamicWorldWindowQuery], CollectedWindowRaster]


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
) -> Path:
    """Consulta meses pos-onset y publica evidencia espacial cruda por evento."""
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
    if len(events) > config.maximum_events:
        raise ValueError("agricultural_collector_event_budget_exceeded")

    event_windows = {
        event.event_id: _monthly_windows(event.onset_end, analysis_end_date) for event in events
    }
    if any(len(windows) > config.maximum_windows_per_event for windows in event_windows.values()):
        raise ValueError("agricultural_collector_window_budget_exceeded")
    query_count = sum(len(windows) for windows in event_windows.values())
    if query_count > config.maximum_total_queries:
        raise ValueError("agricultural_collector_total_query_budget_exceeded")

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
        "agricultural-collector-v1:" + json.dumps(identity, sort_keys=True, separators=(",", ":")),
    )
    files: dict[str, bytes] = {}
    rows: list[dict[str, Any]] = []
    output_features: list[dict[str, Any]] = []
    query_records: list[dict[str, Any]] = []
    event_summaries: list[dict[str, Any]] = []
    visualizations: list[dict[str, Any]] = []

    for event in events:
        windows = event_windows[event.event_id]
        if event.onset_end is None:
            summary = _event_summary(
                event,
                status="insufficient_onset",
                windows=0,
                quality_flags=["estimated_onset_window_end_missing"],
            )
            event_summaries.append(summary)
            output_features.append(_output_feature(event, summary))
            continue
        if not windows:
            summary = _event_summary(
                event,
                status="insufficient_post_onset_window",
                windows=0,
                quality_flags=["analysis_end_not_after_estimated_onset"],
            )
            event_summaries.append(summary)
            output_features.append(_output_feature(event, summary))
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

        for window_id, start_date, end_date_exclusive in windows:
            query = DynamicWorldWindowQuery(
                event_id=event.event_id,
                window_id=window_id,
                start_date=start_date,
                end_date_exclusive=end_date_exclusive,
                geometry_wgs84=event.geometry,
                grid=grid,
                source=source,
                minimum_top1_probability=config.minimum_top1_probability,
            )
            collected = raster_provider(query)
            raster_path = f"{event_root}/{window_id}_dynamic_world.tif"
            raw = collected.content or _empty_dynamic_world_raster(grid)
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
                matched_scene_count=collected.matched_scene_count,
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
                    "matched_scene_count": collected.matched_scene_count,
                    "retrieved_at": collected.retrieved_at.isoformat(),
                    "grid_sha256": grid.grid_sha256,
                    "raster_path": raster_path,
                    "raster_sha256": hashlib.sha256(normalized).hexdigest(),
                    "raster_validation": validation.model_dump(mode="json"),
                }
            )

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
        "method": "dynamic_world_event_monthly_collector_v1",
        "source": source_identity,
        "configuration": config.model_dump(mode="json"),
        "spatial_method": {
            "target_crs": config.target_crs,
            "resolution_m": config.resolution_m,
            "resampling": config.resampling,
            "event_footprint": config.event_footprint_method,
            "area_method": "sum_pixel_area_times_exact_event_footprint_fraction",
            "area_unit": "hectare",
        },
        "temporal_method": {
            "cadence": config.temporal_cadence,
            "window_start": "day_after_estimated_onset_window_end",
            "analysis_end_inclusive": analysis_end_date.isoformat(),
            "persistence_evaluated": False,
        },
        "query_count": len(query_records),
        "queries": query_records,
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
        f"{_safe_id(establishment_id)}-agriculture-v1__"
        f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}__{str(analysis_id)[:8]}"
    )
    run_directory = output_root.resolve() / run_name
    _publish_bundle(
        run_directory=run_directory,
        files=files,
        manifest_metadata={
            "schema_version": AGRICULTURAL_COLLECTION_SCHEMA_VERSION,
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
            "signed_urls_persisted": False,
            "persistence_evaluated": False,
            "visualizations": visualizations,
        },
        remote_data_accessed=bool(query_records),
        datasets=(source_identity,),
    )
    return run_directory


def make_dynamic_world_raster_provider(
    *,
    session: GeeSession,
    output_config: OutputConfig,
    fetch_bytes: FetchBytes | None = None,
) -> RasterProvider:
    """Construye el provider productivo GEE sin exponer la sesión en artefactos."""

    def provider(query: DynamicWorldWindowQuery) -> CollectedWindowRaster:
        return collect_dynamic_world_window(
            query=query,
            session=session,
            output_config=output_config,
            fetch_bytes=fetch_bytes,
        )

    return provider


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

        valid_count = collection.select("crops").count().rename(DYNAMIC_WORLD_RASTER_BANDS[0])

        def qualifying_crop(image: Any) -> Any:
            candidate = module.Image(image)
            return (
                candidate.select("label")
                .eq(4)
                .And(candidate.select("crops").gte(query.minimum_top1_probability))
                .rename("qualifying_crop")
            )

        qualifying_count = (
            collection.map(qualifying_crop).sum().rename(DYNAMIC_WORLD_RASTER_BANDS[1])
        )
        mean_probability = collection.select("crops").mean().rename(DYNAMIC_WORLD_RASTER_BANDS[2])
        observation_fraction = qualifying_count.divide(valid_count).rename(
            DYNAMIC_WORLD_RASTER_BANDS[3]
        )
        image = (
            valid_count.addBands(qualifying_count)
            .addBands(mean_probability)
            .addBands(observation_fraction)
            .updateMask(valid_count.gt(0))
            .toFloat()
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
    """Devuelve el JSON Schema canónico de la recolección cruda v1."""
    schema = AgriculturalCollectionDocument.model_json_schema(mode="serialization")
    schema["$id"] = "urn:deforestation-pipeline:agricultural-collection:1.0.0"
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = AGRICULTURAL_COLLECTION_SCHEMA_VERSION
    return schema
