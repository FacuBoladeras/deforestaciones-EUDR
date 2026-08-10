# Próximos pasos

Este archivo es el único roadmap del proyecto. Parte del checkpoint donde la
**detección de perturbaciones está implementada** y existe una **atribución
post-cambio v0 conservadora por evento**. Aún falta integrar evidencia
independiente de uso posterior y explicaciones alternativas adicionales. No
autoriza a emitir conclusiones legales, confirmar deforestación automáticamente
ni ampliar el alcance hacia trazabilidad animal o sistemas regulatorios.

La regla de ejecución es simple: cerrar cada bloque con evidencia verificable
antes de comenzar el siguiente. No implementar todo el Paso 15 en una sola
corrida.

## 0. Limpieza del checkpoint — completada

Antes de desarrollar capacidad científica nueva:

- [x] regenerar los diez contratos con
  `uv run python scripts/export_schema.py`;
- [x] comprobar que el exportador no crea archivos inesperados;
- [x] ejecutar Ruff, formato, Mypy y Pytest;
- [x] mantener cobertura total `>= 90 %`;
- [x] ejecutar una prueba local sin GEE con el sample anonimizado;
- [x] confirmar que no existen imports, enlaces ni nombres de módulos
  eliminados;
- [x] registrar el resultado del gate sin incorporar outputs o credenciales.

**Criterio de salida:** árbol limpio, schemas determinísticos, CLI importable y
gate completo aprobado.

**Cierre `2026-07-30`:** diez schemas regenerados sin diferencias, smoke local
publicado atómicamente y eliminado después de validarlo, Ruff/formato/Mypy
aprobados, **343 pruebas aprobadas** y **90,94 % de cobertura**.

### 0.1 Dataset P0 provincial 2020 — completado

- [x] limitar el MVP de entrenamiento a Entre Ríos y al año 2020;
- [x] dividir el AOI en UTM 20S y 21S, excluyendo un buffer de 3 km alrededor
  del meridiano `-60°`;
- [x] generar 68 features inferibles desde las cuatro estaciones HLS;
- [x] mantener `proxy_label` separado y excluir las 15 columnas de metadata
  del conjunto de predictores;
- [x] conservar desacuerdo como `ambiguous`;
- [x] obtener 1.000 filas por zona: 400 `forest`, 400 `non_forest` y
  200 `ambiguous`;
- [x] ejecutar el dry-run con conteos y selectores exactos;
- [x] exportar con OAuth de usuario y completar ambas tareas Drive;
- [x] descargar y validar 2.000 filas, 84 columnas, labels, IDs, coordenadas,
  zonas, tamaños, MD5 y SHA-256.

El P0 no es verdad de terreno. MapBiomas y las restantes fuentes generan
pseudolabels; no determinan por sí solas bosque conforme a EUDR. El límite GAUL
es un AOI operativo reproducible, no cartografía provincial oficial.

**Criterio de salida alcanzado:** dos CSV reproducibles y un reporte local de
validación en
`outputs/training_p0/training-p0-20260730T222822Z-validation.json`.

### 0.2 Dataset multianual 2020–2024 — materializado

- [x] verificar en GEE que MapBiomas Argentina Collection 2 v3 llega hasta
  `classification_2024` y excluir 2025 por falta de etiqueta anual;
- [x] reutilizar el exportador P0 y parametrizar sólo el año/configuración;
- [x] versionar el contrato multianual como `1.1.0`, preservando lectura del P0
  `1.0.0` y bloqueando extensiones multianuales bajo la versión antigua;
- [x] mantener un único esquema canónico de 68 features y una allowlist de 56
  predictores idéntica para todos los años;
- [x] exportar y completar ocho tareas Drive para 2021–2024, sin duplicar el P0
  2020 validado;
- [x] descargar y verificar MD5 contra Drive para los ocho CSV nuevos;
- [x] concatenar diez archivos en un CSV de 10.000 observaciones y 84 columnas;
- [x] comprobar labels, votos, tipos, hashes y split espacial constante por
  `block_id` entre años;
- [x] entrenar el único RF multianual usando sólo `forest/non_forest` y la
  allowlist de 56 predictores;
- [x] evaluar candidatos sobre split espacial agrupado y reportar métricas por
  fila, año y sitio, sin seleccionar mediante test;
- [x] comparar peso uniforme contra peso inverso por `sample_id`; ganó el peso
  uniforme en validation (`F1=0,9756`, peor F1 anual `0,9617`);
- [x] reajustar el ganador con train+validation y evaluar una vez en test
  (`F1=0,9613`, balanced accuracy `0,9616`);
- [x] exportar 500 árboles en modo `classification` al nuevo asset candidato
  `projects/ee-facuboladerasgee/assets/models/rf_forest_multiyear_2020_2024_v1`;
- [x] verificar TABLE, 500 propiedades `tree` y paridad GEE↔sklearn: 24/24
  clases iguales, error máximo de votos `0,004` por redondeo de thresholds;
- [x] ejecutar el candidato por cada año sobre `costa-uru`, conservando clase,
  score, soporte común, deltas año–2020 y comparación P0/candidato;
- [ ] validar la transferencia temporal con referencia independiente antes de
  cambiar el asset activo del pipeline.

El CSV combinado está en
`outputs/training_p0/raw/entre_rios_training_2020_2024_multiyear.csv`; su
contrato y auditoría están en
`outputs/training_p0/training-multiyear-2020-2024-validation.json`. Los 10.000
registros corresponden a 2.102 sitios únicos observados en varios años, no a
10.000 sitios independientes. `sample_year`, IDs, coordenadas, bloque, split,
zona y pseudolabel no pueden entrar como predictores. No hay transiciones
directas `forest` ↔ `non_forest` en los pseudolabels: este panel entrena
clasificación interanual, no detección de conversión.

El bundle reproducible de entrenamiento/export está en
`outputs/models/rf_forest_multiyear_2020_2024_v1/`. El asset nuevo es sólo un
candidato: `configs/default.yml` continúa apuntando al RF P0 2020 y
`temporal_transfer_validated` permanece `false`.

**Control temporal `2026-08-06`:** la comparación 2020 usa el mismo stack y
soporte para ambos modelos. El bundle 3.4.0 agrega cinco GeoTIFF QA de 12 bandas
per-pixel —total/L30/S30 por DJF/MAM/JJA/SON— y separa la fecha solicitada
`2024-12-31` de la cobertura efectiva `2024-11-30`. Las figuras incorporan
leyendas discretas, norte y escala métrica. Las pérdidas y ganancias candidatas
siguen requiriendo persistencia temporal y referencia independiente; no se
promueve el candidato y `temporal_transfer_validated` permanece `false`.

El run canónico
`costa-uru-rf-deltas-2020-2024__20260806T213320256834Z__582c56fd-557`
manifestó 56 artefactos y 38 TIFF sobre la misma grilla EPSG:32721 de 149×98
píxeles a 30 m. Los cinco QA tienen 12 bandas `int16`, nodata `-9999`, nombres,
semántica, grilla y SHA-256 verificados. Las métricas quedaron invariantes
respecto del run anterior: el desacuerdo P0/candidato 2020 sigue en 276 píxeles
(`24,84 ha`).

El TABLE GEE se mantiene como artefacto de paridad. GEE construye el
`decisionTreeEnsemble` candidato y `reduceRegion` puntual funciona. El fallo
observado queda limitado a `getDownloadURL` del grafo real completo —stack HLS
anual más clasificación candidata— con `Description length exceeds maximum`;
P0 sí descarga y el componente limitante exacto no está aislado. El fallback
descarga los predictores desde GEE y aplica el joblib sklearn exacto localmente.

La etiqueta multianual sigue siendo un proxy conservador: MapBiomas y Hansen
aportan variación anual, mientras JRC y WorldCover son anclajes 2020. El
dataset no representa un LULC anual completo ni demuestra bosque o ausencia de
deforestación conforme a EUDR.

## 1. Validación independiente del detector actual

La implementación de una señal no demuestra exactitud temática. Antes de
atribuir causas se necesita un conjunto de validación espacialmente
independiente.

**Progreso parcial verificado:**

- [x] ejecutar un smoke remoto autorizado y acotado para 2021–2022 que confirmó
  transporte, materialización CCDC escalar y publicación del paquete;
- [x] conservar 15 períodos de referencia y siete períodos evaluables en el
  expediente, sin atribución ni evaluación final;
- [ ] contrastar las señales contra verdad de referencia independiente.

El smoke autorizado no sustituye un evento positivo conocido ni valida
exactitud temática. No debe utilizarse para ajustar umbrales o afirmar
deforestación.

### 1.1 Diseñar casos

Preparar, como mínimo:

- un evento positivo conocido posterior al 31/12/2020;
- un control forestal sin cambio;
- una perturbación temporal con recuperación;
- un caso de incendio;
- un caso de sequía o fuerte estacionalidad;
- un área con desacuerdo entre fuentes de bosque;
- un área con observaciones ópticas insuficientes.

Los polígonos reales no deben incorporarse al repositorio sin autorización.
Guardar sólo identificadores anonimizados, procedencia permitida y criterios de
verdad de referencia.

### 1.2 Definir la unidad de evaluación

Antes de medir desempeño, fijar:

- unidad espacial: píxel, componente y establecimiento;
- separación espacial entre desarrollo y validación;
- tolerancia temporal para comparar fechas;
- tratamiento explícito de bordes y eventos menores de 0,5 ha;
- métricas mínimas: precision, recall, F1, error de comisión/omisión, IoU,
  error de superficie y error temporal;
- política para observaciones insuficientes y casos ambiguos.

**Criterio de salida:** protocolo escrito y casos seleccionados sin ajustar
umbrales mirando el conjunto de prueba.

### 1.3 QA del P0 y Random Forest baseline

La integración operativa mínima del RF P0 está implementada:

- [x] asset TABLE geemap declarado en configuración, con 500 árboles y clases
  `0=non_forest`, `1=forest`;
- [x] contrato único compartido entre entrenamiento e inferencia;
- [x] allowlist ordenada de 56 variables espectrales;
- [x] exclusión explícita de las 12 bandas de conteo, preservadas como QA;
- [x] publicación de clase, fracción de votos RAW no calibrada y completitud de
  inputs en la línea base 2020;
- [x] metadata limita el modelo a Entre Ríos/2020 y declara dependencia de
  pseudolabels y ausencia de validación temporal;
- [x] F1 `0,9677` reproducida contra pseudolabels del split espacial de test.
- [x] smoke remoto acotado de baseline 2020 con los tres TIFF RF, JSON, PNG y
  hashes de manifiesto verificados.

Ese F1 no constituye validación forestal independiente. El siguiente
incremento debe permanecer acotado a las 2.000 filas ya obtenidas:

1. inspeccionar visualmente una muestra estratificada por label, zona, bloque y
   nivel de desacuerdo;
2. adjudicar un subconjunto con evidencia independiente y registrar fuente,
   fecha, confianza y motivo;
3. cuantificar errores de pseudolabel, píxeles mixtos y casos de bosque abierto;
4. reportar métricas independientes por zona y dificultad, además de matriz de
   confusión;
5. evaluar calibración sólo con una referencia independiente adecuada.

**Paquete de revisión generado `2026-08-03`:** selección determinística de 50
puntos piloto y 200 puntos de test, con cupos exactos por UTM y pseudoclase. El
revisor recibe únicamente IDs ciegos, medianas Sentinel-2 2019/2020/2021,
falso color 2020 y conteos de observaciones válidas. Coordenadas, pseudolabels,
features y predicciones RF permanecen en una tabla privada sellada por hash.
La adjudicación visual asistida por agentes fue congelada antes de abrir esa
tabla. Resultado final: 58 `forest`, 54 `non_forest` y 88 `uncertain`; sólo
112/200 casos fueron adjudicables (`0,56`). Sobre ese subconjunto el RF obtuvo
precision `0,9298`, recall `0,9138`, F1 `0,9217` y balanced accuracy `0,9199`.
El gate conjuntivo **falló** por adjudicabilidad menor a `0,80` y porque los 11
casos `open_woody` quedaron inciertos, impidiendo estimar omisión en ese estrato.
Este gate no habilita transferencia temporal del RF ni una cobertura automática
de todo bosque potencial. El screening temporal sí puede avanzar en el estrato
`automated_forest`, definido antes de la corrida con reglas conservadoras, y
debe preservar el resto como revisión o insuficiencia sin forzar una clase.

El siguiente incremento debe mejorar la referencia independiente de los casos
inciertos mediante evidencia adicional permitida —por ejemplo imágenes de
mayor resolución, interpretación experta o campo— y conservar el test actual
congelado. La referencia visual asistida no es verdad de campo, definición
legal de bosque ni prueba de cumplimiento EUDR.

No usar `sample_id`, coordenadas, bloque, split, zona UTM, votos crudos ni
conteos de fuentes como features. Son metadata de control y procedencia.

**No escalar todavía:** no extraer 100.000 puntos, no incorporar otra provincia
y no agregar RF/GTB adicionales hasta completar QA independiente y comprobar
con curvas de aprendizaje que más datos o complejidad aportan una mejora
medible.

**Criterio de salida:** baseline RF integrado, sin leakage espacial, con
resultados sobre referencia independiente y limitaciones documentadas.

### 1.2 Screening con escalamiento — completado

- [x] congelar `forest_screening` 1.1.0 con votos RF cuantizados a la grilla de
  500 árboles, `>=0,80` para bosque y `<=0,20` para no bosque, soporte core
  mínimo de dos fuentes y unanimidad core;
- [x] publicar `automated_evaluable_2020.tif`, `review_required_2020.tif` e
  `insufficient_data_2020.tif` bajo `tiffs/evidence/`;
- [x] calcular áreas y fracciones desde la transformación de la grilla métrica;
- [x] ejecutar señales robustas y CCDC en todo el AOI, conservando conteos por
  `automated_forest`, `automated_nonforest`, revisión e insuficiencia;
- [x] impedir una frase automática de ausencia de cambio cuando existe alguna
  señal en `automated_forest`, y limitar explícitamente la frase al bosque
  automático de alta confianza y al período efectivamente observado;
- [x] derivar `analysis_end_date` de la última estación publicada y conservar
  la fecha solicitada como `requested_analysis_end_date`;
- [x] mantener `attribution_generated: false` y
  `final_assessment_generated: false`.

El próximo incremento de este bloque es vectorizar las áreas de revisión y
definir una orden de campo/dron sin crear todavía ingestión de imágenes de dron
ni atribución agrícola.

### 1.3 Eventos persistentes y evidencia visual — completado

- [x] intersectar `PERSISTENT_CANDIDATE` con `automated_forest` antes de
  segmentar;
- [x] agrupar con conectividad de ocho vecinos y IDs derivados de grilla más
  píxeles;
- [x] medir área en la grilla proyectada y marcar `0,5 ha` sin filtrar eventos
  menores;
- [x] publicar todos los componentes en CSV, JSON y GeoJSON;
- [x] conservar conectividad lógica de ocho vecinos, polygonizar con cuatro y
  validar topología antes y después de transformar a WGS 84;
- [x] generar un mapa general y limitar las fichas PNG a los cinco eventos de
  mayor superficie, con desempate por ID;
- [x] comparar en cada ficha la última referencia pre-corte y la primera
  ventana del evento de la misma estación, más la serie media multíndice;
- [x] mantener `attribution_generated: false` y evitar lenguaje de
  deforestación confirmada.

Esta segmentación organiza evidencia de **detección** para revisión. No
reemplaza la agregación posterior a la atribución: si varios componentes o
usos posteriores deben combinarse para una regla final, esa decisión pertenece
al Paso 15 y todavía no está implementada.

**Cierre remoto `2026-08-04`:** el AOI autorizado 2020–2025 produjo 58 eventos
persistentes dentro de `automated_forest`, 131,85 ha en total, 19 eventos de al
menos `0,5 ha` y 39 menores conservados. Se publicaron cinco fichas y la
colección completa quedó en CSV, JSON y GeoJSON, sin atribución ni evaluación
final.

**Reauditoría topológica:** el run canónico
`costa-uru-events-topology-v3-2020-2025__20260804T233653731180Z__c9d07cde-445`
conservó métricas e IDs y produjo 43 `Polygon` más 15 `MultiPolygon`, todos
válidos.

### 1.4 Benchmark Hampel protegido — completado, no activado

- [x] congelar una configuración autónoma para NDVI, EVI2, NMDI, LSWI, NIRv y
  kNDVI, ventana 5, umbral 3 × 1,4826 MAD y política explícita para MAD cero;
- [x] aplicar el benchmark por estación meteorológica sobre las medianas
  espaciales estacionales de los 58 eventos persistentes;
- [x] seleccionar diez controles 3 × 3 determinísticos, estrictamente estables
  dentro de `automated_forest` y separados al menos cinco píxeles;
- [x] conservar valor crudo/filtrado, flags, mediana, MAD, umbral, soportes y
  motivo para cada observación;
- [x] impedir reemplazos en bordes, rachas y cambios sin retorno bilateral, con
  pruebas sintéticas para spikes, escalones, NaN, MAD cero y series cortas;
- [x] publicar observaciones, métricas, figura y configuración bajo los roots
  existentes de un bundle derivado autocontenido;
- [x] distinguir artefactos fuente validados, reutilizados sin cambios y
  transformados; `json/run/summary.json` se declara transformado;
- [x] mantener `activated: false` y no realimentar el resultado a detección.

**Cierre local `2026-08-04`:** sobre 1.392 observaciones finitas por índice de
eventos, Hampel marcó entre 93 y 140 candidatos y reemplazó entre 27 y 69. En
240 observaciones finitas por índice de controles marcó entre 19 y 28 y
reemplazó entre 5 y 11. El resultado sólo caracteriza ruido potencial: no
estima un cambio de onset o magnitud porque el detector productivo no fue
recalculado.

**Reauditoría de procedencia:** el run canónico
`costa-uru-hampel-topology-v3-2020-2025__20260804T235427969874Z__383bc639-c90d`
validó 482 artefactos fuente, reutilizó 481 sin cambios y transformó únicamente
`json/run/summary.json`; todos los hashes finales fueron recalculados.

**Gate de activación pendiente:** implementar el orden por escena previo a la
agregación, validar con eventos y controles independientes, y demostrar que
reduce spikes sin suprimir cambios persistentes ni degradar error temporal o
espacial. Hasta cerrar esas tres condiciones, Hampel sigue siendo diagnóstico
y no un preprocesamiento del pipeline.

## 2. AOI irregular y semántica nodata

El caso rectangular no demuestra que el exterior de una grilla alineada quede
correctamente excluido. Crear fixtures anonimizados que incluyan:

**Progreso parcial verificado:**

- [x] fixture sintético 4 × 4 con polígono irregular y hueco;
- [x] nodata confirmado fuera del footprint en los rasters de perturbaciones;
- [x] exterior y hueco excluidos de estadísticas, CSV y figura QA;
- [x] corrección del conteo periódico que antes incluía celdas exteriores;
- [ ] `MultiPolygon`, bordes que cortan píxeles y cruce de zona UTM.

- polígono irregular no alineado a píxeles;
- polígono con hueco;
- `MultiPolygon`;
- borde que corte píxeles;
- si es necesario, un caso que cruce una zona UTM para verificar el rechazo o
  la estrategia prevista.

Verificar en línea base, serie estacional y perturbaciones:

- nodata fuera de la máscara exacta del AOI;
- cero observaciones dentro del AOI cuando un período esté vacío;
- exterior excluido de conteos, porcentajes, tablas y figuras;
- igualdad de CRS, transformación, dimensiones, bounds y `grid_sha256`;
- ausencia de relleno silencioso en huecos o períodos faltantes.

**Criterio de salida:** pruebas automatizadas que fallen si un píxel exterior
participa de estadísticas o recibe un estado analítico.

## 3. Robustez operativa de CCDC

Probar por separado:

**Progreso parcial verificado:**

- [x] runner con resumen escalar CCDC exitoso;
- [x] degradación a indisponibilidad ante falla CCDC global;
- [x] `raw_array_downloaded: false` en ambas rutas;
- [x] cinco activos base de perturbaciones y colección de eventos verificados
  por hashes;
- [x] mediana temporal segura para slices completamente `NaN`, sin suprimir
  warnings globalmente;
- [ ] cerrar arrays sin segmentos, observaciones insuficientes, huecos largos y
  ruptura temporalmente incompatible como matriz operativa completa.

1. serie sin ruptura poscorte;
2. ruptura poscorte conocida;
3. arrays sin segmentos o vacíos;
4. observaciones insuficientes;
5. huecos largos y nubosidad persistente;
6. falla remota o de construcción del grafo;
7. ruptura incompatible temporalmente con la señal robusta.

Invariantes:

- los arrays crudos permanecen en GEE;
- el resumen escalar no inventa rupturas en arrays vacíos;
- una falla CCDC es indisponibilidad, no desacuerdo;
- el detector robusto puede conservarse sin fingir convergencia;
- errores publicados usan códigos sanitizados y no filtran datos de
  autenticación;
- una ruptura CCDC aislada requiere revisión y no prueba persistencia.

**Criterio de salida:** rutas de éxito, ausencia de ruptura y degradación segura
verificadas con pruebas determinísticas y al menos una ejecución remota
acotada.

## 4. Plan de calibración

Los valores actuales (`z >= 3`, soporte de dos índices y persistencia de dos
períodos) son benchmarks conservadores. No optimizarlos hasta contar con el
protocolo del bloque 1.

Comparar configuraciones sin romper estas reglas:

- separar entrenamiento, ajuste y prueba por espacio;
- reportar métricas por ecorregión y tipo de evento;
- conservar magnitudes continuas, no sólo estados;
- evaluar sensibilidad a cantidad de referencias y observaciones válidas;
- calibrar scores sólo con datos independientes adecuados;
- no fusionar el z-score robusto con `changeProb` CCDC;
- seleccionar umbrales según el costo de falsos negativos y revisión humana;
- registrar versión, semilla, parámetros y procedencia.

**Criterio de salida:** informe de calibración reproducible o decisión
documentada de conservar los benchmarks.

## 5. Consolidar el Paso 15: atribución

El atribuidor determinístico v0 ya está implementado sobre eventos persistentes
y trayectorias RF 2020–2024. Su alcance actual es separar recuperación,
contexto de cosecha forestal, perturbación temporal, agricultura probable y
desconocido sin confundir pérdida de cobertura con conversión.

### 5.1 Paso 15.0 — contrato de atribución

La salida versionada ya consume candidatos de perturbación y preserva:

- bosque o regeneración;
- pastura;
- cultivo;
- suelo desnudo persistente;
- infraestructura;
- agua;
- incendio o perturbación temporal;
- desconocido.

El contrato debe conservar:

- evidencia temporal antes y después;
- fuentes y sensores;
- confianza e incertidumbre;
- explicaciones alternativas;
- flags de calidad;
- motivo de revisión;
- separación entre resultado automático y revisión humana.

`unknown` y `review_required` son resultados válidos. El modelo automático no
puede producir `conversion_confirmed`.

### 5.2 Paso 15.1 — evidencia temporal postcambio

La atribución no puede depender de una única escena. Diseñar ventanas que:

- comiencen después del período de perturbación estimado;
- exijan persistencia del uso posterior;
- toleren faltantes sin interpolarlos;
- detecten recuperación forestal;
- conserven estacionalidad y cantidad de observaciones;
- eviten usar información anterior como si describiera el uso posterior.

Las funciones puras y series sintéticas ya cubren pérdida persistente,
recuperación, cosecha declarada y el gate agrícola independiente. Falta ampliar
la ventana más allá de 2024 cuando existan predictores y validación compatibles.

### 5.3 Paso 15.2 — explicaciones alternativas

Separar evidencia compatible con conversión de:

- incendio;
- sequía;
- inundación;
- defoliación;
- cosecha o rotación temporal;
- error de línea base;
- recuperación o regeneración.

La v0 registra la fuente de cada explicación. El contexto declarado de
plantación es una explicación alternativa no independiente; recuperación
forestal se deriva de la trayectoria RF. Cuando falta evidencia explícita de
uso agrícola el resultado es `unknown/review_required`. Todavía falta integrar
capas independientes de incendio, clima y cobertura postcambio.

### 5.4 Paso 15.3 — agregación atribuida

La segmentación de candidatos persistentes ya existe como producto intermedio:
ocho vecinos, vectorización, área métrica y conservación de componentes menores
a `0,5 ha`. Después de atribuir temporalmente todavía falta:

- intersectar y, cuando corresponda, agregar sólo la porción atribuida de cada
  evento con bosque 2020 y el establecimiento;
- aplicar el umbral regulatorio al resultado atribuido, no al candidato bruto
  ni mediante un conteo fijo de píxeles;
- registrar sensibilidad a bordes, conectividad y resolución;
- conservar vínculo reversible con el evento persistente original.

### 5.5 Paso 15.4 — decisión conservadora

Diseñar una tabla de decisión que requiera simultáneamente:

- evidencia de bosque al corte;
- pérdida poscorte;
- persistencia;
- uso agrícola o ganadero posterior;
- superficie aplicable;
- ausencia de explicación alternativa suficientemente respaldada.

Resultados automáticos permitidos:

- `no_change_detected`;
- `low_risk`;
- `review_required`;
- `conversion_likely`;
- `insufficient_data`.

`conversion_likely` debe conservar toda la evidencia que lo respalda y seguir
pendiente de revisión. `conversion_confirmed` requiere revisión humana
documentada y no pertenece al modelo automático.

### 5.6 Paso 15.5 — materialización compacta

La v0 ya publica el mínimo paquete auditable:

- un JSON de atribución;
- referencias por hash a los rasters RF fuente, sin duplicarlos;
- un vector de eventos;
- una tabla por evento;
- una figura de atribución equivalente.

Permanece pendiente un raster multibanda atribuido cuando exista una semántica
per-píxel validada; la v0 es deliberadamente por evento.

No crear carpetas por índice, modelo o regla. Referenciar productos existentes
por hash en lugar de duplicarlos.

**Criterio de salida del Paso 15:** atribución temporal y espacial verificable,
incertidumbre preservada, eventos medidos y ningún lenguaje de certificación.

## 6. Incrementos posteriores, no inmediatos

El entrypoint secuencial `scripts/run_complete_analysis.py` ya materializa un
envelope auditable sobre los cuatro componentes actuales. Su contrato debe ser
la base de la futura API interna: recibir geometría y parámetros acotados,
ejecutar el mismo orquestador y devolver el estado y las referencias del
expediente, sin duplicar la lógica científica en la capa HTTP.
El envelope ya valida integridad completa de los manifests hijos y diferencia
el `analysis_id` padre de los IDs científicos propios de cada bundle.

Mantener fuera del siguiente incremento hasta cerrar lo anterior:

- Sentinel-2 L2A a 10 m como producto óptico principal;
- Sentinel-1 VH, VV y VH/VV como evidencia independiente;
- comparación sistemática HLS 30 m frente a Sentinel-2 10 m;
- calibración por Chaco Seco, Chaco Húmedo, Espinal, Monte, Yungas y Selva
  Paranaense;
- MapBiomas, OTBN, incendios, clima y capas oficiales adicionales;
- lotes, API interna, caché y control de costos;
- despliegue en nube;
- integración con sistemas institucionales o de trazabilidad animal.

Cada incorporación de datos debe actualizar
[`data/catalog.yml`](data/catalog.yml) y
[`data/licenses.yml`](data/licenses.yml), declarar versión, licencia,
atribución, fecha de acceso y limitaciones.
