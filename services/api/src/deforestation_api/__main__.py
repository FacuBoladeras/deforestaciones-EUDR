"""Entrada de desarrollo del proceso HTTP."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "deforestation_api.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
