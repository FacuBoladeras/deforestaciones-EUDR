"""Exporta los contratos JSON Schema versionados del pipeline."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deforestation_pipeline.hls_series import (
    hls_series_coverage_json_schema,
    hls_series_metadata_json_schema,
)
from deforestation_pipeline.schemas import (
    analysis_summary_json_schema,
    raster_grid_json_schema,
)

SCHEMA_EXPORTS: tuple[tuple[Path, Callable[[], dict[str, Any]]], ...] = (
    (
        Path("data/schemas/analysis-summary-v1.0.0.json"),
        analysis_summary_json_schema,
    ),
    (
        Path("data/schemas/raster-grid-v1.0.0.json"),
        raster_grid_json_schema,
    ),
    (
        Path("data/schemas/hls-series-metadata-v1.0.0.json"),
        hls_series_metadata_json_schema,
    ),
    (
        Path("data/schemas/hls-series-coverage-v1.0.0.json"),
        hls_series_coverage_json_schema,
    ),
)


def main() -> None:
    """Escribe todos los JSON Schema con orden y formato determinísticos."""
    for output_path, schema_factory in SCHEMA_EXPORTS:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            schema_factory(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        output_path.write_text(f"{payload}\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
