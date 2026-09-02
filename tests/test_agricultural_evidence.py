from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from deforestation_pipeline.agricultural_evidence import (
    AgriculturalEvidenceDocument,
    AgriculturalEvidenceDocumentV2,
    AgriculturalEvidenceObservationV2,
    AgriculturalEvidencePolicy,
    agricultural_evidence_json_schema,
    agricultural_evidence_policy_json_schema,
    canonical_payload_sha256,
    evaluate_agricultural_evidence,
    load_agricultural_evidence_policy,
)


def _source(*, dataset_id: str = "dynamic_world_v1") -> dict[str, Any]:
    if dataset_id == "mapbiomas_argentina_collection2":
        provider = "MapBiomas Argentina"
        collection = (
            "projects/mapbiomas-public/assets/argentina/lulc/collection2/"
            "mapbiomas_argentina_collection2_integration_v3"
        )
    else:
        provider = "Google / World Resources Institute"
        collection = "GOOGLE/DYNAMICWORLD/V1"
    return {
        "dataset_id": dataset_id,
        "provider": provider,
        "collection": collection,
        "version": "1",
        "asset": "synthetic-event-crop-support.tif",
        "license": "CC-BY-4.0",
        "access_date": "2026-08-11",
        "attribution": "Contains modified Dynamic World data",
        "restrictions": [],
    }


def _observation(*, dataset_id: str = "dynamic_world_v1") -> dict[str, Any]:
    source = _source(dataset_id=dataset_id)
    return {
        "evidence_id": "AGR-PDE-1-DW",
        "event_id": "PDE-1",
        "land_use": "crop",
        "source": source,
        "source_descriptor_sha256": canonical_payload_sha256(source),
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [0.1, 0.0], [0.1, 0.1], [0.0, 0.0]]],
            "crs": "EPSG:4326",
        },
        "spatial_support": {
            "source_resolution_m": 10.0,
            "event_area_ha": 2.0,
            "valid_area_ha": 1.8,
            "attributed_area_ha": 1.4,
            "event_coverage_fraction": 0.9,
            "attributed_event_fraction": 0.7,
        },
        "temporal_support": {
            "estimated_onset_window_end": "2022-05-31",
            "post_change_window_start": "2022-06-01",
            "post_change_window_end": "2024-12-31",
            "effective_observation_date": "2024-12-31",
            "valid_observation_count": 24,
            "qualifying_observation_count": 18,
            "minimum_consecutive_qualifying_periods": 2,
            "observed_consecutive_qualifying_periods": 5,
            "persistence_satisfied": True,
            "long_gap_interpolation_used": False,
            "persistence_rule": "two_or_more_consecutive_qualifying_periods",
        },
        "quality": {
            "confidence": 0.82,
            "confidence_semantics": "class_probability_mean_not_calibrated",
            "quality_flags": [],
            "discarded_observation_reasons": ["cloud_or_shadow"],
            "nodata_area_ha": 0.2,
            "uncertainty_notes": ["class_probability_not_calibrated"],
        },
        "artifact_sha256": "a" * 64,
    }


def _document(*, dataset_id: str = "dynamic_world_v1") -> AgriculturalEvidenceDocument:
    return AgriculturalEvidenceDocument.model_validate(
        {
            "schema_version": "1.0.0",
            "evidence_set_id": "12345678-1234-5678-9234-567812345678",
            "created_at": "2026-08-11T12:00:00+00:00",
            "observations": [_observation(dataset_id=dataset_id)],
        }
    )


def _policy() -> AgriculturalEvidencePolicy:
    return AgriculturalEvidencePolicy.model_validate(
        {
            "schema_version": "1.0.0",
            "policy_id": "agricultural-independence-v1",
            "consumer_model": {
                "model_id": "rf_forest_multiyear_2020_2024_v1",
                "model_family": "random_forest",
                "training_dataset_ids": ["mapbiomas_argentina_collection2"],
                "validation_dataset_ids": ["jrc_gfc2020_v3"],
                "predictor_dataset_ids": ["hlsl30_v002", "hlss30_v002"],
                "sensor_families": ["landsat", "sentinel2"],
            },
            "decision_eligible_levels": ["methodological", "full"],
            "sources": [
                {
                    "dataset_id": "dynamic_world_v1",
                    "version": "1",
                    "provider": "Google / World Resources Institute",
                    "collection": "GOOGLE/DYNAMICWORLD/V1",
                    "evidence_role": "candidate_independent",
                    "upstream_dataset_ids": [],
                    "sensor_families": ["sentinel2"],
                    "model_family": "dynamic_world_cnn",
                },
                {
                    "dataset_id": "mapbiomas_argentina_collection2",
                    "version": "2",
                    "provider": "MapBiomas Argentina",
                    "collection": (
                        "projects/mapbiomas-public/assets/argentina/lulc/collection2/"
                        "mapbiomas_argentina_collection2_integration_v3"
                    ),
                    "evidence_role": "corroborative_non_independent",
                    "upstream_dataset_ids": [],
                    "sensor_families": ["landsat"],
                    "model_family": "mapbiomas_random_forest",
                },
            ],
        }
    )


def test_valid_document_derives_methodological_independence_from_policy() -> None:
    evaluated = evaluate_agricultural_evidence(_document(), _policy())

    item = evaluated["PDE-1"][0]
    assert item.assessment.level == "methodological"
    assert item.assessment.decision_eligible is True
    assert "shared_sensor_family" in item.assessment.reason_codes
    assert item.assessment.policy_sha256


def test_caller_cannot_supply_independent_boolean() -> None:
    payload = _document().model_dump(mode="json")
    payload["observations"][0]["independent"] = True

    with pytest.raises(ValidationError, match="independent"):
        AgriculturalEvidenceDocument.model_validate(payload)


def test_training_source_is_ineligible_even_if_it_claims_agriculture() -> None:
    document = _document(dataset_id="mapbiomas_argentina_collection2")
    source = document.observations[0].source.model_copy(update={"version": "2"})
    observation = document.observations[0].model_copy(
        update={
            "source": source,
            "source_descriptor_sha256": canonical_payload_sha256(source.model_dump(mode="json")),
        }
    )
    document = document.model_copy(update={"observations": [observation]})

    item = evaluate_agricultural_evidence(document, _policy())["PDE-1"][0]

    assert item.assessment.level == "ineligible"
    assert item.assessment.decision_eligible is False
    assert "consumer_training_or_validation_lineage_overlap" in item.assessment.reason_codes


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"independent": True}, "independent"),
        ({"artifact_sha256": "bad"}, "artifact_sha256"),
    ],
)
def test_contract_rejects_untrusted_or_unverifiable_fields(
    mutation: dict[str, Any], message: str
) -> None:
    payload = _document().model_dump(mode="json")
    payload["observations"][0].update(mutation)
    with pytest.raises(ValidationError, match=message):
        AgriculturalEvidenceDocument.model_validate(payload)


def test_contract_rejects_single_date_or_inconsistent_persistence() -> None:
    payload = _document().model_dump(mode="json")
    temporal = payload["observations"][0]["temporal_support"]
    temporal["post_change_window_end"] = temporal["post_change_window_start"]
    temporal["observed_consecutive_qualifying_periods"] = 1

    with pytest.raises(ValidationError, match=r"ventana|persistencia"):
        AgriculturalEvidenceDocument.model_validate(payload)


def test_contract_rejects_unknown_geometry_crs() -> None:
    payload = _document().model_dump(mode="json")
    payload["observations"][0]["geometry"]["crs"] = "EPSG:999999"

    with pytest.raises(ValidationError, match="CRS"):
        AgriculturalEvidenceDocument.model_validate(payload)


def test_contract_requires_timezone_and_unique_evidence_ids() -> None:
    payload = _document().model_dump(mode="json")
    payload["created_at"] = datetime(2026, 8, 11, 12, 0)
    payload["observations"].append(payload["observations"][0])

    with pytest.raises(ValidationError, match=r"zona horaria|duplicados"):
        AgriculturalEvidenceDocument.model_validate(payload)


def test_schema_factories_are_versioned() -> None:
    assert agricultural_evidence_json_schema()["$id"].endswith(":2.0.0")
    assert agricultural_evidence_policy_json_schema()["$id"].endswith(":1.0.0")
    assert _document().evidence_set_id == UUID("12345678-1234-5678-9234-567812345678")
    assert _document().observations[0].source.access_date == date(2026, 8, 11)
    assert _document().created_at == datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def test_production_policy_registers_dynamic_world_as_methodological_candidate() -> None:
    project_root = Path(__file__).resolve().parents[1]
    policy = load_agricultural_evidence_policy(
        project_root / "configs" / "agricultural-evidence.yml"
    )
    evaluated = evaluate_agricultural_evidence(_document(), policy)["PDE-1"][0]

    assert evaluated.assessment.level == "methodological"
    assert evaluated.assessment.decision_eligible is True
    assert "shared_sensor_family" in evaluated.assessment.reason_codes


def test_single_post_event_period_is_policy_eligible_but_not_calibrated_confidence() -> None:
    payload = _document().model_dump(mode="json")
    payload["schema_version"] = "2.0.0"
    temporal = payload["observations"][0]["temporal_support"]
    payload["observations"][0]["temporal_support"] = {
        "estimated_onset_window_end": temporal["estimated_onset_window_end"],
        "post_change_window_start": temporal["post_change_window_start"],
        "post_change_window_end": temporal["post_change_window_end"],
        "effective_observation_date": "2022-06-30",
        "valid_period_count": 1,
        "qualifying_period_count": 1,
        "evidence_basis": "any_qualifying_post_event_period",
        "evidence_satisfied": True,
        "signal_strength": "weak",
        "long_gap_interpolation_used": False,
        "evidence_rule": "1_qualifying_post_event_period",
    }
    document = AgriculturalEvidenceDocumentV2.model_validate(payload)

    item = evaluate_agricultural_evidence(document, _policy())["PDE-1"][0]

    assert item.assessment.decision_eligible is True
    assert isinstance(item.observation, AgriculturalEvidenceObservationV2)
    assert item.observation.temporal_support.signal_strength == "weak"
    assert item.observation.quality.confidence_semantics != "probability_of_conversion"


def test_v2_signal_strength_must_match_qualifying_period_count() -> None:
    payload = _document().model_dump(mode="json")
    payload["schema_version"] = "2.0.0"
    temporal = payload["observations"][0]["temporal_support"]
    payload["observations"][0]["temporal_support"] = {
        "estimated_onset_window_end": temporal["estimated_onset_window_end"],
        "post_change_window_start": temporal["post_change_window_start"],
        "post_change_window_end": temporal["post_change_window_end"],
        "effective_observation_date": "2022-06-30",
        "valid_period_count": 2,
        "qualifying_period_count": 2,
        "evidence_basis": "any_qualifying_post_event_period",
        "evidence_satisfied": True,
        "signal_strength": "weak",
        "long_gap_interpolation_used": False,
        "evidence_rule": "1_qualifying_post_event_period",
    }

    with pytest.raises(ValidationError, match="signal_strength"):
        AgriculturalEvidenceDocumentV2.model_validate(payload)
