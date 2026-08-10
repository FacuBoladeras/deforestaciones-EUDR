from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from deforestation_pipeline.complete_analysis import (
    PROJECT_ROOT,
    CompleteAnalysisError,
    CompleteAnalysisRequest,
    _code_provenance,
    _parameters_hash,
    _sanitize,
    run_complete_analysis,
)

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
        "config_loader": lambda _: object(),
        "model_loader": lambda _: SimpleNamespace(
            supported_observation_years=(2020, 2021, 2022, 2023, 2024)
        ),
        "hampel_loader": lambda _: object(),
        "attribution_runner": lambda **kwargs: _publish_child(
            Path(kwargs["output_root"]), "attribution-child"
        ),
    }


def test_runs_components_in_order_and_publishes_relative_references(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

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

    def attribution(**kwargs: Any) -> Path:
        calls.append(("attribution", kwargs))
        assert kwargs["source_event_bundle"] == calls[0][1]["output_root"] / "full-child"
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
        attribution_runner=attribution,
        **{key: value for key, value in _loaders().items() if key != "attribution_runner"},
    )

    assert [name for name, _ in calls] == ["full", "hampel", "deltas", "attribution"]
    assert output.parent == (tmp_path / "runs").resolve()
    assert ".." not in output.name and "field-a" in output.name
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["overall_status"] == "complete"
    assert [item["status"] for item in manifest["components"]] == ["completed"] * 4
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
    assert len({item["child_analysis_id"] for item in manifest["components"]}) == 4
    assert all(item["child_created_at"] == CREATED.isoformat() for item in manifest["components"])
    assert len(manifest["parameters_hash"]) == 64
    assert manifest["parameters_hash"] == _parameters_hash(manifest)


@pytest.mark.parametrize("failing", ["full", "hampel", "deltas", "attribution"])
def test_failure_is_fail_fast_and_preserves_failure_envelope(tmp_path: Path, failing: str) -> None:
    paths = _inputs(tmp_path)
    calls: list[str] = []

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
    assert request.hls_start_year == 2020 and request.hls_end_year == 2025
    assert request.declared_land_use == "unknown"
    assert request.declared_context_source == "not_provided"


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
