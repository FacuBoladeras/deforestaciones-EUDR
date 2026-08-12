from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from deforestation_pipeline.agricultural_evidence import (
    AgriculturalEvidenceObservation,
    EvaluatedAgriculturalEvidence,
    IndependenceAssessment,
    canonical_payload_sha256,
    load_agricultural_evidence_policy,
)
from deforestation_pipeline.post_change_attribution import (
    AgriculturalAttributionSupport,
    AttributionContext,
    EventTrajectory,
    _validate_disjoint_event_geometries,
    attribute_post_change_event,
    materialize_post_change_attribution,
)


def _evaluated_evidence(*, land_use: str, decision_eligible: bool) -> EvaluatedAgriculturalEvidence:
    observation = AgriculturalEvidenceObservation.model_construct(
        evidence_id="AGR-PDE-1",
        event_id="PDE-1",
        land_use=land_use,
    )
    assessment = IndependenceAssessment(
        level="methodological" if decision_eligible else "ineligible",
        decision_eligible=decision_eligible,
        reason_codes=["test_policy_assessment"],
        policy_id="test-policy-v1",
        policy_sha256="b" * 64,
    )
    return EvaluatedAgriculturalEvidence.model_construct(
        observation=observation,
        assessment=assessment,
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


def test_policy_eligible_crop_evidence_can_reach_conversion_likely_when_all_gates_pass() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=1.4,
            conjunctive_likely_area_ha=1.2,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
        ),
    )

    assert result["post_change_use"] == "agriculture_likely"
    assert result["automatic_status"] == "conversion_likely"
    assert result["gates"]["all_conversion_likely_gates"] is True
    assert result["conversion_confirmed"] is False
    assert result["persistent_agricultural_area_ha"] == 1.4
    assert result["likely_conversion_area_ha"] == 1.2
    assert "all_conjunctive_conversion_gates_passed" in result["reasons_for"]


def test_policy_evidence_without_materialized_spatial_conjunction_stays_review_required() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
    )

    assert result["automatic_status"] == "review_required"
    assert result["likely_conversion_area_ha"] == 0.0
    assert "persistent_agricultural_spatial_support_missing" in result["reasons_against"]


def test_recovery_zeroes_likely_area_even_with_agricultural_spatial_support() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 0.9, 0.1, 0.4, 0.8)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="crop", decision_eligible=True),)
            }
        ),
        agricultural_support=AgriculturalAttributionSupport(
            persistent_agricultural_area_ha=1.4,
            conjunctive_likely_area_ha=1.2,
            persistent_support_raster_path="persistent.tif",
            conjunctive_support_raster_path="likely.tif",
            source_bundle_manifest_sha256="c" * 64,
        ),
    )

    assert result["post_change_use"] == "forest_recovery"
    assert result["likely_conversion_area_ha"] == 0.0
    assert "strong_alternative_explanation_present" in result["reasons_against"]


def test_overlapping_event_geometries_are_rejected_to_prevent_double_counting() -> None:
    geometry = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }
    features = [
        {"type": "Feature", "geometry": geometry, "properties": {"event_id": event_id}}
        for event_id in ("PDE-1", "PDE-2")
    ]

    try:
        _validate_disjoint_event_geometries(features)
    except ValueError as exc:
        assert "overlap" in str(exc)
    else:
        raise AssertionError("overlapping event geometries accepted")


def test_duplicate_event_ids_are_rejected_even_when_geometries_do_not_overlap() -> None:
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[offset, 0], [offset + 1, 0], [offset + 1, 1], [offset, 1], [offset, 0]]
                ],
            },
            "properties": {"event_id": "PDE-1"},
        }
        for offset in (0, 2)
    ]

    with np.testing.assert_raises_regex(ValueError, "event_id duplicado:PDE-1"):
        _validate_disjoint_event_geometries(features)


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


def test_policy_ineligible_agricultural_claim_cannot_unlock_conversion_likely() -> None:
    result = attribute_post_change_event(
        event=_event(),
        trajectory=_trajectory((1.0, 1.0, 0.1, 0.1, 0.1)),
        context=AttributionContext(
            agricultural_use_evidence={
                "PDE-1": (_evaluated_evidence(land_use="pasture", decision_eligible=False),)
            }
        ),
    )

    assert result["post_change_use"] == "unknown"
    assert result["automatic_status"] == "review_required"
    assert "agricultural_use_evidence_not_policy_eligible" in result["reason_codes"]


def _write_source_bundle(
    root: Path,
    *,
    input_sha: str,
    rf: bool,
    rf_forests: tuple[int, int, int, int, int] = (1, 1, 0, 0, 1),
) -> Path:
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
        for year, forest in zip(range(2020, 2025), rf_forests, strict=True):
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


def _write_persistence_bundle(
    root: Path,
    *,
    input_sha: str,
    source_event_manifest_sha: str | None = None,
    source_event_artifact_sha: str | None = None,
    evidence_set_id: str = "33333333-3333-5333-8333-333333333333",
    declared_support_fraction: float = 1.0,
) -> Path:
    bundle = root / "agriculture"
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

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)
    left, bottom = transformer.transform(0, 1)
    right, top = transformer.transform(1, 2)
    raster_path = "tiffs/evidence/agricultural_persistence/PDE-1_persistent_crop_support.tif"
    path = bundle / raster_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:6933",
        transform=from_origin(left, top, (right - left) / 2, (top - bottom) / 2),
        nodata=-9999.0,
    ) as dataset:
        dataset.write(np.ones((1, 2, 2), dtype="float32"))
    raster_content = path.read_bytes()
    raster_sha = hashlib.sha256(raster_content).hexdigest()
    artifacts.append({"path": raster_path, "size_bytes": len(raster_content), "sha256": raster_sha})
    source = {
        "dataset_id": "dynamic_world_v1",
        "provider": "Google / World Resources Institute",
        "collection": "GOOGLE/DYNAMICWORLD/V1",
        "version": "1",
        "asset": raster_path,
        "license": "CC-BY-4.0",
        "access_date": "2026-08-11",
        "attribution": "Contains modified Dynamic World data",
        "restrictions": [],
    }
    pixel_area_ha = ((right - left) / 2) * ((top - bottom) / 2) / 10000
    event_area = pixel_area_ha * 4
    evidence = {
        "schema_version": "1.0.0",
        "evidence_set_id": evidence_set_id,
        "created_at": "2026-08-11T15:00:00+00:00",
        "observations": [
            {
                "evidence_id": "AGR-PERSIST-PDE-1",
                "event_id": "PDE-1",
                "land_use": "crop",
                "source": source,
                "source_descriptor_sha256": canonical_payload_sha256(source),
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 1], [1, 1], [1, 2], [0, 2], [0, 1]]],
                    "crs": "EPSG:4326",
                },
                "spatial_support": {
                    "source_resolution_m": 10.0,
                    "event_area_ha": event_area,
                    "valid_area_ha": event_area,
                    "attributed_area_ha": event_area * declared_support_fraction,
                    "event_coverage_fraction": 1.0,
                    "attributed_event_fraction": declared_support_fraction,
                    "resampling_method": "none_native_equal_area_grid",
                },
                "temporal_support": {
                    "estimated_onset_window_end": "2022-05-31",
                    "post_change_window_start": "2022-06-01",
                    "post_change_window_end": "2022-12-31",
                    "effective_observation_date": "2022-12-31",
                    "valid_observation_count": 7,
                    "qualifying_observation_count": 2,
                    "minimum_consecutive_qualifying_periods": 2,
                    "observed_consecutive_qualifying_periods": 1,
                    "minimum_qualifying_periods": 2,
                    "observed_qualifying_periods": 2,
                    "minimum_observation_duration_days": 180,
                    "observed_duration_days": 214,
                    "persistence_basis": "seasonal_nonconsecutive_periods",
                    "persistence_satisfied": True,
                    "long_gap_interpolation_used": False,
                    "persistence_rule": "2_of_3_over_180_days_nonconsecutive",
                },
                "quality": {
                    "confidence": None,
                    "confidence_semantics": "deterministic_threshold_support_not_probability",
                    "quality_flags": [],
                    "discarded_observation_reasons": [],
                    "nodata_area_ha": 0.0,
                    "uncertainty_notes": ["test_fixture"],
                },
                "artifact_sha256": raster_sha,
            }
        ],
    }
    persistence_event = {
        "event_id": "PDE-1",
        "persistence_status": "persistent_crop_support",
        "estimated_onset_window_end": "2022-05-31",
        "post_change_window_start": "2022-06-01",
        "post_change_window_end": "2022-12-31",
        "observed_duration_days": 214,
        "valid_period_count": 7,
        "qualifying_period_count": 2,
        "observed_consecutive_qualifying_periods": 1,
        "gap_period_count": 0,
        "maximum_consecutive_gap_periods": 0,
        "long_gap_interpolation_used": False,
        "temporal_disagreement_present": True,
        "cross_source_disagreement_state": "single_source_not_assessed",
        "forest_recovery_assessed": False,
        "event_area_ha": event_area,
        "persistent_crop_area_ha": event_area * declared_support_fraction,
        "persistent_event_fraction": declared_support_fraction,
        "support_raster_path": raster_path,
        "support_raster_sha256": raster_sha,
        "sensitivity": [],
        "per_period_support": [],
        "quality_flags": ["test_fixture"],
    }
    source_collection_bundle: dict[str, Any] = {
        "analysis_id": "44444444-4444-5444-8444-444444444444",
        "input_sha256": input_sha,
        "manifest_sha256": "d" * 64,
        "bundle_name": "raw",
    }
    if source_event_manifest_sha is not None:
        source_collection_bundle["source_event_bundle"] = {
            "analysis_id": "22222222-2222-5222-8222-222222222222",
            "input_sha256": input_sha,
            "manifest_sha256": source_event_manifest_sha,
            "bundle_name": "events",
            "event_artifact_path": "json/evidence/disturbance_events.geojson",
            "event_artifact_sha256": source_event_artifact_sha,
        }
    persistence = {
        "schema_version": "1.0.0",
        "analysis_id": "33333333-3333-5333-8333-333333333333",
        "establishment_id": "field",
        "created_at": "2026-08-11T15:00:00+00:00",
        "source_collection_bundle": source_collection_bundle,
        "persistence_rule": "2_of_3_over_180_days_nonconsecutive",
        "primary_crop_observation_fraction": 0.5,
        "events": [persistence_event],
    }
    register("json/evidence/agricultural_evidence_persistent.json", json.dumps(evidence).encode())
    register("json/evidence/agricultural_persistence.json", json.dumps(persistence).encode())
    manifest = {
        "schema_version": "1.0.0",
        "analysis_id": persistence["analysis_id"],
        "created_at": persistence["created_at"],
        "input_sha256": input_sha,
        "artifacts": artifacts,
    }
    manifest_path = bundle / "json/run/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
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


def test_persistent_bundle_materializes_conjunctive_area_and_reversible_sources(
    tmp_path: Path,
) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha="b" * 64,
        source_event_artifact_sha=hashlib.sha256(
            (events / "json/evidence/disturbance_events.geojson").read_bytes()
        ).hexdigest(),
    )

    output = materialize_post_change_attribution(
        source_event_bundle=events,
        source_rf_bundle=rf,
        source_agricultural_bundle=agriculture,
        agricultural_policy=load_agricultural_evidence_policy(
            Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
        ),
        output_root=tmp_path / "out",
        establishment_id="field",
        context=AttributionContext(),
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    payload = json.loads((output / "json/evidence/post_change_attribution.json").read_text("utf-8"))
    event = payload["events"][0]
    assert event["automatic_status"] == "conversion_likely"
    assert event["likely_conversion_area_ha"] > 0
    assert payload["likely_conversion_area_ha"] == event["likely_conversion_area_ha"]
    assert event["likely_conversion_area_ha"] <= event["persistent_agricultural_area_ha"]
    conjunction = output / event["agricultural_spatial_support"]["conjunctive_support_raster_path"]
    assert conjunction.is_file()
    figure = output / "figures/evidence/likely_conversion_conjunction_PDE-1.png"
    assert figure.is_file()
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    assert manifest["source_bundles"]["agricultural_persistence"]["manifest_sha256"]
    assert manifest["visualizations"][0]["path"] == figure.relative_to(output).as_posix()
    assert (
        manifest["visualizations"][0]["sha256"] == hashlib.sha256(figure.read_bytes()).hexdigest()
    )


def test_persistent_bundle_must_derive_from_exact_current_event_artifact(tmp_path: Path) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha=hashlib.sha256(
            (events / "json/run/manifest.json").read_bytes()
        ).hexdigest(),
        source_event_artifact_sha="b" * 64,
    )

    with np.testing.assert_raises_regex(
        ValueError,
        "agricultural source event artifact does not match current event artifact",
    ):
        materialize_post_change_attribution(
            source_event_bundle=events,
            source_rf_bundle=rf,
            source_agricultural_bundle=agriculture,
            agricultural_policy=load_agricultural_evidence_policy(
                Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
            ),
            output_root=tmp_path / "out",
            establishment_id="field",
            context=AttributionContext(),
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
        )


def test_persistence_and_evidence_documents_must_share_analysis_identity(tmp_path: Path) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    event_manifest_sha = hashlib.sha256(
        (events / "json/run/manifest.json").read_bytes()
    ).hexdigest()
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha=event_manifest_sha,
        source_event_artifact_sha=hashlib.sha256(
            (events / "json/evidence/disturbance_events.geojson").read_bytes()
        ).hexdigest(),
        evidence_set_id="55555555-5555-5555-8555-555555555555",
    )

    with np.testing.assert_raises_regex(
        ValueError,
        "persistence and evidence analysis identity mismatch",
    ):
        materialize_post_change_attribution(
            source_event_bundle=events,
            source_rf_bundle=rf,
            source_agricultural_bundle=agriculture,
            agricultural_policy=load_agricultural_evidence_policy(
                Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
            ),
            output_root=tmp_path / "out",
            establishment_id="field",
            context=AttributionContext(),
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
        )


def test_persistent_raster_area_must_match_declared_contract_area(tmp_path: Path) -> None:
    events = _write_source_bundle(tmp_path, input_sha="a" * 64, rf=False)
    rf = _write_source_bundle(
        tmp_path,
        input_sha="a" * 64,
        rf=True,
        rf_forests=(1, 1, 0, 0, 0),
    )
    agriculture = _write_persistence_bundle(
        tmp_path,
        input_sha="a" * 64,
        source_event_manifest_sha=hashlib.sha256(
            (events / "json/run/manifest.json").read_bytes()
        ).hexdigest(),
        source_event_artifact_sha=hashlib.sha256(
            (events / "json/evidence/disturbance_events.geojson").read_bytes()
        ).hexdigest(),
        declared_support_fraction=0.5,
    )

    with np.testing.assert_raises_regex(
        ValueError,
        "persistent raster area does not match persistence contract",
    ):
        materialize_post_change_attribution(
            source_event_bundle=events,
            source_rf_bundle=rf,
            source_agricultural_bundle=agriculture,
            agricultural_policy=load_agricultural_evidence_policy(
                Path(__file__).resolve().parents[1] / "configs/agricultural-evidence.yml"
            ),
            output_root=tmp_path / "out",
            establishment_id="field",
            context=AttributionContext(),
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
        )
