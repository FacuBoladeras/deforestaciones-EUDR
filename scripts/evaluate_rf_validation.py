"""CLI para evaluar el test RF bloqueado después de la adjudicación ciega."""

from __future__ import annotations

import argparse
from pathlib import Path

from deforestation_pipeline.rf_validation_pack import evaluate_validation_pack


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack_directory", type=Path)
    parser.add_argument("--adjudications", type=Path)
    arguments = parser.parse_args()
    result = evaluate_validation_pack(
        arguments.pack_directory,
        adjudication_path=arguments.adjudications,
    )
    print(f"report: {result.report_path}")
    print(f"gate: {'PASS' if result.passed else 'FAIL'}")
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
