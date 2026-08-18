"""CLI del worker local; proceso independiente del servidor FastAPI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from deforestation_worker.service import AnalysisWorker, WorkerSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="procesa como máximo un job")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    worker = AnalysisWorker(WorkerSettings.from_environment())
    if arguments.once:
        worker.initialize()
        return 0 if worker.run_once() else 2
    worker.run_forever()
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
