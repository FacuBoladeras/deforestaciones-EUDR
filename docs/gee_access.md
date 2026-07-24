# Acceso acotado a Google Earth Engine

El Paso 9 incorporó la consulta opcional de metadatos. El Paso 10 mantiene ese
modo y agrega otra acción explícita capaz de construir un composite anual,
descargar GeoTIFF pequeños y renderizar PNG localmente.

## Seguridad de credenciales

Durante el desarrollo local se admite una clave JSON de cuenta de servicio en
`credentials.json`. El archivo:

- está excluido explícitamente en `.gitignore`;
- nunca se copia al bundle;
- nunca se incluye en hashes, logs o mensajes de error;
- no expone correo, project ID, clave privada ni token URI.

Los errores externos se reducen a códigos sanitizados como:

- `invalid_credentials`;
- `project_not_registered`;
- `earth_engine_api_disabled`;
- `permission_denied`;
- `initialization_failed`.

Una clave privada local es aceptable sólo para desarrollo controlado. En
producción se debe preferir Application Default Credentials o identidades de
carga de trabajo, evitando claves descargables.

## Consulta manual

La consulta remota es opt-in:

```powershell
uv run python pruebas.py data/samples/local_test_polygon.geojson `
  --establishment-id gee-smoke `
  --analysis-end-date 2026-07-24 `
  --query-gee `
  --gee-start-date 2021-01-01 `
  --gee-end-date 2021-01-08 `
  --max-scenes-per-source 10
```

`--gee-end-date` es exclusiva, igual que `filterDate` de Earth Engine.

Límites del inventario:

- máximo 366 días por consulta;
- máximo 50 escenas devueltas por colección;
- AOI `Polygon` o `MultiPolygon` válida en EPSG:4326;
- sólo HLSL30 y HLSS30 catalogadas;
- ningún píxel descargado salvo que se agregue `--build-hls-composite`.

Aunque la lista devuelta esté limitada, el resultado conserva
`matched_scene_count` y `truncated` para informar cuántas escenas coincidieron
realmente.

La restricción espacial usa `filterBounds` sobre HLSL30 para resolver las tiles
MGRS y filtra HLSS30 por `MGRS_TILE_ID`. Esto evita falsos positivos observados
en los metadatos espaciales de S30.

## Artefacto generado

Cuando la consulta termina correctamente, el bundle agrega:

```text
gee/
└── scene_metadata.json
```

El archivo registra:

- período consultado;
- fecha de consulta;
- SHA-256 de la AOI, sin repetir sus coordenadas;
- colección y producto;
- cantidad coincidente y cantidad devuelta;
- ID, fecha y nubosidad de cada escena;
- `remote_data_accessed: true`;
- `metadata_only: true`.

Las colecciones consultadas aparecen también en `manifest.datasets` con fecha
de acceso y restricciones. Esto no significa que se hayan usado sus píxeles
para una evaluación.

## Composite anual y descarga

```powershell
uv run python pruebas.py data/samples/local_test_polygon.geojson `
  --query-gee `
  --build-hls-composite `
  --gee-start-date 2023-01-01 `
  --gee-end-date 2024-01-01
```

La descarga usa `ee.Image.getDownloadURL` únicamente si cada raster estimado
queda por debajo de 32 MB y 10.000 píxeles por dimensión. El pipeline nunca
reduce la resolución para evitar el límite. AOI mayores deberán adoptar en el
futuro una exportación batch o un teselado explícito.

## Límites de esta etapa

La colección GEE ya entrega reflectancia escalada; se aplica multiplicador `1`
y offset `0`. La escala nativa `0.0001` se conserva sólo como procedencia. Las
URLs firmadas de descarga son efímeras y no se guardan en el bundle.

El éxito de autenticación tampoco implica que un análisis sea gratuito o que
la modalidad de proyecto sea adecuada para producción. El proyecto Cloud debe
estar registrado, tener Earth Engine API habilitada y cumplir sus condiciones
operativas y comerciales.
