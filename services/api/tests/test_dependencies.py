"""El proceso HTTP conserva un grafo de dependencias liviano."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_api_depends_on_domain_but_not_on_scientific_pipeline() -> None:
    project = Path(__file__).resolve().parents[1]
    payload = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = payload["project"]["dependencies"]

    assert "deforestation-domain" in dependencies
    assert "deforestation-pipeline" not in dependencies
