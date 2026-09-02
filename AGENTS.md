# AGENTS.md

Reglas obligatorias para trabajar en este repositorio.

## 1. Antes de cambiar código

1. Leer este archivo.
2. Leer [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md).
3. Si el cambio toca ciencia, leer [`SCIENCE_CONTEXT.md`](SCIENCE_CONTEXT.md).
4. Si toca API, worker o web, leer [`API_CONTEXT.md`](API_CONTEXT.md).
5. Revisar [`NEXT_STEPS.md`](NEXT_STEPS.md).
6. Inspeccionar Git y preservar cambios no relacionados.
7. Verificar afirmaciones contra código, contratos o documentación oficial.

No aceptar una premisa técnica sin comprobarla. Si algo es incierto, investigar
antes de afirmarlo.

## 2. Reglas de trabajo

- Nunca agregar `Co-Authored-By` ni atribución de IA.
- Usar commits convencionales.
- **Nunca ejecutar builds** de frontend, paquetes o contenedores.
- Sí ejecutar lint, formato en modo check, typecheck y tests relevantes.
- Si es imprescindible hacer una pregunta, detenerse y esperar la respuesta.
- No ampliar alcance sin instrucción explícita.
- Preferir cambios pequeños, tipados, testeables y reversibles.
- Agregar una prueba por cada bug corregido.
- No ocultar parámetros en notebooks o constantes dispersas.
- No borrar modelos ni runs canónicos sin copia verificada.
- No inventar acceso a GEE comercial, RENSPA, SENASA o datos privados.

## 3. Fuentes canónicas

| Tema | Archivo |
| --- | --- |
| Contexto general y checkpoint | `PROJECT_CONTEXT.md` |
| Ciencia, semántica y presentación | `SCIENCE_CONTEXT.md` |
| API, worker, web y cloud | `API_CONTEXT.md` |
| Prioridades | `NEXT_STEPS.md` |
| Uso rápido | `README.md` |
| Parámetros | `configs/*.yml` |
| Contratos | `data/schemas/*.json` |
| Datos y licencias | `data/catalog.yml`, `data/licenses.yml` |

No crear bitácoras Markdown por incremento. Actualizar el documento dueño del
tema. No duplicar una misma verdad en varios archivos salvo un resumen con
enlace.

## 4. Propósito y alcance

El repositorio genera evidencia geoespacial para debida diligencia EUDR de
cadenas bovinas en Argentina.

Incluye:

- ingesta y validación geométrica;
- bosque 2020;
- series temporales;
- detección de perturbaciones;
- ocurrencia agrícola post-evento y atribución;
- áreas, incertidumbre y QA;
- bundles, mapas, PDF y ZIP;
- API interna, worker y cliente local.

No incluye:

- certificación, DDS o conclusión legal;
- TRACES/EUDR Information System;
- trazabilidad animal;
- integraciones SENASA/RENSPA sin autorización;
- ERP, CRM, pagos o portal público;
- decisión negativa automática.

## 5. Invariantes EUDR y científicos

- Fecha de corte: `2020-12-31`.
- Intercambio: WGS 84 con al menos seis decimales.
- Medir áreas en CRS apropiado; no decidir sólo por píxeles.
- El umbral de bosque de 0,5 ha no es un mínimo legal de conversión.
- La referencia operativa de evento usa área conjuntiva estrictamente `>0.5 ha`.
- Conservar candidatos subumbrales.
- Separar detección de atribución.
- Una anomalía no es un evento.
- Recuperación o perturbación temporal no es conversión.
- Una fuente global no es verdad de terreno.
- CCDC y detector robusto comparten HLS; no son sensores independientes. En el
  dominio RF-first aportan soporte o fecha, nunca semilla, expansión ni veto.
- El RF temporal sólo tiene alcance 2020-2024. Puede proponer candidatos de
  pérdida persistente, pero no es calibrado; un candidato sólo RF requiere
  revisión y nunca se promociona automáticamente.
- Dynamic World es la única fuente agrícola temporal operativa actual.
- La observación agrícola comienza el `2021-01-01`; no se recorta por el onset
  RF estimado.
- Una ocurrencia mensual donde `crops` cubre al menos el 5 % del footprint
  completo del candidato satisface únicamente el gate operativo de evidencia
  agrícola. No certifica ni confirma deforestación.
- La potencia agrícola es ordinal, no probabilística: `weak` para un mes que
  supera el gate del 5 %, `moderate` para dos y `strong` para tres o más. La
  suficiencia del seguimiento se informa como calidad y no veta una ocurrencia
  válida.
- En `lean`, agricultura conserva un cubo temporal `uint16` de conteos por
  candidato y hasta cuatro rasters mensuales para presentación; `debug`
  conserva el detalle mensual completo.
- El transporte agrícola usa chunks temporales por grupo espacial: conteos
  `uint16` y probabilidad `float32` en requests separados bajo 24 MB estimados.
- No usar `reduceRegion`/`reduceRegions` como superficie agrícola autoritativa;
  el footprint local Shapely `float64` es parte del contrato científico.
- Ausencia, error o componente no disponible significa **no evaluado**, nunca
  evidencia negativa ni área cero inferida.
- El modelo automático nunca emite `conversion_confirmed`.
- Ambigüedad o soporte insuficiente debe permanecer auditable y, cuando
  corresponda, ir a `review_required`.

Lenguaje:

- “sin deforestación detectada”, no “certificado libre de deforestación”;
- “candidato” o “alerta de cambio”, no “deforestación” antes de atribuir;
- “evidencia compatible con conversión”, no conclusión legal;
- informar período, fuentes, resolución y limitaciones.

## 6. Fronteras de arquitectura

- `src/deforestation_pipeline`: ciencia; no depende de FastAPI, React o AWS.
- `packages/domain`: geometría y área compartidas, sin GEE.
- `packages/jobs`: estado y persistencia de jobs.
- `packages/reporting`: presentación determinística desde contratos verificados.
- `services/api`: adaptador HTTP liviano.
- `services/worker`: único ejecutor del pipeline.
- `apps/web`: cliente; no reimplementa reglas geoespaciales.
- `scripts/run_complete_analysis.py`: orquestador científico canónico.
- `scripts/run_local_stack.py`: launcher local canónico.

La API debe invocar `run_complete_analysis()` mediante el worker. No duplicar
reglas científicas en endpoints.

## 7. Contratos y reproducibilidad

Toda ejecución debe registrar:

- versión o commit;
- configuración;
- AOI y hash;
- datasets, versiones, licencia y acceso;
- período;
- filtros, índices y variables;
- modelo y semilla;
- umbrales;
- CRS y método de área;
- ambiente y dependencias;
- hashes de artefactos.

Reglas:

- versionar cualquier cambio incompatible;
- preservar schemas históricos publicados;
- validar tamaño, SHA-256 y rutas antes de publicar;
- prohibir traversal, rutas absolutas y symlinks fuera de raíz;
- publicar atómicamente;
- no mezclar originales científicos con copias editoriales;
- marcar colecciones remotas mutables mediante fecha o versión de acceso.

## 8. Seguridad y privacidad

- Polígonos productivos e identificadores son sensibles.
- Usar fixtures sintéticos o anonimizados.
- No registrar tokens, claves, URLs firmadas ni payloads privados.
- No versionar `credentials.json`, `.env.local`, `outputs/`, `output/` o `tmp/`.
- No publicar coordenadas reales sin autorización.
- Separar datos públicos, privados y derivados.
- Antes de red: identidad, autorización, TLS, cifrado, auditoría y cuotas.

## 9. Outputs y limpieza

Clasificación:

1. **Versionado y necesario:** código, tests, configs, schemas, catálogos,
   licencias, muestras sintéticas y referencia VISEC.
2. **Regenerable:** `.venv`, `node_modules`, caches, coberturas, `tmp`, PDFs de
   QA. Puede eliminarse.
3. **Operacional privado:** SQLite y objetos curados. Respetar retención.
4. **Científico no recuperable:** modelos y runs GEE. Preservar sólo los
   canónicos, con manifests y hashes verificados.

El índice local `outputs/canonical_artifacts.json` declara qué artefactos están
protegidos. Está ignorado por Git porque puede contener identificadores
operativos.

Antes de borrar un run:

- confirmar que no está activo;
- leer `run_manifest.json`;
- verificar publicación privada si se necesita para API;
- comprobar si es el único ejemplar de un caso científico;
- registrar qué run queda como canónico;
- no borrar credenciales como parte de una limpieza genérica.

## 10. Estructura

```text
.
├── README.md
├── PROJECT_CONTEXT.md
├── SCIENCE_CONTEXT.md
├── API_CONTEXT.md
├── NEXT_STEPS.md
├── AGENTS.md
├── apps/web/
├── packages/{domain,jobs,reporting}/
├── services/{api,worker}/
├── src/deforestation_pipeline/
├── scripts/
├── configs/
├── data/{boundaries,models,samples,schemas}/
├── tests/
├── pyproject.toml
└── uv.lock
```

## 11. Toolchain

- Python 3.12.
- `uv`.
- pytest, Ruff y Mypy.
- Node 22.12 recomendado; mínimo web 20.19.
- pnpm 11.19.
- GEE Python API.

No agregar dependencias pesadas sin justificación medible.

## 12. Gate obligatorio

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts packages services
uv run pytest

uv run pytest -c packages/jobs/pyproject.toml packages/jobs/tests
uv run pytest -c services/api/pyproject.toml services/api/tests
uv run pytest -c services/worker/pyproject.toml services/worker/tests

cd apps/web
pnpm test
pnpm typecheck
```

No usar un smoke manual como sustituto del gate. No ejecutar GEE remoto sin
estimar AOI, bytes, requests, cuotas y almacenamiento.

## 13. Checkpoint vigente

- Fecha: 1 de septiembre de 2026.
- Configuración: `1.13.0`, perfil `lean`.
- Collector agrícola: configuración `1.5.0`, bundle `1.7.0`, contrato `1.1.0`.
- Evidencia agrícola / ocurrencia: `2.1.0` / `2.1.0`.
- Detección / evidencia de perturbación: `1.5.0` / `1.3.0`.
- Dominio candidato RF-first / bundle: `2.0.0` / `2.0.0`.
- Orquestador / atribución: `2.7.0` / `7.0.0`.
- Reporting: `0.15.0`, editorial `3.0.0` para occurrence v2.
- Report assets / selección / dataset: `2.5.0` / `2.4.0` / `2.1.0`.
- API/web/worker: `0.4.0` / `0.5.0` / `0.4.0`.
- Gate del incremento científico: 740 pruebas raíz, 90,40 % de cobertura;
  Ruff, formato y Mypy sobre 163 archivos, sin errores ni warnings.
- El rerun GEE RF-first de Mojones Norte completó los siete componentes y seis
  composiciones Dynamic World anuales; 22 de 139 candidatos superaron el gate
  agrícola y 6 quedaron como `conversion_likely`.
- Próxima prioridad: ampliar la regresión científica con casos controlados y
  comparar explícitamente omisiones y comisiones contra referencia independiente.
- Después: contenedores separados, smoke Compose y AWS gradual.

Consultar los demás documentos para el detalle; no expandir este checkpoint con
historia acumulativa.
