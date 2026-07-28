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

- `temporal/grid.json`: identidad espacial única;
- `temporal/series_metadata.json`: rango, fuentes, método, estimación y rutas;
- `temporal/coverage.json`: QA anual total y por sensor;
- `temporal/years/YYYY/`: cinco TIFF, inventario, metadatos y figuras anuales;
- `tables/hls_annual_summary.csv`: p10, mediana y p90 por variable y año;
- `figures/temporal/annual_rgb_panel.png`: comparación RGB anual;
- `figures/temporal/index_timeseries.png`: medianas anuales de índices;
- `figures/temporal/observation_coverage.png`: cobertura total/L30/S30.

Las figuras son productos de inspección. Los GeoTIFF, JSON y CSV conservan la
evidencia cuantitativa.

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
