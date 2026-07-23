# Pipeline de evidencia geoespacial EUDR

Pipeline reproducible para generar evidencia técnica que apoye la evaluación de
cadenas de carne bovina libres de deforestación en Argentina.

El proyecto genera datos, alertas y productos auditables. No emite
certificaciones legales ni reemplaza la debida diligencia del operador o la
revisión de terceros.

## Estado

El repositorio se encuentra en su etapa de fundación espacial. Ya valida,
normaliza y repara de forma auditable geometrías vectoriales locales, y mide
superficies en hectáreas mediante un CRS equivalente o una zona UTM local. Aún
no accede a Google Earth Engine ni a fuentes satelitales.

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

## Prueba local con un vector

`pruebas.py` ejecuta únicamente las capacidades espaciales disponibles en
local. Acepta un GeoJSON con una geometría, una `Feature` o una
`FeatureCollection` de una sola entidad:

```powershell
uv run python pruebas.py data/samples/local_test_polygon.geojson `
  --establishment-id caso-sintetico `
  --analysis-end-date 2026-07-23
```

Cada ejecución crea una carpeta independiente bajo `outputs/local_tests/`.
El paquete incluye la fuente original, geometrías interpretada, normalizada y
de análisis, validación, medición de área, configuración resuelta, entorno,
resumen local y un manifiesto con SHA-256 de cada artefacto. No consulta datos
remotos ni genera todavía una evaluación de deforestación.

Documentación relacionada:

- `docs/configuration.md`: parámetros, hash y reglas de reproducibilidad;
- `docs/data_dictionary.md`: contratos de entrada, eventos y salida;
- `docs/geometry_validation.md`: validación GIS local, CRS y reparaciones;
- `docs/area_measurement.md`: estrategias de área, umbral y limitaciones;
- `docs/local_testing.md`: uso, estructura y límites del runner local.
