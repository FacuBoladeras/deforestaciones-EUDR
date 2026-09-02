"""Contexto cartográfico OSM vectorial, cacheado y separado de la evidencia científica."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

type MapBounds = tuple[float, float, float, float]
type FeatureKind = Literal["road", "waterway", "water", "building", "landuse"]
type OverpassFetcher = Callable[[str, str, str], dict[str, object]]

_ATTRIBUTION = "© OpenStreetMap contributors · ODbL"
_DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
_DEFAULT_USER_AGENT = "DeforestationEvidenceReport/0.15.0"


@dataclass(frozen=True, slots=True)
class OSMFeature:
    """Geometría OSM mínima necesaria para el mapa de contexto."""

    kind: FeatureKind
    geometry: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class OSMBasemap:
    """Capa OSM normalizada; nunca interviene en la decisión científica."""

    bounds: MapBounds
    features: tuple[OSMFeature, ...]
    opacity: float = 0.5
    attribution: str = _ATTRIBUTION
    available: bool = True
    source_timestamp: str | None = None


class OSMBasemapProvider(Protocol):
    """Puerto mínimo para obtener contexto cartográfico por extensión."""

    def get(self, bounds: MapBounds) -> OSMBasemap: ...


class OverpassOSMProvider:
    """Consulta pocos vectores OSM y conserva una copia determinística por extensión."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        endpoint: str = _DEFAULT_ENDPOINT,
        user_agent: str = _DEFAULT_USER_AGENT,
        fetcher: OverpassFetcher | None = None,
    ) -> None:
        self._cache_dir = cache_dir.resolve()
        self._endpoint = endpoint
        self._user_agent = user_agent
        self._fetcher = fetcher or _fetch_overpass

    def get(self, bounds: MapBounds) -> OSMBasemap:
        normalized_bounds = _normalize_bounds(bounds)
        query = _overpass_query(normalized_bounds)
        cache_key = hashlib.sha256(f"osm-vector-context-1.1.0\n{query}".encode()).hexdigest()
        cache_path = self._cache_dir / f"{cache_key}.json"
        if cache_path.is_file():
            return _load_cache(cache_path)

        try:
            payload = self._fetcher(self._endpoint, query, self._user_agent)
            basemap = OSMBasemap(
                bounds=normalized_bounds,
                features=_features_from_payload(payload),
                source_timestamp=_source_timestamp(payload),
            )
        except (OSError, TimeoutError, json.JSONDecodeError, ValueError):
            return OSMBasemap(bounds=normalized_bounds, features=(), available=False)

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(_cache_json(basemap), encoding="utf-8")
        return basemap


def _normalize_bounds(bounds: MapBounds) -> MapBounds:
    minimum_x, minimum_y, maximum_x, maximum_y = (float(value) for value in bounds)
    if not all(math.isfinite(value) for value in bounds):
        raise ValueError("osm_bounds_not_finite")
    if minimum_x >= maximum_x or minimum_y >= maximum_y:
        raise ValueError("osm_bounds_invalid")
    return (
        round(minimum_x, 7),
        round(minimum_y, 7),
        round(maximum_x, 7),
        round(maximum_y, 7),
    )


def _overpass_query(bounds: MapBounds) -> str:
    west, south, east, north = bounds
    bbox = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    return (
        "[out:json][timeout:20];("
        f'way["highway"]({bbox});'
        f'way["waterway"]({bbox});'
        f'way["natural"="water"]({bbox});'
        f'way["building"]({bbox});'
        f'way["landuse"]({bbox});'
        f");out tags geom({bbox});"
    )


def _fetch_overpass(endpoint: str, query: str, user_agent: str) -> dict[str, object]:
    request = Request(
        endpoint,
        data=urlencode({"data": query}).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("osm_response_invalid")
    return cast(dict[str, object], payload)


def _features_from_payload(payload: dict[str, object]) -> tuple[OSMFeature, ...]:
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise ValueError("osm_elements_invalid")
    features: list[OSMFeature] = []
    for raw_element in elements:
        if not isinstance(raw_element, dict):
            continue
        tags = raw_element.get("tags")
        raw_geometry = raw_element.get("geometry")
        if not isinstance(tags, dict) or not isinstance(raw_geometry, list):
            continue
        kind = _feature_kind(cast(dict[str, object], tags))
        geometry = _geometry(cast(list[object], raw_geometry))
        if kind is not None and len(geometry) >= 2:
            features.append(OSMFeature(kind=kind, geometry=geometry))
    return tuple(features)


def _feature_kind(tags: dict[str, object]) -> FeatureKind | None:
    if "highway" in tags:
        return "road"
    if "waterway" in tags:
        return "waterway"
    if tags.get("natural") == "water":
        return "water"
    if "building" in tags:
        return "building"
    if "landuse" in tags:
        return "landuse"
    return None


def _geometry(raw_geometry: list[object]) -> tuple[tuple[float, float], ...]:
    points: list[tuple[float, float]] = []
    for raw_point in raw_geometry:
        if not isinstance(raw_point, dict):
            continue
        longitude = raw_point.get("lon")
        latitude = raw_point.get("lat")
        if isinstance(longitude, (int, float)) and isinstance(latitude, (int, float)):
            points.append((float(longitude), float(latitude)))
    return tuple(points)


def _cache_json(basemap: OSMBasemap) -> str:
    payload = {
        "schema_version": "1.1.0",
        "source": "OpenStreetMap",
        "attribution": basemap.attribution,
        "opacity": basemap.opacity,
        "source_timestamp": basemap.source_timestamp,
        "bounds": basemap.bounds,
        "features": [
            {"kind": feature.kind, "geometry": feature.geometry} for feature in basemap.features
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_cache(path: Path) -> OSMBasemap:
    payload = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    bounds = _normalize_bounds(cast(MapBounds, tuple(payload["bounds"])))
    features = tuple(
        OSMFeature(
            kind=cast(FeatureKind, raw_feature["kind"]),
            geometry=tuple((float(point[0]), float(point[1])) for point in raw_feature["geometry"]),
        )
        for raw_feature in payload["features"]
    )
    source_timestamp = payload.get("source_timestamp")
    return OSMBasemap(
        bounds=bounds,
        features=features,
        source_timestamp=source_timestamp if isinstance(source_timestamp, str) else None,
    )


def _source_timestamp(payload: dict[str, object]) -> str | None:
    metadata = payload.get("osm3s")
    if not isinstance(metadata, dict):
        return None
    timestamp = metadata.get("timestamp_osm_base")
    return timestamp if isinstance(timestamp, str) else None
