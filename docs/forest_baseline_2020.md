# Línea base forestal 2020 — Paso 13

## Propósito y límite

El Paso 13 construye evidencia raster auditable de cobertura forestal al
`2020-12-31`. No detecta cambios posteriores, no atribuye usos y no determina
cumplimiento EUDR.

El resultado principal es una **fracción de evidencia no calibrada**, no una
probabilidad de bosque. Para hablar de probabilidad sería necesario entrenar y
calibrar un modelo con muestras independientes.

## Frontera temporal y atributos HLS

- Fecha de referencia inmutable: `2020-12-31`.
- Ventana benchmark: `2019-01-01` a `2021-01-01` exclusivo.
- Se construye un composite HLS interno para cada año calendario completo.
- Para reflectancias e índices se conserva la media interanual y el rango
  interanual.
- Se acumula el conteo de observaciones válidas.
- No se incorporan observaciones posteriores al corte ni se interpolan
  períodos largos.

Se usan años calendario dedicados porque las estaciones meteorológicas DJF
cruzan el límite anual y podrían introducir observaciones de 2018 o 2021.
Estos atributos son evidencia espectral auxiliar; todavía no entrenan un
clasificador forestal.

## Fuentes y roles

| Fuente | Rol | Regla de evidencia 2020 |
| --- | --- | --- |
| JRC GFC2020 V3 | núcleo, mapa forestal | `Map == 1` |
| ESA WorldCover 2020 v100 | núcleo, cobertura del suelo | `Map in {10, 95}` |
| Hansen GFC v1.13 | apoyo derivado | copa 2000 ≥ 10 % y sin pérdida hasta 2020 |

Sólo JRC y ESA participan en el consenso núcleo. Hansen se conserva como banda
de apoyo porque reconstruir 2020 desde cobertura 2000 y pérdida no representa
regeneración ni ganancia forestal. Ninguna fuente individual es verdad de
terreno.

En estos productos globales wall-to-wall, un píxel enmascarado por ausencia de
la clase forestal se normaliza explícitamente a evidencia `0` dentro del ROI;
el exterior del footprint de análisis conserva nodata `-9999`.

MapBiomas Chaco permanece diferido hasta disponer de un asset oficial estable
y auditar su semántica temporal respecto de la fecha de corte.

## Convergencia e incertidumbre

Por píxel se generan:

- evidencia binaria por fuente;
- cantidad de fuentes núcleo válidas;
- fracción de fuentes núcleo que aportan evidencia forestal;
- consenso, sólo cuando todas las fuentes núcleo válidas coinciden;
- desacuerdo, cuando las fuentes núcleo válidas difieren.

Se requieren al menos dos fuentes núcleo válidas. El desacuerdo no se fuerza a
una respuesta binaria y debe permanecer visible para revisión.

La definición operacional conserva como constantes de dominio 0,5 ha, altura
potencial mínima de 5 m y cobertura de copa mínima de 10 %, excluyendo uso
predominantemente agrícola o urbano. Los mapas actuales no demuestran por sí
solos todos esos criterios. La conectividad y el umbral de superficie se
evaluarán en la agregación espacial posterior.

## Grilla y materialización

El benchmark se publica a 30 m sobre una única `RasterGridSpec` métrica y
alineada. Todas las capas comparten dimensiones, transformación, CRS y máscara
exacta del ROI. La resolución principal futura seguirá siendo Sentinel-2 a
10 m; mezclar resoluciones exigirá declarar remuestreo y grilla objetivo.

## Contrato compacto de salidas

La corrida reutiliza un único dominio plano `evidence` por tipo físico:

```text
json/evidence/forest_baseline_2020.json
figures/evidence/forest_baseline_2020.png
tiffs/evidence/forest_evidence_fraction_2020.tif
tiffs/evidence/forest_consensus_2020.tif
tiffs/evidence/forest_disagreement_2020.tif
tiffs/evidence/forest_source_count_2020.tif
tiffs/evidence/forest_source_evidence_2020.tif
tiffs/evidence/forest_features_2020.tif
```

`forest_source_evidence_2020.tif` agrupa las fuentes como bandas para evitar
una carpeta o un archivo por proveedor. El JSON concentra reglas, fuentes,
semántica, limitaciones, grilla, estimaciones y validaciones. El PNG presenta
en una sola figura la fracción, consenso, desacuerdo y las evidencias por
fuente.

## Interpretación correcta

La línea base permite responder qué fuentes observan cobertura compatible con
bosque al corte y dónde coinciden o discrepan. No permite afirmar:

- que el establecimiento está certificado libre de deforestación;
- que una pérdida posterior ocurrió;
- que una perturbación fue conversión agropecuaria;
- que la ausencia de evidencia equivale a ausencia de bosque.

El resumen de corrida conserva `final_assessment_generated: false`.
