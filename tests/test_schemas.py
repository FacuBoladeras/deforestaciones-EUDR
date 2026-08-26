"""Pruebas de contratos Pydantic y JSON Schema."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from deforestation_pipeline.agricultural_collector import agricultural_collection_json_schema
from deforestation_pipeline.agricultural_evidence import (
    agricultural_evidence_json_schema,
    agricultural_evidence_policy_json_schema,
)
from deforestation_pipeline.agricultural_persistence import agricultural_persistence_json_schema
from deforestation_pipeline.disturbance_evidence import (
    DisturbanceEvidenceMetadata,
    disturbance_evidence_json_schema,
)
from deforestation_pipeline.schemas import (
    AnalysisSummary,
    AssessmentStatus,
    EstablishmentInput,
    GeometrySource,
    analysis_summary_json_schema,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _valid_summary_payload() -> dict[str, Any]:
    return {
        "establishment_id": "synthetic-establishment-001",
        "analysis_id": UUID("12345678-1234-5678-1234-567812345678"),
        "analysis_version": "0.1.0",
        "cutoff_date": date(2020, 12, 31),
        "analysis_end_date": date(2026, 7, 23),
        "geometry_source": GeometrySource.OTHER,
        "forest_area_2020_ha": 25.0,
        "detected_change_area_ha": 0.4,
        "likely_conversion_area_ha": 0.0,
        "status": AssessmentStatus.REVIEW_REQUIRED,
        "confidence": 0.65,
        "quality_flags": ["subthreshold_change"],
        "events": [],
        "datasets": [],
        "parameters_hash": "a" * 64,
        "created_at": datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    }


def test_synthetic_establishment_matches_input_contract() -> None:
    """La muestra anonimizada debe seguir siendo una entrada válida."""
    path = PROJECT_ROOT / "data" / "samples" / "establishment.synthetic.json"
    establishment = EstablishmentInput.model_validate_json(path.read_text(encoding="utf-8"))

    assert establishment.establishment_id == "synthetic-establishment-001"
    assert establishment.geometry.type == "Polygon"
    assert establishment.geometry_crs == "EPSG:4326"


def test_summary_serializes_the_minimum_contract() -> None:
    """El resumen debe serializar fechas, UUID y estados como JSON."""
    summary = AnalysisSummary.model_validate(_valid_summary_payload())
    payload = summary.model_dump(mode="json")

    assert payload["cutoff_date"] == "2020-12-31"
    assert payload["status"] == "review_required"
    assert payload["analysis_id"] == "12345678-1234-5678-1234-567812345678"


def test_summary_rejects_likely_area_above_detected_area() -> None:
    """La atribución probable debe ser un subconjunto del cambio detectado."""
    payload = _valid_summary_payload()
    payload["likely_conversion_area_ha"] = 0.5

    with pytest.raises(ValidationError, match="no puede superar"):
        AnalysisSummary.model_validate(payload)


def test_summary_requires_timezone() -> None:
    """La procedencia temporal no puede depender de la zona horaria local."""
    payload = _valid_summary_payload()
    payload["created_at"] = datetime(2026, 7, 23, 12, 0)

    with pytest.raises(ValidationError, match="debe incluir zona horaria"):
        AnalysisSummary.model_validate(payload)


def test_committed_json_schema_matches_pydantic_model() -> None:
    """El artefacto versionado no puede divergir silenciosamente del modelo."""
    path = PROJECT_ROOT / "data" / "schemas" / "analysis-summary-v1.0.0.json"
    committed_schema = json.loads(path.read_text(encoding="utf-8"))

    assert committed_schema == analysis_summary_json_schema()


def test_committed_disturbance_evidence_schema_matches_pydantic_model() -> None:
    """La evidencia publicada no debe confundirse con la configuración metodológica."""
    path = PROJECT_ROOT / "data" / "schemas" / "disturbance-evidence-v1.2.0.json"
    committed_schema = json.loads(path.read_text(encoding="utf-8"))

    assert committed_schema == disturbance_evidence_json_schema()
    assert committed_schema["title"] == DisturbanceEvidenceMetadata.__name__
    assert committed_schema["$id"] == ("urn:deforestation-pipeline:disturbance-evidence:1.2.0")


@pytest.mark.parametrize(
    ("filename", "factory"),
    [
        ("agricultural-evidence-v1.0.0.json", agricultural_evidence_json_schema),
        (
            "agricultural-evidence-policy-v1.0.0.json",
            agricultural_evidence_policy_json_schema,
        ),
        (
            "agricultural-collection-v1.0.0.json",
            agricultural_collection_json_schema,
        ),
        (
            "agricultural-persistence-v1.0.0.json",
            agricultural_persistence_json_schema,
        ),
    ],
)
def test_committed_agricultural_schemas_match_pydantic_models(filename: str, factory: Any) -> None:
    committed = json.loads((PROJECT_ROOT / "data" / "schemas" / filename).read_text("utf-8"))
    assert committed == factory()


def test_post_change_attribution_v5_schema_formalizes_polygonal_event_geometry() -> None:
    schema = json.loads(
        (PROJECT_ROOT / "data/schemas/post-change-attribution-v5.0.0.json").read_text("utf-8")
    )
    record = schema["$defs"]["record"]
    required = set(record["required"])

    assert {
        "candidate_geometry",
        "conjunctive_conversion_evidence_geometry",
        "likely_conversion_geometry",
    } <= required
    event_then = record["allOf"][1]["then"]["properties"]
    assert event_then["likely_conversion_area_ha"]["exclusiveMinimum"] == 0.5
    assert event_then["likely_conversion_geometry"] == {"$ref": "#/$defs/polygonalGeometry"}
