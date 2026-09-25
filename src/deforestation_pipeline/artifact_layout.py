"""Política única de rutas relativas para los artefactos publicados."""

from __future__ import annotations

import re

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE_PERIOD_ID = re.compile(r"^\d{4}-(DJF|MAM|JJA|SON)$")


def run_json(name: str) -> str:
    return _path("json", "run", _filename(name))


def input_json(name: str) -> str:
    return _path("json", "input", _filename(name))


def geometry_json(name: str) -> str:
    return _path("json", "geometry", _filename(name))


def configuration_json(name: str) -> str:
    return _path("json", "configuration", _filename(name))


def gee_json(name: str) -> str:
    return _path("json", "gee", _filename(name))


def annual_json(name: str, *, year: int | None = None) -> str:
    parts: tuple[str, ...] = ("json", "temporal", "annual")
    if year is not None:
        parts += (_year(year),)
    return _path(*parts, _filename(name))


def seasonal_json(name: str, *, period_id: str | None = None) -> str:
    parts: tuple[str, ...] = ("json", "temporal", "seasonal")
    if period_id is not None:
        parts += (_period(period_id),)
    return _path(*parts, _filename(name))


def evidence_json(name: str) -> str:
    """Publica metadatos analíticos sin crear carpetas por producto."""
    return _path("json", "evidence", _filename(name))


def annual_tiff(year: int, name: str) -> str:
    return _path("tiffs", "annual", _year(year), _filename(name))


def seasonal_tiff(period_id: str, name: str) -> str:
    return _path("tiffs", "seasonal", _period(period_id), _filename(name))


def evidence_tiff(name: str) -> str:
    """Agrupa rasters derivados en un único dominio plano de evidencia."""
    return _path("tiffs", "evidence", _filename(name))


def annual_figure(year: int | None, name: str) -> str:
    period = _year(year) if year is not None else "qa"
    return _path("figures", "annual", period, _filename(name))


def seasonal_figure(period_id: str | None, name: str) -> str:
    period = _period(period_id) if period_id is not None else "qa"
    return _path("figures", "seasonal", period, _filename(name))


def evidence_figure(name: str) -> str:
    """Agrupa figuras derivadas sin repetir la jerarquía científica."""
    return _path("figures", "evidence", _filename(name))


def evidence_table(name: str) -> str:
    """Agrupa tablas analíticas sin abrir carpetas por detector."""
    return _path("tables", "evidence", _filename(name))


def annual_table(name: str) -> str:
    return _path("tables", "annual", _filename(name))


def seasonal_table(name: str) -> str:
    return _path("tables", "seasonal", _filename(name))


def published_raster_path(path: str, *, cadence: str, period_id: str) -> str:
    """Traduce el nombre técnico interno a una ruta pública semántica."""
    root, separator, filename = path.partition("/")
    if separator != "/" or "/" in filename or root not in {"tiffs", "figures"}:
        raise ValueError("artifact_source_path_not_flat_by_type")

    public_name = {
        "hls_annual_reflectance.tif": "reflectance.tif",
        "hls_period_reflectance.tif": "reflectance.tif",
        "hls_annual_indices.tif": "indices.tif",
        "hls_period_indices.tif": "indices.tif",
        "hls_valid_observation_count.tif": "observation_count_total.tif",
        "hls_valid_observation_count_l30.tif": "observation_count_l30.tif",
        "hls_valid_observation_count_s30.tif": "observation_count_s30.tif",
        "valid_observations.png": "observation_count.png",
    }.get(filename, filename)

    if cadence == "annual":
        year = int(period_id)
        return (
            annual_tiff(year, public_name) if root == "tiffs" else annual_figure(year, public_name)
        )
    if cadence == "seasonal":
        return (
            seasonal_tiff(period_id, public_name)
            if root == "tiffs"
            else seasonal_figure(period_id, public_name)
        )
    raise ValueError("artifact_cadence_not_supported")


def _filename(name: str) -> str:
    if not _SAFE_FILENAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError("artifact_name_not_safe")
    return name


def _year(year: int) -> str:
    if year < 1900 or year > 9999:
        raise ValueError("artifact_year_not_safe")
    return str(year)


def _period(period_id: str) -> str:
    if not _SAFE_PERIOD_ID.fullmatch(period_id):
        raise ValueError("artifact_period_not_safe")
    return period_id


def _path(*parts: str) -> str:
    return "/".join(parts)
