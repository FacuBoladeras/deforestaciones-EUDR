"""Renderers determinísticos de presentación para evidencia agrícola raster."""

from __future__ import annotations

import hashlib
import math
import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final

import matplotlib
import numpy as np
import rasterio
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter
from numpy.typing import NDArray
from rasterio.io import MemoryFile
from rasterio.transform import array_bounds

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

_MONTHLY_BANDS: Final = (
    "valid_observation_count",
    "qualifying_crop_count",
    "mean_crop_probability",
    "crop_observation_fraction",
)
AGRICULTURAL_VISUALIZATION_SCHEMA_VERSION: Final = "1.0.0"
_SELECTION_RULE: Final = (
    "first_scheduled+first_spatially_valid+peak_mean_crop_observation_fraction+last_scheduled"
)
_DISCLAIMER_MONTHLY: Final = (
    "Evidencia técnica automática de cobertura mensual; no confirma conversión ni "
    "cumplimiento/certificación EUDR."
)
_DISCLAIMER_PERSISTENCE: Final = (
    "Soporte agrícola persistente según umbrales versionados; no es verdad de terreno ni "
    "confirmación de conversión."
)
_DISCLAIMER_CONJUNCTION: Final = (
    "Evidencia compatible con conversión; no es confirmación legal ni certificación EUDR."
)
_SAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class EvidenceVisualization:
    """PNG y registro reversible que se incorporan juntos al bundle."""

    path: str
    content: bytes
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Raster:
    values: NDArray[np.float64]
    descriptions: tuple[str, ...]
    nodata: float
    crs: str
    transform: rasterio.Affine
    resolution_x_m: float
    resolution_y_m: float


def select_monthly_evidence_windows(rows: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """Selecciona hasta cuatro meses representativos y conserva la serie completa."""
    windows = sorted({str(row["window_id"]) for row in rows})
    if not windows:
        return ()
    by_window = {
        window: [row for row in rows if str(row["window_id"]) == window] for window in windows
    }
    valid = [
        window
        for window in windows
        if sum(
            _float_value(row.get("area_ha", 0.0))
            for row in by_window[window]
            if row.get("class_name") in {"crop", "unknown"}
        )
        > 0
    ]
    candidates = [windows[0]]
    if valid:
        candidates.append(valid[0])
        peak = max(
            valid,
            key=lambda window: (
                _window_metric(by_window[window], "mean_crop_observation_fraction"),
                -windows.index(window),
            ),
        )
        candidates.append(peak)
    candidates.append(windows[-1])
    selected = set(candidates)
    return tuple(window for window in windows if window in selected)


def render_monthly_agricultural_evidence(
    *,
    event_id: str,
    rows: Sequence[Mapping[str, object]],
    raster_contents: Mapping[str, bytes],
    footprint_content: bytes,
    source_label: str,
    footprint_path: str = "event_footprint_fraction.tif",
) -> EvidenceVisualization | None:
    """Crea una lámina por evento: serie completa y mapas de meses seleccionados."""
    windows = sorted({str(row["window_id"]) for row in rows})
    if not windows:
        return None
    if not any(
        _float_value(row.get("area_ha", 0.0)) > 0 and row.get("class_name") in {"crop", "unknown"}
        for row in rows
    ):
        return None
    selected = select_monthly_evidence_windows(rows)
    footprint = _read_raster(footprint_content, ("event_footprint_fraction",))
    by_window = {
        window: [row for row in rows if str(row["window_id"]) == window] for window in windows
    }
    raster_paths = {window: str(by_window[window][0]["raster_path"]) for window in windows}
    for path in raster_paths.values():
        if path not in raster_contents:
            raise ValueError("agricultural_visualization_source_raster_missing")
    rasters: dict[str, _Raster] = {}
    for window in selected:
        path = raster_paths[window]
        raster = _read_raster(raster_contents[path], _MONTHLY_BANDS)
        _require_same_grid(footprint, raster)
        rasters[window] = raster

    column_count = max(1, len(selected))
    figure = plt.figure(figsize=(max(10.0, 3.5 * column_count), 10.2), constrained_layout=True)
    grid = figure.add_gridspec(3, column_count, height_ratios=(1.25, 1, 1))
    timeline = figure.add_subplot(grid[0, :])
    event_area = max(_float_value(row.get("event_area_ha", 0.0)) for row in rows)
    valid_fraction: list[float] = []
    crop_area_fraction: list[float] = []
    nodata_fraction: list[float] = []
    mean_fraction: list[float] = []
    mean_probability: list[float] = []
    for window in windows:
        grouped = by_window[window]
        crop_area = _class_area(grouped, "crop")
        unknown_area = _class_area(grouped, "unknown")
        nodata_area = _class_area(grouped, "nodata")
        denominator = event_area or crop_area + unknown_area + nodata_area or 1.0
        valid_fraction.append(min(1.0, (crop_area + unknown_area) / denominator))
        crop_area_fraction.append(min(1.0, crop_area / denominator))
        nodata_fraction.append(min(1.0, nodata_area / denominator))
        mean_fraction.append(_window_metric(grouped, "mean_crop_observation_fraction", nan=True))
        mean_probability.append(_window_metric(grouped, "mean_crop_probability", nan=True))
    positions = np.arange(len(windows))
    timeline.plot(positions, valid_fraction, label="fracción válida", color="#2166ac")
    timeline.plot(positions, crop_area_fraction, label="área clasificada cultivo", color="#1b9e77")
    timeline.plot(positions, nodata_fraction, label="nodata", color="#777777")
    timeline.plot(
        positions,
        mean_fraction,
        label="media fracción observaciones cultivo",
        color="#d95f02",
        linestyle="--",
    )
    timeline.plot(
        positions,
        mean_probability,
        label="media score crop",
        color="#7570b3",
        linestyle=":",
    )
    for window in selected:
        timeline.axvline(windows.index(window), color="#222222", alpha=0.15, linewidth=1)
    tick_step = max(1, math.ceil(len(windows) / 12))
    ticks = positions[::tick_step]
    timeline.set_xticks(ticks, [windows[index] for index in ticks], rotation=45, ha="right")
    timeline.set_ylim(-0.03, 1.03)
    timeline.set_ylabel("Fracción [0-1]")
    timeline.set_title("Serie completa poscambio (los paneles seleccionados están marcados)")
    timeline.grid(alpha=0.2)
    timeline.legend(loc="upper center", ncols=3, fontsize=8)

    for column, window in enumerate(selected):
        raster = rasters[window]
        scenes = _int_value(by_window[window][0].get("matched_scene_count", 0))
        for row_index, (band_index, label, cmap) in enumerate(
            (
                (3, "Fracción de observaciones crop", "YlGn"),
                (2, "Score medio crop", "viridis"),
            ),
            start=1,
        ):
            axis = figure.add_subplot(grid[row_index, column])
            _draw_fraction_map(
                axis,
                raster=raster,
                footprint=footprint,
                band_index=band_index,
                cmap_name=cmap,
                add_navigation=column == 0 and row_index == 1,
                add_legend=column == 0,
            )
            axis.set_title(f"{window} · {label}\n{scenes} escenas", fontsize=9)
            if column == 0:
                axis.set_ylabel("Norte relativo [m]")
            axis.set_xlabel("Este relativo [m]")

    figure.suptitle(
        f"Evidencia agrícola mensual — evento {event_id}\n{source_label}",
        fontsize=15,
    )
    figure.text(
        0.5,
        -0.02,
        _footer(
            footprint,
            zero_semantics="observado sin soporte crop",
            disclaimer=_DISCLAIMER_MONTHLY,
        ),
        ha="center",
        fontsize=8,
    )
    content = _save_png(
        figure,
        title="Evidencia agrícola mensual",
        event_id=event_id,
        disclaimer=_DISCLAIMER_MONTHLY,
        extra={"SelectionRule": _SELECTION_RULE},
    )
    path = f"figures/evidence/agricultural_monthly_evidence_{_safe_id(event_id)}.png"
    source_rasters = [
        _source_record(raster_paths[window], raster_contents[raster_paths[window]])
        for window in windows
    ]
    source_rasters.append(_source_record(footprint_path, footprint_content))
    return _visualization(
        path=path,
        content=content,
        visualization_type="agricultural_monthly_evidence_sheet",
        event_id=event_id,
        source_rasters=source_rasters,
        details={
            "selection_rule": _SELECTION_RULE,
            "selected_window_ids": list(selected),
            "all_window_ids": windows,
            "temporal_coverage": {"first_window": windows[0], "last_window": windows[-1]},
            "band_semantics": list(_MONTHLY_BANDS),
            "nodata_semantics": "gris: sin observación válida dentro del evento",
            "zero_semantics": "valor cero: observación válida sin soporte crop",
            "grid": _grid_record(footprint),
            "disclaimer": _DISCLAIMER_MONTHLY,
        },
    )


def render_agricultural_persistence_evidence(
    *,
    event: Mapping[str, object],
    raster_content: bytes,
    source_label: str,
    source_raster_path: str = "persistent_crop_support.tif",
) -> EvidenceVisualization | None:
    """Renderiza soporte persistente más la trayectoria y sensibilidad que lo originan."""
    raster = _read_raster(raster_content, ("persistent_crop_event_fraction",))
    if not _has_positive_support(raster):
        return None
    event_id = str(event["event_id"])
    figure = plt.figure(figsize=(12, 7.5), constrained_layout=True)
    grid = figure.add_gridspec(2, 2, width_ratios=(1.25, 1))
    map_axis = figure.add_subplot(grid[:, 0])
    _draw_fraction_map(
        map_axis,
        raster=raster,
        footprint=None,
        band_index=0,
        cmap_name="YlGn",
        add_navigation=True,
        add_legend=True,
    )
    map_axis.set_title("Fracción espacial con soporte agrícola persistente")
    map_axis.set_xlabel("Este relativo [m]")
    map_axis.set_ylabel("Norte relativo [m]")

    periods = _mapping_sequence(event.get("per_period_support", []))
    timeline = figure.add_subplot(grid[0, 1])
    if periods:
        labels = [str(item["window_id"]) for item in periods if isinstance(item, Mapping)]
        valid = [_float_value(item["valid_area_ha"]) for item in periods]
        crop = [_float_value(item["qualifying_crop_area_ha"]) for item in periods]
        timeline.plot(labels, valid, label="área válida", color="#2166ac")
        timeline.plot(labels, crop, label="área crop calificante", color="#1b9e77")
        tick_step = max(1, math.ceil(len(labels) / 8))
        timeline.set_xticks(range(0, len(labels), tick_step), labels[::tick_step], rotation=45)
        timeline.legend(fontsize=8)
    timeline.set_ylabel("ha")
    timeline.set_title("Soporte temporal completo")
    timeline.grid(alpha=0.2)

    sensitivity_axis = figure.add_subplot(grid[1, 1])
    sensitivity = _mapping_sequence(event.get("sensitivity", []))
    if sensitivity:
        thresholds = [
            _float_value(item["crop_observation_fraction_threshold"]) for item in sensitivity
        ]
        areas = [_float_value(item["persistent_crop_area_ha"]) for item in sensitivity]
        sensitivity_axis.plot(thresholds, areas, marker="o", color="#d95f02")
    sensitivity_axis.set_xlabel("Umbral fracción crop")
    sensitivity_axis.set_ylabel("Área persistente [ha]")
    sensitivity_axis.set_title("Sensibilidad")
    sensitivity_axis.grid(alpha=0.2)

    start = str(event.get("post_change_window_start", "no disponible"))
    end = str(event.get("post_change_window_end", "no disponible"))
    figure.suptitle(
        f"Persistencia agrícola — evento {event_id}\n{source_label} · {start} a {end}",
        fontsize=15,
    )
    figure.text(
        0.5,
        -0.02,
        _footer(
            raster,
            zero_semantics="evaluado sin persistencia",
            disclaimer=_DISCLAIMER_PERSISTENCE,
        ),
        ha="center",
        fontsize=8,
    )
    content = _save_png(
        figure,
        title="Persistencia agrícola",
        event_id=event_id,
        disclaimer=_DISCLAIMER_PERSISTENCE,
    )
    path = f"figures/evidence/agricultural_persistence_{_safe_id(event_id)}.png"
    return _visualization(
        path=path,
        content=content,
        visualization_type="agricultural_persistence_support",
        event_id=event_id,
        source_rasters=[_source_record(source_raster_path, raster_content)],
        details={
            "temporal_coverage": {"start": start, "end": end},
            "band_semantics": ["persistent_crop_event_fraction"],
            "nodata_semantics": "gris: fuera de cobertura evaluable",
            "zero_semantics": "valor cero: evaluado sin soporte persistente",
            "grid": _grid_record(raster),
            "disclaimer": _DISCLAIMER_PERSISTENCE,
        },
    )


def render_likely_conversion_conjunction(
    *,
    event: Mapping[str, object],
    raster_content: bytes,
    source_raster_path: str = "likely_conversion_conjunctive_support.tif",
) -> EvidenceVisualization | None:
    """Renderiza la conjunción espacial compatible con conversión, nunca confirmación."""
    raster = _read_raster(
        raster_content,
        ("likely_conversion_conjunctive_event_fraction",),
    )
    if not _has_positive_support(raster):
        return None
    event_id = str(event["event_id"])
    figure, (axis, text_axis) = plt.subplots(
        1,
        2,
        figsize=(11, 6.5),
        constrained_layout=True,
        gridspec_kw={"width_ratios": (1.5, 0.8)},
    )
    _draw_fraction_map(
        axis,
        raster=raster,
        footprint=None,
        band_index=0,
        cmap_name="YlOrRd",
        add_navigation=True,
        add_legend=True,
    )
    axis.set_title("Fracción de la conjunción espacial")
    axis.set_xlabel("Este relativo [m]")
    axis.set_ylabel("Norte relativo [m]")
    text_axis.axis("off")
    text_axis.text(
        0,
        1,
        "Conjunción requerida:\n"
        "• bosque al corte 2020\n"
        "• pérdida poscorte\n"
        "• no-bosque persistente\n"
        "• agricultura persistente\n\n"
        f"Área compatible: {_float_value(event.get('likely_conversion_area_ha', 0.0)):.4f} ha\n"
        f"Área agrícola persistente: "
        f"{_float_value(event.get('persistent_agricultural_area_ha', 0.0)):.4f} ha\n"
        f"Estado automático: {event.get('automatic_status', 'review_required')}\n\n"
        "Resultado automático sujeto a revisión humana.",
        va="top",
        fontsize=10,
    )
    figure.suptitle(f"Evidencia compatible con conversión — evento {event_id}", fontsize=15)
    figure.text(
        0.5,
        -0.02,
        _footer(
            raster,
            zero_semantics="evaluado sin conjunción completa",
            disclaimer=_DISCLAIMER_CONJUNCTION,
        ),
        ha="center",
        fontsize=8,
    )
    content = _save_png(
        figure,
        title="Conjunción compatible con conversión",
        event_id=event_id,
        disclaimer=_DISCLAIMER_CONJUNCTION,
    )
    path = f"figures/evidence/likely_conversion_conjunction_{_safe_id(event_id)}.png"
    return _visualization(
        path=path,
        content=content,
        visualization_type="likely_conversion_spatial_conjunction",
        event_id=event_id,
        source_rasters=[_source_record(source_raster_path, raster_content)],
        details={
            "conjunction_semantics": (
                "forest_2020 AND post_cutoff_loss AND persistent_nonforest "
                "AND persistent_agriculture"
            ),
            "band_semantics": ["likely_conversion_conjunctive_event_fraction"],
            "nodata_semantics": "gris: fuera de cobertura evaluable",
            "zero_semantics": "valor cero: evaluado sin conjunción completa",
            "grid": _grid_record(raster),
            "disclaimer": _DISCLAIMER_CONJUNCTION,
        },
    )


def _read_raster(content: bytes, expected_descriptions: tuple[str, ...]) -> _Raster:
    try:
        with MemoryFile(content) as memory:
            with memory.open() as dataset:
                descriptions = tuple(item or "" for item in dataset.descriptions)
                if descriptions != expected_descriptions:
                    raise ValueError("agricultural_visualization_band_semantics_mismatch")
                if (
                    dataset.crs is None
                    or dataset.nodata is None
                    or dataset.count != len(expected_descriptions)
                ):
                    raise ValueError("agricultural_visualization_invalid_raster")
                if not dataset.crs.is_projected:
                    raise ValueError("agricultural_visualization_requires_projected_crs")
                if dataset.crs.linear_units.lower() not in {"metre", "meter"}:
                    raise ValueError("agricultural_visualization_requires_metric_crs")
                transform = dataset.transform
                if (
                    transform.a <= 0
                    or transform.e >= 0
                    or not math.isclose(transform.b, 0.0, abs_tol=1e-12)
                    or not math.isclose(transform.d, 0.0, abs_tol=1e-12)
                ):
                    raise ValueError("agricultural_visualization_invalid_grid")
                values = dataset.read().astype("float64")
                if values.size == 0:
                    raise ValueError("agricultural_visualization_invalid_raster")
                values.setflags(write=False)
                return _Raster(
                    values=values,
                    descriptions=descriptions,
                    nodata=float(dataset.nodata),
                    crs=dataset.crs.to_string(),
                    transform=transform,
                    resolution_x_m=abs(float(transform.a)),
                    resolution_y_m=abs(float(transform.e)),
                )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("agricultural_visualization_invalid_raster") from exc


def _require_same_grid(first: _Raster, second: _Raster) -> None:
    if (
        first.values.shape[1:] != second.values.shape[1:]
        or first.crs != second.crs
        or not first.transform.almost_equals(second.transform)
    ):
        raise ValueError("agricultural_visualization_grid_mismatch")


def _draw_fraction_map(
    axis: Any,
    *,
    raster: _Raster,
    footprint: _Raster | None,
    band_index: int,
    cmap_name: str,
    add_navigation: bool,
    add_legend: bool,
) -> None:
    values = raster.values[band_index]
    inside = (
        np.ones(values.shape, dtype=bool)
        if footprint is None
        else np.isfinite(footprint.values[0])
        & ~np.isclose(footprint.values[0], footprint.nodata)
        & (footprint.values[0] > 0)
    )
    valid = inside & np.isfinite(values) & ~np.isclose(values, raster.nodata)
    rgba = np.zeros((*values.shape, 4), dtype="float64")
    rgba[inside & ~valid] = (0.62, 0.62, 0.62, 1.0)
    if np.any(valid):
        normalized = np.clip(values[valid], 0.0, 1.0)
        rgba[valid] = matplotlib.colormaps[cmap_name](normalized)
    west, south, east, north = array_bounds(values.shape[0], values.shape[1], raster.transform)
    axis.imshow(rgba, extent=(west, east, south, north), origin="upper", interpolation="nearest")
    axis.set_aspect("equal")
    axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"{value - west:.0f}"))
    axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"{value - south:.0f}"))
    axis.tick_params(labelsize=7)
    scalar = ScalarMappable(norm=Normalize(0, 1), cmap=cmap_name)
    axis.figure.colorbar(scalar, ax=axis, fraction=0.045, pad=0.02, label="fracción / score")
    if add_legend:
        axis.legend(
            handles=[
                Patch(facecolor="#9e9e9e", label="nodata"),
                Patch(facecolor=matplotlib.colormaps[cmap_name](0.0), label="cero observado"),
            ],
            loc="upper left",
            fontsize=7,
        )
    if add_navigation:
        _add_north_and_scale(axis, west=west, east=east, south=south, north=north)


def _add_north_and_scale(
    axis: Any, *, west: float, east: float, south: float, north: float
) -> None:
    width = east - west
    height = north - south
    axis.annotate(
        "N",
        xy=(east - width * 0.08, north - height * 0.05),
        xytext=(east - width * 0.08, north - height * 0.22),
        ha="center",
        va="center",
        arrowprops={"arrowstyle": "-|>", "color": "black", "linewidth": 1.5},
        fontsize=9,
        fontweight="bold",
    )
    scale = _nice_scale(width * 0.25)
    x0 = west + width * 0.08
    y0 = south + height * 0.08
    axis.plot([x0, x0 + scale], [y0, y0], color="black", linewidth=3)
    axis.text(x0 + scale / 2, y0 + height * 0.025, _scale_label(scale), ha="center", fontsize=7)


def _nice_scale(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10.0 ** math.floor(math.log10(value))
    fraction = value / magnitude
    nice = 1.0 if fraction < 2 else 2.0 if fraction < 5 else 5.0
    return nice * magnitude


def _scale_label(metres: float) -> str:
    return f"{metres / 1000:g} km" if metres >= 1000 else f"{metres:g} m"


def _has_positive_support(raster: _Raster) -> bool:
    values = raster.values[0]
    valid = np.isfinite(values) & ~np.isclose(values, raster.nodata)
    return bool(np.any(valid & (values > 0)))


def _window_metric(rows: Sequence[Mapping[str, object]], key: str, *, nan: bool = False) -> float:
    for row in rows:
        value = row.get(key)
        if value is not None:
            return _float_value(value)
    return float("nan") if nan else -1.0


def _class_area(rows: Sequence[Mapping[str, object]], class_name: str) -> float:
    return sum(
        _float_value(row.get("area_ha", 0.0)) for row in rows if row.get("class_name") == class_name
    )


def _float_value(value: object) -> float:
    if isinstance(value, (str, int, float)):
        return float(value)
    raise ValueError("agricultural_visualization_numeric_value_invalid")


def _int_value(value: object) -> int:
    if isinstance(value, (str, int)):
        return int(value)
    raise ValueError("agricultural_visualization_integer_value_invalid")


def _mapping_sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _save_png(
    figure: Any,
    *,
    title: str,
    event_id: str,
    disclaimer: str,
    extra: Mapping[str, str] | None = None,
) -> bytes:
    stream = BytesIO()
    metadata = {
        "Software": "deforestation-pipeline",
        "Title": title,
        "EventID": event_id,
        "Disclaimer": disclaimer,
        **dict(extra or {}),
    }
    try:
        figure.savefig(
            stream,
            format="png",
            dpi=150,
            metadata=metadata,
            bbox_inches="tight",
            pad_inches=0.25,
        )
    finally:
        plt.close(figure)
    return stream.getvalue()


def _source_record(path: str, content: bytes) -> dict[str, object]:
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if (
        not path
        or "\\" in path
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or str(posix_path) in {"", "."}
    ):
        raise ValueError("agricultural_visualization_source_path_unsafe")
    return {"path": path, "sha256": hashlib.sha256(content).hexdigest()}


def _footer(raster: _Raster, *, zero_semantics: str, disclaimer: str) -> str:
    resolution = (
        f"{raster.resolution_x_m:g} m"
        if math.isclose(raster.resolution_x_m, raster.resolution_y_m, rel_tol=1e-9)
        else f"{raster.resolution_x_m:g} x {raster.resolution_y_m:g} m"
    )
    return textwrap.fill(
        f"CRS {raster.crs} · resolución {resolution} · gris = nodata · "
        f"cero = {zero_semantics} · {disclaimer}",
        width=155,
    )


def _grid_record(raster: _Raster) -> dict[str, object]:
    return {
        "crs": raster.crs,
        "resolution_x_m": raster.resolution_x_m,
        "resolution_y_m": raster.resolution_y_m,
        "north_up": True,
    }


def _visualization(
    *,
    path: str,
    content: bytes,
    visualization_type: str,
    event_id: str,
    source_rasters: list[dict[str, object]],
    details: Mapping[str, object],
) -> EvidenceVisualization:
    record = {
        "schema_version": AGRICULTURAL_VISUALIZATION_SCHEMA_VERSION,
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "media_type": "image/png",
        "visualization_type": visualization_type,
        "event_id": event_id,
        "source_rasters": source_rasters,
        **dict(details),
    }
    return EvidenceVisualization(path=path, content=content, record=record)


def _safe_id(value: str) -> str:
    safe = _SAFE_IDENTIFIER.sub("-", value.strip()).strip("-")
    if not safe:
        raise ValueError("agricultural_visualization_event_id_invalid")
    return safe[:120]
