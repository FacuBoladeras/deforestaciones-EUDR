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

from deforestation_pipeline.agricultural_collector import (
    RasterProvider,
    load_agricultural_collector_config,
    make_dynamic_world_raster_provider,
    materialize_agricultural_evidence_collection,
)
from deforestation_pipeline.agricultural_evidence import (
    AgriculturalEvidenceDocument,
    AgriculturalEvidencePolicy,
    evaluate_agricultural_evidence,
    load_agricultural_evidence_document,
    load_agricultural_evidence_policy,
)
from deforestation_pipeline.agricultural_persistence import (
    load_agricultural_persistence_config,
    materialize_agricultural_persistence,
)
from deforestation_pipeline.config import load_config, load_forest_model_config
from deforestation_pipeline.gee import (
    GeeSession,
    authenticate_earth_engine,
    authenticate_earth_engine_user_oauth,
)
from deforestation_pipeline.hampel_benchmark import (
    HampelDiagnosticUnavailableError,
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
from deforestation_pipeline.report_figures import (
    REPORT_FIGURE_COLLECTION_SCHEMA_VERSION,
    REPORT_FIGURE_SELECTION_POLICY_VERSION,
    materialize_report_figure_collection,
)
from deforestation_pipeline.rf_model_release import verify_local_rf_model_release

COMPLETE_ANALYSIS_SCHEMA_VERSION = "2.3.0"
COMPONENT_STATUS_SCHEMA_VERSION = "1.0.0"
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
    agricultural_evidence_policy_path: Path = PROJECT_ROOT / "configs/agricultural-evidence.yml"
    agricultural_collector_config_path: Path = PROJECT_ROOT / "configs/agricultural-collector.yml"
    agricultural_persistence_config_path: Path = (
        PROJECT_ROOT / "configs/agricultural-persistence.yml"
    )
    catalog_path: Path = PROJECT_ROOT / "data/catalog.yml"
    licenses_path: Path = PROJECT_ROOT / "data/licenses.yml"
    agricultural_evidence_path: Path | None = None
    agricultural_persistence_bundle_path: Path | None = None
    analysis_end_date: date = DEFAULT_ANALYSIS_END_DATE
    hls_start_year: int = 2020
    hls_end_year: int = 2025
    credentials_path: Path | None = None
    gee_project: str | None = None
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
    agricultural_collector_runner: Runner = materialize_agricultural_evidence_collection,
    agricultural_persistence_runner: Runner = materialize_agricultural_persistence,
    attribution_runner: Runner = materialize_post_change_attribution,
    raster_provider: RasterProvider | None = None,
    gee_session: GeeSession | None = None,
    gee_authenticator: Callable[[Path], GeeSession] = authenticate_earth_engine,
    gee_oauth_authenticator: Callable[..., GeeSession] = authenticate_earth_engine_user_oauth,
    agricultural_provider_factory: Callable[..., RasterProvider] = (
        make_dynamic_world_raster_provider
    ),
    agricultural_evidence_loader: Callable[
        [Path], AgriculturalEvidenceDocument
    ] = load_agricultural_evidence_document,
    agricultural_policy_loader: Callable[
        [Path], AgriculturalEvidencePolicy
    ] = load_agricultural_evidence_policy,
    agricultural_collector_loader: Callable[[Path], object] = (load_agricultural_collector_config),
    agricultural_persistence_loader: Callable[[Path], object] = (
        load_agricultural_persistence_config
    ),
) -> Path:
    """Ejecuta el DAG completo y publica todos sus bundles bajo un parent atómico."""
    started = created_at or datetime.now(UTC)
    if started.tzinfo is None or started.utcoffset() is None:
        raise ValueError("created_at_requires_timezone")
    resolved = _validate_request(request, started)
    # Parsear todos los contratos antes de cualquier acceso remoto evita
    # corridas parciales por YAML inválido.
    pipeline_config = config_loader(resolved["config"])
    model_config = model_loader(resolved["model"])
    if getattr(model_config, "schema_version", None) == "1.1.0":
        verify_local_rf_model_release(
            model_config,
            project_root=PROJECT_ROOT,
            load_bundle=False,
        )
    hampel_config = hampel_loader(resolved["hampel"])
    agricultural_policy = agricultural_policy_loader(resolved["agricultural_policy"])
    agricultural_mode = _agricultural_mode(resolved)
    if agricultural_mode == "auto":
        agricultural_collector_loader(resolved["agricultural_collector"])
        agricultural_persistence_loader(resolved["agricultural_persistence"])
    agricultural_evidence = (
        agricultural_evidence_loader(resolved["agricultural_evidence"])
        if resolved["agricultural_evidence"] is not None
        else None
    )
    evaluated_agricultural_evidence = (
        evaluate_agricultural_evidence(agricultural_evidence, agricultural_policy)
        if agricultural_evidence is not None
        else {}
    )
    gee_authentication_mode = (
        "injected_session"
        if gee_session is not None
        else "service_account"
        if resolved["credentials"] is not None
        else "user_oauth"
        if resolved["gee_project"] is not None
        else "unavailable"
    )
    active_session = gee_session
    active_raster_provider = raster_provider
    configured_rf_years = getattr(model_config, "supported_observation_years", None)
    if not isinstance(configured_rf_years, (list, tuple)):
        raise ValueError("rf_supported_observation_years_missing")
    rf_years = tuple(configured_rf_years)
    _validate_rf_years(rf_years, started.year)
    identifier = analysis_id or uuid4()
    slug = _slug(request.establishment_id)
    run_name = f"{slug}__{started.strftime('%Y%m%dT%H%M%S%fZ')}__{str(identifier)[:8]}"
    components = [
        _component("full_pipeline"),
        _component("hampel_benchmark", required_for_publication=False),
        _component("rf_annual_deltas"),
    ]
    if agricultural_mode == "auto":
        components.extend(
            [
                _component("agricultural_collection"),
                _component("agricultural_persistence"),
            ]
        )
    components.append(_component("post_change_attribution"))
    manifest = _base_manifest(
        request,
        resolved,
        started,
        identifier,
        components,
        (min(rf_years), max(rf_years)),
        gee_authentication_mode,
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
        if active_session is None and resolved["credentials"] is not None:
            active_session = gee_authenticator(resolved["credentials"])
        elif active_session is None and resolved["gee_project"] is not None:
            active_session = gee_oauth_authenticator(project=resolved["gee_project"])
        if agricultural_mode == "auto" and active_raster_provider is None:
            if active_session is None:
                raise ValueError("gee_session_required_for_automatic_agriculture")
            output_config = getattr(pipeline_config, "output", None)
            if output_config is None:
                raise ValueError("pipeline_output_config_missing")
            active_raster_provider = agricultural_provider_factory(
                session=active_session,
                output_config=output_config,
            )
        full = _execute(
            components[0],
            staging,
            started,
            lambda root: pipeline_runner(
                input_path=resolved["input"],
                output_root=root,
                config_path=resolved["config"],
                catalog_path=resolved["catalog"],
                forest_model_config_path=None,
                source_crs=request.source_crs,
                establishment_id=request.establishment_id,
                analysis_end_date=request.analysis_end_date,
                created_at=started,
                gee_credentials_path=resolved["credentials"],
                gee_session=active_session,
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
        try:
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
        except HampelDiagnosticUnavailableError as exc:
            _mark_diagnostic_unavailable(components[1], exc)
            manifest["scientific_warnings"].append(
                {"component": "hampel_benchmark", "code": str(exc)}
            )
        deltas = _execute(
            components[2],
            staging,
            started,
            lambda root: delta_runner(
                input_path=resolved["input"],
                output_root=root,
                config_path=resolved["config"],
                catalog_path=resolved["catalog"],
                forest_model_config_path=resolved["model"],
                source_crs=request.source_crs,
                establishment_id=request.establishment_id,
                analysis_end_date=date(max(rf_years), 12, 31),
                created_at=started,
                gee_credentials_path=resolved["credentials"],
                gee_session=active_session,
                generate_hls_composite=False,
                generate_forest_baseline=False,
                generate_rf_deltas=True,
                hls_seasonal_request=None,
                generate_disturbance_detection=False,
                vector_layer=request.vector_layer,
                dissolve_all=request.dissolve_all,
            ),
        )
        persistence_bundle = resolved["agricultural_persistence_bundle"]
        attribution_component_index = 3
        if agricultural_mode == "auto":
            collection = _execute(
                components[3],
                staging,
                started,
                lambda root: agricultural_collector_runner(
                    source_event_bundle=full,
                    output_root=root,
                    establishment_id=request.establishment_id,
                    analysis_end_date=request.analysis_end_date,
                    config_path=resolved["agricultural_collector"],
                    catalog_path=resolved["catalog"],
                    licenses_path=resolved["licenses"],
                    created_at=started,
                    raster_provider=active_raster_provider,
                ),
            )
            persistence_bundle = _execute(
                components[4],
                staging,
                started,
                lambda root: agricultural_persistence_runner(
                    source_collection_bundle=collection,
                    output_root=root,
                    config_path=resolved["agricultural_persistence"],
                    created_at=started,
                ),
            )
            manifest["scientific_flags"]["persistent_agricultural_evidence_provided"] = True
            manifest["scientific_flags"]["agricultural_evidence_generated_in_same_run"] = True
            attribution_component_index = 5
        _execute(
            components[attribution_component_index],
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
                    agricultural_use_evidence=evaluated_agricultural_evidence,
                ),
                created_at=started,
                source_agricultural_bundle=persistence_bundle,
                agricultural_policy=agricultural_policy,
            ),
        )
        _revalidate_components(components, staging, started)
        _validate_child_correlations(components, str(identifier))
        manifest["overall_status"] = (
            "partial"
            if any(item["status"] == "diagnostic_unavailable" for item in components)
            else "complete"
        )
        manifest["completed_at"] = datetime.now(UTC).isoformat()
        _materialize_component_status_receipts(
            components, staging, manifest["dependencies"], manifest["completed_at"]
        )
        manifest["report_assets"] = materialize_report_figure_collection(
            staging=staging,
            components=components,
            overall_status=manifest["overall_status"],
            recorded_at=manifest["completed_at"],
            parent_analysis_id=manifest["analysis_id"],
        )
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
                resolved["agricultural_policy"]: "[AGRICULTURAL_POLICY_PATH]",
                resolved["agricultural_collector"]: "[AGRICULTURAL_COLLECTOR_CONFIG_PATH]",
                resolved["agricultural_persistence"]: ("[AGRICULTURAL_PERSISTENCE_CONFIG_PATH]"),
                resolved["catalog"]: "[CATALOG_PATH]",
                resolved["licenses"]: "[LICENSES_PATH]",
                **(
                    {resolved["agricultural_evidence"]: "[AGRICULTURAL_EVIDENCE_PATH]"}
                    if resolved["agricultural_evidence"] is not None
                    else {}
                ),
                **(
                    {
                        resolved["agricultural_persistence_bundle"]: (
                            "[AGRICULTURAL_PERSISTENCE_BUNDLE]"
                        )
                    }
                    if resolved["agricultural_persistence_bundle"] is not None
                    else {}
                ),
            },
        )
        _mark_failure(components, exc, safe_message)
        manifest["overall_status"] = (
            "partial" if any(item["status"] == "completed" for item in components) else "failed"
        )
        manifest["completed_at"] = datetime.now(UTC).isoformat()
        manifest["failure"] = {"error_type": type(exc).__name__, "message": safe_message}
        _materialize_component_status_receipts(
            components, staging, manifest["dependencies"], manifest["completed_at"]
        )
        if manifest.get("report_assets") is None:
            try:
                manifest["report_assets"] = materialize_report_figure_collection(
                    staging=staging,
                    components=components,
                    overall_status=manifest["overall_status"],
                    recorded_at=manifest["completed_at"],
                    parent_analysis_id=manifest["analysis_id"],
                )
            except Exception as report_exc:
                manifest["report_assets"] = {
                    "schema_version": REPORT_FIGURE_COLLECTION_SCHEMA_VERSION,
                    "selection_policy_version": REPORT_FIGURE_SELECTION_POLICY_VERSION,
                    "status": "unavailable",
                    "error_code": "report_figure_collection_failed",
                    "error": _safe_report_assets_error(report_exc),
                    "figure_count": 0,
                }
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
        ("agricultural_policy", request.agricultural_evidence_policy_path),
        ("agricultural_collector", request.agricultural_collector_config_path),
        ("agricultural_persistence", request.agricultural_persistence_config_path),
        ("catalog", request.catalog_path),
        ("licenses", request.licenses_path),
    ):
        candidate = path.resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"required_file_missing:{key}:{candidate.name}")
        resolved[key] = candidate
    credentials = request.credentials_path.resolve() if request.credentials_path else None
    if credentials is not None and not credentials.is_file():
        raise FileNotFoundError(f"credentials_file_missing:{credentials.name}")
    resolved["credentials"] = credentials
    gee_project = request.gee_project.strip() if request.gee_project is not None else None
    if gee_project == "":
        raise ValueError("gee_project_empty")
    if credentials is not None and gee_project is not None:
        raise ValueError("gee_authentication_modes_are_mutually_exclusive")
    resolved["gee_project"] = gee_project
    agricultural_evidence = (
        request.agricultural_evidence_path.resolve()
        if request.agricultural_evidence_path is not None
        else None
    )
    if agricultural_evidence is not None and not agricultural_evidence.is_file():
        raise FileNotFoundError(
            f"required_file_missing:agricultural_evidence:{agricultural_evidence.name}"
        )
    resolved["agricultural_evidence"] = agricultural_evidence
    persistence_bundle = (
        request.agricultural_persistence_bundle_path.resolve()
        if request.agricultural_persistence_bundle_path is not None
        else None
    )
    if agricultural_evidence is not None and persistence_bundle is not None:
        raise ValueError("agricultural evidence JSON y persistence bundle son excluyentes")
    if persistence_bundle is not None:
        manifest = persistence_bundle / _MANIFEST_RELATIVE_PATH
        if not persistence_bundle.is_dir() or not manifest.is_file():
            raise FileNotFoundError("required_bundle_missing:agricultural_persistence")
    resolved["agricultural_persistence_bundle"] = persistence_bundle
    return resolved


def _agricultural_mode(resolved: Mapping[str, Any]) -> str:
    """Usa evidencia externa sólo cuando fue pedida de forma explícita."""
    if resolved["agricultural_evidence"] is not None:
        return "precomputed_evidence"
    if resolved["agricultural_persistence_bundle"] is not None:
        return "precomputed_persistence"
    return "auto"


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


def _component(name: str, *, required_for_publication: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "required_for_publication": required_for_publication,
        "status": "pending",
        "started_at": None,
        "completed_at": None,
        "elapsed_seconds": None,
        "output_bundle": None,
        "child_manifest": None,
        "child_manifest_sha256": None,
        "status_receipt": None,
        "status_receipt_sha256": None,
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
        error_code = getattr(exc, "code", None)
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-z0-9_]+", error_code):
            error_code = "component_validation_failed"
        component.update(
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            elapsed_seconds=round(time.monotonic() - before, 6),
            error={"error_type": type(exc).__name__, "message": error_code},
        )
        raise


def _mark_diagnostic_unavailable(
    component: dict[str, Any], exc: HampelDiagnosticUnavailableError
) -> None:
    component.update(
        status="diagnostic_unavailable",
        error={"error_type": type(exc).__name__, "message": str(exc)},
    )


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
    child_ids = [
        str(item["child_analysis_id"]) for item in components if item.get("status") == "completed"
    ]
    if parent_analysis_id in child_ids:
        raise ValueError("child_analysis_id_must_differ_from_parent")
    if len(set(child_ids)) != len(child_ids):
        raise ValueError("child_analysis_ids_must_be_unique")


def _revalidate_components(
    components: list[dict[str, Any]], staging: Path, expected_created_at: datetime
) -> None:
    staging_root = staging.resolve()
    for component in components:
        if (
            component.get("status") == "diagnostic_unavailable"
            and component.get("required_for_publication") is False
        ):
            continue
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
    failed_at = datetime.now(UTC).isoformat()
    for component in components:
        if component["status"] == "running":
            component["status"] = "failed"
            component["completed_at"] = failed_at
            component["error"] = {"error_type": type(exc).__name__, "message": safe_message}
        elif component["status"] == "pending":
            component["status"] = "skipped"
            component["completed_at"] = failed_at


def _safe_report_assets_error(exc: Exception) -> dict[str, str]:
    error_type = type(exc).__name__
    if not re.fullmatch(r"[A-Za-z0-9_.]+", error_type):
        error_type = "ReportFigureCollectionError"
    candidate = str(exc)
    code = (
        candidate if re.fullmatch(r"[a-z0-9_]+", candidate) else "report_figure_collection_failed"
    )
    return {"error_type": error_type, "code": code}


def _materialize_component_status_receipts(
    components: list[dict[str, Any]],
    staging: Path,
    dependencies: Mapping[str, list[str]],
    recorded_at: str,
) -> None:
    """Materializa estados sin bundle para que una carpeta vacía sea explicable."""
    for component in components:
        if component["status"] == "completed":
            continue
        name = str(component["name"])
        component_root = staging / "components" / name
        component_root.mkdir(parents=True, exist_ok=True)
        relative = Path("components") / name / "component_status.json"
        error = component.get("error")
        error_type = "DependencyUnavailable"
        error_code = "dependency_not_completed"
        if isinstance(error, dict):
            candidate_type = error.get("error_type")
            if isinstance(candidate_type, str) and re.fullmatch(r"[A-Za-z0-9_.]+", candidate_type):
                error_type = candidate_type
            candidate_code = error.get("message")
            if isinstance(candidate_code, str) and re.fullmatch(r"[a-z0-9_]+", candidate_code):
                error_code = candidate_code
            else:
                error_code = "component_validation_failed"
        receipt = {
            "schema_version": COMPONENT_STATUS_SCHEMA_VERSION,
            "component": name,
            "status": component["status"],
            "required_for_publication": component["required_for_publication"],
            "started_at": component["started_at"],
            "completed_at": component["completed_at"],
            "recorded_at": recorded_at,
            "dependencies": dependencies.get(name, []),
            "error": {"error_type": error_type, "code": error_code},
        }
        receipt_path = staging / relative
        _write_json_atomic(receipt_path, receipt)
        component["status_receipt"] = relative.as_posix()
        component["status_receipt_sha256"] = _sha256(receipt_path)


def _base_manifest(
    request: CompleteAnalysisRequest,
    resolved: Mapping[str, Any],
    created_at: datetime,
    analysis_id: UUID,
    components: list[dict[str, Any]],
    rf_year_range: tuple[int, int],
    gee_authentication_mode: str,
) -> dict[str, Any]:
    agricultural_mode = _agricultural_mode(resolved)
    automatic_agriculture = agricultural_mode == "auto"
    dependencies: dict[str, list[str]] = {
        "hampel_benchmark": ["full_pipeline"],
        "rf_annual_deltas": ["full_pipeline"],
    }
    if automatic_agriculture:
        dependencies.update(
            {
                "agricultural_collection": ["full_pipeline"],
                "agricultural_persistence": ["agricultural_collection"],
                "post_change_attribution": [
                    "full_pipeline",
                    "rf_annual_deltas",
                    "agricultural_persistence",
                ],
            }
        )
    else:
        dependencies["post_change_attribution"] = ["full_pipeline", "rf_annual_deltas"]
    coverage: dict[str, Any] = {
        "full_pipeline": {
            "requested_years": [request.hls_start_year, request.hls_end_year],
            "requested_analysis_end_date": request.analysis_end_date.isoformat(),
            "effective_analysis_end_date": date(request.hls_end_year, 11, 30).isoformat(),
        },
        "hampel_benchmark": {"derived_from": "full_pipeline"},
        "rf_annual_deltas": {
            "requested_years": list(rf_year_range),
            "effective_analysis_end_date": date(rf_year_range[1], 11, 30).isoformat(),
        },
    }
    if automatic_agriculture:
        coverage.update(
            {
                "agricultural_collection": {
                    "event_onset_relative": True,
                    "analysis_end_date_inclusive": request.analysis_end_date.isoformat(),
                    "cadence": "calendar_month",
                },
                "agricultural_persistence": {"derived_from": "agricultural_collection"},
                "post_change_attribution": {
                    "derived_from": [
                        "full_pipeline",
                        "rf_annual_deltas",
                        "agricultural_persistence",
                    ]
                },
            }
        )
    else:
        coverage["post_change_attribution"] = {
            "derived_from": ["full_pipeline", "rf_annual_deltas"]
        }
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
        "coverage": coverage,
        "configs": {
            key: {
                **_portable_path(resolved[key]),
                "sha256": _sha256(resolved[key]),
                "size_bytes": resolved[key].stat().st_size,
            }
            for key in (
                "config",
                "model",
                "hampel",
                "agricultural_policy",
                "agricultural_collector",
                "agricultural_persistence",
                "catalog",
                "licenses",
            )
        },
        "agricultural_evidence_input": (
            None
            if resolved["agricultural_evidence"] is None
            else {
                **_portable_path(resolved["agricultural_evidence"]),
                "sha256": _sha256(resolved["agricultural_evidence"]),
                "size_bytes": resolved["agricultural_evidence"].stat().st_size,
            }
        ),
        "agricultural_persistence_input": (
            None
            if resolved["agricultural_persistence_bundle"] is None
            else {
                "bundle_name": resolved["agricultural_persistence_bundle"].name,
                "scope": "external",
                "manifest_sha256": _sha256(
                    resolved["agricultural_persistence_bundle"] / _MANIFEST_RELATIVE_PATH
                ),
            }
        ),
        "code": _code_provenance(),
        "gee_authentication_mode": gee_authentication_mode,
        "agricultural_mode": agricultural_mode,
        "component_order": [item["name"] for item in components],
        "dependencies": dependencies,
        "components": components,
        "scientific_flags": {
            "automatic_final_assessment_generated": False,
            "attribution_status": "generated_requires_human_review",
            "rf_temporal_transfer_validated": False,
            "automatic_conversion_confirmed": False,
            "declared_land_use": request.declared_land_use,
            "declared_context_source": request.declared_context_source,
            "agricultural_evidence_policy_applied": True,
            "agricultural_evidence_provided": resolved["agricultural_evidence"] is not None,
            "persistent_agricultural_evidence_provided": (
                resolved["agricultural_persistence_bundle"] is not None
            ),
            "agricultural_evidence_generation_requested": automatic_agriculture,
            "agricultural_evidence_generated_in_same_run": False,
        },
        "scientific_warnings": [],
        "report_assets": None,
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
        "agricultural_evidence_input": manifest.get("agricultural_evidence_input"),
        "agricultural_persistence_input": manifest.get("agricultural_persistence_input"),
        "agricultural_mode": manifest.get("agricultural_mode"),
        "gee_authentication_mode": manifest.get("gee_authentication_mode"),
        "component_order": manifest.get("component_order"),
        "dependencies": manifest.get("dependencies"),
        "scientific_flags": manifest.get("scientific_flags"),
        "scientific_warnings": manifest.get("scientific_warnings"),
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
