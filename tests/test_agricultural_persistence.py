from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError
from pyproj import Transformer
from rasterio.transform import from_origin

from deforestation_pipeline.agricultural_collector import _compact_temporal_count_cube
from deforestation_pipeline.agricultural_persistence import (
    AgriculturalPersistenceDocument,
    LegacyAgriculturalPersistenceDocument,
    _bounded_subset_area,
    _candidate_period_coverage_assessment,
    agricultural_persistence_json_schema,
    load_agricultural_persistence_config,
    load_agricultural_persistence_document,
    materialize_agricultural_persistence,
)
from deforestation_pipeline.schemas import raster_grid_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREATED = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)


def _tif(values: tuple[float, float], *, footprint: bool = False) -> bytes:
    from rasterio.io import MemoryFile

    bands = 1 if footprint else 4
    data = np.full((bands, 1, 2), -9999.0, dtype="float32")
    if footprint:
        data[0, 0] = values
    elif values == (-9999.0, -9999.0):
        pass
    else:
        crop = np.asarray(values, dtype="float32")
        data[0, 0] = 3.0
        data[1, 0] = np.where(crop >= 0.5, 2.0, 0.0)
        data[2, 0] = crop
        data[3, 0] = crop
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=2,
            height=1,
            count=bands,
            dtype="float32",
            crs="EPSG:6933",
            transform=from_origin(0, 10, 10, 10),
            nodata=-9999.0,
        ) as dataset:
            dataset.write(data)
            if not footprint:
                dataset.descriptions = (
                    "valid_observation_count",
                    "qualifying_crop_count",
                    "mean_crop_probability",
                    "crop_observation_fraction",
                )
        return bytes(memory.read())


def _raw_bundle(
    root: Path,
    *,
    months: int = 7,
    gap_month: int | None = None,
    valid_months: set[int] | None = None,
    crop_months: set[int] | None = None,
    event_area_exact_ha: float = 0.02,
    footprint_values: tuple[float, float] = (1.0, 1.0),
) -> Path:
    bundle = root / "raw"
    artifacts: list[dict[str, object]] = []

    def register(relative: str, content: bytes) -> None:
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        artifacts.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    footprint_path = "tiffs/evidence/agricultural/PDE-1/event_footprint_fraction.tif"
    register(footprint_path, _tif(footprint_values, footprint=True))
    starts = [
        "2022-06-01",
        "2022-07-01",
        "2022-08-01",
        "2022-09-01",
        "2022-10-01",
        "2022-11-01",
        "2022-12-01",
    ][:months]
    ends = [
        "2022-07-01",
        "2022-08-01",
        "2022-09-01",
        "2022-10-01",
        "2022-11-01",
        "2022-12-01",
        "2023-01-01",
    ][:months]
    rows: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(zip(starts, ends, strict=True)):
        window_id = start[:7]
        raster_path = f"tiffs/evidence/agricultural/PDE-1/{window_id}_dynamic_world.tif"
        valid = index != gap_month and (valid_months is None or index in valid_months)
        crop = index in ({0, 3} if crop_months is None else crop_months)
        values = (-9999.0, -9999.0) if not valid else ((0.8, 0.1) if crop else (0.1, 0.1))
        register(raster_path, _tif(values))
        if not valid:
            areas = {"crop": 0.0, "unknown": 0.0, "nodata": 0.02}
            scenes = 0
        else:
            areas = {
                "crop": 0.01 if index in {0, 3} else 0.0,
                "unknown": 0.01 if index in {0, 3} else 0.02,
                "nodata": 0.0,
            }
            scenes = 3
        for class_name, area in areas.items():
            rows.append(
                {
                    "event_id": "PDE-1",
                    "window_id": window_id,
                    "start_date": start,
                    "end_date_exclusive": end,
                    "source_id": "dynamic_world_v1",
                    "class_name": class_name,
                    "area_ha": area,
                    "event_area_ha": 0.02,
                    "event_area_exact_ha": event_area_exact_ha,
                    "grid_error_area_ha": 0.0,
                    "matched_scene_count": scenes,
                    "mean_crop_probability": None,
                    "mean_crop_observation_fraction": None,
                    "raster_path": raster_path,
                    "event_footprint_path": footprint_path,
                    "grid_sha256": "a" * 64,
                    "persistence_evaluated": False,
                }
            )

    inverse = Transformer.from_crs("EPSG:6933", "EPSG:4326", always_xy=True)
    x0, y0 = inverse.transform(0, 0)
    x1, y1 = inverse.transform(20, 10)
    geometry = {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }
    transform_values = (10.0, 0.0, 0.0, 0.0, -10.0, 10.0)
    bounds = (0.0, 0.0, 20.0, 10.0)
    grid_sha = raster_grid_sha256(
        target_crs="EPSG:6933",
        resolution_m=10.0,
        width=2,
        height=1,
        transform=transform_values,
        bounds=bounds,
        alignment_strategy="projected_crs_origin_outward_snap",
        pixel_orientation="north_up",
    )
    for row in rows:
        row["grid_sha256"] = grid_sha
    raw = {
        "schema_version": "1.0.0",
        "analysis_id": "11111111-1111-5111-8111-111111111111",
        "establishment_id": "test",
        "created_at": CREATED.isoformat(),
        "analysis_end_date": ends[-1],
        "status": "collected",
        "attribution_generated": False,
        "persistence_evaluated": False,
        "events": [
            {
                "event_id": "PDE-1",
                "collection_status": "collected",
                "estimated_onset_window_end": "2022-05-31",
                "window_count": months,
                "quality_flags": [],
                "event_area_exact_ha": event_area_exact_ha,
                "event_area_raster_ha": 0.02,
                "grid_error_area_ha": 0.0,
                "grid": {
                    "schema_version": "1.0.0",
                    "target_crs": "EPSG:6933",
                    "resolution_m": 10.0,
                    "width": 2,
                    "height": 1,
                    "transform": list(transform_values),
                    "bounds": list(bounds),
                    "alignment_strategy": "projected_crs_origin_outward_snap",
                    "pixel_orientation": "north_up",
                    "nodata": -9999.0,
                    "aoi_mask_required": True,
                    "grid_sha256": grid_sha,
                },
            }
        ],
        "rows": rows,
    }
    metadata = {
        "schema_version": "1.0.0",
        "source": {
            "source_id": "dynamic_world_v1",
            "provider": "Google / World Resources Institute",
            "collection": "GOOGLE/DYNAMICWORLD/V1",
            "version": "1",
            "license": "CC-BY-4.0",
            "access_date": "2026-08-11",
            "attribution": "Contains modified Dynamic World data",
            "restrictions": [],
            "spatial_resolution_m": 10.0,
        },
    }
    feature = {
        "type": "Feature",
        "geometry": geometry,
        "properties": {"event_id": "PDE-1"},
    }
    features = {"type": "FeatureCollection", "features": [feature]}
    register("json/evidence/agricultural_evidence.json", json.dumps(raw).encode())
    register("json/evidence/agricultural_evidence_metadata.json", json.dumps(metadata).encode())
    register("json/evidence/agricultural_evidence.geojson", json.dumps(features).encode())
    manifest = {
        "schema_version": "1.0.0",
        "analysis_id": raw["analysis_id"],
        "created_at": CREATED.isoformat(),
        "input_sha256": "f" * 64,
        "source_event_bundle": {
            "analysis_id": "22222222-2222-5222-8222-222222222222",
            "input_sha256": "f" * 64,
            "manifest_sha256": "e" * 64,
            "bundle_name": "events",
            "event_artifact_path": "json/evidence/disturbance_events.geojson",
            "event_artifact_sha256": "d" * 64,
        },
        "artifacts": artifacts,
    }
    manifest_path = bundle / "json/run/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def _compact_bundle(bundle: Path) -> Path:
    payload_path = bundle / "json/evidence/agricultural_evidence.json"
    payload = json.loads(payload_path.read_text("utf-8"))
    rows = payload["rows"]
    raster_contents = {
        row["raster_path"]: (bundle / row["raster_path"]).read_bytes() for row in rows
    }
    content, band_pairs = _compact_temporal_count_cube(
        event_rows=rows,
        raster_contents=raster_contents,
    )
    cube_path = "tiffs/evidence/agricultural/PDE-1/temporal_counts_uint16.tif"
    target = bundle / cube_path
    target.write_bytes(content)
    for row in rows:
        valid_band, qualifying_band = band_pairs[row["window_id"]]
        row["raster_path"] = cube_path
        row["valid_count_band"] = valid_band
        row["qualifying_count_band"] = qualifying_band
    payload["schema_version"] = "1.1.0"
    payload_content = json.dumps(payload).encode()
    payload_path.write_bytes(payload_content)
    manifest_path = bundle / "json/run/manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    for artifact in manifest["artifacts"]:
        if artifact["path"] == "json/evidence/agricultural_evidence.json":
            artifact["size_bytes"] = len(payload_content)
            artifact["sha256"] = hashlib.sha256(payload_content).hexdigest()
    manifest["artifacts"].append(
        {
            "path": cube_path,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_config_versions_single_period_evidence_and_signal_strength() -> None:
    config = load_agricultural_persistence_config(
        PROJECT_ROOT / "configs/agricultural-persistence.yml"
    )
    assert config.schema_version == "2.1.0"
    assert config.minimum_valid_periods >= 3
    assert config.minimum_qualifying_periods == 1
    assert config.minimum_post_onset_duration_days >= 180
    assert config.evidence_rule == "candidate_coverage_at_least_five_percent_post_cutoff"
    assert config.minimum_candidate_agricultural_coverage_fraction == pytest.approx(0.05)
    assert config.signal_strength_periods.model_dump() == {
        "weak": 1,
        "moderate": 2,
        "strong": 3,
    }
    assert config.primary_crop_observation_fraction in config.sensitivity_crop_observation_fractions


def test_nonconsecutive_crop_months_produce_moderate_support_without_interpolation(
    tmp_path: Path,
) -> None:
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(tmp_path),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )

    payload = AgriculturalPersistenceDocument.model_validate_json(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    event = payload.events[0]
    assert event.persistence_status == "agricultural_support_detected"
    assert event.signal_strength == "moderate"
    assert event.observed_consecutive_qualifying_periods == 1
    assert event.qualifying_period_count == 2
    assert event.long_gap_interpolation_used is False
    assert event.persistent_crop_area_ha == 0.01
    low_area = event.sensitivity[0].persistent_crop_area_ha
    high_area = event.sensitivity[-1].persistent_crop_area_ha
    assert low_area >= high_area
    assert event.support_raster_path is not None
    assert (output / event.support_raster_path).is_file()
    figure = output / "figures/evidence/agricultural_persistence_PDE-1.png"
    assert figure.is_file()
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    visualizations = manifest["visualizations"]
    assert len(visualizations) == 1
    assert visualizations[0]["path"] == figure.relative_to(output).as_posix()
    assert visualizations[0]["sha256"] == hashlib.sha256(figure.read_bytes()).hexdigest()

    evidence = json.loads(
        (output / "json/evidence/agricultural_evidence_persistent.json").read_text("utf-8")
    )
    temporal = evidence["observations"][0]["temporal_support"]
    assert temporal["evidence_basis"] == "candidate_coverage_at_least_five_percent_post_cutoff"
    assert temporal["evidence_satisfied"] is True
    assert temporal["signal_strength"] == "moderate"


def test_occurrence_area_is_bounded_by_exact_event_area_under_float_roundoff(
    tmp_path: Path,
) -> None:
    exact_area = 0.01 - 4e-10
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(
            tmp_path,
            months=2,
            crop_months={0},
            event_area_exact_ha=exact_area,
            footprint_values=(1.0, 0.0),
        ),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )

    evidence = json.loads(
        (output / "json/evidence/agricultural_evidence_persistent.json").read_text("utf-8")
    )
    spatial = evidence["observations"][0]["spatial_support"]
    assert spatial["event_area_ha"] == exact_area
    assert spatial["attributed_area_ha"] == exact_area
    assert spatial["attributed_event_fraction"] == 1.0


def test_subset_area_guard_rejects_material_excess() -> None:
    with pytest.raises(
        ValueError,
        match="agricultural_subset_area_exceeds_exact_event_area",
    ):
        _bounded_subset_area(1.0001, 1.0)


def test_one_qualifying_month_in_short_series_emits_weak_evidence(tmp_path: Path) -> None:
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(tmp_path, months=2, crop_months={0}),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )

    document = load_agricultural_persistence_document(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    assert isinstance(document, AgriculturalPersistenceDocument)
    event = document.events[0]
    assert event.persistence_status == "agricultural_support_detected"
    assert event.signal_strength == "weak"
    assert event.qualifying_period_count == 1
    assert event.persistent_crop_area_ha == 0.01
    assert "minimum_post_onset_duration_not_met" in event.quality_flags
    assert event.strength_raster_path is not None
    assert (output / event.strength_raster_path).is_file()
    assert sum(item.area_ha for item in event.signal_strength_areas) == pytest.approx(
        event.persistent_crop_area_ha
    )

    evidence = json.loads(
        (output / "json/evidence/agricultural_evidence_persistent.json").read_text("utf-8")
    )
    temporal = evidence["observations"][0]["temporal_support"]
    assert evidence["schema_version"] == "2.1.0"
    assert temporal["effective_observation_date"] == "2022-06-30"
    assert temporal["signal_strength"] == "weak"


@pytest.mark.parametrize(
    ("crop_months", "expected_strength"),
    [({0}, "weak"), ({0, 3}, "moderate"), ({0, 2, 4}, "strong")],
)
def test_signal_strength_follows_qualifying_period_count(
    tmp_path: Path,
    crop_months: set[int],
    expected_strength: str,
) -> None:
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(tmp_path, crop_months=crop_months),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )
    document = load_agricultural_persistence_document(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    assert isinstance(document, AgriculturalPersistenceDocument)
    assert document.events[0].signal_strength == expected_strength


@pytest.mark.parametrize(
    ("qualifying_areas", "expected_detected", "expected_strength"),
    [
        ([4.99], False, None),
        ([5.0], True, "weak"),
        ([5.0, 7.0], True, "moderate"),
        ([5.0, 7.0, 12.0], True, "strong"),
    ],
)
def test_candidate_agricultural_gate_uses_inclusive_five_percent_coverage(
    qualifying_areas: list[float],
    expected_detected: bool,
    expected_strength: str | None,
) -> None:
    assessment = _candidate_period_coverage_assessment(
        qualifying_areas_ha=qualifying_areas,
        candidate_area_ha=100.0,
        minimum_coverage_fraction=0.05,
    )

    assert assessment.detected is expected_detected
    assert assessment.signal_strength == expected_strength
    assert assessment.qualifying_period_count == sum(area >= 5.0 for area in qualifying_areas)
    assert assessment.maximum_coverage_fraction == pytest.approx(max(qualifying_areas) / 100)


def test_candidate_coverage_denominator_is_full_candidate_not_observed_area() -> None:
    assessment = _candidate_period_coverage_assessment(
        qualifying_areas_ha=[0.4],
        candidate_area_ha=10.0,
        minimum_coverage_fraction=0.05,
    )

    assert assessment.detected is False
    assert assessment.maximum_coverage_fraction == pytest.approx(0.04)


def test_legacy_v1_config_still_materializes_and_loads_v1_contract(tmp_path: Path) -> None:
    config_path = tmp_path / "agricultural-persistence-v1.yml"
    config_path.write_text(
        """schema_version: \"1.0.0\"
source_id: dynamic_world_v1
minimum_valid_periods: 3
minimum_qualifying_periods: 2
minimum_post_onset_duration_days: 180
primary_crop_observation_fraction: 0.50
sensitivity_crop_observation_fractions: [0.40, 0.50, 0.60]
minimum_valid_observations_per_period: 2
maximum_unobserved_gap_periods: 3
seasonality_rule: seasonal_nonconsecutive_periods
long_gap_interpolation_used: false
support_raster_nodata: -9999.0
""",
        encoding="utf-8",
    )
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(tmp_path),
        output_root=tmp_path / "out",
        config_path=config_path,
        created_at=CREATED,
    )

    document = load_agricultural_persistence_document(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    assert isinstance(document, LegacyAgriculturalPersistenceDocument)
    assert document.schema_version == "1.0.0"
    assert document.events[0].persistence_status == "persistent_crop_support"


def test_compact_temporal_cube_preserves_persistence_result(tmp_path: Path) -> None:
    legacy = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(tmp_path / "legacy"),
        output_root=tmp_path / "legacy-out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )
    compact = materialize_agricultural_persistence(
        source_collection_bundle=_compact_bundle(_raw_bundle(tmp_path / "compact")),
        output_root=tmp_path / "compact-out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )
    legacy_event = json.loads(
        (legacy / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )["events"][0]
    compact_event = json.loads(
        (compact / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )["events"][0]

    for field in (
        "persistence_status",
        "observed_duration_days",
        "valid_period_count",
        "qualifying_period_count",
        "persistent_crop_area_ha",
        "persistent_event_fraction",
        "sensitivity",
    ):
        assert compact_event[field] == legacy_event[field]


def test_persistence_accepts_collector_source_event_created_at_identity(tmp_path: Path) -> None:
    bundle = _raw_bundle(tmp_path)
    manifest_path = bundle / "json/run/manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["source_event_bundle"]["created_at"] = CREATED.isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    output = materialize_agricultural_persistence(
        source_collection_bundle=bundle,
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )

    payload = json.loads(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    assert payload["source_collection_bundle"]["source_event_bundle"]["created_at"] == (
        CREATED.isoformat().replace("+00:00", "Z")
    )


def test_short_series_quality_does_not_erase_single_month_occurrence(tmp_path: Path) -> None:
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(tmp_path, months=2),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )
    payload = json.loads(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    event = payload["events"][0]
    assert event["persistence_status"] == "agricultural_support_detected"
    assert event["signal_strength"] == "weak"
    assert "minimum_post_onset_duration_not_met" in event["quality_flags"]
    evidence = json.loads(
        (output / "json/evidence/agricultural_evidence_persistent.json").read_text("utf-8")
    )
    assert len(evidence["observations"]) == 1
    assert evidence["observations"][0]["temporal_support"]["signal_strength"] == "weak"


def test_nodata_gap_is_preserved_and_not_interpolated(tmp_path: Path) -> None:
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(tmp_path, gap_month=2),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )
    payload = json.loads(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    event = payload["events"][0]
    assert event["gap_period_count"] == 1
    assert event["long_gap_interpolation_used"] is False
    assert event["per_period_support"][2]["valid_area_ha"] == 0.0


def test_short_observed_duration_is_reported_as_quality_not_occurrence_gate(
    tmp_path: Path,
) -> None:
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(
            tmp_path,
            valid_months={0, 1, 2},
            crop_months={0, 1},
        ),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )

    payload = AgriculturalPersistenceDocument.model_validate_json(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    event = payload.events[0]
    assert event.persistence_status == "agricultural_support_detected"
    assert event.signal_strength == "moderate"
    assert event.persistent_crop_area_ha > 0
    assert "minimum_post_onset_duration_not_met" in event.quality_flags
    evidence = json.loads(
        (output / "json/evidence/agricultural_evidence_persistent.json").read_text("utf-8")
    )
    assert len(evidence["observations"]) == 1


def test_long_gap_is_reported_without_erasing_observed_occurrence(tmp_path: Path) -> None:
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(
            tmp_path,
            valid_months={0, 5, 6},
            crop_months={0, 5},
        ),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )

    payload = AgriculturalPersistenceDocument.model_validate_json(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    event = payload.events[0]
    assert event.persistence_status == "agricultural_support_detected"
    assert event.signal_strength == "moderate"
    assert event.maximum_consecutive_gap_periods == 4
    assert event.persistent_crop_area_ha > 0
    assert "long_unobserved_gap_present" in event.quality_flags


def test_tampered_raw_bundle_is_rejected(tmp_path: Path) -> None:
    bundle = _raw_bundle(tmp_path)
    target = bundle / "json/evidence/agricultural_evidence.json"
    target.write_bytes(target.read_bytes() + b" ")
    try:
        materialize_agricultural_persistence(
            source_collection_bundle=bundle,
            output_root=tmp_path / "out",
            config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
            created_at=CREATED,
        )
    except ValueError as exc:
        assert "integrity" in str(exc)
    else:
        raise AssertionError("tampered bundle accepted")


def test_persistence_contract_rejects_caller_independence() -> None:
    schema = agricultural_persistence_json_schema()
    assert schema["$id"].endswith(":2.1.0")
    with np.testing.assert_raises(ValidationError):
        AgriculturalPersistenceDocument.model_validate(
            {
                "schema_version": "1.0.0",
                "analysis_id": "11111111-1111-5111-8111-111111111111",
                "establishment_id": "x",
                "created_at": CREATED.isoformat(),
                "source_collection_bundle": {},
                "events": [],
                "independent": True,
            }
        )


def test_nonpersistent_contract_cannot_retain_positive_support_area(tmp_path: Path) -> None:
    output = materialize_agricultural_persistence(
        source_collection_bundle=_raw_bundle(tmp_path),
        output_root=tmp_path / "out",
        config_path=PROJECT_ROOT / "configs/agricultural-persistence.yml",
        created_at=CREATED,
    )
    payload = json.loads(
        (output / "json/evidence/agricultural_persistence.json").read_text("utf-8")
    )
    payload["events"][0]["persistence_status"] = "not_persistent"

    with np.testing.assert_raises(ValidationError):
        AgriculturalPersistenceDocument.model_validate(payload)
