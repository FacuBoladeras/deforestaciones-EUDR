# Configuración reproducible

## Principios

`configs/default.yml` es el punto de entrada declarativo de la primera prueba.
Los modelos de `deforestation_pipeline.config` rechazan parámetros desconocidos
y estructuras incompletas. La configuración validada es inmutable durante una
ejecución. En particular, las colecciones que participan en su identidad
(`data.indices` y `output.formats`) se normalizan a tuplas y no pueden mutarse
después de la validación.

El hash de parámetros (`parameters_hash`) es un SHA-256 calculado sobre una
representación JSON canónica del modelo validado: claves ordenadas, sin
espacios de formato y con valores ya normalizados por Pydantic. Por lo tanto,
no depende del orden de las claves ni del formato superficial del YAML.

La semántica vigente es deliberadamente amplia: el digest incluye todos los
campos de `PipelineConfig`. En consecuencia, un cambio válido de parámetro,
la ruta `output.directory` o el orden de las listas `data.indices` y
`output.formats` modifica el digest. Las listas se tratan como secuencias y no
como conjuntos, aunque sus validadores impiden valores duplicados.

Este hash identifica la configuración validada original. La identidad
científica y la identidad operativa de cada corrida se registran por separado,
como se explica en la sección siguiente.

## Configuración resuelta e identidades de corrida

Antes de ejecutar un análisis, `resolve_run_config(config, analysis_end_date)`
recibe una fecha final concreta y devuelve una `ResolvedPipelineConfig`
inmutable. Si el YAML ya declara una fecha final, debe coincidir con la fecha
recibida; en caso contrario se rechaza la corrida. La configuración de origen
nunca se muta.

Las identidades de una corrida resuelta son deliberadamente distintas:

- `scientific_parameters_hash` incluye `schema_version`, `analysis`, `spatial`
  y `data`; excluye las opciones de salida.
- `execution_config_hash` incluye la configuración resuelta completa, incluidos
  `output.directory` y `output.formats`.

Ambas incluyen la fecha final efectiva. `parameters_hash` permanece disponible
para caracterizar la configuración original completa y no debe sustituir las
identidades de una corrida resuelta.

## Parámetros de dominio

Los siguientes valores no son hiperparámetros calibrables:

- `cutoff_date: 2020-12-31`;
- `forest_definition_min_area_ha: 0.5`;
- `interchange_crs: EPSG:4326`;
- `area_crs_strategy: auto_equal_area`, para medir superficie sin depender de
  un huso;
- `raster_crs_strategy: local_utm`, para una grilla métrica compatible con GEE;
- `minimum_coordinate_decimals: 6`;
- `preserve_subthreshold_events: true`.

El umbral de 0,5 ha no se implementará mediante un conteo fijo de píxeles. La
etapa espacial deberá calcular superficie en un CRS equivalente y conservar
también los eventos menores como evidencia intermedia.

## Parámetros del benchmark

La configuración 1.2.0 declara HLS a 30 m y un composite anual por mediana para
la primera prueba real. También fija explícitamente:

- separación entre el CRS equivalente de medición y la grilla UTM local del
  raster;
- escala nativa HLS `0.0001` y offset `0`, conservados como procedencia;
- multiplicador Earth Engine `1` y offset `0`, porque la colección GEE ya
  entrega reflectancia física;
- descarte de aerosol alto;
- preservación de agua;
- nodata `-9999`;
- límites de descarga directa de 32.000.000 bytes y 10.000 píxeles por eje;
- límite agregado de 256.000.000 bytes sin comprimir estimados por serie;
- rangos de visualización RGB y por índice;
- resolución de render PNG.

Los índices declarados son NDVI, EVI2, NBR, NDMI, NMDI, LSWI, NIRv y kNDVI.
La inclusión en la configuración no implica que todos deban incorporarse a un
modelo final: su selección deberá justificarse con validación.

`analysis_end_date: null` significa que el ejecutor deberá resolver la fecha al
comenzar el análisis y registrarla como valor concreto en el manifiesto. Nunca
debe quedar nula en el resumen final.

Los identificadores de HLSL30 y HLSS30 se mantienen en `data/catalog.yml`, no
en la configuración científica. Las fechas efectivas del composite se reciben
por CLI y deben definir un año calendario completo.

El Paso 11 deriva `RasterGridSpec` una sola vez a partir del AOI, el CRS raster
resuelto, `data.target_resolution_m` y `output.raster_nodata`. La misma
transformación y dimensiones se envían a GEE para cada año y se validan al
reabrir los GeoTIFF. El presupuesto `maximum_series_download_bytes` se evalúa
antes de construir el primer composite remoto.

## Uso desde Python

```python
from pathlib import Path

from deforestation_pipeline.config import load_config, parameters_hash

config = load_config(Path("configs/default.yml"))
digest = parameters_hash(config)
```

## Contrato y licencias

Los JSON Schema del resumen, la grilla, los metadatos temporales y la cobertura
se regeneran desde los modelos Pydantic:

```powershell
uv run python scripts/export_schema.py
```

La prueba automatizada compara el archivo generado con el modelo para detectar
derivas silenciosas.

`data/licenses.yml` registra HLSL30 y HLSS30 desde su primer uso efectivo de
píxeles. Cada registro contiene proveedor, colección, versión, condiciones,
fecha de acceso, atribución y restricciones de plataforma.
