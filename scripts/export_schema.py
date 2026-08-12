"""Exporta los contratos JSON Schema versionados del pipeline."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deforestation_pipeline.agricultural_collector import agricultural_collection_json_schema
from deforestation_pipeline.agricultural_evidence import (
    agricultural_evidence_json_schema,
    agricultural_evidence_policy_json_schema,
)
from deforestation_pipeline.agricultural_persistence import agricultural_persistence_json_schema
from deforestation_pipeline.change_detection import (
    disturbance_detection_json_schema,
)
from deforestation_pipeline.disturbance_evidence import disturbance_evidence_json_schema
from deforestation_pipeline.forest_baseline import forest_baseline_json_schema
from deforestation_pipeline.hls_seasonal import (
    hls_seasonal_metadata_json_schema,
    temporal_cube_index_json_schema,
)
from deforestation_pipeline.schemas import (
    analysis_summary_json_schema,
    raster_grid_json_schema,
)
from deforestation_pipeline.seasonal_qa import seasonal_coverage_json_schema
from deforestation_pipeline.temporal_cube import (
    temporal_cube_spec_json_schema,
    temporal_window_plan_json_schema,
)

SCHEMA_EXPORTS: tuple[tuple[Path, Callable[[], dict[str, Any]]], ...] = (
    (
        Path("data/schemas/agricultural-persistence-v1.0.0.json"),
        agricultural_persistence_json_schema,
    ),
    (
        Path("data/schemas/agricultural-collection-v1.0.0.json"),
        agricultural_collection_json_schema,
    ),
    (
        Path("data/schemas/agricultural-evidence-v1.0.0.json"),
        agricultural_evidence_json_schema,
    ),
    (
        Path("data/schemas/agricultural-evidence-policy-v1.0.0.json"),
        agricultural_evidence_policy_json_schema,
    ),
    (
        Path("data/schemas/analysis-summary-v1.0.0.json"),
        analysis_summary_json_schema,
    ),
    (
        Path("data/schemas/raster-grid-v1.0.0.json"),
        raster_grid_json_schema,
    ),
    (
        Path("data/schemas/forest-baseline-v1.0.0.json"),
        forest_baseline_json_schema,
    ),
    (
        Path("data/schemas/disturbance-detection-v1.4.0.json"),
        disturbance_detection_json_schema,
    ),
    (
        Path("data/schemas/disturbance-evidence-v1.2.0.json"),
        disturbance_evidence_json_schema,
    ),
    (
        Path("data/schemas/temporal-window-plan-v1.0.0.json"),
        temporal_window_plan_json_schema,
    ),
    (
        Path("data/schemas/temporal-cube-spec-v1.0.0.json"),
        temporal_cube_spec_json_schema,
    ),
    (
        Path("data/schemas/hls-seasonal-metadata-v2.0.0.json"),
        hls_seasonal_metadata_json_schema,
    ),
    (
        Path("data/schemas/temporal-cube-index-v2.0.0.json"),
        temporal_cube_index_json_schema,
    ),
    (
        Path("data/schemas/hls-seasonal-coverage-v1.0.0.json"),
        seasonal_coverage_json_schema,
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
