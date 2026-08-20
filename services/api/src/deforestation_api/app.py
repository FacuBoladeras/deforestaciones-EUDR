"""Factoría FastAPI sin estado global ni ejecución científica en requests."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from deforestation_api import __version__
from deforestation_api.geometry_service import (
    GeometryComplexityError,
    GeometryValidationError,
    assess_geometry,
    canonical_feature,
    load_jurisdiction_boundary,
)
from deforestation_api.middleware import RequestSizeLimitMiddleware
from deforestation_api.models import (
    AnalysisStatusResponse,
    ApiFeature,
    ApiGeometry,
    AssetPageResponse,
    CreateAnalysisRequest,
    CreateAnalysisResponse,
    ErrorResponse,
    EventPageResponse,
    GeometryDocument,
    GeometryValidationResponse,
    HealthResponse,
)
from deforestation_api.results import (
    ResultCatalog,
    ResultIntegrityError,
    client_report_download,
    packaged_download,
    purge_expired_private_objects,
)
from deforestation_api.settings import ApiSettings
from deforestation_jobs.models import TERMINAL_STATUSES, AnalysisJob, JobStatus
from deforestation_jobs.repository import IdempotencyConflictError, SQLiteJobRepository


class ApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    active_settings = settings or ApiSettings.from_environment()
    repository = SQLiteJobRepository(active_settings.database_path)
    result_catalog = ResultCatalog(active_settings.storage_root)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        repository.initialize()
        purge_expired_private_objects(
            active_settings.storage_root,
            repository.expired_analysis_ids(),
        )
        _app.state.jurisdiction_boundary = load_jurisdiction_boundary(
            active_settings.jurisdiction_boundary_path
        )
        yield

    app = FastAPI(
        title="Deforestation Evidence API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.state.repository = repository
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=active_settings.max_request_bytes)

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, error: ApiError) -> JSONResponse:
        return _error_response(error.status_code, error.code, error.message, error.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = [_validation_detail(item) for item in error.errors()]
        return _error_response(
            422, "invalid_request", "La solicitud no cumple el contrato", details
        )

    @app.get("/health", response_model=HealthResponse, tags=["operations"])
    def health() -> HealthResponse:
        return HealthResponse(version=__version__)

    @app.post(
        "/api/v1/geometries/validate",
        response_model=GeometryValidationResponse,
        responses={413: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
        tags=["geometries"],
    )
    def validate_geometry_endpoint(document: GeometryDocument, request: Request) -> Any:
        assessment = _assess(document.root, request)
        return GeometryValidationResponse(
            geometry_type=assessment.geometry_type,
            repair_required=assessment.repair_performed,
            area_ha_estimate=assessment.area_ha,
            warnings=list(assessment.warnings),
        )

    @app.post(
        "/api/v1/analyses",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=CreateAnalysisResponse,
        responses={409: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
        tags=["analyses"],
    )
    def create_analysis(
        payload: CreateAnalysisRequest,
        request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    ) -> CreateAnalysisResponse:
        _assess(payload.geometry, request)
        feature = canonical_feature(payload.geometry)
        feature_bytes = _canonical_json_bytes(feature)
        request_bytes = _canonical_json_bytes(
            {
                "establishment_id": payload.establishment_id,
                "geometry": feature,
                "declared_land_use": payload.declared_land_use,
                "declared_context_source": payload.declared_context_source,
            }
        )
        identifier = uuid4()
        object_key = Path("analyses") / str(identifier) / "input.geojson"
        object_path = _safe_object_path(active_settings.storage_root, object_key)
        _write_atomic(object_path, feature_bytes)
        now = datetime.now(UTC)
        job = AnalysisJob(
            analysis_id=str(identifier),
            owner_id=active_settings.owner_id,
            idempotency_key=str(idempotency_key),
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
            establishment_id=payload.establishment_id,
            input_object_key=object_key.as_posix(),
            input_sha256=hashlib.sha256(feature_bytes).hexdigest(),
            status=JobStatus.QUEUED,
            stage="queued",
            attempt=0,
            created_at=now,
            updated_at=now,
            configuration_versions=json.dumps(
                {"api": __version__, "analysis_contract": "1.0.0"}, sort_keys=True
            ),
            code_revision=active_settings.code_revision,
            expires_at=now + timedelta(days=active_settings.retention_days),
            declared_land_use=payload.declared_land_use,
            declared_context_source=payload.declared_context_source,
        )
        try:
            stored, created = repository.create_or_get(job)
        except IdempotencyConflictError as error:
            _remove_proposed_input(object_path, active_settings.storage_root)
            raise ApiError(
                409,
                "idempotency_conflict",
                "La Idempotency-Key ya fue usada con otra solicitud",
            ) from error
        if not created:
            _remove_proposed_input(object_path, active_settings.storage_root)
        return CreateAnalysisResponse(
            analysis_id=UUID(stored.analysis_id),
            status=stored.status.value,
            status_url=f"/api/v1/analyses/{stored.analysis_id}",
            created_at=stored.created_at,
        )

    @app.get(
        "/api/v1/analyses/{analysis_id}",
        response_model=AnalysisStatusResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["analyses"],
    )
    def analysis_status(analysis_id: UUID) -> AnalysisStatusResponse:
        job = _owned_job(repository, active_settings, analysis_id)
        return AnalysisStatusResponse.model_validate(job.to_public_dict())

    @app.get(
        "/api/v1/analyses/{analysis_id}/report",
        response_model=dict[str, Any],
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["results"],
    )
    def analysis_report(analysis_id: UUID) -> dict[str, Any]:
        job = _ready_job(repository, active_settings, analysis_id)
        try:
            return result_catalog.report(job)
        except ResultIntegrityError as error:
            raise _result_integrity_api_error(error) from error

    @app.get(
        "/api/v1/analyses/{analysis_id}/report.pdf",
        response_class=FileResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["results"],
    )
    def analysis_report_pdf(analysis_id: UUID) -> FileResponse:
        job = _ready_job(repository, active_settings, analysis_id)
        try:
            report, digest = client_report_download(
                active_settings.storage_root,
                job.analysis_id,
            )
        except ResultIntegrityError as error:
            raise ApiError(
                409,
                "report_pdf_not_ready",
                "El informe PDF no está disponible o no supera integridad",
            ) from error
        establishment = _safe_filename_component(job.establishment_id)
        return FileResponse(
            report,
            media_type="application/pdf",
            filename=f"informe-tecnico-{establishment}-{job.analysis_id[:8]}.pdf",
            headers={"ETag": f'"{digest}"', "Cache-Control": "private, immutable"},
        )

    @app.get(
        "/api/v1/analyses/{analysis_id}/events",
        response_model=EventPageResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["results"],
    )
    def analysis_events(
        analysis_id: UUID,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> EventPageResponse:
        job = _ready_job(repository, active_settings, analysis_id)
        try:
            events = result_catalog.events(job)
        except ResultIntegrityError as error:
            raise _result_integrity_api_error(error) from error
        items, next_page = _paginate(events, page, page_size)
        return EventPageResponse(
            items=items,
            page=page,
            page_size=page_size,
            total=len(events),
            next_page=next_page,
        )

    @app.get(
        "/api/v1/analyses/{analysis_id}/assets",
        response_model=AssetPageResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["results"],
    )
    def analysis_assets(
        analysis_id: UUID,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> AssetPageResponse:
        job = _ready_job(repository, active_settings, analysis_id)
        try:
            assets = result_catalog.assets(job)
        except ResultIntegrityError as error:
            raise _result_integrity_api_error(error) from error
        public = [item.to_public_dict(job.analysis_id) for item in assets]
        items, next_page = _paginate(public, page, page_size)
        return AssetPageResponse.model_validate(
            {
                "items": items,
                "page": page,
                "page_size": page_size,
                "total": len(public),
                "next_page": next_page,
            }
        )

    @app.get(
        "/api/v1/analyses/{analysis_id}/assets/{asset_id}",
        response_class=FileResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["results"],
    )
    def analysis_asset_download(analysis_id: UUID, asset_id: str) -> FileResponse:
        job = _ready_job(repository, active_settings, analysis_id)
        try:
            asset = result_catalog.asset(job, asset_id)
        except ResultIntegrityError as error:
            raise _result_integrity_api_error(error) from error
        if asset is None:
            raise ApiError(404, "asset_not_found", "El recurso no existe en la allowlist")
        return FileResponse(
            asset.path,
            media_type=asset.media_type,
            filename=f"asset-{asset.asset_id}{asset.path.suffix}",
            headers={"ETag": f'"{asset.sha256}"', "Cache-Control": "private, immutable"},
        )

    @app.get(
        "/api/v1/analyses/{analysis_id}/download",
        response_class=FileResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["results"],
    )
    def analysis_package_download(analysis_id: UUID) -> FileResponse:
        job = _ready_job(repository, active_settings, analysis_id)
        try:
            package, digest = packaged_download(active_settings.storage_root, job.analysis_id)
        except ResultIntegrityError as error:
            raise ApiError(
                409,
                "download_not_ready",
                "La descarga empaquetada no está disponible o no supera integridad",
            ) from error
        return FileResponse(
            package,
            media_type="application/zip",
            filename=f"evidence-{job.analysis_id}.zip",
            headers={"ETag": f'"{digest}"', "Cache-Control": "private, immutable"},
        )

    @app.post(
        "/api/v1/analyses/{analysis_id}/cancel",
        response_model=AnalysisStatusResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["analyses"],
    )
    def cancel_analysis(analysis_id: UUID) -> AnalysisStatusResponse:
        _owned_job(repository, active_settings, analysis_id)
        job = repository.request_cancellation(str(analysis_id))
        if job is None:  # pragma: no cover - protegido por la lectura anterior
            raise ApiError(404, "analysis_not_found", "El análisis no existe")
        return AnalysisStatusResponse.model_validate(job.to_public_dict())

    return app


def _owned_job(
    repository: SQLiteJobRepository, settings: ApiSettings, analysis_id: UUID
) -> AnalysisJob:
    job = repository.get(str(analysis_id))
    if job is None or job.owner_id != settings.owner_id:
        raise ApiError(404, "analysis_not_found", "El análisis no existe")
    if job.expires_at <= datetime.now(UTC):
        raise ApiError(410, "analysis_expired", "El análisis superó su período de retención")
    return job


def _ready_job(
    repository: SQLiteJobRepository, settings: ApiSettings, analysis_id: UUID
) -> AnalysisJob:
    job = _owned_job(repository, settings, analysis_id)
    if job.status not in {JobStatus.COMPLETED, JobStatus.PARTIAL}:
        message = (
            "El análisis todavía no tiene resultados publicados"
            if job.status not in TERMINAL_STATUSES
            else "El análisis finalizó sin resultados publicables"
        )
        raise ApiError(409, "analysis_not_ready", message)
    return job


def _safe_filename_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")
    return normalized[:80] or "establecimiento"


def _paginate(items: list[Any], page: int, page_size: int) -> tuple[list[Any], int | None]:
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], page + 1 if end < len(items) else None


def _result_integrity_api_error(error: ResultIntegrityError) -> ApiError:
    return ApiError(
        409,
        "result_integrity_error",
        "El resultado publicado no supera las verificaciones de integridad",
        [str(error)],
    )


def _assess(document: ApiGeometry | ApiFeature, request: Request):  # type: ignore[no-untyped-def]
    try:
        return assess_geometry(
            document,
            jurisdiction_boundary=request.app.state.jurisdiction_boundary,
            max_vertices=request.app.state.settings.max_vertices,
        )
    except GeometryComplexityError as error:
        raise ApiError(
            413, "geometry_too_complex", "La geometría supera el límite de vértices"
        ) from error
    except GeometryValidationError as error:
        raise ApiError(
            422, "invalid_geometry", "La geometría no es utilizable", list(error.violations)
        ) from error


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _safe_object_path(storage_root: Path, object_key: Path) -> Path:
    root = storage_root.resolve()
    candidate = (root / object_key).resolve()
    if object_key.is_absolute() or not candidate.is_relative_to(root):
        raise RuntimeError("unsafe_server_object_key")
    return candidate


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _remove_proposed_input(path: Path, storage_root: Path) -> None:
    path.unlink(missing_ok=True)
    root = storage_root.resolve()
    current = path.parent
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _validation_detail(item: dict[str, Any]) -> str:
    location = ".".join(str(part) for part in item.get("loc", ()))
    return f"{location}: {item.get('msg', 'valor inválido')}"


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or []}},
    )
