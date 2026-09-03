"""Ejecuta detección, RF, agricultura persistente y atribución en un único run."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from deforestation_pipeline.complete_analysis import (
    DEFAULT_ANALYSIS_END_DATE,
    PROJECT_ROOT,
    CompleteAnalysisError,
    CompleteAnalysisRequest,
    run_complete_analysis,
)
from deforestation_pipeline.gee import GeeAuthenticationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vector", type=Path, help="polígono del establecimiento")
    parser.add_argument("--establishment-id", help="default: nombre del archivo vectorial")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "outputs" / "runs")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "default.yml")
    parser.add_argument(
        "--forest-model-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "rf-forest-entrerios-2020-2024.yml",
    )
    parser.add_argument(
        "--model-artifact",
        type=Path,
        help="joblib RF externo; su tamaño y SHA-256 se verifican contra el registry",
    )
    parser.add_argument(
        "--hampel-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "hampel-benchmark.yml",
    )
    parser.add_argument(
        "--agricultural-evidence-policy",
        type=Path,
        default=PROJECT_ROOT / "configs" / "agricultural-evidence.yml",
        help="política versionada que deriva independencia desde el linaje",
    )
    parser.add_argument(
        "--agricultural-collector-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "agricultural-collector.yml",
    )
    parser.add_argument(
        "--agricultural-persistence-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "agricultural-persistence.yml",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=PROJECT_ROOT / "data" / "catalog.yml",
    )
    parser.add_argument(
        "--licenses",
        type=Path,
        default=PROJECT_ROOT / "data" / "licenses.yml",
    )
    parser.add_argument(
        "--agricultural-evidence-json",
        type=Path,
        help="documento agricultural-evidence v1 opcional; no acepta independent",
    )
    parser.add_argument(
        "--agricultural-persistence-bundle",
        type=Path,
        help="bundle persistente por evento; excluyente con --agricultural-evidence-json",
    )
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument(
        "--credentials", type=Path, help="default: credentials.json si existe"
    )
    authentication.add_argument(
        "--gee-project",
        help="proyecto GEE versionado para reutilizar la credencial OAuth persistente",
    )
    parser.add_argument(
        "--analysis-end-date",
        type=date.fromisoformat,
        default=DEFAULT_ANALYSIS_END_DATE,
        help=f"fecha solicitada YYYY-MM-DD (default: {DEFAULT_ANALYSIS_END_DATE})",
    )
    parser.add_argument("--hls-start-year", type=int, default=2020)
    parser.add_argument("--hls-end-year", type=int, default=2025)
    parser.add_argument("--source-crs")
    parser.add_argument("--layer")
    parser.add_argument("--dissolve-all", action="store_true")
    parser.add_argument(
        "--declared-land-use",
        choices=("unknown", "managed_forest_plantation"),
        default="unknown",
        help="contexto declarado; no se considera evidencia independiente",
    )
    parser.add_argument(
        "--declared-context-source",
        default="not_provided",
        help="fuente auditable del contexto declarado, por ejemplo user_declared",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    default_credentials = PROJECT_ROOT / "credentials.json"
    credentials = arguments.credentials
    if credentials is None and arguments.gee_project is None and default_credentials.is_file():
        credentials = default_credentials
    request = CompleteAnalysisRequest(
        input_path=arguments.vector,
        output_root=arguments.output_root,
        config_path=arguments.config,
        forest_model_config_path=arguments.forest_model_config,
        hampel_config_path=arguments.hampel_config,
        agricultural_evidence_policy_path=arguments.agricultural_evidence_policy,
        agricultural_collector_config_path=arguments.agricultural_collector_config,
        agricultural_persistence_config_path=arguments.agricultural_persistence_config,
        catalog_path=arguments.catalog,
        licenses_path=arguments.licenses,
        agricultural_evidence_path=arguments.agricultural_evidence_json,
        agricultural_persistence_bundle_path=arguments.agricultural_persistence_bundle,
        credentials_path=credentials,
        model_artifact_path=arguments.model_artifact,
        gee_project=arguments.gee_project,
        establishment_id=arguments.establishment_id or arguments.vector.stem,
        analysis_end_date=arguments.analysis_end_date,
        hls_start_year=arguments.hls_start_year,
        hls_end_year=arguments.hls_end_year,
        source_crs=arguments.source_crs,
        vector_layer=arguments.layer,
        dissolve_all=arguments.dissolve_all,
        declared_land_use=arguments.declared_land_use,
        declared_context_source=arguments.declared_context_source,
    )
    try:
        output = run_complete_analysis(request)
    except (CompleteAnalysisError, FileNotFoundError, GeeAuthenticationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
