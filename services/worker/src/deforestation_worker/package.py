"""Empaquetado privado y determinístico de evidencia ya publicada."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any


def create_evidence_package(result_root: Path, storage_root: Path, analysis_id: str) -> Path | None:
    """Crea un ZIP desde la allowlist; no modifica el bundle científico."""
    index_path = result_root / "report_assets" / "index.json"
    if not index_path.is_file():
        return None
    index = _load_object(index_path)
    dataset_relative = _required_relative(index.get("dataset_path"))
    dataset_path = _verified_path(
        result_root,
        dataset_relative,
        expected_sha256=index.get("dataset_sha256"),
        expected_size=None,
    )
    manifest_path = (result_root / "run_manifest.json").resolve()
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
            result_root,
            relative,
            expected_sha256=entry.get("report_sha256"),
            expected_size=entry.get("size_bytes"),
        )

    package = (
        storage_root.resolve() / "analyses" / analysis_id / "downloads" / "evidence-package.zip"
    )
    package.parent.mkdir(parents=True, exist_ok=True)
    temporary = package.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, path in sorted(files.items()):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, path.read_bytes())
    temporary.replace(package)
    _write_json_atomic(
        package.with_suffix(".json"),
        {"sha256": _sha256(package), "size_bytes": package.stat().st_size},
    )
    return package


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
    temporary.replace(path)
