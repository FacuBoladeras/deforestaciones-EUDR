"""Ejecuta el expediente completo: detección, Hampel, deltas RF y atribución v0."""

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vector", type=Path, help="polígono del establecimiento")
    parser.add_argument("--establishment-id", help="default: nombre del archivo vectorial")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "outputs" / "runs")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "default.yml")
    parser.add_argument(
        "--forest-model-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "rf-multiyear-candidate-costa-uru.yml",
    )
    parser.add_argument(
        "--hampel-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "hampel-benchmark.yml",
    )
    parser.add_argument("--credentials", type=Path, help="default: credentials.json si existe")
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
    if credentials is None and default_credentials.is_file():
        credentials = default_credentials
    request = CompleteAnalysisRequest(
        input_path=arguments.vector,
        output_root=arguments.output_root,
        config_path=arguments.config,
        forest_model_config_path=arguments.forest_model_config,
        hampel_config_path=arguments.hampel_config,
        credentials_path=credentials,
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
    except (CompleteAnalysisError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
