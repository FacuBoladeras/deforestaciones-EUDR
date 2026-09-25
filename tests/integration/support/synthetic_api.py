"""Servidor HTTP descartable para el gate E2E multiproceso."""

from __future__ import annotations

import os

import uvicorn

from deforestation_api.app import create_app
from deforestation_api.settings import ApiSettings


def main() -> None:
    uvicorn.run(
        create_app(ApiSettings.from_environment()),
        host="127.0.0.1",
        port=int(os.environ["DEFORESTATION_E2E_PORT"]),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
