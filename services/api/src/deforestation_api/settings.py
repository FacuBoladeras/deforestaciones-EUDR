"""Configuración explícita y testeable del proceso HTTP."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from deforestation_domain.runtime import resolve_runtime_root

PROJECT_ROOT = resolve_runtime_root(Path(__file__).resolve().parents[4])


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Parámetros operativos; ningún cliente puede modificarlos por request."""

    database_path: Path
    storage_root: Path
    jurisdiction_boundary_path: Path
    max_vertices: int = 50_000
    max_request_bytes: int = 2_000_000
    owner_id: str = "local-internal"
    retention_days: int = 30
    code_revision: str = "working-tree"

    @classmethod
    def from_environment(cls) -> ApiSettings:
        runtime_root = resolve_runtime_root(PROJECT_ROOT)
        private_root = Path(
            os.environ.get(
                "DEFORESTATION_API_PRIVATE_ROOT",
                PROJECT_ROOT / "outputs" / "api" / "private",
            )
        )
        return cls(
            database_path=Path(
                os.environ.get(
                    "DEFORESTATION_API_DATABASE",
                    private_root / "jobs.sqlite3",
                )
            ),
            storage_root=Path(
                os.environ.get(
                    "DEFORESTATION_API_STORAGE",
                    private_root / "objects",
                )
            ),
            jurisdiction_boundary_path=Path(
                os.environ.get(
                    "DEFORESTATION_API_JURISDICTION",
                    runtime_root / "data" / "boundaries" / "argentina_operational.geojson",
                )
            ),
            max_vertices=int(os.environ.get("DEFORESTATION_API_MAX_VERTICES", "50000")),
            max_request_bytes=int(os.environ.get("DEFORESTATION_API_MAX_REQUEST_BYTES", "2000000")),
            owner_id=os.environ.get("DEFORESTATION_API_OWNER_ID", "local-internal"),
            retention_days=int(os.environ.get("DEFORESTATION_API_RETENTION_DAYS", "30")),
            code_revision=os.environ.get("DEFORESTATION_CODE_REVISION", "working-tree"),
        )
