"""Lectura segura y verificable de expedientes publicados por el worker."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from deforestation_jobs.models import AnalysisJob


class ResultIntegrityError(RuntimeError):
    """Un resultado publicado no satisface su allowlist o sus hashes."""


@dataclass(frozen=True, slots=True)
class VerifiedAsset:
    asset_id: str
    category: str
    component: str | None
    event_id: str | None
    media_type: str
    size_bytes: int
    sha256: str
    path: Path

    def to_public_dict(self, analysis_id: str) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "category": self.category,
            "component": self.component,
            "event_id": self.event_id,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "download_url": f"/api/v1/analyses/{analysis_id}/assets/{self.asset_id}",
        }


class ResultCatalog:
    """Proyecta sólo archivos declarados por ``report_assets/index.json``."""

    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root.resolve()

    def report(self, job: AnalysisJob) -> dict[str, Any]:
        root, index = self._index(job)
        dataset_path = self._verified_dataset_path(root, job, index)
        return _load_object(dataset_path)

    def events(self, job: AnalysisJob) -> list[dict[str, Any]]:
        root, index = self._index(job)
        dataset = _load_object(self._verified_dataset_path(root, job, index))
        selection = dataset.get("event_selection")
        if not isinstance(selection, dict):
            raise ResultIntegrityError("event_selection_missing")
        inventory_path = selection.get("complete_event_inventory_path")
        if not isinstance(inventory_path, str):
            raise ResultIntegrityError("event_inventory_path_missing")
        entry = self._entry_by_report_path(index, inventory_path)
        path = _verified_allowlisted_path(root, entry)
        payload = _load_object(path)
        events = payload.get("events")
        if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
            raise ResultIntegrityError("event_inventory_invalid")
        return events

    def assets(self, job: AnalysisJob) -> list[VerifiedAsset]:
        root, index = self._index(job)
        entries = index.get("files")
        if not isinstance(entries, list):
            raise ResultIntegrityError("asset_allowlist_invalid")
        assets = [self._verified_asset(root, entry) for entry in entries]
        return sorted(assets, key=lambda item: item.asset_id)

    def asset(self, job: AnalysisJob, asset_id: str) -> VerifiedAsset | None:
        return next((item for item in self.assets(job) if item.asset_id == asset_id), None)

    def _index(self, job: AnalysisJob) -> tuple[Path, dict[str, Any]]:
        root = _published_result_root(self.storage_root, job.analysis_id)
        index_path = _safe_descendant(root, "report_assets/index.json")
        return root, _load_object(index_path)

    def _verified_dataset_path(self, root: Path, job: AnalysisJob, index: dict[str, Any]) -> Path:
        dataset_relative = index.get("dataset_path")
        index_digest = index.get("dataset_sha256")
        if not isinstance(dataset_relative, str) or not _is_sha256(index_digest):
            raise ResultIntegrityError("report_dataset_contract_invalid")
        path = _safe_descendant(root, dataset_relative)
        actual = _sha256(path)
        if actual != index_digest or (
            job.report_dataset_sha256 is not None and actual != job.report_dataset_sha256
        ):
            raise ResultIntegrityError("report_dataset_sha256_mismatch")
        return path

    @staticmethod
    def _entry_by_report_path(index: dict[str, Any], report_path: str) -> dict[str, Any]:
        entries = index.get("files")
        if not isinstance(entries, list):
            raise ResultIntegrityError("asset_allowlist_invalid")
        for entry in entries:
            if isinstance(entry, dict) and entry.get("report_path") == report_path:
                return entry
        raise ResultIntegrityError("event_inventory_not_allowlisted")

    @staticmethod
    def _verified_asset(root: Path, entry: object) -> VerifiedAsset:
        if not isinstance(entry, dict):
            raise ResultIntegrityError("asset_entry_invalid")
        path = _verified_allowlisted_path(root, entry)
        report_path = entry["report_path"]
        digest = entry["report_sha256"]
        category = entry.get("category")
        if not isinstance(category, str):
            raise ResultIntegrityError("asset_category_invalid")
        component = entry.get("component")
        event_id = entry.get("event_id")
        if component is not None and not isinstance(component, str):
            raise ResultIntegrityError("asset_component_invalid")
        if event_id is not None and not isinstance(event_id, str):
            raise ResultIntegrityError("asset_event_id_invalid")
        media_type = mimetypes.guess_type(report_path)[0] or "application/octet-stream"
        return VerifiedAsset(
            asset_id=hashlib.sha256(report_path.encode("utf-8")).hexdigest(),
            category=category,
            component=component,
            event_id=event_id,
            media_type=media_type,
            size_bytes=int(entry["size_bytes"]),
            sha256=digest,
            path=path,
        )


def packaged_download(storage_root: Path, analysis_id: str) -> tuple[Path, str]:
    package = _safe_descendant(
        storage_root.resolve(), f"analyses/{analysis_id}/downloads/evidence-package.zip"
    )
    metadata = _load_object(package.with_suffix(".json"))
    expected_digest = metadata.get("sha256")
    expected_size = metadata.get("size_bytes")
    if not _is_sha256(expected_digest) or not isinstance(expected_size, int):
        raise ResultIntegrityError("download_metadata_invalid")
    if package.stat().st_size != expected_size or _sha256(package) != expected_digest:
        raise ResultIntegrityError("download_integrity_mismatch")
    return package, expected_digest


def client_report_download(storage_root: Path, analysis_id: str) -> tuple[Path, str]:
    root = _published_result_root(storage_root, analysis_id)
    metadata = _load_object(_safe_descendant(root, "client_report/report.json"))
    if (
        metadata.get("schema_version") != "1.0.0"
        or metadata.get("media_type") != "application/pdf"
        or metadata.get("path") != "client_report/informe-tecnico.pdf"
    ):
        raise ResultIntegrityError("client_report_metadata_invalid")
    expected_digest = metadata.get("sha256")
    expected_size = metadata.get("size_bytes")
    if not _is_sha256(expected_digest) or not isinstance(expected_size, int) or expected_size <= 5:
        raise ResultIntegrityError("client_report_metadata_invalid")
    report = _safe_descendant(root, "client_report/informe-tecnico.pdf")
    try:
        with report.open("rb") as source:
            header = source.read(5)
    except OSError as error:
        raise ResultIntegrityError("client_report_unreadable") from error
    if (
        header != b"%PDF-"
        or report.stat().st_size != expected_size
        or _sha256(report) != expected_digest
    ):
        raise ResultIntegrityError("client_report_integrity_mismatch")
    return report, expected_digest


def purge_expired_private_objects(storage_root: Path, analysis_ids: list[str]) -> int:
    """Borra sólo prefijos privados por UUID; los bundles científicos no se tocan."""
    root = storage_root.resolve()
    removed = 0
    for analysis_id in analysis_ids:
        UUID(analysis_id)
        candidate = _safe_target(root, f"analyses/{analysis_id}")
        if candidate.exists():
            shutil.rmtree(candidate)
            removed += 1
    return removed


def _published_result_root(storage_root: Path, analysis_id: str) -> Path:
    UUID(analysis_id)
    return _safe_descendant(storage_root.resolve(), f"analyses/{analysis_id}/results")


def _verified_allowlisted_path(root: Path, entry: dict[str, Any]) -> Path:
    report_path = entry.get("report_path")
    expected_digest = entry.get("report_sha256")
    expected_size = entry.get("size_bytes")
    if (
        not isinstance(report_path, str)
        or not _is_sha256(expected_digest)
        or not isinstance(expected_size, int)
        or expected_size < 0
    ):
        raise ResultIntegrityError("asset_entry_invalid")
    path = _safe_descendant(root, report_path)
    if path.stat().st_size != expected_size or _sha256(path) != expected_digest:
        raise ResultIntegrityError("asset_integrity_mismatch")
    return path


def _safe_descendant(root: Path, relative_value: str) -> Path:
    candidate = _safe_target(root, relative_value)
    if not candidate.is_file() and candidate != root and not candidate.is_dir():
        raise ResultIntegrityError("result_path_invalid")
    return candidate


def _safe_target(root: Path, relative_value: str) -> Path:
    relative = Path(relative_value)
    candidate = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not candidate.is_relative_to(root):
        raise ResultIntegrityError("unsafe_result_path")
    return candidate


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResultIntegrityError("result_json_invalid") from error
    if not isinstance(payload, dict):
        raise ResultIntegrityError("result_json_not_object")
    return payload


def _sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        raise ResultIntegrityError("result_file_unreadable") from error


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
