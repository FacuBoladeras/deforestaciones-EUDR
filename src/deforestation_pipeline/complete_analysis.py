"""Orquestación auditable de los componentes existentes de un análisis completo."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from deforestation_pipeline.config import load_config, load_forest_model_config
from deforestation_pipeline.hampel_benchmark import (
    load_hampel_benchmark_config,
    materialize_seasonal_hampel_benchmark,
)
from deforestation_pipeline.hls_seasonal import HlsSeasonalRequest
from deforestation_pipeline.local_runner import run_local_vector_pipeline
from deforestation_pipeline.post_change_attribution import (
    AttributionContext,
    DeclaredLandUse,
    materialize_post_change_attribution,
)
from deforestation_pipeline.rf_model_release import verify_local_rf_model_release

COMPLETE_ANALYSIS_SCHEMA_VERSION = "1.1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ANALYSIS_END_DATE = date(2025, 12, 31)
_MANIFEST_RELATIVE_PATH = Path("json/run/manifest.json")
_SAFE_SLUG = re.compile(r"[^a-z0-9]+")
_SECRET_KEY = (
    r"[a-z0-9_-]*(?:token|secret|password|credential|private[_-]?key(?:[_-]?id)?|"
    r"api[_-]?key|signature|authorization)[a-z0-9_-]*"
)
_ASSIGNMENT_SECRET = re.compile(
    rf"(?i)\b({_SECRET_KEY})\b(\s*[:=]\s*)"
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]&]+)"""
)
_BEARER_SECRET = re.compile(r"(?i)\b(?:authorization\s*[:=]?\s*)?bearer\s+\S+")
_QUERY_SECRET = re.compile(rf"(?i)([?&](?:{_SECRET_KEY})=)[^&#\s]+")
_PEM_SECRET = re.compile(r"-----BEGIN [^-\r\n]+-----.*?(?:-----END [^-\r\n]+-----|\Z)", re.DOTALL)


@dataclass(frozen=True, slots=True)
class CompleteAnalysisRequest:
    input_path: Path
    output_root: Path
    config_path: Path
    forest_model_config_path: Path
    hampel_config_path: Path
    establishment_id: str
    analysis_end_date: date = DEFAULT_ANALYSIS_END_DATE
    hls_start_year: int = 2020
    hls_end_year: int = 2025
    credentials_path: Path | None = None
    source_crs: str | None = None
    vector_layer: str | None = None
    dissolve_all: bool = False
    declared_land_use: DeclaredLandUse = "unknown"
    declared_context_source: str = "not_provided"


class CompleteAnalysisError(RuntimeError):
    """Fallo controlado con un envelope de diagnóstico ya publicado."""

    def __init__(self, message: str, failure_path: Path) -> None:
        super().__init__(message)
        self.failure_path = failure_path


Runner = Callable[..., Path]


def run_complete_analysis(
    request: CompleteAnalysisRequest,
    *,
    created_at: datetime | None = None,
    analysis_id: UUID | None = None,
    pipeline_runner: Runner = run_local_vector_pipeline,
    hampel_runner: Runner = materialize_seasonal_hampel_benchmark,
    hampel_loader: Callable[[Path], object] = load_hampel_benchmark_config,
    config_loader: Callable[[Path], object] = load_config,
    model_loader: Callable[[Path], object] = load_forest_model_config,
    delta_runner: Runner = run_local_vector_pipeline,
    attribution_runner: Runner = materialize_post_change_attribution,
) -> Path:
    """Ejecuta full → Hampel → deltas → atribución y publica un envelope auditable."""
    started = created_at or datetime.now(UTC)
    if started.tzinfo is None or started.utcoffset() is None:
        raise ValueError("created_at_requires_timezone")
    resolved = _validate_request(request, started)
    # Parsear los tres contratos antes de cualquier acceso remoto evita corridas
    # parciales por YAML inválido.
    config_loader(resolved["config"])
    model_config = model_loader(resolved["model"])
    if getattr(model_config, "schema_version", None) == "1.1.0":
        verify_local_rf_model_release(
            model_config,
            project_root=PROJECT_ROOT,
            load_bundle=False,
        )
    hampel_config = hampel_loader(resolved["hampel"])
    configured_rf_years = getattr(model_config, "supported_observation_years", None)
    if not isinstance(configured_rf_years, (list, tuple)):
        raise ValueError("rf_supported_observation_years_missing")
    rf_years = tuple(configured_rf_years)
    _validate_rf_years(rf_years, started.year)
    identifier = analysis_id or uuid4()
    slug = _slug(request.establishment_id)
    run_name = f"{slug}__{started.strftime('%Y%m%dT%H%M%S%fZ')}__{str(identifier)[:8]}"
    components = [
        _component(name)
        for name in (
            "full_pipeline",
            "hampel_benchmark",
            "rf_annual_deltas",
            "post_change_attribution",
        )
    ]
    manifest = _base_manifest(
        request, resolved, started, identifier, components, (min(rf_years), max(rf_years))
    )
    output_root = request.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    final = output_root / run_name
    staging = output_root / f".{run_name}.staging"
    failed = output_root / f"{run_name}.failed"
    if final.exists() or staging.exists() or failed.exists():
        raise FileExistsError(f"complete_analysis_run_collision:{run_name}")
    staging.mkdir()
    try:
        full = _execute(
            components[0],
            staging,
            started,
            lambda root: pipeline_runner(
                input_path=resolved["input"],
                output_root=root,
                config_path=resolved["config"],
                forest_model_config_path=None,
                source_crs=request.source_crs,
                establishment_id=request.establishment_id,
                analysis_end_date=request.analysis_end_date,
                created_at=started,
                gee_credentials_path=resolved["credentials"],
                generate_hls_composite=False,
                generate_forest_baseline=True,
                generate_rf_deltas=False,
                hls_seasonal_request=HlsSeasonalRequest(
                    start_year=request.hls_start_year, end_year=request.hls_end_year
                ),
                generate_disturbance_detection=True,
                vector_layer=request.vector_layer,
                dissolve_all=request.dissolve_all,
            ),
        )
        _execute(
            components[1],
            staging,
            started,
            lambda root: hampel_runner(
                source_bundle=full,
                output_root=root,
                input_geojson=resolved["input"],
                establishment_id=request.establishment_id,
                config=hampel_config,
                created_at=started,
            ),
        )
        deltas = _execute(
            components[2],
            staging,
            started,
            lambda root: delta_runner(
                input_path=resolved["input"],
                output_root=root,
                config_path=resolved["config"],
                forest_model_config_path=resolved["model"],
                source_crs=request.source_crs,
                establishment_id=request.establishment_id,
                analysis_end_date=date(max(rf_years), 12, 31),
                created_at=started,
                gee_credentials_path=resolved["credentials"],
                generate_hls_composite=False,
                generate_forest_baseline=False,
                generate_rf_deltas=True,
                hls_seasonal_request=None,
                generate_disturbance_detection=False,
                vector_layer=request.vector_layer,
                dissolve_all=request.dissolve_all,
            ),
        )
        _execute(
            components[3],
            staging,
            started,
            lambda root: attribution_runner(
                source_event_bundle=full,
                source_rf_bundle=deltas,
                output_root=root,
                establishment_id=request.establishment_id,
                context=AttributionContext(
                    declared_land_use=request.declared_land_use,
                    declared_context_source=request.declared_context_source,
                ),
                created_at=started,
            ),
        )
        _revalidate_components(components, staging, started)
        _validate_child_correlations(components, str(identifier))
        manifest["overall_status"] = "complete"
        manifest["completed_at"] = datetime.now(UTC).isoformat()
        manifest["parameters_hash"] = _parameters_hash(manifest)
        _write_json_atomic(staging / "run_manifest.json", manifest)
        staging.rename(final)
        return final
    except Exception as exc:
        safe_message = _sanitize(
            str(exc),
            resolved["credentials"],
            path_replacements={
                resolved["input"]: "[INPUT_PATH]",
                resolved["config"]: "[CONFIG_PATH]",
                resolved["model"]: "[MODEL_CONFIG_PATH]",
                resolved["hampel"]: "[HAMPEL_CONFIG_PATH]",
            },
        )
        _mark_failure(components, exc, safe_message)
        manifest["overall_status"] = (
            "partial" if any(item["status"] == "completed" for item in components) else "failed"
        )
        manifest["completed_at"] = datetime.now(UTC).isoformat()
        manifest["failure"] = {"error_type": type(exc).__name__, "message": safe_message}
        manifest["parameters_hash"] = _parameters_hash(manifest)
        _write_json_atomic(staging / "run_manifest.json", manifest)
        staging.rename(failed)
        raise CompleteAnalysisError(
            f"complete_analysis_{manifest['overall_status']}: {safe_message}", failed
        ) from exc


def _validate_request(request: CompleteAnalysisRequest, now: datetime) -> dict[str, Any]:
    if not request.establishment_id.strip():
        raise ValueError("establishment_id_empty")
    if request.declared_land_use not in {"unknown", "managed_forest_plantation"}:
        raise ValueError("declared_land_use_invalid")
    if not request.declared_context_source.strip():
        raise ValueError("declared_context_source_empty")
    if not (2015 <= request.hls_start_year <= request.hls_end_year < now.year):
        raise ValueError("hls_years_must_be_ordered_closed_years")
    if not request.hls_start_year <= 2020 <= request.hls_end_year:
        raise ValueError("hls_range_must_include_reference_year_2020")
    minimum_end = date(request.hls_end_year, 11, 30)
    if request.analysis_end_date < minimum_end:
        raise ValueError("analysis_end_date_precedes_seasonal_coverage")
    resolved: dict[str, Any] = {}
    for key, path in (
        ("input", request.input_path),
        ("config", request.config_path),
        ("model", request.forest_model_config_path),
        ("hampel", request.hampel_config_path),
    ):
        candidate = path.resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"required_file_missing:{key}:{candidate.name}")
        resolved[key] = candidate
    credentials = request.credentials_path.resolve() if request.credentials_path else None
    if credentials is not None and not credentials.is_file():
        raise FileNotFoundError(f"credentials_file_missing:{credentials.name}")
    resolved["credentials"] = credentials
    return resolved


def _validate_rf_years(years: tuple[int, ...], current_year: int) -> None:
    if not years:
        raise ValueError("rf_supported_observation_years_empty")
    if tuple(sorted(set(years))) != years:
        raise ValueError("rf_supported_observation_years_must_be_sorted_unique")
    if years != tuple(range(years[0], years[-1] + 1)):
        raise ValueError("rf_supported_observation_years_must_be_contiguous")
    if 2020 not in years:
        raise ValueError("rf_supported_observation_years_must_include_2020")
    if years[0] < 2015 or years[-1] >= current_year:
        raise ValueError("rf_supported_observation_years_must_be_closed")


def _component(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pending",
        "started_at": None,
        "completed_at": None,
        "elapsed_seconds": None,
        "output_bundle": None,
        "child_manifest": None,
        "child_manifest_sha256": None,
        "error": None,
    }


def _execute(
    component: dict[str, Any], staging: Path, created_at: datetime, run: Callable[[Path], Path]
) -> Path:
    root = staging / "components" / component["name"]
    root.mkdir(parents=True)
    component["status"] = "running"
    component["started_at"] = created_at.isoformat()
    before = time.monotonic()
    try:
        published_bundle = run(root)
        if published_bundle.is_symlink():
            raise ValueError("child_bundle_symlink_forbidden")
        bundle = published_bundle.resolve()
        if not bundle.is_relative_to(root.resolve()):
            raise ValueError("child_bundle_outside_component_root")
        if not bundle.is_dir():
            raise ValueError("child_bundle_not_directory")
        child_manifest = bundle / _MANIFEST_RELATIVE_PATH
        if _path_contains_symlink(bundle, _MANIFEST_RELATIVE_PATH):
            raise ValueError("child_manifest_symlink_forbidden")
        if not child_manifest.is_file():
            raise ValueError(f"child_manifest_missing:{component['name']}")
        child_payload = json.loads(child_manifest.read_text(encoding="utf-8"))
        child_analysis_id, child_created_at = _validate_child_manifest(
            bundle, child_payload, created_at
        )
        component.update(
            status="completed",
            completed_at=datetime.now(UTC).isoformat(),
            elapsed_seconds=round(time.monotonic() - before, 6),
            output_bundle=bundle.relative_to(staging).as_posix(),
            child_manifest=child_manifest.relative_to(staging).as_posix(),
            child_manifest_sha256=_sha256(child_manifest),
            child_analysis_id=child_analysis_id,
            child_created_at=child_created_at,
        )
        return bundle
    except Exception as exc:
        component.update(
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            elapsed_seconds=round(time.monotonic() - before, 6),
            error={"error_type": type(exc).__name__, "message": "component_validation_failed"},
        )
        raise


def _validate_child_manifest(
    bundle: Path, payload: object, expected_created_at: datetime
) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("child_manifest_not_object")
    if not isinstance(payload.get("schema_version"), str) or not payload["schema_version"]:
        raise ValueError("child_manifest_schema_version_missing")
    raw_analysis_id = payload.get("analysis_id")
    try:
        child_analysis_id = str(UUID(str(raw_analysis_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("child_manifest_invalid_analysis_id") from exc
    raw_created_at = payload.get("created_at")
    try:
        child_created_at = datetime.fromisoformat(str(raw_created_at))
    except ValueError as exc:
        raise ValueError("child_manifest_invalid_created_at") from exc
    if child_created_at.tzinfo is None or child_created_at.utcoffset() is None:
        raise ValueError("child_manifest_created_at_requires_timezone")
    if child_created_at != expected_created_at:
        raise ValueError("child_manifest_created_at_mismatch")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("child_manifest_artifacts_missing_or_empty")
    seen: set[Path] = set()
    bundle_root = bundle.resolve()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("child_manifest_artifact_not_object")
        relative_text = artifact.get("path")
        if not isinstance(relative_text, str) or not relative_text:
            raise ValueError("child_manifest_artifact_path_invalid")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts or relative in seen:
            raise ValueError("child_manifest_artifact_path_unsafe_or_duplicate")
        seen.add(relative)
        artifact_path = bundle / relative
        if _path_contains_symlink(bundle, relative):
            raise ValueError("child_manifest_artifact_symlink_forbidden")
        resolved_artifact = artifact_path.resolve()
        if not resolved_artifact.is_relative_to(bundle_root):
            raise ValueError("child_manifest_artifact_escape")
        if not resolved_artifact.is_file():
            raise ValueError("child_manifest_artifact_missing")
        size = artifact.get("size_bytes")
        digest = artifact.get("sha256")
        if not isinstance(size, int) or size < 0 or resolved_artifact.stat().st_size != size:
            raise ValueError("child_manifest_artifact_size_mismatch")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("child_manifest_artifact_sha256_invalid")
        if _sha256(resolved_artifact) != digest:
            raise ValueError("child_manifest_artifact_sha256_mismatch")
    return child_analysis_id, child_created_at.isoformat()


def _validate_child_correlations(components: list[dict[str, Any]], parent_analysis_id: str) -> None:
    child_ids = [str(item.get("child_analysis_id")) for item in components]
    if parent_analysis_id in child_ids:
        raise ValueError("child_analysis_id_must_differ_from_parent")
    if len(set(child_ids)) != len(child_ids):
        raise ValueError("child_analysis_ids_must_be_unique")


def _revalidate_components(
    components: list[dict[str, Any]], staging: Path, expected_created_at: datetime
) -> None:
    staging_root = staging.resolve()
    for component in components:
        if component.get("status") != "completed":
            raise ValueError("component_not_completed_before_publication")
        relative_text = component.get("output_bundle")
        if not isinstance(relative_text, str) or not relative_text:
            raise ValueError("component_output_bundle_missing")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("component_output_bundle_path_unsafe")
        if _path_contains_symlink(staging, relative):
            raise ValueError("child_bundle_symlink_forbidden")
        bundle = (staging / relative).resolve()
        component_root = (staging / "components" / str(component["name"])).resolve()
        if not bundle.is_relative_to(component_root) or not bundle.is_dir():
            raise ValueError("child_bundle_outside_component_root")
        if not bundle.is_relative_to(staging_root):
            raise ValueError("child_bundle_outside_staging")
        child_manifest = bundle / _MANIFEST_RELATIVE_PATH
        if _path_contains_symlink(bundle, _MANIFEST_RELATIVE_PATH):
            raise ValueError("child_manifest_symlink_forbidden")
        if not child_manifest.is_file():
            raise ValueError(f"child_manifest_missing:{component['name']}")
        if _sha256(child_manifest) != component.get("child_manifest_sha256"):
            raise ValueError("child_manifest_changed_after_validation")
        payload = json.loads(child_manifest.read_text(encoding="utf-8"))
        child_analysis_id, child_created_at = _validate_child_manifest(
            bundle, payload, expected_created_at
        )
        if child_analysis_id != component.get("child_analysis_id"):
            raise ValueError("child_analysis_id_changed_after_validation")
        if child_created_at != component.get("child_created_at"):
            raise ValueError("child_created_at_changed_after_validation")


def _mark_failure(components: list[dict[str, Any]], exc: Exception, safe_message: str) -> None:
    for component in components:
        if component["status"] == "running":
            component["status"] = "failed"
            component["error"] = {"error_type": type(exc).__name__, "message": safe_message}
        elif component["status"] == "pending":
            component["status"] = "skipped"


def _base_manifest(
    request: CompleteAnalysisRequest,
    resolved: Mapping[str, Any],
    created_at: datetime,
    analysis_id: UUID,
    components: list[dict[str, Any]],
    rf_year_range: tuple[int, int],
) -> dict[str, Any]:
    return {
        "schema_version": COMPLETE_ANALYSIS_SCHEMA_VERSION,
        "analysis_id": str(analysis_id),
        "establishment_id": request.establishment_id,
        "created_at": created_at.isoformat(),
        "completed_at": None,
        "overall_status": "running",
        "input": {
            **_portable_path(resolved["input"]),
            "sha256": _sha256(resolved["input"]),
            "size_bytes": resolved["input"].stat().st_size,
            "source_crs": request.source_crs,
            "layer": request.vector_layer,
            "dissolve_all": request.dissolve_all,
        },
        "coverage": {
            "full_pipeline": {
                "requested_years": [request.hls_start_year, request.hls_end_year],
                "analysis_end_date": request.analysis_end_date.isoformat(),
            },
            "hampel_benchmark": {"derived_from": "full_pipeline"},
            "rf_annual_deltas": {
                "requested_years": list(rf_year_range),
                "analysis_end_date": date(rf_year_range[1], 12, 31).isoformat(),
            },
            "post_change_attribution": {"derived_from": ["full_pipeline", "rf_annual_deltas"]},
        },
        "configs": {
            key: {
                **_portable_path(resolved[key]),
                "sha256": _sha256(resolved[key]),
                "size_bytes": resolved[key].stat().st_size,
            }
            for key in ("config", "model", "hampel")
        },
        "code": _code_provenance(),
        "component_order": [item["name"] for item in components],
        "dependencies": {
            "hampel_benchmark": ["full_pipeline"],
            "rf_annual_deltas": ["hampel_benchmark"],
            "post_change_attribution": ["full_pipeline", "rf_annual_deltas"],
        },
        "components": components,
        "scientific_flags": {
            "automatic_final_assessment_generated": False,
            "attribution_status": "generated_requires_human_review",
            "rf_temporal_transfer_validated": False,
            "automatic_conversion_confirmed": False,
            "declared_land_use": request.declared_land_use,
            "declared_context_source": request.declared_context_source,
        },
        "failure": None,
    }


def _code_provenance() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=PROJECT_ROOT, text=True, capture_output=True, check=False
            )
        except OSError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    revision = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    dirty = None if status is None else bool(status)
    module = Path(__file__).resolve()
    script = PROJECT_ROOT / "scripts" / "run_complete_analysis.py"
    return {
        "git_revision": revision,
        "git_dirty": dirty,
        "orchestrator_sha256": _sha256(module),
        "script_sha256": _sha256(script) if script.is_file() else None,
    }


def _slug(value: str) -> str:
    slug = _SAFE_SLUG.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("establishment_id_has_no_safe_characters")
    return slug[:80]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parameters_hash(manifest: Mapping[str, Any]) -> str:
    parameters = {
        "schema_version": manifest.get("schema_version"),
        "establishment_id": manifest.get("establishment_id"),
        "input": manifest.get("input"),
        "coverage": manifest.get("coverage"),
        "configs": manifest.get("configs"),
        "component_order": manifest.get("component_order"),
        "dependencies": manifest.get("dependencies"),
        "scientific_flags": manifest.get("scientific_flags"),
    }
    return hashlib.sha256(
        json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _sanitize(
    message: str,
    credentials_path: Path | None = None,
    *,
    path_replacements: Mapping[Path, str] | None = None,
) -> str:
    sanitized = message
    for path, replacement in (path_replacements or {}).items():
        for representation in {str(path), path.as_posix()}:
            sanitized = re.sub(
                re.escape(representation), replacement, sanitized, flags=re.IGNORECASE
            )
    if credentials_path is not None:
        for representation in {str(credentials_path), credentials_path.as_posix()}:
            sanitized = re.sub(
                re.escape(representation), "[CREDENTIALS_PATH]", sanitized, flags=re.IGNORECASE
            )
    sanitized = _PEM_SECRET.sub("[REDACTED_PEM]", sanitized)
    sanitized = _BEARER_SECRET.sub("Bearer [REDACTED]", sanitized)
    sanitized = _QUERY_SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", sanitized)
    sanitized = _ASSIGNMENT_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", sanitized
    )
    return " ".join(sanitized.split())[:500]


def _portable_path(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return {"scope": "external", "name": resolved.name, "suffix": resolved.suffix}
    return {"scope": "project", "path": relative.as_posix()}


def _path_contains_symlink(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
