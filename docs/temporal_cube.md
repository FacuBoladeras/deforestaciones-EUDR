# Cubo temporal estacional — Paso 12.0

## Alcance

El Paso 12.0 define contratos auditables antes de construir datos remotos.
Los Pasos 12.1 y 12.2 generalizan el compositor HLS y preparan una colección
remota perezosa por ventana. Todavía no descargan ni persisten el cubo y no
ejecutan detección de cambios.

## Ventanas

El esquema `meteorological_south_v1` usa cuatro estaciones:

- `DJF`: verano, desde el 1 de diciembre del año anterior hasta el 1 de marzo;
- `MAM`: otoño, desde el 1 de marzo hasta el 1 de junio;
- `JJA`: invierno, desde el 1 de junio hasta el 1 de septiembre;
- `SON`: primavera, desde el 1 de septiembre hasta el 1 de diciembre.

Los intervalos son cerrados al inicio y abiertos al final. El año de `DJF`
corresponde a enero y febrero; diciembre es un período de soporte del año
anterior. Los planes sólo admiten años calendario cerrados y registran fechas
de soporte para que ninguna consulta quede implícita.

## Contrato del cubo

`TemporalCubeSpec` fija:

- la `RasterGridSpec` compartida;
- el plan y hash de ventanas;
- bandas HLS de reflectancia e índices espectrales;
- conteos válidos total, HLSL30 y HLSS30;
- dimensiones explícitas para reflectancia, índices y conteos;
- mediana de reflectancia antes de calcular índices;
- conservación de períodos sin datos, sin interpolación automática.

Los hashes detectan cambios o adulteraciones en planes y especificaciones. Los
esquemas canónicos son:

- `data/schemas/temporal-window-plan-v1.0.0.json`;
- `data/schemas/temporal-cube-spec-v1.0.0.json`.

La materialización estacional y el formato físico del cubo pertenecen a los
siguientes subpasos del Paso 12.

## Paso 12.1: compositor por intervalo

`HlsTemporalCompositeRequest` admite intervalos no vacíos de hasta 366 días,
con límites inicio inclusivo y fin exclusivo. `build_hls_composite` aplica el
mismo enmascarado Fmask, normalización, mediana, índices e inventario completo
que el benchmark anual, pero registra
`indices_derived_from: period_median_reflectance`.

El contrato anual anterior permanece disponible y sigue exigiendo un año
calendario completo. Esto evita romper el Paso 10 y la serie del Paso 11.

## Paso 12.2: colección estacional perezosa

`build_hls_seasonal_composites`:

1. valida que la grilla y la resolución configurada coincidan;
2. construye el `TemporalCubeSpec`;
3. recorre las ventanas en el orden firmado por `plan_sha256`;
4. solicita un composite HLS con los límites exactos de cada ventana;
5. rechaza cualquier respuesta cuyos límites no coincidan.

El resultado queda en estado `seasonal_composites_ready`. Contiene imágenes
perezosas de Earth Engine, no GeoTIFF locales. La descarga sobre grilla fija,
la validación estructural y su publicación en el bundle corresponden al Paso
12.3. El QA científico por estación queda para el Paso 12.5.

## Paso 12.3: materialización estacional

Antes de consultar GEE se valida:

- coincidencia entre resolución configurada y grilla;
- coincidencia de `nodata`;
- límites de descarga por archivo;
- presupuesto total para todas las ventanas.

Cada ventana produce cinco GeoTIFF sobre la misma transformación:

- reflectancia;
- índices;
- conteo válido total;
- conteo válido HLSL30;
- conteo válido HLSS30.

Una estación puede contener escenas coincidentes pero ninguna observación
válida después de Fmask. En ese caso la estación no se elimina ni aborta
genéricamente:

- reflectancia e índices permanecen completamente en `nodata`;
- los conteos valen `0` dentro del ROI y `nodata` fuera;
- la validación registra `valid_pixel_count_by_band = 0` y las bandas en
  `all_nodata_band_names`;
- esta tolerancia sólo aplica cuando **todo** el producto espectral estacional
  está vacío; una única banda vacía junto a otras válidas se considera una
  inconsistencia y detiene la corrida.

Cada período posee su propia carpeta, por ejemplo
`tiffs/seasonal/2022-DJF/reflectance.tif`. Los inventarios y metadatos se
publican bajo `json/temporal/seasonal/2022-DJF/`, y las figuras equivalentes
bajo `figures/seasonal/2022-DJF/`.

## Paso 12.4: cubo virtual

El cubo se publica mediante `json/temporal/seasonal/cube_index.json`. El índice conserva:

- ejes de tiempo, bandas, índices y sensores;
- dimensiones lógicas;
- grilla y especificación temporal;
- rutas de los GeoTIFF de cada período;
- SHA-256 de cada GeoTIFF referenciado;
- política sin interpolación;
- hash de identidad.

Se eligió `virtual_geotiff_collection` en lugar de Zarr porque reutiliza los
productos auditables del Paso 12.3, no duplica píxeles, no agrega dependencias
y mantiene cada activo cuantitativo como archivo independiente. El costo es que un análisis masivo debe
abrir varios GeoTIFF; una exportación Zarr podrá incorporarse más adelante si
las mediciones demuestran que ese costo es relevante.

Los esquemas adicionales son:

- `data/schemas/hls-seasonal-metadata-v2.0.0.json`;
- `data/schemas/temporal-cube-index-v2.0.0.json`.

## Paso 12.5: QA científico estacional

El QA se calcula exclusivamente dentro del ROI rasterizado con semántica de
centro de píxel. Para cada período conserva:

- escenas totales, HLSL30 y HLSS30;
- píxeles observados y fracción válida por sensor;
- mínimo, p05, mediana, p95 y máximo de conteos;
- estado de cobertura completa, parcial o vacía;
- banderas factuales por ausencia de cobertura; una estación vacía agrega
  `insufficient_data`;
- p10, mediana y p90 de cada banda e índice.

La igualdad `conteo_total = conteo_L30 + conteo_S30` es una invariante: una
discrepancia detiene la corrida, no se convierte en advertencia. No se
introdujeron umbrales arbitrarios de “buena” cobertura y no se interpolan
períodos. Las estadísticas espectrales de una estación vacía conservan
`valid_pixel_count = 0` y percentiles nulos; etapas robustas posteriores
reciben `NaN`, por lo que no pueden convertir ausencia de datos en estabilidad.

Productos:

- `json/temporal/seasonal/coverage.json`;
- `tables/seasonal/summary.csv`;
- `figures/seasonal/qa/index_timeseries.png`;
- `figures/seasonal/qa/observation_coverage.png`;
- `figures/seasonal/qa/rgb_timeline.png`: panel único ordenado cronológicamente
  desde `t0` hasta `tx`, con la misma grilla y escala RGB.

El esquema canónico de cobertura es
`data/schemas/hls-seasonal-coverage-v1.0.0.json`.

## Paso 12.6: validación real

El 28 de julio de 2026 se ejecutó el pipeline estacional completo sobre
`data/samples/local_test_polygon.geojson` para el año de temporada 2022.

Resultados:

- cuatro períodos en orden: DJF, MAM, JJA y SON;
- 20 GeoTIFF y 46 PNG;
- 56 filas de resumen espectral;
- una única grilla para todos los rasters;
- cobertura observacional completa del ROI en los cuatro períodos;
- igualdad exacta entre conteo total y suma HLSL30 + HLSS30;
- hashes del manifiesto y de todos los activos del cubo verificados;
- cero interpolaciones;
- cero subdirectorios dentro de los grupos del bundle.

Inventario de escenas total/HLSL30/HLSS30:

- DJF: 65/16/49;
- MAM: 66/16/50;
- JJA: 63/16/47;
- SON: 66/18/48.

La validación confirma el transporte, la estructura y el QA del cubo para este
caso. NO valida todavía detección de cambio, representatividad regional ni
exactitud temática.
