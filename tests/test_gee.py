"""Pruebas de la frontera remota GEE sin realizar conexiones reales."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import Point

from deforestation_pipeline.catalog import (
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.gee import (
    GeeAuthenticationError,
    GeeMetadataQuery,
    GeeQueryError,
    GeeSession,
    authenticate_earth_engine,
    authenticate_earth_engine_user_oauth,
    query_hls_scene_metadata,
)
from deforestation_pipeline.geometry import validate_geometry
from deforestation_pipeline.schemas import GeoJSONGeometry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.yml"
CONFIG_PATH = PROJECT_ROOT / "configs" / "default.yml"


class _InfoValue:
    def __init__(self, value: object) -> None:
        self.value = value

    def getInfo(self) -> object:
        return self.value


class _FakeCollection:
    def __init__(self, source_id: str, records: list[dict[str, object]]) -> None:
        self.source_id = source_id
        self.records = records
        self.date_filter: tuple[str, str] | None = None
        self.limit_value: int | None = None
        self.bounds_applied = False
        self.property_filter: tuple[str, tuple[str, ...]] | None = None

    def filterBounds(self, geometry: object) -> _FakeCollection:
        self.bounds_applied = geometry is not None
        return self

    def filterDate(self, start: str, end: str) -> _FakeCollection:
        self.date_filter = (start, end)
        return self

    def sort(self, property_name: str) -> _FakeCollection:
        assert property_name == "system:time_start"
        return self

    def filter(
        self,
        filter_expression: tuple[str, tuple[str, ...]],
    ) -> _FakeCollection:
        property_name, accepted_values = filter_expression
        self.property_filter = filter_expression
        accepted = set(accepted_values)
        self.records = [record for record in self.records if record.get(property_name) in accepted]
        return self

    def size(self) -> _InfoValue:
        return _InfoValue(len(self.records))

    def limit(self, value: int) -> _FakeCollection:
        self.limit_value = value
        return self

    def aggregate_array(self, property_name: str) -> _InfoValue:
        records = self.records[: self.limit_value]
        return _InfoValue([record[property_name] for record in records])


class _FakeEarthEngine:
    class Filter:
        @staticmethod
        def inList(
            property_name: str,
            values: list[str],
        ) -> tuple[str, tuple[str, ...]]:
            return property_name, tuple(values)

    def __init__(self, records: dict[str, list[dict[str, object]]]) -> None:
        self.records = records
        self.collections: dict[str, _FakeCollection] = {}
        self.geometry_payload: object | None = None
        self.initialize_project: str | None = None
        self.credentials_arguments: tuple[str, str] | None = None

    def ServiceAccountCredentials(self, email: str, key_path: str) -> object:
        self.credentials_arguments = (email, key_path)
        return object()

    def Initialize(self, credentials: object, *, project: str) -> None:
        assert credentials is not None
        self.initialize_project = project

    def Geometry(self, payload: object) -> object:
        self.geometry_payload = payload
        return object()

    def ImageCollection(self, collection_id: str) -> _FakeCollection:
        collection = _FakeCollection(collection_id, self.records[collection_id])
        self.collections[collection_id] = collection
        return collection


class _FailingInitializationEarthEngine(_FakeEarthEngine):
    def __init__(self, message: str) -> None:
        super().__init__({})
        self.message = message

    def Initialize(self, credentials: object, *, project: str) -> None:
        raise RuntimeError(f"{self.message}: {project}")


class _FailingCollectionEarthEngine(_FakeEarthEngine):
    def ImageCollection(self, collection_id: str) -> _FakeCollection:
        raise RuntimeError(f"permission denied for SECRET-{collection_id}")


class _ReferenceTileFallbackEarthEngine(_FakeEarthEngine):
    def __init__(self) -> None:
        super().__init__(
            {
                "NASA/HLS/HLSL30/v002": [],
                "NASA/HLS/HLSS30/v002": [
                    {
                        "system:index": "T20HNJ_S30-A",
                        "system:time_start": 1704067200000,
                        "CLOUD_COVERAGE": 5.0,
                        "MGRS_TILE_ID": "20HNJ",
                    }
                ],
            }
        )
        self.l30_collection_calls = 0

    def ImageCollection(self, collection_id: str) -> _FakeCollection:
        if collection_id == "NASA/HLS/HLSL30/v002":
            self.l30_collection_calls += 1
            records = (
                []
                if self.l30_collection_calls == 1
                else [
                    {
                        "system:index": "T20HNJ_REFERENCE",
                        "system:time_start": 1672531200000,
                        "CLOUD_COVERAGE": 10.0,
                    }
                ]
            )
            collection = _FakeCollection(collection_id, records)
            self.collections[f"{collection_id}:{self.l30_collection_calls}"] = collection
            return collection
        return super().ImageCollection(collection_id)


def _write_service_account(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "dummy-project",
                "client_email": "dummy@example.invalid",
                "private_key": "DUMMY-PRIVATE-KEY",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        encoding="utf-8",
    )


def _plan() -> Any:
    return build_benchmark_source_plan(
        load_source_catalog(CATALOG_PATH),
        load_config(CONFIG_PATH),
    )


def _validated_aoi() -> Any:
    geometry = GeoJSONGeometry(
        type="Polygon",
        coordinates=[
            [
                [-63.0, -33.0],
                [-62.99, -33.0],
                [-62.99, -32.99],
                [-63.0, -32.99],
                [-63.0, -33.0],
            ]
        ],
    )
    return validate_geometry(geometry, "EPSG:4326").analysis_geometry


def test_service_account_authentication_never_exposes_identity(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    _write_service_account(credentials_path)
    fake_ee = _FakeEarthEngine({})

    session = authenticate_earth_engine(credentials_path, ee_module=fake_ee)

    assert fake_ee.initialize_project == "dummy-project"
    assert fake_ee.credentials_arguments == (
        "dummy@example.invalid",
        str(credentials_path),
    )
    assert session.initialized is True
    assert "dummy-project" not in repr(session)
    assert "dummy@example.invalid" not in repr(session)


def test_user_oauth_initializes_with_only_the_versioned_project() -> None:
    class FakeUserOAuthEarthEngine:
        def __init__(self) -> None:
            self.project: str | None = None

        def Initialize(self, *, project: str) -> None:
            self.project = project

    fake_ee = FakeUserOAuthEarthEngine()

    session = authenticate_earth_engine_user_oauth(
        project="ee-facuboladerasgee",
        ee_module=fake_ee,
    )

    assert fake_ee.project == "ee-facuboladerasgee"
    assert session.initialized is True
    assert "ee-facuboladerasgee" not in repr(session)


def test_authentication_failure_is_sanitized(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    _write_service_account(credentials_path)
    fake_ee = _FailingInitializationEarthEngine(
        "invalid_grant dummy@example.invalid DUMMY-PRIVATE-KEY"
    )

    with pytest.raises(GeeAuthenticationError) as captured:
        authenticate_earth_engine(credentials_path, ee_module=fake_ee)

    message = str(captured.value)
    assert "invalid_credentials" in message
    assert "dummy-project" not in message
    assert "dummy@example.invalid" not in message
    assert "DUMMY-PRIVATE-KEY" not in message


@pytest.mark.parametrize(
    ("remote_message", "expected_code"),
    [
        ("project not registered", "project_not_registered"),
        ("permission denied", "permission_denied"),
        ("Earth Engine API has not been used or is disabled", "earth_engine_api_disabled"),
        ("unexpected response", "initialization_failed"),
    ],
)
def test_authentication_errors_are_classified_without_remote_text(
    tmp_path: Path,
    remote_message: str,
    expected_code: str,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    _write_service_account(credentials_path)

    with pytest.raises(GeeAuthenticationError, match=expected_code):
        authenticate_earth_engine(
            credentials_path,
            ee_module=_FailingInitializationEarthEngine(remote_message),
        )


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ("not-json", "credential_file_unreadable"),
        ("[]", "unsupported_credential_shape"),
        (
            json.dumps({"type": "service_account", "project_id": "dummy"}),
            "incomplete_service_account",
        ),
    ],
)
def test_invalid_credential_files_return_only_sanitized_codes(
    tmp_path: Path,
    content: str,
    expected_code: str,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(content, encoding="utf-8")

    with pytest.raises(GeeAuthenticationError, match=expected_code):
        authenticate_earth_engine(credentials_path, ee_module=_FakeEarthEngine({}))


def test_metadata_query_is_bounded_and_returns_sanitized_scene_records(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    _write_service_account(credentials_path)
    records = {
        "NASA/HLS/HLSL30/v002": [
            {
                "system:index": "T20HNJ_L30-A",
                "system:time_start": 1609459200000,
                "CLOUD_COVERAGE": 10.0,
            },
            {
                "system:index": "T20HNJ_L30-B",
                "system:time_start": 1609545600000,
                "CLOUD_COVERAGE": 20.0,
            },
            {
                "system:index": "T20HNJ_L30-C",
                "system:time_start": 1609632000000,
                "CLOUD_COVERAGE": 30.0,
            },
        ],
        "NASA/HLS/HLSS30/v002": [
            {
                "system:index": "T20HNJ_S30-A",
                "system:time_start": 1609459200000,
                "CLOUD_COVERAGE": 5.0,
                "MGRS_TILE_ID": "20HNJ",
            },
            {
                "system:index": "T01FBF_S30-IRRELEVANT",
                "system:time_start": 1609459200000,
                "CLOUD_COVERAGE": 1.0,
                "MGRS_TILE_ID": "01FBF",
            },
        ],
    }
    fake_ee = _FakeEarthEngine(records)
    session = authenticate_earth_engine(credentials_path, ee_module=fake_ee)

    result = query_hls_scene_metadata(
        session=session,
        plan=_plan(),
        aoi_wgs84=_validated_aoi(),
        query=GeeMetadataQuery(
            start_date=date(2021, 1, 1),
            end_date=date(2021, 1, 10),
            max_scenes_per_source=2,
        ),
        queried_at=datetime(2026, 7, 24, 12, tzinfo=UTC),
    )

    assert result.remote_data_accessed is True
    assert result.metadata_only is True
    assert result.aoi_sha256
    assert len(result.collections) == 2
    assert result.collections[0].matched_scene_count == 3
    assert result.collections[0].returned_scene_count == 2
    assert result.collections[0].truncated is True
    assert tuple(scene.image_id for scene in result.collections[0].scenes) == (
        "T20HNJ_L30-A",
        "T20HNJ_L30-B",
    )
    assert result.collections[1].truncated is False
    assert result.collections[1].matched_scene_count == 1
    assert result.mgrs_tile_ids == ("20HNJ",)
    assert result.spatial_filter_strategy == "l30_bounds_then_shared_mgrs_tiles"
    assert fake_ee.collections["NASA/HLS/HLSL30/v002"].date_filter == (
        "2021-01-01",
        "2021-01-10",
    )
    assert fake_ee.collections["NASA/HLS/HLSL30/v002"].limit_value == 2
    assert fake_ee.collections["NASA/HLS/HLSS30/v002"].property_filter == (
        "MGRS_TILE_ID",
        ("20HNJ",),
    )
    assert "coordinates" not in result.model_dump(mode="json")


def test_remote_query_failure_is_sanitized(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    _write_service_account(credentials_path)
    fake_ee = _FailingCollectionEarthEngine({})
    session = authenticate_earth_engine(credentials_path, ee_module=fake_ee)

    with pytest.raises(GeeQueryError) as captured:
        query_hls_scene_metadata(
            session=session,
            plan=_plan(),
            aoi_wgs84=_validated_aoi(),
            query=GeeMetadataQuery(
                start_date=date(2021, 1, 1),
                end_date=date(2021, 1, 2),
                max_scenes_per_source=1,
            ),
        )

    assert "remote_query_failed:permission_denied" in str(captured.value)
    assert "SECRET-" not in str(captured.value)


def test_metadata_query_resolves_s30_tile_from_l30_reference_year() -> None:
    fake_ee = _ReferenceTileFallbackEarthEngine()

    result = query_hls_scene_metadata(
        session=GeeSession(module=fake_ee),
        plan=_plan(),
        aoi_wgs84=_validated_aoi(),
        query=GeeMetadataQuery(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
            max_scenes_per_source=2,
        ),
        queried_at=datetime(2026, 7, 24, 12, tzinfo=UTC),
    )

    assert fake_ee.l30_collection_calls == 2
    assert result.mgrs_tile_ids == ("20HNJ",)
    assert result.collections[0].matched_scene_count == 0
    assert result.collections[1].matched_scene_count == 1


def test_query_rejects_invalid_aoi_dates_limit_and_timestamp_before_remote_calls() -> None:
    with pytest.raises(GeeQueryError) as captured:
        query_hls_scene_metadata(
            session=GeeSession(module=object()),
            plan=_plan(),
            aoi_wgs84=Point(-63, -33),
            query=GeeMetadataQuery(
                start_date=date(2021, 1, 2),
                end_date=date(2021, 1, 1),
                max_scenes_per_source=0,
            ),
            queried_at=datetime(2026, 7, 24),
        )

    violations = captured.value.violations
    assert any("end_date" in violation for violation in violations)
    assert any("max_scenes_per_source" in violation for violation in violations)
    assert any("Polygon" in violation for violation in violations)
    assert any("zona horaria" in violation for violation in violations)


@pytest.mark.parametrize(
    "query",
    [
        GeeMetadataQuery(
            start_date=date(2021, 1, 1),
            end_date=date(2022, 1, 3),
            max_scenes_per_source=10,
        ),
        GeeMetadataQuery(
            start_date=date(2021, 1, 1),
            end_date=date(2021, 1, 2),
            max_scenes_per_source=51,
        ),
    ],
)
def test_query_limits_are_enforced_before_remote_calls(query: GeeMetadataQuery) -> None:
    with pytest.raises(GeeQueryError):
        query_hls_scene_metadata(
            session=GeeSession(module=object()),
            plan=_plan(),
            aoi_wgs84=_validated_aoi(),
            query=query,
        )
