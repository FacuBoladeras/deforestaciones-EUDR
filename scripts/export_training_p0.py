"""CLI explícita para el preflight y exportación Drive del training P0."""

from __future__ import annotations

import argparse
from pathlib import Path

from deforestation_pipeline.training_p0 import run_training_p0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/training-p0-entrerios.yml"),
    )
    parser.add_argument(
        "--auth-mode",
        choices=("service_account", "user_oauth"),
        required=True,
    )
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--start-export", action="store_true")
    arguments = parser.parse_args()
    if arguments.auth_mode == "service_account" and arguments.credentials is None:
        parser.error("--credentials es obligatorio con --auth-mode service_account")
    result = run_training_p0(
        config_path=arguments.config,
        credentials_path=arguments.credentials,
        auth_mode=arguments.auth_mode,
        start_export=arguments.start_export,
    )
    print(f"{result.status}: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
