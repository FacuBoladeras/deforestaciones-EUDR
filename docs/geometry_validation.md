# Validación geométrica local

## Alcance

`deforestation_pipeline.geometry` es la frontera de dominio espacial entre los
contratos GeoJSON de transporte y cualquier análisis posterior. Este paso sólo
interpreta, normaliza y valida geometrías sintéticas o autorizadas. No calcula
superficies, no selecciona CRS equivalentes o UTM y no consulta fuentes remotas.

La API principal es:

```python
from deforestation_pipeline.geometry import validate_geometry

validated = validate_geometry(
    establishment.geometry,
    establishment.geometry_crs,
    jurisdiction_boundary_wgs84=authorized_boundary,
)
```

`GeoJSONGeometry` y `EstablishmentInput` permanecen como contratos de
transporte. La lógica GIS no fue incorporada a los modelos Pydantic.

## Geometrías conservadas

`ValidatedGeometry` es inmutable y mantiene tres representaciones separadas:

- `original_interpreted`: geometría declarada, interpretada en su CRS fuente;
- `normalized_wgs84`: misma geometría reproyectada a EPSG:4326 mediante
  `always_xy=True`;
- `analysis_geometry`: geometría válida que puede usar una etapa posterior.

Si la geometría normalizada es inválida, se intenta `make_valid`. La reparación
nunca reemplaza el original: `repair_performed`, `warnings` y
`quality_flags` registran explícitamente lo ocurrido. Un resultado vacío, aún
inválido o de tipo no soportado se rechaza.

Los anillos abiertos de Polygon y MultiPolygon se rechazan inspeccionando las
coordenadas GeoJSON crudas antes de llamar a Shapely. Esto evita que un cierre
implícito altere silenciosamente la geometría declarada.

## Jurisdicción

La pertenencia jurisdiccional sólo se evalúa cuando el llamador proporciona una
frontera poligonal explícita, válida y expresada en EPSG:4326. La validación usa
`covers`, por lo que acepta geometrías ubicadas sobre el borde.

El proyecto no incluye ni aproxima todavía una frontera de Argentina. Una capa
oficial, autorizada y versionada deberá ingresar mediante el catálogo de datos
en un incremento posterior. Si no se proporciona una frontera, el resultado
incluye `jurisdiction_not_evaluated` y no permite afirmar pertenencia nacional.

La correspondencia entre un punto declarado y un polígono productivo se valida
por separado con:

```python
validate_point_polygon_correspondence(point_wgs84, polygon_wgs84)
```

Esta función también usa `covers` para aceptar un punto ubicado exactamente
sobre el límite.

## Fuera de alcance

- cálculo de hectáreas y aplicación del umbral de 0,5 ha;
- elección de CRS equivalente o huso UTM;
- simplificación o modificación cartográfica;
- descarga de límites administrativos;
- geometrías reales de productores;
- procesamiento raster, GEE, HLS o Sentinel.
