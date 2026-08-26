# Pipeline de evidencia geoespacial EUDR

Pipeline reproducible para generar evidencia técnica sobre cobertura forestal y
señales de perturbación en establecimientos ganaderos de Argentina.

El sistema **no certifica** que un establecimiento sea libre de deforestación,
no determina por sí solo cumplimiento del Reglamento (UE) 2023/1115 y no
reemplaza la debida diligencia ni la revisión humana. La formulación correcta
del resultado actual es _evidencia de perturbación y atribución conservadora_;
la v1 separa explicaciones compatibles y deriva elegibilidad desde una política
de linaje, pero exige revisión humana antes de sostener una conversión.

Este archivo es la referencia canónica del estado actual. El orden de trabajo
pendiente vive únicamente en [`NEXT_STEPS.md`](NEXT_STEPS.md). Las reglas de
dominio y de desarrollo que deben respetar humanos y agentes viven en
[`AGENTS.md`](AGENTS.md). La semántica científica que separa perturbaciones,
episodios candidatos y eventos probables vive en
[`DISTURBANCE_INTERPRETATION.md`](DISTURBANCE_INTERPRETATION.md).

## Checkpoint actual

**Fecha del checkpoint:** 20 de agosto de 2026.

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
15. publicar todos los episodios candidatos en CSV, JSON y GeoJSON, más un mapa
    general y hasta cinco fichas de los candidatos de mayor superficie;
16. atribuir conservadoramente cada candidato mediante su trayectoria RF, evidencia
    opcional habilitada por política y explicaciones alternativas declaradas;
17. publicar la atribución v4 con inventarios separados: `records` auditables,
    `candidates` no finales y `events` sólo para `conversion_likely`, sin emitir
    confirmación automática;
18. validar polígonos mediante una API interna `/api/v1`, registrar solicitudes
    idempotentes en SQLite y ejecutarlas con un worker local separado del
    proceso HTTP;
19. construir un modelo editorial acotado desde `report_assets` verificados y
    renderizar un informe PDF técnico determinístico, sin reinterpretar la
    evidencia científica;
20. ejecutar por defecto con perfil de artefactos `lean`: los contenedores TIFF
    usados sólo como transporte desde GEE son efímeros, mientras los productos
    científicos y los insumos de figuras conservan sus grillas, hashes y linaje.

La frontera actual es deliberada:

- **detección de perturbaciones:** implementada;
- **atribución post-cambio v4:** implementada por candidato; distingue agricultura
  probable, recuperación, cosecha forestal, perturbación temporal y desconocido;
  todavía requiere recolectar evidencia agrícola y ampliar causas alternativas;
- **episodios candidatos persistentes:** vectorizados y medidos; el umbral de
  0,5 ha clasifica superficie defendible, pero NO convierte una señal en evento;
- **eventos de conversión probable:** sólo existen cuando se satisfacen todas
  las compuertas de atribución definidas en la guía de interpretación;
- **evaluación automática final:** no generada;
- **confirmación de conversión o certificación legal:** fuera de la capacidad
  automática actual.

### API interna local

Los Incrementos 1–4 de [`API_CONTEXT.md`](API_CONTEXT.md) están implementados
como proyectos independientes dentro del monorepo; los paquetes Python forman
el `uv workspace` y el cliente mantiene su toolchain Node aislado:

```text
apps/web/          # React + TypeScript: carga/dibujo, polling y resultados
packages/domain/   # geometría, CRS y medición de área sin stack científico
packages/jobs/     # contrato y repositorio SQLite compartido
packages/reporting/ # contrato editorial y renderer PDF sin GEE ni HTTP
services/api/      # FastAPI: health, validación y registro/consulta de jobs
services/worker/   # consumidor secuencial que invoca run_complete_analysis()
```

La API publica:

- `GET /health`;
- `POST /api/v1/geometries/validate`;
- `POST /api/v1/analyses` con `Idempotency-Key` y respuesta `202`;
- `GET /api/v1/analyses/{analysis_id}`;
- `GET /api/v1/analyses/{analysis_id}/report`;
- `GET /api/v1/analyses/{analysis_id}/candidates` con perturbaciones no finales;
- `GET /api/v1/analyses/{analysis_id}/events` reservado a conversiones probables;
- `GET /api/v1/analyses/{analysis_id}/assets` y descarga por ID opaco;
- `GET /api/v1/analyses/{analysis_id}/report.pdf` para el informe cliente;
- `GET /api/v1/analyses/{analysis_id}/download` para el ZIP verificable;
- `POST /api/v1/analyses/{analysis_id}/cancel` best-effort.

El proceso HTTP nunca ejecuta GEE. Persiste el GeoJSON bajo una clave generada
por el servidor y el worker lo consume fuera del request, con adquisición
condicional del job, concurrencia uno y recuperación local después de un
reinicio. La frontera jurisdiccional de screening se deriva de `ign:pais` del
IGN, está versionada bajo `data/boundaries/` y no sustituye evidencia catastral
ni revisión de casos cercanos a límites.

Los resultados científicos sólo se publican desde `report_assets`: dataset,
inventario y archivos se verifican por tamaño y SHA-256. Después de esa
verificación, el worker genera el PDF cliente como postproceso, sella su tamaño
y SHA-256, y lo agrega sin modificar la allowlist científica. Antes de completar
el job crea tanto el ZIP determinístico como una copia curada bajo
`storage_root/analyses/{analysis_id}/results/attempt-{attempt}`. El intento que
completa mediante CAS en SQLite es la referencia visible; FastAPI lee únicamente esa copia
privada durable: `output_prefix` queda como procedencia y la desaparición del
workspace científico no rompe `/report`, `/candidates`, `/events` ni `/assets`. La expiración
local responde `410`, impide adquirir jobs vencidos y limpia únicamente objetos
privados, nunca los runs científicos.

### Informe técnico PDF - versión cliente

`packages/reporting` `0.11.0`, con contrato editorial `2.8.0`, implementa el
informe como una frontera independiente. Verifica el tamaño y SHA-256 de
`report_assets/index.json`, su dataset y todos los assets allowlisted; luego
proyecta JSON, CSV y GeoJSON verificados a un modelo Pydantic editorial. También
recupera la geometría declarada desde el `input.geojson` sellado por el
`run_manifest.json`; una diferencia de tamaño o hash invalida el render. El
cuerpo cliente conserva métricas, candidatos, eventos probables, condiciones de evidencia, calidad,
fuentes, limitaciones y trazabilidad, pero excluye comparaciones internas de
modelos, votos candidatos y diagnósticos crudos sin interpretación.

`report_assets` `2.1.0` y su dataset `1.4.0` declaran para cada componente si
su evidencia está disponible, junto con el estado y motivo de indisponibilidad.
Un run parcial puede renderizar las tablas y figuras verificadas que sí existen;
si persistencia o atribución no se completaron, el PDF lo identifica como
evidencia incompleta y NO afirma riesgo bajo ni ausencia de conversión. En
cambio, si un componente declara `completed`, la falta de un asset requerido
continúa siendo un error de integridad.
El dataset `1.4.0` mantiene además el inventario completo de perturbaciones del
detector como fuente de `/candidates` aunque la atribución falle; `/events`
permanece vacío y el estado parcial conserva explícitamente la incertidumbre.

El renderer ReportLab produce de forma atómica y determinística un PDF A4 orientado
a revisión y auditoría: portada mínima, resumen ejecutivo en dashboard, identificación,
alcance EUDR, resultado cartográfico, eventos prioritarios, evidencia temporal,
conclusión humana y anexos técnicos. Distingue explícitamente cambio detectado,
evidencia compatible con conversión y decisión humana. El cuerpo sólo rotula cuatro
registros prioritarios; el inventario completo y las fichas secundarias permanecen en
el anexo. La selección GIS incorpora la
cobertura RF anual 2020-2024 con pérdidas candidatas, el timeline RGB estacional
y la evidencia espectral de cada evento detallado. Los mapas vectoriales agregan
perímetro declarado, grilla WGS 84, norte, escala aproximada y un localizador del
evento. Las fichas detalladas suman contexto vectorial OpenStreetMap al 50 % de
opacidad, con atribución visible; se consultan extensiones pequeñas mediante
Overpass y se cachea la respuesta normalizada junto al PDF. El mapa base sólo
orienta y no participa en la detección, atribución o medición científica. Una
repetición con el mismo cache conserva el render determinístico. P0 frente a multianual,
votos candidatos, desacuerdos de clase y diagnósticos crudos permanecen fuera
del cuerpo, pero siguen verificados y disponibles en el bundle técnico.

El rediseño cliente se validó contra el expediente real preservado: 41 assets,
cuatro eventos y dos fichas detalladas. Su PDF final tiene quince páginas -nueve
verticales y seis apaisadas- inspeccionadas una por una. El worker `0.3.0`
invoca el renderer fuera del pipeline científico y fuera del request HTTP,
publica `client_report/informe-tecnico.pdf` con metadatos verificables y lo
incluye en el ZIP. La API `0.4.0` sirve el archivo sólo para jobs publicables,
separa `/candidates` de `/events`, y el cliente web `0.5.0` presenta ambos
inventarios sin equiparar una perturbación con deforestación.

La integración downstream actual consume atribución `5.0.0`: conserva
`candidate_geometry` para auditoría y usa `likely_conversion_geometry` sólo en
eventos probables. La evidencia conjuntiva que no supera estrictamente 0,5 ha
permanece como `subthreshold_conversion_evidence` en `/candidates`; nunca se
mezcla con `/events`. El PDF muestra únicamente las clases presentes y un run
sin eventos no incorpora leyenda ni texto de conversión.

Para desarrollo local, la entrada recomendada es un único supervisor. Inicia
API, **exactamente un worker** y Vite con la misma SQLite, almacenamiento privado
y raíz de runs; valida ambos puertos, `/health`, el servidor web y que el worker
siga vivo antes de declarar el stack listo:

```powershell
uv run python scripts/run_local_stack.py
```

El modo foreground es intencional: `Ctrl+C` ejecuta un shutdown de los tres
procesos iniciados por ese supervisor. Si el trabajo debe sobrevivir al cierre
de la terminal se puede usar `--background` y luego `--stop`:

```powershell
uv run python scripts/run_local_stack.py --background
uv run python scripts/run_local_stack.py --stop
```

La API escucha sólo en `127.0.0.1:8000` y Vite en `127.0.0.1:5173`. El launcher
rechaza otro supervisor vivo y puertos ocupados; **no mata procesos ajenos**.
Los logs sanitizados quedan separados por proceso bajo
`outputs/local-stack/logs/<fecha>/`. API y worker comparten por defecto
`outputs/local-stack/private/jobs.sqlite3` y
`outputs/local-stack/private/objects`; por eso no deben levantarse a la vez con
los comandos manuales antiguos. El worker reutiliza `credentials.json` si
existe o hereda `DEFORESTATION_GEE_CREDENTIALS`; el launcher nunca imprime el
entorno ni las credenciales.

El cliente web minimalista permite cargar un `Polygon`/`MultiPolygon` GeoJSON o
dibujar un `Polygon`, validarlo, crear el job, consultar su estado, cancelar en
modo best-effort, descargar el informe PDF y visualizar el resumen,
perturbaciones candidatas, eventos probables y figuras disponibles. Una
`FeatureCollection` se normaliza únicamente cuando contiene exactamente una
`Feature` poligonal; las colecciones vacías o múltiples se rechazan para
conservar la regla un establecimiento–una geometría. El cliente no reimplementa
reglas geoespaciales: la habilitación de la solicitud depende de la respuesta de
`/api/v1/geometries/validate`.

Durante una ejecución, la interfaz muestra una barra deliberadamente etiquetada
como **estimada**: avanza lentamente mientras el estado sea activo, se limita al
92 % y sólo llega al 100 % cuando la API confirma `completed` o `partial`. Un
fallo o cancelación nunca se presenta como finalización. El `analysis_id` queda
en `?analysis=<uuid>`, por lo que recargar o compartir esa URL reanuda el polling
sin reconstruir estado científico en el navegador.

El toolchain web actual requiere Node 20.19 o posterior; `.nvmrc` fija Node
22.12.0. El launcher comprueba la versión y, si `PATH` todavía apunta a Node 18,
prioriza el Node compatible del runtime bundled de Codex —actualmente Node 24—
sin modificar el `PATH` global. Los comandos manuales siguientes quedan sólo
como alternativa de diagnóstico experto, no como flujo operativo habitual:

```powershell
uv run --package deforestation-api deforestation-api
uv run --package deforestation-worker deforestation-worker

cd apps/web
# Primero instalar/activar Node 22.12 o posterior.
node --version
npx pnpm@11.19.0 install
# Copiar .env.example a .env.local y completar VITE_ARCGIS_API_KEY.
npx pnpm@11.19.0 dev
```

Vite publica el cliente en `127.0.0.1:5173` y redirige `/api` y `/health` a
`127.0.0.1:8000`, por lo que este incremento no abre CORS. El selector del mapa
alterna entre `arcgis/imagery` y `open/osm-style` mediante ArcGIS Basemap Styles
Service v2 y MapLibre, sin cambiar las herramientas de dibujo. Requiere
`VITE_ARCGIS_API_KEY`; la clave del navegador debe restringirse a los orígenes y
servicios autorizados. Ambos mapas son sólo contexto visual y no integran la
evidencia científica.

El gate local de integración levanta una API HTTP descartable y un worker
one-shot en procesos separados. Comparten sólo SQLite y almacenamiento temporal,
usan un runner sintético y verifican estado, reporte JSON, PDF, eventos, asset y
ZIP sin consultar GEE:

```powershell
uv run --all-packages pytest -o addopts='' `
  tests/integration/test_api_worker_flow.py -q
```

Para repetir un análisis real desde la web, verificar primero las rutas sin
iniciar Earth Engine:

```powershell
$input = "C:\Users\Facu\Desktop\prueba-viale.geojson"
$credentials = "C:\Users\Facu\Desktop\deforestaciones-EUDR\credentials.json"
$outputRoot = "C:\Users\Facu\Desktop\deforestaciones-EUDR\outputs\runs"

if (-not (Test-Path -LiteralPath $input -PathType Leaf)) { throw "Falta GeoJSON" }
if (-not (Test-Path -LiteralPath $credentials -PathType Leaf)) { throw "Faltan credenciales" }
$env:DEFORESTATION_GEE_CREDENTIALS = $credentials
$env:DEFORESTATION_ANALYSIS_OUTPUT_ROOT = $outputRoot
```

Con esas variables en la terminal, ejecutar el launcher único, cargar `$input`
y usar `establecimiento-2`. Ese submit sí ejecuta `run_complete_analysis()` y
consume recursos GEE; el gate sintético anterior no.

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
- escala robusta local: `1.4826 × MAD`;
- estabilización: piso cuantílico espacial por estación e índice sobre escalas
  positivas; los MAD nulos siguen siendo no estandarizables;
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
algorítmico complementario. Como comparte HLS con el detector robusto, su
acuerdo no se presenta como independencia de sensor. Los arrays crudos permanecen en Earth Engine; sólo se descarga
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
grilla proyectada. El umbral de `0,5 ha` no elimina componentes menores: genera
una máscara operativa separada de la máscara candidata. Los píxeles evaluables
contiguos a una celda no evaluable o al borde de grilla reciben `EDGE_PIXEL`
como bandera de calidad, nunca como filtro. La ventana de inicio informada es la primera composición
estacional robusta del evento y no una fecha exacta.

### 5. Datasets P0 y multianual de entrenamiento para Entre Ríos

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

El mismo exportador P0 quedó parametrizado para **2020–2024**. El rango termina
en 2024 porque el asset verificado de MapBiomas Argentina Collection 2 v3
publica `classification_2024`, pero no `classification_2025`. No se inventa una
etiqueta 2025.

Los contratos sampling/export/run multianuales se versionan como **1.1.0**:
agregan años y el modo de conteo diferido sin remover campos ni cambiar la
lectura del P0. El config P0 histórico `1.0.0` continúa aceptado, pero esa
versión queda restringida a 2020 y conteos síncronos; cualquier año posterior o
payload diferido exige `1.1.0`.

Cada año conserva exactamente el esquema P0 de 84 columnas. Los composites HLS
se calculan para el `sample_year` real y luego se renombran al contrato canónico
de slots `2020_<estación>_<variable>`. Ese prefijo es compatibilidad de esquema:
**no es un predictor temporal**. `sample_year`, IDs, bloque, split, coordenadas,
zona y procedencia continúan como metadata excluida del RF.

La etiqueta es deliberadamente conservadora: MapBiomas usa la banda del año de
la fila y Hansen aplica pérdida hasta ese mismo año; JRC GFC2020 y WorldCover
v100 permanecen como anclajes fijos 2020. Por eso el dataset favorece clases
estables y **no constituye un LULC anual completo, verdad de terreno ni bosque
EUDR**.

Los diez CSV anuales/UTM se concatenaron sin transformar filas en:

- `outputs/training_p0/raw/entre_rios_training_2020_2024_multiyear.csv`;
- 10.000 observaciones, 84 columnas y SHA-256
  `0701a5f80a44861f5e2fcd0daf19dd4ad75de3dcbeedd99466ef724fead651a2`;
- 2.000 observaciones por año: 800 `forest`, 800 `non_forest` y 400
  `ambiguous`.

No son 10.000 sitios independientes: existen 2.102 `sample_id` únicos y 7.898
repeticiones anuales. Esto forma un panel longitudinal; el split por
`block_id` permanece constante entre años para impedir que el mismo bloque
espacial aparezca en train y test. `ambiguous` se conserva para QA, pero no es
una clase del RF binario. Ningún sitio pasa directamente de `forest` a
`non_forest`; 38 sitios alternan entre una clase unánime y `ambiguous`. El
dataset ayuda a aprender invariancia interanual, no a entrenar detección de
conversión.

La auditoría completa —headers y orden, números finitos, votos, labels,
conteos, tareas, MD5 Drive/local, hashes, claves `(sample_year, sample_id)` y
consistencia espacial del split— está en
`outputs/training_p0/training-multiyear-2020-2024-validation.json`.

### 5.1 Candidato RF multianual 2020–2024

Se entrenó un único candidato de **500 árboles** y 56 predictores para
clasificar el proxy estable `forest/non_forest` en Entre Ríos. `ambiguous`
queda fuera del ajuste y de las métricas binarias, pero conserva predicciones
diagnósticas. La selección comparó, con los mismos hiperparámetros y semilla
42:

- peso uniforme por observación;
- peso `1 / cantidad de observaciones binarias del sample_id`.

Ambos candidatos se ajustaron sólo con `train`; la elección usó únicamente
`validation`. El peso uniforme ganó por peor F1 anual, balanced accuracy y F1
global. El artefacto final se reajustó con `train + validation` y recién
entonces se evaluó una vez sobre `test`.

| Evaluación | Balanced accuracy | Precision bosque | Recall bosque | F1 bosque | ROC-AUC |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline 2020-only, validation | 0,9504 | 0,9548 | 0,9487 | 0,9518 | 0,9839 |
| Multianual peso por sitio, validation | 0,9724 | 0,9900 | 0,9551 | 0,9723 | 0,9932 |
| Multianual uniforme, validation | **0,9756** | **0,9901** | **0,9615** | **0,9756** | 0,9929 |
| Multianual uniforme final, test | 0,9616 | 0,9648 | 0,9577 | 0,9613 | 0,9912 |

En `test`, el peor F1 anual fue `0,9406` en 2023. La evaluación agrupada por
los 231 sitios de test obtuvo F1 `0,9664`. El split por bloques de 3 km evita
que `sample_id` o `block_id` crucen particiones, pero **no constituye un
holdout regional con buffer** ni verdad de terreno independiente.

El modelo y sus resultados reproducibles viven en
`outputs/models/rf_forest_multiyear_2020_2024_v1/`. Se exportó, sin reemplazar
el RF P0, al asset candidato:

`projects/ee-facuboladerasgee/assets/models/rf_forest_multiyear_2020_2024_v1`

El release está registrado en
`data/models/rf_forest_multiyear_2020_2024_v1.json`. El registro no finge que
el joblib de 14 MiB ni los CSV sensibles estén en Git: declara sus paths,
tamaños, hashes, disponibilidad externa y comando de regeneración. Antes de
consultar GEE, el pipeline verifica el hash del registro, el joblib y la
metadata interna del bundle. El contenido remoto también se verificó sobre
los 500 strings `tree`, independientemente de su orden, con fingerprint
`ebb6752bafc3939f673d0c915e89b29cb5efd3826957e83ae1da3e4e55d139a3`.

La TABLE contiene 500 propiedades `tree` en modo `classification`; el
pipeline deriva localmente la fracción RAW promediando la predicción binaria
de cada árbol. No usa `RandomForestClassifier.predict_proba`, que representa
otra magnitud, y el resultado **no es una probabilidad calibrada**. La paridad
sobre 24 filas de test conservó 24/24
clases. En tres filas cambiaron uno o dos votos de 500 porque geemap redondea
los thresholds serializados a seis decimales; el error máximo de fracción fue
`0,004` y no alteró ninguna clase.

El exportador respeta el límite real de **10 MiB por request** de GEE: divide
automáticamente el payload en partes de hasta 8 MiB, usa nombres temporales con
un UUID propio de cada ejecución, verifica un asset de staging y limpia en un
`finally` solamente los assets registrados por ese run. `--allow-large-model`
no evita el límite remoto; queda sólo como opción legacy y siempre usa
multipart. Todas las descripciones efectivas de tareas, incluso una base
provista con `--description`, se normalizan a caracteres seguros, respetan un
máximo conservador de 100 caracteres e incorporan el UUID del run; el lookup
usa esa descripción exacta y no reutiliza tareas históricas con la misma base.
`--overwrite` tampoco promete atomicidad: GEE expone `renameAsset`
y `copyAsset`, pero no una transacción multiasset. Por eso el reemplazo usa
staging verificado, backup, promoción y rollback best-effort; durante esas
llamadas puede existir una ventana sin el nombre final.

Este asset sigue siendo **candidato**: no reemplaza el asset activo de
`configs/default.yml` hasta validar transferencia temporal dentro del pipeline.
Clasifica cobertura forestal proxy estable; no detecta conversión y no demuestra
ausencia de deforestación ni cumplimiento EUDR.

### 5.2 Recolector agrícola mensual por evento

`agricultural_collector.py` implementa la adquisición espacial acotada del
incremento agrícola. Consume un bundle de eventos, inicia las ventanas el día
posterior al fin del onset estimado y consulta únicamente meses calendario
hasta la fecha inclusiva pedida. La fuente candidata inicial es Dynamic World
V1: `crops` requiere simultáneamente etiqueta 4 y probabilidad superior al
umbral versionado; `grass` no se interpreta automáticamente como pastura y
`built` no demuestra uso ganadero.

Cada evento usa una grilla única EPSG:6933 a 10 m. El área no se obtiene del
rectángulo raster: se pondera con un GeoTIFF de fracción exacta de intersección
entre el polígono y cada píxel. Por ventana se conservan conteos válidos,
conteos de cultivo calificante, probabilidad media y fracción de observaciones
agrícolas. `crop`, `unknown` y `nodata` son mutuamente excluyentes y deben
cerrar el área del evento.

El bundle atómico publica `agricultural_evidence.csv`,
`agricultural_evidence.geojson`, `agricultural_evidence.json`,
`agricultural_evidence_metadata.json`, los rasters por evento auditables y un manifest
con hashes y referencias al bundle fuente. La salida cruda **no** afirma
persistencia ni conversión. El componente de persistencia consume este bundle
sin reinterpretar una observación mensual como uso permanente.

Desde la configuración del collector `1.2.0`, los candidatos que comparten un
mes reutilizan una única adquisición Dynamic World sobre una grilla común
acotada. `maximum_events_per_batch` limita a 100 candidatos cada evaluación
vectorizada de conteos de escenas, pero **no limita el inventario científico**:
todos los lotes se procesan en orden por identificador y el GeoTIFF mensual se
descarga una sola vez. El raster común se recorta localmente, con alineación
`nearest`, a cada grilla candidata. Así también se conservan los candidatos
subumbrales, cada uno con raster, footprint, área, `nodata`, hashes y referencia
a su adquisición y lote de origen. Los límites independientes siguen
controlando adquisiciones mensuales y píxeles de la grilla común.

El payload científico `agricultural-collection` permanece en `1.0.0`; el bundle
sube a `1.3.0` porque su metadata y manifest declaran el plan de lotes. La
partición modifica transporte y auditoría, no ventanas, áreas ni semántica de
persistencia.

El perfil `output.artifact_profile: lean`, activo por defecto, no incorpora al
bundle el GeoTIFF común bajo `_shared`: se usa como transporte efímero y cada
ventana por evento mantiene el raster que consume persistencia. El perfil
`debug` conserva además ese raster compartido para diagnóstico. Este incremento
reduce almacenamiento y transferencia del entregable sin cambiar cálculos,
ventanas, áreas ni productos downstream; mover la persistencia temporal a GEE
queda diferido al siguiente incremento.

Cuando existe al menos un período con soporte espacial válido, también publica
una lámina `agricultural_monthly_evidence_<event-id>.png` por evento. La lámina
no multiplica un PNG por mes: muestra la serie temporal completa y hasta cuatro
pares de mapas, elegidos por una regla determinística registrada en el manifest
—primer período programado, primer período espacialmente válido, pico de la
fracción media de observaciones `crop` y último período programado—. Cada par
separa fracción de observaciones `crop` y score medio `crop`. `nodata` aparece
en gris y nunca se confunde con un cero observado. El manifest registra hash
del PNG, TIFFs fuente, ventanas seleccionadas, semántica de bandas y disclaimer.

```powershell
uv run python scripts/run_agricultural_collector.py `
  outputs/runs/<run-id>/components/full_pipeline/<bundle> `
  --establishment-id establecimiento `
  --analysis-end-date 2025-12-31 `
  --credentials .\credentials.json
```

El comando completo recomendado ya ejecuta este recolector y la persistencia
sin exigir que el operador copie rutas entre comandos:

```powershell
uv run python scripts/run_complete_analysis.py .\establecimiento.geojson `
  --establishment-id establecimiento `
  --credentials .\credentials.json
```

Dentro del mismo parent run, el orquestador pasa el bundle de eventos recién
creado al recolector, su salida a persistencia y los tres bundles del mismo run
al atribuidor. El linaje conserva `analysis_id`, hash del input y el hash exacto
de `disturbance_events.geojson`; así se rechaza evidencia derivada de otros
eventos. `--agricultural-persistence-bundle` y
`--agricultural-evidence-json` permanecen sólo como modos precomputados
explícitos para reproducir o diagnosticar expedientes anteriores; son
excluyentes y desactivan la recolección automática.

La ruta productiva GEE está cubierta por tests con dobles, incluidos filtros
espaciales/temporales y la regla de clase. Además se ejecutó el smoke remoto
acotado de un evento/mes descripto en la sección de verificación; sigue
pendiente el run remoto completo.

### 5.3 Persistencia agrícola post-cambio

`agricultural_persistence.py` aplica la regla versionada de
`configs/agricultural-persistence.yml`. La ventana continúa siendo estrictamente
posterior al onset. El gate primario exige al menos tres períodos válidos, dos
períodos agrícolas calificantes y 180 días observados. Los períodos agrícolas
no tienen que ser consecutivos: un barbecho observado no borra un cultivo
estacional previo. Los meses sin soporte permanecen como gaps y nunca se
interpolan. Tanto la duración como el máximo de gaps se evalúan por píxel sobre
observaciones efectivamente válidas; la mera extensión de la ventana solicitada
no satisface el gate.

Por píxel se materializa un raster de fracción persistente del evento en
EPSG:6933. El bundle publica además período, soporte mensual, gaps, desacuerdo
temporal, área persistente y sensibilidad a umbrales `0,40`, `0,50` y `0,60`.
Una serie corta produce `insufficient_series` y cero observaciones
habilitantes. Con una sola fuente temporal, el desacuerdo entre fuentes queda
`single_source_not_assessed`; no se fabrica concordancia.

Un evento con soporte persistente espacial positivo agrega
`agricultural_persistence_<event-id>.png`: mapa del soporte, serie temporal
completa y sensibilidad a umbrales. Un evento insuficiente o sin soporte no
fabrica una lámina vacía. El PNG es presentación derivada; el GeoTIFF continúa
siendo el producto científico utilizado para calcular superficie.

```powershell
uv run python scripts/run_agricultural_persistence.py `
  outputs/agricultural_evidence/<bundle-mensual>
```

Dynamic World no distingue por sí mismo recuperación forestal. Esa explicación
alternativa se evalúa después con la trayectoria RF y queda registrada como
limitación del bundle de persistencia.

### 5.4 Atribución post-cambio v4

`post_change_attribution.py` cruza cada geometría candidata de perturbación persistente
con las clases y votos RF 2020–2024. Publica CSV, JSON, GeoJSON, metadata y una
figura con cinco resultados posibles: `agriculture_likely`,
`forest_recovery`, `managed_harvest`, `temporary_disturbance` y `unknown`.

La regla es deliberadamente conservadora. Un registro sólo se publica como
`conversion_likely_event` cuando `agriculture_likely` alcanza
`conversion_likely` y coinciden bosque al corte, pérdida
poscorte, persistencia, área defendible, evidencia **habilitada por política** de uso
agrícola/ganadero posterior y ausencia de una explicación alternativa fuerte.
Una pérdida RF sola queda `unknown/review_required`. Un contexto declarado de
plantación forestal se registra como explicación alternativa, nunca como
evidencia independiente. El atribuidor automático siempre conserva
`conversion_confirmed: false`. El JSON `5.0.0` publica `records` como inventario
completo, `candidates` para lo que no completó las compuertas y `events`
exclusivamente para conversiones probables. Un candidato temporal, recuperado o
no persistente ya no fuerza por sí solo `review_required` en el establecimiento.

La integración persistente acepta un **bundle**, no un booleano ni un área
declarada. Para cada evento reproyecta la conjunción RF de bosque 2020, pérdida
poscorte y no-bosque persistente sobre la grilla agrícola métrica; la multiplica
por el footprint agrícola persistente y publica el raster resultante. El área
de ese raster sólo se informa como `likely_conversion_area_ha` cuando también
aprueban política, geometría y ausencia de recuperación. Si falla cualquier
gate, el área probable es cero y quedan razones a favor y en contra.

Cuando la conjunción contiene soporte positivo, el bundle agrega
`likely_conversion_conjunction_<event-id>.png`, con mapa, escala, norte, área y
los cuatro términos de la conjunción. La leyenda distingue `nodata` de cero y
el título usa “evidencia compatible con conversión”: no es confirmación legal
ni certificación. Una conjunción vacía conserva el TIFF y el resultado
estructurado, pero no genera un mapa engañoso sin soporte.

La entrada opcional sigue el contrato estricto
`agricultural-evidence-v1.0.0`: exige fuente y licencia, fecha de acceso,
geometría, resolución, cobertura espacial, ventana post-cambio, persistencia,
calidad y hashes. El CLI rechaza campos extra —incluido `independent`— y deriva
la independencia con `configs/agricultural-evidence.yml`. Una fuente no
registrada o presente en el linaje de entrenamiento/validación no abre el gate.

El control costa-uru, declarado por el usuario como plantación forestal con
ciclos de cosecha, produjo 37 eventos: seis compatibles con
`managed_harvest` por pérdida seguida de recuperación y 31 `unknown` sin
recuperación suficiente dentro de 2020–2024. No produjo eventos
`conversion_likely` ni `conversion_confirmed`.

### 5.5 Deltas experimentales del RF multianual

La CLI incorpora un modo opt-in exclusivo que calcula los stacks HLS anuales
2020–2024 en GEE, canoniza cada año a los 56 slots del modelo y aplica el
joblib sellado por SHA-256 sobre el AOI. GEE sí puede construir el
`decisionTreeEnsemble` candidato y evaluarlo puntualmente con `reduceRegion`.
La limitación observada es más acotada: `getDownloadURL` falló con
`Description length exceeds maximum` para el raster de clase 2020 formado por
el stack HLS anual real más el candidato, mientras el mismo flujo P0 sí pudo
descargarse. El componente exacto que lleva ese grafo completo al límite no
está aislado; por eso no se atribuye causalidad solamente al tamaño del TABLE.

El fallback descarga desde GEE un transporte multibanda por año con los 56
predictores, las 12 bandas QA y la completitud, y aplica localmente el joblib
sklearn exacto, sin reducir árboles ni cambiar el modelo. El transporte se
divide localmente en los mismos productos científicos y no se persiste. Para
2020–2024 más P0 esto reduce el caso normal de 18 a 6 transportes; si una
solicitud consolidada supera el límite directo configurado, vuelve de forma
adaptativa a las descargas individuales. Se conserva por año un GeoTIFF QA de
12 bandas: conteo válido per-pixel
total, L30 y S30 para DJF, MAM, JJA y SON. Cada QA registra nombres de banda,
semántica, grilla, nodata y SHA-256. La fecha solicitada `2024-12-31` queda como
`requested_analysis_end_date`; la cobertura efectiva termina en la última
estación publicada, `analysis_end_date=2024-11-30`.

```powershell
uv run python pruebas.py C:/Users/Facu/Desktop/costa-uru.geojson `
  --rf-annual-deltas `
  --forest-model-config configs/rf-forest-entrerios-2020-2024.yml `
  --analysis-end-date 2024-12-31 `
  --establishment-id costa-uru-rf-deltas-2020-2024
```

La corrida canónica de `costa-uru`
`costa-uru-rf-deltas-2020-2024__20260806T213320256834Z__582c56fd-557`
publicó clases, fracciones no calibradas,
deltas año–2020, transiciones, comparación P0/candidato, QA espacial, CSV, JSON
y dos figuras: 56 artefactos manifestados y 38 TIFF sobre una única grilla
EPSG:32721 de 149×98 píxeles a 30 m, nodata `-9999`. El panel principal usa
leyendas discretas completas, norte y escala métrica. Las métricas permanecen
idénticas al control anterior; las pérdidas y ganancias candidatas no pueden
interpretarse como conversión sin persistencia, atribución y referencia
independiente. Por eso `temporal_transfer_validated` continúa en `false` y el
default sigue en P0.

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

Para los años adicionales se reutiliza el mismo script, cambiando únicamente
el archivo de configuración:

```powershell
2021..2024 | ForEach-Object {
  uv run python scripts/export_training_p0.py `
    --config "configs/training-p0-entrerios-$_.yml" `
    --auth-mode user_oauth `
    --start-export
}
```

El conteo síncrono provincial excedió dos veces el límite de cómputo interactivo
de GEE para 2021. Los configs 2021–2024 difieren esa comprobación a la validación
del CSV batch ya exportado; no omiten la validación final. Las ocho tareas
terminaron en `COMPLETED`:

| Año | UTM 20S | UTM 21S |
| --- | --- | --- |
| 2021 | `ZJ65D3BSWRZ6TDFH4NTU3KKU` | `TGS6FSSD2JRONUEU4UIRTUC7` |
| 2022 | `YVYXRYGBGHR3TS3UVBQHEKUQ` | `GQ44P7NLPDMXKPI7XJOU3V6O` |
| 2023 | `ZW2HNUCOZVNNILDBWBOYNTFS` | `2JY6QHD64VYUU3TB5XJDHI3L` |
| 2024 | `QHVAG7L3NEAY3ZMRUTT6I4FC` | `KTEFWYP73BDDMG7NC62PSQPI` |

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

La segmentación persistente agrega cinco activos para la colección completa y
como máximo cinco fichas seleccionadas por superficie descendente e ID:

```text
tables/evidence/disturbance_events.csv
json/evidence/disturbance_events.json
json/evidence/disturbance_events.geojson
tiffs/evidence/disturbance_event_masks.tif
figures/evidence/disturbance_events.png
figures/evidence/disturbance_event_<event-id>.png
```

El CSV, JSON y GeoJSON conservan **todos** los eventos, incluidos los menores a
`0,5 ha`; el TIFF de dos bandas distingue `persistent_candidate_mask` de
`operational_event_mask`. El límite espacial no borra evidencia y la selección
por superficie sólo controla cuántos PNG individuales se renderizan. Las
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
sensibles. Los runs analíticos son evidencia local y otro clon no debe asumirlos
presentes. La excepción actual es
`outputs/models/rf_forest_multiyear_2020_2024_v1/`: la inferencia local del RF
multianual necesita allí el joblib registrado. Un clon nuevo debe restaurar ese
release por sus hashes o regenerarlo; el asset GEE por sí solo no satisface el
preflight local vigente.

## Configuración, catálogo y contratos

- [`configs/default.yml`](configs/default.yml) contiene los parámetros
  científicos y operativos resueltos en cada corrida.
- [`data/catalog.yml`](data/catalog.yml) registra colecciones, bandas y roles de
  fuentes.
- [`data/licenses.yml`](data/licenses.yml) conserva licencias, atribuciones,
  fechas de acceso y restricciones de los datasets incorporados.
- [`scripts/export_schema.py`](scripts/export_schema.py) regenera catorce JSON
  Schema canónicos. El contrato de atribución post-cambio se conserva además
  como schema versionado independiente.

Versiones vigentes:

| Contrato | Versión |
| --- | ---: |
| paquete Python | `0.1.0` |
| configuración del pipeline | `1.13.0` |
| bundle local | `3.1.0` |
| bundle derivado con benchmark Hampel | `3.2.0` |
| bundle experimental de deltas RF multianuales | `3.5.0` |
| envelope del análisis completo | `2.3.0` |
| índice de figuras para informe / política de selección | `2.0.0` |
| dataset editorial para informe | `1.4.0` |
| paquete de reporting / view-model editorial | `0.11.0` / `2.8.0` |
| atribución post-cambio por candidato/evento | `5.0.0` |
| evidencia agrícola / política de independencia | `1.0.0` |
| documento de recolección agrícola mensual | `1.0.0` |
| bundle de recolección agrícola compartida / configuración | `1.2.0` |
| persistencia agrícola por evento | `1.0.0` |
| catálogo de fuentes | `2.1.0` |
| resumen de análisis | `1.0.0` |
| grilla raster | `1.0.0` |
| línea base forestal | `1.0.0` |
| configuración/metadatos RF forestal P0 | `1.0.0` |
| sampling/export/run de entrenamiento multianual | `1.1.0` |
| evidencia temporal RF y QA per-pixel | `1.2.0` |
| screening forestal | `1.1.0` |
| configuración de detección de perturbaciones | `1.5.0` |
| evidencia publicada de perturbaciones | `1.2.0` |
| episodios persistentes candidatos (path legado `disturbance_events`) | `1.1.0` |
| configuración/metadatos Hampel protegido | `1.0.0` |
| metadatos HLS estacionales | `2.0.0` |
| índice del cubo temporal | `2.0.0` |
| plan y especificación temporal | `1.0.0` |
| cobertura estacional | `1.0.0` |

La identidad científica incluye reglas de análisis, espacio, datos, línea base,
asset/semántica del modelo forestal, detección y segmentación de eventos. La identidad de ejecución
agrega opciones de salida. Una cambia cuando cambia el método; la otra también
cambia ante diferencias operativas.

En corridas estacionales y en los deltas RF multianuales, `analysis_end_date`
es la fecha inclusiva final de la última estación efectivamente publicada —por
ejemplo, SON 2024 termina el `2024-11-30`—. La fecha pedida por el operador se
conserva por separado como `requested_analysis_end_date`; nunca se presenta
como cobertura observada.

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

El cierre de los pasos 0–1 de evidencia agrícola del `2026-08-11` aprobó el
gate completo con **534 pruebas** y **90,53 % de cobertura total**. Ruff, el
control de formato y Mypy también finalizaron sin errores.

Los incrementos 2–3 de evidencia agrícola del `2026-08-11` aprobaron el gate
completo con **547 pruebas** y **90,36 % de cobertura total**. Ruff, el control
de formato y Mypy también finalizaron sin errores. Esta verificación usa un
proveedor GEE falso determinístico; no sustituye el smoke remoto pendiente.

Los incrementos 4–5 de persistencia e integración agrícola del `2026-08-11`
aprobaron el gate completo con **566 pruebas** y **90,38 % de cobertura total**.
Ruff, formato y Mypy también aprobaron. Los casos agrícolas nuevos son
sintéticos y determinísticos: verifican contratos e invariantes, no sustituyen
un smoke GEE ni validación temática independiente.

La integración operativa one-command y la corrección del transporte
EPSG:6933 del `2026-08-11` aprobaron el gate completo con **577 pruebas** y
**90,43 % de cobertura total**. Ruff, formato, Mypy sobre 105 archivos y
`git diff --check` también aprobaron. Todavía no se ejecutó un run remoto real
de los seis componentes.

La incorporación de láminas agrícolas auditables del `2026-08-12` aprobó el
gate completo con **587 pruebas** y **90,62 % de cobertura total**. Ruff,
formato, Mypy sobre 107 archivos y `git diff --check` también aprobaron. El QA
visual usó los TIFF del collector del run real conservado y derivó localmente
persistencia y conjunción en una carpeta temporal, sin modificar el expediente
histórico ni realizar nuevas consultas GEE. Esta comprobación valida el render
y el linaje operativo, no la exactitud temática de Dynamic World.

La colección curada de figuras para informe del `2026-08-12` aprobó el gate
completo con **589 pruebas** y **90,50 % de cobertura total**. Ruff, formato,
Mypy y `git diff --check` también aprobaron. La colección copia únicamente PNG
declarados y hasheados por los manifests hijos; no renderiza de nuevo ni mueve
los productos científicos originales.

La reparación de escala del collector del `2026-08-13` aprobó el gate completo
con **593 pruebas** y **90,52 % de cobertura total**. Ruff, formato, Mypy y
`git diff --check` también aprobaron. El gate incluye planificación compartida,
recorte a grillas de evento, compatibilidad con persistencia/atribución y
diagnóstico sanitizado de `report_assets`; el micro-smoke remoto se limitó a
una ventana mensual y no sustituye la repetición del run completo.

La estructuración del paquete editorial del `2026-08-13` aprobó el gate
completo con **595 pruebas** y **90,62 % de cobertura total**. Ruff, formato,
Mypy y `git diff --check` también aprobaron. Además se materializó la política
sobre una copia temporal del expediente `establecimiento-2`: seleccionó dos
eventos prioritarios, 16 figuras y 41 archivos curados, sin modificar el run
histórico ni ejecutar GEE. Esta verificación prueba organización, hashes y
linaje; no constituye una nueva evaluación científica del establecimiento.

Los Incrementos 1–2 de API local del `2026-08-17` agregaron suites modulares
sin GEE: **3 pruebas** para `deforestation-jobs` con 91,43 % de cobertura,
**8 pruebas** para `deforestation-api` con 91,92 % y **11 pruebas** para
`deforestation-worker` con 96,06 %. Verifican validación Polygon/MultiPolygon,
jurisdicción y área métrica; idempotencia; adquisición condicional; reinicio;
publicación `complete/partial`; hashes; límites sobre bytes realmente recibidos;
rechazo de rutas inseguras y errores sanitizados. Estas pruebas operativas no
reemplazan el smoke remoto pendiente.

El Incremento 3 amplió las suites modulares a **6 pruebas** para
`deforestation-jobs` con 95,27 % de cobertura, **13 pruebas** para
`deforestation-api` con 90,36 % y **13 pruebas** para
`deforestation-worker` con 91,47 %. El gate científico independiente conservó
sus **595 pruebas** y 90,62 %. Ruff, formato, Mypy y `git diff --check`
aprobaron; no hubo build ni ejecución remota GEE. Además, una integración de
lectura sobre el expediente real preservado `establecimiento-nuevo` verificó
41 assets y el inventario completo de cuatro eventos sin modificar el run.

El cliente web minimalista del `2026-08-17` agrega **13 pruebas Vitest** para
normalización GeoJSON, errores HTTP, idempotencia, flujo de validación/creación,
progreso estimado honesto y reanudación del seguimiento por URL.
El chequeo estricto `tsc --noEmit` y la auditoría de dependencias productivas
aprobaron sin vulnerabilidades conocidas. También se verificaron visualmente la
vista desktop, el layout móvil, el dibujo de un polígono y una validación contra
la API local. Una prueba de configuración protege además la exclusión de
MapLibre del prebundle de Vite, necesaria para cargar su worker de forma estable
en Windows. No se ejecutó build ni un nuevo análisis GEE.

El gate de integración multiproceso del `2026-08-17` elevó el gate raíz a
**596 pruebas** y conservó **90,62 % de cobertura total**. La prueba levanta
FastAPI sobre TCP y un worker one-shot separado, encola por HTTP, publica un
`report_assets` sintético y verifica estado, reporte, evento, asset hasheado y
ZIP. Las suites modulares, Ruff, formato, Mypy y `git diff --check` permanecen
verdes. Este gate demuestra el plumbing local, NO la validez científica ni el
smoke GEE de seis componentes.

El endurecimiento previo a contenedores del `2026-08-18` elevó el gate raíz a
**598 pruebas** y **90,67 % de cobertura total**. Las suites modulares aprobaron
con **35 pruebas** del dominio geoespacial (90,03 %), **6** de jobs (95,27 %),
**14** de API (90,66 %) y **17** de worker (90,30 %); el gate multiproceso
también permaneció verde. Ruff, formato, Mypy sobre 139 archivos y
`git diff --check` aprobaron. El cambio corrige limitaciones de atribución
obsoletas en reportes nuevos, publica resultados curados durables y elimina la
dependencia de la API sobre el paquete científico completo.

El Incremento 1 del informe PDF del `2026-08-18` elevó el gate raíz a
**605 pruebas** y conservó **90,67 % de cobertura total**. El paquete
`deforestation-reporting` agrega **7 pruebas** con **93,67 % de cobertura** para
contrato editorial, integridad de assets, lenguaje legal, determinismo byte a
byte y escritura atómica. Ruff, formato sobre 144 archivos, Mypy y
`git diff --check` aprobaron. También se renderizaron e inspeccionaron las seis
páginas de un expediente sintético representativo; no hubo build, integración
HTTP ni ejecución GEE.

El Incremento 2 del informe PDF del `2026-08-18` elevó el gate raíz a
**606 pruebas** y mantuvo **90,67 % de cobertura total**. Las **8 pruebas** de
`deforestation-reporting` alcanzaron **94,00 %**. El PDF real quedó en 21
páginas A4, con cinco páginas apaisadas para figuras ultrapanorámicas y 16
verticales; su contenido se verificó con PyPDF/PDFPlumber y todas las páginas
se renderizaron con Poppler para QA visual. El expediente fuente permaneció
intacto y no se ejecutó GEE ni build.

El rediseño cliente `0.3.0` del `2026-08-18` mantiene el gate raíz en **606
pruebas** y **90,67 % de cobertura total**. Las **8 pruebas** de
`deforestation-reporting` alcanzan **91,62 %** tras ampliar el contrato a datos
estructurados verificados. El PDF real quedó en nueve páginas A4 verticales con
mapa e inventario completo, condiciones por evento, trayectorias forestales,
soporte agrícola, sensibilidad, calidad y fuentes. Ruff, formato sobre 146
archivos, Mypy sobre 145 archivos y `git diff --check` aprobaron. Las figuras
internas permanecen en el expediente técnico y no se ejecutó GEE ni build.

El ajuste editorial `0.4.0` del `2026-08-18` conserva **606 pruebas** y **90,67
% de cobertura total**. Las **8 pruebas** de `deforestation-reporting` alcanzan
**91,84 %**. El mismo expediente real ahora produce trece páginas -nueve A4
verticales y cuatro apaisadas- con cobertura RF anual, timeline RGB y evidencia
espectral por evento, mientras el resumen abandona las tarjetas de dashboard en
favor de una tabla sobria. Ruff, formato, Mypy y `git diff --check` aprobaron;
no se ejecutó GEE ni build.

El ajuste cartográfico y narrativo `0.5.0` del `2026-08-20` eleva el gate a
**607 pruebas** y conserva **90,67 % de cobertura total**. Las **9 pruebas** de
`deforestation-reporting` alcanzan **90,32 %** e incluyen la verificación de la
geometría declarada. El expediente real produce quince páginas -nueve A4
verticales y seis apaisadas- con índice, texto interpretativo, RF y RGB
recompuestos sin superposiciones y mapas con grilla WGS 84, norte, escala y
localizador. Ruff, formato sobre 146 archivos, Mypy sobre 145 archivos y
`git diff --check` aprobaron; las 15 páginas se inspeccionaron con Poppler. No se
ejecutó GEE ni build.

El ajuste de contexto cartográfico `0.6.0` del `2026-08-20` eleva el gate a
**609 pruebas** y conserva **90,67 % de cobertura total**. Las **11 pruebas** de
`deforestation-reporting` alcanzan **90,15 %** e incluyen caché, atribución,
opacidad fija y degradación no bloqueante del contexto OSM. La portada presenta
un índice explícito y explica el carácter científico del informe; los mapas de
eventos componen vectores OpenStreetMap al 50 % sin usar automáticamente el
servidor comunitario de teselas. Ruff, formato sobre 147 archivos, Mypy sobre
146 archivos y `git diff --check` aprobaron; no se ejecutó GEE ni build.

La integración operativa del PDF del `2026-08-20` eleva el gate raíz a **610
pruebas** y mantiene **90,67 % de cobertura total**. Reporting aprueba **12
pruebas** con **90,15 %**, el worker **20 pruebas** con **90,62 %**, la API **15
pruebas** con **90,47 %** y el cliente web **15 pruebas Vitest** más typecheck
estricto. El gate multiproceso confirma HTTP → SQLite → worker → PDF → API/ZIP
sin GEE. Ruff, formato sobre 147 archivos, Mypy sobre 146 archivos y
`git diff --check` aprobaron. No se ejecutó un nuevo análisis remoto ni se
construyó el frontend o una imagen de contenedor.

La optimización lean y su endurecimiento operativo elevan el gate raíz a **614
pruebas** y mantienen **90,66 % de cobertura total**. El gate cubre los
perfiles `lean/debug`, un transporte HLS consolidado, seis transportes RF en el
caso normal, fallback RF
por límite directo y retención condicional del raster agrícola compartido.
Ruff, formato sobre 147 archivos, Mypy sobre 148 archivos y
`git diff --check` aprobaron. No se ejecutó GEE, frontend ni build de contenedor.

El incremento de robustez eleva el gate raíz vigente a **619 pruebas** y
**90,69 % de cobertura total**. Incluye el piso de escala robusta, QA de borde,
máscaras candidata/operativa, compatibilidad editorial retroactiva y lenguaje
explícito en el PDF. Ruff, formato sobre 147 archivos, Mypy sobre 148 archivos y
`git diff --check` aprobaron. No se ejecutó GEE ni ningún build.

El incremento semántico candidato/evento eleva el gate raíz a **620 pruebas** y
**90,71 % de cobertura total**. Los gates específicos aprobaron **16 pruebas de
API con 90,37 %**, **16 de reporting con 90,29 %** y **16 Vitest**, además de
typecheck, Ruff, formato, Mypy y `git diff --check`. No se ejecutó GEE, build del
frontend ni build de contenedores.

El smoke posterior `nativo-robustness-retry` sí ejecutó GEE y cerró los seis
componentes. Sobre el mismo GeoJSON del run anterior conservó 34 candidatos,
pero el subconjunto por área bajó de 8 a 6 candidatos y de 6,39 a 4,95 ha. La
atribución disponible muestra cero eventos probables: ningún candidato reunió
persistencia y uso agropecuario posterior. Un
primer intento encontró `remote_server_error` en el transporte RF 2021; el
fallback multibanda acepta ahora ese fallo remoto sanitizado y puede continuar
con los productos individuales.

La prueba de consola posterior con `plantaciones-uru.geojson` verificó el guard
de costo para AOI grandes: 74.949,64 ha producen una grilla HLS de 1.224 × 922 y
1,68 GB estimados para las 24 estaciones 2020–2025, frente al presupuesto de
256 MB. El run se detuvo antes de descargar rasters; no se aumentaron límites ni
se simplificó la geometría. El caso queda como entrada real para el Incremento 2
de agregación remota y entrega tabular.

La prueba posterior con `prueba-pequeña.geojson` (1.198,06 ha) sí completó los
seis componentes científicos: pipeline principal, Hampel, RF, colección
agrícola, persistencia y atribución. `report_assets` reunió 53 archivos y 26
figuras, y desde ese expediente se renderizó e inspeccionó un PDF cliente de 37
páginas. El transporte RF consolidado necesitó el fallback individual porque
Earth Engine no pudo generar la URL remota; el fallback ahora cubre también
`download_url_failed`. La publicación final del run encontró un bloqueo
transitorio de Windows (`WinError 5`) y dejó el expediente en `.staging` con un
sobre `partial`, aunque todos los componentes constan como `completed`. El
orquestador reutiliza ahora el mismo rename atómico con reintentos que ya usa el
runner local. Falta repetir el smoke para validar ese último cierre; no se
interpreta el expediente interrumpido como estabilidad operativa completa.

El smoke web real `c938a1c0-ca33-42a4-94b3-81c7276a3ef8` terminó `partial` por
la indisponibilidad diagnóstica y no bloqueante de Hampel; los otros cinco
componentes completaron y dejaron cuatro eventos y 41 assets verificables. Su
copia privada fue migrada sin alterar el expediente para comprobar que la API
puede leer reporte, eventos y assets aunque ya no exista `outputs/runs`.

El primer run remoto conservado expuso una incompatibilidad concreta de
transporte: Earth Engine no interpreta el alias literal `EPSG:6933` en
`getDownloadURL`, aunque sí acepta la definición equivalente WKT1_GDAL. El
pipeline mantiene `EPSG:6933` como CRS científico y del GeoTIFF, y convierte
solamente ese parámetro remoto a WKT1_GDAL. La corrección se comprobó de forma
acotada sobre el evento `PDE-F225FE2B4977`, ventana `2021-09`: seis escenas,
grilla 38 x 94 y GeoTIFF validado. Esto NO equivale todavía a un run remoto
completo de seis componentes.

## Ejecución completa recomendada

El orquestador reutiliza las funciones públicas existentes, autentica Earth
Engine una sola vez y ejecuta este DAG secuencial:

1. pipeline principal y eventos;
2. benchmark Hampel;
3. deltas anuales del RF multianual candidato;
4. recolección mensual Dynamic World por evento;
5. persistencia agrícola;
6. atribución post-cambio.

```powershell
.\.venv\Scripts\python.exe .\scripts\run_complete_analysis.py `
  "C:\Users\Facu\Desktop\costa-uru.geojson" `
  --establishment-id costa-uru-completo `
  --declared-land-use managed_forest_plantation `
  --declared-context-source user_declared `
  --credentials .\credentials.json
```

Como alternativa a la cuenta de servicio, el mismo comando acepta
`--gee-project <proyecto-versionado>` para reutilizar la credencial OAuth de
usuario ya persistida. `--credentials` y `--gee-project` son excluyentes; en
ambos modos se crea una sola sesión GEE y se comparte entre HLS, RF y Dynamic
World.

Cada ejecución se publica bajo `outputs/runs/<run-id>/`. El directorio contiene
`run_manifest.json` y seis bundles hijos intactos bajo:

```text
components/full_pipeline/<bundle>
components/hampel_benchmark/<bundle>                 # o diagnostic_unavailable
components/rf_annual_deltas/<bundle>
components/agricultural_collection/<bundle>
components/agricultural_persistence/<bundle>
components/post_change_attribution/<bundle>
report_assets/index.json
report_assets/report_dataset.json
report_assets/figures/main/*.png
report_assets/figures/events/<event-id>/*.png
report_assets/data/main/*.{json,csv}
report_assets/annex/spatial/*.geojson
report_assets/annex/tables/*.csv
report_assets/annex/methodology/*.json
report_assets/annex/provenance/*_component_status.json
```

`report_assets/` es un paquete editorial versionado y no un nuevo bundle
científico. La política `2.0.0` reúne copias byte a byte de las figuras y datos
más útiles sin obligar a navegar cada componente. `figures/main/` contiene la
secuencia general; `figures/events/<event-id>/` agrupa perturbación, agricultura,
persistencia y conjunción del mismo evento; `data/main/` conserva los JSON y CSV
que alimentarán texto, indicadores y tablas; `annex/` separa geometrías, tablas
extensas y metadatos metodológicos. Los originales permanecen intactos y son la
fuente autoritativa.

Las fichas priorizan todos los eventos `conversion_likely` y después los
`review_required` que alcanzan el umbral de área, hasta completar diez fichas;
si atribución no está disponible se usan los cinco eventos de perturbación de
mayor superficie. El inventario completo siempre queda disponible en los JSON,
CSV y GeoJSON seleccionados, aunque un evento no tenga ficha gráfica.

`report_assets/report_dataset.json` expone una interfaz normalizada para el
renderer PDF y un futuro DOCX: identificación, estado, métricas principales,
eventos seleccionados, estado de componentes, limitaciones y rutas internas. No emite
una conclusión legal ni duplica series raster. `report_assets/index.json`
registra para cada copia categoría, componente, motivo, evento cuando aplica,
ruta y SHA-256 del original, manifest hijo y ruta/hash de destino. También
hashea el dataset normalizado y conserva el estado de componentes ausentes.
Por eso un run `partial` o `.failed` puede reunir de forma auditable lo producido
antes del fallo, sin fabricar evidencia para componentes sin soporte.

Cuando un componente no puede publicar un bundle porque queda
`diagnostic_unavailable`, `failed` o `skipped`, su carpeta contiene
`component_status.json`. Este recibo no finge ser un bundle científico:
registra estado, timestamps, dependencias y un código de error sanitizado. El
manifest superior referencia y hashea el recibo para que una carpeta sin
productos científicos no quede inexplicablemente vacía.

El
manifest superior registra hashes, coberturas diferentes, dependencias y
estado; no mezcla ni reinterpreta los contratos científicos de los hijos.
Antes de publicar `complete`, el orquestador verifica todos los tamaños y
SHA-256 declarados por cada manifest hijo, además de impedir rutas absolutas,
traversal, duplicados y escapes mediante enlaces. El `analysis_id` superior
correlaciona el expediente; cada bundle hijo conserva y declara su propio ID.
Los componentes se invocan como funciones Python: `subprocess` se usa solamente
para registrar revisión y estado Git, nunca para ejecutar el procesamiento.

El flujo es secuencial y *fail-fast* para sus componentes científicos
obligatorios. Hampel es un diagnóstico lateral: si no encuentra suficientes
controles estrictamente estables queda `diagnostic_unavailable`, RF y
atribución continúan, y el expediente se publica normalmente con estado
`partial` y un warning científico. Cualquier otro error de Hampel sigue siendo
bloqueante. Los fallos bloqueantes se conservan en un directorio terminado en
`.failed`, con error sanitizado y sin credenciales. No hay reintentos automáticos.

Este comando produce un expediente técnico de detección, atribución
conservadora y revisión. No emite certificación ni confirma automáticamente
deforestación.

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
- ejecutar un smoke remoto completo de los seis componentes; ya se validó de
  forma acotada el primer evento/mes Dynamic World contra un bundle científico
  preservado, pero no la cadena completa. El intento real costa-uru del
  `2026-08-13` completó HLS, Hampel y RF, pero el collector antiguo planificó
  2.686 consultas evento-mes frente al máximo operativo de 500. La reparación
  posterior agrupa esas salidas en 55 adquisiciones mensuales y su micro-smoke
  real de 58 eventos × un mes aprobó; todavía falta repetir la cadena completa;
- sumar una segunda fuente temporal para medir desacuerdo cross-source, hoy
  declarado explícitamente como no evaluado;
- validar la persistencia y la integración agrícola sobre casos reales sin perder
  `unknown/review_required` cuando el soporte sea insuficiente;
- integrar capas de incendio, sequía e inundación para reducir resultados
  `unknown` de la atribución v1;
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


# Lanzar proyecto

cd C:\Users\Facu\Desktop\deforestaciones-EUDR
uv run python scripts/run_local_stack.py
