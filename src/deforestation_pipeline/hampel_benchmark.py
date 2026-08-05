"""Benchmark Hampel estacional, protegido y reversible para evidencia VISEC."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import NAMESPACE_URL, uuid5

import matplotlib
import numpy as np
import rasterio
import yaml
from matplotlib.figure import Figure
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deforestation_pipeline.artifact_layout import (
    configuration_json,
    evidence_figure,
    evidence_json,
    evidence_table,
    run_json,
)
from deforestation_pipeline.change_detection import DisturbanceStateCode
from deforestation_pipeline.disturbance_events import segment_persistent_disturbance_events
from deforestation_pipeline.forest_screening import (
    ForestScreeningConfig,
    ForestScreeningDomain,
    build_forest_screening_domain,
)
from deforestation_pipeline.schemas import RasterGridSpec

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

VISEC_HAMPEL_INDICES = ("NDVI", "EVI2", "NMDI", "LSWI", "NIRv", "kNDVI")
HAMPEL_BENCHMARK_SCHEMA_VERSION = "1.0.0"
DERIVED_BUNDLE_SCHEMA_VERSION = "3.2.0"
_SAFE_ESTABLISHMENT = re.compile(r"[^a-z0-9-]+")


class ProtectedHampelConfig(BaseModel):
    """Contrato congelado del benchmark; jamás habilita el filtro productivo."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"]
    temporal_granularity: Literal["seasonal_composite_medians"]
    season_stratification: Literal["same_meteorological_season_only"]
    visec_reference_order: Literal["scene_level_before_aggregation"]
    methodological_equivalence_to_visec: Literal[False]
    indices: tuple[Literal["NDVI", "EVI2", "NMDI", "LSWI", "NIRv", "kNDVI"], ...]
    window_size: Annotated[int, Field(ge=3, strict=True)]
    sigma_threshold: Annotated[float, Field(gt=0, strict=True)]
    mad_scale_constant: Annotated[float, Field(gt=0, strict=True)]
    minimum_window_support: Annotated[int, Field(ge=3, strict=True)]
    minimum_absolute_deviation: Annotated[float, Field(gt=0, strict=True)]
    zero_mad_policy: Literal["candidate_if_nonzero_deviation"]
    return_window_size: Annotated[int, Field(ge=1, strict=True)]
    minimum_return_support_each_side: Annotated[int, Field(ge=1, strict=True)]
    maximum_replacement_run_length: Literal[1]
    edge_policy: Literal["flag_without_replacement"]
    missing_data_policy: Literal["preserve_nan_without_interpolation"]
    replacement_value: Literal["local_median"]
    stable_control_count: Annotated[int, Field(ge=1, le=100, strict=True)]
    stable_control_radius_pixels: Annotated[int, Field(ge=0, le=5, strict=True)]
    stable_control_minimum_center_distance_pixels: Annotated[
        int,
        Field(ge=1, le=100, strict=True),
    ]
    stable_control_selection: Literal["sha256_ranked_strict_stable_neighborhood_v1"]
    activation_gate: Literal["independent_validation_required"]
    activation_allowed: Literal[False]
    activated: Literal[False]

    @field_validator("window_size")
    @classmethod
    def window_is_odd(cls, value: int) -> int:
        if value % 2 == 0:
            raise ValueError("hampel_window_size_must_be_odd")
        return value

    @model_validator(mode="after")
    def contract_is_consistent(self) -> ProtectedHampelConfig:
        if self.indices != VISEC_HAMPEL_INDICES:
            raise ValueError("hampel_indices_must_match_visec_six")
        if self.minimum_window_support > self.window_size:
            raise ValueError("hampel_minimum_support_exceeds_window")
        if self.minimum_return_support_each_side > self.return_window_size:
            raise ValueError("hampel_return_support_exceeds_window")
        if self.mad_scale_constant != 1.4826:
            raise ValueError("hampel_mad_scale_constant_must_be_1_4826")
        minimum_distance = 2 * self.stable_control_radius_pixels + 1
        if self.stable_control_minimum_center_distance_pixels < minimum_distance:
            raise ValueError("hampel_control_neighborhoods_must_not_overlap")
        return self


@dataclass(frozen=True, slots=True)
class HampelPoint:
    """Diagnóstico reversible de una observación de la serie."""

    raw_value: float | None
    filtered_value: float | None
    outlier_candidate: bool
    replacement_applied: bool
    local_median: float | None
    local_mad: float | None
    scaled_mad: float | None
    threshold: float | None
    window_support_count: int
    return_support_before: int
    return_support_after: int
    reason_code: str


@dataclass(frozen=True, slots=True)
class StableControl:
    """Unidad de control espacial estable sin exponer coordenadas geográficas."""

    unit_id: str
    row: int
    column: int
    pixels: tuple[tuple[int, int], ...]

    @property
    def pixel_count(self) -> int:
        return len(self.pixels)


def load_hampel_benchmark_config(path: Path) -> ProtectedHampelConfig:
    """Carga una configuración autónoma para no alterar el pipeline productivo."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("hampel_config_root_must_be_mapping")
    return ProtectedHampelConfig.model_validate(document)


def apply_protected_hampel(
    values: NDArray[np.float64],
    *,
    config: ProtectedHampelConfig,
) -> tuple[HampelPoint, ...]:
    """Marca candidatos y reemplaza sólo picos aislados con retorno bilateral."""
    raw = np.asarray(values, dtype=np.float64)
    if raw.ndim != 1:
        raise ValueError("hampel_series_must_be_one_dimensional")
    if raw.size < config.window_size:
        return tuple(
            _missing_point("missing_preserved")
            if not math.isfinite(float(value))
            else HampelPoint(
                raw_value=float(value),
                filtered_value=float(value),
                outlier_candidate=False,
                replacement_applied=False,
                local_median=None,
                local_mad=None,
                scaled_mad=None,
                threshold=None,
                window_support_count=int(np.count_nonzero(np.isfinite(raw))),
                return_support_before=0,
                return_support_after=0,
                reason_code="series_too_short",
            )
            for value in raw
        )

    local_statistics = tuple(_local_statistics(raw, index, config) for index in range(raw.size))
    candidates = tuple(
        _is_candidate(float(raw[index]), statistics, config)
        for index, statistics in enumerate(local_statistics)
    )
    return tuple(
        _protect_point(
            raw=raw,
            index=index,
            statistics=local_statistics[index],
            candidates=candidates,
            config=config,
        )
        for index in range(raw.size)
    )


def select_stable_controls(
    stable_mask: NDArray[np.bool_],
    *,
    grid_sha256: str,
    requested_count: int,
    radius_pixels: int,
    minimum_center_distance_pixels: int,
) -> tuple[StableControl, ...]:
    """Selecciona vecindades 100 % estables por ranking SHA-256 y separación Chebyshev."""
    mask = np.asarray(stable_mask)
    if mask.ndim != 2 or mask.dtype != np.bool_:
        raise ValueError("hampel_stable_mask_must_be_2d_boolean")
    if len(grid_sha256) != 64:
        raise ValueError("hampel_grid_sha256_invalid")
    if requested_count < 1 or radius_pixels < 0 or minimum_center_distance_pixels < 1:
        raise ValueError("hampel_control_selection_parameters_invalid")
    if minimum_center_distance_pixels < 2 * radius_pixels + 1:
        raise ValueError("hampel_control_neighborhoods_must_not_overlap")

    candidates: list[tuple[str, int, int, tuple[tuple[int, int], ...]]] = []
    for row in range(radius_pixels, mask.shape[0] - radius_pixels):
        for column in range(radius_pixels, mask.shape[1] - radius_pixels):
            pixels = tuple(
                (candidate_row, candidate_column)
                for candidate_row in range(row - radius_pixels, row + radius_pixels + 1)
                for candidate_column in range(
                    column - radius_pixels,
                    column + radius_pixels + 1,
                )
            )
            if not all(bool(mask[pixel]) for pixel in pixels):
                continue
            digest = hashlib.sha256(f"{grid_sha256}|{row}|{column}".encode()).hexdigest()
            candidates.append((digest, row, column, pixels))

    selected: list[StableControl] = []
    for digest, row, column, pixels in sorted(candidates):
        if any(
            max(abs(row - item.row), abs(column - item.column)) < minimum_center_distance_pixels
            for item in selected
        ):
            continue
        selected.append(
            StableControl(
                unit_id=f"CTRL-{digest[:12].upper()}",
                row=row,
                column=column,
                pixels=pixels,
            )
        )
        if len(selected) == requested_count:
            break
    return tuple(selected)


def materialize_seasonal_hampel_benchmark(
    *,
    source_bundle: Path,
    output_root: Path,
    input_geojson: Path,
    establishment_id: str,
    config: ProtectedHampelConfig,
    created_at: datetime | None = None,
) -> Path:
    """Deriva un bundle autocontenido sin cambiar la detección ni consultar GEE."""
    generated_at = created_at or datetime.now(UTC)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("hampel_created_at_requires_timezone")
    if not establishment_id.strip():
        raise ValueError("hampel_establishment_id_empty")
    source_bundle = source_bundle.resolve()
    source_manifest_path = source_bundle / run_json("manifest.json")
    source_manifest = _read_json(source_manifest_path)
    source_artifact_count, source_artifact_bytes = _validate_source_bundle(
        source_bundle=source_bundle,
        manifest=source_manifest,
    )
    input_verification = _verify_requested_input(
        source_bundle=source_bundle,
        input_geojson=input_geojson.resolve(),
    )
    config_hash = _canonical_hash(config.model_dump(mode="json"))
    source_analysis_id = _required_text(source_manifest, "analysis_id")
    source_manifest_sha256 = _sha256(source_manifest_path.read_bytes())
    analysis_id = str(
        uuid5(
            NAMESPACE_URL,
            "|".join(
                (
                    "deforestation-pipeline:seasonal-hampel-benchmark",
                    source_analysis_id,
                    config_hash,
                    establishment_id,
                )
            ),
        )
    )
    run_id = _derived_run_id(establishment_id, generated_at, analysis_id)
    run_directory = output_root.resolve() / run_id
    if run_directory.exists():
        raise FileExistsError(f"hampel_run_directory_exists:{run_directory}")

    analysis = _analyze_source_bundle(
        source_bundle=source_bundle,
        config=config,
        generated_at=generated_at,
        analysis_id=analysis_id,
        run_id=run_id,
        source_manifest=source_manifest,
        source_manifest_sha256=source_manifest_sha256,
        source_artifact_count=source_artifact_count,
        source_artifact_bytes=source_artifact_bytes,
        input_verification=input_verification,
    )
    files = {
        _required_text(artifact, "path"): (
            source_bundle / _required_text(artifact, "path")
        ).read_bytes()
        for artifact in _artifact_records(source_manifest)
    }
    files.update(analysis.files)
    files[configuration_json("hampel_benchmark.json")] = _json_bytes(
        {
            "schema_version": HAMPEL_BENCHMARK_SCHEMA_VERSION,
            "parameters_hash": config_hash,
            "parameters": config.model_dump(mode="json"),
        }
    )
    source_summary = _read_json(source_bundle / run_json("summary.json"))
    source_scientific_hash = _required_text(source_manifest, "scientific_parameters_hash")
    derived_scientific_hash = _canonical_hash(
        {
            "source_scientific_parameters_hash": source_scientific_hash,
            "hampel_parameters_hash": config_hash,
        }
    )
    derived_execution_hash = _canonical_hash(
        {
            "source_execution_config_hash": _required_text(
                source_manifest,
                "execution_config_hash",
            ),
            "hampel_parameters_hash": config_hash,
            "derived_bundle_schema_version": DERIVED_BUNDLE_SCHEMA_VERSION,
        }
    )
    updated_summary = {
        **source_summary,
        "schema_version": DERIVED_BUNDLE_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "run_id": run_id,
        "establishment_id": establishment_id,
        "created_at": generated_at.isoformat(),
        "stage": "seasonal_hampel_benchmark",
        "scientific_parameters_hash": derived_scientific_hash,
        "execution_config_hash": derived_execution_hash,
        "source_scientific_parameters_hash": source_scientific_hash,
        "hampel_benchmark": analysis.summary,
        "final_assessment_generated": False,
    }
    limitations = list(cast(Sequence[object], updated_summary.get("limitations", [])))
    limitations.extend(
        (
            "Hampel se evaluó sobre medianas estacionales, no sobre escenas antes de agregar.",
            "El benchmark no fue realimentado a detectores ni cambió eventos o estados.",
        )
    )
    updated_summary["limitations"] = list(dict.fromkeys(str(item) for item in limitations))
    files[run_json("summary.json")] = _json_bytes(updated_summary)

    from deforestation_pipeline.local_runner import _publish_bundle

    datasets = tuple(
        cast(Mapping[str, object], item)
        for item in cast(Sequence[object], source_manifest.get("datasets", []))
        if isinstance(item, dict)
    )
    manifest_metadata: dict[str, object] = {
        key: source_manifest[key]
        for key in (
            "analysis_end_date",
            "catalog_sha256",
            "input_sha256",
            "pixel_data_accessed",
            "requested_analysis_end_date",
        )
        if key in source_manifest
    }
    manifest_metadata.update(
        {
            "schema_version": DERIVED_BUNDLE_SCHEMA_VERSION,
            "analysis_id": analysis_id,
            "run_id": run_id,
            "establishment_id": establishment_id,
            "created_at": generated_at.isoformat(),
            "scientific_parameters_hash": derived_scientific_hash,
            "execution_config_hash": derived_execution_hash,
            "source_scientific_parameters_hash": source_scientific_hash,
            "derivation": analysis.summary["source_bundle"],
            "hampel_parameters_hash": config_hash,
            "hampel_activated": False,
        }
    )
    _publish_bundle(
        run_directory=run_directory,
        files=files,
        manifest_metadata=manifest_metadata,
        remote_data_accessed=bool(source_manifest.get("remote_data_accessed")),
        datasets=datasets,
    )
    return run_directory


@dataclass(frozen=True, slots=True)
class _BenchmarkMaterialization:
    files: Mapping[str, bytes]
    summary: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _AnalysisUnit:
    unit_id: str
    cohort: Literal["persistent_event", "stable_control"]
    pixels: tuple[tuple[int, int], ...]


def _analyze_source_bundle(
    *,
    source_bundle: Path,
    config: ProtectedHampelConfig,
    generated_at: datetime,
    analysis_id: str,
    run_id: str,
    source_manifest: Mapping[str, object],
    source_manifest_sha256: str,
    source_artifact_count: int,
    source_artifact_bytes: int,
    input_verification: Mapping[str, object],
) -> _BenchmarkMaterialization:
    cube_spec = _read_json(source_bundle / "json/temporal/seasonal/cube_spec.json")
    cube_index = _read_json(source_bundle / "json/temporal/seasonal/cube_index.json")
    grid = RasterGridSpec.model_validate(cube_spec["grid"])
    resolved = _read_json(source_bundle / configuration_json("resolved.json"))
    screening_config = ForestScreeningConfig.model_validate(resolved["forest_screening"])
    screening = _screening_from_bundle(
        source_bundle=source_bundle,
        grid=grid,
        config=screening_config,
    )
    disturbance_state = _read_named_raster(
        source_bundle / "tiffs/evidence/disturbance_summary.tif",
        band_names=("disturbance_state_code",),
        grid=grid,
    )[0]
    persistent = np.isfinite(disturbance_state) & (
        disturbance_state == int(DisturbanceStateCode.PERSISTENT_CANDIDATE)
    )
    event_config_value = resolved["disturbance_events"]
    if not isinstance(event_config_value, dict):
        raise ValueError("hampel_source_event_config_invalid")
    event_config = cast(Mapping[str, object], event_config_value)
    if _required_int(event_config, "connectivity") != 8:
        raise ValueError("hampel_source_event_connectivity_not_supported")
    segmentation = segment_persistent_disturbance_events(
        persistent_mask=np.asarray(persistent, dtype=np.bool_),
        automated_forest_mask=np.asarray(screening.automated_forest, dtype=np.bool_),
        grid_spec=grid,
        connectivity=8,
        area_threshold_ha=_required_float(event_config, "area_threshold_ha"),
    )
    published_events = _read_json(source_bundle / evidence_json("disturbance_events.json"))
    published_ids = tuple(
        _required_text(cast(Mapping[str, object], item), "event_id")
        for item in cast(Sequence[object], published_events["events"])
    )
    segmented_ids = tuple(event.event_id for event in segmentation.events)
    if segmented_ids != published_ids:
        raise ValueError("hampel_source_event_ids_do_not_match_rasters")
    event_units = tuple(
        _AnalysisUnit(event.event_id, "persistent_event", event.pixels)
        for event in segmentation.events
    )
    stable_mask = np.asarray(
        screening.automated_forest
        & np.isfinite(disturbance_state)
        & (disturbance_state == int(DisturbanceStateCode.STABLE_OBSERVED)),
        dtype=np.bool_,
    )
    controls = select_stable_controls(
        stable_mask,
        grid_sha256=grid.grid_sha256,
        requested_count=config.stable_control_count,
        radius_pixels=config.stable_control_radius_pixels,
        minimum_center_distance_pixels=(config.stable_control_minimum_center_distance_pixels),
    )
    if len(controls) < config.stable_control_count:
        raise ValueError("hampel_insufficient_strict_stable_controls")
    control_units = tuple(
        _AnalysisUnit(control.unit_id, "stable_control", control.pixels) for control in controls
    )
    units = (*event_units, *control_units)
    period_products = tuple(
        cast(Mapping[str, object], item)
        for item in cast(Sequence[object], cube_index["period_products"])
    )
    raw_rows = _extract_unit_series(
        source_bundle=source_bundle,
        grid=grid,
        period_products=period_products,
        units=units,
        indices=config.indices,
    )
    rows = _apply_season_stratified_hampel(raw_rows, config=config)
    metrics = _benchmark_metrics(rows, indices=config.indices)
    observation_path = evidence_table("hampel_benchmark_observations.csv")
    metric_path = evidence_table("hampel_benchmark_metrics.csv")
    metadata_path = evidence_json("seasonal_hampel_benchmark.json")
    figure_path = evidence_figure("seasonal_hampel_benchmark.png")
    transformed_source_artifacts = (run_json("summary.json"),)
    source_artifact_paths = {
        _required_text(artifact, "path") for artifact in _artifact_records(source_manifest)
    }
    if not set(transformed_source_artifacts) <= source_artifact_paths:
        raise ValueError("hampel_transformed_source_artifact_not_manifested")
    source_bundle_metadata: dict[str, object] = {
        "source_run_id": _required_text(source_manifest, "run_id"),
        "source_analysis_id": _required_text(source_manifest, "analysis_id"),
        "source_manifest_sha256": source_manifest_sha256,
        "validated_source_artifact_count": source_artifact_count,
        "validated_source_artifact_bytes": source_artifact_bytes,
        "source_integrity_validated": True,
        "reused_unchanged_artifact_count": (
            source_artifact_count - len(transformed_source_artifacts)
        ),
        "transformed_artifact_count": len(transformed_source_artifacts),
        "transformed_source_artifacts": list(transformed_source_artifacts),
        "all_final_artifacts_rehashed": True,
        "remote_query_performed_for_benchmark": False,
        "input_verification": dict(input_verification),
    }
    summary: dict[str, object] = {
        "schema_version": HAMPEL_BENCHMARK_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "run_id": run_id,
        "generated_at": generated_at.isoformat(),
        "benchmark_type": "seasonal_hampel_benchmark",
        "temporal_granularity": config.temporal_granularity,
        "season_stratification": config.season_stratification,
        "methodological_equivalence_to_visec": False,
        "visec_reference_order": config.visec_reference_order,
        "source_composite_method": cube_spec.get("composite_method"),
        "indices": list(config.indices),
        "source_bundle": source_bundle_metadata,
        "cohorts": {
            "persistent_event": {
                "selection": "all_published_persistent_events_in_automated_forest",
                "unit_count": len(event_units),
                "pixel_count": sum(len(unit.pixels) for unit in event_units),
            },
            "stable_control": {
                "selection": config.stable_control_selection,
                "eligibility": "automated_forest_and_stable_observed_strict_neighborhood",
                "requested_unit_count": config.stable_control_count,
                "unit_count": len(control_units),
                "radius_pixels": config.stable_control_radius_pixels,
                "minimum_center_distance_pixels": (
                    config.stable_control_minimum_center_distance_pixels
                ),
                "controls": [
                    {
                        "unit_id": control.unit_id,
                        "row": control.row,
                        "column": control.column,
                        "pixel_count": control.pixel_count,
                    }
                    for control in controls
                ],
            },
        },
        "metrics": metrics,
        "decision_impact": {
            "production_detector_rerun": False,
            "production_event_membership_preserved": True,
            "production_event_count_before": len(event_units),
            "production_event_count_after": None,
            "onset_impact_assessed": False,
            "magnitude_impact_assessed": False,
            "reason": "benchmark_not_fed_back_into_detectors",
        },
        "activation": {
            "activated": False,
            "activation_allowed": False,
            "gate": config.activation_gate,
            "gate_status": "not_met",
            "blocking_reasons": [
                "scene_level_before_aggregation_not_implemented",
                "independent_validation_not_executed",
                "detector_onset_and_magnitude_impact_not_assessed",
            ],
        },
        "artifacts": {
            "observations": observation_path,
            "metrics": metric_path,
            "figure": figure_path,
            "configuration": configuration_json("hampel_benchmark.json"),
        },
        "limitations": [
            "El orden VISEC escena-Hampel-agregación no puede reconstruirse desde composites.",
            "Las medianas por evento y control reducen variabilidad espacial intragrupo.",
            "Este benchmark no atribuye causa, uso posterior ni deforestación.",
        ],
    }
    files = {
        observation_path: _rows_csv_bytes(rows),
        metric_path: _rows_csv_bytes(metrics),
        metadata_path: _json_bytes(summary),
        figure_path: _render_metrics(metrics, indices=config.indices),
    }
    return _BenchmarkMaterialization(files=files, summary=summary)


@dataclass(frozen=True, slots=True)
class _LocalStatistics:
    median: float | None
    mad: float | None
    scaled_mad: float | None
    threshold: float | None
    support_count: int


def _local_statistics(
    raw: NDArray[np.float64],
    index: int,
    config: ProtectedHampelConfig,
) -> _LocalStatistics:
    half_window = config.window_size // 2
    start = max(0, index - half_window)
    stop = min(raw.size, index + half_window + 1)
    window = raw[start:stop]
    finite = window[np.isfinite(window)]
    support_count = int(finite.size)
    if support_count < config.minimum_window_support:
        return _LocalStatistics(None, None, None, None, support_count)
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median)))
    scaled_mad = config.mad_scale_constant * mad
    return _LocalStatistics(
        median=median,
        mad=mad,
        scaled_mad=scaled_mad,
        threshold=config.sigma_threshold * scaled_mad,
        support_count=support_count,
    )


def _is_candidate(
    value: float,
    statistics: _LocalStatistics,
    config: ProtectedHampelConfig,
) -> bool:
    if not math.isfinite(value) or statistics.median is None or statistics.mad is None:
        return False
    deviation = abs(value - statistics.median)
    if statistics.mad == 0:
        return deviation >= config.minimum_absolute_deviation
    assert statistics.threshold is not None
    return deviation > statistics.threshold


def _protect_point(
    *,
    raw: NDArray[np.float64],
    index: int,
    statistics: _LocalStatistics,
    candidates: tuple[bool, ...],
    config: ProtectedHampelConfig,
) -> HampelPoint:
    value = float(raw[index])
    if not math.isfinite(value):
        return _missing_point("missing_preserved")
    if statistics.median is None:
        return _retained_point(
            value=value,
            statistics=statistics,
            candidate=False,
            reason="insufficient_window_support",
        )
    if not candidates[index]:
        return _retained_point(
            value=value,
            statistics=statistics,
            candidate=False,
            reason="within_hampel_envelope",
        )

    candidate_start = index
    while candidate_start > 0 and candidates[candidate_start - 1]:
        candidate_start -= 1
    candidate_stop = index + 1
    while candidate_stop < len(candidates) and candidates[candidate_stop]:
        candidate_stop += 1
    if candidate_stop - candidate_start > config.maximum_replacement_run_length:
        return _retained_point(
            value=value,
            statistics=statistics,
            candidate=True,
            reason="candidate_run_protected",
        )

    before = raw[max(0, index - config.return_window_size) : index]
    after = raw[index + 1 : index + 1 + config.return_window_size]
    before_finite = before[np.isfinite(before)]
    after_finite = after[np.isfinite(after)]
    if (
        before_finite.size < config.minimum_return_support_each_side
        or after_finite.size < config.minimum_return_support_each_side
    ):
        return _retained_point(
            value=value,
            statistics=statistics,
            candidate=True,
            reason="edge_candidate_retained",
            return_support_before=int(before_finite.size),
            return_support_after=int(after_finite.size),
        )

    assert statistics.median is not None
    tolerance = max(statistics.threshold or 0.0, config.minimum_absolute_deviation)
    before_return = int(np.count_nonzero(np.abs(before_finite - statistics.median) <= tolerance))
    after_return = int(np.count_nonzero(np.abs(after_finite - statistics.median) <= tolerance))
    if (
        before_return < config.minimum_return_support_each_side
        or after_return < config.minimum_return_support_each_side
    ):
        return _retained_point(
            value=value,
            statistics=statistics,
            candidate=True,
            reason="candidate_retained_no_two_sided_return",
            return_support_before=before_return,
            return_support_after=after_return,
        )
    return HampelPoint(
        raw_value=value,
        filtered_value=statistics.median,
        outlier_candidate=True,
        replacement_applied=True,
        local_median=statistics.median,
        local_mad=statistics.mad,
        scaled_mad=statistics.scaled_mad,
        threshold=statistics.threshold,
        window_support_count=statistics.support_count,
        return_support_before=before_return,
        return_support_after=after_return,
        reason_code="isolated_spike_replaced",
    )


def _retained_point(
    *,
    value: float,
    statistics: _LocalStatistics,
    candidate: bool,
    reason: str,
    return_support_before: int = 0,
    return_support_after: int = 0,
) -> HampelPoint:
    return HampelPoint(
        raw_value=value,
        filtered_value=value,
        outlier_candidate=candidate,
        replacement_applied=False,
        local_median=statistics.median,
        local_mad=statistics.mad,
        scaled_mad=statistics.scaled_mad,
        threshold=statistics.threshold,
        window_support_count=statistics.support_count,
        return_support_before=return_support_before,
        return_support_after=return_support_after,
        reason_code=reason,
    )


def _missing_point(reason: str) -> HampelPoint:
    return HampelPoint(
        raw_value=None,
        filtered_value=None,
        outlier_candidate=False,
        replacement_applied=False,
        local_median=None,
        local_mad=None,
        scaled_mad=None,
        threshold=None,
        window_support_count=0,
        return_support_before=0,
        return_support_after=0,
        reason_code=reason,
    )


def _screening_from_bundle(
    *,
    source_bundle: Path,
    grid: RasterGridSpec,
    config: ForestScreeningConfig,
) -> ForestScreeningDomain:
    evidence = source_bundle / "tiffs/evidence"
    return build_forest_screening_domain(
        source_count=_read_named_raster(
            evidence / "forest_source_count_2020.tif",
            band_names=("forest_core_source_count_2020",),
            grid=grid,
        )[0],
        consensus_forest=_read_named_raster(
            evidence / "forest_consensus_2020.tif",
            band_names=("forest_consensus_2020",),
            grid=grid,
        )[0],
        disagreement=_read_named_raster(
            evidence / "forest_disagreement_2020.tif",
            band_names=("forest_disagreement_2020",),
            grid=grid,
        )[0],
        evidence_fraction=_read_named_raster(
            evidence / "forest_evidence_fraction_2020.tif",
            band_names=("forest_evidence_fraction_2020",),
            grid=grid,
        )[0],
        rf_vote_fraction=_read_named_raster(
            evidence / "rf_forest_vote_fraction_2020.tif",
            band_names=("rf_forest_vote_fraction_2020",),
            grid=grid,
        )[0],
        rf_input_complete=_read_named_raster(
            evidence / "rf_forest_input_complete_2020.tif",
            band_names=("rf_forest_input_complete_2020",),
            grid=grid,
        )[0],
        grid_spec=grid,
        config=config,
    )


def _extract_unit_series(
    *,
    source_bundle: Path,
    grid: RasterGridSpec,
    period_products: Sequence[Mapping[str, object]],
    units: Sequence[_AnalysisUnit],
    indices: Sequence[str],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen_periods: set[str] = set()
    for product in period_products:
        period_id = _required_text(product, "period_id")
        if period_id in seen_periods:
            raise ValueError("hampel_period_axis_contains_duplicates")
        seen_periods.add(period_id)
        season = _required_text(product, "season")
        year = _required_int(product, "season_year")
        raster_path = source_bundle / _required_text(product, "indices_raster_path")
        values = _read_named_raster(raster_path, band_names=tuple(indices), grid=grid)
        for unit in units:
            pixel_rows = np.fromiter((pixel[0] for pixel in unit.pixels), dtype=np.intp)
            pixel_columns = np.fromiter((pixel[1] for pixel in unit.pixels), dtype=np.intp)
            for band_index, index_name in enumerate(indices):
                samples = values[band_index, pixel_rows, pixel_columns]
                finite = samples[np.isfinite(samples)]
                raw_value = float(np.median(finite)) if finite.size else None
                rows.append(
                    {
                        "unit_id": unit.unit_id,
                        "cohort": unit.cohort,
                        "period_id": period_id,
                        "season_year": year,
                        "season": season,
                        "index": index_name,
                        "raw_value": raw_value,
                        "support_pixel_count": int(finite.size),
                    }
                )
    return rows


def _apply_season_stratified_hampel(
    raw_rows: Sequence[Mapping[str, object]],
    *,
    config: ProtectedHampelConfig,
) -> list[dict[str, object]]:
    grouped: defaultdict[tuple[str, str, str], list[Mapping[str, object]]] = defaultdict(list)
    for row in raw_rows:
        key = (
            str(row["unit_id"]),
            str(row["index"]),
            str(row["season"]),
        )
        grouped[key].append(row)
    result: list[dict[str, object]] = []
    for key in sorted(grouped):
        series_rows = sorted(
            grouped[key],
            key=lambda row: (_object_int(row["season_year"]), str(row["period_id"])),
        )
        raw = np.asarray(
            [
                np.nan if row["raw_value"] is None else _object_float(row["raw_value"])
                for row in series_rows
            ],
            dtype=np.float64,
        )
        points = apply_protected_hampel(raw, config=config)
        for sequence_position, (row, point) in enumerate(zip(series_rows, points, strict=True)):
            result.append(
                {
                    **row,
                    "filtered_value": point.filtered_value,
                    "outlier_candidate": point.outlier_candidate,
                    "replacement_applied": point.replacement_applied,
                    "local_median": point.local_median,
                    "local_mad": point.local_mad,
                    "scaled_mad": point.scaled_mad,
                    "threshold": point.threshold,
                    "window_size": config.window_size,
                    "window_support_count": point.window_support_count,
                    "return_support_before": point.return_support_before,
                    "return_support_after": point.return_support_after,
                    "sequence_position": sequence_position,
                    "sequence_length": len(series_rows),
                    "reason_code": point.reason_code,
                }
            )
    return sorted(
        result,
        key=lambda row: (
            str(row["cohort"]),
            str(row["unit_id"]),
            str(row["index"]),
            _object_int(row["season_year"]),
            str(row["season"]),
        ),
    )


def _benchmark_metrics(
    rows: Sequence[Mapping[str, object]],
    *,
    indices: Sequence[str],
) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    for cohort in ("persistent_event", "stable_control"):
        for index_name in indices:
            selected = [
                row for row in rows if row["cohort"] == cohort and row["index"] == index_name
            ]
            finite = [row for row in selected if row["raw_value"] is not None]
            candidates = [row for row in finite if bool(row["outlier_candidate"])]
            replacements = [row for row in candidates if bool(row["replacement_applied"])]
            adjustments = [
                abs(_object_float(row["filtered_value"]) - _object_float(row["raw_value"]))
                for row in finite
                if row["filtered_value"] is not None
            ]
            metrics.append(
                {
                    "cohort": cohort,
                    "index": index_name,
                    "unit_count": len({str(row["unit_id"]) for row in selected}),
                    "observation_count": len(selected),
                    "finite_observation_count": len(finite),
                    "missing_observation_count": len(selected) - len(finite),
                    "outlier_candidate_count": len(candidates),
                    "replacement_count": len(replacements),
                    "retained_candidate_count": len(candidates) - len(replacements),
                    "candidate_rate": _safe_ratio(len(candidates), len(finite)),
                    "replacement_rate": _safe_ratio(len(replacements), len(finite)),
                    "replacement_rate_among_candidates": _safe_ratio(
                        len(replacements),
                        len(candidates),
                    ),
                    "mean_absolute_adjustment_all_finite": (
                        round(float(np.mean(adjustments)), 10) if adjustments else 0.0
                    ),
                    "maximum_absolute_adjustment": (
                        round(float(np.max(adjustments)), 10) if adjustments else 0.0
                    ),
                    "two_sided_return_not_confirmed_count": sum(
                        row["reason_code"] == "candidate_retained_no_two_sided_return"
                        for row in candidates
                    ),
                    "candidate_run_protected_count": sum(
                        row["reason_code"] == "candidate_run_protected" for row in candidates
                    ),
                    "edge_candidate_retained_count": sum(
                        row["reason_code"] == "edge_candidate_retained" for row in candidates
                    ),
                }
            )
    return metrics


def _render_metrics(
    metrics: Sequence[Mapping[str, object]],
    *,
    indices: Sequence[str],
) -> bytes:
    figure = Figure(figsize=(12.0, 5.2), constrained_layout=True)
    axes = figure.subplots(1, 2)
    x = np.arange(len(indices), dtype=np.float64)
    width = 0.36
    colors = {"persistent_event": "#b23a48", "stable_control": "#277da1"}
    labels = {"persistent_event": "Eventos persistentes", "stable_control": "Controles"}
    for axis, metric_name, title in zip(
        axes,
        ("candidate_rate", "replacement_rate"),
        ("Candidatos Hampel", "Reemplazos protegidos"),
        strict=True,
    ):
        for offset, cohort in ((-width / 2, "persistent_event"), (width / 2, "stable_control")):
            values = [
                _object_float(
                    next(
                        row[metric_name]
                        for row in metrics
                        if row["cohort"] == cohort and row["index"] == index_name
                    )
                )
                for index_name in indices
            ]
            axis.bar(
                x + offset,
                values,
                width,
                label=labels[cohort],
                color=colors[cohort],
            )
        axis.set_title(title)
        axis.set_xticks(x, indices, rotation=35, ha="right")
        axis.set_ylabel("Fracción de observaciones finitas")
        axis.set_ylim(bottom=0)
        axis.grid(axis="y", alpha=0.25)
    axes[0].legend(frameon=False)
    figure.suptitle(
        "Benchmark Hampel estacional — sólo diagnóstico, no activado",
        fontweight="bold",
    )
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=150)
    plt.close(figure)
    return buffer.getvalue()


def _read_named_raster(
    path: Path,
    *,
    band_names: tuple[str, ...],
    grid: RasterGridSpec,
) -> NDArray[np.float64]:
    with rasterio.open(path) as dataset:
        if dataset.width != grid.width or dataset.height != grid.height:
            raise ValueError(f"hampel_raster_grid_shape_mismatch:{path.name}")
        if dataset.crs is None or dataset.crs.to_string() != grid.target_crs:
            raise ValueError(f"hampel_raster_grid_crs_mismatch:{path.name}")
        if not np.allclose(tuple(dataset.transform)[:6], grid.transform, atol=1e-9, rtol=0):
            raise ValueError(f"hampel_raster_grid_transform_mismatch:{path.name}")
        descriptions = tuple(value or "" for value in dataset.descriptions)
        try:
            indexes = tuple(descriptions.index(name) + 1 for name in band_names)
        except ValueError as error:
            raise ValueError(f"hampel_raster_band_missing:{path.name}") from error
        masked = dataset.read(indexes, masked=True).astype(np.float64)
        return np.asarray(masked.filled(np.nan), dtype=np.float64)


def _validate_source_bundle(
    *,
    source_bundle: Path,
    manifest: Mapping[str, object],
) -> tuple[int, int]:
    total_bytes = 0
    artifacts = _artifact_records(manifest)
    for artifact in artifacts:
        relative_text = _required_text(artifact, "path")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("hampel_source_manifest_path_unsafe")
        path = source_bundle / relative
        content = path.read_bytes()
        if len(content) != _required_int(artifact, "size_bytes"):
            raise ValueError(f"hampel_source_artifact_size_mismatch:{relative_text}")
        if _sha256(content) != _required_text(artifact, "sha256"):
            raise ValueError(f"hampel_source_artifact_hash_mismatch:{relative_text}")
        total_bytes += len(content)
    return len(artifacts), total_bytes


def _artifact_records(manifest: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, list):
        raise ValueError("hampel_source_manifest_artifacts_invalid")
    if not all(isinstance(item, dict) for item in raw):
        raise ValueError("hampel_source_manifest_artifact_invalid")
    return tuple(cast(Mapping[str, object], item) for item in raw)


def _verify_requested_input(
    *,
    source_bundle: Path,
    input_geojson: Path,
) -> Mapping[str, object]:
    if not input_geojson.is_file():
        raise FileNotFoundError(f"hampel_input_geojson_not_found:{input_geojson}")
    content = input_geojson.read_bytes()
    digest = _sha256(content)
    ingestion = _read_json(source_bundle / "json/input/ingestion.json")
    source_files = ingestion.get("source_files")
    if not isinstance(source_files, list):
        raise ValueError("hampel_source_ingestion_files_invalid")
    match = next(
        (item for item in source_files if isinstance(item, dict) and item.get("sha256") == digest),
        None,
    )
    if not isinstance(match, dict):
        raise ValueError("hampel_requested_input_does_not_match_source_bundle")
    return {
        "requested_input_name": input_geojson.name,
        "requested_input_sha256": digest,
        "matched_source_name": match.get("name"),
        "matched_source_bundle": True,
    }


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"hampel_json_root_must_be_object:{path.name}")
    return cast(dict[str, object], value)


def _required_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"hampel_required_text_missing:{key}")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"hampel_required_int_missing:{key}")
    return value


def _required_float(mapping: Mapping[str, object], key: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"hampel_required_float_missing:{key}")
    return float(value)


def _object_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("hampel_internal_int_invalid")
    return value


def _object_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("hampel_internal_float_invalid")
    return float(value)


def _rows_csv_bytes(rows: Sequence[Mapping[str, object]]) -> bytes:
    if not rows:
        raise ValueError("hampel_csv_rows_empty")
    import csv

    output = io.StringIO(newline="")
    fieldnames = list(rows[0])
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: (
                    ""
                    if value is None
                    else str(value).lower()
                    if isinstance(value, bool)
                    else value
                )
                for key, value in row.items()
            }
        )
    return output.getvalue().encode("utf-8")


def _canonical_hash(payload: object) -> str:
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _sha256(content.encode("utf-8"))


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 10) if denominator else 0.0


def _derived_run_id(establishment_id: str, created_at: datetime, analysis_id: str) -> str:
    safe = _SAFE_ESTABLISHMENT.sub("-", establishment_id.strip().lower()).strip("-")
    if not safe:
        raise ValueError("hampel_establishment_id_has_no_safe_characters")
    timestamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{safe}__{timestamp}__{analysis_id[:13]}"
