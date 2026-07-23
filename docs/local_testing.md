# Runner local de prueba

`pruebas.py` es una interfaz exclusivamente local para recorrer las capacidades
que el pipeline implementa hoy. No inicia un servidor, no usa Google Earth
Engine, no descarga imágenes y no accede a datasets remotos.

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
├── config/
│   └── resolved_config.json
├── geometry/
│   ├── original_interpreted.json
│   ├── normalized_wgs84.geojson
│   ├── analysis_geometry.geojson
│   └── validation.json
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

La geometría original interpretada usa `.json`, no `.geojson`, porque puede
permanecer en un CRS distinto de WGS 84. Las geometrías normalizada y de
análisis sí se exportan como GeoJSON en EPSG:4326.

## Límites científicos

El resumen indica `stage: spatial_preparation` y
`final_assessment_generated: false`. En esta etapa:

- se valida y, si corresponde, repara la geometría;
- se normaliza a EPSG:4326;
- se calcula la superficie en hectáreas;
- no se construye bosque de referencia 2020;
- no se detectan cambios;
- no se atribuye uso posterior;
- no se determina cumplimiento EUDR.

Los archivos de `outputs/` son productos locales generados y están ignorados
por Git. Su trazabilidad se garantiza dentro del paquete mediante hashes y
metadatos; no deben incorporarse al repositorio si contienen geometrías reales
o sensibles.
