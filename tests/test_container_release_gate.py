from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REVISION = "0123456789abcdef0123456789abcdef01234567"
SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "container_release_gate.py"
SPEC = importlib.util.spec_from_file_location("container_release_gate", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_release_plan_rejects_noncanonical_revision(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="40 caracteres hexadecimales lowercase"):
        MODULE.build_release_plan(
            revision="HEAD",
            output_dir=tmp_path,
            platform="linux/amd64",
        )


def test_execution_source_must_be_clean_and_match_revision() -> None:
    with pytest.raises(RuntimeError, match="cambios sin commit"):
        MODULE._validate_source_state(
            requested_revision=REVISION,
            actual_revision=REVISION,
            porcelain_status=" M Dockerfile.api",
        )
    with pytest.raises(RuntimeError, match="no coincide"):
        MODULE._validate_source_state(
            requested_revision=REVISION,
            actual_revision="f" * 40,
            porcelain_status="",
        )


def test_release_plan_covers_both_images_without_latest_tag(tmp_path: Path) -> None:
    plan = MODULE.build_release_plan(
        revision=REVISION,
        output_dir=tmp_path,
        platform="linux/amd64",
    )

    assert plan.images == (
        MODULE.ImageSpec(name="api", dockerfile="Dockerfile.api"),
        MODULE.ImageSpec(name="worker", dockerfile="Dockerfile.worker"),
    )
    assert [image.reference for image in plan.planned_images] == [
        f"deforestation-api:{REVISION}",
        f"deforestation-worker:{REVISION}",
    ]
    assert all(":latest" not in image.reference for image in plan.planned_images)


def test_release_plan_requires_reproducible_metadata_sbom_and_strict_scan(
    tmp_path: Path,
) -> None:
    plan = MODULE.build_release_plan(
        revision=REVISION,
        output_dir=tmp_path,
        platform="linux/amd64",
    )

    commands = [command.argv for command in plan.commands]
    builds = [command for command in commands if command[:3] == ("docker", "buildx", "build")]
    sboms = [command for command in commands if command[:3] == ("docker", "scout", "sbom")]
    scans = [command for command in commands if command[:3] == ("docker", "scout", "cves")]

    assert len(builds) == len(sboms) == len(scans) == 2
    for command in builds:
        assert "--pull" in command
        assert "--load" in command
        assert "--metadata-file" in command
        assert "--provenance=mode=max" in command
        assert "--sbom=true" in command
        assert f"VCS_REF={REVISION}" in command
        assert "linux/amd64" in command
    for command in sboms:
        assert "--format" in command
        assert "spdx" in command
        assert any(argument.startswith("local://deforestation-") for argument in command)
    for command in scans:
        assert "--exit-code" in command
        assert "--only-severity" in command
        assert "critical,high" in command
        assert "--format" in command
        assert "sarif" in command

    assert plan.environment == {"BUILDX_METADATA_PROVENANCE": "max"}


def test_cli_defaults_to_plan_only_and_does_not_require_docker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "evidence"
    exit_code = MODULE.main(
        [
            "--revision",
            REVISION,
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    assert not output_dir.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "plan"
    assert payload["revision"] == REVISION
    assert len(payload["commands"]) == 8


def test_execute_writes_verified_hash_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "evidence"
    plan = MODULE.build_release_plan(
        revision=REVISION,
        output_dir=output_dir,
        platform="linux/amd64",
    )
    monkeypatch.setattr(MODULE, "_check_tooling", lambda: None)
    monkeypatch.setattr(MODULE, "_check_source_revision", lambda _: None)

    def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ("docker", "buildx", "build"):
            metadata_path = Path(argv[argv.index("--metadata-file") + 1])
            metadata_path.write_text(
                json.dumps(
                    {
                        "containerimage.digest": "sha256:" + "a" * 64,
                        "buildx.build.provenance": {},
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ("docker", "image", "inspect"):
            config = {
                "Labels": {"org.opencontainers.image.revision": REVISION},
                "User": "10001:10001",
                "Healthcheck": {"Test": ["CMD", "true"]},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(config), "")
        output_path = Path(argv[argv.index("--output") + 1])
        output_path.write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    manifest_path = MODULE.execute_release_plan(plan)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["revision"] == REVISION
    assert manifest["platform"] == "linux/amd64"
    assert [image["name"] for image in manifest["images"]] == ["api", "worker"]
    assert all(len(file["sha256"]) == 64 for image in manifest["images"] for file in image["files"])
