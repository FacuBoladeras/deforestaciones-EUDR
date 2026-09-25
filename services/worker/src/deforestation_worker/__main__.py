"""CLI del worker local; proceso independiente del servidor FastAPI."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence

from deforestation_worker.service import AnalysisWorker, WorkerSettings, check_worker_runtime


class WorkerJsonFormatter(logging.Formatter):
    """Serializa sólo contexto operacional seguro, sin mensajes de excepciones."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "analysis_id": getattr(record, "analysis_id", None),
            "attempt": getattr(record, "attempt", None),
            "duration_seconds": getattr(record, "duration_seconds", 0.0),
            "event": record.getMessage(),
            "level": record.levelname,
            "safe_error_code": getattr(record, "safe_error_code", None),
            "stage": getattr(record, "stage", None),
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging() -> None:
    """Habilita logs JSON INFO visibles al ejecutar el worker desde la CLI."""
    logger = logging.getLogger("deforestation_worker")
    handler = logging.StreamHandler()
    handler.setFormatter(WorkerJsonFormatter())
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="procesa como máximo un job")
    mode.add_argument(
        "--healthcheck",
        action="store_true",
        help="valida recursos y mounts sin contactar servicios remotos",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.healthcheck:
        return 0 if check_worker_runtime(WorkerSettings.from_environment()) else 1
    configure_logging()
    worker = AnalysisWorker(WorkerSettings.from_environment())
    if arguments.once:
        worker.initialize()
        return 0 if worker.run_once() else 2
    worker.run_forever()
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
