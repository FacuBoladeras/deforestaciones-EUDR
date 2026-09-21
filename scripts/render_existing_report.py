"""Renderiza un informe nuevo desde un expediente existente sin ejecutar ciencia."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import NoReturn

from deforestation_reporting import (
    OverpassOSMProvider,
    load_report_package,
    render_technical_appendix,
    render_technical_report,
)
from deforestation_reporting.editorial_content import REPORT_TEMPLATE_VERSION


def _offline_fetcher(_endpoint: str, _query: str, _user_agent: str) -> NoReturn:
    raise OSError("network_context_disabled")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Renderiza informe y anexo desde report_assets verificados. "
            "No ejecuta el pipeline ni contacta GEE."
        )
    )
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True, dest="input_path")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--allow-network-context",
        action="store_true",
        help="Permite consultar Overpass cuando el contexto OSM no existe en cache.",
    )
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    run_root = arguments.run_root.resolve()
    input_path = arguments.input_path.resolve()
    output_dir = arguments.output_dir.resolve()
    if output_dir == run_root or output_dir.is_relative_to(run_root):
        raise ValueError("report_output_must_not_modify_source_run")

    report_path = output_dir / "informe-cliente.pdf"
    appendix_path = output_dir / "anexo-tecnico.pdf"
    metadata_path = output_dir / "render-metadata.json"
    existing = [path for path in (report_path, appendix_path, metadata_path) if path.exists()]
    if existing:
        raise FileExistsError(f"report_output_exists:{existing[0]}")

    package = load_report_package(run_root, input_path=input_path)
    cache_dir = run_root / ".osm-cache"
    provider = (
        OverpassOSMProvider(cache_dir=cache_dir)
        if arguments.allow_network_context
        else OverpassOSMProvider(cache_dir=cache_dir, fetcher=_offline_fetcher)
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        report = render_technical_report(
            package,
            report_path,
            osm_basemap_provider=provider,
        )
        appendix = render_technical_appendix(package, appendix_path)
        index_path = run_root / "report_assets" / "index.json"
        metadata = {
            "schema_version": "1.0.0",
            "template_version": REPORT_TEMPLATE_VERSION,
            "source": {
                "run_manifest_sha256": _sha256(run_root / "run_manifest.json"),
                "report_assets_index_sha256": _sha256(index_path),
                "input_sha256": _sha256(input_path),
            },
            "network_context_allowed": bool(arguments.allow_network_context),
            "artifacts": [
                {
                    "name": report.path.name,
                    "sha256": report.sha256,
                    "size_bytes": report.size_bytes,
                },
                {
                    "name": appendix.path.name,
                    "sha256": appendix.sha256,
                    "size_bytes": appendix.size_bytes,
                },
            ],
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        for path in (report_path, appendix_path, metadata_path):
            path.unlink(missing_ok=True)
        if output_dir.exists() and not any(output_dir.iterdir()):
            output_dir.rmdir()
        raise

    print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
