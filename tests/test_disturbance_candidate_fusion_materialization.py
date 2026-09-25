from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from deforestation_pipeline.agricultural_collector import _load_event_bundle, _parse_events
from deforestation_pipeline.change_detection import (
    DISTURBANCE_DIAGNOSTIC_BAND_NAMES,
    DISTURBANCE_SUMMARY_BAND_NAMES,
    DisturbanceStateCode,
)
from deforestation_pipeline.disturbance_candidate_fusion_materialization import (
    _publish,
    disturbance_candidate_fusion_json_schema,
    materialize_disturbance_candidate_fusion,
)
from deforestation_pipeline.forest_rf_temporal import ForestTransitionCode
from deforestation_pipeline.schemas import raster_grid_sha256


def _tiff(values: np.ndarray, names: tuple[str, ...]) -> bytes:
    cube = np.asarray(values)
    if cube.ndim == 2:
        cube = cube[np.newaxis, ...]
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=cube.shape[2],
            height=cube.shape[1],
            count=cube.shape[0],
            dtype=str(cube.dtype),
            crs="EPSG:32721",
            transform=Affine(30, 0, 500000, 0, -30, 6200000),
            nodata=-9999,
        ) as dataset:
            dataset.write(cube)
            dataset.descriptions = names
            dataset.update_tags(
                grid_sha256=raster_grid_sha256(
                    target_crs="EPSG:32721",
                    resolution_m=30.0,
                    width=cube.shape[2],
                    height=cube.shape[1],
                    transform=(30.0, 0.0, 500000.0, 0.0, -30.0, 6200000.0),
                    bounds=(
                        500000.0,
                        6200000.0 - 30.0 * cube.shape[1],
                        500000.0 + 30.0 * cube.shape[2],
                        6200000.0,
                    ),
                    alignment_strategy="projected_crs_origin_outward_snap",
                    pixel_orientation="north_up",
                )
            )
        return bytes(memory.read())


def _bundle(root: Path, files: dict[str, bytes]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    artifacts = [
        {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": "image/tiff" if relative.endswith(".tif") else "text/csv",
        }
        for relative, content in sorted(files.items())
    ]
    manifest = {
        "schema_version": "fixture-1",
        "analysis_id": "00000000-0000-0000-0000-000000000123",
        "establishment_id": "synthetic",
        "created_at": "2026-08-28T12:00:00+00:00",
        "input_sha256": "b" * 64,
        "artifacts": artifacts,
    }
    manifest_path = root / "json/run/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _source_bundles(tmp_path: Path) -> tuple[Path, Path]:
    summary = np.full((len(DISTURBANCE_SUMMARY_BAND_NAMES), 1, 3), -9999, dtype=np.float32)
    summary[DISTURBANCE_SUMMARY_BAND_NAMES.index("first_anomalous_period_index")] = [0, 2, -9999]
    diagnostics = np.full((len(DISTURBANCE_DIAGNOSTIC_BAND_NAMES), 1, 3), -9999, dtype=np.float32)
    diagnostics[DISTURBANCE_DIAGNOSTIC_BAND_NAMES.index("ccdc_break_day_offset_from_cutoff")] = [
        -9999,
        -9999,
        365,
    ]
    robust = np.asarray(
        [[int(DisturbanceStateCode.PERSISTENT_CANDIDATE)] * 2 + [0]], dtype=np.float32
    )
    full = _bundle(
        tmp_path / "full",
        {
            "tiffs/evidence/disturbance_robust_state.tif": _tiff(robust, ("robust_state_code",)),
            "tiffs/evidence/disturbance_summary.tif": _tiff(
                summary, DISTURBANCE_SUMMARY_BAND_NAMES
            ),
            "tiffs/evidence/disturbance_diagnostics.tif": _tiff(
                diagnostics, DISTURBANCE_DIAGNOSTIC_BAND_NAMES
            ),
            "tiffs/evidence/automated_evaluable_2020.tif": _tiff(
                np.asarray([[1, 1, 0]], dtype=np.float32), ("automated_evaluable_2020",)
            ),
            "tiffs/evidence/review_required_2020.tif": _tiff(
                np.asarray([[0, 0, 1]], dtype=np.float32), ("review_required_2020",)
            ),
            "tables/evidence/disturbance_period_summary.csv": (
                b"period_index,period_id,start_date,end_date_exclusive\n"
                b"0,2021-MAM,2021-03-01,2021-06-01\n"
                b"2,2021-SON,2021-09-01,2021-12-01\n"
            ),
        },
    )
    loss = int(ForestTransitionCode.CANDIDATE_FOREST_LOSS)
    stable = int(ForestTransitionCode.STABLE_FOREST)
    rf_files = {
        f"tiffs/evidence/rf_forest_transition_{year}_vs_2020.tif": _tiff(
            np.asarray([[stable, loss, loss]], dtype=np.int16),
            (f"rf_forest_transition_{year}_vs_2020",),
        )
        for year in (2021, 2022, 2023, 2024)
    }
    return full, _bundle(tmp_path / "rf", rf_files)


def test_atomic_publish_retries_transient_windows_access_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "published"
    real_replace = Path.replace
    attempts = 0

    def replace_with_transient_lock(source: Path, destination: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            error = PermissionError(13, "directorio bloqueado temporalmente")
            error.winerror = 5
            raise error
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace_with_transient_lock)

    published = _publish(
        target,
        files={"json/evidence/minimal.json": b"{}"},
        manifest={"schema_version": "test"},
    )

    assert published == target
    assert attempts == 2
    assert target.is_dir()


def test_materializer_publishes_rf_seeded_candidates_with_support_and_shadow(
    tmp_path: Path,
) -> None:
    full, rf = _source_bundles(tmp_path)

    output = materialize_disturbance_candidate_fusion(
        source_event_bundle=full,
        source_rf_bundle=rf,
        output_root=tmp_path / "out",
        created_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
    )

    manifest = json.loads((output / "json/run/manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2.0.0"
    assert manifest["input_sha256"] == "b" * 64
    assert manifest["candidate_domain"] == "rf_terminal_persistent_forest_loss"
    collection = json.loads(
        (output / "json/evidence/disturbance_candidate_fusion.json").read_text(encoding="utf-8")
    )
    schema = disturbance_candidate_fusion_json_schema()
    assert set(collection) == set(schema["required"])
    assert set(collection["candidates"][0]) == set(schema["$defs"]["candidate"]["required"])
    assert collection["robust_support_inside_candidate_pixel_count"] == 1
    assert collection["ccdc_support_inside_candidate_pixel_count"] == 1
    assert collection["ccdc_outside_candidate_pixel_count"] == 0
    geojson = json.loads(
        (output / "json/evidence/disturbance_events.geojson").read_text(encoding="utf-8")
    )
    assert len(geojson["features"]) == 1
    properties = geojson["features"][0]["properties"]
    assert properties["pixel_count"] == 2
    assert properties["candidate_id"].startswith("RFC-")
    assert properties["segmentation_source"] == "rf_terminal_persistent_forest_loss"
    assert properties["support_sources"] == [
        "robust_persistent_support",
        "ccdc_break_support",
    ]
    assert properties["resolution"] == "review_required"
    assert properties["robust_support_pixel_count"] == 1
    assert properties["ccdc_support_pixel_count"] == 1
    assert properties["estimated_onset_window_end"] == "2021-12-31"
    assert properties["segmentation_semantics"] == "spatial_8_connected_no_gap_filling"
    shadow = json.loads(
        (output / "json/evidence/robust_shadow_inventory.json").read_text(encoding="utf-8")
    )
    assert shadow["alert_count"] == 1
    assert shadow["alerts"][0]["pixel_count"] == 1
    assert shadow["alerts"][0]["can_seed_primary_candidate"] is False
    _, _, downstream_collection = _load_event_bundle(output)
    downstream_events = _parse_events(downstream_collection)
    assert tuple(event.event_id for event in downstream_events) == (properties["event_id"],)


def test_materializer_rejects_tampered_source_artifact(tmp_path: Path) -> None:
    full, rf = _source_bundles(tmp_path)
    (full / "tiffs/evidence/disturbance_robust_state.tif").write_bytes(b"tampered")

    with np.testing.assert_raises_regex(ValueError, "source_artifact_integrity_mismatch"):
        materialize_disturbance_candidate_fusion(
            source_event_bundle=full,
            source_rf_bundle=rf,
            output_root=tmp_path / "out",
            created_at=datetime(2026, 8, 28, 12, tzinfo=UTC),
        )
