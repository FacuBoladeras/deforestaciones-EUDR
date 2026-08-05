# Pipeline de evidencia geoespacial EUDR

Pipeline reproducible para generar evidencia técnica sobre cobertura forestal y
señales de perturbación en establecimientos ganaderos de Argentina.

El sistema **no certifica** que un establecimiento sea libre de deforestación,
no determina por sí solo cumplimiento del Reglamento (UE) 2023/1115 y no
reemplaza la debida diligencia ni la revisión humana. La formulación correcta
del resultado actual es _evidencia de perturbación detectada o no detectada_;
la atribución de uso posterior todavía no está implementada.

Este archivo es la referencia canónica del estado actual. El orden de trabajo
pendiente vive únicamente en [`NEXT_STEPS.md`](NEXT_STEPS.md). Las reglas de
dominio y de desarrollo que deben respetar humanos y agentes viven en
[`AGENTS.md`](AGENTS.md).

## Checkpoint actual

**Fecha del checkpoint:** 4 de agosto de 2026.

La etapa de detección de perturbaciones está implementada de extremo a extremo
para rangos estacionales HLS. El pipeline puede:

1. ingerir un vector local y conservar su procedencia;
2. validar, normalizar y, cuando corresponde, reparar su geometría;
3. medir superficie y derivar una grilla raster común;
4. construir composiciones HLS estacionales y un cubo temporal virtual;
5. producir QA de observaciones y variables espectrales;
6. construir una línea base forestal para el 31/12/2020;
7. comparar cada estación poscorte con su historia homóloga;
8. ejecutar CCDC como detector independiente;
9. conservar coincidencias, desacuerdos e indisponibilidad entre detectores;
10. validar la evidencia de perturbaciones contra un contrato Pydantic
    versionado antes de publicarla;
11. publicar un paquete local atómico, trazable y verificable por hashes;
12. construir y exportar un dataset P0 provincial de bosque/no bosque proxy
    para Entre Ríos en 2020, con features, label y metadata separados;
13. separar el AOI en dominio automatizado, revisión requerida y datos
    insuficientes, y conservar señales temporales en los tres dominios;
14. segmentar los candidatos persistentes dentro de `automated_forest` con
    ocho vecinos, asignar IDs determinísticos y medir cada componente;
15. publicar todos los eventos en CSV, JSON y GeoJSON, más un mapa general y
    hasta cinco fichas de los eventos de mayor superficie.

La frontera actual es deliberada:

- **detección de perturbaciones:** implementada;
- **atribución del cambio a cultivo, pastura u otro uso:** pendiente;
- **eventos persistentes de detección:** vectorizados y medidos; el umbral de
  0,5 ha se informa sin descartar eventos menores;
- **agregación final luego de atribuir uso posterior:** pendiente;
- **evaluación automática final:** no generada;
- **confirmación de conversión o certificación legal:** fuera de la capacidad
  automática actual.

## Arquitectura ejecutable

La puerta de entrada local es [`pruebas.py`](pruebas.py), que delega en
`deforestation_pipeline.local_runner`.

```text
vector local
  │
  ▼
pruebas.py / local_runner
  ├─ vector_ingestion ──► geometría original + procedencia
  ├─ geometry ──────────► geometría normalizada/reparada
  ├─ area + raster_grid ► hectáreas + grilla métrica fija
  ├─ config + catalog ──► parámetros y fuentes versionadas
  ├─ gee + hls_composite
  │    └─► reflectancia enmascarada, índices y conteos
  ├─ temporal_cube + hls_seasonal + seasonal_qa
  │    └─► composiciones DJF/MAM/JJA/SON + cubo virtual
  ├─ forest_baseline_*
  │    └─► evidencia forestal 2020 y desacuerdo entre fuentes
  ├─ change_detection + ccdc_benchmark
  │    └─► detector robusto + resumen escalar CCDC
  ├─ disturbance_materialization
  │    └─► estados, diagnósticos, tabla y figura
  ├─ disturbance_events
  │    └─► componentes persistentes, GeoJSON, tabla y fichas acotadas
  └─ publicación atómica + manifiesto SHA-256
```

El núcleo analítico está en
[`src/deforestation_pipeline/`](src/deforestation_pipeline/). La CLI no está
acoplada a un servidor ni a una interfaz web. Cada corrida es un bundle local
independiente.

## Flujo científico vigente

### 1. Ingesta y preparación espacial

- Se acepta un archivo vectorial que pueda leer el GDAL instalado, por ejemplo
  GeoJSON, GeoPackage, Shapefile, FlatGeoBuf, GML o KML.
- Se procesa un territorio por corrida. Una fuente multicapa requiere
  `--layer`; varias entidades requieren la decisión explícita
  `--dissolve-all`.
- El CRS embebido prevalece. `--source-crs` sólo completa una fuente que no
  declara CRS y nunca corrige silenciosamente una contradicción.
- Se conservan por separado la geometría interpretada, la normalizada a
  EPSG:4326 y la geometría de análisis reparada.
- La estrategia de área predeterminada usa un CRS equivalente. No se calculan
  hectáreas en EPSG:4326 ni mediante conteo fijo de píxeles.
- El repositorio no incorpora todavía un límite oficial de Argentina; si no se
  suministra una frontera autorizada, la jurisdicción queda explícitamente sin
  evaluar.

### 2. Serie estacional y cubo temporal

El flujo completo por rango trabaja con HLSL30 y HLSS30 a 30 m. Aplica Fmask,
conserva agua según configuración, compone la mediana de reflectancia y deriva:

- NDVI;
- EVI2;
- NBR;
- NDMI;
- NMDI;
- LSWI;
- NIRv;
- kNDVI.

Las ventanas meteorológicas del hemisferio sur son DJF, MAM, JJA y SON, con
intervalos `[inicio, fin)`. Todos los períodos usan la misma grilla, conservan
conteos total/HLSL30/HLSS30 y permanecen explícitos cuando no tienen
observaciones. No se interpolan huecos.

El cubo es una colección virtual de GeoTIFF referenciada por un índice JSON.
No duplica los píxeles en otro contenedor y verifica hashes, dimensiones,
transformación, CRS, nodata y máscara del AOI.

### 3. Línea base forestal 2020

La fecha de referencia es inmutable: `2020-12-31`. La línea base combina:

| Fuente | Rol actual |
| --- | --- |
| JRC Global Forest Cover 2020 V3 | evidencia núcleo |
| ESA WorldCover 2020 v100 | evidencia núcleo |
| Hansen Global Forest Change v1.13 | evidencia derivada de apoyo |

JRC y ESA forman el consenso núcleo; Hansen permanece separado porque su
reconstrucción desde cobertura 2000 y pérdidas no representa completamente la
regeneración. El resultado es una
`uncalibrated_core_evidence_fraction`, **no una probabilidad de bosque ni verdad
de terreno**. La evidencia por fuente y el desacuerdo siempre se conservan.

La línea base también integra el RF P0 ya entrenado para Entre Ríos como
**evidencia aprendida de apoyo**. El asset GEE se reconstruye desde la TABLE
geemap declarada en configuración y recibe exactamente 56 predictores:
`DJF/MAM/JJA/SON × (6 reflectancias + 8 índices)`. Los 12 conteos de
observaciones permanecen como QA y nunca ingresan al clasificador. Se publican
la clase `0=non_forest / 1=forest`, la completitud de inputs y la
`uncalibrated_binary_tree_vote_fraction`; esta última es una fracción de votos
RAW, **no una probabilidad calibrada**. Como el modelo aprendió pseudolabels
construidos a partir de MapBiomas/JRC/ESA/Hansen, no cuenta como evidencia
independiente adicional ni está autorizado para transferencia a otros años o
territorios.

El alcance automático del MVP es un **screening con escalamiento**, no una
clasificación exhaustiva del bosque abierto. La regla `forest_screening` 1.1.0
define tres máscaras mutuamente excluyentes dentro del AOI:

- `automated_evaluable`: RF completo, al menos dos fuentes core y evidencia
  inequívoca; bosque requiere consenso forestal y votos `>= 0,80`, mientras no
  bosque requiere cero votos forestales core y votos RF `<= 0,20`;
- `review_required`: soporte suficiente, pero desacuerdo, margen intermedio o
  cobertura potencial no resuelta;
- `insufficient_data`: falta de inputs RF o del soporte core mínimo.

Las superficies se calculan con el determinante afín de la grilla proyectada.
La comparación temporal se calcula sobre todo el AOI para no borrar señales,
pero su interpretación automática primaria se limita a `automated_forest`.
Fuera de ese estrato las señales alimentan revisión, no una conclusión.
Antes de aplicar los umbrales, la fracción de votos float32 se cuantiza a la
grilla real de los 500 árboles (`1/500`); el raster RAW se conserva sin cambios.

### 4. Detección de perturbaciones

La detección comienza después de la fecha de corte:

- historia configurada desde `2017-01-01`;
- análisis poscorte desde `2021-01-01`;
- `2021-DJF` se conserva para auditoría, pero se excluye porque cruza el corte;
- cada período se compara sólo con referencias de la misma estación;
- centro histórico: mediana;
- escala robusta: `1.4826 × MAD`;
- umbral benchmark: caída estandarizada `>= 3.0`;
- señal multíndice: al menos dos índices;
- persistencia: al menos dos períodos consecutivos;
- períodos faltantes conservan `NaN` y cortan la racha.

Los índices detectores iniciales son NDVI, NBR, NDMI y NIRv. El score robusto
es evidencia no calibrada, no una probabilidad.

La mediana temporal y el MAD se calculan únicamente en posiciones que contienen
al menos una observación finita. Los slices completamente vacíos permanecen
como `NaN` sin emitir warnings ni introducir relleno.

CCDC procesa observaciones densas HLSL30 enmascaradas y funciona como benchmark
independiente. Los arrays crudos permanecen en Earth Engine; sólo se descarga
un resumen escalar sobre la grilla declarada. `changeProb` conserva la
semántica de pseudoprobabilidad algorítmica de la ruptura y nunca se interpreta
como probabilidad de deforestación.

La procedencia operativa de CCDC es obligatoria en la materialización: cada
evidencia declara si el resumen escalar fue descargado o si el detector quedó
globalmente indisponible mediante un código de falla sanitizado. No existe un
metadata preliminar que afirme disponibilidad antes de conocer el resultado.

La convergencia no promedia escalas incompatibles. Conserva por separado la
evidencia robusta y CCDC, diferencia una falla de detector de un desacuerdo y
puede producir estos estados de **detección**:

- `NOT_EVALUABLE`;
- `STABLE_OBSERVED`;
- `TRANSIENT_SIGNAL`;
- `PERSISTENT_CANDIDATE`;
- `DETECTOR_DISAGREEMENT`.

Ninguno es una clase de uso del suelo ni una conclusión EUDR.

Los píxeles `PERSISTENT_CANDIDATE` se segmentan sólo después de intersectarlos
con `automated_forest`. La conectividad es de ocho vecinos y cada ID deriva de
la grilla más el conjunto ordenado de píxeles, por lo que no depende del orden
de recorrido en memoria. El área se calcula con el determinante afín de la
grilla proyectada. El umbral de `0,5 ha` agrega una bandera: **no elimina**
componentes menores. La ventana de inicio informada es la primera composición
estacional robusta del evento y no una fecha exacta.

### 5. Dataset P0 de entrenamiento para Entre Ríos

El primer dataset provincial reproducible quedó materializado para **2020**.
Es un insumo de desarrollo para entrenar y evaluar un clasificador forestal;
no es una determinación de bosque EUDR ni verdad de terreno.

La provincia se divide en dos particiones:

- UTM 20S, `EPSG:32720`;
- UTM 21S, `EPSG:32721`.

La división ocurre en el meridiano `-60°` y excluye un buffer de **3 km** a
cada lado de la costura para reducir leakage espacial entre particiones. El AOI
operativo proviene de `FAO/GAUL/2015/level1`, filtrado por
`ADM0_NAME=Argentina` y `ADM1_NAME=Entre Rios`. GAUL permite reproducir el P0,
pero **no es un límite provincial argentino oficial vigente**.

Cada fila contiene:

- **68 features inferibles**: cuatro estaciones
  `DJF/MAM/JJA/SON × (6 bandas de reflectancia + 8 índices + 3 conteos de
  observaciones)`;
- `proxy_label`, declarado separadamente;
- **15 columnas de metadata**, excluidas del modelo.

El RF integrado usa una allowlist exacta de 56 variables espectrales. Los 12
conteos de observaciones se preservan como QA, pero no son predictores.
Identidad de muestra, bloque, split, coordenadas, zona UTM, votos crudos,
conteos de fuentes y código interno de label son metadata; no pueden entrar
como predictores.

La etiqueta proxy conserva tres estados:

- `forest`;
- `non_forest`;
- `ambiguous`.

MapBiomas Argentina Collection 2 participa como una fuente de pseudolabel junto
con JRC GFC2020, ESA WorldCover y Hansen. MapBiomas no es verdad de terreno y
la etiqueta resultante **no reproduce por sí sola la definición EUDR de
bosque**.

El P0 contiene **2.000 filas**:

| Partición | `forest` | `non_forest` | `ambiguous` | Total |
| --- | ---: | ---: | ---: | ---: |
| UTM 20S | 400 | 400 | 200 | 1.000 |
| UTM 21S | 400 | 400 | 200 | 1.000 |
| Provincial | 800 | 800 | 400 | 2.000 |

Los CSV validados están en:

- `outputs/training_p0/raw/entre_rios_training_2020_p0_utm20.csv`;
- `outputs/training_p0/raw/entre_rios_training_2020_p0_utm21.csv`.

Cada archivo tiene 1.000 filas, 84 columnas y hashes verificados contra Drive.
La evidencia de la ejecución vive en:

- dry-run:
  `outputs/training_p0/training-p0-20260730T220842Z.json`;
- export:
  `outputs/training_p0/training-p0-20260730T222822Z.json`;
- validación local:
  `outputs/training_p0/training-p0-20260730T222822Z-validation.json`.

## Modos de ejecución

### Preparar el entorno

```powershell
uv sync --dev
```

Las credenciales GEE son locales. `credentials.json` está ignorado por Git y
no debe copiarse a documentación, logs, commits ni bundles.

### Inspección local sin datos remotos

```powershell
uv run python pruebas.py data/samples/local_test_polygon.geojson `
  --establishment-id caso-sintetico `
  --analysis-end-date 2026-07-30
```

Para listar los formatos vectoriales realmente disponibles:

```powershell
uv run python pruebas.py --list-vector-formats
```

### Pipeline completo por rango

Este es el comando operativo principal:

```powershell
uv run python pruebas.py data/samples/local_test_polygon.geojson `
  --full-pipeline `
  --hls-start-year 2019 `
  --hls-end-year 2024 `
  --establishment-id caso-sintetico
```

El rango es inclusivo y sólo admite años calendario cerrados. El runner obtiene
soporte histórico anterior cuando lo necesita, pero publica únicamente el
rango solicitado. La opción `--seasonal` permanece sólo por compatibilidad: un
rango de `--full-pipeline` ya es estacional.

### Composite anual individual

El modo anual aislado sigue disponible para inspección y comparación:

```powershell
uv run python pruebas.py data/samples/local_test_polygon.geojson `
  --full-pipeline `
  --hls-year 2023 `
  --establishment-id caso-anual
```

Este modo no ejecuta la detección temporal por rango.

También se pueden consultar únicamente metadatos:

```powershell
uv run python pruebas.py data/samples/local_test_polygon.geojson `
  --query-gee `
  --gee-start-date 2023-01-01 `
  --gee-end-date 2024-01-01 `
  --max-scenes-per-source 25
```

Agregar `--build-hls-composite` a esa consulta construye y descarga el
composite anual para un ROI pequeño.

### Dataset P0 provincial

El dry-run autentica la cuenta de servicio, construye las dos particiones,
verifica los conteos y escribe el manifiesto, pero no inicia una tarea remota:

```powershell
uv run python scripts/export_training_p0.py `
  --config configs/training-p0-entrerios.yml `
  --auth-mode service_account `
  --credentials credentials.json
```

Las cuentas de servicio pueden ejecutar Earth Engine, pero no tienen cuota de
almacenamiento propia en Google Drive. Por eso `--start-export` debe usar la
credencial OAuth persistente del usuario:

```powershell
uv run python scripts/export_training_p0.py `
  --config configs/training-p0-entrerios.yml `
  --auth-mode user_oauth `
  --start-export
```

La exportación verificada escribió en la carpeta Drive
`deforestation-pipeline-training-p0` los archivos:

- `entre_rios_training_2020_p0_utm20.csv`;
- `entre_rios_training_2020_p0_utm21.csv`.

Las tareas `HWKA35PYKROORPW2RJQC27ED` y
`PJ4V5D6TUTZA4SCIQJOH3SWW` terminaron en `COMPLETED`.

Earth Engine creó dos carpetas homónimas durante el arranque concurrente de las
particiones. Al recuperar los resultados no se debe elegir una carpeta por
posición: hay que buscar los **nombres exactos** de ambos CSV entre todas las
carpetas `deforestation-pipeline-training-p0`.

## Paquete de salida

Cada ejecución crea una carpeta nueva bajo `outputs/local_tests/`. La
organización estable es **tipo físico → dominio/cadencia → período → rol**:

```text
<corrida>/
├── json/
│   ├── run/                  # resumen, entorno y manifiesto
│   ├── input/                # ingesta y vector convertido
│   ├── geometry/             # geometrías, validación y área
│   ├── configuration/        # configuración resuelta y plan de fuentes
│   ├── gee/                  # inventario remoto, cuando se solicita
│   ├── temporal/seasonal/    # ventanas, metadatos, QA e índice del cubo
│   └── evidence/             # línea base y detección
├── figures/
│   ├── annual/
│   ├── seasonal/
│   └── evidence/
├── tiffs/
│   ├── annual/
│   ├── seasonal/
│   └── evidence/
├── tables/
│   ├── seasonal/
│   └── evidence/
└── source/                   # copia de la fuente y sidecars
```

El manifiesto registra tamaño y SHA-256 de cada artefacto, junto con las
identidades científica y operativa de la corrida. La publicación se realiza
primero en un directorio temporal y luego se renombra, para no dejar un bundle
parcial como si fuera válido.

La línea base concentra quince activos:

```text
json/evidence/forest_baseline_2020.json
figures/evidence/forest_baseline_2020.png
tiffs/evidence/forest_evidence_fraction_2020.tif
tiffs/evidence/forest_consensus_2020.tif
tiffs/evidence/forest_disagreement_2020.tif
tiffs/evidence/forest_source_count_2020.tif
tiffs/evidence/forest_source_evidence_2020.tif
tiffs/evidence/forest_features_2020.tif
tiffs/evidence/rf_forest_class_2020.tif
tiffs/evidence/rf_forest_vote_fraction_2020.tif
tiffs/evidence/rf_forest_input_complete_2020.tif
tiffs/evidence/automated_evaluable_2020.tif
tiffs/evidence/review_required_2020.tif
tiffs/evidence/insufficient_data_2020.tif
figures/evidence/forest_screening_2020.png
```

La detección agrega cinco activos base, sin abrir carpetas por detector o
índice:

```text
json/evidence/disturbance_detection.json
tiffs/evidence/disturbance_summary.tif
tiffs/evidence/disturbance_diagnostics.tif
figures/evidence/disturbance_detection.png
tables/evidence/disturbance_period_summary.csv
```

La segmentación persistente agrega cuatro activos para la colección completa y
como máximo cinco fichas seleccionadas por superficie descendente e ID:

```text
tables/evidence/disturbance_events.csv
json/evidence/disturbance_events.json
json/evidence/disturbance_events.geojson
figures/evidence/disturbance_events.png
figures/evidence/disturbance_event_<event-id>.png
```

El CSV, JSON y GeoJSON conservan **todos** los eventos, incluidos los menores a
`0,5 ha`; el límite sólo controla cuántos PNG individuales se renderizan. Las
fichas comparan NDVI de la última referencia pre-corte con la primera ventana
del evento de la misma estación y muestran la serie media multíndice. Esta
comparación es evidencia visual de perturbación, no atribución de causa.

La conectividad de ocho vecinos define la pertenencia al evento, pero la
polygonización usa cuatro vecinos y une después sus polígonos. Así, dos píxeles
que sólo se tocan por una esquina permanecen en un mismo evento lógico sin
crear anillos autointersectados: su geometría puede ser `MultiPolygon`. Cada
geometría se valida en el CRS proyectado y nuevamente luego de transformarla a
WGS 84.

El benchmark Hampel protegido se ejecuta como una **derivación local e
inmutable** de un bundle estacional ya materializado. Como el cubo publicado
sólo conserva composites, el filtro se evalúa sobre la mediana espacial de
cada evento/control y separa las series por estación meteorológica. Por eso se
declara `seasonal_hampel_benchmark`: no reproduce todavía el orden exacto de
VISEC —Hampel por escena antes de agregar— y no modifica detectores, eventos ni
estados. Conserva cada valor crudo, valor filtrado, candidato, reemplazo,
mediana, MAD, umbral, soporte y código de motivo.

```text
configs/hampel-benchmark.yml
json/configuration/hampel_benchmark.json
json/evidence/seasonal_hampel_benchmark.json
tables/evidence/hampel_benchmark_observations.csv
tables/evidence/hampel_benchmark_metrics.csv
figures/evidence/seasonal_hampel_benchmark.png
```

Los controles son diez vecindades 3 × 3 totalmente incluidas en
`automated_forest` y con estado `STABLE_OBSERVED`, seleccionadas por ranking
SHA-256 y separación espacial determinística. El reemplazo sólo se permite
para un candidato aislado con retorno bilateral a la mediana local; bordes,
rachas de candidatos, faltantes y escalones persistentes quedan sin modificar.
`activated` y `activation_allowed` permanecen siempre en `false`.
La procedencia separa los artefactos fuente validados, los reutilizados byte a
byte y los transformados. En particular, `json/run/summary.json` se declara
como transformado; no se afirma que todos los artefactos fuente sean copiados
sin cambios. El manifiesto final vuelve a calcular los hashes de todos los
activos publicados.

El exterior del AOI queda como nodata y no participa de estadísticas. El
resumen conserva siempre `attribution_generated: false` y
`final_assessment_generated: false` en el checkpoint actual.

El JSON de detección se valida antes de publicarse contra
`disturbance-evidence-v1.2.0`. El contrato conserva áreas del screening y
conteos de señales por dominio. La frase “sin cambio detectado en el bosque
automático de alta confianza entre {inicio} y {fin}” sólo se materializa cuando
no existe ninguna señal robusta, CCDC, transitoria, persistente o de desacuerdo
en `automated_forest`; señales exclusivas de `automated_nonforest` no amplían
ese alcance.
La figura QA aplica exclusivamente al panel de
anomalía robusta una escala visual raíz cuadrada con recorte superior p99,
declarada en el propio gráfico. Ese recorte no modifica los arrays, GeoTIFF,
estados, scores ni estadísticas.

`outputs/` está ignorado por Git porque puede contener geometrías o resultados
sensibles. Es evidencia local regenerable, no una dependencia del código ni un
artefacto que otro clon deba asumir presente.

## Configuración, catálogo y contratos

- [`configs/default.yml`](configs/default.yml) contiene los parámetros
  científicos y operativos resueltos en cada corrida.
- [`data/catalog.yml`](data/catalog.yml) registra colecciones, bandas y roles de
  fuentes.
- [`data/licenses.yml`](data/licenses.yml) conserva licencias, atribuciones,
  fechas de acceso y restricciones de los datasets incorporados.
- [`scripts/export_schema.py`](scripts/export_schema.py) regenera los diez JSON
  Schema canónicos.

Versiones vigentes:

| Contrato | Versión |
| --- | ---: |
| paquete Python | `0.1.0` |
| configuración del pipeline | `1.11.0` |
| bundle local | `3.1.0` |
| bundle derivado con benchmark Hampel | `3.2.0` |
| resumen de análisis | `1.0.0` |
| grilla raster | `1.0.0` |
| línea base forestal | `1.0.0` |
| configuración/metadatos RF forestal P0 | `1.0.0` |
| screening forestal | `1.1.0` |
| configuración de detección de perturbaciones | `1.4.0` |
| evidencia publicada de perturbaciones | `1.2.0` |
| eventos persistentes de perturbación | `1.0.0` |
| configuración/metadatos Hampel protegido | `1.0.0` |
| metadatos HLS estacionales | `2.0.0` |
| índice del cubo temporal | `2.0.0` |
| plan y especificación temporal | `1.0.0` |
| cobertura estacional | `1.0.0` |

La identidad científica incluye reglas de análisis, espacio, datos, línea base,
asset/semántica del modelo forestal, detección y segmentación de eventos. La identidad de ejecución
agrega opciones de salida. Una cambia cuando cambia el método; la otra también
cambia ante diferencias operativas.

En corridas estacionales, `analysis_end_date` es la fecha inclusiva final de la
última estación efectivamente publicada —por ejemplo, SON 2025 termina el
`2025-11-30`—. La fecha pedida por el operador se conserva por separado como
`requested_analysis_end_date`; nunca se presenta como cobertura observada.

## Verificación alcanzada

El flujo fue verificado con casos sintéticos determinísticos y con un smoke
remoto autorizado y acotado:

- un caso sintético pequeño verificó la salida segura
  `NOT_EVALUABLE` en 1.216 píxeles sin evidencia forestal base suficiente;
- un fixture sintético de 4 × 4 con AOI irregular y hueco confirmó nodata en
  rasters, exclusión del exterior en estadísticas y CSV, máscara de la figura y
  candidatos persistentes únicamente en las celdas previstas;
- las rutas del runner con CCDC escalar exitoso y globalmente indisponible
  publican los cinco activos base y la colección de eventos, todos presentes
  en el manifiesto con sus hashes;
- el smoke remoto autorizado para 2021–2022 produjo ocho activos de línea base
  y cinco de perturbaciones, validó el JSON con schema `1.0.0`, materializó el
  resumen escalar CCDC sin descargar arrays crudos, registró 15 períodos de
  referencia y siete períodos evaluables, y conservó
  `attribution_generated: false` y `final_assessment_generated: false`;
- el smoke estacional 2020–2025 sobre el AOI autorizado materializó 24
  estaciones publicadas y 15 períodos históricos de referencia. Sobre la
  huella raster de 1.255,32 ha delimitó 900,45 ha automáticamente evaluables,
  314,64 ha de revisión y 40,23 ha con datos insuficientes; dentro del dominio
  automatizado, 543,87 ha corresponden a bosque de alta confianza. CCDC quedó
  disponible y coincidió con el detector robusto en 1.848 candidatos
  persistentes del AOI, 1.465 dentro de `automated_forest`. Como existen
  señales, la corrida conservó `automatic_statement_generated: false`, sin
  atribución ni evaluación final;
- la segmentación de esos 1.465 píxeles forestales produjo 58 eventos y
  131,85 ha de candidatos persistentes: 19 eventos alcanzan `0,5 ha` y 39 se
  conservan por debajo. Los 383 píxeles persistentes fuera de
  `automated_forest` permanecen explícitamente fuera de la interpretación
  primaria. Se generaron cinco fichas, pero las 58 geometrías quedaron en CSV,
  JSON y GeoJSON. La reauditoría validó las 58 geometrías: 43 `Polygon` y 15
  `MultiPolygon`, sin inválidas, preservando IDs, píxeles y superficies. Estos
  conteos no atribuyen deforestación ni uso posterior.
- la derivación Hampel del mismo cubo auditó los 58 eventos y diez controles
  estables para NDVI, EVI2, NMDI, LSWI, NIRv y kNDVI. En eventos marcó entre
  93 y 140 candidatos por índice y aplicó entre 27 y 69 reemplazos; en
  controles marcó entre 19 y 28 candidatos y aplicó entre 5 y 11 reemplazos.
  Los 482 artefactos fuente fueron validados; 481 se reutilizaron byte a byte y
  `json/run/summary.json` se declaró transformado. Todos los artefactos finales
  fueron rehasheados. El gate quedó `not_met`: no hubo orden
  escena-Hampel-agregación,
  validación independiente ni recálculo de impacto sobre onset/magnitud, por
  lo que el benchmark no está activado.
- el P0 provincial 2020 exportó y recuperó 2.000 muestras balanceadas por
  diseño entre UTM 20S y 21S; los dos CSV aprobaron validación de filas,
  columnas, labels, IDs, coordenadas, zona, tamaños y hashes.
- la integración local del RF verifica el contrato ordenado de 56 predictores,
  la exclusión de 12 bandas QA, la reconstrucción de los árboles geemap y la
  publicación de clase, fracción de votos RAW y completitud de inputs; un smoke
  remoto acotado sobre un AOI autorizado confirmó además la reconstrucción del
  asset y la materialización extremo a extremo, sin transferencia temporal.
- el protocolo congelado de validación RF selecciona 50 muestras piloto del
  split `validation` y 200 muestras bloqueadas del split `test`, separa el
  formulario ciego de predicciones, coordenadas y pseudolabels, y genera fichas
  Sentinel-2 2019/2020/2021 sin usar productos forestales para la adjudicación;
  la adjudicación visual asistida por agentes se congeló antes de unir el RF y
  el gate conjuntivo falló por adjudicabilidad `0,56` y ausencia de casos de
  bosque abierto adjudicados como bosque. Sobre los 112 casos adjudicables el
  RF obtuvo precision `0,9298`, recall `0,9138`, F1 `0,9217` y balanced
  accuracy `0,9199`, pero esas métricas parciales no habilitan transferencia
  temporal.

Esas corridas no se versionan y no se identifican aquí porque sus outputs son
locales y potencialmente sensibles. Demuestran transporte y ejecución del
detector; no constituyen validación temática independiente ni prueba de
deforestación.

El gate obligatorio del árbol actual es:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest
```

La configuración exige al menos 90 % de cobertura. Un checkpoint sólo debe
considerarse reproducible después de aprobar el gate completo; no se sustituye
por una corrida manual exitosa.

El árbol depurado del `2026-07-30` aprobó este gate con **343 pruebas** y
**90,94 % de cobertura total**. Ruff, el control de formato y Mypy también
finalizaron sin errores.

El incremento de integración RF del `2026-08-03` aprobó el mismo gate con
**373 pruebas** y **90,88 % de cobertura total**, sin ejecutar exports remotos.

## Límites conocidos

Todavía falta:

- validar con verdad de referencia independiente y casos positivos conocidos;
- obtener referencia independiente adicional para los 88 casos visualmente
  inciertos, especialmente los 11 de bosque abierto; la adjudicación visual
  asistida ya terminó, pero no constituye referencia de campo ni legal y no
  superó el gate científico predefinido;
- calibrar umbrales y scores por paisaje o ecorregión;
- ampliar la cobertura espacial sintética a `MultiPolygon`, bordes que cortan
  píxeles y casos que cruzan zonas UTM;
- atribuir el uso posterior y distinguir conversión de incendio, sequía,
  inundación, defoliación o recuperación;
- validar eventos segmentados contra referencia espacial independiente y
  cuantificar sensibilidad a bordes y resolución;
- incorporar Sentinel-2 a 10 m como producto óptico principal del MVP;
- evaluar Sentinel-1 como evidencia independiente;
- realizar validación espacial independiente.

Las capas globales de bosque no son verdad de terreno. La ausencia de señal no
prueba automáticamente ausencia de conversión; una señal persistente tampoco
prueba su causa. Los estados ambiguos deben avanzar a revisión, no a una
afirmación binaria.

## Cómo retomar el proyecto en otra sesión

1. Leer este `README.md`.
2. Leer [`NEXT_STEPS.md`](NEXT_STEPS.md) sin saltar sus criterios de entrada.
3. Leer [`AGENTS.md`](AGENTS.md) antes de cambiar reglas científicas o alcance.
4. Verificar el árbol y la configuración vigentes; no confiar en nombres de
   pasos históricos.
5. Ejecutar el gate completo antes y después de modificar el núcleo.
6. Tratar `credentials.json`, vectores productivos y `outputs/` como datos
   locales sensibles.
7. Avanzar en incrementos pequeños: contrato, pruebas, implementación,
   verificación y recién entonces el siguiente incremento.
