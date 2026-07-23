# Pipeline de evidencia geoespacial EUDR

Pipeline reproducible para generar evidencia técnica que apoye la evaluación de
cadenas de carne bovina libres de deforestación en Argentina.

El proyecto genera datos, alertas y productos auditables. No emite
certificaciones legales ni reemplaza la debida diligencia del operador o la
revisión de terceros.

## Estado

El repositorio se encuentra en su etapa de fundación. Todavía no contiene
lógica de análisis GIS ni acceso a Google Earth Engine.

## Requisitos de desarrollo

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Git

## Preparación

```powershell
uv sync --dev
```

## Verificaciones

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

Las reglas metodológicas y los límites de alcance se encuentran en
`AGENTS.md`.
