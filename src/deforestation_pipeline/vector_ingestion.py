"""Ingesta vectorial local desacoplada del contrato GeoJSON del pipeline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import pyogrio
from pyproj import CRS
from shapely import union_all
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.artifact_layout import input_json
from deforestation_pipeline.schemas import GeoJSONGeometry

SUPPORTED_TERRITORY_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})


class VectorIngestionError(ValueError):
    """Entrada vectorial ilegible o ambigua antes de la validación GIS."""


@dataclass(frozen=True, slots=True)
class VectorIngestion:
    """Resultado reproducible de convertir una fuente local al contrato interno."""

    geometry: GeoJSONGeometry
    source_crs: str
    source_crs_origin: str
    source_format: str
    selected_layer: str
    available_layers: tuple[str, ...]
    feature_count: int
    dissolved: bool
    converted_geojson_bytes: bytes
    source_files: Mapping[str, bytes]
    input_sha256: str

    def provenance_payload(self) -> dict[str, object]:
        """Describe la conversión sin persistir rutas absolutas."""
        source_records = [
            {
                "name": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(self.source_files.items())
        ]
        return {
            "format": self.source_format,
            "source_crs": self.source_crs,
            "source_crs_origin": self.source_crs_origin,
            "selected_layer": self.selected_layer,
            "available_layers": self.available_layers,
            "feature_count": self.feature_count,
            "dissolved": self.dissolved,
            "converted_path": input_json("converted.geojson"),
            "source_files": source_records,
            "input_sha256": self.input_sha256,
        }

    def bundle_source_files(self) -> dict[str, bytes]:
        """Ubica la fuente original dentro del namespace seguro del bundle."""
        return {f"source/{name}": content for name, content in sorted(self.source_files.items())}


def ingest_local_vector(
    path: Path,
    *,
    declared_crs: str | None = None,
    layer: str | None = None,
    dissolve_all: bool = False,
) -> VectorIngestion:
    """Lee una fuente GDAL local y produce una única geometría contractual."""
    source_path = path.resolve()
    if not source_path.is_file():
        raise VectorIngestionError(f"la fuente vectorial no es un archivo legible: {path}")

    open_path = _gdal_path(source_path)
    available_layers = _spatial_layers(open_path)
    selected_layer = _select_layer(available_layers, layer)
    try:
        frame = gpd.read_file(
            open_path,
            layer=selected_layer,
            columns=[],
            engine="pyogrio",
            on_invalid="raise",
        )
        source_info = pyogrio.read_info(open_path, layer=selected_layer)
    except Exception as error:
        raise VectorIngestionError(
            f"no se pudo leer la capa vectorial seleccionada: {_io_error_code(error)}"
        ) from None

    feature_count = len(frame)
    if feature_count == 0:
        raise VectorIngestionError("la capa vectorial no contiene entidades")
    geometries = _valid_source_geometries(tuple(frame.geometry))
    if feature_count > 1 and not dissolve_all:
        raise VectorIngestionError(
            f"la capa contiene {feature_count} entidades; use --dissolve-all "
            "para confirmar su unión"
        )

    geometry = _combine_geometries(geometries, dissolve_all=dissolve_all)
    geometry_contract = _geometry_contract(geometry)
    source_crs, source_crs_origin = _resolve_source_crs(
        embedded_crs=frame.crs,
        declared_crs=declared_crs,
    )
    source_files = _read_source_files(source_path)
    converted = _converted_geojson_bytes(geometry_contract)
    source_format = _source_driver(source_info)
    input_sha256 = _input_identity(
        source_files=source_files,
        converted=converted,
        source_crs=source_crs,
        selected_layer=selected_layer,
        dissolved=dissolve_all,
    )
    return VectorIngestion(
        geometry=geometry_contract,
        source_crs=source_crs,
        source_crs_origin=source_crs_origin,
        source_format=source_format,
        selected_layer=selected_layer,
        available_layers=available_layers,
        feature_count=feature_count,
        dissolved=dissolve_all,
        converted_geojson_bytes=converted,
        source_files=source_files,
        input_sha256=input_sha256,
    )


def readable_vector_drivers() -> tuple[str, ...]:
    """Informa los drivers de lectura realmente disponibles en el runtime."""
    return tuple(sorted(pyogrio.list_drivers(read=True)))


def _gdal_path(path: Path) -> str:
    if path.suffix.lower() == ".zip":
        return f"zip://{path.as_posix()}"
    return str(path)


def _spatial_layers(open_path: str) -> tuple[str, ...]:
    try:
        layer_rows = pyogrio.list_layers(open_path).tolist()
    except Exception as error:
        raise VectorIngestionError(
            f"formato vectorial no reconocido por GDAL: {_io_error_code(error)}"
        ) from None
    layers = tuple(
        str(name)
        for name, geometry_type in layer_rows
        if isinstance(name, str) and geometry_type is not None
    )
    if not layers:
        raise VectorIngestionError("la fuente no contiene capas espaciales")
    return layers


def _select_layer(available_layers: tuple[str, ...], requested: str | None) -> str:
    if requested is not None:
        if requested not in available_layers:
            raise VectorIngestionError(
                f"la capa solicitada no existe; disponibles: {', '.join(available_layers)}"
            )
        return requested
    if len(available_layers) > 1:
        raise VectorIngestionError(
            "la fuente contiene múltiples capas; seleccione una con --layer: "
            + ", ".join(available_layers)
        )
    return available_layers[0]


def _valid_source_geometries(
    values: tuple[BaseGeometry | None, ...],
) -> tuple[BaseGeometry, ...]:
    if any(geometry is None for geometry in values):
        raise VectorIngestionError("la capa contiene geometrías nulas")
    geometries = tuple(geometry for geometry in values if geometry is not None)
    if any(geometry.is_empty for geometry in geometries):
        raise VectorIngestionError("la capa contiene geometrías vacías")
    unsupported = sorted(
        {geometry.geom_type for geometry in geometries} - SUPPORTED_TERRITORY_GEOMETRY_TYPES
    )
    if unsupported:
        raise VectorIngestionError(
            "el territorio debe contener sólo Polygon o MultiPolygon; encontrados: "
            + ", ".join(unsupported)
        )
    return geometries


def _combine_geometries(
    geometries: tuple[BaseGeometry, ...],
    *,
    dissolve_all: bool,
) -> BaseGeometry:
    if len(geometries) == 1:
        return geometries[0]
    if not dissolve_all:
        raise VectorIngestionError("la unión de entidades requiere --dissolve-all")
    try:
        return union_all(geometries)
    except Exception:
        raise VectorIngestionError("no fue posible disolver las entidades") from None


def _geometry_contract(geometry: BaseGeometry) -> GeoJSONGeometry:
    if geometry.geom_type not in SUPPORTED_TERRITORY_GEOMETRY_TYPES:
        raise VectorIngestionError(
            f"la unión produjo una geometría no territorial: {geometry.geom_type}"
        )
    try:
        return GeoJSONGeometry.model_validate(mapping(geometry))
    except Exception:
        raise VectorIngestionError("la geometría no pudo convertirse al contrato GeoJSON") from None


def _resolve_source_crs(
    *,
    embedded_crs: Any,
    declared_crs: str | None,
) -> tuple[str, str]:
    embedded = _parse_crs(embedded_crs, label="CRS embebido") if embedded_crs else None
    declared = _parse_crs(declared_crs, label="CRS declarado") if declared_crs else None
    if (
        embedded is not None
        and declared is not None
        and not embedded.equals(
            declared,
            ignore_axis_order=True,
        )
    ):
        raise VectorIngestionError("el --source-crs declarado no coincide con el CRS embebido")
    resolved = embedded or declared
    if resolved is None:
        raise VectorIngestionError(
            "la fuente no declara CRS; indíquelo explícitamente con --source-crs"
        )
    return _crs_identifier(resolved), ("embedded" if embedded is not None else "declared")


def _parse_crs(value: Any, *, label: str) -> CRS:
    try:
        return CRS.from_user_input(value)
    except Exception:
        raise VectorIngestionError(f"{label} inválido") from None


def _crs_identifier(crs: CRS) -> str:
    authority = crs.to_authority()
    return ":".join(authority) if authority is not None else crs.to_string()


def _read_source_files(path: Path) -> dict[str, bytes]:
    candidates = (
        tuple(sorted(item for item in path.parent.glob(f"{path.stem}.*") if item.is_file()))
        if path.suffix.lower() == ".shp"
        else (path,)
    )
    try:
        return {item.name: item.read_bytes() for item in candidates}
    except OSError:
        raise VectorIngestionError("no fue posible conservar la fuente original") from None


def _converted_geojson_bytes(geometry: GeoJSONGeometry) -> bytes:
    payload = {
        "type": "Feature",
        "properties": {},
        "geometry": geometry.model_dump(mode="json"),
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _source_driver(info: Mapping[str, Any]) -> str:
    driver = info.get("driver")
    if not isinstance(driver, str) or not driver:
        raise VectorIngestionError("GDAL no informó el formato de la fuente")
    return driver


def _input_identity(
    *,
    source_files: Mapping[str, bytes],
    converted: bytes,
    source_crs: str,
    selected_layer: str,
    dissolved: bool,
) -> str:
    payload = {
        "source_files": [
            {
                "name": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(source_files.items())
        ],
        "converted_sha256": hashlib.sha256(converted).hexdigest(),
        "source_crs": source_crs,
        "selected_layer": selected_layer,
        "dissolved": dissolved,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _io_error_code(error: Exception) -> str:
    message = str(error).lower()
    if "driver" in message or "unsupported" in message:
        return "unsupported_format_or_driver"
    if "permission" in message or "denied" in message:
        return "permission_denied"
    if "layer" in message:
        return "layer_unreadable"
    return "source_unreadable"
