# Primer composite anual HLS

> Compatibilidad: desde el Paso 12.1 el núcleo también admite intervalos
> explícitos mediante `HlsTemporalCompositeRequest`. Este documento conserva
> el flujo anual usado por los Pasos 10 y 11.

## Alcance

Esta etapa comprueba el flujo completo vectorial → GEE → GeoTIFF → PNG. No
implementa línea base forestal, cambio, atribución ni una conclusión EUDR.

## Flujo científico

1. Filtrar HLSL30 y HLSS30 por ROI y año calendario.
2. Normalizar azul, verde, rojo, NIR, SWIR1 y SWIR2.
3. Resolver tiles MGRS desde HLSL30 y restringir HLSS30 por `MGRS_TILE_ID`.
4. Descartar fill, nube, adyacencia, sombra, nieve y aerosol alto con Fmask.
5. Conservar agua y usar la reflectancia física ya escalada por Earth Engine.
6. Combinar ambas colecciones y calcular mediana anual por banda.
7. Calcular NDVI, EVI2, NBR, NDMI, NMDI, LSWI, NIRv y kNDVI.
8. Conservar el conteo de observaciones válidas por píxel total y separado
   para HLSL30 y HLSS30.

Los índices se calculan desde la reflectancia mediana. No son la mediana de
índices calculados escena por escena. NDMI y LSWI usan la misma expresión
NIR/SWIR1 en esta convención y, por lo tanto, son equivalentes.

## Descarga

Los cinco GeoTIFF se solicitan a 30 m en el huso UTM local que contiene por
completo al ROI. Esta grilla se versiona por separado del CRS equivalente
EPSG:6933 usado para medir superficie: ambos son métricos, pero cumplen
responsabilidades distintas. Si el ROI cruza un huso UTM, la corrida falla de
forma explícita en esta etapa, sin elegir una grilla silenciosamente.

La estimación conservadora usa ancho × alto × bandas × bytes por muestra. Si
se superan 32.000.000 bytes o 10.000 píxeles por eje, la corrida falla sin
remuestrear ni dividir el ROI silenciosamente.

Cada solicitud fija explícitamente CRS, transformación afín y dimensiones del
`RasterGridSpec`; no usa una extensión implícita elegida por GEE. Cada GeoTIFF
descargado se reabre y verifica:

- CRS, transformación y resolución;
- cantidad, orden y nombres de bandas;
- nodata y tipos;
- presencia de píxeles válidos;
- mínimos y máximos por banda.

El composite anual es un producto requerido: si cualquiera de sus bandas de
reflectancia o índices queda completamente en `nodata`, la materialización
falla y no publica el bundle. Esta política estricta no debe confundirse con
la serie estacional, donde un período completamente sin observaciones se
conserva explícitamente como dato faltante.

Los conteos tienen otra semántica: ausencia de una observación válida equivale
a `0` **dentro del ROI**. Fuera del ROI permanecen enmascarados y se exportan
como `nodata`; nunca se convierte el exterior en cero.

## Diagnóstico de construcción

La construcción remota se divide en etapas con nombres estables:
`remote_aoi`, `source_lookup`, `l30_collection`, `l30_inventory`,
`s30_collection`, `s30_inventory`, `normalize_l30`, `normalize_s30`, `merge`,
`reflectance`, `total_count`, `l30_count`, `s30_count` e `indices`.

Un fallo inesperado informa período, etapa y categoría sanitizada. Por ejemplo:

```text
2023_l30_collection_permission_denied
20221201_20230301_indices_service_unavailable
2022_djf_s30_inventory_hlss30_inventory_system_time_start_timeout
```

Los años usan `YYYY`; una ventana estacional usa `YYYY_estacion`; un intervalo
genérico usa `YYYYMMDD_YYYYMMDD`. Si una operación ya produjo un
`HlsCompositeError` detallado, ese código se conserva como sufijo dentro del
contexto de período y etapa. Nunca se incorpora el texto remoto, una URL, un
token ni un identificador de proyecto.

Estas etapas no agregan reintentos. La única excepción sigue siendo la lectura
idempotente de campos del inventario mediante `getInfo`, documentada para la
serie anual. Validaciones locales anteriores a la construcción, como un AOI
inválido, conservan sus códigos históricos.

## PNG

Los PNG se generan localmente desde los GeoTIFF validados. Usan escalas
declaradas en `configs/default.yml`, nodata transparente y contorno del ROI.
Son productos de inspección; los GeoTIFF siguen siendo los datos científicos.

## Limitaciones

- La mediana anual es una imagen sintética, no una adquisición individual.
- Puede suavizar fenología y eventos breves.
- Una escena coincidente no garantiza un píxel válido dentro del ROI.
- Los problemas conocidos de HLS deben considerarse en etapas posteriores de
  validación.
- `filterBounds` por sí solo no es confiable para HLSS30 en el catálogo actual;
  el bundle registra las tiles MGRS realmente usadas.
- Ningún producto de esta etapa representa detección de deforestación.
