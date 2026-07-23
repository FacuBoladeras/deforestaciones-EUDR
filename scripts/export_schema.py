"""Exporta el contrato versionado del resumen de análisis."""

from __future__ import annotations

import json
from pathlib import Path

from deforestation_pipeline.schemas import analysis_summary_json_schema

DEFAULT_OUTPUT = Path("data/schemas/analysis-summary-v1.0.0.json")


def main() -> None:
    """Escribe el JSON Schema canónico con orden y formato determinísticos."""
    DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        analysis_summary_json_schema(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    DEFAULT_OUTPUT.write_text(f"{payload}\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
