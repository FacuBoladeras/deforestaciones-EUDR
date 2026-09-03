from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

import deforestation_pipeline.complete_analysis as complete_module
from deforestation_pipeline.agricultural_evidence import (
    AgriculturalEvidenceDocument,
    AgriculturalEvidenceDocumentV2,
    AgriculturalEvidencePolicy,
)
from deforestation_pipeline.complete_analysis import (
    PROJECT_ROOT,
    CompleteAnalysisError,
    CompleteAnalysisRequest,
    _code_provenance,
    _parameters_hash,
    _sanitize,
    run_complete_analysis,
)
from deforestation_pipeline.disturbance_candidate_fusion_materialization import (
    CandidateFusionUnavailableError,
)
from deforestation_pipeline.hampel_benchmark import HampelDiagnosticUnavailableError

CREATED = datetime(2026, 8, 6, tzinfo=UTC)
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_complete_analysis.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("run_complete_analysis_script", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
SCRIPT_MODULE = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(SCRIPT_MODULE)


def _inputs(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "vector": tmp_path / "field.geojson",
        "config": tmp_path / "default.yml",
        "model": tmp_path / "model.yml",
        "hampel": tmp_path / "hampel.yml",
    }
    paths["vector"].write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    paths["config"].write_text("schema_version: '1'\n", encoding="utf-8")
    paths["model"].write_text(
        "supported_observation_years: [2020, 2021, 2022, 2023, 2024]\n", encoding="utf-8"
    )
    paths["hampel"].write_text("window_size: 5\n", encoding="utf-8")
    return paths


def _publish_child(root: Path, name: str) -> Path:
    bundle = root / name
    artifact = bundle / "json" / "run" / "summary.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")
    manifest = bundle / "json" / "run" / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "test",
                "analysis_id": str(uuid5(NAMESPACE_URL, name)),
                "created_at": CREATED.isoformat(),
                "artifacts": [
                    {
                        "path": "json/run/summary.json",
                        "size_bytes": artifact.stat().st_size,
                        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return bundle


def _loaders() -> dict[str, Any]:
    return {
        "config_loader": lambda _: SimpleNamespace(output=object()),
        "model_loader": lambda _: SimpleNamespace(
            supported_observation_years=(2020, 2021, 2022, 2023, 2024)
        ),
        "hampel_loader": lambda _: object(),
        "agricultural_collector_loader": lambda _: object(),
        "agricultural_persistence_loader": lambda _: object(),
        "agricultural_persistence_bundle_validator": lambda _: object(),
        "raster_provider": lambda _: None,
        "agricultural_collector_runner": lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "agricultural-collection-child"
        ),
        "agricultural_persistence_runner": lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "agricultural-persistence-child"
        ),
        "candidate_fusion_runner": lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "candidate-fusion-child"
        ),
        "attribution_runner": lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "attribution-child"
        ),
    }


def test_runs_components_in_order_and_publishes_relative_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _inputs(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []
    rename_calls: list[tuple[Path, Path]] = []

    def retrying_rename(source: Path, target: Path) -> None:
        rename_calls.append((source, target))
        source.rename(target)

    monkeypatch.setattr(complete_module, "_rename_directory_with_retry", retrying_rename)

    def full(**kwargs: Any) -> Path:
        calls.append(("full", kwargs))
        return _publish_child(Path(kwargs["output_root"]), "full-child")

    def hampel(**kwargs: Any) -> Path:
        calls.append(("hampel", kwargs))
        assert kwargs["source_bundle"] == calls[0][1]["output_root"] / "full-child"
        return _publish_child(Path(kwargs["output_root"]), "hampel-child")

    def deltas(**kwargs: Any) -> Path:
        calls.append(("deltas", kwargs))
        return _publish_child(Path(kwargs["output_root"]), "delta-child")

    def fusion(**kwargs: Any) -> Path:
        calls.append(("fusion", kwargs))
        assert kwargs["source_event_bundle"] == calls[0][1]["output_root"] / "full-child"
        assert kwargs["source_rf_bundle"] == calls[2][1]["output_root"] / "delta-child"
        return _publish_child(Path(kwargs["output_root"]), "fusion-child")

    def attribution(**kwargs: Any) -> Path:
        calls.append(("attribution", kwargs))
        assert kwargs["source_event_bundle"] == calls[3][1]["output_root"] / "fusion-child"
        assert kwargs["source_rf_bundle"] == calls[2][1]["output_root"] / "delta-child"
        return _publish_child(Path(kwargs["output_root"]), "attribution-child")

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="Field ../ A",
        analysis_end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
        hls_start_year=2020,
        hls_end_year=2025,
        source_crs="EPSG:4326",
        vector_layer="plots",
        dissolve_all=True,
    )
    output = run_complete_analysis(
        request,
        created_at=CREATED,
        pipeline_runner=full,
        hampel_runner=hampel,
        delta_runner=deltas,
        candidate_fusion_runner=fusion,
        attribution_runner=attribution,
        **{
            key: value
            for key, value in _loaders().items()
            if key not in {"attribution_runner", "candidate_fusion_runner"}
        },
    )

    assert [name for name, _ in calls] == ["full", "hampel", "deltas", "fusion", "attribution"]
    assert output.parent == (tmp_path / "runs").resolve()
    assert ".." not in output.name and "field-a" in output.name
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["overall_status"] == "complete"
    assert manifest["scientific_flags"]["candidate_fusion_applied"] is True
    assert manifest["scientific_flags"]["candidate_fusion_fallback_to_historical_events"] is False
    assert manifest["scientific_flags"]["rf_first_candidate_domain_applied"] is True
    assert manifest["scientific_flags"]["candidate_domain_semantics"] == (
        "rf_terminal_persistent_forest_loss"
    )
    assert [item["status"] for item in manifest["components"]] == ["completed"] * 7
    assert all(not Path(item["output_bundle"]).is_absolute() for item in manifest["components"])
    assert manifest["coverage"]["rf_annual_deltas"]["requested_years"] == [2020, 2024]
    assert calls[0][1]["created_at"] == calls[1][1]["created_at"] == calls[2][1]["created_at"]
    assert calls[2][1]["forest_model_config_path"] == paths["model"].resolve()
    for runner_call in (calls[0][1], calls[2][1]):
        assert runner_call["source_crs"] == "EPSG:4326"
        assert runner_call["vector_layer"] == "plots"
        assert runner_call["dissolve_all"] is True
    assert manifest["input"]["scope"] == "external"
    assert "path" not in manifest["input"]
    assert str(tmp_path.resolve()) not in json.dumps(manifest)
    assert all(config["size_bytes"] > 0 for config in manifest["configs"].values())
    assert all(item["child_analysis_id"] for item in manifest["components"])
    assert all(
        item["child_analysis_id"] != manifest["analysis_id"] for item in manifest["components"]
    )
    assert len({item["child_analysis_id"] for item in manifest["components"]}) == 7
    assert all(item["child_created_at"] == CREATED.isoformat() for item in manifest["components"])
    assert len(manifest["parameters_hash"]) == 64
    assert manifest["parameters_hash"] == _parameters_hash(manifest)
    assert rename_calls == [(rename_calls[0][0], output)]
    assert rename_calls[0][0].name.endswith(".staging")


def test_expected_hampel_unavailability_continues_as_partial_run(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    calls: list[str] = []

    def runner(name: str) -> Any:
        def call(**kwargs: Any) -> Path:
            calls.append(name)
            if name == "hampel":
                raise HampelDiagnosticUnavailableError("hampel_insufficient_strict_stable_controls")
            return _publish_child(Path(kwargs["output_root"]), name)

        return call

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    output = run_complete_analysis(
        request,
        created_at=CREATED,
        pipeline_runner=runner("full"),
        hampel_runner=runner("hampel"),
        delta_runner=runner("deltas"),
        attribution_runner=runner("attribution"),
        **{key: value for key, value in _loaders().items() if key != "attribution_runner"},
    )

    assert calls == ["full", "hampel", "deltas", "attribution"]
    assert not output.name.endswith(".failed")
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["overall_status"] == "partial"
    hampel = manifest["components"][1]
    assert hampel["required_for_publication"] is False
    assert hampel["status"] == "diagnostic_unavailable"
    assert hampel["error"] == {
        "error_type": "HampelDiagnosticUnavailableError",
        "message": "hampel_insufficient_strict_stable_controls",
    }
    assert manifest["scientific_warnings"] == [
        {
            "component": "hampel_benchmark",
            "code": "hampel_insufficient_strict_stable_controls",
        }
    ]
    assert manifest["failure"] is None
    assert manifest["dependencies"]["rf_annual_deltas"] == ["full_pipeline"]
    receipt_path = output / hampel["status_receipt"]
    assert receipt_path.is_file()
    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == hampel["status_receipt_sha256"]
    receipt = json.loads(receipt_path.read_text("utf-8"))
    assert receipt == {
        "schema_version": "1.0.0",
        "component": "hampel_benchmark",
        "status": "diagnostic_unavailable",
        "required_for_publication": False,
        "started_at": CREATED.isoformat(),
        "completed_at": hampel["completed_at"],
        "recorded_at": receipt["recorded_at"],
        "dependencies": ["full_pipeline"],
        "error": {
            "error_type": "HampelDiagnosticUnavailableError",
            "code": "hampel_insufficient_strict_stable_controls",
        },
    }
    completed = [item for item in manifest["components"] if item["status"] == "completed"]
    assert len({item["child_analysis_id"] for item in completed}) == 6


def test_historical_bundle_without_raw_robust_state_uses_explicit_fusion_fallback(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path)
    captured: list[Path] = []

    def unavailable(**_: Any) -> Path:
        raise CandidateFusionUnavailableError("candidate_fusion_raw_robust_state_unavailable")

    def attribution(**kwargs: Any) -> Path:
        captured.append(Path(kwargs["source_event_bundle"]))
        return _publish_child(Path(kwargs["output_root"]), "attribution-fallback")

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    output = run_complete_analysis(
        request,
        created_at=CREATED,
        pipeline_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "full"),
        hampel_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "hampel"),
        delta_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "deltas"),
        candidate_fusion_runner=unavailable,
        attribution_runner=attribution,
        **{
            key: value
            for key, value in _loaders().items()
            if key not in {"attribution_runner", "candidate_fusion_runner"}
        },
    )

    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    fusion = next(
        item for item in manifest["components"] if item["name"] == "disturbance_candidate_fusion"
    )
    assert fusion["status"] == "diagnostic_unavailable"
    assert manifest["scientific_flags"]["candidate_fusion_fallback_to_historical_events"] is True
    assert (
        manifest["scientific_flags"]["rf_first_candidate_domain_fallback_to_historical_events"]
        is True
    )
    assert manifest["scientific_warnings"][-1] == {
        "component": "disturbance_candidate_fusion",
        "code": "candidate_fusion_raw_robust_state_unavailable",
    }
    assert captured[0].name == "full"


def test_validated_agricultural_evidence_is_forwarded_to_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _inputs(tmp_path)
    evidence_path = tmp_path / "agricultural-evidence.json"
    evidence_path.write_text('{"schema_version":"1.0.0"}', encoding="utf-8")
    loaded_document = object()
    loaded_policy = object()
    evaluated = {"PDE-1": (object(),)}
    monkeypatch.setattr(
        complete_module,
        "evaluate_agricultural_evidence",
        lambda document, policy: (
            evaluated
            if document is loaded_document and policy is loaded_policy
            else pytest.fail("se evaluaron contratos distintos de los precargados")
        ),
    )

    def attribution(**kwargs: Any) -> Path:
        assert kwargs["context"].agricultural_use_evidence == evaluated
        return _publish_child(Path(kwargs["output_root"]), "attribution")

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        agricultural_evidence_path=evidence_path,
        establishment_id="field",
    )
    output = run_complete_analysis(
        request,
        created_at=CREATED,
        pipeline_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "full"),
        hampel_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "hampel"),
        delta_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "deltas"),
        attribution_runner=attribution,
        agricultural_evidence_loader=lambda _: cast(AgriculturalEvidenceDocument, loaded_document),
        agricultural_policy_loader=lambda _: cast(AgriculturalEvidencePolicy, loaded_policy),
        **{key: value for key, value in _loaders().items() if key != "attribution_runner"},
    )

    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["scientific_flags"]["agricultural_evidence_policy_applied"] is True
    assert manifest["scientific_flags"]["agricultural_evidence_provided"] is True
    assert (
        manifest["agricultural_evidence_input"]["sha256"]
        == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    )


def test_v2_agricultural_evidence_loader_is_forwarded_without_legal_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _inputs(tmp_path)
    evidence_path = tmp_path / "agricultural-evidence-v2.json"
    evidence_path.write_text('{"schema_version":"2.0.0"}', encoding="utf-8")
    loaded_document = cast(AgriculturalEvidenceDocumentV2, object())
    loaded_policy = cast(AgriculturalEvidencePolicy, object())
    evaluated = {"PDE-1": (object(),)}
    monkeypatch.setattr(
        complete_module,
        "evaluate_agricultural_evidence",
        lambda document, policy: (
            evaluated
            if document is loaded_document and policy is loaded_policy
            else pytest.fail("v2 no llegó al evaluador")
        ),
    )

    def attribution(**kwargs: Any) -> Path:
        assert kwargs["context"].agricultural_use_evidence == evaluated
        return _publish_child(Path(kwargs["output_root"]), "attribution-v2")

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        agricultural_evidence_path=evidence_path,
        establishment_id="field",
    )
    output = run_complete_analysis(
        request,
        created_at=CREATED,
        pipeline_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "full"),
        hampel_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "hampel"),
        delta_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "deltas"),
        attribution_runner=attribution,
        agricultural_evidence_loader=lambda _: loaded_document,
        agricultural_policy_loader=lambda _: loaded_policy,
        **{key: value for key, value in _loaders().items() if key != "attribution_runner"},
    )

    manifest = json.loads((output / "run_manifest.json").read_text("utf-8"))
    assert manifest["scientific_flags"]["automatic_conversion_confirmed"] is False
    assert manifest["scientific_flags"]["agricultural_evidence_contract_version"] == "2.0.0"


def test_invalid_automatic_agricultural_component_is_failed_not_silently_skipped(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path)

    def reject_bundle(_: Path) -> dict[str, str]:
        raise ValueError("agricultural_persistence_document_missing")

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(CompleteAnalysisError) as error:
        run_complete_analysis(
            request,
            created_at=CREATED,
            pipeline_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "full"),
            hampel_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "hampel"),
            delta_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "deltas"),
            agricultural_persistence_bundle_validator=reject_bundle,
            **{
                key: value
                for key, value in _loaders().items()
                if key != "agricultural_persistence_bundle_validator"
            },
        )

    manifest = json.loads((error.value.failure_path / "run_manifest.json").read_text("utf-8"))
    persistence = next(
        item for item in manifest["components"] if item["name"] == "agricultural_persistence"
    )
    attribution = next(
        item for item in manifest["components"] if item["name"] == "post_change_attribution"
    )
    assert persistence["status"] == "failed"
    assert persistence["error"]["message"] == "component_validation_failed"
    assert attribution["status"] == "skipped"


def test_persistent_agricultural_bundle_is_forwarded_reversibly_to_attribution(
    tmp_path: Path,
) -> None:
    paths = _inputs(tmp_path)
    agriculture = tmp_path / "agriculture"
    agriculture_manifest = agriculture / "json/run/manifest.json"
    agriculture_manifest.parent.mkdir(parents=True)
    agriculture_manifest.write_text(
        json.dumps({"analysis_id": str(uuid5(NAMESPACE_URL, "agriculture")), "artifacts": []}),
        encoding="utf-8",
    )
    captured: list[dict[str, Any]] = []

    def attribution(**kwargs: Any) -> Path:
        captured.append(kwargs)
        return _publish_child(Path(kwargs["output_root"]), "attribution")

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        agricultural_persistence_bundle_path=agriculture,
        establishment_id="field",
    )
    output = run_complete_analysis(
        request,
        created_at=CREATED,
        pipeline_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "full"),
        hampel_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "hampel"),
        delta_runner=lambda **kwargs: _publish_child(Path(kwargs["output_root"]), "deltas"),
        attribution_runner=attribution,
        **{key: value for key, value in _loaders().items() if key != "attribution_runner"},
    )

    assert captured[0]["source_agricultural_bundle"] == agriculture.resolve()
    assert captured[0]["agricultural_policy"] is not None
    manifest = json.loads((output / "run_manifest.json").read_text("utf-8"))
    expected = hashlib.sha256(agriculture_manifest.read_bytes()).hexdigest()
    assert manifest["agricultural_persistence_input"]["manifest_sha256"] == expected
    assert manifest["scientific_flags"]["persistent_agricultural_evidence_provided"] is True


@pytest.mark.parametrize("failing", ["full", "hampel", "deltas", "attribution"])
def test_failure_is_fail_fast_and_preserves_failure_envelope(
    tmp_path: Path, failing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _inputs(tmp_path)
    calls: list[str] = []
    rename_calls: list[tuple[Path, Path]] = []

    def retrying_rename(source: Path, target: Path) -> None:
        rename_calls.append((source, target))
        source.rename(target)

    monkeypatch.setattr(complete_module, "_rename_directory_with_retry", retrying_rename)

    def runner(name: str) -> Any:
        def call(**kwargs: Any) -> Path:
            calls.append(name)
            if name == failing:
                raise RuntimeError("secret-token=abc")
            return _publish_child(Path(kwargs["output_root"]), name)

        return call

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
        analysis_end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
        hls_start_year=2020,
        hls_end_year=2025,
    )
    with pytest.raises(CompleteAnalysisError) as error:
        run_complete_analysis(
            request,
            created_at=CREATED,
            pipeline_runner=runner("full"),
            hampel_runner=runner("hampel"),
            delta_runner=runner("deltas"),
            attribution_runner=runner("attribution"),
            **{key: value for key, value in _loaders().items() if key != "attribution_runner"},
        )
    expected = {
        "full": ["full"],
        "hampel": ["full", "hampel"],
        "deltas": ["full", "hampel", "deltas"],
        "attribution": ["full", "hampel", "deltas", "attribution"],
    }
    assert calls == expected[failing]
    assert rename_calls == [(rename_calls[0][0], error.value.failure_path)]
    assert rename_calls[0][0].name.endswith(".staging")
    assert error.value.failure_path.name.endswith(".failed")
    report = json.loads(
        (error.value.failure_path / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert report["overall_status"] == ("failed" if failing == "full" else "partial")
    assert "secret-token=abc" not in json.dumps(report)
    assert report["failure"]["message"] == "secret-token=[REDACTED]"
    assert not error.value.failure_path.with_suffix("").exists()
    assert not any(error.value.failure_path.parent.glob(".*.staging"))
    completed = [item for item in report["components"] if item["status"] == "completed"]
    for component in completed:
        assert (error.value.failure_path / component["output_bundle"]).is_dir()
    incomplete = [item for item in report["components"] if item["status"] != "completed"]
    for component in incomplete:
        receipt_path = error.value.failure_path / component["status_receipt"]
        assert receipt_path.is_file()
        assert (
            hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            == component["status_receipt_sha256"]
        )
        receipt = json.loads(receipt_path.read_text("utf-8"))
        assert receipt["component"] == component["name"]
        assert receipt["status"] == component["status"]
        assert receipt["dependencies"] == report["dependencies"].get(component["name"], [])
        assert "abc" not in json.dumps(receipt)


def test_failure_manifest_never_leaks_external_input_path(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)

    def fail(**_: Any) -> Path:
        raise RuntimeError(f"could not process {paths['vector'].resolve()}")

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(CompleteAnalysisError) as error:
        run_complete_analysis(request, created_at=CREATED, pipeline_runner=fail, **_loaders())
    serialized = (error.value.failure_path / "run_manifest.json").read_text(encoding="utf-8")
    assert str(tmp_path.resolve()) not in serialized
    assert "[INPUT_PATH]" in serialized


def test_rejects_invalid_years_and_missing_inputs_before_runner(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
        analysis_end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
        hls_start_year=2025,
        hls_end_year=2026,
    )
    with pytest.raises(ValueError, match="closed"):
        run_complete_analysis(request, created_at=CREATED)


def test_invalid_config_fails_before_creating_run_or_calling_runner(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    calls: list[str] = []

    def invalid(_: Path) -> object:
        raise ValueError("invalid_config")

    def unexpected(**_: Any) -> Path:
        calls.append("called")
        return tmp_path

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(ValueError, match="invalid_config"):
        run_complete_analysis(
            request,
            created_at=CREATED,
            config_loader=invalid,
            pipeline_runner=unexpected,
            **{key: value for key, value in _loaders().items() if key != "config_loader"},
        )
    assert calls == []
    assert not (tmp_path / "runs").exists()


def test_missing_child_manifest_blocks_complete(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)

    def broken(**kwargs: Any) -> Path:
        bundle = Path(kwargs["output_root"]) / "broken"
        bundle.mkdir(parents=True)
        return bundle

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
        analysis_end_date=datetime(2025, 12, 31, tzinfo=UTC).date(),
        hls_start_year=2020,
        hls_end_year=2025,
    )
    with pytest.raises(CompleteAnalysisError) as error:
        run_complete_analysis(
            request,
            created_at=CREATED,
            pipeline_runner=broken,
            **_loaders(),
        )
    assert "child_manifest_missing" in str(error.value)


def test_all_children_are_revalidated_immediately_before_publication(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)

    def full(**kwargs: Any) -> Path:
        return _publish_child(Path(kwargs["output_root"]), "full")

    def hampel(**kwargs: Any) -> Path:
        source_bundle = Path(kwargs["source_bundle"])
        (source_bundle / "json/run/summary.json").write_text("tampered", encoding="utf-8")
        return _publish_child(Path(kwargs["output_root"]), "hampel")

    def deltas(**kwargs: Any) -> Path:
        return _publish_child(Path(kwargs["output_root"]), "deltas")

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(CompleteAnalysisError, match="artifact_size_mismatch"):
        run_complete_analysis(
            request,
            created_at=CREATED,
            pipeline_runner=full,
            hampel_runner=hampel,
            delta_runner=deltas,
            **_loaders(),
        )


@pytest.mark.parametrize(
    ("years", "message"),
    [
        ((2021, 2025), "reference_year_2020"),
        ((2020, 2022, 2024), "contiguous"),
        ((2020, 2022, 2021), "sorted_unique"),
        ((2020, 2021, 2021, 2022), "sorted_unique"),
        ((2021, 2022), "reference_year_2020"),
        ((2020, 2026), "closed"),
    ],
)
def test_preflight_rejects_incoherent_ranges_before_runner(
    tmp_path: Path, years: tuple[int, ...], message: str
) -> None:
    paths = _inputs(tmp_path)
    calls: list[str] = []
    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
        hls_start_year=years[0],
        hls_end_year=years[-1],
    )
    loaders = _loaders()
    loaders["model_loader"] = lambda _: SimpleNamespace(supported_observation_years=years)

    def unexpected_runner(**_: Any) -> Path:
        calls.append("called")
        return tmp_path

    with pytest.raises(ValueError, match=message):
        run_complete_analysis(
            request,
            created_at=CREATED,
            pipeline_runner=unexpected_runner,
            **loaders,
        )
    assert not calls
    assert not (tmp_path / "runs").exists()


def test_rf_years_must_include_2020_before_runner(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    loaders = _loaders()
    loaders["model_loader"] = lambda _: SimpleNamespace(
        supported_observation_years=(2021, 2022, 2023, 2024)
    )
    with pytest.raises(ValueError, match="include_2020"):
        run_complete_analysis(request, created_at=CREATED, **loaders)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (SimpleNamespace(), "missing"),
        (SimpleNamespace(supported_observation_years=()), "empty"),
        (SimpleNamespace(supported_observation_years=tuple(range(2020, 2027))), "closed"),
    ],
)
def test_rf_contract_is_validated_before_any_runner(
    tmp_path: Path, model: SimpleNamespace, message: str
) -> None:
    paths = _inputs(tmp_path)
    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    loaders = _loaders()
    loaders["model_loader"] = lambda _: model
    with pytest.raises(ValueError, match=message):
        run_complete_analysis(request, created_at=CREATED, **loaders)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("size", "size_mismatch"),
        ("sha", "sha256_mismatch"),
        ("traversal", "unsafe_or_duplicate"),
        ("absolute", "unsafe_or_duplicate"),
        ("duplicate", "unsafe_or_duplicate"),
        ("missing", "artifact_missing"),
        ("directory", "artifact_missing"),
        ("missing_id", "invalid_analysis_id"),
        ("bad_created_at", "invalid_created_at"),
        ("created_at_mismatch", "created_at_mismatch"),
    ],
)
def test_corrupt_child_manifest_blocks_completion(
    tmp_path: Path, mutation: str, message: str
) -> None:
    paths = _inputs(tmp_path)

    def corrupt(**kwargs: Any) -> Path:
        bundle = _publish_child(Path(kwargs["output_root"]), "broken")
        manifest_path = bundle / "json/run/manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if mutation == "size":
            payload["artifacts"][0]["size_bytes"] += 1
        elif mutation == "sha":
            payload["artifacts"][0]["sha256"] = "0" * 64
        elif mutation == "traversal":
            payload["artifacts"][0]["path"] = "../outside.txt"
        elif mutation == "absolute":
            payload["artifacts"][0]["path"] = str((tmp_path / "outside.txt").resolve())
        elif mutation == "duplicate":
            payload["artifacts"].append(payload["artifacts"][0].copy())
        elif mutation == "missing":
            (bundle / "json/run/summary.json").unlink()
        elif mutation == "directory":
            (bundle / "json/run/summary.json").unlink()
            (bundle / "json/run/summary.json").mkdir()
        elif mutation == "bad_created_at":
            payload["created_at"] = "not-a-date"
        elif mutation == "created_at_mismatch":
            payload["created_at"] = "2026-08-05T00:00:00+00:00"
        else:
            payload.pop("analysis_id")
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        return bundle

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(CompleteAnalysisError, match=message):
        run_complete_analysis(request, created_at=CREATED, pipeline_runner=corrupt, **_loaders())


def test_runner_cannot_publish_outside_component_root(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    outside = _publish_child(tmp_path, "outside")
    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(CompleteAnalysisError, match="outside_component_root"):
        run_complete_analysis(
            request, created_at=CREATED, pipeline_runner=lambda **_: outside, **_loaders()
        )


def test_child_artifact_symlink_is_rejected(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)

    def linked(**kwargs: Any) -> Path:
        bundle = _publish_child(Path(kwargs["output_root"]), "linked")
        target = bundle / "target.json"
        target.write_text("{}\n", encoding="utf-8")
        artifact = bundle / "json/run/summary.json"
        artifact.unlink()
        try:
            artifact.symlink_to(target)
        except OSError:
            pytest.skip("symlinks are not available in this Windows environment")
        payload_path = bundle / "json/run/manifest.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        payload["artifacts"][0]["size_bytes"] = target.stat().st_size
        payload["artifacts"][0]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        return bundle

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(CompleteAnalysisError, match="symlink_forbidden"):
        run_complete_analysis(request, created_at=CREATED, pipeline_runner=linked, **_loaders())


@pytest.mark.parametrize("collision", ["final", "staging", "failed"])
def test_run_name_collisions_never_overwrite(tmp_path: Path, collision: str) -> None:
    paths = _inputs(tmp_path)
    analysis_id = UUID("12345678-1234-5678-1234-567812345678")
    run_name = "field__20260806T000000000000Z__12345678"
    output_root = tmp_path / "runs"
    existing = {
        "final": output_root / run_name,
        "staging": output_root / f".{run_name}.staging",
        "failed": output_root / f"{run_name}.failed",
    }[collision]
    existing.mkdir(parents=True)
    marker = existing / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=output_root,
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(FileExistsError, match="run_collision"):
        run_complete_analysis(request, created_at=CREATED, analysis_id=analysis_id, **_loaders())
    assert marker.read_text(encoding="utf-8") == "keep"


def test_duplicate_or_parent_child_analysis_ids_block_publication(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    parent_id = UUID("12345678-1234-5678-1234-567812345678")

    def child_with_id(**kwargs: Any) -> Path:
        bundle = _publish_child(Path(kwargs["output_root"]), "child")
        manifest = bundle / "json/run/manifest.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["analysis_id"] = str(parent_id)
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        return bundle

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(CompleteAnalysisError, match="must_differ_from_parent"):
        run_complete_analysis(
            request,
            created_at=CREATED,
            analysis_id=parent_id,
            pipeline_runner=child_with_id,
            hampel_runner=child_with_id,
            delta_runner=child_with_id,
            **_loaders(),
        )


def test_duplicate_child_analysis_ids_block_publication(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    duplicate_id = UUID("87654321-1234-5678-1234-567812345678")

    def child_with_duplicate_id(**kwargs: Any) -> Path:
        bundle = _publish_child(Path(kwargs["output_root"]), "child")
        manifest = bundle / "json/run/manifest.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["analysis_id"] = str(duplicate_id)
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        return bundle

    request = CompleteAnalysisRequest(
        input_path=paths["vector"],
        output_root=tmp_path / "runs",
        config_path=paths["config"],
        forest_model_config_path=paths["model"],
        hampel_config_path=paths["hampel"],
        establishment_id="field",
    )
    with pytest.raises(CompleteAnalysisError, match="child_analysis_ids_must_be_unique"):
        run_complete_analysis(
            request,
            created_at=CREATED,
            pipeline_runner=child_with_duplicate_id,
            hampel_runner=child_with_duplicate_id,
            delta_runner=child_with_duplicate_id,
            **_loaders(),
        )


def test_sanitizer_redacts_common_credential_shapes() -> None:
    message = (
        'private_key: "very-secret" Authorization: Bearer abc.def '
        "https://x.invalid/?token=qwerty\n"
        "https://x.invalid/?access_token=access-value&X-Goog-Signature=signed-value\n"
        'client_secret="two words should disappear"\n'
        "private_key_id: private-id\n"
        "-----BEGIN PRIVATE KEY-----\nline1\nline2\n-----END PRIVATE KEY-----"
    )
    sanitized = _sanitize(message)
    assert "very-secret" not in sanitized
    assert "abc.def" not in sanitized
    assert "qwerty" not in sanitized
    assert "access-value" not in sanitized
    assert "signed-value" not in sanitized
    assert "two words" not in sanitized
    assert "private-id" not in sanitized
    assert "line1" not in sanitized
    assert "\n" not in sanitized


def test_code_provenance_is_nullable_and_uses_git_only_for_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> SimpleNamespace:
        calls.append(command)
        output = "abc123\n" if "rev-parse" in command else " M file.py\n"
        return SimpleNamespace(stdout=output, returncode=0)

    monkeypatch.setattr("deforestation_pipeline.complete_analysis.subprocess.run", fake_run)
    provenance = _code_provenance()
    assert provenance["git_revision"] == "abc123"
    assert provenance["git_dirty"] is True
    assert calls == [
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain"],
    ]


def test_code_provenance_does_not_claim_clean_when_git_status_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_: Any) -> SimpleNamespace:
        if "status" in command:
            return SimpleNamespace(stdout="", returncode=1)
        return SimpleNamespace(stdout="abc123\n", returncode=0)

    monkeypatch.setattr("deforestation_pipeline.complete_analysis.subprocess.run", fake_run)
    provenance = _code_provenance()
    assert provenance["git_revision"] == "abc123"
    assert provenance["git_dirty"] is None


def test_code_provenance_uses_injected_revision_without_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEFORESTATION_CODE_REVISION", "image-abc123")
    monkeypatch.setattr(
        "deforestation_pipeline.complete_analysis.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )

    provenance = _code_provenance()

    assert provenance["git_revision"] == "image-abc123"
    assert provenance["git_dirty"] is None


def test_cli_defaults_and_forwarding_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector = tmp_path / "field.geojson"
    vector.write_text("{}", encoding="utf-8")
    captured: list[CompleteAnalysisRequest] = []

    def fake_run(request: CompleteAnalysisRequest) -> Path:
        captured.append(request)
        return tmp_path / "published"

    monkeypatch.setattr(SCRIPT_MODULE, "run_complete_analysis", fake_run)
    monkeypatch.setattr(SCRIPT_MODULE.Path, "is_file", lambda path: False)
    exit_code = SCRIPT_MODULE.main(
        [
            str(vector),
            "--model-artifact",
            str(tmp_path / "candidate.joblib"),
            "--source-crs",
            "EPSG:4326",
            "--layer",
            "plots",
            "--dissolve-all",
        ]
    )
    assert exit_code == 0
    request = captured[0]
    assert request.establishment_id == "field"
    assert request.credentials_path is None
    assert request.source_crs == "EPSG:4326"
    assert request.vector_layer == "plots"
    assert request.dissolve_all is True
    assert request.model_artifact_path == tmp_path / "candidate.joblib"
    assert request.hls_start_year == 2020 and request.hls_end_year == 2025
    assert request.declared_land_use == "unknown"
    assert request.declared_context_source == "not_provided"
    assert request.agricultural_evidence_policy_path == (
        PROJECT_ROOT / "configs" / "agricultural-evidence.yml"
    )
    assert request.agricultural_collector_config_path == (
        PROJECT_ROOT / "configs" / "agricultural-collector.yml"
    )
    assert request.agricultural_persistence_config_path == (
        PROJECT_ROOT / "configs" / "agricultural-persistence.yml"
    )
    assert request.catalog_path == PROJECT_ROOT / "data" / "catalog.yml"
    assert request.licenses_path == PROJECT_ROOT / "data" / "licenses.yml"
    assert request.agricultural_evidence_path is None
    assert request.agricultural_persistence_bundle_path is None


def test_cli_forwards_declared_managed_forest_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector = tmp_path / "field.geojson"
    vector.write_text("{}", encoding="utf-8")
    captured: list[CompleteAnalysisRequest] = []

    def fake_run(request: CompleteAnalysisRequest) -> Path:
        captured.append(request)
        return tmp_path / "published"

    monkeypatch.setattr(SCRIPT_MODULE, "run_complete_analysis", fake_run)
    monkeypatch.setattr(SCRIPT_MODULE.Path, "is_file", lambda path: False)

    assert (
        SCRIPT_MODULE.main(
            [
                str(vector),
                "--declared-land-use",
                "managed_forest_plantation",
                "--declared-context-source",
                "user_declared",
            ]
        )
        == 0
    )
    assert captured[0].declared_land_use == "managed_forest_plantation"
    assert captured[0].declared_context_source == "user_declared"


def test_cli_forwards_all_automatic_agriculture_contract_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector = tmp_path / "field.geojson"
    vector.write_text("{}", encoding="utf-8")
    named = {
        "collector": tmp_path / "collector.yml",
        "persistence": tmp_path / "persistence.yml",
        "policy": tmp_path / "policy.yml",
        "catalog": tmp_path / "catalog.yml",
        "licenses": tmp_path / "licenses.yml",
    }
    captured: list[CompleteAnalysisRequest] = []

    def fake_run(request: CompleteAnalysisRequest) -> Path:
        captured.append(request)
        return tmp_path / "published"

    monkeypatch.setattr(
        SCRIPT_MODULE,
        "run_complete_analysis",
        fake_run,
    )
    monkeypatch.setattr(SCRIPT_MODULE.Path, "is_file", lambda path: False)
    assert (
        SCRIPT_MODULE.main(
            [
                str(vector),
                "--agricultural-collector-config",
                str(named["collector"]),
                "--agricultural-persistence-config",
                str(named["persistence"]),
                "--agricultural-evidence-policy",
                str(named["policy"]),
                "--catalog",
                str(named["catalog"]),
                "--licenses",
                str(named["licenses"]),
            ]
        )
        == 0
    )
    request = captured[0]
    assert request.agricultural_collector_config_path == named["collector"]
    assert request.agricultural_persistence_config_path == named["persistence"]
    assert request.agricultural_evidence_policy_path == named["policy"]
    assert request.catalog_path == named["catalog"]
    assert request.licenses_path == named["licenses"]


def test_cli_forwards_agricultural_evidence_contract_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector = tmp_path / "field.geojson"
    evidence = tmp_path / "agriculture.json"
    policy = tmp_path / "policy.yml"
    vector.write_text("{}", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")
    policy.write_text("{}", encoding="utf-8")
    captured: list[CompleteAnalysisRequest] = []

    def fake_run(request: CompleteAnalysisRequest) -> Path:
        captured.append(request)
        return tmp_path / "published"

    monkeypatch.setattr(SCRIPT_MODULE, "run_complete_analysis", fake_run)
    assert (
        SCRIPT_MODULE.main(
            [
                str(vector),
                "--agricultural-evidence-json",
                str(evidence),
                "--agricultural-evidence-policy",
                str(policy),
            ]
        )
        == 0
    )
    assert captured[0].agricultural_evidence_path == evidence
    assert captured[0].agricultural_evidence_policy_path == policy


def test_cli_forwards_persistent_agricultural_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector = tmp_path / "field.geojson"
    bundle = tmp_path / "agriculture"
    vector.write_text("{}", encoding="utf-8")
    bundle.mkdir()
    captured: list[CompleteAnalysisRequest] = []

    def fake_run(request: CompleteAnalysisRequest) -> Path:
        captured.append(request)
        return tmp_path / "published"

    monkeypatch.setattr(SCRIPT_MODULE, "run_complete_analysis", fake_run)
    assert SCRIPT_MODULE.main([str(vector), "--agricultural-persistence-bundle", str(bundle)]) == 0
    assert captured[0].agricultural_persistence_bundle_path == bundle


def test_cli_forwards_user_oauth_project_without_service_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector = tmp_path / "field.geojson"
    vector.write_text("{}", encoding="utf-8")
    captured: list[CompleteAnalysisRequest] = []

    def fake_run(request: CompleteAnalysisRequest) -> Path:
        captured.append(request)
        return tmp_path / "published"

    monkeypatch.setattr(SCRIPT_MODULE, "run_complete_analysis", fake_run)
    assert SCRIPT_MODULE.main([str(vector), "--gee-project", "versioned-ee-project"]) == 0
    assert captured[0].gee_project == "versioned-ee-project"
    assert captured[0].credentials_path is None


def test_cli_failure_returns_nonzero_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vector = tmp_path / "field.geojson"
    vector.write_text("{}", encoding="utf-8")

    def fail(_: CompleteAnalysisRequest) -> Path:
        raise ValueError("bad_request")

    monkeypatch.setattr(SCRIPT_MODULE, "run_complete_analysis", fail)
    monkeypatch.setattr(SCRIPT_MODULE.Path, "is_file", lambda path: False)
    assert SCRIPT_MODULE.main([str(vector)]) == 1
    assert capsys.readouterr().err.strip() == "error: bad_request"


def test_cli_forwards_explicit_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vector = tmp_path / "field.geojson"
    credentials = tmp_path / "service-account.json"
    vector.write_text("{}", encoding="utf-8")
    credentials.write_text("{}", encoding="utf-8")
    captured: list[CompleteAnalysisRequest] = []

    def fake_run(request: CompleteAnalysisRequest) -> Path:
        captured.append(request)
        return tmp_path / "published"

    monkeypatch.setattr(SCRIPT_MODULE, "run_complete_analysis", fake_run)
    assert SCRIPT_MODULE.main([str(vector), "--credentials", str(credentials)]) == 0
    assert captured[0].credentials_path == credentials
