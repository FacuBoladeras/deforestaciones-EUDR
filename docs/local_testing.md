# Runner local de prueba

`pruebas.py` es una interfaz local: no inicia un servidor y publica todo en una
carpeta de corrida. Por defecto no accede a datos remotos. Con flags explícitos
puede consultar GEE y, para un ROI pequeño, descargar los primeros productos
raster HLS.

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

Esto ejecuta la preparación espacial, consulta GEE, construye el composite HLS
anual, descarga GeoTIFF pequeños y genera PNG. “Completo” se refiere al estado
actual del repositorio; no incluye todavía detección ni atribución de cambio.

Para ejecutar una serie anual completa del Paso 11:

```powershell
uv run python pruebas.py C:/Users/Facu/Desktop/costa-uru.geojson `
  --full-pipeline `
  --hls-start-year 2019 `
  --hls-end-year 2024 `
  --establishment-id costa-uru
```

`--hls-year` y el rango son alternativas mutuamente excluyentes. El rango es
inclusivo, exige ambos extremos y rechaza el año calendario todavía abierto.

## Paquete generado

Cada corrida crea una carpeta nueva y nunca sobrescribe una carpeta existente:

```text
outputs/local_tests/<establecimiento>__<fecha-UTC>__<analysis-id>/
├── catalog/
│   └── source_plan.json
├── config/
│   └── resolved_config.json
├── geometry/
│   ├── original_interpreted.json
│   ├── normalized_wgs84.geojson
│   ├── analysis_geometry.geojson
│   └── validation.json
├── gee/                         # sólo cuando se usa --query-gee
│   ├── scene_metadata.json
│   ├── composite_input_inventory.json
│   └── composite_metadata.json
├── rasters/                     # sólo con --build-hls-composite
│   ├── hls_annual_reflectance.tif
│   ├── hls_annual_indices.tif
│   ├── hls_valid_observation_count.tif
│   ├── hls_valid_observation_count_l30.tif
│   └── hls_valid_observation_count_s30.tif
├── spatial/
│   └── raster_grid.json         # modo de un año
├── temporal/                    # modo de serie
│   ├── grid.json
│   ├── coverage.json
│   ├── series_metadata.json
│   └── years/YYYY/              # metadatos, cinco TIFF y figuras por año
├── tables/
│   └── hls_annual_summary.csv
├── figures/                     # sólo con --build-hls-composite
│   ├── rgb.png
│   ├── indices_panel.png
│   ├── valid_observations.png
│   ├── temporal/
│   │   ├── annual_rgb_panel.png
│   │   ├── index_timeseries.png
│   │   └── observation_coverage.png
│   └── indices/
├── input/
│   ├── original/                   # fuente y sidecars, por ejemplo SHP/DBF/SHX/PRJ
│   ├── converted.geojson           # única geometría entregada al núcleo
│   └── ingestion.json              # driver, capa, CRS, unión y hashes
├── measurements/
│   └── area.json
├── environment.json
├── manifest.json
└── run_summary.json
```

`manifest.json` registra tamaño y SHA-256 de cada artefacto, además de los
hashes científico y de ejecución, la identidad del análisis y si el árbol de
trabajo Git estaba modificado. El manifiesto se excluye de su propia lista para
evitar un hash autorreferencial.

`catalog/source_plan.json` enumera las colecciones HLS v2 y sus bandas
requeridas, pero declara `remote_data_accessed: false`: es un plan local, no el
resultado de una consulta.

`input/ingestion.json` registra formato, capa, cantidad de entidades, CRS,
si hubo disolución y SHA-256 de cada archivo original. La geometría original
interpretada usa `.json`, no `.geojson`, porque puede
permanecer en un CRS distinto de WGS 84. Las geometrías normalizada y de
análisis sí se exportan como GeoJSON en EPSG:4326.

## Límites científicos

El resumen indica `stage: spatial_preparation` y
`final_assessment_generated: false`. En esta etapa:

- se valida y, si corresponde, repara la geometría;
- se normaliza a EPSG:4326;
- se calcula la superficie en hectáreas;
- se valida localmente qué productos HLS serían compatibles con el benchmark;
- opcionalmente se consultan IDs, fechas y nubosidad de escenas HLS;
- opcionalmente se genera un composite anual RGB e índices con píxeles reales;
- opcionalmente se genera una serie de composites anuales sobre grilla fija,
  con QA total y separado para HLSL30 y HLSS30;
- no se construye bosque de referencia 2020;
- no se detectan cambios;
- no se atribuye uso posterior;
- no se determina cumplimiento EUDR.

Los archivos de `outputs/` son productos locales generados y están ignorados
por Git. Su trazabilidad se garantiza dentro del paquete mediante hashes y
metadatos; no deben incorporarse al repositorio si contienen geometrías reales
o sensibles.
