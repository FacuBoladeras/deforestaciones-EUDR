"""Configuración explícita y testeable del proceso HTTP."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Parámetros operativos; ningún cliente puede modificarlos por request."""

    database_path: Path
    storage_root: Path
    output_root: Path
    jurisdiction_boundary_path: Path
    max_vertices: int = 50_000
    max_request_bytes: int = 2_000_000
    owner_id: str = "local-internal"
    retention_days: int = 30
    code_revision: str = "working-tree"

    @classmethod
    def from_environment(cls) -> ApiSettings:
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
            output_root=Path(
                os.environ.get(
                    "DEFORESTATION_ANALYSIS_OUTPUT_ROOT",
                    PROJECT_ROOT / "outputs" / "runs",
                )
            ),
            jurisdiction_boundary_path=Path(
                os.environ.get(
                    "DEFORESTATION_API_JURISDICTION",
                    PROJECT_ROOT / "data" / "boundaries" / "argentina_operational.geojson",
                )
            ),
            max_vertices=int(os.environ.get("DEFORESTATION_API_MAX_VERTICES", "50000")),
            max_request_bytes=int(os.environ.get("DEFORESTATION_API_MAX_REQUEST_BYTES", "2000000")),
            owner_id=os.environ.get("DEFORESTATION_API_OWNER_ID", "local-internal"),
            retention_days=int(os.environ.get("DEFORESTATION_API_RETENTION_DAYS", "30")),
            code_revision=os.environ.get("DEFORESTATION_CODE_REVISION", "working-tree"),
        )
