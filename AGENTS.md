# AGENTS.md

## 1. Propósito del proyecto

Este repositorio desarrolla un pipeline geoespacial reproducible para producir
evidencia técnica que apoye la evaluación de cadenas de carne bovina libres de
deforestación en Argentina, en el marco del Reglamento (UE) 2023/1115 (EUDR).

El norte metodológico inicial es el Protocolo VISEC Carne Libre de Deforestación
Argentina. El proyecto debe emular sus componentes técnicamente sólidos y
superarlos en resolución espacial, trazabilidad de los datos, cuantificación de
incertidumbre, manejo de alertas y reproducibilidad.

El producto del proyecto no es una certificación legal. Es un sistema técnico
de análisis GIS y teledetección que genera datos, mapas, alertas y expedientes
de evidencia para que operadores, verificadores o certificadores realicen su
debida diligencia.

## 2. Objetivo actual

Construir y validar un pipeline que, para una unidad productiva o
establecimiento ganadero:

1. reciba y valide su geometría;
2. determine la cobertura de bosque existente al 31 de diciembre de 2020;
3. detecte perturbaciones de cobertura posteriores a esa fecha;
4. evalúe si una perturbación persistente es compatible con conversión de
   bosque a uso agrícola o ganadero;
5. cuantifique superficie, fecha, confianza e incertidumbre;
6. genere productos intermedios auditables;
7. produzca mapas y un paquete de evidencia reproducible.

El MVP termina en la generación de evidencia geoespacial por establecimiento.
No incluye todavía la gestión completa de trazabilidad animal ni la
presentación de declaraciones en sistemas de la Unión Europea.

### 2.1 Checkpoint operativo — 18 de agosto de 2026

- La CLI `scripts/run_complete_analysis.py` orquesta en un solo run: pipeline
  principal, Hampel diagnóstico, RF 2020–2024, recolección agrícola,
  persistencia y atribución post-cambio.
- El collector agrícola `1.1.0` comparte adquisiciones mensuales entre eventos
  y conserva rasters, grillas, áreas, `nodata`, hashes y linaje individuales.
  Un micro-smoke real de una ventana sobre 58 eventos fue válido; aún falta
  repetir el run remoto completo de seis componentes.
- `report_assets` `2.0.0` organiza figuras generales y por evento, datos
  principales, anexos y `report_dataset.json`, sin alterar los bundles
  científicos. `packages/reporting` `0.6.0`, con contrato editorial `2.3.0`,
  verifica sus assets y el `input.geojson` declarado, y proyecta JSON, CSV y
  GeoJSON a un PDF cliente determinístico y atómico. El expediente real produjo
  quince páginas inspeccionadas desde 41 assets: nueve verticales y seis
  apaisadas para RF anual, RGB y evidencia espectral de eventos. El informe suma
  índice, narrativa científica de portada y mapas con perímetro, grilla WGS 84,
  norte, escala, localizador y contexto vectorial OSM al 50 %, cacheado por
  extensión y con atribución ODbL. El mapa base es presentación, no evidencia.
  Las figuras internas no seleccionadas permanecen en el bundle técnico y el
  worker `0.2.0` ya genera el PDF como postproceso, lo publica con metadatos de
  integridad y lo incorpora al ZIP. La API `0.2.0` y el cliente web `0.2.0`
  exponen su descarga sin renderizar dentro del request HTTP.
- El último gate local aprobado contiene 610 tests, cobertura total 90,67 %,
  Ruff, formato sobre 147 archivos, Mypy sobre 146 archivos y
  `git diff --check` verdes. No se construyó frontend ni imagen de contenedor.
- Los Incrementos 1–4 de la **API interna asíncrona** están implementados en el
  monorepo; API, jobs y worker integran el `uv workspace`, mientras el cliente
  conserva su toolchain Node aislado. La capa Python cubre validación GeoJSON,
  jobs SQLite idempotentes, worker separado, resultados verificados, descargas
  allowlisted, cancelación y retención local. Sus 75 pruebas modulares no
  consultan GEE y mantienen cobertura por paquete superior a 90 %.
- `apps/web` agrega el cliente React/TypeScript minimalista: carga o dibujo,
  validación, creación, polling, cancelación y lectura básica de resultados.
  Tiene quince pruebas Vitest, typecheck estricto y auditoría productiva verde.
  El progreso es estimado y limitado hasta que la API confirma el terminal; el
  `analysis_id` persiste en la URL para reanudar polling después de recargar.
- Un gate multiproceso sintético verifica API HTTP → SQLite → worker →
  `report_assets` → PDF → endpoints/ZIP sin GEE. El runner y renderer falsos viven sólo bajo
  `tests/integration`; no existe un modo fake en producción.
- El worker publica primero el contrato curado y verificado bajo almacenamiento
  privado durable; `/report`, `/events` y `/assets` ya no dependen de conservar
  el workspace científico. `packages/domain` concentra geometría y superficies,
  por lo que la API no instala el pipeline GEE completo.
- La lectura se comprobó contra un expediente real preservado con 41 assets y
  cuatro eventos. No se ejecutó un nuevo análisis remoto ni se modificó el run.
- El smoke web real `c938a1c0-ca33-42a4-94b3-81c7276a3ef8` procesó el Polygon de
  1.438,42 ha y terminó `partial` por Hampel `diagnostic_unavailable`; pipeline,
  RF, agricultura, persistencia y atribución completaron. Publicó 41 assets y
  cuatro eventos verificables. La copia durable permite leerlos aun sin el
  workspace científico original.

### 2.2 Límites científicos y operativos vigentes

- No afirmar estabilidad operativa cloud hasta cerrar un smoke real posterior
  al collector compartido y `report_assets` 2.0.
- Hampel sigue siendo diagnóstico estacional y no equivalente metodológicamente
  a VISEC; su indisponibilidad justificada no bloquea los demás componentes.
- El RF sólo soporta transferencia 2020–2024 y no está calibrado como
  probabilidad. No extender años ni semántica sin validación.
- La validación temática independiente continúa siendo insuficiente,
  especialmente en bosque abierto; agricultura usa Dynamic World como única
  fuente temporal operativa del MVP.
- Ninguna salida automática puede emitir `conversion_confirmed`, certificación
  o conclusión legal EUDR.
- Conservar modelos y runs científicos canónicos: los outputs están ignorados
  por Git y no son recuperables mediante rollback.

### 2.3 Orden de avance actual

1. contenerizar API y worker por separado;
2. ejecutar un smoke local con Compose, volúmenes privados y límites explícitos;
3. desplegar gradualmente en AWS con almacenamiento privado, cola, identidad,
   observabilidad y límites de concurrencia.

La API es un adaptador: debe invocar `run_complete_analysis()` y no duplicar
reglas científicas ni acoplar el núcleo a FastAPI, AWS o React.

### 2.4 Documentos canónicos de contexto

- `README.md`: capacidades implementadas, ejecución y evidencia de gates.
- `NEXT_STEPS.md`: roadmap científico y validación pendiente.
- `API_CONTEXT.md`: contrato, tecnologías y evolución de API/cliente/AWS.

No agregar nuevos documentos históricos por incremento. Actualizar estos tres
cuando cambien arquitectura, capacidad operativa o prioridad.

## 3. Alcance autorizado

### 3.1 Incluido

- Procesamiento de imágenes satelitales en Google Earth Engine (GEE).
- Desarrollo en Python de descarga, validación, análisis y exportación.
- Análisis raster y vectorial.
- Construcción de una línea base forestal para el 31/12/2020.
- Series temporales desde 2020 hasta la fecha de ejecución.
- Detección de cambios y segmentación de eventos.
- Clasificación del uso posterior al cambio.
- Cálculo de áreas afectadas.
- Cruce con cartografía forestal y ambiental oficial.
- Generación de GeoJSON, GeoPackage, Cloud Optimized GeoTIFF, Parquet, CSV,
  PNG y JSON.
- Generación de gráficos y mapas de evidencia.
- Validación estadística y espacial.
- Registro de procedencia, versiones y parámetros.
- Diseño de una API interna para ejecutar análisis por establecimiento.

### 3.2 Fuera de alcance en esta etapa

- Declarar que un campo, animal, lote o producto está legalmente certificado.
- Emitir una DDS o una declaración simplificada EUDR.
- Enviar información a TRACES o al EUDR Information System.
- Integrar SIGSA, SIGICA, SIGCER, DT-e, ARCA, REPSAL o INAI.
- Reconstruir movimientos individuales de animales.
- Verificar legalidad fiscal, laboral, dominial o indígena.
- Sustituir decisiones de una autoridad, auditor o certificadora.
- Construir un ERP, CRM, portal comercial o interfaz pública completa.
- Gestionar pagos, contratos o información sanitaria.
- Automatizar una decisión negativa sin revisión humana.

Un agente no debe ampliar el alcance hacia estos componentes sin una
instrucción explícita del usuario.

## 4. Lenguaje de producto y límites legales

Usar lenguaje técnico prudente:

- Preferir "sin deforestación detectada" a "certificado libre de
  deforestación".
- Preferir "alerta de cambio" a "deforestación" hasta confirmar la conversión.
- Preferir "evidencia compatible con conversión" cuando no exista validación
  independiente.
- Informar siempre el período analizado, las fuentes y la resolución.
- Separar resultado automático, revisión humana y conclusión final.
- No presentar una fuente global como verdad de terreno.
- No afirmar cumplimiento EUDR basándose solamente en una intersección
  espacial.

La responsabilidad legal permanece en el operador que introduce el producto
en el mercado de la Unión Europea. Una certificación o verificación de tercera
parte puede apoyar la evaluación de riesgo, pero no sustituye la debida
diligencia.

## 5. Criterios EUDR que condicionan el GIS

El pipeline debe implementar como reglas de dominio:

- Fecha de corte: 31 de diciembre de 2020.
- Deforestación: conversión de bosque a uso agrícola, sea o no inducida
  directamente por humanos.
- Bosque: superficie superior a 0,5 ha, árboles de al menos 5 m de altura
  y cobertura de copa de al menos 10 %, o capaces de alcanzar esos umbrales,
  excluyendo tierras de uso predominantemente agrícola o urbano.
- La ganadería, las pasturas y la infraestructura de cría forman parte del uso
  agrícola.
- Una pérdida temporal por incendio, inundación, sequía o defoliación no debe
  considerarse automáticamente deforestación.
- Una perturbación seguida de recuperación forestal no equivale a conversión.
- El uso registrado en un catastro u OTBN es evidencia complementaria; las
  características observables del terreno prevalecen para determinar bosque.
- Para ganado, la geolocalización regulatoria puede representarse mediante un
  punto por establecimiento, pero el análisis ambiental del proyecto debe
  utilizar el polígono productivo siempre que esté disponible.
- Las coordenadas intercambiables con sistemas EUDR deben expresarse en WGS 84,
  EPSG:4326, con al menos seis decimales.

No aplicar el concepto adicional de degradación forestal propio de productos
de madera como si fuera un requisito independiente para carne bovina.

## 6. Fuentes de referencia

### 6.1 Referencias normativas y metodológicas

Antes de modificar reglas de dominio, consultar:

- Reglamento (UE) 2023/1115 y sus modificaciones vigentes.
- FAQ EUDR más reciente de la Comisión Europea.
- Guidance Document for the Regulation on Deforestation-Free Products más
  reciente.
- EU Observatory on Deforestation and Forest Degradation.
- Protocolo VISEC Carne Libre de Deforestación Argentina suministrado con el
  proyecto.

Las guías y FAQ pueden cambiar. No codificar fechas de aplicación, roles
empresariales o reglas administrativas sin registrar la versión de la fuente.
La fecha de corte ambiental del 31/12/2020 sí es una constante de dominio.

### 6.2 Fuentes satelitales primarias

Prioridad inicial:

1. Sentinel-2 Level-2A Surface Reflectance, 10 m.
2. Sentinel-1 GRD, radar, para complementar nubosidad y cambios estructurales.
3. HLS Sentinel-2/Landsat, 30 m, para continuidad temporal y comparación con
   VISEC.
4. Landsat 8 y 9 Collection 2 Level-2, 30 m, como serie independiente.

Sentinel-2 debe ser la fuente óptica principal del MVP. HLS y Landsat actúan
como evidencia complementaria y como línea de comparación.

### 6.3 Mapas de bosque y cobertura

- JRC Global Forest Cover 2020, versión 3 o posterior documentada.
- JRC Global Forest Types 2020 cuando corresponda.
- MapBiomas Chaco y colecciones posteriores disponibles.
- Hansen Global Forest Change.
- Global Forest Watch y sus alertas, respetando la licencia de cada producto.
- Mapas oficiales nacionales o provinciales de bosques nativos.
- OTBN provinciales.

Ningún mapa individual debe utilizarse como fuente definitiva. El pipeline debe
aplicar convergencia de evidencias y conservar desacuerdos entre productos.

### 6.4 Capas complementarias

- Límites administrativos oficiales.
- SIFAP/CIAM para áreas protegidas.
- Catastro y polígonos de unidades productivas cuando estén autorizados.
- Geometrías RENSPA cuando exista un acuerdo de acceso válido.
- Modelos de elevación para explicar efectos topográficos.
- Precipitación, sequía, incendios y anomalías climáticas para reducir falsos
  positivos.
- Imágenes de alta resolución o evidencias de campo únicamente cuando su
  licencia y autorización permitan su uso.

### 6.5 Licencias y uso comercial

- Los datos Copernicus Sentinel admiten uso comercial y productos derivados,
  con atribución adecuada.
- Un producto derivado debe indicar que contiene datos Copernicus Sentinel
  modificados y el período correspondiente.
- Google Earth Engine requiere modalidad comercial si el pipeline se ofrece
  como servicio comercial.
- La disponibilidad de un dataset en el catálogo de GEE no garantiza que su
  licencia permita todos los usos.
- Registrar por dataset: proveedor, colección, versión, licencia, fecha de
  acceso, atribución y restricciones.
- No incorporar datos privados de productores o SENASA sin autorización.

Crear y mantener `data/licenses.yml` cuando comience la implementación.

## 7. Patrón metodológico base

La referencia VISEC utiliza:

- HLS a 30 m;
- enmascarado Fmask;
- NDVI, EVI2, NMDI, LSWI, NIRv y kNDVI;
- agregaciones mensuales y períodos específicos;
- Random Forest y Gradient Tree Boosting;
- tres clasificaciones por píxel;
- decisión por convergencia de dos de tres modelos;
- CCDC para detectar cambios abruptos;
- seis píxeles conectados a 30 m como aproximación a 0,5 ha;
- actualización anual.

Esta arquitectura es una línea base, no una restricción. Toda reproducción debe
ser atribuida al protocolo utilizado como referencia y reimplementada con
código propio.

## 8. Mejoras obligatorias respecto de la línea base VISEC

### 8.1 Mayor resolución espacial

- Procesar Sentinel-2 a 10 m como producto principal.
- Mantener HLS a 30 m como benchmark.
- No mezclar resoluciones sin declarar remuestreo, kernel y grilla objetivo.
- Calcular superficie en una proyección equivalente; no decidir únicamente por
  cantidad de píxeles.

Como control aproximado:

- 50 píxeles de 10 x 10 m equivalen a 0,5 ha.
- 6 píxeles de 30 x 30 m equivalen a 0,54 ha.

La decisión final debe usar área vectorizada o área raster en CRS equivalente,
no estos conteos aproximados.

### 8.2 Productos intermedios explícitos

El pipeline debe conservar, como mínimo:

- máscara de nubes, sombras y nieve;
- composición mensual o estacional;
- cantidad de observaciones válidas por píxel;
- cubo de índices espectrales;
- probabilidad de bosque en la línea base;
- desacuerdo entre mapas de bosque;
- fecha estimada de perturbación;
- magnitud del cambio;
- probabilidad de perturbación;
- persistencia temporal;
- probabilidad de conversión a uso agrícola;
- uso posterior al cambio;
- componentes espaciales y superficie;
- nivel de confianza;
- códigos de motivo y banderas de calidad.

No conservar solamente la decisión binaria final.

### 8.3 Separar detección de atribución

Implementar dos etapas independientes:

1. Detección: identificar pérdida o cambio abrupto en vegetación.
2. Atribución: determinar si el cambio representa conversión de bosque a uso
   agrícola o ganadero.

Una perturbación solo puede avanzar a "deforestación confirmada" cuando exista
evidencia de:

- bosque al 31/12/2020;
- pérdida posterior a la fecha de corte;
- persistencia del cambio;
- uso agrícola o ganadero posterior;
- superficie que alcance el umbral aplicable;
- ausencia de una explicación alternativa suficientemente respaldada.

### 8.4 Integración óptica y radar

- Usar Sentinel-1 como evidencia independiente.
- Evaluar VH, VV, cociente VH/VV y estadísticas temporales.
- No forzar la fusión SAR-óptica si no mejora métricas de validación.
- Documentar órbita, polarización, preprocesamiento y normalización.

### 8.5 Calibración regional

Argentina no debe modelarse como un único paisaje homogéneo. Preparar
estratificación por ecorregión, comenzando por:

- Chaco Seco;
- Chaco Húmedo;
- Espinal;
- Monte;
- Yungas;
- Selva Paranaense.

Validar por separado bosques caducifolios, sistemas silvopastoriles, sequías,
incendios y ambientes con baja cobertura de copa.

### 8.6 Incertidumbre y revisión

Todo resultado debe pertenecer a uno de estos estados:

- `no_change_detected`: sin cambio relevante detectado;
- `low_risk`: evidencia consistente con ausencia de conversión;
- `review_required`: alerta, desacuerdo o información insuficiente;
- `conversion_likely`: evidencia fuerte de conversión;
- `conversion_confirmed`: confirmado mediante revisión documentada;
- `insufficient_data`: observaciones o geometría inadecuadas.

El modelo automático no debe emitir `conversion_confirmed`.

## 9. Pipeline objetivo

### Etapa 0. Ingesta

Entrada mínima:

- `establishment_id`;
- geometría punto o polígono;
- fuente de la geometría;
- fecha de alta o versión;
- provincia y ecorregión si están disponibles.

Validar:

- geometría no vacía;
- CRS conocido;
- coordenadas dentro de Argentina;
- topología válida;
- ausencia de vértices redundantes extremos;
- superficie razonable;
- correspondencia entre punto y polígono cuando existan ambos.

### Etapa 1. Preparación espacial

- Normalizar intercambio a EPSG:4326.
- Seleccionar CRS equivalente o UTM apropiado para áreas y distancias.
- Generar AOI de análisis sin alterar el polígono declarado.
- Mantener geometría original y geometría reparada por separado.
- Registrar cualquier reparación topológica.

### Etapa 2. Línea base forestal 2020

- Recortar productos forestales disponibles.
- Generar variables Sentinel-2 y/o Landsat cercanas a la fecha de corte.
- Estimar probabilidad de bosque.
- Crear máscara de consenso y mapa de desacuerdo.
- Identificar zonas que requieren revisión.

Salida mínima:

- `forest_probability_2020.tif`;
- `forest_consensus_2020.tif`;
- `forest_disagreement_2020.tif`;
- metadatos de fuentes y modelo.

### Etapa 3. Serie temporal

- Aplicar máscaras QA.
- Crear composiciones mensuales y estacionales.
- Conservar conteo de observaciones.
- Calcular índices ópticos.
- Incorporar variables SAR cuando estén disponibles.
- Evitar interpolar períodos largos sin marcarlos.

Índices iniciales:

- NDVI;
- EVI2;
- NBR;
- NDMI;
- NMDI;
- LSWI;
- NIRv;
- kNDVI.

No asumir que más índices implican mejor modelo. Mantener selección de
variables basada en validación.

### Etapa 4. Detección de cambios

Implementar al menos:

- benchmark CCDC;
- comparación temporal robusta pre/post;
- detector alternativo basado en probabilidad o ensamble.

Cada evento debe registrar:

- fecha inicial y final estimada;
- magnitud;
- persistencia;
- sensores que lo respaldan;
- área;
- confianza;
- banderas climáticas o de incendio.

### Etapa 5. Atribución del cambio

Clasificar cobertura posterior:

- bosque o regeneración;
- pastura;
- cultivo;
- suelo desnudo persistente;
- infraestructura;
- agua;
- incendio o perturbación temporal;
- desconocido.

La atribución debe considerar contexto temporal, no una única escena.

### Etapa 6. Agregación espacial

- Vectorizar eventos persistentes.
- Disolver píxeles conectados con una regla documentada.
- Eliminar artefactos únicamente mediante parámetros versionados.
- Calcular área en hectáreas en CRS equivalente.
- Intersectar con bosque 2020 y con la unidad productiva.
- Conservar también eventos menores a 0,5 ha como información intermedia.

Los eventos menores no deben cambiar automáticamente el estado final, pero no
deben descartarse del registro.

### Etapa 7. Evaluación y evidencia

Generar:

- resumen JSON legible por máquinas;
- GeoJSON de eventos;
- COG de probabilidad y cambio;
- tabla Parquet o CSV de la serie temporal;
- mapa PNG antes/después;
- gráfico temporal por evento;
- mosaico de fuentes;
- manifiesto de procedencia;
- informe técnico opcional.

## 10. Contrato de salida

El resumen por establecimiento debe contener, como mínimo:

```json
{
  "establishment_id": "string",
  "analysis_id": "uuid",
  "analysis_version": "semver-or-git-sha",
  "cutoff_date": "2020-12-31",
  "analysis_end_date": "YYYY-MM-DD",
  "geometry_source": "declared|renspa|other",
  "forest_area_2020_ha": 0.0,
  "detected_change_area_ha": 0.0,
  "likely_conversion_area_ha": 0.0,
  "status": "low_risk|review_required|conversion_likely|insufficient_data",
  "confidence": 0.0,
  "quality_flags": [],
  "events": [],
  "datasets": [],
  "parameters_hash": "string",
  "created_at": "ISO-8601"
}
```

No cambiar este contrato sin versionar el esquema.

## 11. Tecnologías

### 11.1 Lenguaje y entorno

- Python 3.12 o versión definida en `pyproject.toml`.
- `uv` para dependencias y entornos.
- Notebooks solo para exploración.
- Código productivo dentro de paquetes Python importables.
- JavaScript de GEE permitido para prototipos, no como única implementación.

### 11.2 Procesamiento geoespacial

- Google Earth Engine Python API.
- `geemap` para desarrollo y depuración.
- `geopandas`, `shapely`, `pyproj`.
- `rasterio`, `rioxarray`, `xarray`.
- `numpy`, `pandas`, `pyarrow`.
- `scikit-learn`.
- `duckdb` para análisis local.
- `pystac-client` y STAC cuando se procese fuera de GEE.

Agregar LightGBM, XGBoost, PyTorch u otras dependencias solo si existe una
justificación medible.

### 11.3 Calidad y operación

- `pydantic` para contratos y configuración.
- `typer` para CLI.
- `pytest` para pruebas.
- `ruff` para lint y formato.
- `mypy` o `pyright` para tipado.
- `pre-commit` cuando el repositorio tenga flujo Git estable.
- YAML o TOML para parámetros versionados.

### 11.4 Almacenamiento

- GeoJSON o GeoPackage para intercambio vectorial.
- GeoParquet para volumen y análisis.
- Cloud Optimized GeoTIFF para raster.
- Parquet para series y métricas.
- JSON para manifiestos.
- PostGIS en una etapa posterior, no como dependencia del primer prototipo.

## 12. Estructura vigente

```text
.
├── AGENTS.md
├── README.md                  # referencia canónica del estado actual
├── NEXT_STEPS.md              # roadmap científico y operativo
├── API_CONTEXT.md             # arquitectura de API, cliente web y AWS
├── pyproject.toml
├── apps/
│   └── web/                   # cliente React + TypeScript + MapLibre
├── packages/
│   ├── domain/                # geometría/área compartidas sin stack GEE
│   ├── jobs/                  # contrato y repositorio SQLite
│   └── reporting/             # contrato editorial y renderer PDF
├── services/
│   ├── api/                   # adaptador HTTP liviano
│   └── worker/                # ejecución del pipeline científico
├── configs/
│   └── default.yml
├── data/
│   ├── catalog.yml
│   ├── licenses.yml
│   ├── schemas/
│   └── samples/
├── scripts/
├── src/
│   └── deforestation_pipeline/
├── tests/
├── pruebas.py
└── outputs/
```

No fragmentar el estado del proyecto en documentos por pasos históricos.
Actualizar `README.md` cuando cambie la capacidad implementada y
`NEXT_STEPS.md` cuando cambie el orden científico. Actualizar `API_CONTEXT.md`
cuando cambie el contrato o la arquitectura de integración. No crear
directorios vacíos por anticipado; incorporarlos cuando una tarea los necesite.

## 13. Configuración y reproducibilidad

Toda ejecución debe registrar:

- commit o versión del código;
- archivo de configuración;
- AOI y versión de geometría;
- colecciones y versiones;
- período analizado;
- filtros de nubes;
- composición temporal;
- índices y variables;
- modelo y semilla;
- umbrales;
- CRS y método de cálculo de área;
- fecha de ejecución;
- entorno y versiones de dependencias.

No ocultar parámetros dentro de notebooks o constantes dispersas.

Usar semillas determinísticas cuando el algoritmo lo permita. Una nueva
ejecución con los mismos datos y parámetros debe producir el mismo resultado,
salvo que una colección remota haya cambiado; en ese caso, registrar la
versión o fecha de acceso.

## 14. Validación

### 14.1 Diseño

- Separar entrenamiento, validación y prueba espacialmente.
- Evitar que píxeles del mismo establecimiento aparezcan en entrenamiento y
  prueba.
- Validar por ecorregión.
- Incluir casos sin cambio, desmontes, incendios, sequías, inundaciones,
  silvopastoreo y regeneración.
- Comparar contra VISEC cuando existan resultados disponibles, sin asumir que
  VISEC es verdad de terreno.

### 14.2 Métricas

Reportar como mínimo:

- precision;
- recall;
- F1;
- balanced accuracy;
- matriz de confusión;
- error de comisión;
- error de omisión;
- IoU espacial de eventos;
- error de superficie;
- error temporal de fecha de cambio;
- calibración de probabilidades.

Priorizar la reducción de falsos negativos sin volver inutilizable el sistema
por falsos positivos. Los umbrales deben seleccionarse de acuerdo con el costo
operativo de la revisión humana.

### 14.3 Pruebas mínimas

- geometrías inválidas;
- polígonos multiparte;
- establecimientos que cruzan husos UTM;
- píxeles en el límite del establecimiento;
- cambios menores y mayores a 0,5 ha;
- nubes persistentes;
- ausencia de observaciones;
- incendio con recuperación;
- pérdida seguida de pastura;
- discrepancia entre GFC 2020, OTBN y MapBiomas;
- reproyección y cálculo de área;
- serialización del contrato de salida.

## 15. Reglas para agentes de desarrollo

1. Leer este archivo antes de proponer o implementar cambios.
2. Inspeccionar el repositorio y preservar cambios no relacionados.
3. Mantener las tareas dentro del alcance GIS y teledetección.
4. No inventar acceso a RENSPA, SENASA, GEE comercial ni datos privados.
5. No usar credenciales incrustadas en código, notebooks o archivos de
   configuración.
6. No descargar mosaicos nacionales completos cuando una consulta por AOI
   resuelva la tarea.
7. No ejecutar análisis nacionales costosos sin estimar primero área,
   cómputo, almacenamiento y cuotas.
8. Preferir funciones pequeñas, testeables y tipadas.
9. Separar I/O, procesamiento, reglas de decisión y presentación.
10. No acoplar el núcleo analítico a una interfaz web.
11. Conservar datos intermedios necesarios para auditar una decisión.
12. No descartar alertas ambiguas: enviarlas a `review_required`.
13. No etiquetar una perturbación como deforestación sin atribuir el uso
    posterior.
14. No modificar umbrales regulatorios para mejorar métricas.
15. Documentar toda desviación respecto de la metodología base.
16. Agregar pruebas para cada corrección de un error.
17. Ejecutar las verificaciones relevantes antes de dar una tarea por
    terminada.

## 16. Seguridad y privacidad

- Tratar polígonos productivos, identificadores RENSPA y datos de productores
  como información sensible.
- Usar ejemplos sintéticos o anonimizados en pruebas y documentación.
- No publicar coordenadas reales sin autorización.
- No registrar tokens, claves o URLs firmadas.
- Separar datos públicos, privados y derivados.
- Preparar controles de acceso antes de incorporar información institucional.

## 17. Fases de desarrollo

### Fase 0. Benchmark reproducible

- Una AOI pequeña y casos conocidos.
- HLS a 30 m.
- Índices de VISEC.
- CCDC o alternativa equivalente.
- Componentes de cambio y umbral de 0,5 ha.
- Salida GeoJSON y mapa.

### Fase 1. MVP mejorado

- Sentinel-2 a 10 m.
- Línea base GFC 2020 v3 más clasificación propia.
- Probabilidades y desacuerdo.
- Detección y atribución separadas.
- Estados de revisión.
- Paquete de evidencia reproducible.

### Fase 2. Robustez regional

- Sentinel-1.
- Calibración por ecorregión.
- Incendios, sequía y clima.
- Validación espacial independiente.
- Comparación sistemática 10 m frente a 30 m.

### Fase 3. Operación

- CLI y API interna.
- Ejecuciones por lotes.
- Caché y control de costos.
- Manifiestos, versionado y trazabilidad completa.
- Generación automatizada de informes.

### Fase 4. Integración futura

Solo con autorización y acuerdos:

- datos RENSPA;
- trazabilidad animal;
- sistemas SENASA;
- plataformas sectoriales;
- verificadores de tercera parte;
- integración EUDR Information System.

## 18. Definición de terminado para el MVP

El MVP estará terminado cuando:

- procese de extremo a extremo al menos un conjunto de establecimientos de
  prueba;
- construya una línea base forestal 2020 documentada;
- genere series 2021-actualidad;
- detecte y vectorice eventos;
- distinga perturbación temporal de conversión probable;
- calcule áreas correctamente;
- produzca estados con confianza y banderas;
- exporte los productos definidos en el contrato;
- registre procedencia y parámetros;
- incluya pruebas automatizadas;
- tenga validación con datos independientes;
- pueda reproducirse desde una CLI documentada;
- no emita afirmaciones de certificación legal.

## 19. Decisión rectora

Cuando exista tensión entre rapidez y auditabilidad, priorizar auditabilidad.
Cuando exista tensión entre una decisión binaria y la incertidumbre real,
conservar la incertidumbre. Cuando exista desacuerdo entre fuentes, mostrarlo.

El objetivo no es producir el mapa más convincente, sino la evidencia técnica
más reproducible y defendible posible.
