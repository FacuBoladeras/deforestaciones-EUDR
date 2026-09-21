# Contexto general del proyecto

Actualizado: **21 de septiembre de 2026**.

## 1. Propósito

Construir un pipeline geoespacial reproducible que genere evidencia técnica para
la debida diligencia de cadenas bovinas frente al Reglamento (UE) 2023/1115.

El producto analiza una unidad productiva, conserva el linaje de datos y entrega
resultados auditables. No es un certificador, una DDS, un sistema de trazabilidad
animal ni una integración con sistemas de la Unión Europea.

## 2. Resultado esperado por establecimiento

1. validar la geometría declarada;
2. estimar el dominio forestal a la fecha de corte `2020-12-31`;
3. detectar perturbaciones posteriores;
4. evaluar persistencia o recuperación de la perturbación;
5. detectar ocurrencia agrícola post-evento y atribuir el uso posterior;
6. cuantificar área, período, calidad e incertidumbre;
7. publicar datos, mapas, manifiestos, PDF y ZIP para revisión.

Estados públicos posibles:

- `no_change_detected`;
- `low_risk`;
- `review_required`;
- `conversion_likely`;
- `insufficient_data`.

`conversion_confirmed` queda reservado a revisión humana documentada.

## 3. Arquitectura vigente

```text
apps/web               React, TypeScript, MapLibre, React Query
services/api           FastAPI; validación, jobs y lectura de resultados
services/worker        ejecución, leases, publicación, PDF y ZIP
packages/domain        geometría y superficie sin stack GEE
packages/jobs          contrato de jobs y repositorio SQLite
packages/reporting     contrato editorial y renderer PDF
src/deforestation_pipeline
                       núcleo científico y bundles auditables
scripts                CLIs operativas y de mantenimiento científico
configs                parámetros versionados
data                   catálogos, licencias, esquemas y muestras sintéticas
tests                  gates científicos e integración multiproceso
```

### Fronteras obligatorias

- FastAPI es un adaptador y llama a `run_complete_analysis()`.
- La API no instala ni reimplementa el stack científico.
- El worker es el único proceso que ejecuta un job.
- El navegador no decide validez geoespacial.
- `report_assets` cura una vista; no reemplaza los bundles científicos.
- El PDF presenta evidencia; no altera métricas ni reglas de dominio.
- La imagen API se define separada del worker y no contiene el stack científico;
  su build y escaneo reales ya se ejecutaron, mientras que el smoke aún está
  pendiente.
- La imagen worker contiene el stack científico pero no la API; credenciales y
  modelo RF son mounts obligatorios y los runs viven fuera de la imagen.
- Compose conecta una API y un worker mononodo mediante almacenamiento privado
  común, identidad numérica compartida y límites operativos parametrizables;
  todavía no se ejecutó el smoke.
- El gate de imágenes ya ejecutó build por SHA, procedencia, SBOM SPDX, SARIF y
  bloqueo high/critical. Bookworm quedó descartado por vulnerabilidades de base;
  con Trixie la API pasó sin hallazgos high/critical y el worker quedó bloqueado
  únicamente por `cryptography==49.0.0`. El lock y la restricción de resolución
  ya exigen `cryptography>=50.0.1,<51`; falta repetir el gate sobre ese commit.

## 4. Versiones autoritativas

| Componente/contrato | Versión |
| --- | --- |
| Pipeline Python | `0.1.0` |
| Configuración principal | `1.13.0` |
| Orquestador completo | `2.7.0` |
| Bundle local | `3.1.0` |
| Detección de perturbaciones | `1.5.0` |
| Evidencia de perturbación | `1.3.0` |
| Episodios candidatos | `1.2.0` |
| RF temporal / bundle | `1.2.0` / `3.5.0` |
| Dominio candidato RF-first / bundle | `2.0.0` / `2.0.0` |
| Collector agrícola / bundle | `1.5.0` / `1.7.0` |
| Evidencia agrícola / occurrence | `2.1.0` / `2.1.0` |
| Atribución post-cambio | `7.0.0` |
| `report_assets` / política de selección | `2.5.0` / `2.4.0` |
| `report_dataset` | `2.1.0` |
| Reporting / contrato editorial | `0.15.0` / `3.0.0` para occurrence v2 |
| Plantilla narrativa PDF | `1.1.0` |
| API | `0.4.0` |
| Worker | `0.4.0` |
| Web | `0.5.0` |
| Domain / jobs | `0.1.0` / `0.1.0` |

Los esquemas históricos en `data/schemas/` son contratos versionados, no basura.
No se eliminan por falta de referencias directas.

## 5. Checkpoint verificado

### Calidad de código

Gate completo del árbol actual, ejecutado el 21 de septiembre de 2026:

- Ruff check: aprobado.
- Ruff format: 171 archivos conformes.
- Mypy: 171 archivos, sin errores.
- Pytest raíz: 760 pruebas, 90,40 % de cobertura.
- Jobs: 10 pruebas, 94,14 %.
- API: 20 pruebas, 90,97 %.
- Worker: 51 pruebas, 90,13 %.
- Web: 34 pruebas y typecheck estricto.
- Warnings: cero.

El primer intento web con Node 18 falló por incompatibilidad del runtime. Con
Node 24 del runtime local —compatible con el requisito `>=20.19.0`— ambos gates
aprobaron. El proyecto fija Node 22.12.0 en `.nvmrc`.

### Operación real

El stack local asíncrono ya procesa solicitudes HTTP hasta PDF y ZIP. Dos runs
remotos históricos completaron el flujo anterior de seis componentes y la
publicación. No validan el flujo actual de siete componentes, la fusión de
candidatos ni occurrence v2. Otros runs parciales preservaron evidencia y
códigos sanitizados.

Esto demuestra funcionamiento local, no estabilidad cloud ni exactitud temática
generalizable.

El rerun GEE RF-first de Mojones Norte con collector `1.5.0`, occurrence `2.1.0`
y attribution `7.0.0` completó desde cero los siete componentes y las seis
composiciones anuales Dynamic World 2020-2025. Produjo 139 candidatos; 22
superaron el gate agrícola del 5 % y 6 cumplieron además todos los gates
automáticos, con 181,2347766 ha de intersección conjuntiva. Sus 2.081 artefactos
declarados verificaron tamaño y SHA-256 sin errores.

### Footprint local después de la auditoría

- Código, contratos y documentación no ignorados: 230 archivos, 5,65 MB.
- Artefactos ignorados preservados: 399,62 MB.
- Composición preservada: un run científico completo, objetos privados aún
  dentro de retención y el modelo RF liberado.
- Eliminado: 2.010,04 MB de entornos, dependencias instaladas, caches, QA
  temporal y workspaces anteriores ya publicados de forma privada.

Los valores son un checkpoint local, no una cuota contractual.

## 6. Alcance incluido

- GEE, HLS, índices espectrales, CCDC y detección robusta.
- Línea base forestal 2020 por convergencia de evidencia.
- RF multianual 2020-2024 como fuente no calibrada capaz de proponer candidatos
  conservadores para revisión.
- Dynamic World mensual, occurrence agrícola y atribución post-cambio.
- Raster/vector, áreas métricas, hashes y procedencia.
- API interna, worker local, cliente web e informe PDF.
- Casos sintéticos, pruebas unitarias e integración sin GEE.

## 7. Fuera de alcance

- Certificación legal o DDS.
- TRACES/EUDR Information System.
- SIGSA, SIGICA, SIGCER, DT-e, ARCA, REPSAL o INAI.
- Trazabilidad individual de animales.
- Legalidad fiscal, laboral, dominial o indígena.
- Decisión negativa automática.
- Portal público, pagos, ERP o CRM.
- Acceso a RENSPA/SENASA sin autorización.

## 8. Datos, privacidad y retención

Polígonos productivos, identificadores, credenciales y expedientes son
sensibles.

- Sólo ejemplos sintéticos o anonimizados entran en Git.
- `credentials.json`, `.env*`, `outputs/`, `output/` y `tmp/` están ignorados.
- La API usa almacenamiento privado y retención por defecto de 30 días.
- El arranque de API purga objetos privados expirados; no borra bundles
  científicos.
- Los runs científicos canónicos y modelos no tienen respaldo en Git.
- `outputs/canonical_artifacts.json` identifica localmente el único run
  científico completo preservado y los modelos liberados.
- Un cleanup debe comprobar estado, manifest, hashes y publicación antes de
  borrar un workspace.

## 9. Decisiones vigentes

1. **Lenguaje prudente.** “Candidato”, “alerta” y “evidencia compatible”; nunca
   “certificado”.
2. **Detección separada de atribución.** Una señal no es conversión.
3. **Área métrica.** No decidir por conteo de píxeles.
4. **Intermedios auditables.** No conservar sólo el resultado binario.
5. **Configuración versionada.** Nada crítico oculto en notebooks.
6. **Sin acoplamiento web-ciencia.**
7. **No borrar contratos históricos.**
8. **Documentación por responsabilidad.** Este archivo no absorbe ciencia,
   endpoints ni backlog.
9. **Ausencia no es cero.** Componentes fallidos o no disponibles permanecen
   como no evaluados en ciencia, reporting y API.

## 10. Fuentes de verdad

- Método y semántica: [`SCIENCE_CONTEXT.md`](SCIENCE_CONTEXT.md).
- API y operación: [`API_CONTEXT.md`](API_CONTEXT.md).
- Prioridades: [`NEXT_STEPS.md`](NEXT_STEPS.md).
- Reglas de trabajo: [`AGENTS.md`](AGENTS.md).
- Parámetros ejecutables: `configs/*.yml`.
- Contratos: `data/schemas/*.json`.
- Licencias: `data/licenses.yml`.
