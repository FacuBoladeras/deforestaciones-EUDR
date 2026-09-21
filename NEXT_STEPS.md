# Próximos pasos

Actualizado: **21 de septiembre de 2026**.

Este archivo es el único backlog canónico. No acumula historia cerrada.

## Estado de entrada

Ya existe:

- pipeline científico de siete componentes, incluido el dominio candidato RF-first;
- publicación atómica;
- API/worker/web locales;
- PDF y ZIP verificados;
- dos runs remotos recientes `complete`;
- gate actual verde: 760 pruebas raíz, 90,40 % de cobertura; Ruff, formato y
  Mypy sobre 171 archivos; jobs 10, API 20, worker 51 y web 34 + typecheck;
- cero warnings en el gate;
- perfiles `lean/debug`.

La deuda dominante ya no es “hacer funcionar el run único”. Es ejecutar el
dominio RF-first integrado contra GEE y verificar que soporte robusto, CCDC y
occurrence agrícola discriminen los candidatos sin promociones indebidas.

## P0 — Regresión científica y reporte del flujo nuevo

### Avance implementado

- Occurrence agrícola `2.1.0`: la observación comienza el `2021-01-01`; un mes
  con `crops` en al menos 5 % del candidato satisface el gate agrícola y la
  potencia ordinal queda como `weak`, `moderate` o `strong`.
- Atribución `7.0.0` exige el gate porcentual explícito y reconoce el subconjunto
  forestal automático dentro de candidatos basales mixtos.
- Dominio candidato RF-first `2.0.0`: segmenta pérdida RF terminalmente
  persistente, asocia robusto/CCDC como soporte y conserva robust-only en un
  inventario sombra de revisión.
- Disturbance evidence `1.3.0` preserva el estado robusto previo a convergencia.
- Reporting `0.15.0` representa occurrence v2, cobertura/umbral y ausencia como
  no evaluada; el dominio candidato RF-first es el fallback cuando attribution
  no está disponible. El informe cliente y el anexo técnico se publican como
  PDF separados, con una ficha de candidato por página.
- El informe conserva una serie anual categórica Dynamic World con la paleta
  oficial y agrega los candidatos `<= 0,5 ha` en una tabla, con detalle en CSV.
- El transporte agrícola tipado por chunks conserva el presupuesto de 24 MB y
  la superficie local `float64`.
- El rerun GEE integral de Mojones Norte produjo 139 candidatos RF-first; 14 son
  estrictamente mayores a 0,5 ha y 125 quedan preservados como subumbrales.
- La reatribución local 2.1/7.0 sobre los rasters canónicos encontró 20
  candidatos con cobertura agrícola >=5 % y promovió 5 eventos, con 161,2770289
  ha conjuntivas. Los 183 + 31 artefactos regenerados verificaron tamaño y hash.
- El rerun integral posterior con collector `1.5.0` completó 7/7 componentes:
  22 de 139 candidatos superaron el gate, 6 quedaron como
  `conversion_likely` y sumaron 181,2347766 ha conjuntivas. Se verificaron
  tamaño y SHA-256 de 2.081 artefactos sin errores.

### Objetivo

Validar el caso Mojones Norte y fixtures controlados, cerrar los contratos
editoriales y ampliar la validación después del smoke GEE acotado ya completado.

### Alcance

1. comparar el Mojones Norte regenerado contra referencia independiente y
   revisar inventario, geometrías, fuentes y áreas;
2. comprobar parches robust-only, RF-only, superpuestos y baseline review;
3. verificar que CCDC aporta soporte/fecha y nunca veto;
4. verificar occurrence de uno, dos y tres o más meses y sus áreas por potencia;
5. conservar raster de occurrence, potencia y conjunción, junto con aliases
   históricos necesarios para compatibilidad;
6. comprobar que componentes ausentes producen `null`/“no evaluado”, no cero;
7. comparar el smoke GEE completado contra casos de referencia independientes;
8. mantener verde el gate completo y registrar bytes y requests del smoke.

### No negociable

- una occurrence agrícola satisface sólo ese gate; no certifica conversión;
- RF-only nunca produce promoción automática;
- no usar CCDC como veto ni tratar fuentes HLS como independientes;
- no truncar inventario por `maximum_events_per_batch`;
- no perder hashes, grillas, áreas ni procedencia;
- no elevar límites de descarga a ciegas;
- no convertir ausencia de raster o componente en ausencia de evidencia.

### Gate

- gate local actual: 760 pruebas raíz, 90,40 %; Ruff, formato y Mypy sobre 171
  archivos; jobs 10/94,14 %, API 20/90,97 %, worker 51/90,13 %, web 34 más
  typecheck y cero warnings;
- tests de contratos, fusión, occurrence, reporting parcial y paridad raster;
- Ruff, formato, Mypy y cobertura >=90 %;
- revisión explícita de diferencias contra el expediente anterior: la semántica
  cambió y no corresponde exigir candidatos idénticos a ciegas;
- reducción estimada de bytes y requests registrada; medición real pendiente;
- smoke remoto acotado;
- documentación actualizada.

## P1 — Contenerización separada

### Preparación implementada

- `DEFORESTATION_RUNTIME_ROOT` desacopla `configs/` y `data/` del layout del
  paquete Python instalado;
- `DEFORESTATION_RF_MODEL_ARTIFACT` y `--model-artifact` permiten montar el
  joblib canónico fuera de la imagen, conservando la verificación contra el
  registry, tamaño, SHA-256 y metadata;
- API, worker y los módulos científicos que consumen recursos comparten la
  misma resolución explícita del runtime root;
- el web dispone de un build productivo tipado hacia `dist/`, sin sourcemaps y
  con manifest para inventario y publicación del artefacto estático.
- `Dockerfile.api` define una imagen multi-stage API-only, locked y no editable,
  con bases fijadas por digest, usuario no root, healthcheck y estado privado
  externo;
- `.dockerignore` impide enviar credenciales, outputs, caches, AOI GeoJSON y
  modelos binarios al daemon; sólo admite el límite jurisdiccional público.
- `Dockerfile.worker` instala el stack científico separado de la API, exige
  credenciales y joblib como mounts externos, ejecuta como no root y conserva
  estado privado y runs en rutas distintas;
- `deforestation-worker --healthcheck` valida recursos y mounts sin contactar
  GEE ni cargar el modelo, y exige una revisión de código inyectada.
- `scripts/container_release_gate.py` prepara build por SHA/plataforma,
  procedencia máxima, attestation SBOM, metadata, inspección no-root, SBOM SPDX,
  SARIF y bloqueo ante CVE high/critical; su modo seguro sólo imprime el plan y
  la ejecución rechaza un checkout sucio o una revisión distinta de `HEAD`.

### Objetivo

Crear imágenes independientes para API y worker sin acoplar la API al stack GEE.

### Entregables

- Dockerfile API;
- Dockerfile worker;
- usuario no root;
- healthcheck;
- variables y secretos externos;
- límites explícitos de CPU, memoria y disco;
- `.dockerignore` que excluya outputs, credenciales, caches y datasets privados;
- documentación de versiones base y actualización.

### Gate

- no incluir `credentials.json`, `.env.local`, `outputs/` ni geometrías reales;
- API sin dependencias científicas pesadas;
- worker con dependencias GEE/reporting;
- escaneo de imagen y SBOM;
- no afirmar reproducibilidad hasta ejecutar el smoke Compose.

### Pendiente de ejecución

- runner local preparado: Docker Desktop `4.89.0`, Engine `29.7.2`, Buildx
  `0.36.1`, BuildKit `0.32.2`, Scout `1.24.0` y containerd image store;
- el gate real descartó Bookworm por vulnerabilidades heredadas; con Trixie la
  API pasó y el worker quedó bloqueado por CVE-2026-69247 en
  `cryptography==49.0.0`;
- el lock ya usa `cryptography==50.0.1` y la raíz impone
  `cryptography>=50.0.1,<51` como restricción transitiva;
- repetir el gate con `--execute` y revisar tamaño, wheels, procedencia, SBOM y
  vulnerabilidades de ambas imágenes sobre la nueva revisión;
- no promover una imagen mientras exista una CVE high/critical sin una decisión
  de riesgo explícita y versionada.

## P2 — Smoke local con Compose

### Preparación implementada

- `compose.yaml` define una API y un worker mononodo, con build args de revisión,
  dependencia por healthcheck y API publicada sólo en loopback;
- un bind privado común usa UID/GID `10001`, rechaza paths inexistentes y
  mantiene credenciales/modelo como mounts read-only específicos del worker;
- root filesystem read-only, capabilities vacías, `no-new-privileges`, init,
  tmpfs, logs rotados y límites parametrizables de CPU, memoria y PIDs;
- `compose.env.example` documenta paths y límites iniciales sin contener
  secretos;
- `docker compose config --quiet` aprueba con Compose `2.24.5`; no se ejecutó
  build, `up` ni smoke.

### Objetivo

Validar el flujo HTTP -> job -> worker -> ciencia -> PDF/ZIP con volúmenes
privados.

### Casos

- job exitoso;
- Hampel `diagnostic_unavailable`;
- fallo reintentable de transporte;
- cancelación;
- reinicio de worker;
- expiración;
- disco insuficiente;
- concurrencia limitada;
- lectura después de eliminar el workspace científico cuando la copia curada ya
  fue publicada.

### Gate

- hashes y ETags verificados;
- ningún secreto en logs;
- recuperación y fencing probados;
- límites documentados;
- cleanup seguro y medido.

## P3 — Despliegue AWS gradual

### Diseño mínimo antes de implementar

- API detrás de identidad y TLS;
- cola administrada;
- workers con concurrencia controlada;
- S3 privado con cifrado;
- estado durable;
- observabilidad;
- dead-letter queue;
- presupuestos y alarmas;
- política de retención y borrado;
- aislamiento por owner.

No desplegar un SQLite compartido ni un filesystem local como arquitectura
multiinstancia.

## P4 — Validación científica

Puede avanzar en paralelo cuando haya datos autorizados:

1. referencia independiente;
2. casos positivos y negativos por ecorregión;
3. bosque abierto;
4. incendios, sequías, inundaciones y recuperación;
5. error de comisión/omisión, F1, balanced accuracy e IoU;
6. error de área y fecha de cambio;
7. calibración de scores;
8. Sentinel-2 a 10 m;
9. Sentinel-1 como evidencia independiente.

## Mantenimiento continuo

- conservar `data/licenses.yml`;
- versionar contratos;
- mantener schemas históricos;
- revisar límites de GEE;
- ejecutar cleanup sólo con manifests y publicación verificadas;
- mantener un único run científico canónico completo y los modelos liberados;
- actualizar:
  - `PROJECT_CONTEXT.md` cuando cambie arquitectura o estado;
  - `SCIENCE_CONTEXT.md` cuando cambie método o semántica;
  - `API_CONTEXT.md` cuando cambie integración;
  - este archivo cuando cambie prioridad;
  - `AGENTS.md` sólo cuando cambien reglas para contribuidores.

## Definición de terminado del siguiente incremento

Un incremento no está terminado hasta que:

- contrato y compatibilidad están definidos;
- pruebas reproducen el caso;
- implementación está tipada;
- outputs conservan linaje;
- Ruff, formato, Mypy y tests pasan;
- no se ejecutó ningún build prohibido;
- no se expusieron datos sensibles;
- documentación canónica quedó sincronizada;
- un smoke remoto se ejecutó sólo si fue autorizado y acotado.
