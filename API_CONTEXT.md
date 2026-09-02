# Contexto de API, worker y cliente web

Actualizado: **28 de agosto de 2026**.

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

## 9. Operación

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

Gate completo del árbol actual, ejecutado el 28 de agosto de 2026:

- raíz: 737 pruebas, 90,39 % de cobertura;
- Ruff, formato y Mypy: 153 archivos, sin errores ni warnings;
- jobs: 10 pruebas, 94,14 %;
- API: 19 pruebas, 90,62 %;
- worker: 47 pruebas, 90,97 %;
- web: 32 pruebas + typecheck;
- integración multiproceso sin GEE incluida en el gate raíz;
- cero warnings.

Los runs GEE históricos preservan el flujo anterior de seis componentes. El
smoke RF-first de Mojones Norte completó el flujo nuevo de siete componentes;
esto valida la integración remota del caso, no exactitud temática generalizable.

No se ejecutó build de frontend ni de contenedor.

## 12. Evolución pendiente

1. ampliar la regresión científica con casos independientes al smoke Mojones Norte;
2. contenerizar API y worker por separado;
3. definir volúmenes privados, healthchecks y límites en Compose;
4. ejecutar smoke local con Compose;
5. sustituir SQLite por cola/estado administrado sólo cuando el modelo de
   concurrencia esté definido;
6. migrar objetos privados a almacenamiento compatible con S3;
7. añadir identidad y autorización;
8. desplegar gradualmente en AWS;
9. medir costo, timeout, reintentos y backpressure.

El orden y los gates están en [`NEXT_STEPS.md`](NEXT_STEPS.md).
