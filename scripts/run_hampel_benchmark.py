"""Deriva un benchmark Hampel protegido desde un bundle estacional existente."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from deforestation_pipeline.hampel_benchmark import (
    load_hampel_benchmark_config,
    materialize_seasonal_hampel_benchmark,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--input-geojson", type=Path, required=True)
    parser.add_argument("--establishment-id", required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/hampel-benchmark.yml"),
    )
    arguments = parser.parse_args(argv)
    output = materialize_seasonal_hampel_benchmark(
        source_bundle=arguments.source_bundle,
        output_root=arguments.output_root,
        input_geojson=arguments.input_geojson,
        establishment_id=arguments.establishment_id,
        config=load_hampel_benchmark_config(arguments.config),
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
