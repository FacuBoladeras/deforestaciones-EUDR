# Serie anual HLS — Paso 11

## Propósito

El Paso 11 transforma el composite anual aislado en una serie comparable,
auditable y acotada. Todavía no detecta cambios ni clasifica deforestación.

## Flujo

1. Validar un rango inclusivo de años calendario cerrados.
2. Derivar una única `RasterGridSpec` para el ROI.
3. Estimar el peso sin comprimir de los cinco rasters por año y rechazar la
   corrida completa antes de llamar a GEE si excede el presupuesto.
4. Construir por año la mediana HLSL30+HLSS30 y sus índices.
5. Descargar sobre la misma transformación y dimensiones:
   - reflectancia de seis bandas;
   - ocho índices configurados;
   - conteo válido total;
   - conteo válido HLSL30;
   - conteo válido HLSS30.
6. Reabrir cada GeoTIFF y verificar igualdad exacta de grilla.
7. Rasterizar el ROI con semántica de centro de píxel y calcular QA y
   estadísticas únicamente dentro del territorio.
8. Generar tablas y figuras locales sin modificar los datos científicos.

## Productos

- `json/temporal/annual/grid.json`: identidad espacial única;
- `json/temporal/annual/series_metadata.json`: rango, fuentes, método y rutas;
- `json/temporal/annual/coverage.json`: QA anual total y por sensor;
- `json/temporal/annual/YYYY/`: metadatos e inventario de cada año;
- `tiffs/annual/YYYY/`: cinco productos raster por año;
- `figures/annual/YYYY/`: figuras del composite anual;
- `tables/annual/summary.csv`: p10, mediana y p90 por variable y año;
- `figures/annual/qa/rgb_panel.png`: comparación RGB anual;
- `figures/annual/qa/index_timeseries.png`: medianas anuales de índices;
- `figures/annual/qa/observation_coverage.png`: cobertura total/L30/S30.

Las figuras son productos de inspección. Los GeoTIFF, JSON y CSV conservan la
evidencia cuantitativa. El año se expresa una sola vez en la carpeta; los
nombres quedan reservados para el rol científico del activo.

## Reintentos de metadatos GEE

El inventario remoto obtiene `size`, `system:index`, `system:time_start` y
`CLOUD_COVERAGE` de forma independiente. Cada operación tiene como máximo dos
intentos totales y un único backoff de 0,1 segundos. Un campo que respondió
correctamente no vuelve a consultarse cuando falla otro campo.

Sólo se reintentan fallos transitorios compatibles con una consulta
idempotente: timeout/deadline, límite de tasa o cuota, errores HTTP 5xx o de
backend/servicio y reinicios de conexión o transferencia. Permisos,
autenticación, argumentos inválidos, límites de memoria y errores no
clasificados fallan en el primer intento.

La lectura aislada de inventario usa únicamente un código estable con producto,
operación y categoría, por ejemplo
`hlsl30_inventory_system_index_service_unavailable`. Cuando el inventario se
ejecuta dentro de un composite, el error final agrega año o período y etapa sin
perder ese detalle:
`2023_l30_inventory_hlsl30_inventory_system_index_service_unavailable`. Nunca
conserva el texto remoto, URLs, tokens ni identificadores de proyecto.

Las demás etapas del composite no se reintentan: varias sólo construyen un
grafo perezoso y repetirlas no resolvería un fallo ni demostraría que la
operación remota sea idempotente. Su diagnóstico usa el formato
`periodo_etapa_categoria`; la lista completa de etapas está documentada en
`docs/hls_annual_composite.md`.

## QA

El pipeline comprueba localmente que:

- el conteo total sea igual a L30 + S30;
- los estadísticos se calculen dentro del ROI;
- una variable sólo sea válida donde el conteo total sea mayor que cero;
- todos los años compartan `grid_sha256`;
- los años sin observaciones permanezcan explícitos y no se interpolen.

`complete_observed_coverage`, `partial_observed_coverage` y
`no_observed_coverage` expresan cobertura observacional, no riesgo ambiental.

## Límites

- Una mediana anual puede suavizar eventos breves y fenología.
- La serie no construye todavía bosque 2020.
- No compara años para declarar pérdida.
- No atribuye cambios a agricultura o ganadería.
- No produce una conclusión EUDR.
