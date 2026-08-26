# Guía de interpretación de perturbaciones y eventos

## 1. Propósito

Este documento define el vocabulario y las reglas de decisión para interpretar
señales de cambio dentro del bosque analizado por el pipeline. Su objetivo es
evitar que una anomalía espectral, un píxel aislado o una perturbación temporal
se comuniquen como deforestación.

La guía es un contrato científico y editorial. Debe ser aplicada por el núcleo
analítico, los contratos JSON/GeoJSON, la API y el informe PDF. No constituye
una certificación legal ni habilita al modelo automático a emitir
`conversion_confirmed`.

## 2. Principio rector

Una anomalía espectral **no es un evento**. El término `evento` queda reservado
para una conversión al menos probable, respaldada conjuntamente por bosque en
la fecha de corte, pérdida posterior, persistencia sin recuperación, uso
agrícola o ganadero posterior y coherencia espacial.

Toda señal que no complete esa cadena permanece como candidato auditable.

## 3. Jerarquía de interpretación

### Nivel 1 — `spectral_candidate`

Señal espectral localizada que supera los criterios versionados de detección.

- Puede abarcar uno o varios píxeles.
- Puede corresponder a ruido residual, fenología, sequía, incendio, sombra,
  degradación, manejo forestal o pérdida real.
- Se conserva en los productos técnicos.
- No modifica por sí sola el estado del establecimiento.
- No debe denominarse evento en contratos ni productos para clientes.

### Nivel 2 — `candidate_episode`

Agrupación espacial y temporal coherente de candidatos espectrales.

- Sus miembros deben tener proximidad, período de inicio y trayectoria
  compatibles.
- El área representa únicamente píxeles respaldados; no incluye huecos creados
  por envolventes, buffers o polígonos convexos.
- Puede resolverse como recuperación, perturbación temporal, cambio persistente
  no atribuido o datos insuficientes.
- Continúa siendo candidato mientras no complete todos los criterios de
  conversión probable.

### Nivel 3 — `conversion_likely_event`

Único nivel que puede comunicarse como evento automático. Requiere que todos
los siguientes criterios sean verdaderos:

1. `forest_at_cutoff`: bosque respaldado al 31 de diciembre de 2020;
2. `post_cutoff_loss`: pérdida observada después de la fecha de corte;
3. `persistent_change`: cambio persistente durante la ventana definida;
4. ausencia de recuperación forestal respaldada;
5. `agricultural_or_livestock_post_use`: uso agrícola, pastura o
   infraestructura ganadera posterior respaldado;
6. `defensible_area_and_geometry`: área y geometría espacial defendibles;
7. `strong_alternative_explanation_absent`: ninguna explicación alternativa
   fuerte permanece activa.

La decisión automática resultante es `conversion_likely`. La confirmación
`conversion_confirmed` requiere revisión humana documentada.

### 3.1 Dos umbrales de 0,5 ha que no deben confundirse

El umbral EUDR de **0,5 ha** forma parte de la definición de la superficie que
puede constituir bosque. No establece un tamaño mínimo legal de conversión y
no autoriza a descartar pérdidas menores.

Separadamente, el protocolo VISEC usa una referencia operativa de **más de
0,5 ha** para presentar una pérdida conectada. Este proyecto adopta esa
convención de forma explícita y conservadora para la publicación automática de
`conversion_likely_event`:

- la relación es estricta: el área debe ser `> 0.5`, no `>= 0.5`;
- se mide sobre la geometría materializada de
  `bosque 2020 ∩ pérdida poscorte ∩ persistencia ∩ uso agrícola posterior`;
- no se mide sobre la huella completa del candidato;
- una conjunción `<= 0.5 ha` se conserva como
  `subthreshold_conversion_evidence`, permanece en `candidates` y exige
  `review_required`;
- esta regla es una política operativa de presentación inspirada en VISEC, no
  una conclusión jurídica de que una conversión menor sea irrelevante para la
  EUDR.

## 4. Resolución de candidatos

Los candidatos que no alcanzan el nivel de evento se clasifican sin perderse:

| Resolución | Interpretación | Revisión humana |
| --- | --- | --- |
| `candidate_only` | Señal sin persistencia suficiente | No por defecto |
| `temporary_or_recovered` | Perturbación temporal o recuperación observada | No por defecto |
| `persistent_unattributed` | Cambio persistente sin uso posterior demostrable | Sí |
| `insufficient_data` | Soporte temporal o espacial insuficiente | Sí |
| `conversion_likely` | Todos los criterios conjuntivos satisfechos | Sí |

Un establecimiento no pasa a `review_required` por la mera existencia de
candidatos resueltos como `candidate_only` o `temporary_or_recovered`.

## 5. Contexto espacial

La forma y la posición modifican la plausibilidad, pero nunca sustituyen la
atribución temporal y temática.

### Patrones relevantes

- **Expansión de frontera:** el cambio prolonga una superficie agrícola
  preexistente sobre el bosque.
- **Conexión entre lotes:** el cambio elimina o atraviesa una franja forestal
  entre dos superficies agrícolas.
- **Apertura interior:** el cambio comienza dentro del bosque. No se descarta,
  pero requiere evidencia temporal y de uso posterior más fuerte.
- **Salpicado forestal:** componentes pequeños, dispersos y sin trayectoria
  común. Permanecen como candidatos y no se agregan solamente por distancia.

### Variables espaciales recomendadas

- distancia al borde bosque–agricultura previo al cambio;
- proporción del perímetro en contacto con agricultura preexistente;
- conectividad por lados y existencia de un núcleo espacial;
- compactación, elongación y ancho mínimo;
- coherencia temporal entre componentes próximos;
- proporción del área con uso agrícola persistente posterior;
- patrón de puente entre superficies agrícolas.

La cercanía al borde es evidencia contextual, no una condición obligatoria ni
suficiente. Los efectos de borde también pueden producir anomalías forestales
sin conversión.

## 6. Reglas de agrupación

- No agrupar exclusivamente por distancia.
- No agrupar componentes de años diferentes por mera proximidad.
- Exigir compatibilidad de inicio, dirección espectral y trayectoria posterior.
- Mantener la relación entre cada episodio y sus candidatos de origen.
- Sumar solamente el área observada de los miembros.
- Versionar distancias, ventanas temporales, conectividad y métricas de forma.
- Calibrar los umbrales espaciales con casos etiquetados antes de utilizarlos
  como filtros decisorios.

La agregación `disturbance_events` `1.2.0` aplica una ventana versionada de
coherencia de inicio. Dentro de un componente espacial, ningún episodio puede
abarcar índices de inicio cuya diferencia exceda
`maximum_onset_period_difference`. Los píxeles sin inicio estimable se
conservan, pero se agrupan en episodios desconocidos separados; no actúan como
puente entre inicios incompatibles.

## 7. Comunicación del resultado

El informe debe separar siempre:

1. candidatos espectrales observados;
2. episodios candidatos coherentes;
3. eventos de conversión probable.

Ejemplo sin eventos:

> Se observaron perturbaciones espectrales dentro del bosque analizado, pero
> ninguna reunió persistencia, ausencia de recuperación y uso agrícola o
> ganadero posterior suficientes para constituir un evento de conversión
> probable.

El mapa puede mostrar candidatos para mantener transparencia, usando una
simbología distinta y menos prominente que los eventos probables.

## 8. Invariantes de implementación

- En el contrato post-atribución, `event_count` cuenta exclusivamente
  `conversion_likely_event`.
- `candidate_count` incluye toda señal o episodio que no alcanzó el nivel de
  evento.
- `likely_conversion_area_ha` se calcula únicamente sobre la conjunción
  espacial materializada de todos los criterios.
- `candidate_area_ha` conserva la huella original y
  `conjunctive_conversion_evidence_area_ha` conserva la superficie conjuntiva
  aun cuando no supere el umbral operativo.
- La geometría de un `conversion_likely_event` es la geometría vectorizada de
  la conjunción; `candidate_geometry` conserva por separado la huella de
  origen.
- Un candidato recuperado nunca aporta área de conversión probable.
- Un candidato sin uso agrícola o ganadero posterior nunca es un evento.
- Todos los candidatos y sus razones de exclusión permanecen auditables.
- Un presupuesto operativo de consulta puede particionar el procesamiento en
  lotes determinísticos, pero nunca truncar el inventario ni descartar
  candidatos subumbrales antes de evaluar persistencia y atribución.
- El modelo automático nunca emite `conversion_confirmed`.

### 8.1 Compatibilidad técnica durante la migración

Los artefactos científicos anteriores a esta guía conservan rutas históricas
como `disturbance_events.json` e identificadores `PDE-*`. En los runs nuevos,
cada registro de esa etapa declara además `candidate_id`,
`record_type: persistent_disturbance_candidate` e
`interpretation_level: candidate_episode`; el nombre físico legado no le
otorga semántica de evento. `candidate_count` y
`above_visec_area_reference_candidate_count` son los contadores canónicos. El
campo técnico `event_count` se conserva por compatibilidad con la ruta física;
los aliases `operational_event_*` y `*_area_threshold_event_*` ya no se publican
en `1.2.0` y no autorizan lenguaje de evento. El contrato
científico post-atribución `5.0.0`, la API,
el cliente web y el PDF son la frontera autoritativa: sólo su colección
`events` contiene `conversion_likely_event`.

El contrato `5.0.0` registra además qué fuentes de explicaciones alternativas
fueron realmente evaluadas. Incendio, inundación, sequía o evidencia de campo
se declaran como no evaluadas cuando el run no aporta esos datos; su ausencia
no se inventa como evidencia negativa.

## 9. Caso de referencia `nativo`

El run de robustez del 21 de agosto de 2026 produjo 34 componentes candidatos.
Ninguno presentó simultáneamente persistencia y uso agrícola o ganadero
posterior; trece mostraron recuperación y el área de conversión probable fue
cero. Bajo esta guía, el resultado esperado es:

- 34 candidatos conservados;
- 0 eventos de conversión probable;
- sin atribuir deforestación a las perturbaciones observadas.

Este caso funciona como regresión semántica, no como conjunto suficiente para
calibrar umbrales científicos generales.
