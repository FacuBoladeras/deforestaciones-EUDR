"""Pruebas HTTP de los dos primeros incrementos de la API interna."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from deforestation_api.app import create_app
from deforestation_api.settings import ApiSettings


def _write_boundary(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-65.0, -35.0],
                        [-58.0, -35.0],
                        [-58.0, -28.0],
                        [-65.0, -28.0],
                        [-65.0, -35.0],
                    ]
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _settings(
    tmp_path: Path,
    *,
    max_vertices: int = 50_000,
    max_request_bytes: int = 1_000_000,
) -> ApiSettings:
    return ApiSettings(
        database_path=tmp_path / "private" / "jobs.sqlite3",
        storage_root=tmp_path / "private" / "objects",
        output_root=tmp_path / "outputs",
        jurisdiction_boundary_path=_write_boundary(tmp_path / "boundary.geojson"),
        max_vertices=max_vertices,
        max_request_bytes=max_request_bytes,
        owner_id="local-internal",
        retention_days=30,
    )


def _feature(*, west: float = -60.3, south: float = -32.1) -> dict[str, object]:
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [west + 0.02, south],
                    [west + 0.02, south + 0.02],
                    [west, south + 0.02],
                    [west, south],
                ]
            ],
        },
    }


def test_health_and_openapi_expose_versioned_contract(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        health = client.get("/health")
        schema = client.get("/openapi.json").json()

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "service": "deforestation-api", "version": "0.1.0"}
    assert "/api/v1/geometries/validate" in schema["paths"]
    assert "/api/v1/analyses" in schema["paths"]
    assert "/api/v1/analyses/{analysis_id}" in schema["paths"]


def test_validate_geometry_uses_domain_rules_and_metric_area(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/api/v1/geometries/validate",
            headers={"Content-Type": "application/geo+json"},
            json=_feature(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["geometry_type"] == "Polygon"
    assert payload["source_crs"] == "EPSG:4326"
    assert payload["valid"] is True
    assert payload["repair_required"] is False
    assert payload["area_ha_estimate"] > 0
    assert payload["warnings"] == []


def test_validate_geometry_rejects_non_polygon_and_outside_jurisdiction(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        point = client.post(
            "/api/v1/geometries/validate",
            json={"type": "Point", "coordinates": [-60.0, -32.0]},
        )
        outside = client.post(
            "/api/v1/geometries/validate",
            json=_feature(west=-50.0, south=-20.0),
        )

    assert point.status_code == 422
    assert point.json()["error"]["code"] == "invalid_request"
    assert outside.status_code == 422
    assert outside.json()["error"]["code"] == "invalid_geometry"
    assert "fuera de la jurisdicción" in outside.json()["error"]["details"][0]


def test_validate_geometry_enforces_vertex_limit(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, max_vertices=4))) as client:
        response = client.post("/api/v1/geometries/validate", json=_feature())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "geometry_too_complex"


def test_request_limit_uses_received_bytes_not_only_content_length(tmp_path: Path) -> None:
    body = json.dumps(_feature()).encode("utf-8")
    with TestClient(create_app(_settings(tmp_path, max_request_bytes=32))) as client:
        response = client.post(
            "/api/v1/geometries/validate",
            content=body,
            headers={"Content-Type": "application/json", "Content-Length": "1"},
        )

    assert len(body) > 32
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_create_analysis_is_idempotent_and_persists_private_input(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    request = {
        "establishment_id": "establecimiento-2",
        "geometry": _feature(),
        "declared_land_use": "unknown",
        "declared_context_source": "user_declared",
    }
    headers = {"Idempotency-Key": "44444444-4444-4444-8444-444444444444"}

    with TestClient(create_app(settings)) as client:
        first = client.post("/api/v1/analyses", headers=headers, json=request)
        repeated = client.post("/api/v1/analyses", headers=headers, json=request)
        status = client.get(first.json()["status_url"])

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert repeated.json()["analysis_id"] == first.json()["analysis_id"]
    UUID(first.json()["analysis_id"])
    assert first.json()["status"] == "queued"
    assert status.status_code == 200
    assert status.json()["status"] == "queued"

    inputs = list(settings.storage_root.glob("analyses/*/input.geojson"))
    assert len(inputs) == 1
    persisted = json.loads(inputs[0].read_text(encoding="utf-8"))
    assert persisted["type"] == "Feature"
    assert str(settings.storage_root) not in first.text


def test_idempotency_key_cannot_be_reused_for_another_payload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    headers = {"Idempotency-Key": "55555555-5555-4555-8555-555555555555"}
    request = {
        "establishment_id": "establecimiento-2",
        "geometry": _feature(),
        "declared_land_use": "unknown",
        "declared_context_source": "user_declared",
    }

    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/analyses", headers=headers, json=request).status_code == 202
        request["establishment_id"] = "otro-establecimiento"
        conflict = client.post("/api/v1/analyses", headers=headers, json=request)

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_unknown_analysis_returns_stable_not_found_error(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.get("/api/v1/analyses/66666666-6666-4666-8666-666666666666")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "analysis_not_found"
