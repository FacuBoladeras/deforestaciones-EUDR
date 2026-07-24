# Runner local de prueba

`pruebas.py` es una interfaz local: no inicia un servidor y publica todo en una
carpeta de corrida. Por defecto no accede a datos remotos. Con flags explícitos
puede consultar GEE y, para un ROI pequeño, descargar los primeros productos
raster HLS.

## Entrada admitida

La primera versión acepta archivos `.geojson` o `.json` con:

- una geometría `Point`, `Polygon` o `MultiPolygon`;
- una `Feature`; o
- una `FeatureCollection` con exactamente una `Feature`.

Se procesa un establecimiento por corrida. Esto evita mezclar geometrías,
identidades y evidencias dentro de un mismo paquete. El CRS se declara con
`--source-crs`; el valor predeterminado es `EPSG:4326`.

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
--source-crs EPSG:4326
--config configs/default.yml
--output-root outputs/local_tests
--establishment-id IDENTIFICADOR
--analysis-end-date YYYY-MM-DD
```

Si se omite el identificador se utiliza el nombre del archivo. Si se omite la
fecha final se registra la fecha UTC de ejecución.

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
│   └── hls_valid_observation_count.tif
├── figures/                     # sólo con --build-hls-composite
│   ├── rgb.png
│   ├── indices_panel.png
│   ├── valid_observations.png
│   └── indices/
├── input/
│   └── source.geojson
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

La geometría original interpretada usa `.json`, no `.geojson`, porque puede
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
- no se construye bosque de referencia 2020;
- no se detectan cambios;
- no se atribuye uso posterior;
- no se determina cumplimiento EUDR.

Los archivos de `outputs/` son productos locales generados y están ignorados
por Git. Su trazabilidad se garantiza dentro del paquete mediante hashes y
metadatos; no deben incorporarse al repositorio si contienen geometrías reales
o sensibles.
