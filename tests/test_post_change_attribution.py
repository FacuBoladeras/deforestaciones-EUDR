from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from deforestation_pipeline.post_change_attribution import (
    AgriculturalUseEvidence,
    AttributionContext,
    EventTrajectory,
    attribute_post_change_event,
    materialize_post_change_attribution,
)


def _event() -> dict[str, object]:
    return {
        "event_id": "PDE-1",
        "event_type": "persistent_disturbance_event",
        "primary_interpretation_domain": "automated_forest",
        "area_ha": 2.0,
        "area_threshold_met": True,
        "estimated_onset_period_id": "2022-MAM",
    }


def _trajectory(values: tuple[float, ...]) -> EventTrajectory:
    return EventTrajectory(
        years=(2020, 2021, 2022, 2023, 2024),
        forest_fraction=values,
        mean_hard_vote_fraction=values,
        candidate_loss_fraction=(0.0, 0.0, 0.8, 0.8, 0.8),
        evaluable_pixel_count=(20, 20, 20, 20, 20),
    )


def test_persistent_rf_loss_without_post_use_evidence_stays_unknown() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(),
    )

    assert result["post_change_use"] == "unknown"
    assert result["automatic_status"] == "review_required"
    assert "agricultural_use_evidence_missing" in result["reason_codes"]
    assert result["human_review_required"] is True
    assert result["conversion_confirmed"] is False


def test_independent_crop_evidence_can_reach_conversion_likely_when_all_gates_pass() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": AgriculturalUseEvidence(
                    land_use="crop",
                    source="independent_reference_v1",
                    independent=True,
                )
            }
        ),
    )

    assert result["post_change_use"] == "agriculture_likely"
    assert result["automatic_status"] == "conversion_likely"
    assert result["gates"]["all_conversion_likely_gates"] is True
    assert result["conversion_confirmed"] is False


def test_declared_plantation_plus_loss_and_recovery_is_managed_harvest_not_conversion() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 0.9, 0.1, 0.4, 0.8)),
        context=AttributionContext(
            declared_land_use="managed_forest_plantation",
            declared_context_source="user_declared",
        ),
    )

    assert result["post_change_use"] == "managed_harvest"
    assert result["automatic_status"] == "review_required"
    assert result["gates"]["strong_alternative_explanation_absent"] is False
    assert "declared_managed_forest_context" in result["reason_codes"]


def test_recovery_without_managed_context_is_forest_recovery() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 0.9, 0.1, 0.4, 0.8)),
        context=AttributionContext(),
    )

    assert result["post_change_use"] == "forest_recovery"
    assert result["automatic_status"] == "review_required"
    assert "forest_recovery_after_trough" in result["reason_codes"]


def test_non_independent_agricultural_claim_cannot_unlock_conversion_likely() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": AgriculturalUseEvidence(
                    land_use="pasture",
                    source="producer_statement",
                    independent=False,
                )
            }
        ),
    )

    assert result["post_change_use"] == "unknown"
    assert result["automatic_status"] == "review_required"
    assert "agricultural_use_evidence_not_independent" in result["reason_codes"]


def _write_source_bundle(root: Path, *, input_sha: str, rf: bool) -> Path:
    bundle = root / ("rf" if rf else "events")
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

    if rf:
        register(
            "json/evidence/rf_forest_deltas_metadata.json",
            json.dumps({"score_semantics": "uncalibrated_binary_tree_vote_fraction"}).encode(
                "utf-8"
            ),
        )
        transform = from_origin(0, 2, 1, 1)
        for year, forest in zip(range(2020, 2025), (1, 1, 0, 0, 1), strict=True):
            for kind, value, dtype, nodata in (
                ("class", forest, "int16", -9999),
                ("vote_fraction", float(forest), "float32", -9999.0),
            ):
                relative = f"tiffs/evidence/rf_forest_{kind}_{year}.tif"
                path = bundle / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with rasterio.open(
                    path,
                    "w",
                    driver="GTiff",
                    width=2,
                    height=2,
                    count=1,
                    dtype=dtype,
                    crs="EPSG:4326",
                    transform=transform,
                    nodata=nodata,
                ) as dataset:
                    dataset.write(np.full((1, 2, 2), value, dtype=dtype))
                content = path.read_bytes()
                artifacts.append(
                    {
                        "path": relative,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            if year > 2020:
                relative = f"tiffs/evidence/rf_forest_transition_{year}_vs_2020.tif"
                path = bundle / relative
                with rasterio.open(
                    path,
                    "w",
                    driver="GTiff",
                    width=2,
                    height=2,
                    count=1,
                    dtype="int16",
                    crs="EPSG:4326",
                    transform=transform,
                    nodata=-9999,
                ) as dataset:
                    dataset.write(np.full((1, 2, 2), 4 if forest else 3, dtype="int16"))
                content = path.read_bytes()
                artifacts.append(
                    {
                        "path": relative,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
    else:
        feature_collection = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 1], [1, 1], [1, 2], [0, 2], [0, 1]]],
                    },
                    "properties": _event(),
                }
            ],
        }
        register(
            "json/evidence/disturbance_events.geojson",
            json.dumps(feature_collection).encode("utf-8"),
        )
    manifest = {
        "schema_version": "test",
        "analysis_id": (
            "11111111-1111-5111-8111-111111111111" if rf else "22222222-2222-5222-8222-222222222222"
        ),
        "created_at": "2026-08-10T12:00:00+00:00",
        "input_sha256": input_sha,
        "artifacts": artifacts,
    }
    path = bundle / "json/run/manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_materializer_overlays_events_with_rf_trajectory_and_publishes_manifest(
    tmp_path: Path,
) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=True)

    output = materialize_post_change_attribution(
        source_event_bundle=events,
        source_rf_bundle=rf,
        output_root=tmp_path / "out",
        establishment_id="field",
        context=AttributionContext(
            declared_land_use="managed_forest_plantation",
            declared_context_source="user_declared",
        ),
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    payload = json.loads(
        (output / "json/evidence/post_change_attribution.json").read_text(encoding="utf-8")
    )
    assert payload["events"][0]["post_change_use"] == "managed_harvest"
    assert payload["events"][0]["conversion_confirmed"] is False
    manifest = json.loads((output / "json/run/manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["artifacts"]) == 6
    assert all((output / item["path"]).is_file() for item in manifest["artifacts"])
    assert manifest["source_bundles"]["events"]["input_sha256"] == "a" * 64
    assert len(manifest["code"]["module_sha256"]) == 64
    assert len(manifest["code"]["schema_sha256"]) == 64


def test_materializer_rejects_bundles_from_different_input_geometry(tmp_path: Path) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(tmp_path, input_sha="b" * 64, rf=True)

    try:
        materialize_post_change_attribution(
            source_event_bundle=events,
            source_rf_bundle=rf,
            output_root=tmp_path / "out",
            establishment_id="field",
            context=AttributionContext(),
            created_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
    except ValueError as exc:
        assert "input_sha256" in str(exc)
    else:
        raise AssertionError("se esperaba rechazo de bundles incompatibles")
