# Contexto científico y de presentación

Actualizado: **1 de septiembre de 2026**.

## 1. Pregunta científica

Para una geometría productiva, el pipeline busca responder:

> ¿Existe evidencia espacial y temporal compatible con conversión de bosque a
> uso agrícola o ganadero después del 31 de diciembre de 2020?

La respuesta automática es evidencia para revisión, no una conclusión legal.

## 2. Reglas de dominio

- Fecha de corte: `2020-12-31`.
- Bosque: superficie superior a 0,5 ha, árboles de al menos 5 m y copa de al
  menos 10 %, excluyendo usos predominantemente agrícolas o urbanos.
- Ganadería, pasturas e infraestructura de cría son uso agrícola.
- Incendio, inundación, sequía, defoliación o recuperación no equivalen
  automáticamente a conversión.
- El mapa global o catastro es evidencia complementaria, no verdad de terreno.
- Intercambio: WGS 84 (`EPSG:4326`) con al menos seis decimales.
- Superficies: CRS equivalente o proyectado apropiado.
- El modelo automático nunca emite `conversion_confirmed`.

## 3. Pipeline implementado

### 3.1 Ingesta y grilla

- Acepta `Polygon` o `MultiPolygon`.
- Valida topología, jurisdicción argentina, tamaño, vértices y área.
- Conserva geometría original y normalizada.
- Usa una grilla HLS local documentada; nodata permanece explícito.
- Los bordes evaluables reciben `EDGE_PIXEL` como QA, no se filtran.

### 3.2 Línea base forestal 2020

Combina evidencia multifuente y RF:

- productos forestales de catálogo;
- variables HLS próximas a 2020;
- RF de 500 árboles para Entre Ríos;
- conteo de fuentes, desacuerdo y completitud.

El RF publica fracción de votos binarios, **no probabilidad calibrada**. Su
transferencia admitida es 2020-2024 y no se extrapola a otras regiones o años
sin validación.

### 3.3 Serie temporal

El producto operativo es HLS a 30 m, con Fmask y ocho índices:

`NDVI`, `EVI2`, `NBR`, `NDMI`, `NMDI`, `LSWI`, `NIRv`, `kNDVI`.

La configuración `1.13.0` usa perfil `lean` por defecto. Consolida transportes
multibanda, conserva rasters necesarios para ciencia y downstream, y evita
duplicados temporales que no aportan trazabilidad.

Sentinel-2 a 10 m sigue siendo el producto objetivo; todavía no es el flujo
principal implementado.

### 3.4 Detección

Se conservan dos resultados espectrales con roles distintos:

1. comparación robusta estacional pre/post;
2. CCDC sobre observaciones HLS densas.

La detección `1.5.0` estabiliza escalas MAD positivas con un piso cuantílico
espacial. Un MAD nulo sigue siendo no estandarizable; no se suaviza
reflectancia.

CCDC y el detector robusto comparten HLS. Su acuerdo NO constituye independencia
de sensor ni fusión de probabilidades. En el dominio RF-first ambos describen
soporte o fecha dentro de un candidato; ninguno propone, amplía ni veta su
geometría.

### 3.5 Candidatos y episodios

Jerarquía:

1. `spectral_candidate`: señal localizada;
2. `candidate_episode`: agrupación espacial-temporal coherente;
3. `conversion_likely_event`: conversión probable tras atribución.

La colección técnica histórica `1.2.0` preserva los candidatos robustos como
antecedente. El dominio candidato `2.0.0` se construye exclusivamente desde
pérdida forestal RF **terminalmente persistente** entre 2021 y 2024 sobre bosque
elegible al corte. Los parches usan conectividad espacial 8, conservan los
subumbrales y no rellenan huecos con buffers o envolventes.

Dentro de cada parche RF se asocian soporte robusto y CCDC. Una señal robusta
fuera del dominio RF se conserva en un inventario sombra de revisión, pero no
crea ni expande candidatos primarios. RF sigue siendo una fuente aprendida no
calibrada; sin soporte robusto el candidato permanece en `review_required` y
nunca se promociona automáticamente. CCDC aporta soporte o fecha y nunca
desbloquea promoción por sí solo.

### 3.6 Ocurrencia agrícola post-evento

Dynamic World es la única fuente temporal agrícola operativa del MVP.

- Adquisición mensual compartida entre candidatos.
- Conteos por lotes sin truncar el inventario.
- El transporte remoto agrupa ventanas mensuales en chunks sobre grupos
  espaciales determinísticos que no superan la grilla configurada.
- Cada chunk descarga por separado conteos `uint16` y probabilidad `float32`;
  omite la razón redundante y la reconstruye como `qualifying/valid`.
- El presupuesto por transferencia es 24 MB sin comprimir, por debajo del
  límite directo de 32 MB; no degrada resolución ni eleva límites.
- Grilla, nodata, áreas, hashes y linaje por candidato.
- La observación comienza el `2021-01-01`, independientemente del onset RF
  estimado. Un mes en el que Dynamic World clase 4 cubre al menos el 5 % del
  footprint completo del candidato satisface el gate operativo de evidencia
  agrícola post-corte. No demuestra pastura, ganadería ni agricultura en
  sentido amplio, y por sí sola no confirma conversión.
- En perfil `lean`, cubo temporal `uint16` por candidato con conteos válidos y
  calificantes por mes; hasta cuatro rasters mensuales quedan para la figura.
- La razón `qualifying/valid` se reconstruye exactamente al evaluar occurrence,
  sin cuantizar umbrales. El formato mensual completo permanece en `debug`.
- Las áreas continúan usando intersección polígono-píxel local `float64`.
  `reduceRegion` y `reduceRegions` no son autoritativos porque su ponderación
  de borde cuantizada no preserva ese contrato exacto.
- El contrato `2.1.0` usa el área exacta completa del candidato como denominador
  del 5 % y clasifica potencia temporal ordinal: `weak` con un mes que alcanza
  el umbral, `moderate` con dos y `strong` con tres o más. No es probabilidad de
  conversión. El área observada se conserva separada de la cobertura máxima.
- La suficiencia de seguimiento, duración y huecos permanece como información
  de calidad; no veta una occurrence válida.
- Rasters de soporte de occurrence, potencia y conjunción para atribución. El
  nombre de componente `agricultural_persistence` y campos `persistent_*` se
  conservan sólo como aliases de transición para consumidores históricos.

La evidencia es política-dependiente y no se trata como verdad de campo.

### 3.7 Atribución `7.0.0`

Un registro sólo pasa a `conversion_likely_event` si cumple conjuntamente:

1. bosque al corte, incluso como subconjunto automático de un candidato basal
   mixto;
2. pérdida poscorte;
3. cambio persistente;
4. ausencia de recuperación respaldada;
5. cobertura agrícola elegible de al menos 5 % del candidato en uno o más meses
   post-corte;
6. soporte espacial conjuntivo;
7. área conjuntiva estrictamente mayor que 0,5 ha;
8. ausencia de explicación alternativa fuerte evaluada.

El documento conserva:

- `records`: inventario total;
- `candidates`: señales no promovidas;
- `events`: sólo `conversion_likely_event`;
- geometría candidata y geometría conjuntiva;
- razones a favor y en contra;
- fuentes alternativas evaluadas y no evaluadas.

Un contexto declarado de plantación forestal es contextual; no reemplaza
evidencia independiente.

## 4. El umbral de 0,5 ha

Hay dos conceptos distintos:

1. la definición EUDR de superficie que puede constituir bosque;
2. la referencia operativa VISEC para presentar una pérdida conectada.

La política automática usa `> 0.5 ha` sobre la intersección materializada:

```text
bosque 2020
∩ pérdida posterior
∩ cambio persistente
∩ occurrence agrícola post-evento
```

Una conjunción `<= 0.5 ha` se conserva como
`subthreshold_conversion_evidence`, permanece en candidatos y requiere revisión
cuando corresponda. No se interpreta como irrelevante jurídicamente.

En presentación, estos candidatos se agregan en una única tabla resumen. El
detalle por candidato se conserva en
`report_assets/annex/tables/subthreshold_candidates.csv`; no se descarta ni se
convierte en evidencia negativa.

## 4.1. Contexto anual Dynamic World

El collector puede publicar una serie anual 2020-fin del análisis sobre la unión
de los footprints candidatos RF-first. Cada GeoTIFF conserva la moda anual de la
banda `label` top-1 a 10 m, siguiendo el compuesto documentado por Dynamic World.
Es contexto descriptivo de cobertura/uso del suelo: no alimenta gates, no
expande candidatos y no confirma conversión.

Los códigos y colores oficiales son: agua `0/#419BDF`, árboles `1/#397D49`,
pastizal `2/#88B053`, vegetación inundada `3/#7A87C6`, cultivos `4/#E49635`,
arbustos/matorral `5/#DFC35A`, construido `6/#C4281B`, suelo desnudo `7/#A59B8F`
y nieve/hielo `8/#B39FE1`. Referencia: Brown et al. (2022),
doi:10.1038/s41597-022-01307-4.

## 5. Resolución de candidatos

| Resolución | Lectura |
| --- | --- |
| `candidate_only` | señal sin persistencia de perturbación suficiente |
| `temporary_or_recovered` | perturbación temporal o recuperación |
| `persistent_unattributed` | cambio persistente sin uso posterior demostrado |
| `insufficient_data` | soporte temporal o espacial insuficiente |
| `conversion_likely` | todos los gates automáticos satisfechos |

La mera existencia de candidatos resueltos como temporales no fuerza el estado
global a `review_required`.

## 6. Productos auditables

El orquestador completo publica:

```text
run_manifest.json
components/
  full_pipeline/
  hampel_benchmark/
  rf_annual_deltas/
  disturbance_candidate_fusion/  # alias estable del componente RF-first v2
  agricultural_collection/
  agricultural_persistence/
  post_change_attribution/
report_assets/
  index.json
  report_dataset.json
  figures/
  data/
  annex/
```

Cada bundle conserva manifest, hashes, configuración, cobertura, grilla,
artifacts y recibos de componentes no disponibles.

Versiones relevantes:

- complete analysis `2.7.0`;
- disturbance evidence `1.3.0`;
- dominio candidato RF-first y bundle `2.0.0`;
- forest temporal bundle `3.5.0`;
- Hampel derived bundle `3.2.0`;
- collector agrícola configuración `1.5.0`, bundle `1.7.0`, contrato `1.1.0`;
- evidencia agrícola y occurrence `2.1.0`;
- atribución `7.0.0`;
- report assets `2.5.0`, política de selección `2.4.0`;
- report dataset `2.1.0`;
- reporting `0.15.0`, contrato editorial `3.0.0` para occurrence v2 y plantilla
  narrativa `1.1.0`.

## 7. Presentación

`packages/reporting` transforma exclusivamente `report_assets` verificados y el
`input.geojson` declarado.

El PDF:

- es determinístico cuando usa el mismo cache de contexto;
- separa el informe principal del anexo técnico en dos PDF independientes;
- organiza el informe principal como propósito, método, síntesis, identificación,
  mapa, eventos prioritarios, evidencia temporal, interpretación y conclusión;
- abre con una carátula ejecutiva que separa resultado automático, estado de
  revisión humana, alcance y descargo legal;
- incluye índice multipaso, marcadores PDF, jerarquía navegable de tres niveles y
  versión explícita de plantilla;
- incorpora un marco conceptual común, parafraseado a partir del Reglamento (UE)
  2023/1115 y del Protocolo VISEC Carne suministrado como referencia, con
  atribución y aclaración expresa de que no constituye una evaluación VISEC;
- mantiene el contenido conceptual estable separado de las métricas dinámicas del
  expediente;
- confina cada ficha de candidato a una única página editorial;
- separa candidatos de eventos probables;
- deriva para cada candidato RF-first seleccionado una comparación NDVI
  pre/post de la misma estación, su delta espacial y un único plot zonal con
  NDVI, NBR, NDMI y NIRv sobre el footprint exacto y los TIFF estacionales HLS
  verificados, sin emparejarlo heurísticamente con eventos PDE históricos;
- muestra período, fuentes, resolución, limitaciones y revisión humana;
- incluye mapas con perímetro, grilla WGS 84, norte, escala y localizador;
- puede usar contexto OSM/ArcGIS sólo como orientación;
- presenta potencia agrícola como ordinal y fecha efectiva, no como confianza;
- presenta la serie anual Dynamic World con paleta y referencia oficiales;
- individualiza candidatos `> 0,5 ha` y resume los `<= 0,5 ha`, cuyo detalle
  permanece consultable en CSV;
- cuando attribution o agricultura no están disponibles muestra “no evaluado”,
  conserva valores desconocidos como `null` y no transforma la ausencia en
  gates fallidos o áreas cero;
- usa el dominio candidato RF-first como fallback para no perder candidatos RF
  en un informe parcial;
- no modifica ciencia ni introduce una conclusión legal.

`scripts/render_existing_report.py` permite regenerar ambos PDF desde un
expediente verificado sin ejecutar el pipeline. Exige una salida externa al run,
no sobrescribe artefactos existentes, deshabilita la red por defecto y registra
en `render-metadata.json` la versión de plantilla y los hashes de las fuentes y
de los PDF resultantes.

El worker renderiza fuera del request HTTP y publica ambos PDF, metadatos y ZIP
atómicamente.

## 8. Incertidumbre y explicaciones alternativas

Deben declararse explícitamente:

- observaciones insuficientes;
- desacuerdo entre fuentes;
- bordes y nodata;
- huecos temporales;
- recuperación;
- incendio, inundación, sequía o campo no evaluados;
- transferencia RF no validada;
- fuente agrícola única;
- score no calibrado.

La ausencia de una capa no puede convertirse en “explicación descartada”.

## 9. Validación disponible

- El gate actual registró 740 pruebas raíz con 90,40 % de cobertura, Ruff,
  formato y Mypy sobre 163 archivos, sin errores ni warnings.
- Fixtures sintéticos para nodata, bordes, áreas, geometrías, persistencia,
  recuperación, contratos y fallos.
- Integración HTTP -> SQLite -> worker -> PDF -> API/ZIP sin GEE.
- Dos runs completos históricos preservan el flujo anterior de seis
  componentes. El smoke remoto RF-first de Mojones Norte completó los siete
  componentes y produjo 139 candidatos, incluidos 125 en o bajo 0,5 ha.
- La extensión editorial recuperó las composiciones Dynamic World 2020-2025;
  esto valida transporte y presentación, no exactitud temática ni referencia de
  campo.
- La reatribución local de Mojones Norte con occurrence `2.1.0` y attribution
  `7.0.0` encontró 20 candidatos sobre el gate agrícola del 5 %: 11 `strong`,
  2 `moderate` y 7 `weak`. Cinco cumplieron todos los gates automáticos y
  sumaron 161,2770289 ha de intersección conjuntiva. Esta comparación reutilizó
  la colección mensual histórica pos-onset; no sustituye un rerun collector
  `1.5.0` desde `2021-01-01` ni una referencia independiente.
- El rerun integral posterior sí ejecutó collector `1.5.0` desde `2021-01-01`:
  22 de 139 candidatos superaron el gate (13 `strong`, 2 `moderate` y 7
  `weak`); 6 quedaron como `conversion_likely`, con 181,2347766 ha de
  intersección conjuntiva. Los siete componentes y 2.081 artefactos declarados
  verificaron estado, tamaño y SHA-256. Sigue siendo un caso operativo sin
  referencia de campo independiente.
- Un conjunto RF visual produjo métricas parciales altas, pero falló el gate de
  adjudicabilidad y bosque abierto; no valida transferencia temática general.

## 10. Deuda científica prioritaria

1. comparar el smoke Mojones Norte contra referencia independiente y revisar
   omisiones, comisiones, soporte robusto/CCDC e inventario sombra;
2. ampliar la validación de occurrence v2 y transporte agrícola con casos
   positivos y negativos controlados;
3. validar transferencia temática fuera del caso Mojones Norte;
4. calibrar por ecorregión, especialmente bosque abierto;
5. incorporar Sentinel-2 a 10 m;
6. evaluar Sentinel-1 como evidencia independiente;
7. integrar incendio, sequía e inundación;
8. medir error espacial, temporal, de superficie y calibración.

El orden ejecutable vive en [`NEXT_STEPS.md`](NEXT_STEPS.md).
