"""Materialización verificable del dominio candidato RF-first.

El adaptador consume dos bundles ya publicados, valida su integridad y produce
un bundle mínimo compatible con los consumidores de candidatos. No ejecuta GEE.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Final, cast
from uuid import NAMESPACE_URL, uuid5

import numpy as np
import rasterio
from numpy.typing import NDArray
from rasterio.features import shapes
from rasterio.io import MemoryFile
from rasterio.warp import transform_geom
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from deforestation_pipeline.change_detection import (
    DISTURBANCE_DIAGNOSTIC_BAND_NAMES,
    DISTURBANCE_SUMMARY_BAND_NAMES,
    DisturbanceStateCode,
)
from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode
from deforestation_pipeline.rf_candidate_evidence_association import (
    RFCandidateEvidenceAssociation,
    associate_rf_candidate_evidence,
)
from deforestation_pipeline.rf_seeded_candidate_domain import (
    CandidateBaselineDomain,
    RFSeededCandidate,
    segment_rf_seeded_candidate_domain,
)
from deforestation_pipeline.rf_terminal_persistence import (
    RFTerminalPersistenceResult,
    classify_rf_terminal_persistence,
)
from deforestation_pipeline.robust_shadow_inventory import (
    RobustShadowAlert,
    build_robust_shadow_inventory,
)
from deforestation_pipeline.schemas import RasterGridSpec, raster_grid_sha256

_ATOMIC_PUBLISH_RETRY_DELAYS_SECONDS: Final = (0.1, 0.2, 0.4, 0.8, 1.6)
FUSION_BUNDLE_SCHEMA_VERSION: Final = "2.0.0"
CANDIDATE_FUSION_SCHEMA_VERSION: Final = "2.0.0"
FUSION_COLLECTION_SCHEMA_ID: Final = "urn:deforestation-pipeline:disturbance-candidate-fusion:2.0.0"
FUSION_COLLECTION_PATH: Final = "json/evidence/disturbance_candidate_fusion.json"
FUSION_EVENT_PATH: Final = "json/evidence/disturbance_events.geojson"
FUSION_MASK_PATH: Final = "tiffs/evidence/disturbance_candidate_fusion_mask.tif"
RF_TERMINAL_PERSISTENCE_PATH: Final = "tiffs/evidence/rf_terminal_persistence.tif"
ROBUST_SHADOW_PATH: Final = "json/evidence/robust_shadow_inventory.json"
ROBUST_SHADOW_MASK_PATH: Final = "tiffs/evidence/robust_shadow_inventory_mask.tif"
_MANIFEST_PATH: Final = "json/run/manifest.json"
_ROBUST_STATE_PATH: Final = "tiffs/evidence/disturbance_robust_state.tif"
_SUMMARY_PATH: Final = "tiffs/evidence/disturbance_summary.tif"
_DIAGNOSTICS_PATH: Final = "tiffs/evidence/disturbance_diagnostics.tif"
_AUTOMATED_PATH: Final = "tiffs/evidence/automated_evaluable_2020.tif"
_REVIEW_PATH: Final = "tiffs/evidence/review_required_2020.tif"
_PERIODS_PATH: Final = "tables/evidence/disturbance_period_summary.csv"
_RF_TRANSITION = re.compile(r"^tiffs/evidence/rf_forest_transition_(?P<year>\d{4})_vs_2020\.tif$")


class CandidateFusionUnavailableError(RuntimeError):
    """Un bundle histórico carece del insumo aditivo de estado robusto."""


def materialize_disturbance_candidate_fusion(
    *,
    source_event_bundle: Path,
    source_rf_bundle: Path,
    output_root: Path,
    created_at: datetime,
) -> Path:
    """Publica candidatos sembrados sólo por RF; robusto y CCDC son soporte."""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("candidate_fusion_created_at_requires_timezone")
    event_root, event_manifest, event_files = _load_bundle(source_event_bundle)
    rf_root, rf_manifest, rf_files = _load_bundle(source_rf_bundle)
    _validate_source_identity(event_manifest, rf_manifest)

    robust, grid = _read_raster(
        event_files,
        _ROBUST_STATE_PATH,
        expected_names=("robust_state_code",),
    )
    summary, summary_grid = _read_raster(
        event_files,
        _SUMMARY_PATH,
        expected_names=DISTURBANCE_SUMMARY_BAND_NAMES,
    )
    diagnostics, diagnostics_grid = _read_raster(
        event_files,
        _DIAGNOSTICS_PATH,
        expected_names=DISTURBANCE_DIAGNOSTIC_BAND_NAMES,
    )
    automated, automated_grid = _read_raster(event_files, _AUTOMATED_PATH)
    review, review_grid = _read_raster(event_files, _REVIEW_PATH)
    for other in (summary_grid, diagnostics_grid, automated_grid, review_grid):
        _validate_matching_grid(grid, other)

    year_paths = sorted(
        (int(match.group("year")), relative)
        for relative in rf_files
        if (match := _RF_TRANSITION.fullmatch(relative)) is not None
    )
    if not year_paths:
        raise ValueError("candidate_fusion_rf_transition_artifacts_missing")
    transitions: list[NDArray[Any]] = []
    for year, relative in year_paths:
        values, transition_grid = _read_raster(
            rf_files,
            relative,
            expected_names=(f"rf_forest_transition_{year}_vs_2020",),
        )
        _validate_matching_grid(grid, transition_grid)
        transition = values[0].copy()
        transition[~np.isfinite(transition)] = int(ForestTransitionCode.INSUFFICIENT_DATA)
        transitions.append(transition)

    onset = summary[DISTURBANCE_SUMMARY_BAND_NAMES.index("first_anomalous_period_index")]
    ccdc_offset = diagnostics[
        DISTURBANCE_DIAGNOSTIC_BAND_NAMES.index("ccdc_break_day_offset_from_cutoff")
    ]
    persistence = classify_rf_terminal_persistence(
        transitions=np.stack(transitions, axis=0),
        years=tuple(year for year, _ in year_paths),
    )
    automated_mask = automated[0] == 1
    review_mask = review[0] == 1
    candidate_domain = segment_rf_seeded_candidate_domain(
        persistence=persistence,
        automated_forest_mask=automated_mask,
        baseline_review_mask=review_mask,
        grid_spec=grid,
    )
    robust_mask = robust[0] == float(DisturbanceStateCode.PERSISTENT_CANDIDATE)
    ccdc_mask = np.isfinite(ccdc_offset)
    associations = associate_rf_candidate_evidence(
        candidate_domain=candidate_domain,
        robust_persistent_mask=robust_mask,
        robust_onset_period_index=onset,
        ccdc_break_mask=ccdc_mask,
        ccdc_break_day_offset=ccdc_offset,
        grid_spec=grid,
    )
    shadow = build_robust_shadow_inventory(
        robust_persistent_mask=(robust[0] == float(DisturbanceStateCode.PERSISTENT_CANDIDATE)),
        robust_onset_period_index=onset,
        rf_candidate_mask=candidate_domain.candidate_mask,
        automated_forest_mask=automated_mask,
        baseline_review_mask=review_mask,
        grid_spec=grid,
    )
    periods = _read_period_axis(event_files[_PERIODS_PATH])
    association_by_id = {record.candidate_id: record for record in associations.records}
    features = tuple(
        _candidate_feature(
            candidate,
            association=association_by_id[candidate.candidate_id],
            grid=grid,
            periods=periods,
        )
        for candidate in candidate_domain.candidates
    )
    collection = {
        "schema_version": CANDIDATE_FUSION_SCHEMA_VERSION,
        "generated_at": created_at.isoformat(),
        "analysis_id": event_manifest["analysis_id"],
        "candidate_domain": "rf_terminal_persistent_forest_loss",
        "segmentation_semantics": "spatial_8_connected_no_gap_filling",
        "ccdc_semantics": "shared_hls_support_or_date_never_veto",
        "robust_semantics": "support_inside_rf_footprint_never_expands_geometry",
        "rf_semantics": "uncalibrated_learned_candidate_segmentation_no_automatic_promotion",
        "candidate_count": candidate_domain.candidate_count,
        "candidates": [
            _candidate_record(candidate, association_by_id[candidate.candidate_id])
            for candidate in candidate_domain.candidates
        ],
        "robust_support_inside_candidate_pixel_count": (
            associations.robust_support_inside_candidate_pixel_count
        ),
        "ccdc_support_inside_candidate_pixel_count": (
            associations.ccdc_support_inside_candidate_pixel_count
        ),
        "ccdc_outside_candidate_pixel_count": associations.ccdc_outside_candidate_pixel_count,
        "robust_shadow_alert_count": shadow.alert_count,
    }
    geojson = {
        "type": "FeatureCollection",
        "name": "rf_seeded_disturbance_candidates",
        "schema_version": CANDIDATE_FUSION_SCHEMA_VERSION,
        "features": list(features),
    }
    files = {
        FUSION_COLLECTION_PATH: _json_bytes(collection),
        FUSION_EVENT_PATH: _json_bytes(geojson),
        FUSION_MASK_PATH: _mask_tiff(candidate_domain.candidate_mask, grid=grid),
        RF_TERMINAL_PERSISTENCE_PATH: _persistence_tiff(persistence, grid=grid),
        ROBUST_SHADOW_PATH: _json_bytes(
            {
                "schema_version": shadow.schema_version,
                "generated_at": created_at.isoformat(),
                "alert_count": shadow.alert_count,
                "alerts": [_shadow_record(alert) for alert in shadow.alerts],
                "semantics": "review_only_never_primary_candidate",
            }
        ),
        ROBUST_SHADOW_MASK_PATH: _mask_tiff(
            shadow.shadow_mask,
            grid=grid,
            band_name="robust_shadow_inventory_mask",
        ),
    }
    source_identities = {
        "disturbance": _source_identity(event_root, event_manifest),
        "rf_annual_deltas": _source_identity(rf_root, rf_manifest),
    }
    fusion_analysis_id = uuid5(
        NAMESPACE_URL,
        "rf-seeded-candidate-domain-v2:"
        + json.dumps(source_identities, sort_keys=True, separators=(",", ":")),
    )
    manifest = {
        "schema_version": FUSION_BUNDLE_SCHEMA_VERSION,
        "analysis_id": str(fusion_analysis_id),
        "establishment_id": event_manifest.get("establishment_id"),
        "created_at": created_at.isoformat(),
        "input_sha256": event_manifest["input_sha256"],
        "manifest_self_excluded": True,
        "remote_data_accessed": False,
        "candidate_count": candidate_domain.candidate_count,
        "candidate_domain": "rf_terminal_persistent_forest_loss",
        "robust_shadow_alert_count": shadow.alert_count,
        "event_artifact_path": FUSION_EVENT_PATH,
        "automatic_promotion_allowed": False,
        "sources": source_identities,
    }
    name = (
        "rf-candidate-domain__"
        f"{created_at.strftime('%Y%m%dT%H%M%S%fZ')}__{str(event_manifest['analysis_id'])[:8]}"
    )
    return _publish(output_root.resolve() / name, files=files, manifest=manifest)


def _load_bundle(bundle: Path) -> tuple[Path, dict[str, Any], dict[str, Path]]:
    root = bundle.resolve()
    manifest_path = root / _MANIFEST_PATH
    if not root.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("candidate_fusion_source_manifest_missing")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        raise ValueError("candidate_fusion_source_manifest_invalid")
    files: dict[str, Path] = {}
    for artifact in payload["artifacts"]:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise ValueError("candidate_fusion_source_artifact_invalid")
        relative = cast(str, artifact["path"])
        path_token = Path(relative)
        if path_token.is_absolute() or ".." in path_token.parts or relative in files:
            raise ValueError("candidate_fusion_source_artifact_path_unsafe_or_duplicate")
        path = (root / path_token).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != artifact.get("size_bytes")
            or _sha256_file(path) != artifact.get("sha256")
        ):
            raise ValueError("source_artifact_integrity_mismatch")
        files[relative] = path
    return root, payload, files


def _validate_source_identity(event: Mapping[str, Any], rf: Mapping[str, Any]) -> None:
    if not isinstance(event.get("input_sha256"), str) or event.get("input_sha256") != rf.get(
        "input_sha256"
    ):
        raise ValueError("candidate_fusion_source_input_sha256_mismatch")
    if (
        event.get("establishment_id") is not None
        and rf.get("establishment_id") is not None
        and event.get("establishment_id") != rf.get("establishment_id")
    ):
        raise ValueError("candidate_fusion_source_establishment_id_mismatch")


def _read_raster(
    files: Mapping[str, Path],
    relative: str,
    *,
    expected_names: tuple[str, ...] | None = None,
) -> tuple[NDArray[np.float64], RasterGridSpec]:
    path = files.get(relative)
    if path is None:
        if relative == _ROBUST_STATE_PATH:
            raise CandidateFusionUnavailableError("candidate_fusion_raw_robust_state_unavailable")
        raise ValueError(f"candidate_fusion_required_artifact_missing:{relative}")
    with rasterio.open(path) as dataset:
        names = tuple(name or "" for name in dataset.descriptions)
        if expected_names is not None and names != expected_names:
            raise ValueError("candidate_fusion_raster_band_contract_mismatch")
        values = dataset.read().astype(np.float64)
        if dataset.nodata is not None:
            values[values == float(dataset.nodata)] = np.nan
        affine = dataset.transform
        transform = (
            float(affine.a),
            float(affine.b),
            float(affine.c),
            float(affine.d),
            float(affine.e),
            float(affine.f),
        )
        bounds = (
            float(dataset.bounds.left),
            float(dataset.bounds.bottom),
            float(dataset.bounds.right),
            float(dataset.bounds.top),
        )
        tags = dataset.tags()
        identity = {
            "target_crs": str(dataset.crs),
            "resolution_m": abs(float(dataset.transform.a)),
            "width": dataset.width,
            "height": dataset.height,
            "transform": transform,
            "bounds": bounds,
            "alignment_strategy": "projected_crs_origin_outward_snap",
            "pixel_orientation": "north_up",
        }
        derived_grid_sha256 = raster_grid_sha256(**identity)
        tagged_grid_sha256 = tags.get("grid_sha256")
        if tagged_grid_sha256 is not None and tagged_grid_sha256 != derived_grid_sha256:
            raise ValueError("candidate_fusion_grid_sha256_mismatch")
        nodata = float(dataset.nodata) if dataset.nodata is not None else -9999.0
        grid = RasterGridSpec(
            target_crs=cast(str, identity["target_crs"]),
            resolution_m=cast(float, identity["resolution_m"]),
            width=cast(int, identity["width"]),
            height=cast(int, identity["height"]),
            transform=transform,
            bounds=bounds,
            nodata=nodata,
            aoi_mask_required=True,
            alignment_strategy="projected_crs_origin_outward_snap",
            pixel_orientation="north_up",
            grid_sha256=derived_grid_sha256,
        )
    return values, grid


def _validate_matching_grid(expected: RasterGridSpec, actual: RasterGridSpec) -> None:
    if (
        expected.grid_sha256 != actual.grid_sha256
        or expected.target_crs != actual.target_crs
        or expected.width != actual.width
        or expected.height != actual.height
        or any(
            not math.isclose(left, right, abs_tol=1e-8)
            for left, right in zip(expected.transform, actual.transform, strict=True)
        )
    ):
        raise ValueError("candidate_fusion_source_grid_mismatch")


def _read_period_axis(path: Path) -> dict[int, tuple[str, date, date]]:
    rows = csv.DictReader(StringIO(path.read_text(encoding="utf-8")))
    periods: dict[int, tuple[str, date, date]] = {}
    for row in rows:
        index = int(row["period_index"])
        periods[index] = (
            row["period_id"],
            date.fromisoformat(row["start_date"]),
            date.fromisoformat(row["end_date_exclusive"]),
        )
    return periods


def _candidate_feature(
    candidate: RFSeededCandidate,
    *,
    association: RFCandidateEvidenceAssociation,
    grid: RasterGridSpec,
    periods: Mapping[int, tuple[str, date, date]],
) -> dict[str, Any]:
    mask = np.zeros((grid.height, grid.width), dtype=np.uint8)
    rows, columns = zip(*candidate.pixels, strict=True)
    mask[np.asarray(rows), np.asarray(columns)] = 1
    geometries = [
        shape(geometry)
        for geometry, value in shapes(
            mask, mask=mask.astype(bool), transform=rasterio.Affine(*grid.transform)
        )
        if int(value) == 1
    ]
    projected = unary_union(geometries)
    wgs84 = transform_geom(grid.target_crs, "EPSG:4326", mapping(projected), precision=8)
    onset_id, onset_start, onset_end = _candidate_onset(candidate, association, periods=periods)
    bounds = shape(wgs84).bounds
    properties = {
        **_candidate_record(candidate, association),
        "event_id": candidate.candidate_id,
        "event_type": "persistent_disturbance_event",
        "record_type": "persistent_disturbance_candidate",
        "interpretation_level": "candidate_episode",
        "primary_interpretation_domain": (
            "automated_forest"
            if candidate.baseline_domain == CandidateBaselineDomain.AUTOMATED_FOREST
            else "baseline_review"
        ),
        "candidate_area_reference_ha": candidate.area_ha,
        "candidate_footprint_above_visec_area_reference": candidate.area_ha > 0.5,
        "estimated_onset_period_id": onset_id,
        "estimated_onset_window_start": onset_start.isoformat() if onset_start else None,
        "estimated_onset_window_end": onset_end.isoformat() if onset_end else None,
        "onset_semantics": "latest_rf_first_loss_year_end_for_candidate_wide_post_window",
        "bbox_wgs84": list(bounds),
        "segmentation_semantics": candidate.segmentation_semantics,
        "attribution_generated": False,
        "automatic_final_assessment_generated": False,
    }
    return {
        "type": "Feature",
        "id": candidate.candidate_id,
        "geometry": wgs84,
        "properties": properties,
    }


def _candidate_record(
    candidate: RFSeededCandidate,
    association: RFCandidateEvidenceAssociation,
) -> dict[str, Any]:
    payload = asdict(candidate)
    payload.pop("pixels")
    payload["baseline_domain"] = candidate.baseline_domain.value
    payload.update(asdict(association))
    payload["support_sources"] = [source.value for source in association.support_sources]
    payload["resolution"] = (
        "candidate_only"
        if association.robust_support_pixel_count > 0
        and candidate.baseline_domain == CandidateBaselineDomain.AUTOMATED_FOREST
        else "review_required"
    )
    return payload


def _shadow_record(alert: RobustShadowAlert) -> dict[str, Any]:
    payload = asdict(alert)
    payload.pop("pixels")
    payload["baseline_domain"] = alert.baseline_domain.value
    return payload


def _candidate_onset(
    candidate: RFSeededCandidate,
    association: RFCandidateEvidenceAssociation,
    *,
    periods: Mapping[int, tuple[str, date, date]],
) -> tuple[str, date, date]:
    year = candidate.first_loss_year_range[1]
    rf_onset = (f"RF-{year}", date(year, 1, 1), date(year, 12, 31))
    if association.robust_onset_period_range is None:
        return rf_onset
    robust_period = periods.get(association.robust_onset_period_range[1])
    if robust_period is None:
        return rf_onset
    robust_onset = (robust_period[0], robust_period[1], robust_period[2] - timedelta(days=1))
    return max((rf_onset, robust_onset), key=lambda item: item[2])


def _mask_tiff(
    mask: NDArray[np.bool_],
    *,
    grid: RasterGridSpec,
    band_name: str = "rf_seeded_disturbance_candidate_mask",
) -> bytes:
    values = np.asarray(mask, dtype=np.uint8)
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=grid.width,
            height=grid.height,
            count=1,
            dtype="uint8",
            crs=grid.target_crs,
            transform=rasterio.Affine(*grid.transform),
            nodata=255,
            compress="deflate",
        ) as dataset:
            dataset.write(values, 1)
            dataset.descriptions = (band_name,)
            dataset.update_tags(
                schema_version=CANDIDATE_FUSION_SCHEMA_VERSION,
                grid_sha256=grid.grid_sha256,
                automatic_promotion_allowed="false",
            )
        return bytes(memory.read())


def _persistence_tiff(
    persistence: RFTerminalPersistenceResult,
    *,
    grid: RasterGridSpec,
) -> bytes:
    values = np.stack(
        (
            persistence.status,
            persistence.first_loss_year,
            persistence.last_evaluable_year,
            persistence.terminal_consecutive_loss_years,
            persistence.evaluable_year_count,
            persistence.gap_year_count,
        ),
        axis=0,
    ).astype(np.int16)
    names = (
        "rf_terminal_persistence_status",
        "rf_first_loss_year",
        "rf_last_evaluable_year",
        "rf_terminal_consecutive_loss_years",
        "rf_evaluable_year_count",
        "rf_gap_year_count",
    )
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=grid.width,
            height=grid.height,
            count=len(names),
            dtype="int16",
            crs=grid.target_crs,
            transform=rasterio.Affine(*grid.transform),
            nodata=-9999,
            compress="deflate",
        ) as dataset:
            dataset.write(values)
            dataset.descriptions = names
            dataset.update_tags(
                schema_version=persistence.schema_version,
                grid_sha256=grid.grid_sha256,
                persistence_semantics="terminal_consecutive_loss_without_gap_imputation",
            )
        return bytes(memory.read())


def _source_identity(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "analysis_id": manifest["analysis_id"],
        "manifest_sha256": _sha256_file(root / _MANIFEST_PATH),
    }


def _publish(target: Path, *, files: Mapping[str, bytes], manifest: dict[str, Any]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for relative, content in files.items():
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        manifest["artifacts"] = [
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "media_type": (
                    "application/geo+json"
                    if relative.endswith(".geojson")
                    else "application/json"
                    if relative.endswith(".json")
                    else "image/tiff"
                ),
            }
            for relative, content in sorted(files.items())
        ]
        manifest_path = temporary / _MANIFEST_PATH
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(_json_bytes(manifest))
        if target.exists():
            raise FileExistsError("candidate_fusion_output_collision")
        _replace_directory_with_retry(temporary, target)
        return target
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _replace_directory_with_retry(source: Path, target: Path) -> None:
    """Tolera bloqueos exclusivos transitorios de Windows al publicar el bundle."""
    for delay_seconds in (*_ATOMIC_PUBLISH_RETRY_DELAYS_SECONDS, None):
        if target.exists():
            raise FileExistsError("candidate_fusion_output_collision")
        try:
            source.replace(target)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) != 5 or delay_seconds is None:
                raise
            time.sleep(delay_seconds)


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def disturbance_candidate_fusion_json_schema() -> dict[str, Any]:
    """Contrato determinístico del inventario candidato RF-first."""
    non_negative_integer = {"type": "integer", "minimum": 0}
    non_negative_number = {"type": "number", "minimum": 0}
    nullable_integer_pair = {
        "oneOf": [
            {
                "type": "array",
                "prefixItems": [{"type": "integer"}, {"type": "integer"}],
                "minItems": 2,
                "maxItems": 2,
            },
            {"type": "null"},
        ]
    }
    candidate_properties: dict[str, Any] = {
        "candidate_id": {"type": "string", "pattern": r"^RFC-[0-9A-F]{12}$"},
        "pixel_count": {"type": "integer", "minimum": 1},
        "area_ha": {"type": "number", "exclusiveMinimum": 0},
        "seed_source": {"const": "rf_terminal_persistent_forest_loss"},
        "baseline_domain": {"enum": ["automated_forest", "baseline_review", "mixed"]},
        "automated_forest_pixel_count": non_negative_integer,
        "baseline_review_pixel_count": non_negative_integer,
        "first_loss_year_range": nullable_integer_pair,
        "last_evaluable_year_range": nullable_integer_pair,
        "terminal_consecutive_loss_year_range": nullable_integer_pair,
        "evaluable_year_count_range": nullable_integer_pair,
        "gap_year_count_range": nullable_integer_pair,
        "segmentation_semantics": {"const": "spatial_8_connected_no_gap_filling"},
        "rf_evidence_role": {"const": "learned_candidate_segmentation"},
        "rf_calibrated_probability": {"const": False},
        "rf_independent_evidence": {"const": False},
        "automatic_promotion_allowed": {"const": False},
        "candidate_pixel_count": {"type": "integer", "minimum": 1},
        "rf_seed_pixel_count": {"type": "integer", "minimum": 1},
        "robust_support_pixel_count": non_negative_integer,
        "robust_support_fraction": {"type": "number", "minimum": 0, "maximum": 1},
        "robust_support_area_ha": non_negative_number,
        "robust_unknown_onset_pixel_count": non_negative_integer,
        "robust_onset_period_range": nullable_integer_pair,
        "ccdc_support_pixel_count": non_negative_integer,
        "ccdc_support_fraction": {"type": "number", "minimum": 0, "maximum": 1},
        "ccdc_support_area_ha": non_negative_number,
        "ccdc_unknown_date_pixel_count": non_negative_integer,
        "ccdc_break_day_offset_range": nullable_integer_pair,
        "support_sources": {
            "type": "array",
            "items": {"enum": ["robust_persistent_support", "ccdc_break_support"]},
            "uniqueItems": True,
        },
        "segmentation_source": {"const": "rf_terminal_persistent_forest_loss"},
        "robust_role": {"const": "support_inside_rf_footprint_never_expands_geometry"},
        "ccdc_role": {"const": "shared_hls_support_or_date_never_veto"},
        "shared_hls_support_is_independent": {"const": False},
        "geometry_expansion_pixel_count": {"const": 0},
        "resolution": {"enum": ["candidate_only", "review_required"]},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": FUSION_COLLECTION_SCHEMA_ID,
        "title": "RF-first disturbance candidate collection",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "generated_at",
            "analysis_id",
            "candidate_domain",
            "segmentation_semantics",
            "ccdc_semantics",
            "robust_semantics",
            "rf_semantics",
            "candidate_count",
            "candidates",
            "robust_support_inside_candidate_pixel_count",
            "ccdc_support_inside_candidate_pixel_count",
            "ccdc_outside_candidate_pixel_count",
            "robust_shadow_alert_count",
        ],
        "properties": {
            "schema_version": {"const": CANDIDATE_FUSION_SCHEMA_VERSION},
            "generated_at": {"type": "string", "format": "date-time"},
            "analysis_id": {"type": "string", "format": "uuid"},
            "candidate_domain": {"const": "rf_terminal_persistent_forest_loss"},
            "segmentation_semantics": {"const": "spatial_8_connected_no_gap_filling"},
            "ccdc_semantics": {"const": "shared_hls_support_or_date_never_veto"},
            "robust_semantics": {"const": "support_inside_rf_footprint_never_expands_geometry"},
            "rf_semantics": {
                "const": "uncalibrated_learned_candidate_segmentation_no_automatic_promotion"
            },
            "candidate_count": non_negative_integer,
            "candidates": {"type": "array", "items": {"$ref": "#/$defs/candidate"}},
            "robust_support_inside_candidate_pixel_count": non_negative_integer,
            "ccdc_support_inside_candidate_pixel_count": non_negative_integer,
            "ccdc_outside_candidate_pixel_count": non_negative_integer,
            "robust_shadow_alert_count": non_negative_integer,
        },
        "$defs": {
            "candidate": {
                "type": "object",
                "additionalProperties": False,
                "required": list(candidate_properties),
                "properties": candidate_properties,
            },
        },
        "x-schema-version": CANDIDATE_FUSION_SCHEMA_VERSION,
    }
