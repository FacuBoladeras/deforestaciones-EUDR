"""Recolecta evidencia Dynamic World mensual acotada a eventos persistentes."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from deforestation_pipeline.agricultural_collector import (
    AgriculturalCollectorRemoteError,
    make_dynamic_world_raster_provider,
    materialize_agricultural_evidence_collection,
)
from deforestation_pipeline.complete_analysis import PROJECT_ROOT
from deforestation_pipeline.config import load_config
from deforestation_pipeline.gee import (
    GeeAuthenticationError,
    authenticate_earth_engine,
    authenticate_earth_engine_user_oauth,
)
from deforestation_pipeline.raster_products import RasterDownloadError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_bundle", type=Path)
    parser.add_argument("--establishment-id", required=True)
    parser.add_argument("--analysis-end-date", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "agricultural_evidence",
    )
    parser.add_argument(
        "--collector-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "agricultural-collector.yml",
    )
    parser.add_argument(
        "--pipeline-config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "default.yml",
        help="provee únicamente los límites comunes de descarga raster",
    )
    parser.add_argument("--catalog", type=Path, default=PROJECT_ROOT / "data" / "catalog.yml")
    parser.add_argument("--licenses", type=Path, default=PROJECT_ROOT / "data" / "licenses.yml")
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument("--credentials", type=Path)
    authentication.add_argument("--gee-project")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    default_credentials = PROJECT_ROOT / "credentials.json"
    credentials = arguments.credentials
    if credentials is None and arguments.gee_project is None and default_credentials.is_file():
        credentials = default_credentials
    try:
        if credentials is not None:
            session = authenticate_earth_engine(credentials)
        elif arguments.gee_project:
            session = authenticate_earth_engine_user_oauth(project=arguments.gee_project)
        else:
            raise ValueError("se requiere --credentials o --gee-project para consultar GEE")
        output_config = load_config(arguments.pipeline_config).output
        provider = make_dynamic_world_raster_provider(
            session=session,
            output_config=output_config,
        )
        output = materialize_agricultural_evidence_collection(
            source_event_bundle=arguments.event_bundle,
            output_root=arguments.output_root,
            establishment_id=arguments.establishment_id,
            analysis_end_date=arguments.analysis_end_date,
            config_path=arguments.collector_config,
            catalog_path=arguments.catalog,
            licenses_path=arguments.licenses,
            created_at=datetime.now(UTC),
            raster_provider=provider,
            artifact_profile=output_config.artifact_profile,
        )
    except (
        AgriculturalCollectorRemoteError,
        GeeAuthenticationError,
        OSError,
        RasterDownloadError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
