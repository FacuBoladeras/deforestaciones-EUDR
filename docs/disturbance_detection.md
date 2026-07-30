# Detección de perturbaciones — Paso 14 completo

## Propósito

El Paso 14.0 define la frontera científica de la detección de perturbaciones
posteriores al `2020-12-31`. Los pasos 14.1–14.5 implementan dominio,
diagnósticos, persistencia, CCDC y convergencia. El Paso 14.6 los integra al
runner y materializa un paquete compacto.

Una perturbación es una señal espectral o estructural que merece análisis. No
equivale por sí sola a deforestación, conversión agropecuaria o incumplimiento
EUDR.

## Frontera temporal

- Historia de referencia por defecto: desde `2017-01-01`.
- Inicio inmutable del análisis posterior al corte: `2021-01-01`.
- La historia debe comenzar el 1 de enero y ser anterior al corte.
- El período `2021-DJF` cruza la fecha de corte y su política es `exclude`.
- No se permite interpolar huecos temporales largos.

Cambiar el inicio de la historia es una decisión metodológica válida y modifica
el hash científico. Cambiar el inicio post-corte o incluir silenciosamente una
estación que cruza el corte es inválido.

## Dominio forestal

La detección exige al menos dos fuentes válidas en la línea base,
coincidiendo con `forest_baseline.minimum_independent_sources`.

Cuando las fuentes forestales discrepen, la política es
`evaluate_and_flag`: la zona sigue siendo evaluable, pero debe llevar la
bandera `BASELINE_UNCERTAIN`. No se descarta ni se fuerza una certeza
artificial.

El Paso 14.1 implementa esta regla como una operación local pura sobre matrices
sintéticas. Cada píxel recibe uno de cuatro códigos de dominio:

| Código | Dominio | Evaluable |
| ---: | --- | :---: |
| 0 | `NOT_EVALUABLE_INSUFFICIENT_SOURCES` | no |
| 1 | `NOT_EVALUABLE_NON_FOREST` | no |
| 2 | `EVALUABLE_CONSENSUS_FOREST` | sí |
| 3 | `EVALUABLE_BASELINE_UNCERTAIN` | sí |

La insuficiencia de fuentes también conserva `BASELINE_UNCERTAIN`, pero no
habilita el píxel. Un desacuerdo con evidencia forestal sí lo habilita y lo
marca como incierto. Así se distingue la falta de base, la evidencia unánime
de no bosque, el consenso forestal y el desacuerdo sin convertir la fracción
de evidencia en una probabilidad.

14.1 permanece como función pura; 14.6 lee los GeoTIFF de línea base, valida
grilla y la invoca sin alterar su regla.

## Comparación robusta estacional

El Paso 14.2 implementa una operación NumPy pura sobre series sintéticas. Cada
observación posterior al corte se compara **únicamente** con la distribución
histórica de la misma estación (`DJF`, `MAM`, `JJA` o `SON`). No se mezclan
estaciones porque hacerlo confundiría fenología normal con perturbación.

Las ventanas usan límites `[start_date, end_date_exclusive)`, el mismo contrato
del cubo temporal. La función exige fechas tanto para las referencias como para
los períodos poscorte: así puede verificar que ninguna observación posterior al
corte se filtre dentro de la distribución histórica. Los intervalos vacíos se
rechazan.

Para cada período, índice y píxel evaluable se conservan:

- centro histórico: mediana de las referencias válidas;
- escala robusta: `1.4826 × MAD`;
- cantidad de referencias finitas;
- delta direccional crudo: `mediana histórica - observación`, de modo que una
  caída de vegetación sea positiva;
- magnitud estandarizada, solamente cuando existen al menos tres referencias
  y la escala robusta es mayor que cero;
- bitmask de calidad heredado del dominio forestal.

Los `NaN` se conservan y nunca se interpolan. Si faltan referencias se activa
`INSUFFICIENT_REFERENCE_HISTORY`. Si la MAD es cero, el delta crudo continúa
siendo auditable pero la magnitud estandarizada queda en `NaN`: dividir por una
escala inexistente produciría certeza artificial. Los píxeles fuera de
`ForestEvaluationDomain.evaluable` no reciben diagnósticos.

Toda referencia debe finalizar, con extremo exclusivo, como máximo el
`2021-01-01`. Una ventana poscorte cuyo extremo exclusivo no supera esa fecha
es inválida. Una ventana que cruza el corte —en particular `2021-DJF`— se
conserva en la dimensión temporal, pero sus diagnósticos quedan en `NaN` y se
activa `CUTOFF_STRADDLING_PERIOD_EXCLUDED`.

Los parámetros viven en `disturbance_detection.robust_seasonal`, están
versionados y participan del hash científico. 14.2 no aplica un umbral de
anomalía ni una regla de persistencia. Por lo tanto, no asigna estados, no
produce probabilidades y no etiqueta deforestación. Esa separación permite que
14.3 decida persistencia sin ocultar los valores continuos originales.

14.2 tampoco consulta GEE ni atribuye uso. Desde 14.6 sus diagnósticos alimentan
los productos compactos, sin vectorización ni umbral de 0,5 ha.

## Detectores declarados

El contrato reserva dos roles:

1. `robust_seasonal_pre_post`: comparación robusta y explicable;
2. `ccdc_benchmark`: benchmark independiente de rupturas temporales.

El diagnóstico robusto está implementado desde 14.2 y su regla de señal desde
14.3. Desde 14.4 CCDC existe como benchmark independiente y auditable. El Paso
14.5 los converge de forma conservadora sin recalcular ningún detector ni
fusionar sus escalas.

El subconjunto espectral inicial es NDVI, NBR, NDMI y NIRv. Los demás índices
siguen disponibles como diagnósticos, pero no se incorporan automáticamente al
detector.

## Señal y persistencia robusta

El Paso 14.3 consume directamente `RobustSeasonalDiagnostics`; no recalcula la
mediana, la MAD ni la magnitud estandarizada. Un índice respalda una señal
cuando su caída estandarizada es mayor o igual a `3.0`. Este valor es un
**benchmark conservador**, no una probabilidad, y debe calibrarse en el futuro
con validación independiente.

Un período tiene señal solamente con soporte simultáneo de al menos dos entre
NDVI, NBR, NDMI y NIRv. Un píxel es candidato persistente cuando esa condición
se mantiene durante al menos dos períodos poscorte consecutivos. Para poder
clasificarlo se exigen además dos períodos válidos; de lo contrario queda
`NOT_EVALUABLE` y activa `INSUFFICIENT_POST_CUTOFF_OBSERVATIONS`.

Un período es válido cuando al menos dos índices tienen magnitud estandarizada
finita. Un `NaN` o período excluido corta la racha: no se interpola y tampoco
se interpreta como recuperación. Dos o más períodos inválidos consecutivos
activan `LONG_OBSERVATION_GAP`; un faltante aislado no lo activa. Las ventanas
deben llegar ordenadas, sin superposición y de forma continua. Una ventana
faltante se representa explícitamente mediante `NaN`; omitirla del eje temporal
podría convertir señales separadas por un hueco en una racha falsa y por eso se
rechaza.

La salida local inmutable conserva por píxel:

- índice cero-basado del primer período con señal, o `-1` si no existe;
- máxima magnitud estandarizada positiva;
- longitud máxima de la racha;
- máximo soporte multíndice;
- cantidad de períodos poscorte válidos;
- score de evidencia no calibrado;
- bitmask heredado y ampliado.

En 14.3 el score replica explícitamente la máxima magnitud estandarizada
positiva. No se introduce una combinación arbitraria ni se presenta como
probabilidad. `TRANSIENT_SIGNAL` solo significa que hubo señal sin una racha
suficiente: no prueba recuperación. `DETECTOR_DISAGREEMENT` no puede emitirse
hasta que exista una regla posterior de convergencia.

## Benchmark CCDC independiente

El Paso 14.4 usa
[`ee.Algorithms.TemporalSegmentation.Ccdc`](https://developers.google.com/earth-engine/apidocs/ee-algorithms-temporalsegmentation-ccdc)
como benchmark de rupturas. La referencia oficial fue consultada el
`2026-07-29`; la página indicaba actualización `2026-04-20` y el entorno local
verificado usa Earth Engine Python API `1.7.36`.

CCDC consume la colección **densa** de escenas HLS enmascaradas, no los cuatro
composites estacionales anuales. El perfil primario es exclusivamente
`HLSL30`: combinar HLSL30 y HLSS30 queda diferido hasta validar que la mezcla
no introduzca diferencias sensorales en el ajuste.

El builder reutiliza exactamente el preprocesamiento HLS existente: escala la
reflectancia, aplica Fmask y la política de aerosol, agrega NDVI, NBR, NDMI y
NIRv por escena, y preserva `system:time_start`. No calcula mediana ni colapsa
el eje temporal. El intervalo remoto es
`[reference_history_start_date, analysis_end_date + 1 día)`.

Los argumentos son explícitos y versionados:

| Argumento GEE | Valor benchmark |
| --- | ---: |
| `breakpointBands` | NDVI, NBR, NDMI, NIRv |
| `minObservations` | 6 |
| `chiSquareProbability` | 0.99 |
| `minNumOfYearsScaler` | 1.33 |
| `dateFormat` | 1, año fraccional |
| `lambda` | 20 |
| `maxIterations` | 25000 |
| `tmaskBands` | deshabilitado |

TMask queda deshabilitado porque HLS Fmask ya se aplica antes de CCDC. Esto es
una política del benchmark, no una afirmación de superioridad; cualquier cambio
debe validarse y versionarse. La documentación oficial exige que las bandas
TMask pertenezcan también a `breakpointBands`, condición que no corresponde al
perfil actual basado en índices.

La salida remota conserva la imagen de arrays CCDC cruda. El adaptador NumPy
probado con segmentos sintéticos selecciona la primera ruptura cuyo `tBreak`
sea igual o posterior a `2021.0`, ignora rupturas precorte y conserva:

- elegibilidad por dominio y observaciones;
- estado de ajuste y `CCDC_FIT_FAILURE`;
- `CCDC_INSUFFICIENT_OBSERVATIONS` como causa diferente;
- primera ruptura poscorte e índice del segmento;
- `numObs` del segmento;
- magnitud firmada por índice;
- magnitud direccional `-magnitud`, para que una caída sea positiva;
- `changeProb` bajo el nombre `ccdc_breakpoint_pseudo_probability`.

Los conteos `numObs` y de observaciones válidas se normalizan a `uint16`;
valores fuera de ese rango se rechazan en lugar de desbordarse silenciosamente.

El catálogo oficial del producto CCDC global describe
[`changeProb` como una pseudoprobabilidad de que la ruptura sea
real](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_GLOBAL_CCDC_V1).
Por eso no se renombra ni interpreta como probabilidad calibrada de
deforestación, conversión o cumplimiento. Tampoco se aplica en 14.4 un umbral
de aceptación sobre `changeProb`.

Las pruebas usan dobles de GEE y arrays sintéticos. No ejecutan una consulta
remota, no descargan arrays y no fingen que el output crudo fue materializado.

## Convergencia conservadora

El Paso 14.5 consume directamente `RobustSeasonalSignalResult` y
`CcdcBenchmarkResult`. La disponibilidad robusta requiere un estado distinto
de `NOT_EVALUABLE`; la disponibilidad CCDC requiere un ajuste exitoso.
`CCDC_FIT_FAILURE` e insuficiencia de observaciones significan indisponibilidad,
no desacuerdo.

La compatibilidad temporal usa los límites reales de los períodos robustos. Un
`tBreak` es compatible solamente si cae en `[inicio, fin)` de un período con
señal robusta. Para un candidato persistente se consideran únicamente los
períodos que pertenecen a una racha que satisface el mínimo de persistencia.
El margen es cero: no se inventa una fecha diaria ni se ensancha la ventana
para obtener acuerdo.

| Robusto | CCDC | Compatibilidad | Estado convergente | Disponibles | Concordantes | Motivo |
| --- | --- | --- | --- | ---: | ---: | --- |
| no disponible | no disponible | — | `NOT_EVALUABLE` | 0 | 0 | `NO_DETECTOR_AVAILABLE` |
| estable | no disponible | — | `STABLE_OBSERVED` | 1 | 1 | `ROBUST_ONLY_STABLE` |
| transitorio | no disponible | — | `TRANSIENT_SIGNAL` | 1 | 1 | `ROBUST_ONLY_TRANSIENT` |
| persistente | no disponible | — | `PERSISTENT_CANDIDATE` | 1 | 1 | `ROBUST_ONLY_PERSISTENT` |
| no disponible | sin ruptura | — | `STABLE_OBSERVED` | 1 | 1 | `CCDC_ONLY_STABLE` |
| no disponible | ruptura | — | `TRANSIENT_SIGNAL` | 1 | 1 | `CCDC_ONLY_BREAK_REQUIRES_REVIEW` |
| estable | sin ruptura | — | `STABLE_OBSERVED` | 2 | 2 | `BOTH_STABLE` |
| transitorio | sin ruptura | — | `TRANSIENT_SIGNAL` | 2 | 1 | `ROBUST_TRANSIENT_CCDC_NO_BREAK` |
| estable | ruptura | — | `DETECTOR_DISAGREEMENT` | 2 | 1 | `DETECTOR_POLARITY_DISAGREEMENT` |
| persistente | sin ruptura | — | `DETECTOR_DISAGREEMENT` | 2 | 1 | `DETECTOR_POLARITY_DISAGREEMENT` |
| transitorio | ruptura | compatible | `TRANSIENT_SIGNAL` | 2 | 2 | `COMPATIBLE_TRANSIENT_SIGNALS` |
| persistente | ruptura | compatible | `PERSISTENT_CANDIDATE` | 2 | 2 | `COMPATIBLE_PERSISTENT_SIGNALS` |
| transitorio o persistente | ruptura | incompatible | `DETECTOR_DISAGREEMENT` | 2 | 1 | `POSITIVE_SIGNALS_TEMPORALLY_INCOMPATIBLE` |

Una ruptura CCDC aislada queda como señal transitoria que requiere revisión:
CCDC no demuestra por sí solo persistencia robusta. Un resultado robusto
persistente puede conservarse cuando CCDC no está disponible, pero el conteo
de detectores disponibles y concordantes queda en uno; no se presenta como
convergencia de dos fuentes.

La salida inmutable conserva separadamente:

- score y magnitud máxima robusta no calibrados;
- `changeProb` CCDC con su nombre de pseudoprobabilidad;
- magnitud CCDC por banda;
- primer índice de período robusto y `tBreak` fraccional originales;
- soporte de cada detector;
- cantidad de detectores disponibles y concordantes;
- estado, código de motivo y OR de los bitmasks.

No se promedian magnitudes, z-scores ni `changeProb`. Tampoco se crea una
probabilidad conjunta.

## Estados de detección

| Código | Estado | Semántica |
| ---: | --- | --- |
| 0 | `NOT_EVALUABLE` | observaciones o línea base insuficientes |
| 1 | `STABLE_OBSERVED` | cobertura observada sin señal persistente |
| 2 | `TRANSIENT_SIGNAL` | señal aislada o sin persistencia demostrada |
| 3 | `PERSISTENT_CANDIDATE` | candidato de perturbación persistente |
| 4 | `DETECTOR_DISAGREEMENT` | detectores con evidencia incompatible |

Estos códigos no son estados de conversión ni de cumplimiento.

## Integración y materialización 14.6

Un rango de `--full-pipeline` ejecuta la serie estacional, línea base y los dos
detectores. El rango publicado sigue siendo exactamente el pedido. Si faltan
años desde `reference_history_start_date`, se materializan en memoria como
soporte y se descartan antes de publicar. Por ejemplo, 2019–2024 usa 2017–2018
internamente y conserva 2019–2024 en el cubo visible.

Los rasters estacionales no se recalculan: se leen NDVI, NBR, NDMI y NIRv desde
los GeoTIFF existentes, junto con los conteos QA. Antes de apilarlos se validan
CRS, transformación, dimensiones, nodata y nombres de banda. Los `NaN` se
preservan y nunca se interpolan.

CCDC se ejecuta sobre HLSL30 denso. Un adaptador server-side selecciona la
primera ruptura poscorte, su score algorítmico, `numObs` y las magnitudes por
índice. Sólo ese resumen escalar se descarga sobre la grilla declarada; los
arrays crudos no salen de Earth Engine. Una falla global CCDC se conserva como
detector no disponible con bandera de calidad, nunca como desacuerdo. En ese
caso la procedencia declara `scalar_summary_downloaded: false`; no se registra
una descarga que no ocurrió.

Los arrays CCDC sin segmentos o sin rupturas poscorte se rellenan únicamente
hasta longitud uno para evitar extracciones fuera de rango. Los centinelas no
se interpretan como evidencia: `ccdc_fit_succeeded` y
`ccdc_has_post_cutoff_break` gobiernan su validez y el adaptador local conserva
nodata cuando no existe una ruptura.

La misma degradación estrecha se aplica si falla la construcción de la
colección HLSL30 densa exclusiva de CCDC. El código sanitizado conserva el
intervalo y la etapa (`dense_<inicio>_<fin>_<etapa>_<causa>`), y se publica en
`ccdc.failure_code`. Esto no convierte una falla de la serie HLS estacional
principal en opcional: si esa serie falla, la corrida completa se aborta antes
de la publicación atómica.

Se publican exactamente:

```text
json/evidence/disturbance_detection.json
tiffs/evidence/disturbance_summary.tif
tiffs/evidence/disturbance_diagnostics.tif
figures/evidence/disturbance_detection.png
tables/evidence/disturbance_period_summary.csv
```

Ambos TIFF usan `float32` común y nodata explícito. Los códigos enteros y el
bitmask se preservan exactamente dentro de ese dtype. `recovery_indicator`
permanece nodata y etiquetado como reservado. El CSV mapea índices de período a
intervalos estacionales `[inicio, fin)`; no inventa fechas diarias. El fin del
análisis es el último día del último composite cerrado materializado.

La máscara espacial se hereda de la huella válida de la línea base. Todos los
canales de ambos TIFF permanecen nodata fuera del AOI, los conteos del JSON no
incluyen esos píxeles y cada raster conserva `grid_sha256` en sus tags. La
estación DJF que cruza el corte se mantiene en el CSV para auditoría, pero se
declara explícitamente excluida y no integra `evaluable_range`.

14.6 sólo orquesta y serializa contratos 1.4.0 ya publicados, por lo que no
cambia las versiones científicas ni el esquema del pipeline.

## Bitmask de calidad reservado

Las posiciones son estables:

| Bit | Bandera |
| ---: | --- |
| 0 | `BASELINE_UNCERTAIN` |
| 1 | `INSUFFICIENT_REFERENCE_HISTORY` |
| 2 | `INSUFFICIENT_POST_CUTOFF_OBSERVATIONS` |
| 3 | `PERSISTENT_CLOUD_OR_SHADOW` |
| 4 | `CUTOFF_STRADDLING_PERIOD_EXCLUDED` |
| 5 | `SENSOR_IMBALANCE` |
| 6 | `LONG_OBSERVATION_GAP` |
| 7 | `CCDC_FIT_FAILURE` |
| 8 | `EDGE_PIXEL` |
| 9 | `CCDC_INSUFFICIENT_OBSERVATIONS` |

## Semántica del score

El nombre contractual es
`uncalibrated_disturbance_evidence_score`. No es una probabilidad.

Hasta contar con validación independiente y calibración, el pipeline no puede
publicarlo como riesgo probabilístico ni usarlo para una evaluación final.

## Salidas compactas reservadas

```text
json/evidence/disturbance_detection.json
tiffs/evidence/disturbance_summary.tif
tiffs/evidence/disturbance_diagnostics.tif
figures/evidence/disturbance_detection.png
tables/evidence/disturbance_period_summary.csv
```

Los dos GeoTIFF serán multibanda. Esto evita crear archivos por índice,
detector o período. `first_anomalous_period_index` referenciará la tabla de
períodos en lugar de fingir una fecha diaria para una ventana compuesta.

Desde 14.6 estas rutas se materializan mediante `pruebas.py` cuando
`--full-pipeline` recibe un rango estacional.

## Límites inmutables

El contrato obliga a que:

- se preserve la evidencia de cada detector;
- se preserve el desacuerdo;
- no se aplique todavía el umbral espacial de 0,5 ha;
- no se atribuya uso posterior;
- no se genere una evaluación automática final.

La atribución, vectorización y agregación espacial pertenecen a pasos
posteriores.
