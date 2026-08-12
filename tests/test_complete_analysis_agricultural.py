from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import pytest

from deforestation_pipeline.agricultural_collector import RasterProvider
from deforestation_pipeline.agricultural_evidence import AgriculturalEvidencePolicy
from deforestation_pipeline.complete_analysis import (
    CompleteAnalysisError,
    CompleteAnalysisRequest,
    run_complete_analysis,
)
from deforestation_pipeline.gee import GeeSession

CREATED = datetime(2026, 8, 11, tzinfo=UTC)


def _inputs(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "vector": tmp_path / "field.geojson",
        "config": tmp_path / "default.yml",
        "model": tmp_path / "model.yml",
        "hampel": tmp_path / "hampel.yml",
        "collector": tmp_path / "collector.yml",
        "persistence": tmp_path / "persistence.yml",
        "policy": tmp_path / "policy.yml",
        "catalog": tmp_path / "catalog.yml",
        "licenses": tmp_path / "licenses.yml",
        "credentials": tmp_path / "credentials.json",
    }
    for path in paths.values():
        path.write_text("{}\n", encoding="utf-8")
    return paths


def _publish_child(root: Path, name: str) -> Path:
    bundle = root / name
    selected_figure_paths = {
        "full": "figures/seasonal/qa/rgb_timeline.png",
        "hampel": "figures/evidence/seasonal_hampel_benchmark.png",
        "rf": "figures/evidence/rf_forest_deltas_2020_2024.png",
        "collection": "figures/evidence/agricultural_monthly_evidence_pde-test.png",
        "persistence": "figures/evidence/agricultural_persistence_pde-test.png",
        "attribution": "figures/evidence/post_change_attribution.png",
    }
    artifact = bundle / "json/run/summary.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")
    artifacts = [
        {
            "path": "json/run/summary.json",
            "size_bytes": artifact.stat().st_size,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    ]
    selected_relative = selected_figure_paths.get(name)
    if selected_relative is not None:
        selected = bundle / selected_relative
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_bytes(f"figure:{name}".encode())
        artifacts.append(
            {
                "path": selected_relative,
                "size_bytes": selected.stat().st_size,
                "sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
            }
        )
    manifest = bundle / "json/run/manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "test",
                "analysis_id": str(uuid5(NAMESPACE_URL, name)),
                "created_at": CREATED.isoformat(),
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return bundle


def _request(paths: dict[str, Path], output: Path) -> CompleteAnalysisRequest:
    return CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=output,
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        agricultural_collector_config_path=paths["collector"],
        agricultural_persistence_config_path=paths["persistence"],
        agricultural_evidence_policy_path=paths["policy"],
        catalog_path=paths["catalog"],
        licenses_path=paths["licenses"],
        credentials_path=paths["credentials"],
        establishment_id="field",
        analysis_end_date=date(2025, 12, 31),
    )


def test_auto_mode_runs_six_components_and_forwards_only_same_run_bundles(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

    def runner(name: str) -> Any:
        def call(**kwargs: Any) -> Path:
            calls.append((name, kwargs))
            return _publish_child(Path(kwargs["output_root"]), name)

        return call

    output = run_complete_analysis(
        _request(paths, tmp_path / "runs"),
        created_at=CREATED,
        pipeline_runner=runner("full"),
        hampel_runner=runner("hampel"),
        delta_runner=runner("rf"),
        agricultural_collector_runner=runner("collection"),
        agricultural_persistence_runner=runner("persistence"),
        attribution_runner=runner("attribution"),
        raster_provider=cast(
            RasterProvider,
            lambda _: pytest.fail("fake collector must not query provider"),
        ),
        gee_authenticator=lambda _: GeeSession(module=object()),
        config_loader=lambda _: SimpleNamespace(output=object()),
        model_loader=lambda _: SimpleNamespace(
            supported_observation_years=(2020, 2021, 2022, 2023, 2024)
        ),
        hampel_loader=lambda _: object(),
        agricultural_policy_loader=lambda _: cast(AgriculturalEvidencePolicy, object()),
        agricultural_collector_loader=lambda _: object(),
        agricultural_persistence_loader=lambda _: object(),
    )

    assert [name for name, _ in calls] == [
        "full",
        "hampel",
        "rf",
        "collection",
        "persistence",
        "attribution",
    ]
    full = calls[0][1]["output_root"] / "full"
    rf = calls[2][1]["output_root"] / "rf"
    collection = calls[3][1]["output_root"] / "collection"
    persistence = calls[4][1]["output_root"] / "persistence"
    assert calls[3][1]["source_event_bundle"] == full
    assert calls[4][1]["source_collection_bundle"] == collection
    assert calls[5][1]["source_event_bundle"] == full
    assert calls[5][1]["source_rf_bundle"] == rf
    assert calls[5][1]["source_agricultural_bundle"] == persistence
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["component_order"] == [
        "full_pipeline",
        "hampel_benchmark",
        "rf_annual_deltas",
        "agricultural_collection",
        "agricultural_persistence",
        "post_change_attribution",
    ]
    assert manifest["dependencies"]["agricultural_collection"] == ["full_pipeline"]
    assert manifest["dependencies"]["agricultural_persistence"] == ["agricultural_collection"]
    assert manifest["dependencies"]["post_change_attribution"] == [
        "full_pipeline",
        "rf_annual_deltas",
        "agricultural_persistence",
    ]
    assert manifest["coverage"]["full_pipeline"] == {
        "requested_years": [2020, 2025],
        "requested_analysis_end_date": "2025-12-31",
        "effective_analysis_end_date": "2025-11-30",
    }
    assert manifest["coverage"]["rf_annual_deltas"] == {
        "requested_years": [2020, 2024],
        "effective_analysis_end_date": "2024-11-30",
    }
    assert manifest["coverage"]["agricultural_collection"]["analysis_end_date_inclusive"] == (
        "2025-12-31"
    )
    assert all(not Path(item["output_bundle"]).is_absolute() for item in manifest["components"])
    assert manifest["schema_version"] == "2.2.0"
    assert manifest["report_assets"]["status"] == "completed"
    assert manifest["report_assets"]["figure_count"] == 6
    report_index_path = output / manifest["report_assets"]["index_path"]
    assert report_index_path.is_file()
    assert (
        hashlib.sha256(report_index_path.read_bytes()).hexdigest()
        == manifest["report_assets"]["index_sha256"]
    )
    serialized = json.dumps(manifest)
    assert str(tmp_path.resolve()) not in serialized
    assert "credentials.json" not in serialized


def test_authenticates_once_and_reuses_session_for_hls_rf_and_dynamic_world(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path)
    session = GeeSession(module=object())
    authenticated: list[Path] = []
    factory_sessions: list[object] = []
    pipeline_sessions: list[object] = []

    def provider(_: object) -> Any:
        return None

    def runner(**kwargs: Any) -> Path:
        pipeline_sessions.append(kwargs["gee_session"])
        return _publish_child(Path(kwargs["output_root"]), str(len(pipeline_sessions)))

    def provider_factory(**kwargs: Any) -> Any:
        factory_sessions.append(kwargs["session"])
        return provider

    def authenticate(path: Path) -> GeeSession:
        authenticated.append(path)
        return session

    run_complete_analysis(
        _request(paths, tmp_path / "runs"),
        created_at=CREATED,
        pipeline_runner=runner,
        hampel_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "hampel"),
        delta_runner=runner,
        agricultural_collector_runner=lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]),
            "collection" if kwargs["raster_provider"] is provider else "wrong-provider",
        ),
        agricultural_persistence_runner=lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "persistence"
        ),
        attribution_runner=lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "attribution"
        ),
        gee_authenticator=authenticate,
        agricultural_provider_factory=provider_factory,
        config_loader=lambda _: SimpleNamespace(output=object()),
        model_loader=lambda _: SimpleNamespace(
            supported_observation_years=(2020, 2021, 2022, 2023, 2024)
        ),
        hampel_loader=lambda _: object(),
        agricultural_policy_loader=lambda _: cast(AgriculturalEvidencePolicy, object()),
        agricultural_collector_loader=lambda _: object(),
        agricultural_persistence_loader=lambda _: object(),
    )
    assert authenticated == [paths["credentials"].resolve()]
    assert pipeline_sessions == [session, session]
    assert factory_sessions == [session]


def test_user_oauth_authenticates_once_and_reuses_session_for_every_remote_component(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path)
    session = GeeSession(module=object())
    authenticated: list[str] = []
    pipeline_sessions: list[object] = []
    request = _request(paths, tmp_path / "runs")
    request = replace(request, credentials_path=None, gee_project="versioned-ee-project")

    def runner(**kwargs: Any) -> Path:
        pipeline_sessions.append(kwargs["gee_session"])
        return _publish_child(Path(kwargs["output_root"]), str(len(pipeline_sessions)))

    def authenticate_oauth(*, project: str) -> GeeSession:
        authenticated.append(project)
        return session

    run_complete_analysis(
        request,
        created_at=CREATED,
        pipeline_runner=runner,
        hampel_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "hampel"),
        delta_runner=runner,
        agricultural_collector_runner=lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "collection"
        ),
        agricultural_persistence_runner=lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "persistence"
        ),
        attribution_runner=lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "attribution"
        ),
        gee_oauth_authenticator=authenticate_oauth,
        agricultural_provider_factory=lambda **kwargs: cast(RasterProvider, lambda _: None),
        config_loader=lambda _: SimpleNamespace(output=object()),
        model_loader=lambda _: SimpleNamespace(
            supported_observation_years=(2020, 2021, 2022, 2023, 2024)
        ),
        hampel_loader=lambda _: object(),
        agricultural_policy_loader=lambda _: cast(AgriculturalEvidencePolicy, object()),
        agricultural_collector_loader=lambda _: object(),
        agricultural_persistence_loader=lambda _: object(),
    )

    assert authenticated == ["versioned-ee-project"]
    assert pipeline_sessions == [session, session]


def test_authentication_failure_publishes_sanitized_failed_parent(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path)

    def fail_authentication(_: Path) -> GeeSession:
        raise RuntimeError("credential_token=do-not-persist")

    with pytest.raises(CompleteAnalysisError) as captured:
        run_complete_analysis(
            _request(paths, tmp_path / "runs"),
            created_at=CREATED,
            gee_authenticator=fail_authentication,
            config_loader=lambda _: SimpleNamespace(output=object()),
            model_loader=lambda _: SimpleNamespace(
                supported_observation_years=(2020, 2021, 2022, 2023, 2024)
            ),
            hampel_loader=lambda _: object(),
            agricultural_policy_loader=lambda _: cast(AgriculturalEvidencePolicy, object()),
            agricultural_collector_loader=lambda _: object(),
            agricultural_persistence_loader=lambda _: object(),
        )

    assert captured.value.failure_path.name.endswith(".failed")
    serialized = (captured.value.failure_path / "run_manifest.json").read_text("utf-8")
    assert "do-not-persist" not in serialized
    assert "[REDACTED]" in serialized
    manifest = json.loads(serialized)
    assert manifest["overall_status"] == "failed"
    assert all(component["status"] == "skipped" for component in manifest["components"])


@pytest.mark.parametrize("failing", ["collection", "persistence", "attribution"])
def test_agricultural_failure_is_atomic_sanitized_and_skips_downstream(
    tmp_path: Path, failing: str
) -> None:
    paths = _inputs(tmp_path)
    calls: list[str] = []

    def runner(name: str) -> Any:
        def call(**kwargs: Any) -> Path:
            calls.append(name)
            if name == failing:
                raise RuntimeError("https://x.invalid?a=1&token=secret-value")
            return _publish_child(Path(kwargs["output_root"]), name)

        return call

    with pytest.raises(CompleteAnalysisError) as captured:
        run_complete_analysis(
            _request(paths, tmp_path / "runs"),
            created_at=CREATED,
            pipeline_runner=runner("full"),
            hampel_runner=runner("hampel"),
            delta_runner=runner("rf"),
            agricultural_collector_runner=runner("collection"),
            agricultural_persistence_runner=runner("persistence"),
            attribution_runner=runner("attribution"),
            raster_provider=cast(RasterProvider, lambda _: None),
            gee_authenticator=lambda _: GeeSession(module=object()),
            config_loader=lambda _: SimpleNamespace(output=object()),
            model_loader=lambda _: SimpleNamespace(
                supported_observation_years=(2020, 2021, 2022, 2023, 2024)
            ),
            hampel_loader=lambda _: object(),
            agricultural_policy_loader=lambda _: cast(AgriculturalEvidencePolicy, object()),
            agricultural_collector_loader=lambda _: object(),
            agricultural_persistence_loader=lambda _: object(),
        )
    expected = {
        "collection": ["full", "hampel", "rf", "collection"],
        "persistence": ["full", "hampel", "rf", "collection", "persistence"],
        "attribution": [
            "full",
            "hampel",
            "rf",
            "collection",
            "persistence",
            "attribution",
        ],
    }
    assert calls == expected[failing]
    assert captured.value.failure_path.name.endswith(".failed")
    assert not list(captured.value.failure_path.parent.glob(".*.staging"))
    serialized = (captured.value.failure_path / "run_manifest.json").read_text("utf-8")
    assert "secret-value" not in serialized
    assert "[REDACTED]" in serialized
    manifest = json.loads(serialized)
    persistence_was_materialized = failing == "attribution"
    assert (
        manifest["scientific_flags"]["persistent_agricultural_evidence_provided"]
        is persistence_was_materialized
    )
    assert (
        manifest["scientific_flags"]["agricultural_evidence_generated_in_same_run"]
        is persistence_was_materialized
    )
    noncompleted = [item for item in manifest["components"] if item["status"] != "completed"]
    assert noncompleted
    expected_figure_count = {"collection": 3, "persistence": 4, "attribution": 5}
    assert manifest["report_assets"]["status"] == "completed"
    assert manifest["report_assets"]["figure_count"] == expected_figure_count[failing]
    report_index = json.loads(
        (captured.value.failure_path / manifest["report_assets"]["index_path"]).read_text("utf-8")
    )
    assert report_index["parent_run_status"] == "partial"
    for component in noncompleted:
        receipt_path = captured.value.failure_path / component["status_receipt"]
        assert receipt_path.is_file()
        assert (
            hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            == component["status_receipt_sha256"]
        )
        receipt = json.loads(receipt_path.read_text("utf-8"))
        assert receipt["status"] == component["status"]
        assert receipt["dependencies"] == manifest["dependencies"].get(component["name"], [])
        assert "secret-value" not in json.dumps(receipt)
        if component["status"] == "skipped":
            assert receipt["error"]["code"] == "dependency_not_completed"
