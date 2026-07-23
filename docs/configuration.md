# Configuración reproducible

## Principios

`configs/default.yml` es el punto de entrada declarativo de la primera prueba.
Los modelos de `deforestation_pipeline.config` rechazan parámetros desconocidos
y estructuras incompletas. La configuración validada es inmutable durante una
ejecución.

El hash de parámetros es un SHA-256 calculado sobre una representación JSON
canónica del modelo validado. Por lo tanto, no depende del orden de las claves
ni del formato superficial del YAML.

## Parámetros de dominio

Los siguientes valores no son hiperparámetros calibrables:

- `cutoff_date: 2020-12-31`;
- `forest_definition_min_area_ha: 0.5`;
- `interchange_crs: EPSG:4326`;
- `minimum_coordinate_decimals: 6`;
- `preserve_subthreshold_events: true`.

El umbral de 0,5 ha no se implementará mediante un conteo fijo de píxeles. La
etapa espacial deberá calcular superficie en un CRS equivalente y conservar
también los eventos menores como evidencia intermedia.

## Parámetros del benchmark

La configuración inicial declara HLS a 30 m y composiciones mensuales para
reproducir una línea de comparación metodológica. Esto no modifica la decisión
de usar Sentinel-2 a 10 m como fuente óptica principal del MVP mejorado.

Los índices declarados son NDVI, EVI2, NBR, NDMI, NMDI, LSWI, NIRv y kNDVI.
La inclusión en la configuración no implica que todos deban incorporarse a un
modelo final: su selección deberá justificarse con validación.

`analysis_end_date: null` significa que el ejecutor deberá resolver la fecha al
comenzar el análisis y registrarla como valor concreto en el manifiesto. Nunca
debe quedar nula en el resumen final.

Los identificadores de colecciones GEE no se incorporan todavía. Se definirán
en el catálogo cuando se verifiquen versión, disponibilidad, licencia,
atribución y restricciones.

## Uso desde Python

```python
from pathlib import Path

from deforestation_pipeline.config import load_config, parameters_hash

config = load_config(Path("configs/default.yml"))
digest = parameters_hash(config)
```

## Contrato y licencias

El JSON Schema del resumen se regenera desde el modelo Pydantic:

```powershell
uv run python scripts/export_schema.py
```

La prueba automatizada compara el archivo generado con el modelo para detectar
derivas silenciosas.

`data/licenses.yml` sólo debe contener datasets efectivamente verificados y
utilizados. Cada registro requiere proveedor, colección, versión, licencia,
fecha de acceso, atribución y restricciones.
