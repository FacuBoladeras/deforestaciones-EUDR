# Grilla raster temporal — Paso 11

## Alcance

El Paso 11.0 definió la identidad espacial que comparten todos los productos de
una serie temporal. La derivación es local y determinística; los pasos 11.1–11.4
ya la usan para solicitar y validar datos reales. Ninguna de estas operaciones
detecta cambios.

El contrato ejecutable es `RasterGridSpec` y su JSON Schema canónico es
`data/schemas/raster-grid-v1.0.0.json`.

## Derivación

`derive_raster_grid_spec` recibe:

- un `Polygon` o `MultiPolygon` válido en EPSG:4326;
- un CRS objetivo proyectado y expresado en metros;
- una resolución positiva;
- el valor nodata del producto.

La envolvente del AOI se proyecta y se ajusta **hacia afuera** a múltiplos de
la resolución medidos desde el origen del CRS:

- `left` y `bottom` usan `floor`;
- `right` y `top` usan `ceil`;
- la transformación es norte-arriba y sin rotación;
- `width` y `height` se derivan de la extensión alineada.

Este ajuste puede agregar píxeles exteriores, pero nunca recorta el AOI. Por
eso `aoi_mask_required` es siempre `true`: la grilla define almacenamiento y
alineación, no reemplaza la máscara exacta del territorio.

## Campos

- `schema_version`: versión del contrato, actualmente `1.0.0`;
- `target_crs`: identificador normalizado del CRS;
- `resolution_m`: tamaño de píxel;
- `width`, `height`: dimensiones;
- `transform`: seis coeficientes afines en orden
  `[xScale, xShear, xOrigin, yShear, yScale, yOrigin]`;
- `bounds`: `[left, bottom, right, top]`;
- `alignment_strategy`: `projected_crs_origin_outward_snap`;
- `pixel_orientation`: `north_up`;
- `nodata`: codificación explícita de ausencia;
- `aoi_mask_required`: recuerda que deben enmascararse los píxeles exteriores;
- `grid_sha256`: identidad SHA-256 de los campos exclusivamente espaciales.

`grid_sha256` excluye `nodata` y `aoi_mask_required`. Dos productos con distinta
codificación nodata pueden compartir la misma grilla y compararse píxel a
píxel. El modelo rechaza un hash que no corresponda a la transformación,
dimensiones, bounds, CRS y resolución declarados.

## Invariantes

El contrato rechaza:

- geometrías vacías, no poligonales, inválidas o fuera del rango WGS 84;
- CRS desconocidos, geográficos o con unidades no métricas;
- resolución no positiva;
- nodata o coordenadas no finitas;
- transformaciones rotadas o inconsistentes;
- bounds incompatibles con la transformación y las dimensiones;
- campos desconocidos o un hash adulterado.

## Integración desde el Paso 11.1

El pipeline reutiliza una única instancia de `RasterGridSpec` para todos los
años. Cada solicitud usa `crs`, `crs_transform` y `dimensions`; no delega a GEE
la elección implícita de origen o extensión. Cada GeoTIFF se reabre y se rechaza
si difieren CRS, dimensiones, transformación o bounds.

El bundle de serie publica el contrato efectivo como `temporal/grid.json`.
El modo compatible de un año lo publica como `spatial/raster_grid.json`.
