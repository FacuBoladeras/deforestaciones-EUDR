# Pipeline de evidencia geoespacial EUDR

Pipeline reproducible para generar evidencia técnica sobre cambios de cobertura
forestal posteriores al 31 de diciembre de 2020 en establecimientos ganaderos de
Argentina.

El sistema **no certifica cumplimiento EUDR** y no confirma deforestación de
forma automática. Produce candidatos, eventos probables, métricas, mapas,
manifiestos y un informe para revisión humana.

## Estado actual

Checkpoint verificado: **3 de septiembre de 2026**.

- Pipeline científico integrado de siete componentes.
- API local asíncrona `0.4.0`, worker `0.4.0` y cliente web `0.5.0`.
- Informe y anexo PDF determinísticos mediante `deforestation-reporting 0.15.0`;
  la plantilla narrativa `1.1.0` incorpora carátula ejecutiva, índice y
  navegación jerárquica de tres niveles, y un marco conceptual EUDR/VISEC
  común a todos los expedientes.
- Dos ejecuciones remotas históricas cerraron el flujo anterior de seis
  componentes. No validan el dominio RF-first `2.0.0` ni occurrence agrícola
  `2.0.0`.
- Gate Python actual: 760 pruebas, 90,41 % de cobertura; Ruff, formato y Mypy
  sobre 169 archivos, sin errores ni warnings.
- Gate modular: jobs 10/94,14 %, API 20/90,97 % y worker 51/90,13 %.
- Cliente: 34 pruebas Vitest y typecheck estricto, con Node compatible.
- Mojones Norte fue regenerado desde cero con el flujo RF-first y occurrence
  `2.1.0`: 22 de 139 candidatos superaron el gate agrícola y 6 quedaron como
  `conversion_likely`; el informe incorpora contexto anual Dynamic World y
  resumen subumbral consultable en CSV.
- Existen contratos separados de imagen API/worker y Compose mononodo, todavía
  sin build ni smoke; el despliegue AWS permanece pendiente.

Las versiones, límites y decisiones actuales se detallan en
[`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md).

## Documentación canónica

| Documento | Responsabilidad |
| --- | --- |
| [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) | Contexto general, arquitectura, estado y decisiones vigentes |
| [`SCIENCE_CONTEXT.md`](SCIENCE_CONTEXT.md) | Método científico, datos, semántica, productos y límites |
| [`API_CONTEXT.md`](API_CONTEXT.md) | API, jobs, worker, cliente web, almacenamiento y evolución cloud |
| [`NEXT_STEPS.md`](NEXT_STEPS.md) | Backlog priorizado y criterios de terminado |
| [`AGENTS.md`](AGENTS.md) | Reglas operativas para agentes y contribuidores |
| [`PROTOCOLO-VISEC-CARNE-.pdf`](PROTOCOLO-VISEC-CARNE-.pdf) | Referencia metodológica suministrada; no es código del proyecto |

No usar el historial Git ni nombres de commits como documentación funcional.

## Arquitectura

```text
GeoJSON
  -> FastAPI: validación + job SQLite
  -> worker
      -> run_complete_analysis()
          1. pipeline HLS y perturbaciones
          2. Hampel diagnóstico
          3. RF anual 2020-2024
          4. dominio candidato RF-first; robusto y CCDC como soporte
          5. Dynamic World en chunks tipados + cubo compacto en `lean`
          6. occurrence agrícola y potencia ordinal
          7. atribución post-cambio
      -> report_assets verificados
      -> PDF cliente
      -> copia privada curada + ZIP
  -> API: estado, candidatos, eventos, assets y descargas
  -> React/MapLibre
```

El adaptador HTTP nunca duplica ciencia. El núcleo no depende de FastAPI, React
ni AWS.

El transporte agrícola agrupa meses sobre grupos espaciales acotados y separa
conteos `uint16` y probabilidad `float32`. La fracción calificante se
reconstruye localmente y las áreas siguen usando el footprint exacto Shapely;
`reduceRegion`/`reduceRegions` no es fuente autoritativa de superficie.

## Requisitos

- Python `3.12`.
- `uv`.
- Node `22.12.0` recomendado por `.nvmrc`; el cliente acepta `>=20.19.0`.
- `pnpm 11.19.0`.
- Credenciales GEE locales sólo para ejecuciones remotas.

## Preparar el entorno

```powershell
uv sync --all-packages --group dev

cd apps/web
pnpm install --frozen-lockfile
cd ../..
```

`credentials.json` y `apps/web/.env.local` están ignorados por Git. Nunca deben
aparecer en logs, commits, bundles o documentación.

Para una instalación que no conserve el layout del checkout, definir
`DEFORESTATION_RUNTIME_ROOT` apuntando al bundle que contiene `configs/` y
`data/`. El modelo RF puede montarse por separado mediante
`DEFORESTATION_RF_MODEL_ARTIFACT`; también está disponible
`--model-artifact` en `scripts/run_complete_analysis.py`. El binario externo no
se confía por ubicación: se verifican tamaño, SHA-256 y metadata contra el
registry versionado antes de usarlo.

## Ejecutar el proyecto completo en local

Desde la raíz del repositorio, el comando canónico para levantar el proyecto
completo es:

```powershell
uv run python scripts/run_local_stack.py
```

El supervisor inicia exactamente una API, un worker y Vite con almacenamiento
compartido. La terminal queda ocupada y `Ctrl+C` detiene los tres procesos.

Modo desacoplado:

```powershell
uv run python scripts/run_local_stack.py --background
uv run python scripts/run_local_stack.py --stop
```

Servicios:

- Web: `http://127.0.0.1:5173`
- API: `http://127.0.0.1:8000`
- Health: `http://127.0.0.1:8000/health`

El launcher no libera puertos ni mata procesos ajenos.

Para el mapa web, copiar `apps/web/.env.example` a `apps/web/.env.local` y
configurar `VITE_ARCGIS_API_KEY`. Los mapas base son contexto visual, no
evidencia científica.

El artefacto web productivo se define mediante `pnpm build`: ejecuta primero el
typecheck y luego genera `apps/web/dist/`, con manifest, assets hasheados y sin
sourcemaps. La clave `VITE_ARCGIS_API_KEY` se incorpora al JavaScript cliente:
no es un secreto y debe restringirse por origen y servicios autorizados. El
artefacto mantiene `/api` como ruta same-origin para su futuro enrutamiento por
el frontend de despliegue.

## Contrato de imagen API

[`Dockerfile.api`](Dockerfile.api) define una imagen multi-stage exclusiva para
`deforestation-api`: instala desde `uv.lock` sólo API, domain y jobs; no copia el
pipeline científico, reporting ni el worker. El runtime usa UID/GID `10001`,
expone el healthcheck `/health`, incorpora únicamente el límite jurisdiccional
público y reserva `/var/lib/deforestation/private` para estado privado montado.

Las bases están fijadas por tag y digest multi-arquitectura: Python
`3.12-slim-trixie` —resuelto como `3.12.14` el 1 de septiembre de 2026— y uv
`0.10.12`. [`.dockerignore`](.dockerignore) excluye credenciales, variables
locales, outputs, caches, AOI GeoJSON y modelos binarios; sólo reingresa el
límite operacional versionado. Los secretos y `DEFORESTATION_CODE_REVISION`
no se hornean como archivos. El build exige `VCS_REF`; ambas imágenes lo
propagan como `DEFORESTATION_CODE_REVISION` para que la procedencia no dependa
de incluir Git dentro del runtime.

Ejemplo de ejecución futura, limitado a loopback mientras no existan identidad,
TLS y autorización:

```powershell
docker volume create deforestation-api-private
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m `
  --cpus 1 --memory 512m --pids-limit 200 `
  --mount type=volume,src=deforestation-api-private,dst=/var/lib/deforestation `
  --env DEFORESTATION_CODE_REVISION=<commit> `
  --publish 127.0.0.1:8000:8000 deforestation-api:<version>
```

[`Dockerfile.worker`](Dockerfile.worker) define por separado el runtime
científico completo: pipeline, jobs, reporting, GEE y librerías geoespaciales.
No incorpora API, credenciales ni el joblib RF. Ambos artefactos se montan
read-only en `/run/secrets/gee-credentials.json` y
`/opt/deforestation/models/random_forest_forest_multiyear_2020_2024.joblib`.
El worker escribe el estado compartido bajo `/var/lib/deforestation/private` y
los runs científicos bajo `/var/lib/deforestation/runs`.

El comando `deforestation-worker --healthcheck` comprueba recursos versionados,
artefactos externos y permisos de los mounts sin cargar el modelo ni contactar
GEE. También rechaza una revisión vacía, `unknown` o `working-tree`. El
contenedor ejecuta como UID/GID `10001`, usa `/tmp` para caches y espera un
filesystem raíz read-only. Ausencia de credenciales o del modelo significa
`unhealthy`, no evidencia negativa ni un worker parcialmente operativo.

No se construyeron ni escanearon las imágenes. Tamaño, disponibilidad de wheels
Linux, vulnerabilidades, CPU, memoria, cuota de disco y smoke quedan como gates
antes de promoverlas. El contrato automatizado de build, procedencia, SBOM y
escaneo se describe más abajo.

## Contrato Compose local

[`compose.yaml`](compose.yaml) conecta una API y un único worker mediante un
directorio privado compartido. Ambos usan UID/GID `10001` para que SQLite, WAL y
objetos conserven permisos coherentes entre contenedores. La API sólo publica
`127.0.0.1:8000`; el worker no expone puertos y espera que la API esté healthy.

El contrato aplica filesystem raíz read-only, capabilities vacías,
`no-new-privileges`, init, tmpfs acotados, rotación de logs y límites iniciales
parametrizables. Los defaults —API `0.5 CPU/512 MiB`, worker `4 CPU/8 GiB`— son
techos PROVISIONALES para el smoke, no sizing productivo medido.

Preparación:

```powershell
Copy-Item compose.env.example compose.env
New-Item -ItemType Directory -Force .\tmp\compose-private
# Reemplazar VCS_REF por el SHA completo y verificar paths de credenciales/modelo.
docker compose --env-file compose.env config --quiet
```

El bind privado usa `create_host_path: false`: un typo falla en vez de crear un
directorio vacío. Compose limita CPU, memoria, PIDs y tmpfs, pero no impone una
cuota portable al filesystem persistente. Para el smoke, ese directorio debe
vivir en un volumen con cuota/capacidad administrada por el host; luego se mide
el pico real antes de fijar almacenamiento en cloud.

## Gate local de release de imágenes

[`scripts/container_release_gate.py`](scripts/container_release_gate.py) arma un
plan determinístico para API y worker. El modo por defecto **no ejecuta Docker**:

```powershell
$revision = (git rev-parse HEAD).Trim()
uv run python scripts/container_release_gate.py --revision $revision
```

El plan usa tags con el SHA completo, `linux/amd64` por defecto, bases frescas,
procedencia `mode=max`, attestations SBOM, metadata BuildKit, inspección de la
configuración final y un SBOM SPDX independiente. Docker Scout bloquea la
promoción ante cualquier CVE `high` o `critical` y emite SARIF. Si TODO pasa,
el gate genera `artifacts/container-release/<revision>/release-evidence.json`
con tamaños y SHA-256 de las evidencias.

La ejecución real requiere Docker con containerd image store, Buildx `>=0.14`
y Docker Scout `>=1.4`; además exige `--execute` explícito, un checkout limpio
y que `--revision` coincida exactamente con `git rev-parse HEAD`:

```powershell
# Sólo operador o CI autorizado: construye y escanea ambas imágenes.
uv run python scripts/container_release_gate.py --revision $revision --execute
```

El runner local quedó preparado con Docker Desktop `4.89.0`, Engine `29.7.2`,
Buildx `0.36.1`, BuildKit `0.32.2`, Scout `1.24.0` y containerd image store. El
preflight pasó para el commit documentado, pero no se ejecutó `--execute` ni se
construyó ninguna imagen en este incremento.

## Ejecución científica directa

El antiguo launcher exploratorio `pruebas.py` fue eliminado. Los casos locales
sin GEE viven como fixtures y pruebas; el orquestador científico canónico es
`scripts/run_complete_analysis.py`.

Orquestación completa con GEE:

```powershell
uv run python scripts/run_complete_analysis.py .\establecimiento.geojson `
  --establishment-id establecimiento-anonimizado `
  --credentials .\credentials.json
```

También se admite `--gee-project <id>` para OAuth persistido. Es excluyente con
`--credentials`.

La salida se publica atómicamente bajo la raíz configurada en
`DEFORESTATION_ANALYSIS_OUTPUT_ROOT` o, por defecto, `outputs/`.

### Regenerar sólo el informe

Para rediseñar o verificar la presentación de un expediente ya cerrado sin
volver a ejecutar ciencia ni GEE:

```powershell
uv run python scripts/render_existing_report.py `
  --run-root .\outputs\runs\<run-id> `
  --input .\ruta\al\input.geojson `
  --output-dir .\output\pdf-redesign\<version>
```

La salida debe quedar fuera del run fuente y en un directorio nuevo. La red está
deshabilitada por defecto; el comando reutiliza el cache OSM disponible y genera
`render-metadata.json` con hashes de procedencia. `--allow-network-context`
habilita explícitamente la consulta de contexto cartográfico cuando sea necesaria.
El informe identifica la versión de plantilla y mantiene separados el marco
conceptual estable, los resultados dinámicos del expediente y la decisión humana.

## Verificación

No ejecutar builds en este repositorio. El gate vigente es:

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

Si el shell resuelve Node 18, los comandos web fallan aunque el código sea
correcto. Activar primero Node 22.12 o posterior.

## Datos, outputs y peso del workspace

Git contiene código, contratos, configuraciones, muestras sintéticas, límites
operativos y la referencia VISEC. No versiona:

- entornos (`.venv`, `node_modules`);
- caches y coberturas;
- `tmp/`, `output/` y `outputs/`;
- credenciales;
- geometrías o expedientes reales.

`outputs/` puede contener evidencia científica no reproducible sin volver a
consultar GEE. No se borra masivamente. La política es:

1. preservar modelos liberados y al menos un run científico canónico;
2. conservar resultados privados durante su retención operativa;
3. eliminar temporales, caches, PDFs de QA y workspaces no canónicos sólo
   después de verificar publicación e integridad;
4. no publicar coordenadas reales.

El índice local ignorado `outputs/canonical_artifacts.json` identifica los
artefactos preservados y el SHA-256 del manifest canónico.

## Límites principales

- La operación cloud todavía no está validada.
- El producto óptico operativo actual es HLS a 30 m; Sentinel-2 a 10 m sigue
  pendiente.
- El RF temporal 2020-2024 es no calibrado. Puede proponer pérdida persistente,
  pero RF-only requiere revisión y nunca produce promoción automática.
- CCDC y el detector robusto comparten HLS; no son independencia de sensor.
  CCDC aporta soporte o fecha dentro del candidato RF y nunca actúa como veto.
- Dynamic World es la única fuente temporal agrícola operativa.
- La observación agrícola comienza el `2021-01-01`. Una occurrence mensual en
  la que `crops` cubre al menos el 5 % del footprint completo del candidato
  satisface únicamente el gate de evidencia agrícola; no certifica ni confirma
  deforestación. La potencia es ordinal: débil con un mes, moderada con dos y
  fuerte con tres o más.
- En `lean`, occurrence consume un cubo `uint16` por candidato con los
  conteos válidos/calificantes de cada mes; sólo se retienen hasta cuatro
  rasters mensuales representativos para la figura. `debug` conserva todo.
- Un componente ausente se presenta como no evaluado y con valores desconocidos
  `null`, nunca como gate fallido o área cero inferida.
- Faltan validación temática independiente, calibración regional y evidencia
  SAR.
- El modelo automático nunca emite `conversion_confirmed`.

El roadmap único está en [`NEXT_STEPS.md`](NEXT_STEPS.md).
