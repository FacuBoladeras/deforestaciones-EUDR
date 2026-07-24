# Catálogo local de fuentes

`data/catalog.yml` registra fuentes candidatas de manera local, versionada y
tipada. Cargar el catálogo no inicializa Earth Engine, no lista escenas y no
transfiere datos.

## Primera familia: HLS v2

El benchmark usa HLS como una familia formada por dos productos separados:

| Producto | Colección Earth Engine | Inicio | Resolución |
| --- | --- | --- | --- |
| HLSL30 | `NASA/HLS/HLSL30/v002` | 2013-04-11 | 30 m |
| HLSS30 | `NASA/HLS/HLSS30/v002` | 2015-11-28 | 30 m |

Ambos productos son provistos por NASA LP DAAC y ofrecen reflectancia ajustada
por BRDF a nadir sobre una grilla común. La fecha final no se copia al catálogo
local porque las colecciones continúan actualizándose; registrar una fecha
dinámica como constante volvería obsoleto el descriptor.

Metadatos verificados el 2026-07-24 contra:

- [catálogo Earth Engine de HLSL30](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSL30_v002);
- [catálogo Earth Engine de HLSS30](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSS30_v002);
- [HLS User Guide v2](https://lpdaac.usgs.gov/documents/1698/HLS_User_Guide_V2.pdf).

## Roles espectrales comunes

El catálogo no finge que ambos productos usan el mismo nombre de banda. Define
roles científicos y conserva su correspondencia por producto:

| Rol | HLSL30 | HLSS30 |
| --- | --- | --- |
| azul | `B2` | `B2` |
| verde | `B3` | `B3` |
| rojo | `B4` | `B4` |
| NIR | `B5` | `B8A` |
| SWIR 1 | `B6` | `B11` |
| SWIR 2 | `B7` | `B12` |
| calidad | `Fmask` | `Fmask` |

Estos roles permiten generar RGB y preparar NDVI, EVI2, NBR, NDMI, NMDI,
LSWI, NIRv y kNDVI.

Los bits `Fmask` catalogados son:

- bit 1: nube;
- bit 2: adyacente a nube o sombra;
- bit 3: sombra de nube;
- bit 4: nieve o hielo;
- bit 5: agua;
- bits 6–7: nivel de aerosol.

El adaptador del Paso 10 descarta fill, nube, adyacencia, sombra, nieve y
aerosol alto. El agua se conserva. Los COG nativos documentan factor `0.0001`
y offset `0`, pero una verificación real mostró que las colecciones Earth
Engine ya exponen reflectancia física (`B4` aproximadamente `0.07–0.20` en el
ROI sintético). Por eso el adaptador GEE aplica multiplicador `1`, no vuelve a
escalar.

Para HLSS30 no se usa `filterBounds` como única restricción: algunos metadatos
espaciales producen falsos positivos globales. Las tiles MGRS intersectadas se
resuelven desde HLSL30 y luego HLSS30 se filtra por `MGRS_TILE_ID`.

## Plan de fuente

`build_benchmark_source_plan` cruza el catálogo con `configs/default.yml` y
rechaza:

- ausencia de HLSL30 o HLSS30;
- orden o identidad inesperados;
- resolución distinta de 30 m;
- falta de roles espectrales requeridos;
- estados que impliquen un acceso no representado por este paso.

El plan conserva `remote_data_accessed: false`. `pruebas.py` lo exporta como
`catalog/source_plan.json` para que la siguiente etapa sepa exactamente qué
colecciones y bandas pretende consultar.

## Catálogo no equivale a uso

`data/catalog.yml` contiene fuentes verificadas. `data/licenses.yml` ya
registra HLSL30 y HLSS30 porque el Paso 10 utiliza sus píxeles; cada corrida
también incorpora los registros efectivos en su manifiesto.

Los términos abiertos de los datos NASA no sustituyen las condiciones de la
plataforma usada para accederlos. En particular, las condiciones operativas y
comerciales de Earth Engine deben evaluarse por separado antes de producción.
