"""Segmentación espacial y evidencia de eventos de perturbación persistente."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from typing import Annotated, Any, Literal, cast

import matplotlib
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rasterio.features import shapes
from rasterio.transform import Affine
from rasterio.warp import transform_geom
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from deforestation_pipeline.artifact_layout import evidence_figure, evidence_json, evidence_table
from deforestation_pipeline.change_detection import DetectorConvergenceReasonCode
from deforestation_pipeline.schemas import EUDR_CUTOFF_DATE, RasterGridSpec

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

DISTURBANCE_EVENT_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
EVENT_TYPE: Literal["persistent_disturbance_event"] = "persistent_disturbance_event"
PRIMARY_DOMAIN: Literal["automated_forest"] = "automated_forest"
ONSET_SEMANTICS: Literal["earliest_robust_seasonal_window_not_exact_date"] = (
    "earliest_robust_seasonal_window_not_exact_date"
)
EVENT_ONSET_FIGURE_LABEL = "primera ventana anómala robusta estimada"


class DisturbanceEventConfig(BaseModel):
    """Parámetros versionados de agregación; el umbral no elimina eventos."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = DISTURBANCE_EVENT_SCHEMA_VERSION
    connectivity: Literal[8]
    area_threshold_ha: Annotated[float, Field(gt=0)]
    area_method: Literal["projected_grid_affine_determinant"]
    area_threshold_policy: Literal["flag_without_filtering"]
    event_id_strategy: Literal["grid_and_pixels_sha256_v1"]
    ordering_policy: Literal["area_descending_then_event_id"]
    detail_figure_limit: Annotated[int, Field(ge=0, le=20, strict=True)]
    detail_selection_policy: Literal["largest_area_then_event_id"]
    comparison_policy: Literal["latest_pre_cutoff_same_season_vs_earliest_event_onset_window"]
    geometry_crs: Literal["EPSG:4326"]
    primary_interpretation_domain: Literal["automated_forest"]
    automatic_final_assessment_allowed: Literal[False]
    attribution_allowed: Literal[False]


@dataclass(frozen=True, slots=True)
class SegmentedEvent:
    """Componente conexo determinístico aún sin estadísticas temporales."""

    event_id: str
    pixels: tuple[tuple[int, int], ...]
    pixel_count: int
    area_ha: float
    area_threshold_met: bool


@dataclass(frozen=True, slots=True)
class EventSegmentation:
    """Resultado puro de la segmentación sobre el dominio forestal automático."""

    events: tuple[SegmentedEvent, ...]
    label_raster: NDArray[np.int32]
    outside_primary_domain_pixel_count: int


class DisturbanceEvent(BaseModel):
    """Registro auditable de un componente, sin atribuir uso posterior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    event_type: Literal["persistent_disturbance_event"]
    primary_interpretation_domain: Literal["automated_forest"]
    pixel_count: Annotated[int, Field(gt=0)]
    area_ha: Annotated[float, Field(gt=0)]
    area_threshold_ha: Annotated[float, Field(gt=0)]
    area_threshold_met: bool
    estimated_onset_period_id: str | None
    estimated_onset_window_start: date | None
    estimated_onset_window_end: date | None
    onset_semantics: Literal["earliest_robust_seasonal_window_not_exact_date"]
    maximum_disturbance_magnitude: float | None
    mean_disturbance_magnitude: float | None
    maximum_disturbance_score: float | None
    mean_disturbance_score: float | None
    ccdc_support_pixel_count: Annotated[int, Field(ge=0)]
    dual_detector_agreement_pixel_count: Annotated[int, Field(ge=0)]
    maximum_available_detector_count: Annotated[int, Field(ge=0)]
    dominant_convergence_reason_code: Annotated[int, Field(ge=0)]
    dominant_convergence_reason: str
    quality_flags_union: Annotated[int, Field(ge=0)]
    bbox_wgs84: tuple[float, float, float, float]
    detail_figure_path: str | None
    attribution_generated: Literal[False]
    automatic_final_assessment_generated: Literal[False]


class DisturbanceEventCollection(BaseModel):
    """Contrato JSON de todos los eventos y su criterio de presentación."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"]
    generated_at: datetime
    scientific_parameters_hash: str
    event_type: Literal["persistent_disturbance_event"]
    primary_interpretation_domain: Literal["automated_forest"]
    area_crs: str
    pixel_area_ha: Annotated[float, Field(gt=0)]
    segmentation: DisturbanceEventConfig
    event_count: Annotated[int, Field(ge=0)]
    total_event_area_ha: Annotated[float, Field(ge=0)]
    area_threshold_event_count: Annotated[int, Field(ge=0)]
    below_area_threshold_event_count: Annotated[int, Field(ge=0)]
    outside_primary_domain_persistent_pixel_count: Annotated[int, Field(ge=0)]
    detail_figure_count: Annotated[int, Field(ge=0)]
    detail_figure_selection_policy: Literal["largest_area_then_event_id"]
    onset_semantics: Literal["earliest_robust_seasonal_window_not_exact_date"]
    attribution_generated: Literal[False]
    automatic_final_assessment_generated: Literal[False]
    events: tuple[DisturbanceEvent, ...]

    @model_validator(mode="after")
    def counts_match_events(self) -> DisturbanceEventCollection:
        if self.event_count != len(self.events):
            raise ValueError("event_count_mismatch")
        met = sum(event.area_threshold_met for event in self.events)
        if self.area_threshold_event_count != met:
            raise ValueError("area_threshold_event_count_mismatch")
        if self.below_area_threshold_event_count != self.event_count - met:
            raise ValueError("below_area_threshold_event_count_mismatch")
        detail_count = sum(event.detail_figure_path is not None for event in self.events)
        if self.detail_figure_count != detail_count:
            raise ValueError("detail_figure_count_mismatch")
        return self


@dataclass(frozen=True, slots=True)
class DisturbanceEventMaterialization:
    """Artefactos tabulares, vectoriales y visuales de los eventos."""

    files: Mapping[str, bytes]
    collection: DisturbanceEventCollection


def segment_persistent_disturbance_events(
    *,
    persistent_mask: NDArray[Any],
    automated_forest_mask: NDArray[Any],
    grid_spec: RasterGridSpec,
    connectivity: Literal[8],
    area_threshold_ha: float,
) -> EventSegmentation:
    """Agrupa píxeles persistentes en 8 vecinos, exclusivamente dentro del dominio."""
    persistent = _boolean_array(persistent_mask, grid_spec=grid_spec, name="persistent_mask")
    automated = _boolean_array(
        automated_forest_mask,
        grid_spec=grid_spec,
        name="automated_forest_mask",
    )
    if connectivity != 8:
        raise ValueError("event_connectivity_must_be_8")
    if not math.isfinite(area_threshold_ha) or area_threshold_ha <= 0:
        raise ValueError("event_area_threshold_invalid")
    target = persistent & automated
    outside_count = int(np.count_nonzero(persistent & ~automated))
    labels = np.zeros(target.shape, dtype=np.int32)
    components: list[tuple[tuple[int, int], ...]] = []
    neighbor_offsets = (
        (-1, -1),
        (-1, 0),
        (-1, 1),
        (0, -1),
        (0, 1),
        (1, -1),
        (1, 0),
        (1, 1),
    )
    next_label = 1
    for row in range(grid_spec.height):
        for column in range(grid_spec.width):
            if not target[row, column] or labels[row, column] != 0:
                continue
            queue: deque[tuple[int, int]] = deque(((row, column),))
            labels[row, column] = next_label
            pixels: list[tuple[int, int]] = []
            while queue:
                current_row, current_column = queue.popleft()
                pixels.append((current_row, current_column))
                for row_offset, column_offset in neighbor_offsets:
                    candidate_row = current_row + row_offset
                    candidate_column = current_column + column_offset
                    if not (
                        0 <= candidate_row < grid_spec.height
                        and 0 <= candidate_column < grid_spec.width
                    ):
                        continue
                    if (
                        target[candidate_row, candidate_column]
                        and labels[candidate_row, candidate_column] == 0
                    ):
                        labels[candidate_row, candidate_column] = next_label
                        queue.append((candidate_row, candidate_column))
            components.append(tuple(sorted(pixels)))
            next_label += 1

    pixel_area_ha = _pixel_area_ha(grid_spec)
    events = tuple(
        SegmentedEvent(
            event_id=_event_id(grid_spec.grid_sha256, pixels),
            pixels=pixels,
            pixel_count=len(pixels),
            area_ha=round(len(pixels) * pixel_area_ha, 8),
            area_threshold_met=(len(pixels) * pixel_area_ha) >= area_threshold_ha,
        )
        for pixels in components
    )
    events = tuple(sorted(events, key=lambda event: (-event.area_ha, event.event_id)))
    labels.setflags(write=False)
    return EventSegmentation(
        events=events,
        label_raster=labels,
        outside_primary_domain_pixel_count=outside_count,
    )


def materialize_persistent_disturbance_events(
    *,
    persistent_mask: NDArray[Any],
    automated_forest_mask: NDArray[Any],
    first_anomalous_period_index: NDArray[Any],
    maximum_disturbance_magnitude: NDArray[Any],
    disturbance_score: NDArray[Any],
    convergence_reason_code: NDArray[Any],
    available_detector_count: NDArray[Any],
    agreeing_detector_count: NDArray[Any],
    ccdc_break_day_offset: NDArray[Any],
    quality_flags_bitmask: NDArray[Any],
    post_period_ids: Sequence[str],
    post_period_start_dates: Sequence[date],
    post_period_end_dates_exclusive: Sequence[date],
    all_period_ids: Sequence[str],
    all_period_seasons: Sequence[str],
    all_period_start_dates: Sequence[date],
    all_period_end_dates_exclusive: Sequence[date],
    index_names: Sequence[str],
    index_cube: NDArray[Any],
    grid_spec: RasterGridSpec,
    config: DisturbanceEventConfig,
    generated_at: datetime,
    scientific_parameters_hash: str,
    png_dpi: int,
) -> DisturbanceEventMaterialization:
    """Calcula atributos y serializa todos los eventos, con fichas limitadas."""
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("event_generated_at_requires_timezone")
    if len(scientific_parameters_hash) != 64:
        raise ValueError("event_scientific_parameters_hash_invalid")
    if not 72 <= png_dpi <= 600:
        raise ValueError("event_png_dpi_invalid")
    _validate_period_axis(
        post_period_ids,
        post_period_start_dates,
        post_period_end_dates_exclusive,
        name="post",
    )
    if not (
        len(all_period_ids)
        == len(all_period_seasons)
        == len(all_period_start_dates)
        == len(all_period_end_dates_exclusive)
    ):
        raise ValueError("event_all_period_axis_mismatch")
    if len(set(all_period_ids)) != len(all_period_ids):
        raise ValueError("event_all_period_ids_duplicated")
    cube = np.asarray(index_cube, dtype=np.float64)
    expected_cube_shape = (
        len(all_period_ids),
        len(index_names),
        grid_spec.height,
        grid_spec.width,
    )
    if cube.shape != expected_cube_shape:
        raise ValueError("event_index_cube_shape_mismatch")
    if len(set(index_names)) != len(index_names) or not index_names:
        raise ValueError("event_index_names_invalid")
    arrays = {
        "first_anomalous_period_index": first_anomalous_period_index,
        "maximum_disturbance_magnitude": maximum_disturbance_magnitude,
        "disturbance_score": disturbance_score,
        "convergence_reason_code": convergence_reason_code,
        "available_detector_count": available_detector_count,
        "agreeing_detector_count": agreeing_detector_count,
        "ccdc_break_day_offset": ccdc_break_day_offset,
        "quality_flags_bitmask": quality_flags_bitmask,
    }
    normalized = {
        name: _numeric_array(value, grid_spec=grid_spec, name=name)
        for name, value in arrays.items()
    }
    automated = _boolean_array(
        automated_forest_mask,
        grid_spec=grid_spec,
        name="automated_forest_mask",
    )
    persistent = _boolean_array(persistent_mask, grid_spec=grid_spec, name="persistent_mask")
    segmentation = segment_persistent_disturbance_events(
        persistent_mask=persistent,
        automated_forest_mask=automated,
        grid_spec=grid_spec,
        connectivity=config.connectivity,
        area_threshold_ha=config.area_threshold_ha,
    )
    selected_ids = {event.event_id for event in segmentation.events[: config.detail_figure_limit]}
    event_records: list[DisturbanceEvent] = []
    wgs84_geometries: dict[str, Mapping[str, Any]] = {}
    event_masks: dict[str, NDArray[np.bool_]] = {}
    detail_paths: dict[str, str] = {}
    for event in segmentation.events:
        event_mask = np.zeros((grid_spec.height, grid_spec.width), dtype=np.bool_)
        rows, columns = zip(*event.pixels, strict=True)
        event_mask[np.asarray(rows), np.asarray(columns)] = True
        event_masks[event.event_id] = event_mask
        projected_geometry = _event_geometry(event_mask, grid_spec=grid_spec)
        wgs84_geometry = cast(
            Mapping[str, Any],
            transform_geom(
                grid_spec.target_crs,
                "EPSG:4326",
                projected_geometry,
                precision=8,
            ),
        )
        _validate_polygonal_geometry(
            shape(dict(wgs84_geometry)),
            error_code="event_wgs84_geometry_invalid",
        )
        wgs84_geometries[event.event_id] = wgs84_geometry
        bounds = shape(dict(wgs84_geometry)).bounds
        onset_index = _earliest_period_index(
            normalized["first_anomalous_period_index"][event_mask],
            period_count=len(post_period_ids),
        )
        if onset_index is None:
            onset_period_id = None
            onset_start = None
            onset_end = None
        else:
            onset_period_id = post_period_ids[onset_index]
            onset_start = post_period_start_dates[onset_index]
            onset_end = post_period_end_dates_exclusive[onset_index] - timedelta(days=1)
        reason_code = _dominant_integer(normalized["convergence_reason_code"][event_mask])
        selected_path = (
            evidence_figure(f"disturbance_event_{event.event_id.lower()}.png")
            if event.event_id in selected_ids
            else None
        )
        if selected_path is not None:
            detail_paths[event.event_id] = selected_path
        record = DisturbanceEvent(
            event_id=event.event_id,
            event_type=EVENT_TYPE,
            primary_interpretation_domain=PRIMARY_DOMAIN,
            pixel_count=event.pixel_count,
            area_ha=event.area_ha,
            area_threshold_ha=config.area_threshold_ha,
            area_threshold_met=event.area_threshold_met,
            estimated_onset_period_id=onset_period_id,
            estimated_onset_window_start=onset_start,
            estimated_onset_window_end=onset_end,
            onset_semantics=ONSET_SEMANTICS,
            maximum_disturbance_magnitude=_finite_stat(
                normalized["maximum_disturbance_magnitude"][event_mask],
                reducer="max",
            ),
            mean_disturbance_magnitude=_finite_stat(
                normalized["maximum_disturbance_magnitude"][event_mask],
                reducer="mean",
            ),
            maximum_disturbance_score=_finite_stat(
                normalized["disturbance_score"][event_mask],
                reducer="max",
            ),
            mean_disturbance_score=_finite_stat(
                normalized["disturbance_score"][event_mask],
                reducer="mean",
            ),
            ccdc_support_pixel_count=int(
                np.count_nonzero(np.isfinite(normalized["ccdc_break_day_offset"][event_mask]))
            ),
            dual_detector_agreement_pixel_count=int(
                np.count_nonzero(normalized["agreeing_detector_count"][event_mask] >= 2)
            ),
            maximum_available_detector_count=_maximum_nonnegative_integer(
                normalized["available_detector_count"][event_mask]
            ),
            dominant_convergence_reason_code=reason_code,
            dominant_convergence_reason=_reason_name(reason_code),
            quality_flags_union=_integer_bitwise_union(
                normalized["quality_flags_bitmask"][event_mask]
            ),
            bbox_wgs84=cast(tuple[float, float, float, float], tuple(round(x, 8) for x in bounds)),
            detail_figure_path=selected_path,
            attribution_generated=False,
            automatic_final_assessment_generated=False,
        )
        event_records.append(record)

    collection = DisturbanceEventCollection(
        schema_version=DISTURBANCE_EVENT_SCHEMA_VERSION,
        generated_at=generated_at,
        scientific_parameters_hash=scientific_parameters_hash,
        event_type=EVENT_TYPE,
        primary_interpretation_domain=PRIMARY_DOMAIN,
        area_crs=grid_spec.target_crs,
        pixel_area_ha=_pixel_area_ha(grid_spec),
        segmentation=config,
        event_count=len(event_records),
        total_event_area_ha=round(sum(event.area_ha for event in event_records), 8),
        area_threshold_event_count=sum(event.area_threshold_met for event in event_records),
        below_area_threshold_event_count=sum(
            not event.area_threshold_met for event in event_records
        ),
        outside_primary_domain_persistent_pixel_count=(
            segmentation.outside_primary_domain_pixel_count
        ),
        detail_figure_count=len(detail_paths),
        detail_figure_selection_policy=config.detail_selection_policy,
        onset_semantics=ONSET_SEMANTICS,
        attribution_generated=False,
        automatic_final_assessment_generated=False,
        events=tuple(event_records),
    )
    files: dict[str, bytes] = {
        evidence_table("disturbance_events.csv"): _csv_bytes(collection.events),
        evidence_json("disturbance_events.json"): _json_bytes(collection.model_dump(mode="json")),
        evidence_json("disturbance_events.geojson"): _geojson_bytes(
            events=collection.events,
            geometries=wgs84_geometries,
        ),
        evidence_figure("disturbance_events.png"): _render_event_overview(
            automated_forest=automated,
            events=collection.events,
            event_masks=event_masks,
            grid_spec=grid_spec,
            area_threshold_ha=config.area_threshold_ha,
            dpi=png_dpi,
        ),
    }
    event_by_id = {event.event_id: event for event in collection.events}
    for event_id, detail_path in sorted(detail_paths.items()):
        files[detail_path] = _render_event_detail(
            event=event_by_id[event_id],
            event_mask=event_masks[event_id],
            index_cube=cube,
            index_names=tuple(index_names),
            all_period_ids=tuple(all_period_ids),
            all_period_seasons=tuple(all_period_seasons),
            all_period_start_dates=tuple(all_period_start_dates),
            all_period_end_dates_exclusive=tuple(all_period_end_dates_exclusive),
            grid_spec=grid_spec,
            dpi=png_dpi,
        )
    return DisturbanceEventMaterialization(files=files, collection=collection)


def disturbance_event_output_paths(*, detail_event_ids: Sequence[str] = ()) -> dict[str, str]:
    """Expone las rutas públicas sin abrir raíces ni carpetas por evento."""
    paths = {
        "table": evidence_table("disturbance_events.csv"),
        "metadata": evidence_json("disturbance_events.json"),
        "geojson": evidence_json("disturbance_events.geojson"),
        "overview_figure": evidence_figure("disturbance_events.png"),
    }
    for event_id in detail_event_ids:
        if not event_id.startswith("PDE-"):
            raise ValueError("event_id_invalid")
        paths[f"detail_{event_id}"] = evidence_figure(f"disturbance_event_{event_id.lower()}.png")
    return paths


def _boolean_array(
    values: NDArray[Any],
    *,
    grid_spec: RasterGridSpec,
    name: str,
) -> NDArray[np.bool_]:
    array = np.asarray(values)
    if array.shape != (grid_spec.height, grid_spec.width):
        raise ValueError(f"event_{name}_shape_mismatch")
    if array.dtype != np.bool_:
        raise ValueError(f"event_{name}_dtype_invalid")
    return np.asarray(array, dtype=np.bool_)


def _numeric_array(
    values: NDArray[Any],
    *,
    grid_spec: RasterGridSpec,
    name: str,
) -> NDArray[np.float64]:
    array = np.asarray(values, dtype=np.float64)
    if array.shape != (grid_spec.height, grid_spec.width):
        raise ValueError(f"event_{name}_shape_mismatch")
    if np.isinf(array).any():
        raise ValueError(f"event_{name}_contains_infinity")
    return array


def _pixel_area_ha(grid_spec: RasterGridSpec) -> float:
    x_scale, x_shear, _, y_shear, y_scale, _ = grid_spec.transform
    area = abs(x_scale * y_scale - x_shear * y_shear) / 10_000.0
    if not math.isfinite(area) or area <= 0:
        raise ValueError("event_grid_pixel_area_invalid")
    return area


def _event_id(grid_sha256: str, pixels: tuple[tuple[int, int], ...]) -> str:
    identity = ";".join(f"{row},{column}" for row, column in pixels)
    digest = hashlib.sha256(f"{grid_sha256}|{identity}".encode()).hexdigest()[:12].upper()
    return f"PDE-{digest}"


def _event_geometry(
    event_mask: NDArray[np.bool_],
    *,
    grid_spec: RasterGridSpec,
) -> Mapping[str, Any]:
    transform = Affine(*grid_spec.transform)
    geometries = [
        shape(geometry)
        for geometry, value in shapes(
            event_mask.astype(np.uint8),
            mask=event_mask,
            transform=transform,
            connectivity=4,
        )
        if int(value) == 1
    ]
    if not geometries:
        raise RuntimeError("event_geometry_empty")
    merged = unary_union(geometries)
    _validate_polygonal_geometry(merged, error_code="event_projected_geometry_invalid")
    return cast(Mapping[str, Any], mapping(merged))


def _validate_polygonal_geometry(geometry: Any, *, error_code: str) -> None:
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError(error_code)
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"{error_code}_type")


def _validate_period_axis(
    period_ids: Sequence[str],
    starts: Sequence[date],
    ends_exclusive: Sequence[date],
    *,
    name: str,
) -> None:
    if not period_ids or not (len(period_ids) == len(starts) == len(ends_exclusive)):
        raise ValueError(f"event_{name}_period_axis_mismatch")
    if len(set(period_ids)) != len(period_ids):
        raise ValueError(f"event_{name}_period_ids_duplicated")
    if any(start >= end for start, end in zip(starts, ends_exclusive, strict=True)):
        raise ValueError(f"event_{name}_period_bounds_invalid")


def _earliest_period_index(values: NDArray[np.float64], *, period_count: int) -> int | None:
    finite = values[np.isfinite(values)]
    if not finite.size:
        return None
    if np.any(finite != np.floor(finite)):
        raise ValueError("event_first_anomalous_period_index_not_integer")
    indexes = finite.astype(np.int64)
    if np.any((indexes < 0) | (indexes >= period_count)):
        raise ValueError("event_first_anomalous_period_index_out_of_range")
    return int(np.min(indexes))


def _finite_stat(
    values: NDArray[np.float64],
    *,
    reducer: Literal["max", "mean"],
) -> float | None:
    finite = values[np.isfinite(values)]
    if not finite.size:
        return None
    result = np.max(finite) if reducer == "max" else np.mean(finite)
    return round(float(result), 8)


def _dominant_integer(values: NDArray[np.float64]) -> int:
    finite = values[np.isfinite(values)]
    if not finite.size or np.any((finite < 0) | (finite != np.floor(finite))):
        raise ValueError("event_reason_code_invalid")
    counts = Counter(int(value) for value in finite)
    maximum = max(counts.values())
    return min(code for code, count in counts.items() if count == maximum)


def _reason_name(code: int) -> str:
    try:
        return DetectorConvergenceReasonCode(code).name.lower()
    except ValueError:
        return f"unknown_reason_code_{code}"


def _maximum_nonnegative_integer(values: NDArray[np.float64]) -> int:
    finite = values[np.isfinite(values)]
    if not finite.size:
        return 0
    if np.any((finite < 0) | (finite != np.floor(finite))):
        raise ValueError("event_detector_count_invalid")
    return int(np.max(finite))


def _integer_bitwise_union(values: NDArray[np.float64]) -> int:
    finite = values[np.isfinite(values)]
    if not finite.size:
        return 0
    if np.any((finite < 0) | (finite != np.floor(finite))):
        raise ValueError("event_quality_flags_invalid")
    result = 0
    for value in finite:
        result |= int(value)
    return result


def _csv_bytes(events: Sequence[DisturbanceEvent]) -> bytes:
    fieldnames = tuple(DisturbanceEvent.model_fields)
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for event in events:
        row = event.model_dump(mode="json")
        row["area_threshold_met"] = str(row["area_threshold_met"]).lower()
        row["attribution_generated"] = str(row["attribution_generated"]).lower()
        row["automatic_final_assessment_generated"] = str(
            row["automatic_final_assessment_generated"]
        ).lower()
        row["bbox_wgs84"] = json.dumps(row["bbox_wgs84"], separators=(",", ":"))
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _geojson_bytes(
    *,
    events: Sequence[DisturbanceEvent],
    geometries: Mapping[str, Mapping[str, Any]],
) -> bytes:
    features = []
    for event in events:
        properties = event.model_dump(mode="json")
        properties.pop("bbox_wgs84")
        features.append(
            {
                "type": "Feature",
                "id": event.event_id,
                "geometry": geometries[event.event_id],
                "properties": properties,
            }
        )
    return _json_bytes(
        {
            "type": "FeatureCollection",
            "name": "persistent_disturbance_events",
            "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
            "features": features,
        }
    )


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _render_event_overview(
    *,
    automated_forest: NDArray[np.bool_],
    events: Sequence[DisturbanceEvent],
    event_masks: Mapping[str, NDArray[np.bool_]],
    grid_spec: RasterGridSpec,
    area_threshold_ha: float,
    dpi: int,
) -> bytes:
    display = np.full(automated_forest.shape, np.nan, dtype=np.float64)
    display[automated_forest] = 0
    for event in events:
        display[event_masks[event.event_id]] = 2 if event.area_threshold_met else 1
    figure = Figure(figsize=(9.5, 7.2), constrained_layout=True)
    axis = figure.subplots()
    cmap = ListedColormap(("#d9ead3", "#f6b26b", "#cc0000"))
    norm = BoundaryNorm((-0.5, 0.5, 1.5, 2.5), cmap.N)
    axis.imshow(display, cmap=cmap, norm=norm, interpolation="nearest")
    axis.set_title("Eventos de perturbación persistente dentro de bosque automático")
    axis.set_xlabel(f"Grilla {grid_spec.target_crs}; no implica atribución de uso posterior")
    axis.set_xticks([])
    axis.set_yticks([])
    axis.legend(
        handles=(
            Patch(color="#d9ead3", label="Bosque automático sin evento persistente"),
            Patch(
                color="#f6b26b",
                label=f"Evento < {area_threshold_ha:g} ha (conservado)",
            ),
            Patch(color="#cc0000", label=f"Evento >= {area_threshold_ha:g} ha"),
        ),
        loc="lower left",
        fontsize=8,
        framealpha=0.9,
    )
    axis.text(
        0.01,
        0.99,
        f"Eventos: {len(events)} | umbral informativo: {area_threshold_ha:g} ha",
        transform=axis.transAxes,
        va="top",
        ha="left",
        fontsize=8,
        bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none"},
    )
    return _figure_bytes(figure, dpi=dpi)


def _render_event_detail(
    *,
    event: DisturbanceEvent,
    event_mask: NDArray[np.bool_],
    index_cube: NDArray[np.float64],
    index_names: tuple[str, ...],
    all_period_ids: tuple[str, ...],
    all_period_seasons: tuple[str, ...],
    all_period_start_dates: tuple[date, ...],
    all_period_end_dates_exclusive: tuple[date, ...],
    grid_spec: RasterGridSpec,
    dpi: int,
) -> bytes:
    if event.estimated_onset_period_id is None:
        onset_axis_index = None
    else:
        onset_axis_index = all_period_ids.index(event.estimated_onset_period_id)
    pre_axis_index, _ = _same_season_comparison_indices(
        onset_axis_index=onset_axis_index,
        all_period_seasons=all_period_seasons,
        all_period_start_dates=all_period_start_dates,
        all_period_end_dates_exclusive=all_period_end_dates_exclusive,
    )
    ndvi_index = index_names.index("NDVI") if "NDVI" in index_names else 0
    row_indexes, column_indexes = np.nonzero(event_mask)
    row_min = max(0, int(np.min(row_indexes)) - 2)
    row_max = min(grid_spec.height, int(np.max(row_indexes)) + 3)
    column_min = max(0, int(np.min(column_indexes)) - 2)
    column_max = min(grid_spec.width, int(np.max(column_indexes)) + 3)
    crop = np.s_[row_min:row_max, column_min:column_max]
    figure = Figure(figsize=(12, 8), constrained_layout=True)
    axes = figure.subplots(2, 2)
    pre_axis, post_axis, delta_axis, series_axis = axes.flat
    if pre_axis_index is None:
        pre_axis.text(0.5, 0.5, "Sin período pre-corte comparable", ha="center", va="center")
        pre_axis.set_title("NDVI pre-corte, misma estación")
        pre_values = None
    else:
        pre_values = index_cube[pre_axis_index, ndvi_index]
        pre_axis.imshow(pre_values[crop], vmin=-1, vmax=1, cmap="RdYlGn")
        pre_axis.contour(event_mask[crop], levels=(0.5,), colors=("black",), linewidths=1)
        pre_axis.set_title(f"NDVI pre-corte: {all_period_ids[pre_axis_index]}")
    if onset_axis_index is None:
        post_axis.text(0.5, 0.5, "Ventana de inicio no disponible", ha="center", va="center")
        post_axis.set_title("NDVI en ventana inicial")
        post_values = None
    else:
        post_values = index_cube[onset_axis_index, ndvi_index]
        post_axis.imshow(post_values[crop], vmin=-1, vmax=1, cmap="RdYlGn")
        post_axis.contour(event_mask[crop], levels=(0.5,), colors=("black",), linewidths=1)
        post_axis.set_title(f"NDVI ventana de inicio: {all_period_ids[onset_axis_index]}")
    if pre_values is None or post_values is None:
        delta_axis.text(0.5, 0.5, "Comparación no disponible", ha="center", va="center")
    else:
        delta_axis.imshow((post_values - pre_values)[crop], vmin=-0.6, vmax=0.6, cmap="RdBu")
        delta_axis.contour(event_mask[crop], levels=(0.5,), colors=("black",), linewidths=1)
    delta_axis.set_title("Delta NDVI (inicio - pre-corte)")
    for axis in (pre_axis, post_axis, delta_axis):
        axis.set_xticks([])
        axis.set_yticks([])
    x = np.arange(len(all_period_ids))
    for index_position, index_name in enumerate(index_names):
        means = np.asarray(
            [
                float(np.nanmean(index_cube[period, index_position][event_mask]))
                if np.isfinite(index_cube[period, index_position][event_mask]).any()
                else np.nan
                for period in range(len(all_period_ids))
            ]
        )
        series_axis.plot(x, means, marker="o", markersize=2.5, linewidth=1, label=index_name)
    cutoff_position = max(
        (index for index, start in enumerate(all_period_start_dates) if start <= EUDR_CUTOFF_DATE),
        default=-1,
    )
    series_axis.axvline(cutoff_position + 0.5, color="black", linestyle="--", linewidth=1)
    if onset_axis_index is not None:
        series_axis.axvline(onset_axis_index, color="#cc0000", linewidth=1)
    tick_step = max(1, len(all_period_ids) // 8)
    tick_positions = np.arange(0, len(all_period_ids), tick_step)
    series_axis.set_xticks(tick_positions, [all_period_ids[index] for index in tick_positions])
    series_axis.tick_params(axis="x", rotation=45, labelsize=7)
    series_axis.set_title("Serie media del evento")
    series_axis.text(
        0.01,
        0.98,
        f"Línea roja = {EVENT_ONSET_FIGURE_LABEL}",
        transform=series_axis.transAxes,
        va="top",
        ha="left",
        fontsize=8,
        bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none"},
    )
    series_axis.set_ylabel("Índice espectral")
    series_axis.grid(alpha=0.2)
    series_axis.legend(ncol=2, fontsize=7)
    figure.suptitle(
        f"{event.event_id} · {event.area_ha:.2f} ha · evento de perturbación persistente\n"
        "Comparación estacional; no atribuye deforestación ni uso posterior",
        fontsize=11,
    )
    return _figure_bytes(figure, dpi=dpi)


def _figure_bytes(figure: Figure, *, dpi: int) -> bytes:
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi, metadata={"Software": "matplotlib"})
    plt.close(figure)
    return buffer.getvalue()


def _same_season_comparison_indices(
    *,
    onset_axis_index: int | None,
    all_period_seasons: Sequence[str],
    all_period_start_dates: Sequence[date],
    all_period_end_dates_exclusive: Sequence[date],
) -> tuple[int | None, int | None]:
    """Elige una referencia completamente pre-corte de la misma estación."""
    if onset_axis_index is None:
        return None, None
    if not (
        len(all_period_seasons)
        == len(all_period_start_dates)
        == len(all_period_end_dates_exclusive)
    ):
        raise ValueError("event_comparison_period_axis_mismatch")
    if not 0 <= onset_axis_index < len(all_period_seasons):
        raise ValueError("event_onset_axis_index_out_of_range")
    onset_season = all_period_seasons[onset_axis_index]
    analysis_start = EUDR_CUTOFF_DATE + timedelta(days=1)
    candidates = [
        index
        for index, (season, end_exclusive) in enumerate(
            zip(all_period_seasons, all_period_end_dates_exclusive, strict=True)
        )
        if season == onset_season and end_exclusive <= analysis_start
    ]
    pre_index = (
        max(candidates, key=lambda index: all_period_start_dates[index]) if candidates else None
    )
    return pre_index, onset_axis_index
