"""Plan or execute the auditable local release gate for container images.

Planning is the safe default. Execution builds images and therefore must only be
requested by an operator outside environments where repository policy forbids builds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
_VERSION_PATTERN = re.compile(r"(?:buildx\s+|version:\s*)v?(\d+)\.(\d+)\.(\d+)", re.I)
_MINIMUM_BUILDX = (0, 14, 0)
_MINIMUM_SCOUT = (1, 4, 0)
_IMAGES = (
    # The image names deliberately match the Compose service roles.
    ("api", "Dockerfile.api"),
    ("worker", "Dockerfile.worker"),
)


@dataclass(frozen=True)
class ImageSpec:
    """Static image input."""

    name: str
    dockerfile: str


@dataclass(frozen=True)
class PlannedImage:
    """Image reference and expected evidence files."""

    name: str
    reference: str
    metadata_path: Path
    config_path: Path
    sbom_path: Path
    vulnerabilities_path: Path


@dataclass(frozen=True)
class PlannedCommand:
    """One command in execution order, optionally captured to a file."""

    argv: tuple[str, ...]
    stdout_path: Path | None = None


@dataclass(frozen=True)
class ReleasePlan:
    """Complete local release-gate plan."""

    revision: str
    platform: str
    output_dir: Path
    images: tuple[ImageSpec, ...]
    planned_images: tuple[PlannedImage, ...]
    commands: tuple[PlannedCommand, ...]
    environment: dict[str, str]

    def as_json_value(self, *, mode: str) -> dict[str, Any]:
        return {
            "mode": mode,
            "revision": self.revision,
            "platform": self.platform,
            "output_dir": str(self.output_dir),
            "environment": self.environment,
            "images": [
                {
                    **asdict(image),
                    "metadata_path": str(image.metadata_path),
                    "config_path": str(image.config_path),
                    "sbom_path": str(image.sbom_path),
                    "vulnerabilities_path": str(image.vulnerabilities_path),
                }
                for image in self.planned_images
            ],
            "commands": [list(command.argv) for command in self.commands],
        }


def _validate_revision(revision: str) -> None:
    if _REVISION_PATTERN.fullmatch(revision) is None:
        raise ValueError("revision debe tener 40 caracteres hexadecimales lowercase")


def _validate_platform(platform: str) -> None:
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("platform debe ser linux/amd64 o linux/arm64")


def build_release_plan(*, revision: str, output_dir: Path, platform: str) -> ReleasePlan:
    """Create a deterministic build, evidence and vulnerability-scan plan."""

    _validate_revision(revision)
    _validate_platform(platform)
    specs = tuple(ImageSpec(name=name, dockerfile=dockerfile) for name, dockerfile in _IMAGES)
    planned_images: list[PlannedImage] = []
    commands: list[PlannedCommand] = []

    for spec in specs:
        image_dir = output_dir / spec.name
        reference = f"deforestation-{spec.name}:{revision}"
        image = PlannedImage(
            name=spec.name,
            reference=reference,
            metadata_path=image_dir / "build-metadata.json",
            config_path=image_dir / "image-config.json",
            sbom_path=image_dir / "sbom.spdx.json",
            vulnerabilities_path=image_dir / "vulnerabilities.sarif.json",
        )
        planned_images.append(image)
        commands.extend(
            (
                PlannedCommand(
                    (
                        "docker",
                        "buildx",
                        "build",
                        "--pull",
                        "--load",
                        "--platform",
                        platform,
                        "--provenance=mode=max",
                        "--sbom=true",
                        "--metadata-file",
                        str(image.metadata_path),
                        "--build-arg",
                        f"VCS_REF={revision}",
                        "--file",
                        spec.dockerfile,
                        "--tag",
                        reference,
                        ".",
                    )
                ),
                PlannedCommand(
                    (
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{json .Config}}",
                        reference,
                    ),
                    stdout_path=image.config_path,
                ),
                PlannedCommand(
                    (
                        "docker",
                        "scout",
                        "sbom",
                        "--format",
                        "spdx",
                        "--output",
                        str(image.sbom_path),
                        f"local://{reference}",
                    )
                ),
                PlannedCommand(
                    (
                        "docker",
                        "scout",
                        "cves",
                        "--exit-code",
                        "--only-severity",
                        "critical,high",
                        "--format",
                        "sarif",
                        "--output",
                        str(image.vulnerabilities_path),
                        f"local://{reference}",
                    )
                ),
            )
        )

    return ReleasePlan(
        revision=revision,
        platform=platform,
        output_dir=output_dir,
        images=specs,
        planned_images=tuple(planned_images),
        commands=tuple(commands),
        environment={"BUILDX_METADATA_PROVENANCE": "max"},
    )


def _tool_version(argv: tuple[str, ...], minimum: tuple[int, int, int]) -> None:
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    match = _VERSION_PATTERN.search(result.stdout + result.stderr)
    if match is None:
        raise RuntimeError(f"no se pudo determinar la versión de {' '.join(argv)}")
    installed = tuple(int(component) for component in match.groups())
    if installed < minimum:
        required = ".".join(str(component) for component in minimum)
        found = ".".join(str(component) for component in installed)
        raise RuntimeError(f"{' '.join(argv)} {found} es menor que la versión requerida {required}")


def _check_tooling() -> None:
    subprocess.run(
        ("docker", "info", "--format", "{{.ServerVersion}}"),
        check=True,
        capture_output=True,
        text=True,
    )
    _tool_version(("docker", "buildx", "version"), _MINIMUM_BUILDX)
    _tool_version(("docker", "scout", "version"), _MINIMUM_SCOUT)


def _validate_source_state(
    *, requested_revision: str, actual_revision: str, porcelain_status: str
) -> None:
    if porcelain_status.strip():
        raise RuntimeError("el release no admite cambios sin commit")
    if actual_revision.strip() != requested_revision:
        raise RuntimeError("VCS_REF no coincide con el commit checkout")


def _check_source_revision(revision: str) -> None:
    actual = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=all"),
        check=True,
        capture_output=True,
        text=True,
    )
    _validate_source_state(
        requested_revision=revision,
        actual_revision=actual.stdout,
        porcelain_status=status.stdout,
    )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"artefacto JSON inválido: {path}") from exc


def _validate_image_evidence(image: PlannedImage, revision: str) -> None:
    metadata = _load_json(image.metadata_path)
    config = _load_json(image.config_path)
    _load_json(image.sbom_path)
    _load_json(image.vulnerabilities_path)
    if not isinstance(metadata, dict) or not metadata.get("containerimage.digest"):
        raise RuntimeError(f"metadata sin digest para {image.reference}")
    if not isinstance(metadata.get("buildx.build.provenance"), dict):
        raise RuntimeError(f"metadata sin procedencia para {image.reference}")
    if not isinstance(config, dict):
        raise RuntimeError(f"config de imagen inválida para {image.reference}")
    labels = config.get("Labels")
    if not isinstance(labels, dict) or labels.get("org.opencontainers.image.revision") != revision:
        raise RuntimeError(f"label de revisión inválida para {image.reference}")
    if config.get("User") != "10001:10001":
        raise RuntimeError(f"usuario runtime inválido para {image.reference}")
    if not isinstance(config.get("Healthcheck"), dict):
        raise RuntimeError(f"healthcheck ausente para {image.reference}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execute_release_plan(plan: ReleasePlan) -> Path:
    """Execute a plan and emit a hash manifest only after every gate passes."""

    _check_source_revision(plan.revision)
    _check_tooling()
    if plan.output_dir.exists() and any(plan.output_dir.iterdir()):
        raise RuntimeError(f"el directorio de evidencia no está vacío: {plan.output_dir}")
    for image in plan.planned_images:
        image.metadata_path.parent.mkdir(parents=True, exist_ok=True)

    environment = os.environ.copy()
    environment.update(plan.environment)
    for command in plan.commands:
        if command.stdout_path is None:
            subprocess.run(command.argv, check=True, env=environment)
            continue
        result = subprocess.run(
            command.argv,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        command.stdout_path.write_text(result.stdout, encoding="utf-8")

    for image in plan.planned_images:
        _validate_image_evidence(image, plan.revision)

    manifest_path = plan.output_dir / "release-evidence.json"
    artifacts = []
    for image in plan.planned_images:
        paths = (
            image.metadata_path,
            image.config_path,
            image.sbom_path,
            image.vulnerabilities_path,
        )
        artifacts.append(
            {
                "name": image.name,
                "reference": image.reference,
                "files": [
                    {
                        "path": str(path.relative_to(plan.output_dir)),
                        "size": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                    for path in paths
                ],
            }
        )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "revision": plan.revision,
                "platform": plan.platform,
                "images": artifacts,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="SHA git lowercase completo")
    parser.add_argument("--platform", default="linux/amd64", choices=("linux/amd64", "linux/arm64"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="ejecuta builds y escaneos; sin este flag sólo imprime el plan",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = args.output_dir or Path("artifacts") / "container-release" / args.revision
    try:
        plan = build_release_plan(
            revision=args.revision,
            output_dir=output_dir,
            platform=args.platform,
        )
        if args.execute:
            manifest = execute_release_plan(plan)
            print(json.dumps({"mode": "execute", "manifest": str(manifest)}))
        else:
            print(json.dumps(plan.as_json_value(mode="plan"), indent=2, sort_keys=True))
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"container release gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
