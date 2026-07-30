# Runner local de prueba

`pruebas.py` es una interfaz local: no inicia un servidor y publica todo en una
carpeta de corrida. Por defecto no accede a datos remotos. Con flags explícitos
puede consultar GEE y, para un ROI pequeño, descargar productos raster HLS,
la línea base forestal 2020 y evidencia de perturbaciones.

## Entrada admitida

La frontera usa GeoPandas y Pyogrio/GDAL. Por lo tanto, acepta archivos
vectoriales soportados por los drivers instalados. En el entorno validado
están disponibles, entre otros:

- GeoJSON y GeoJSONSeq;
- ESRI Shapefile;
- GeoPackage;
- FlatGeoBuf;
- GML y KML;
- MapInfo;
- Shapefile comprimido en ZIP.

La lista efectiva se obtiene sin procesar datos:

```powershell
uv run python pruebas.py --list-vector-formats
```

No se promete literalmente cualquier formato existente: se promete cualquier
**archivo vectorial legible por el GDAL instalado**, con geometría poligonal y
CRS resoluble.

Se procesa un territorio por corrida. Si una fuente contiene varias capas,
`--layer NOMBRE` es obligatorio. Si la capa contiene varias entidades, el
runner falla salvo que el usuario confirme su unión con `--dissolve-all`.
Esto evita analizar accidentalmente parcelas o capas equivocadas.

El CRS embebido se usa automáticamente. `--source-crs` sólo es necesario
cuando falta; si contradice al CRS embebido, la corrida se rechaza.

El repositorio contiene un caso sintético y anonimizado:

```powershell
uv run python pruebas.py data/samples/local_test_polygon.geojson `
  --establishment-id caso-sintetico `
  --analysis-end-date 2026-07-23
```

La consulta GEE es opcional y requiere fechas explícitas. Su uso se documenta
en `docs/gee_access.md`.

Opciones principales:

```text
--source-crs EPSG:XXXX
--layer NOMBRE
--dissolve-all
--full-pipeline
--hls-year YYYY
--hls-start-year YYYY
--hls-end-year YYYY
--config configs/default.yml
--output-root outputs/local_tests
--establishment-id IDENTIFICADOR
--analysis-end-date YYYY-MM-DD
```

Si se omite el identificador se utiliza el nombre del archivo. Si se omite la
fecha final se registra la fecha UTC de ejecución.

Para ejecutar un único año:

```powershell
uv run python pruebas.py C:/Users/Facu/Desktop/costa-uru.geojson `
  --full-pipeline `
  --hls-year 2023 `
  --establishment-id costa-uru
```

Esto ejecuta la preparación espacial, el composite anual y la línea base
forestal 2020. La detección no se ejecuta en modo anual y el resumen registra
`single_year_mode_requires_range`; no se fingen resultados.

Para ejecutar el pipeline completo hasta el Paso 14:

```powershell
uv run python pruebas.py C:/Users/Facu/Desktop/costa-uru.geojson `
  --full-pipeline `
  --hls-start-year 2019 `
  --hls-end-year 2024 `
  --establishment-id costa-uru
```

`--hls-year` y el rango son alternativas mutuamente excluyentes. El rango es
inclusivo, exige ambos extremos y rechaza el año calendario todavía abierto.
Todo rango de `--full-pipeline` genera automáticamente DJF/MAM/JJA/SON, línea
base 2020 y detección de perturbaciones; `--seasonal` se conserva solamente
por compatibilidad.

Para materializar las cuatro estaciones y construir el cubo virtual:

```powershell
uv run python pruebas.py C:/Users/Facu/Desktop/costa-uru.geojson `
  --full-pipeline `
  --hls-start-year 2020 `
  --hls-end-year 2022
```

Conviene comenzar por un único año y un ROI pequeño: cada año estacional
produce cuatro períodos y cinco GeoTIFF por período. El preflight rechaza la
corrida completa antes de GEE si excede el presupuesto configurado.

La misma corrida agrega `json/temporal/seasonal/coverage.json`,
`tables/seasonal/summary.csv` y tres figuras bajo `figures/seasonal/qa/`,
incluido `rgb_timeline.png` para comparar todos los períodos desde `t0` hasta
`tx` en una sola lámina. Los estados de
cobertura describen disponibilidad de observaciones; NO implican cambio,
deforestación ni cumplimiento.

Si el rango pedido comienza en 2019, el runner obtiene 2017–2018 como soporte
histórico interno para no reducir silenciosamente las referencias mínimas por
estación. Esos períodos no se publican en el cubo solicitado. CCDC consume
HLSL30 denso; sus arrays crudos permanecen en GEE y sólo se descarga un resumen
escalar. El fin analítico es el último día cubierto por la última estación
materializada, no la fecha actual.

## Paquete generado

Cada corrida crea una carpeta nueva y nunca sobrescribe una carpeta existente:

```text
outputs/local_tests/<establecimiento>__<fecha-UTC>__<analysis-id>/
├── json/
│   ├── run/                     # manifiesto, resumen y entorno
│   ├── input/                   # ingesta y vector convertido
│   ├── geometry/                # representaciones, validación y área
│   ├── configuration/           # configuración resuelta y plan de fuentes
│   ├── gee/                     # inventario remoto de escenas
│   ├── evidence/                # metadatos concentrados de línea base 2020
│   └── temporal/
│       ├── annual/[YYYY]/       # contratos generales y metadatos por año
│       └── seasonal/[YYYY-EEE]/ # contratos generales y metadatos por estación
├── figures/
│   ├── annual/[YYYY|qa]/
│   ├── seasonal/[YYYY-EEE|qa]/
│   └── evidence/                # QA de línea base y perturbaciones
├── tiffs/
│   ├── annual/YYYY/
│   ├── seasonal/YYYY-EEE/
│   └── evidence/                # línea base y dos rasters de perturbaciones
├── tables/
│   ├── annual/
│   ├── seasonal/
│   └── evidence/                # una tabla temporal de perturbaciones
└── source/                      # fuente original y sidecars SHP/DBF/SHX/PRJ
```

El bundle `3.0.0` aplica el criterio **tipo físico → dominio o cadencia →
período → rol del activo**. Así, el nombre corto describe qué es el archivo
(`reflectance.tif`, `coverage.json`) y la ruta aporta su contexto. `source/` se
mantiene separado porque puede contener formatos arbitrarios.

`json/run/manifest.json` registra tamaño y SHA-256 de cada artefacto, además de los
hashes científico y de ejecución, la identidad del análisis y si el árbol de
trabajo Git estaba modificado. El manifiesto se excluye de su propia lista para
evitar un hash autorreferencial.

`json/configuration/source_plan.json` enumera las colecciones HLS v2 y sus
bandas requeridas, pero declara `remote_data_accessed: false`: es un plan
local, no el resultado de una consulta. La procedencia y las reglas de las
fuentes forestales utilizadas se concentran en
`json/evidence/forest_baseline_2020.json`.

El Paso 14 agrega exactamente cinco activos, sin crear árboles por detector:

```text
json/evidence/disturbance_detection.json
tiffs/evidence/disturbance_summary.tif
tiffs/evidence/disturbance_diagnostics.tif
figures/evidence/disturbance_detection.png
tables/evidence/disturbance_period_summary.csv
```

Los GeoTIFF de perturbaciones mantienen nodata en toda celda exterior a la
huella válida del AOI y registran `grid_sha256`. Si CCDC falla antes de
descargar su resumen escalar, el JSON declara
`scalar_summary_downloaded: false`; la evidencia robusta puede conservarse,
pero no se simula disponibilidad del segundo detector.

Una falla al preparar la colección HLSL30 densa usada solamente por CCDC sigue
esa misma política: `ccdc.status` queda en `unavailable_global_failure` y
`ccdc.failure_code` conserva únicamente etapa, intervalo y categoría
sanitizados. No aborta la corrida ni inventa desacuerdo. En cambio, una falla
de la serie HLS estacional principal sí aborta antes de publicar cualquier
carpeta de corrida, porque esa serie alimenta al detector robusto y al cubo
temporal solicitado.

`json/input/ingestion.json` registra formato, capa, cantidad de entidades, CRS,
si hubo disolución y SHA-256 de cada archivo original. La geometría original
interpretada usa `.json`, no `.geojson`, porque puede
permanecer en un CRS distinto de WGS 84. Las geometrías normalizada y de
análisis sí se exportan como GeoJSON en EPSG:4326.

## Límites científicos

El resumen indica la etapa más avanzada alcanzada y siempre conserva
`final_assessment_generated: false`. En el pipeline disponible:

- se valida y, si corresponde, repara la geometría;
- se normaliza a EPSG:4326;
- se calcula la superficie en hectáreas;
- se valida localmente qué productos HLS serían compatibles con el benchmark;
- opcionalmente se consultan IDs, fechas y nubosidad de escenas HLS;
- opcionalmente se genera un composite anual RGB e índices con píxeles reales;
- opcionalmente se genera una serie de composites anuales sobre grilla fija,
  con QA total y separado para HLSL30 y HLSS30;
- `--full-pipeline` construye evidencia forestal 2020 por convergencia de JRC,
  ESA WorldCover y Hansen;
- esa convergencia no es una probabilidad calibrada ni verdad de terreno;
- con un rango se detectan señales transitorias, persistentes y desacuerdos;
- no se atribuye uso posterior;
- no se determina cumplimiento EUDR.

Los archivos de `outputs/` son productos locales generados y están ignorados
por Git. Su trazabilidad se garantiza dentro del paquete mediante hashes y
metadatos; no deben incorporarse al repositorio si contienen geometrías reales
o sensibles.
