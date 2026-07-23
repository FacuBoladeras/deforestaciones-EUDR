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
uv run mypy src tests scripts
uv run pytest
```

Las reglas metodológicas y los límites de alcance se encuentran en
`AGENTS.md`.

## Configuración y contratos

La configuración reproducible inicial se encuentra en `configs/default.yml`.
Puede validarse y convertirse en un hash determinístico mediante las funciones
de `deforestation_pipeline.config`.

El contrato Pydantic del resumen por establecimiento está versionado como JSON
Schema. Para regenerarlo:

```powershell
uv run python scripts/export_schema.py
```

El registro `data/licenses.yml` permanece vacío hasta que un dataset sea
incorporado efectivamente al pipeline. Su presencia en un catálogo no implica
que su licencia haya sido validada para el uso previsto.

Documentación relacionada:

- `docs/configuration.md`: parámetros, hash y reglas de reproducibilidad;
- `docs/data_dictionary.md`: contratos de entrada, eventos y salida.
