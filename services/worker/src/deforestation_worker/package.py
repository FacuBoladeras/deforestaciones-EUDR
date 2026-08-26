"""Empaquetado privado y determinístico de evidencia ya publicada."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import zipfile
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

_CLIENT_REPORT_PATH = "client_report/informe-tecnico.pdf"
_CLIENT_REPORT_METADATA_PATH = "client_report/report.json"


def retry_filesystem_operation[OperationResult](
    operation: Callable[[], OperationResult],
    *,
    attempts: int = 5,
    initial_delay_seconds: float = 0.05,
) -> OperationResult:
    """Reintenta bloqueos transitorios de Windows sin ocultar el error final."""
    if attempts < 1 or initial_delay_seconds < 0:
        raise ValueError("filesystem_retry_options_invalid")
    delay = initial_delay_seconds
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (PermissionError, OSError):
            if attempt == attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("filesystem_retry_unreachable")  # pragma: no cover


def create_evidence_package(
    result_root: Path,
    storage_root: Path,
    analysis_id: str,
    *,
    client_report: Path | None = None,
    attempt: int = 1,
    before_commit: Callable[[], None] | None = None,
) -> Path | None:
    """Crea un ZIP desde la allowlist; no modifica el bundle científico."""
    files = _verified_files(result_root)
    if files is None:
        return None
    report_metadata = _client_report_metadata(client_report) if client_report else None
    analysis_root = _analysis_root(storage_root, analysis_id)
    token = _attempt_token(attempt)
    generation = analysis_root / "downloads" / f"attempt-{token}"
    package = generation / "evidence-package.zip"
    generation.mkdir(parents=True, exist_ok=True)
    temporary = generation / ".evidence-package.tmp.zip"
    metadata_path = package.with_suffix(".json")
    metadata_temporary = generation / ".evidence-package.tmp.json"
    fence = before_commit or _noop
    try:
        if temporary.exists():
            retry_filesystem_operation(lambda: temporary.unlink())
        if metadata_temporary.exists():
            retry_filesystem_operation(lambda: metadata_temporary.unlink())
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, path in sorted(files.items()):
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, path.read_bytes())
            if client_report is not None and report_metadata is not None:
                _write_zip_entry(archive, _CLIENT_REPORT_PATH, client_report.read_bytes())
                _write_zip_entry(
                    archive,
                    _CLIENT_REPORT_METADATA_PATH,
                    json.dumps(report_metadata, sort_keys=True).encode("utf-8"),
                )
        metadata_temporary.write_text(
            json.dumps(
                {"sha256": _sha256(temporary), "size_bytes": temporary.stat().st_size},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        fence()
        retry_filesystem_operation(lambda: temporary.replace(package))
        fence()
        retry_filesystem_operation(lambda: metadata_temporary.replace(metadata_path))
    except Exception:
        for attempt_file in (temporary, metadata_temporary):
            if attempt_file.exists():
                retry_filesystem_operation(attempt_file.unlink)
        if generation.exists() and not any(generation.iterdir()):
            retry_filesystem_operation(generation.rmdir)
        raise
    return package


def publish_curated_results(
    result_root: Path,
    storage_root: Path,
    analysis_id: str,
    *,
    client_report: Path | None = None,
    attempt: int = 1,
    before_commit: Callable[[], None] | None = None,
) -> Path | None:
    """Publica una copia privada e inmutable del contrato curado y su allowlist."""
    files = _verified_files(result_root)
    if files is None:
        return None
    report_metadata = _client_report_metadata(client_report) if client_report else None
    analysis_root = _analysis_root(storage_root, analysis_id)
    token = _attempt_token(attempt)
    generations_root = analysis_root / "results"
    destination = generations_root / f"attempt-{token}"
    staging = generations_root / f".attempt-{token}.tmp"
    fence = before_commit or _noop
    generations_root.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        retry_filesystem_operation(lambda: shutil.rmtree(staging))
    if destination.exists():
        raise ValueError("curated_result_generation_already_exists")
    try:
        for relative, source in sorted(files.items()):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            retry_filesystem_operation(partial(shutil.copyfile, source, target))
            if _sha256(target) != _sha256(source):
                raise ValueError("curated_result_copy_sha256_mismatch")
        if client_report is not None and report_metadata is not None:
            report_target = staging / _CLIENT_REPORT_PATH
            report_target.parent.mkdir(parents=True, exist_ok=True)
            retry_filesystem_operation(lambda: shutil.copyfile(client_report, report_target))
            if _sha256(report_target) != report_metadata["sha256"]:
                raise ValueError("client_report_copy_sha256_mismatch")
            (staging / _CLIENT_REPORT_METADATA_PATH).write_text(
                json.dumps(report_metadata, sort_keys=True),
                encoding="utf-8",
            )
        fence()
        retry_filesystem_operation(lambda: staging.rename(destination))
    except Exception:
        if staging.exists():
            retry_filesystem_operation(lambda: shutil.rmtree(staging))
        raise
    return destination


def _verified_files(result_root: Path) -> dict[str, Path] | None:
    resolved_root = result_root.resolve()
    index_path = result_root / "report_assets" / "index.json"
    if not index_path.is_file():
        return None
    index_path = index_path.resolve()
    if not index_path.is_relative_to(resolved_root):
        raise ValueError("report_asset_index_unsafe")
    index = _load_object(index_path)
    dataset_relative = _required_relative(index.get("dataset_path"))
    dataset_path = _verified_path(
        resolved_root,
        dataset_relative,
        expected_sha256=index.get("dataset_sha256"),
        expected_size=None,
    )
    manifest_path = (resolved_root / "run_manifest.json").resolve()
    if not manifest_path.is_relative_to(resolved_root) or not manifest_path.is_file():
        raise ValueError("run_manifest_contract_invalid")
    files: dict[str, Path] = {
        "run_manifest.json": manifest_path,
        "report_assets/index.json": index_path.resolve(),
        dataset_relative: dataset_path,
    }
    entries = index.get("files")
    if not isinstance(entries, list):
        raise ValueError("report_asset_allowlist_invalid")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("report_asset_entry_invalid")
        relative = _required_relative(entry.get("report_path"))
        files[relative] = _verified_path(
            resolved_root,
            relative,
            expected_sha256=entry.get("report_sha256"),
            expected_size=entry.get("size_bytes"),
        )

    return files


def _analysis_root(storage_root: Path, analysis_id: str) -> Path:
    root = storage_root.resolve()
    relative = Path("analyses") / analysis_id
    candidate = (root / relative).resolve()
    if (
        not analysis_id
        or Path(analysis_id).name != analysis_id
        or analysis_id in {".", ".."}
        or not candidate.is_relative_to(root)
    ):
        raise ValueError("analysis_id_unsafe")
    return candidate


def _attempt_token(attempt: int) -> str:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("worker_attempt_invalid")
    return str(attempt)


def _noop() -> None:
    return None


def _client_report_metadata(path: Path) -> dict[str, object]:
    try:
        resolved = path.resolve(strict=True)
        size_bytes = resolved.stat().st_size
        with resolved.open("rb") as source:
            header = source.read(5)
    except OSError as error:
        raise ValueError("client_report_invalid") from error
    if not resolved.is_file() or size_bytes <= 5 or header != b"%PDF-":
        raise ValueError("client_report_invalid")
    return {
        "media_type": "application/pdf",
        "path": _CLIENT_REPORT_PATH,
        "schema_version": "1.0.0",
        "sha256": _sha256(resolved),
        "size_bytes": size_bytes,
    }


def _write_zip_entry(archive: zipfile.ZipFile, relative: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, payload)


def _verified_path(
    root: Path,
    relative_value: str,
    *,
    expected_sha256: object,
    expected_size: object,
) -> Path:
    relative = Path(relative_value)
    resolved_root = root.resolve()
    path = (resolved_root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(resolved_root):
        raise ValueError("report_asset_path_unsafe")
    if not path.is_file() or not _is_sha256(expected_sha256):
        raise ValueError("report_asset_contract_invalid")
    if expected_size is not None and (
        not isinstance(expected_size, int) or path.stat().st_size != expected_size
    ):
        raise ValueError("report_asset_size_mismatch")
    if _sha256(path) != expected_sha256:
        raise ValueError("report_asset_sha256_mismatch")
    return path


def _required_relative(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("report_asset_relative_path_invalid")
    return value


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("report_asset_index_not_object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_json_atomic(path: Path, payload: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    retry_filesystem_operation(lambda: temporary.replace(path))
