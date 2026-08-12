"""Deriva persistencia agrícola auditable desde un bundle mensual recolectado."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from deforestation_pipeline.agricultural_persistence import (
    materialize_agricultural_persistence,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection_bundle", type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "agricultural_persistence",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "agricultural-persistence.yml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        output = materialize_agricultural_persistence(
            source_collection_bundle=arguments.collection_bundle,
            output_root=arguments.output_root,
            config_path=arguments.config,
            created_at=datetime.now(UTC),
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
