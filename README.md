# Pipeline de evidencia geoespacial EUDR

Pipeline reproducible para generar evidencia técnica sobre cambios de cobertura
forestal posteriores al 31 de diciembre de 2020 en establecimientos ganaderos de
Argentina.

El sistema **no certifica cumplimiento EUDR** y no confirma deforestación de
forma automática. Produce candidatos, eventos probables, métricas, mapas,
manifiestos y un informe para revisión humana.

## Estado actual

Checkpoint verificado: **2 de septiembre de 2026**.

- Pipeline científico integrado de siete componentes.
- API local asíncrona `0.4.0`, worker `0.3.0` y cliente web `0.5.0`.
- Informe PDF determinístico mediante `deforestation-reporting 0.14.0`.
- Dos ejecuciones remotas históricas cerraron el flujo anterior de seis
  componentes. No validan el dominio RF-first `2.0.0` ni occurrence agrícola
  `2.0.0`.
- Gate Python actual: 740 pruebas, 90,40 % de cobertura; Ruff, formato y Mypy
  sobre 163 archivos, sin errores ni warnings.
- Gate modular: jobs 10/94,14 %, API 19/90,62 % y worker 47/90,97 %.
- Cliente: 32 pruebas Vitest y typecheck estricto, con Node compatible.
- Mojones Norte fue regenerado desde cero con el flujo RF-first y occurrence
  `2.1.0`: 22 de 139 candidatos superaron el gate agrícola y 6 quedaron como
  `conversion_likely`; el informe incorpora contexto anual Dynamic World y
  resumen subumbral consultable en CSV.
- No hay contenedores ni despliegue AWS todavía.

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

## Operación local recomendada

El supervisor inicia exactamente una API, un worker y Vite con almacenamiento
compartido:

```powershell
uv run python scripts/run_local_stack.py
```

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
