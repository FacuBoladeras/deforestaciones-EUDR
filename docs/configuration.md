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

- `scientific_parameters_hash` incluye `schema_version`, `analysis`, `spatial`,
  `data`, `forest_baseline` y `disturbance_detection`; excluye las opciones de
  salida.
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

La configuración 1.8.0 declara HLS a 30 m y un composite anual por mediana para
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

## Contrato de línea base forestal 2020

`forest_baseline` incorpora en la identidad científica las reglas previas a
consultar o clasificar bosque. El contrato 1.0.0 fija:

- fecha de referencia `2020-12-31`;
- atributos espectrales desde `2019-01-01` hasta `2021-01-01` exclusivo;
- benchmark HLS a 30 m;
- al menos dos fuentes independientes de evidencia;
- definición de bosque de 0,5 ha, 5 m de altura y 10 % de cobertura de copa;
- exclusión de tierras predominantemente agrícolas o urbanas;
- conservación de evidencia por fuente y desacuerdo;
- prohibición de observaciones posteriores al corte y de interpolaciones
  largas.

`feature_end_date_exclusive` es deliberadamente `2021-01-01`: incluye el día
de corte pero impide que información futura contamine la línea base. Los
datasets concretos y sus roles se documentan en `data/catalog.yml`.

## Contrato de detección de perturbaciones

`disturbance_detection` incorpora al hash científico la frontera del Paso
14.0, la regla robusta 14.3, el benchmark CCDC 14.4 y su convergencia
conservadora 14.5. Define:

- historia de referencia desde `2017-01-01`;
- análisis post-corte desde `2021-01-01`;
- exclusión de períodos que cruzan la fecha de corte;
- evaluación con bandera cuando la línea base forestal discrepa;
- comparación exclusivamente contra la misma estación;
- mínimo de tres referencias válidas, mediana y escala `1.4826 × MAD`;
- caída de vegetación positiva y escala cero no estandarizable;
- preservación de `NaN`, umbral robusto y persistencia explícitos;
- NDVI, NBR, NDMI y NIRv como variables detectoras iniciales;
- detector robusto y CCDC como evidencias independientes;
- CCDC sobre observaciones HLSL30 densas, ya enmascaradas por Fmask, sin
  fusionar HLSS30;
- parámetros CCDC oficiales declarados: bandas de ruptura,
  `minObservations`, `chiSquareProbability`, `minNumOfYearsScaler`,
  `dateFormat`, `lambda`, `maxIterations` y política TMask;
- Earth Engine Python API `1.7.36`, referencia oficial actualizada el
  `2026-04-20` y consultada el `2026-07-29`;
- preservación de los arrays CCDC crudos y de `changeProb` únicamente como
  pseudoprobabilidad algorítmica de la ruptura;
- compatibilidad temporal únicamente cuando `tBreak` cae dentro de un intervalo
  robusto real bajo límites `[inicio, fin)`, sin margen temporal;
- preservación del resultado disponible cuando el otro detector falla, sin
  fingir convergencia;
- conservación explícita del desacuerdo y prohibición de fusionar las escalas
  de evidencia robusta y CCDC;
- score explícitamente no calibrado;
- prohibición de atribución, umbral espacial y evaluación final.

El perfil comparativo HLSL30+HLSS30 permanece deshabilitado hasta validar
explícitamente la mezcla de sensores. Los parámetros y esa decisión participan
del hash científico.

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
