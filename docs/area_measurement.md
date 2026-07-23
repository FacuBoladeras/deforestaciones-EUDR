# Medición de superficies

La medición de superficies es una operación de dominio separada del ingreso de
archivos y del procesamiento raster. Recibe exclusivamente una
`ValidatedGeometry` y mide su campo `analysis_geometry`; nunca sustituye ni
modifica la geometría declarada original.

## Unidad y método

Las superficies se calculan en metros cuadrados mediante el área planar de una
geometría reproyectada y luego se convierten a hectáreas usando
`1 ha = 10 000 m²`.

Como control automatizado independiente, un polígono sintético medido en
EPSG:6933 se compara con el área elipsoidal de `pyproj.Geod` con una tolerancia
relativa de 2 partes por millón. Esa tolerancia valida métodos numéricos; tampoco
representa incertidumbre de la fuente.

No se calcula área en EPSG:4326 y no se utiliza cantidad de píxeles.
Todas las transformaciones usan `always_xy=True`. El resultado registra:

- superficie total y por componente;
- CRS y estrategia de cálculo;
- método utilizado;
- relación con el umbral;
- tolerancia numérica;
- zona y hemisferio cuando se usa UTM.

Los huecos de un polígono se restan. En un `MultiPolygon`, cada polígono es un
componente y el total es la suma de sus áreas.

## Estrategias de CRS

### `AUTO_EQUAL_AREA`

Usa EPSG:6933 (WGS 84 / NSIDC EASE-Grid 2.0 Global), un CRS equivalente,
explícito y estable entre corridas. Su dominio cubre las latitudes de Argentina
y es la estrategia predeterminada. También permite comparar establecimientos
que cruzan límites de zonas UTM sin un cambio silencioso de método.

### `LOCAL_UTM`

Selecciona determinísticamente el EPSG de la zona UTM y hemisferio que contienen
la geometría completa. La estrategia rechaza geometrías que cruzan una zona UTM
o el ecuador y recomienda `AUTO_EQUAL_AREA`. No existe fallback automático:
cambiar de estrategia es una decisión auditable.

UTM es útil como control local y para operaciones métricas posteriores, pero no
es la estrategia general para geometrías que abarcan más de una zona.

## Umbral de 0,5 ha

La relación se conserva explícitamente como:

- `below`;
- `equal`;
- `above`.

La definición forestal exige una superficie **superior a 0,5 ha**. Por lo tanto,
`equal` no equivale a `above`.

La tolerancia predeterminada de `1e-9 ha` se usa únicamente para absorber ruido
de coma flotante en la comparación. Es aproximadamente `0,00001 m²` y **no**
representa precisión cartográfica, error posicional ni incertidumbre
científica. La incertidumbre de la fuente deberá modelarse y reportarse por
separado.

## Limitaciones

- El resultado hereda la calidad, resolución y exactitud posicional de la
  geometría de entrada.
- Una reparación topológica puede cambiar el área analizable; el original y la
  reparación permanecen separados en `ValidatedGeometry`.
- La medición no decide por sí sola si existe bosque ni conversión.
- La relación con 0,5 ha no reemplaza la evaluación de cobertura, persistencia,
  atribución o incertidumbre.
