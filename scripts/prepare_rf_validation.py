"""CLI para generar el paquete ciego de validación independiente RF 2020."""

from __future__ import annotations

import argparse
from pathlib import Path

from deforestation_pipeline.rf_validation_pack import prepare_validation_pack


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/rf-validation-entrerios.yml"),
    )
    arguments = parser.parse_args()
    result = prepare_validation_pack(arguments.config)
    print(f"pack: {result.pack_directory}")
    print(f"pilot={result.pilot_count}, test={result.test_count}, cards={result.card_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
