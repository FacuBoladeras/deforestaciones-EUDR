"""Resolución explícita del bundle de recursos compartido por los procesos."""

from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ROOT_ENV = "DEFORESTATION_RUNTIME_ROOT"


def resolve_runtime_root(fallback: Path) -> Path:
    """Devuelve el root desplegable sin atarlo al layout del paquete instalado."""
    configured = os.environ.get(RUNTIME_ROOT_ENV)
    return Path(configured).resolve() if configured else fallback.resolve()
