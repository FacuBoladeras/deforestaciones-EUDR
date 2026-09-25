"""Frontera acotada y sanitizada para consultar metadatos en Earth Engine."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.catalog import BenchmarkSourcePlan, CatalogSource
from deforestation_pipeline.hls_tiles import mgrs_tile_ids_from_hls_identifiers

MAX_QUERY_DAYS = 366
MAX_SCENES_PER_SOURCE = 50


class GeeAuthenticationError(RuntimeError):
    """Fallo de autenticación que nunca incluye valores de la credencial."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Autenticación GEE fallida: {code}")


class GeeQueryError(ValueError):
    """Consulta insegura, inválida o fallida, sin propagar secretos remotos."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(violations)
        if not self.violations:
            raise ValueError("GeeQueryError requiere al menos una violación")
        super().__init__("Consulta GEE inválida: " + "; ".join(self.violations))


class StrictGeeModel(BaseModel):
    """Base inmutable para resultados remotos auditables."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SceneMetadata(StrictGeeModel):
    """Metadatos mínimos de una escena; no contiene píxeles ni geometría."""

    image_id: str = Field(min_length=1)
    acquired_at: datetime
    cloud_coverage_pct: float = Field(ge=0, le=100)


class CollectionMetadataResult(StrictGeeModel):
    """Resultado acotado para una colección HLS."""

    source_id: str
    product: str
    collection_id: str
    matched_scene_count: int = Field(ge=0)
    returned_scene_count: int = Field(ge=0)
    truncated: bool
    scenes: tuple[SceneMetadata, ...]


class GeeMetadataQueryResult(StrictGeeModel):
    """Evidencia de una consulta remota que sólo recuperó metadatos."""

    queried_at: datetime
    start_date: date
    end_date_exclusive: date
    max_scenes_per_source: int
    aoi_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    remote_data_accessed: Literal[True] = True
    metadata_only: Literal[True] = True
    spatial_filter_strategy: Literal["l30_bounds_then_shared_mgrs_tiles"] = (
        "l30_bounds_then_shared_mgrs_tiles"
    )
    mgrs_tile_ids: tuple[str, ...] = ()
    collections: tuple[CollectionMetadataResult, ...]


@dataclass(frozen=True, slots=True)
class GeeMetadataQuery:
    """Ventana de consulta explícita y acotada."""

    start_date: date
    end_date: date
    max_scenes_per_source: int = 25


@dataclass(frozen=True, slots=True)
class GeeSession:
    """Sesión inicializada que oculta cliente, cuenta y proyecto en su repr."""

    module: Any = field(repr=False, compare=False)
    initialized: bool = True


def authenticate_earth_engine(
    credentials_path: Path,
    *,
    ee_module: Any | None = None,
) -> GeeSession:
    """Inicializa GEE con una cuenta de servicio sin exponer su identidad."""
    document = _load_service_account_document(credentials_path)
    module = ee_module if ee_module is not None else _import_earth_engine()
    try:
        credentials = module.ServiceAccountCredentials(
            document["client_email"],
            str(credentials_path),
        )
        module.Initialize(credentials, project=document["project_id"])
    except Exception as error:
        raise GeeAuthenticationError(_authentication_error_code(error)) from None
    return GeeSession(module=module)


def authenticate_earth_engine_user_oauth(
    *,
    project: str,
    ee_module: Any | None = None,
) -> GeeSession:
    """Inicializa con la credencial persistente del usuario sin leer identidad ni tokens."""
    if not project:
        raise GeeAuthenticationError("project_required")
    module = ee_module if ee_module is not None else _import_earth_engine()
    try:
        module.Initialize(project=project)
    except Exception as error:
        raise GeeAuthenticationError(_authentication_error_code(error)) from None
    return GeeSession(module=module)


def query_hls_scene_metadata(
    *,
    session: GeeSession,
    plan: BenchmarkSourcePlan,
    aoi_wgs84: BaseGeometry,
    query: GeeMetadataQuery,
    queried_at: datetime | None = None,
) -> GeeMetadataQueryResult:
    """Consulta sólo IDs, fechas y nubosidad; nunca descarga bandas."""
    violations = _query_violations(aoi_wgs84, query)
    query_time = queried_at or datetime.now(UTC)
    if query_time.tzinfo is None or query_time.utcoffset() is None:
        violations.append("queried_at debe incluir zona horaria")
    if violations:
        raise GeeQueryError(violations)

    module = session.module
    try:
        remote_aoi = module.Geometry(mapping(aoi_wgs84))
        source_by_product = {source.product: source for source in plan.sources}
        l30_source = source_by_product["HLSL30"]
        s30_source = source_by_product["HLSS30"]
        l30_collection = _filter_collection_by_bounds(
            module=module,
            remote_aoi=remote_aoi,
            source=l30_source,
            start_date=query.start_date,
            end_date=query.end_date,
        )
        mgrs_tile_ids = _resolve_mgrs_tile_ids(
            module=module,
            remote_aoi=remote_aoi,
            l30_source=l30_source,
            primary_collection=l30_collection,
        )
        s30_collection = (
            module.ImageCollection(s30_source.collection_id)
            .filterDate(query.start_date.isoformat(), query.end_date.isoformat())
            .filter(module.Filter.inList("MGRS_TILE_ID", list(mgrs_tile_ids)))
            .sort("system:time_start")
        )
        results = (
            _query_collection_metadata(
                collection=l30_collection,
                source=l30_source,
                query=query,
            ),
            _query_collection_metadata(
                collection=s30_collection,
                source=s30_source,
                query=query,
            ),
        )
    except GeeQueryError:
        raise
    except Exception as error:
        raise GeeQueryError([f"remote_query_failed:{_remote_error_code(error)}"]) from None

    return GeeMetadataQueryResult(
        queried_at=query_time,
        start_date=query.start_date,
        end_date_exclusive=query.end_date,
        max_scenes_per_source=query.max_scenes_per_source,
        aoi_sha256=hashlib.sha256(aoi_wgs84.wkb).hexdigest(),
        mgrs_tile_ids=mgrs_tile_ids,
        collections=results,
    )


def _query_collection_metadata(
    *,
    collection: Any,
    source: CatalogSource,
    query: GeeMetadataQuery,
) -> CollectionMetadataResult:
    matched_count = _non_negative_integer(
        collection.size().getInfo(),
        field_name="matched_scene_count",
    )
    limited = collection.limit(query.max_scenes_per_source)
    image_ids = _list_value(
        limited.aggregate_array("system:index").getInfo(),
        field_name="system:index",
    )
    timestamps = _list_value(
        limited.aggregate_array("system:time_start").getInfo(),
        field_name="system:time_start",
    )
    cloud_coverages = _list_value(
        limited.aggregate_array("CLOUD_COVERAGE").getInfo(),
        field_name="CLOUD_COVERAGE",
    )
    if not (len(image_ids) == len(timestamps) == len(cloud_coverages)):
        raise GeeQueryError([f"{source.product} devolvió metadatos desalineados"])

    scenes = tuple(
        SceneMetadata(
            image_id=_non_empty_string(image_id, field_name="system:index"),
            acquired_at=_timestamp_milliseconds(timestamp),
            cloud_coverage_pct=_percentage(
                cloud_coverage,
                field_name="CLOUD_COVERAGE",
            ),
        )
        for image_id, timestamp, cloud_coverage in zip(
            image_ids,
            timestamps,
            cloud_coverages,
            strict=True,
        )
    )
    return CollectionMetadataResult(
        source_id=source.source_id,
        product=source.product,
        collection_id=source.collection_id,
        matched_scene_count=matched_count,
        returned_scene_count=len(scenes),
        truncated=matched_count > len(scenes),
        scenes=scenes,
    )


def _filter_collection_by_bounds(
    *,
    module: Any,
    remote_aoi: Any,
    source: CatalogSource,
    start_date: date,
    end_date: date,
) -> Any:
    return (
        module.ImageCollection(source.collection_id)
        .filterBounds(remote_aoi)
        .filterDate(start_date.isoformat(), end_date.isoformat())
        .sort("system:time_start")
    )


def _resolve_mgrs_tile_ids(
    *,
    module: Any,
    remote_aoi: Any,
    l30_source: CatalogSource,
    primary_collection: Any,
) -> tuple[str, ...]:
    identifiers = _all_collection_identifiers(primary_collection)
    if not identifiers:
        reference_collection = _filter_collection_by_bounds(
            module=module,
            remote_aoi=remote_aoi,
            source=l30_source,
            start_date=date(2023, 1, 1),
            end_date=date(2024, 1, 1),
        )
        identifiers = _all_collection_identifiers(reference_collection)
    if not identifiers:
        raise GeeQueryError(["no fue posible resolver tiles MGRS para el AOI"])
    try:
        return mgrs_tile_ids_from_hls_identifiers(identifiers)
    except ValueError:
        raise GeeQueryError(["HLSL30 devolvió un identificador sin tile MGRS"]) from None


def _all_collection_identifiers(collection: Any) -> tuple[str, ...]:
    values = _list_value(
        collection.aggregate_array("system:index").getInfo(),
        field_name="system:index",
    )
    return tuple(_non_empty_string(value, field_name="system:index") for value in values)


def _query_violations(
    geometry: BaseGeometry,
    query: GeeMetadataQuery,
) -> list[str]:
    violations: list[str] = []
    if query.end_date <= query.start_date:
        violations.append("end_date debe ser posterior a start_date")
    elif (query.end_date - query.start_date).days > MAX_QUERY_DAYS:
        violations.append(f"el período no puede superar {MAX_QUERY_DAYS} días")
    if not 1 <= query.max_scenes_per_source <= MAX_SCENES_PER_SOURCE:
        violations.append(f"max_scenes_per_source debe estar entre 1 y {MAX_SCENES_PER_SOURCE}")
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        violations.append("AOI debe ser Polygon o MultiPolygon")
    if geometry.is_empty or not geometry.is_valid:
        violations.append("AOI debe ser no vacía y topológicamente válida")
    if not geometry.is_empty:
        min_lon, min_lat, max_lon, max_lat = geometry.bounds
        if not (-180 <= min_lon <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
            violations.append("AOI debe estar normalizada en EPSG:4326")
    return violations


def _load_service_account_document(path: Path) -> dict[str, str]:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raise GeeAuthenticationError("credential_file_unreadable") from None
    if not isinstance(document, Mapping) or document.get("type") != "service_account":
        raise GeeAuthenticationError("unsupported_credential_shape")
    required_fields = ("project_id", "client_email", "private_key", "token_uri")
    values: dict[str, str] = {}
    for field_name in required_fields:
        value = document.get(field_name)
        if not isinstance(value, str) or not value:
            raise GeeAuthenticationError("incomplete_service_account") from None
        values[field_name] = value
    return values


def _import_earth_engine() -> Any:
    try:
        return importlib.import_module("ee")
    except ImportError:
        raise GeeAuthenticationError("earthengine_api_not_installed") from None


def _authentication_error_code(error: Exception) -> str:
    message = str(error).lower()
    if "invalid_grant" in message or "invalid jwt" in message:
        return "invalid_credentials"
    if "not registered" in message:
        return "project_not_registered"
    if "permission" in message or "forbidden" in message:
        return "permission_denied"
    if "api has not been used" in message or "disabled" in message:
        return "earth_engine_api_disabled"
    return "initialization_failed"


def _remote_error_code(error: Exception) -> str:
    message = str(error).lower()
    if "permission" in message or "forbidden" in message:
        return "permission_denied"
    if "quota" in message or "rate" in message:
        return "quota_or_rate_limit"
    if "timeout" in message or "deadline" in message:
        return "timeout"
    return "service_error"


def _list_value(value: object, *, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise GeeQueryError([f"{field_name} no devolvió una lista"])
    return value


def _non_negative_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GeeQueryError([f"{field_name} no devolvió un entero no negativo"])
    return value


def _non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise GeeQueryError([f"{field_name} no devolvió texto"])
    return value


def _timestamp_milliseconds(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeeQueryError(["system:time_start no devolvió milisegundos"])
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _percentage(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeeQueryError([f"{field_name} no devolvió un porcentaje"])
    percentage = float(value)
    if not 0 <= percentage <= 100:
        raise GeeQueryError([f"{field_name} quedó fuera de 0..100"])
    return percentage
