# Contexto de API, worker y cliente web

Actualizado: **25 de septiembre de 2026**.

## 1. Objetivo

Exponer el pipeline científico como un servicio interno asíncrono sin duplicar
reglas ni ejecutar GEE dentro del request HTTP.

La API administra solicitudes y sirve resultados. El worker ejecuta
`run_complete_analysis()`, verifica contratos, genera el PDF y publica
artefactos privados.

## 2. Arquitectura local

```text
React/MapLibre
    |
FastAPI 0.4.0
    |-- validación geoespacial liviana (packages/domain)
    |-- SQLite (packages/jobs)
    |-- almacenamiento privado
    |
worker 0.4.0
    |-- lease + heartbeat + cancelación
    |-- deforestation_pipeline
    |-- deforestation_reporting
    |-- publicación curada + ZIP
    |
outputs/local-stack/
    |-- private/jobs.sqlite3
    |-- private/objects/
    |-- runs/
    |-- logs/
```

`packages/domain` evita que la API instale Earth Engine y el stack raster
completo.

## 3. Contrato HTTP vigente

| Método | Ruta | Función |
| --- | --- | --- |
| `GET` | `/health` | versión y readiness HTTP |
| `POST` | `/api/v1/geometries/validate` | valida geometría y estima área |
| `POST` | `/api/v1/analyses` | crea job idempotente |
| `GET` | `/api/v1/analyses/{id}` | estado, etapa y progreso observable |
| `POST` | `/api/v1/analyses/{id}/cancel` | cancelación best-effort |
| `GET` | `/api/v1/analyses/{id}/report` | resumen JSON |
| `GET` | `/api/v1/analyses/{id}/report.pdf` | informe PDF verificado |
| `GET` | `/api/v1/analyses/{id}/candidates` | perturbaciones candidatas |
| `GET` | `/api/v1/analyses/{id}/events` | sólo eventos probables |
| `GET` | `/api/v1/analyses/{id}/assets` | allowlist paginada |
| `GET` | `/api/v1/analyses/{id}/assets/{asset_id}` | asset verificado |
| `GET` | `/api/v1/analyses/{id}/download` | ZIP determinístico |

Crear análisis exige `Idempotency-Key: <uuid>`. Reutilizar la clave con otro
payload devuelve conflicto.

## 4. Validación de entrada

La API acepta una geometría por establecimiento:

- `Polygon` o `MultiPolygon`;
- GeoJSON canónico;
- jurisdicción argentina;
- máximo 50.000 vértices;
- máximo 2.000.000 bytes por request;
- área métrica mediante el paquete de dominio;
- reparación y warnings explícitos.

El cliente puede normalizar una `FeatureCollection` sólo cuando contiene una
única feature poligonal. La decisión final de validez siempre pertenece a la
API.

## 5. Job y concurrencia

Estados principales:

```text
queued
-> running
-> completed | partial | failed | cancelled
```

El repositorio SQLite implementa:

- idempotencia por owner y clave;
- claim condicional;
- intento monotónico;
- lease por worker;
- heartbeat;
- fencing de intentos viejos;
- recuperación después de caída;
- cancelación;
- expiración.

Debe operar **exactamente un worker local** sobre la SQLite compartida. El
launcher lo garantiza; no levantar en paralelo procesos manuales que apunten al
mismo estado.

## 6. Ejecución y publicación

El worker:

1. carga el GeoJSON privado y verifica SHA-256;
2. ejecuta el pipeline científico de siete componentes, incluida la fusión
   conservadora de candidatos robustos + RF;
3. registra el resultado científico antes del postproceso;
4. reanuda desde ese punto si falla PDF/publicación/ZIP;
5. renderiza el PDF;
6. copia la allowlist curada a una generación inmutable;
7. crea un ZIP determinístico;
8. completa el job con hashes y estado científico.

Las operaciones de filesystem críticas tienen reintentos para bloqueos
transitorios de Windows. Los errores públicos usan códigos sanitizados; no
incluyen rutas, credenciales ni excepciones remotas completas.

Candidate fusion puede quedar `diagnostic_unavailable` cuando falta el estado
robusto histórico requerido. En ese caso el worker conserva el inventario
histórico, marca el run `partial` y no inventa una conclusión. Si attribution no
se completa, reporting usa el inventario fusionado disponible como fallback:
gates y usos quedan no evaluados, y las áreas desconocidas permanecen `null`, no
se convierten en cero.

## 7. Almacenamiento

Raíz por defecto de API manual:

```text
outputs/api/private/
  jobs.sqlite3
  objects/analyses/<uuid>/
```

Raíz del launcher:

```text
outputs/local-stack/
  private/jobs.sqlite3
  private/objects/analyses/<uuid>/
  runs/<run-id>/
  logs/<timestamp>/
```

La publicación privada contiene únicamente:

- `run_manifest.json`;
- `report_assets/index.json`;
- `report_assets/report_dataset.json`;
- archivos permitidos por el índice;
- GeoTIFFs anuales Dynamic World y CSV subumbral cuando estén declarados por la
  allowlist;
- PDF y metadatos;
- ZIP y metadatos.

Los bundles científicos completos permanecen en `runs/`. La copia privada es
suficiente para la API, pero no sustituye todos los intermedios de auditoría.

Retención por defecto: 30 días. El startup de API purga objetos privados
expirados. No elimina automáticamente workspaces científicos.

## 8. Cliente web `0.5.0`

Capacidades:

- carga o dibujo de geometría;
- selector de mapa base;
- validación;
- creación idempotente;
- polling;
- cancelación;
- reanudación mediante `?analysis=<uuid>`;
- resumen;
- candidatos y eventos en secciones separadas;
- assets y descargas;
- informe PDF.

El progreso es estimado, se limita al 92 % mientras el job está activo y sólo
llega al 100 % con `completed` o `partial`.

Requisitos:

- Node `>=20.19.0`;
- `.nvmrc`: `22.12.0`;
- pnpm `11.19.0`;
- `VITE_ARCGIS_API_KEY` restringida por origen y servicio.

Vite escucha en `127.0.0.1:5173` y proxya `/api` y `/health` a
`127.0.0.1:8000`; no se abre CORS para el flujo local.

El contrato productivo genera `apps/web/dist/` con assets hasheados bajo
`assets/`, target ES2022, sourcemaps deshabilitados y manifest de Vite en
`.vite/manifest.json`. El build conserva rutas `/api` same-origin; el proxy de
desarrollo no forma parte del artefacto. Todo valor `VITE_*`, incluida la clave
ArcGIS, queda expuesto al navegador y debe restringirse por origen y servicio.

## 9. Operación

### Contrato de recursos runtime

Los procesos admiten un bundle de recursos separado del paquete instalado:

- `DEFORESTATION_RUNTIME_ROOT`: raíz que contiene `configs/`, `data/` y,
  opcionalmente, `scripts/` para procedencia;
- `DEFORESTATION_RF_MODEL_ARTIFACT`: path explícito al joblib RF montado o
  descargado fuera del bundle.

El registry y la configuración permanecen versionados dentro del runtime root.
El joblib externo se acepta únicamente después de verificar tamaño, SHA-256 y
metadata contra ambos contratos. La ubicación física no se registra como
identidad del modelo. Las variables se resuelven al iniciar cada proceso; en
despliegue deben existir antes de importar la aplicación.

### Imagen API

`Dockerfile.api` materializa sólo el adaptador HTTP y sus dependencias internas
`deforestation-domain` y `deforestation-jobs`. Usa instalación no editable y
locked, copia al runtime únicamente el virtualenv y el límite jurisdiccional,
ejecuta Uvicorn con un proceso bajo UID/GID `10001` y comprueba `/health` con la
biblioteca estándar. No contiene GEE, pipeline científico, reporting, worker,
credenciales ni estado operacional.

Contrato de filesystem:

- `/opt/deforestation/runtime`: sólo lectura; recursos versionados incluidos;
- `/var/lib/deforestation/private`: volumen privado escribible para SQLite y
  objetos;
- `/tmp`: tmpfs acotado cuando el root filesystem sea read-only.

El intento actual usa `cgr.dev/chainguard/wolfi-base:latest` fijada por digest
de índice OCI en ambas imágenes. Builder y runtime instalan el mismo paquete
`python-3.12=3.12.14-r9` de Wolfi (glibc); la base sola pasó el escaneo remoto
high/critical, y se comprobó que el paquete está disponible, arranca Python
3.12.14 y admite directorios con UID/GID `10001`. El índice APK y sus
dependencias transitivas pueden cambiar entre builds: la versión directa y el
digest de base no fijan por sí solos todo el sistema. Sólo el build con SBOM y
escaneo finales permite validar este intento; falta comprobar los wheels
geoespaciales del worker. Para actualizar la base se debe inspeccionar el
manifest, cambiar tag y digest juntos, ejecutar los gates, generar SBOM,
escanear vulnerabilidades y recién entonces probar el smoke Compose.
Sobrescribir un `ARG` con una base no fijada invalida esa garantía. El
argumento obligatorio `VCS_REF` se propaga a
`DEFORESTATION_CODE_REVISION`; el manifest científico usa esa revisión cuando
Git no está presente en el runtime.

### Imagen worker

`Dockerfile.worker` instala desde el mismo lock el pipeline, domain, jobs,
reporting y worker, pero no copia el código de la API. El runtime incluye
configs, catálogos, licencias, schemas, registry RF y el script canónico usado
para procedencia. Credenciales GEE y joblib se exigen como mounts read-only; no
se hornean en ninguna capa.

Contrato de filesystem:

- `/opt/deforestation/runtime`: recursos científicos versionados, sólo lectura;
- `/opt/deforestation/models`: mount read-only del joblib verificado;
- `/run/secrets`: mount read-only de credenciales GEE;
- `/var/lib/deforestation/private`: SQLite y objetos compartidos con la API;
- `/var/lib/deforestation/runs`: staging y runs científicos persistentes;
- `/tmp`: tmpfs para caches de librerías sin home escribible.

El healthcheck del worker valida la presencia de los recursos, credenciales,
modelo, revisión inyectada y directorios escribibles sin contactar GEE ni
cargar el joblib. Esto es readiness local, no prueba conectividad, cuotas ni
salud de Earth Engine.

### Compose local

`compose.yaml` define exactamente una API y un worker para el smoke mononodo.
Comparten un bind privado en `/var/lib/deforestation` y la misma identidad
numérica `10001:10001`; esto evita permisos incompatibles sobre SQLite/WAL y los
objetos. No se debe escalar el worker mientras el backend sea SQLite y
filesystem local.

El worker recibe las credenciales mediante un secret de Compose y el joblib por
bind read-only con `create_host_path: false`. Ambos servicios usan root
filesystem read-only, `cap_drop: ALL`, `no-new-privileges`, init, logs rotados,
tmpfs y límites de CPU/memoria/PIDs. La API se publica sólo en loopback.

Los límites iniciales son deliberadamente configurables en
`compose.env.example`; todavía no son sizing validado. Compose no ofrece una
cuota portable para el bind persistente: la capacidad/cuota debe imponerse en
el filesystem host y verificarse durante los casos de disco insuficiente. Se
validó `docker compose config`, pero no se ejecutó build, `up` ni smoke.

### Gate de release de imágenes

`scripts/container_release_gate.py` formaliza el gate local previo al smoke. Su
modo por defecto sólo imprime un plan JSON y no accede al daemon. Para API y
worker fija plataforma y tag por SHA completo, fuerza pull de las bases, pide
procedencia BuildKit `mode=max` y attestation SBOM, captura metadata e inspecciona
que la revisión OCI, el usuario `10001:10001` y el healthcheck sobrevivan en la
imagen final.

Después genera un SBOM SPDX independiente y un reporte SARIF con Docker Scout.
El proceso falla si aparece cualquier CVE `high` o `critical`; no filtra sólo
vulnerabilidades corregibles porque eso ocultaría riesgo conocido sin parche.
Únicamente tras pasar todos los controles escribe `release-evidence.json` con
tamaño y SHA-256 de cada evidencia. Los artefactos quedan ignorados por Git.

La ejecución requiere Docker con containerd image store, Buildx `>=0.14` y
Scout `>=1.4`, y está protegida por `--execute`. Rechaza un checkout con cambios
sin commit o una revisión distinta de `HEAD`, evitando etiquetar contenido
mutable con una identidad Git falsa. El 3 de septiembre de 2026 el runner local
quedó en Docker Desktop `4.89.0`, Engine `29.7.2`, Buildx `0.36.1`, BuildKit
`0.32.2`, Scout `1.24.0` y containerd image store. El gate real sobre Bookworm
bloqueó vulnerabilidades heredadas de la base. Después de migrar a Trixie, la
API pasó sin hallazgos high/critical y el worker quedó bloqueado sólo por
CVE-2026-69247 en `cryptography==49.0.0`. El lock posterior fijó `50.0.1`.
El gate sobre `a66c24f1f0b2aef7c62e7be4ed59e0e17d45265e` construyó la API
pero Scout bloqueó 16 hallazgos high/critical en paquetes Debian de la base
Trixie fijada; dejó SBOM y SARIF, no produjo `release-evidence.json` ni llegó
al worker. El pin Trixie actualizado se verificó por índice OCI remoto: su
imagen base `linux/amd64` presenta todavía dos high sin parche declarado,
CVE-2026-82560 (Perl) y CVE-2026-85091 (zlib). Cambiar el digest reduce
hallazgos conocidos, pero no aprueba el gate ni demuestra el estado del worker.
Por eso se preparó el intento con Wolfi descrito arriba; sus imágenes finales
aún no se construyeron ni escanearon.

```powershell
uv run python scripts/run_local_stack.py
```

Background:

```powershell
uv run python scripts/run_local_stack.py --background
uv run python scripts/run_local_stack.py --stop
```

El launcher:

- valida puertos 8000 y 5173;
- selecciona un Node compatible;
- comparte entorno y estado;
- separa logs;
- confirma API, web y worker;
- detiene sólo sus propios procesos.

## 10. Seguridad

Estado actual: servicio local interno, sin autenticación de producción.

Antes de exponerlo en red se requieren:

- identidad y autorización por tenant/owner;
- TLS;
- almacenamiento privado externo;
- cola administrada;
- límites de concurrencia y cuotas;
- secretos gestionados;
- auditoría y observabilidad;
- URLs firmadas o proxy autorizado;
- política explícita de borrado y backups;
- aislamiento de datos por establecimiento.

## 11. Gates

Gate completo del árbol actual, ejecutado el 21 de septiembre de 2026:

- raíz: 760 pruebas, 90,40 % de cobertura;
- Ruff, formato y Mypy: 171 archivos, sin errores ni warnings;
- jobs: 10 pruebas, 94,14 %;
- API: 20 pruebas, 90,97 %;
- worker: 51 pruebas, 90,13 %;
- web: 34 pruebas + typecheck;
- integración multiproceso sin GEE incluida en el gate raíz;
- cero warnings.

Los runs GEE históricos preservan el flujo anterior de seis componentes. El
smoke RF-first de Mojones Norte completó el flujo nuevo de siete componentes;
esto valida la integración remota del caso, no exactitud temática generalizable.

El build de API de `a66c24f1f0b2aef7c62e7be4ed59e0e17d45265e` se detuvo en
el escaneo; no se ejecutó el build del worker ni el smoke Compose. La variante
Wolfi aún no fue construida ni escaneada como imagen de aplicación.

## 12. Evolución pendiente

1. ampliar la regresión científica con casos independientes al smoke Mojones Norte;
2. ejecutar el gate de imágenes en el runner local ya preparado;
3. ejecutar los casos del smoke local con Compose;
4. sustituir SQLite por cola/estado administrado sólo cuando el modelo de
   concurrencia esté definido;
5. migrar objetos privados a almacenamiento compatible con S3;
6. añadir identidad y autorización;
7. desplegar gradualmente en AWS;
8. medir costo, timeout, reintentos y backpressure.

El orden y los gates están en [`NEXT_STEPS.md`](NEXT_STEPS.md).
