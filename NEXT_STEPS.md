# Próximos pasos: evidencia agrícola post-cambio

Este archivo es el roadmap científico vigente. Parte de la auditoría del
`2026-08-10`: la detección de perturbaciones está avanzada y el bloque de
agricultura por cultivo ya se ejecuta desde el mismo run. La brecha siguiente
es validarlo con casos reales y ampliar evidencia independiente sin convertir
el MVP en un LULC nacional. El contrato y la evolución de API, worker, cliente
web, informe PDF y AWS viven en `API_CONTEXT.md`. El bloque PDF previo a
contenedores no modifica este orden científico: el bloque editorial ya está
implementado y validado contra el expediente real en una versión cliente de
quince páginas que combina GIS, estadísticas y evidencia, con índice, narrativa
y mapas orientados mediante geometría declarada verificada y contexto vectorial
OSM al 50 %. Su generación y publicación ya están integradas como postproceso
del worker, con descarga verificada desde API y cliente web. Antes de
contenerizar, el bloque operativo vigente optimiza la materialización remota:
el Incremento 1 ya consolidó transportes HLS/RF y agregó perfiles `lean/debug`;
el incremento de robustez ya estabilizó la detección y separó evidencia
candidata de métricas operativas; el Incremento 2 debe trasladar agregaciones agrícolas temporales a GEE y
descargar tablas más un conjunto raster mínimo.

El objetivo del siguiente bloque es cerrar esa brecha sin ampliar el MVP hacia
un LULC completo, otra familia de detectores o una certificación automática.

## 0. Precondición operativa del run único

Antes de evaluar evidencia agrícola mediante el orquestador:

- [x] hacer que el benchmark Hampel sea diagnóstico y no bloqueante cuando el
  AOI no tenga suficientes controles estables;
- [x] conservar la indisponibilidad científica como warning explícito, sin impedir RF ni
  atribución;
- [x] incorporar a `run_complete_analysis.py` la entrada versionada de evidencia
  agrícola y su política de linaje;
- [x] ejecutar un smoke real de los seis componentes y verificar publicación
  `complete` o una degradación `partial` científicamente justificada;
- [x] aislar y validar el primer request Dynamic World real: el transporte de
  EPSG:6933 usa WKT1_GDAL porque Earth Engine no interpreta el alias EPSG,
  conservando EPSG:6933 en la grilla y el GeoTIFF;
- [x] materializar `component_status.json` hasheado para componentes
  `diagnostic_unavailable`, `failed` o `skipped`, sin fabricar bundles;
- [x] recolectar las figuras principales manifestadas en
  `report_assets/figures/`, con copias byte a byte, orden estable e índice
  reversible; conservar también lo ya producido en runs `partial` o `.failed`;
- [x] estructurar `report_assets/` para informe: figuras generales y por evento,
  datos principales, anexos espaciales/tabulares/metodológicos y
  `report_dataset.json` normalizado, todo con hashes y sin mover los originales;
- [ ] mantener RF en 2020–2024 hasta disponer de un año adicional compatible.

**Criterio de salida:** un polígono llega hasta atribución aunque Hampel no
encuentre controles, y el manifest conserva esa limitación sin ocultarla.

**Intento real `2026-08-13`:** `costa-uru-mvp-smoke` completó pipeline
principal, Hampel y RF, y verificó 1.025 artefactos hijos sin faltantes ni
diferencias de tamaño o SHA-256. Produjo 58 eventos —20 de al menos 0,5 ha— y
133,92 ha de perturbaciones candidatas. El collector agrícola se detuvo antes
de consultar Dynamic World: las ventanas pos-onset requieren 2.686 consultas
evento-mes frente al presupuesto versionado de 500. Persistencia y atribución
quedaron correctamente `skipped`; el run es `partial`, pero **no** satisface el
criterio de salida porque no llegó a atribución. No elevar el presupuesto a
ciegas: el próximo incremento operativo debe agrupar o cachear consultas
mensuales compartidas entre eventos y conservar los mismos rasters/áreas por
evento. La colección `report_assets` también quedó `unavailable` en ese cierre
fallido, aunque la misma selección de 15 figuras funciona después sobre una
copia sellada del expediente; registrar el error secundario exacto antes de
repetir el smoke.

**Reparación operativa `2026-08-24`:** el collector dejó de descargar cada
mes por separado para cada candidato. La configuración `1.2.0` agrupa por
ventana, descarga una grilla mensual común y divide únicamente la evaluación
de conteos de escenas en lotes determinísticos de hasta 100 candidatos. El
límite ya no rechaza inventarios mayores ni elimina subumbrales. Una prueba
local sin GEE cubre 122 candidatos en lotes 100 + 22, con dos meses, dos
descargas compartidas y los 244 rasters candidato-mes derivados localmente.
Para costa-uru, el plan baja de 2.686
productos evento-mes a **55 adquisiciones mensuales** (`48,84×` de reúso); la
grilla común es 438 × 286, 125.268 píxeles, bajo el tope versionado de
1.000.000. Se mantienen los 2.686 rasters evento-mes, footprints, áreas,
`nodata`, hashes y contratos downstream. Un micro-smoke real sobre noviembre
de 2025 contó seis escenas en cada uno de los 58 eventos y descargó un GeoTIFF
de cuatro bandas válido, 517.630 bytes, sin persistir URL. Aún falta repetir
el run completo de `costa-prueba` para cerrar el criterio de salida remoto.

**Optimización lean `2026-08-20` — Incremento 1 cerrado:** la configuración
`1.12.0` usa `output.artifact_profile: lean` por defecto. HLS reúne sus cinco
productos en un transporte multibanda efímero y RF reduce el caso normal de 18
a seis transportes para 2020–2024 más P0; ambos reconstruyen localmente los
mismos TIFF científicos y conservan fallback ante el límite de descarga
directa. Agricultura deja de persistir el raster mensual `_shared` en `lean`,
pero mantiene los raster evento-mes que requiere el gate actual. `debug`
conserva el transporte compartido. No se cambió la ciencia ni se ejecutó un
nuevo smoke remoto.

**Prueba real grande `2026-08-20`:** `plantaciones-uru.geojson` contiene un
Polygon válido de 74.949,64 ha. Su grilla HLS a 30 m mide 1.224 × 922
(1.128.528 píxeles) y las 24 estaciones 2020–2025 representarían 1,68 GB sin
comprimir frente al presupuesto de serie de 256 MB. Además, el producto de ocho
índices por período estima 36,11 MB frente al límite directo de 32 MB. El
preflight detuvo correctamente el run antes de descargar rasters con
`direct_download_limit_exceeded`. Este caso confirma que el Incremento 2 debe
evitar materializar el cubo estacional completo para AOI grandes; no corresponde
elevar los límites ni alterar la geometría para forzar la corrida.

**Prueba real pequeña `2026-08-21`:** `prueba-pequeña.geojson` cubre 1.198,06
ha y completó los seis componentes científicos bajo el perfil `lean`.
`report_assets` reunió 53 archivos y 26 figuras, y permitió renderizar y revisar
un PDF cliente de 37 páginas. RF activó correctamente el fallback individual
ante `download_url_failed`. El único fallo ocurrió al renombrar atómicamente el
directorio final por un bloqueo transitorio de Windows (`WinError 5`), después
de completar ciencia y reporte; el orquestador ahora usa el helper de rename
con reintentos tanto para publicación final como fallida. El expediente
original permanece en `.staging` con el sobre de infraestructura para conservar
la evidencia del incidente. Falta repetir el run para cerrar el gate operativo.

**Robustez espacial, estadística y semántica `2026-08-21` — cerrada localmente:** la
configuración `1.13.0` evita amplificar MAD locales casi nulos mediante un piso
cuantílico espacial por estación e índice, sin rellenar MAD nulos ni suavizar la
reflectancia. La colección técnica `1.1.0` conserva todos los componentes
persistentes como candidatos auditables y usa 0,5 ha únicamente como evidencia
de área defendible. La atribución `4.0.0` separa el inventario completo de
`candidates` y reserva `events` a conversiones probables que satisfacen todas
las compuertas conjuntivas. Los
píxeles del borde evaluable se marcan con `EDGE_PIXEL`, pero no se filtran. El
PDF distingue candidatos de eventos probables, incluso al leer datasets
editoriales previos. Esto elimina la sobreestimación semántica sin esconder
salpicado, sin atribuir causas y sin fingir independencia
entre el detector robusto y CCDC, que comparten HLS.

**Smoke real de robustez `2026-08-21`:** `nativo.geojson` completó los seis
componentes como `nativo-robustness-retry`. Frente al run anterior del mismo
archivo, mantuvo 34 componentes candidatos y 15 de un píxel, redujo el área
candidata de 10,35 a 9,81 ha y el subconjunto por área de 8 a 6 candidatos
(6,39 a 4,95 ha). Ninguno reunió persistencia más uso agropecuario posterior,
por lo que el resultado semántico esperado es cero eventos probables. Es
evidencia de funcionamiento, no calibración temática. Un
intento previo agotó reintentos HTTP 5xx sobre `rf_transport_2021.tif`; el
fallback RF multibanda cubre ahora `remote_server_error` además de límite y
fallo de URL.

El `2026-08-17` se cerró por separado el plumbing local de producto con un gate
multiproceso sintético: API HTTP, SQLite, worker, `report_assets`, assets y ZIP.
No consulta GEE y por lo tanto **no marca** el checkbox del smoke científico.
El preflight del caso real `prueba-viale.geojson` confirmó un Polygon único de
1.438,42 ha, válido y sin reparación; el cliente normaliza su wrapper
`FeatureCollection` de una sola feature. El siguiente run remoto puede entrar
por la web sin cambiar el contrato científico.

El smoke web real `c938a1c0-ca33-42a4-94b3-81c7276a3ef8` cerró la precondición:
terminó `partial` únicamente porque Hampel quedó `diagnostic_unavailable`, una
degradación prevista y no bloqueante. Pipeline principal, RF, colección
agrícola, persistencia y atribución completaron; el expediente expone cuatro
eventos y 41 assets hasheados. El endurecimiento del `2026-08-18` publica esa
selección curada en almacenamiento privado durable antes de completar el job,
sin convertirla en una conclusión legal ni alterar el bundle científico.

El cierre fallido ahora conserva además `error_type` y código sanitizado del
recolector de `report_assets`; no persiste el mensaje arbitrario de una
excepción secundaria.

## 1. Contrato v1 de evidencia agrícola

La afirmación mínima `{land_use, source, independent}` fue reemplazada por un
contrato Pydantic y JSON Schema versionado. Cada observación debe conservar:

- `evidence_id` y `event_id`;
- clase observada: `crop`, `pasture` o `livestock_infrastructure`;
- proveedor, dataset, colección y versión;
- licencia, atribución, restricciones y fecha de acceso;
- ventana temporal post-cambio;
- geometría fuente, CRS, resolución y método de remuestreo;
- fracción del evento cubierta y superficie intersectada;
- persistencia temporal y cantidad de observaciones válidas;
- calidad, semántica de confianza, flags y motivos de descarte;
- SHA-256 del artefacto y del descriptor de fuente;
- evaluación derivada contra el entrenamiento/validación del RF y política de
  independencia aplicada.

El campo `independent` ya no es aceptado como verdad desde el CLI. La política
versionada `configs/agricultural-evidence.yml` conoce el linaje del RF, deja
MapBiomas/JRC/ESA/Hansen como corroboración no independiente y produce niveles
`ineligible`, `unknown`, `methodological` o `full` con códigos de motivo.

**Criterio de salida:** inputs incompletos, sin licencia, sin fecha o sin
geometría quedan rechazados; la independencia se calcula, no se declara.

## 2. Registro y selección mínima de fuentes

No construir un LULC nacional. Registrar primero un conjunto pequeño de fuentes
aplicables a Entre Ríos y clasificarlas por rol:

1. **evidencia candidata independiente:** producto o relevamiento no utilizado
   en pseudolabels, entrenamiento ni ajuste del RF;
2. **corroboración no independiente:** fuente útil que participó del dataset de
   entrenamiento;
3. **contexto declarado:** uso informado por el usuario o establecimiento;
4. **referencia humana o de campo:** interpretación documentada, dron o visita,
   con responsable y fecha.

Para cada fuente elegida:

- [x] agregar catálogo y licencia;
- [x] fijar versión o fecha de acceso;
- [x] documentar clases agrícolas relevantes y sus limitaciones;
- [x] comprobar y registrar cobertura temporal posterior al evento;
- [x] definir cómo se convierte a la grilla métrica del evento;
- [x] probar nodata y bordes; el desacuerdo entre fuentes queda para la
  persistencia e integración multifuente.

MapBiomas y cualquier otro producto usado en los pseudolabels del RF pueden
corroborar, pero **no** deben cerrar por sí solos el gate independiente.

Dynamic World V1 queda registrado como candidato `methodological` a 10 m y
MapBiomas Argentina Collection 2 como corroboración. Sólo `crops` de Dynamic
World puede aportar soporte agrícola automático en este incremento, combinando
la etiqueta 4 con un umbral versionado de probabilidad. `grass` no se traduce
automáticamente a pastura ganadera y `built` no demuestra finalidad pecuaria.
La ruta GEE tiene tests con dobles, pero el smoke remoto real todavía está
pendiente.

**Criterio de salida:** al menos una fuente candidata independiente y una fuente
corroborativa están registradas con licencia y procedencia reproducible.

## 3. Recolector espacial por evento

Implementar un componente pequeño que reciba el bundle de eventos y consulte
sólo el AOI y las ventanas necesarias. Debe:

- recortar cada fuente a la geometría del evento;
- conservar la intersección raster/vector, no sólo una etiqueta;
- calcular hectáreas y fracción del evento por clase;
- separar nodata de `unknown`;
- registrar grilla, CRS, resolución, resampling y error de área;
- generar tabla larga por `event_id`, fecha, fuente y clase;
- escribir rasters o vectores derivados únicamente cuando aporten auditoría;
- renderizar una lámina mensual acotada por evento sin ocultar la serie completa;
- evitar mosaicos provinciales o nacionales innecesarios.

Salida mínima:

- `agricultural_evidence.csv`;
- `agricultural_evidence.geojson`;
- `agricultural_evidence.json`;
- `agricultural_evidence_metadata.json`;
- manifest con hashes y referencias a los bundles fuente.

**Criterio de salida:** cada porcentaje atribuido puede reconstruirse desde un
artefacto espacial y ningún píxel fuera del soporte válido suma superficie.

**Estado implementado:** `agricultural_collector.py` consume el bundle de
eventos, consulta ventanas mensuales posteriores al onset y publica los cuatro
artefactos mínimos, rasters fuente, footprint fraccional exacto y manifest
atómico. La grilla es EPSG:6933 a 10 m, las áreas se ponderan por la
intersección exacta evento-píxel y `crop`, `unknown` y `nodata` cierran el área
del evento. La persistencia permanece deliberadamente en `false`: pertenece al
incremento 4 y no se infiere de una observación mensual cruda.
Para `getDownloadURL`, sólo la representación remota del CRS se traduce a
WKT1_GDAL; el contrato local, el cálculo de área y el GeoTIFF siguen declarando
EPSG:6933.
Cada evento con observación válida publica además un único PNG mensual: serie
completa y como máximo cuatro ventanas seleccionadas mediante una regla fija
registrada en el manifest. `nodata` y cero observado usan símbolos distintos;
los eventos completamente sin datos no fabrican mapas.

## 4. Persistencia post-cambio

Una única clase posterior no demuestra conversión. Para cada evento:

- [x] iniciar la ventana después del onset estimado;
- [x] exigir más de un período válido y una duración mínima versionada;
- [x] conservar cultivos estacionales sin confundir barbecho con abandono;
- [x] distinguir recuperación forestal posterior en el atribuidor RF;
- [x] marcar insuficiencia cuando la serie termina demasiado cerca del evento;
- [x] conservar desacuerdos entre fechas y declarar el desacuerdo entre fuentes
  como no evaluado cuando sólo existe una fuente temporal;
- [x] no interpolar intervalos largos y conservar gaps explícitos.

Los umbrales de cobertura y persistencia deben permanecer en configuración y no
elegirse observando el resultado del caso de prueba.

**Criterio de salida:** la evidencia agrícola reporta período, soporte,
persistencia y sensibilidad al umbral, no una clase instantánea.

**Estado implementado:** `agricultural_persistence.py` publica contrato y
schema v1, CSV, JSON de evidencia habilitable y raster de soporte persistente.
La regla primaria exige tres períodos válidos, dos calificantes distribuidos
en al menos 180 días y no exige consecutividad ciega. La duración y los gaps se
miden por píxel con soporte válido; la extensión del calendario solicitado no
reemplaza observaciones y un gap mayor al máximo configurado no habilita el
gate. Reporta sensibilidad `0,40/0,50/0,60`, desacuerdo temporal e insuficiencia. No existe todavía
un smoke GEE real ni una segunda fuente temporal para medir desacuerdo
cross-source.
Los eventos con soporte persistente positivo publican una lámina que combina
mapa, soporte temporal y sensibilidad. El GeoTIFF permanece como evidencia
cuantitativa canónica y el PNG sólo agrega presentación auditable.

## 5. Integración con el atribuidor v1

Extender el atribuidor sin cambiar sus reglas conservadoras. Un evento sólo
puede alcanzar `conversion_likely` cuando convergen:

1. bosque al 31/12/2020;
2. pérdida posterior al corte;
3. persistencia de la pérdida;
4. evidencia agrícola o ganadera posterior espacialmente coincidente;
5. superficie atribuida defendible;
6. al menos una fuente independiente según la política versionada;
7. ausencia de recuperación o explicación alternativa fuerte.

Ante falta de datos, conflicto o evidencia exclusivamente declarada/no
independiente, conservar `unknown` y `review_required`. El modelo automático
nunca produce `conversion_confirmed`.

Agregar al bundle:

- evidencia por evento y fuente;
- área agrícola intersectada;
- `likely_conversion_area_ha` sin doble conteo;
- razones a favor y en contra;
- mapa de eventos atribuidos;
- referencia reversible a los artefactos de detección y agricultura.

**Criterio de salida:** ninguna hectárea candidata se convierte en hectárea
atribuida sin superar todos los gates y conservar su procedencia.

**Estado implementado:** el atribuidor consume el bundle persistente, aplica la
política de linaje y materializa por evento la conjunción en grilla agrícola de
bosque 2020, pérdida poscorte, no-bosque persistente y soporte agrícola. El
resultado conserva área agrícola, `likely_conversion_area_ha`, razones a favor
y en contra, raster conjuntivo y hashes reversibles. El linaje verifica el
`analysis_id`, el input y el hash exacto de `disturbance_events.geojson`, además
de la coherencia entre el área declarada y el raster. Por defecto, el run único
genera y consume recolección y persistencia dentro del mismo parent; los modos
precomputados aceptan un bundle sellado y rechazan mezclarlo con un JSON
agrícola manual.
La conjunción espacial positiva publica un PNG por evento con hash y referencia
reversible a su TIFF. Una conjunción de área cero no genera un mapa vacío y el
disclaimer mantiene explícito que `conversion_likely` no equivale a
confirmación ni certificación.
Faltantes, ineligibilidad, recuperación o intersección vacía conservan
`review_required` y área probable cero; `conversion_confirmed` permanece falso.

## 6. Casos de validación del incremento

Usar casos con roles distintos, no sólo un AOI conveniente:

- **costa-uru:** control de plantación/cosecha; debe evitar falsos positivos de
  conversión agrícola;
- **Viale:** candidato con eventos persistentes para recolectar evidencia, sin
  asumir de antemano que sean agricultura;
- **caso positivo independiente:** pérdida de bosque seguida de cultivo o
  pastura documentada;
- **caso sin cambio:** control de estabilidad;
- **caso alternativo:** incendio, inundación, sequía o recuperación cuando
  exista referencia suficiente.

Medir como mínimo:

- precision, recall y F1 de la atribución cuando haya referencia;
- error de comisión y omisión;
- IoU y error de superficie atribuida;
- error temporal del inicio del uso posterior;
- cobertura y adjudicabilidad por fuente;
- sensibilidad a resolución, bordes y umbrales;
- tasa de `unknown/review_required`.

**Criterio de salida:** resultados separados por caso y fuente, sin usar el
producto evaluado como su propia verdad de referencia.

## 7. Definición de terminado del bloque agrícola

El incremento termina cuando:

- [x] el contrato y schema son versionados y exportables;
- [x] las fuentes y licencias están registradas;
- [x] la independencia se deriva mediante política reproducible;
- [x] la evidencia espacial/temporal cruda se materializa por evento;
- [x] el atribuidor consume evidencia generada por el mismo run único;
- [x] Hampel diagnóstico no impide llegar a atribución;
- [ ] costa-uru sigue sin falso positivo de conversión;
- [ ] existe al menos un caso positivo y uno negativo con referencia externa;
- [x] las áreas atribuidas se calculan en CRS métrico sobre una conjunción
  materializada sin sumar etiquetas;
- [x] manifests, hashes, parámetros y quality flags permiten reproducir el run;
- [x] Ruff, formato, Mypy y Pytest aprueban con cobertura total `>= 90 %`;
- [x] el lenguaje final permanece en evidencia y revisión, nunca certificación.

## Fuera de este bloque

- GTB y ensamble VISEC dos de tres;
- espacio mensual completo y año julio–junio;
- Sentinel-2 principal a 10 m;
- Sentinel-1, clima e incendios como integración regional completa;
- API pública, lotes masivos y trazabilidad animal;
- `conversion_confirmed` automático o certificación EUDR.

Esos incrementos sólo se retoman después de demostrar que la evidencia agrícola
por evento funciona de extremo a extremo.
