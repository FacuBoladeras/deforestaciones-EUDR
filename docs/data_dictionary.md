# Diccionario de datos

## Versionado

El contrato del resumen por establecimiento comienza en la versión `1.0.0`.
El archivo canónico es `data/schemas/analysis-summary-v1.0.0.json` y se deriva
del modelo `AnalysisSummary`.

Todo cambio incompatible exige un nuevo archivo de esquema y una decisión
explícita de migración. `analysis_version` identifica el código o modelo usado;
no reemplaza la versión del esquema.

La identidad espacial temporal usa un contrato independiente,
`RasterGridSpec` 1.0.0, cuyo archivo canónico es
`data/schemas/raster-grid-v1.0.0.json`. Separar ambos contratos evita confundir
el formato del resumen de evaluación con la grilla de los productos raster.

`HlsSeriesMetadata` usa versión `2.0.0` por la migración incompatible de rutas
al bundle plano; su esquema canónico es
`hls-series-metadata-v2.0.0.json`. `HlsSeriesCoverageDocument` conserva la
versión `1.0.0` y el esquema `hls-series-coverage-v1.0.0.json`, porque su
contenido científico no cambió. El esquema de metadatos `1.0.0` se conserva
como registro histórico.

Los contratos del Paso 12.0 son `TemporalWindowPlan` y `TemporalCubeSpec`,
ambos `1.0.0`. Sus esquemas canónicos son
`temporal-window-plan-v1.0.0.json` y `temporal-cube-spec-v1.0.0.json`.

La materialización estacional agrega `HlsSeasonalMetadata` 1.0.0 y
`TemporalCubeIndex` 1.0.0, definidos respectivamente en
`hls-seasonal-metadata-v1.0.0.json` y
`temporal-cube-index-v1.0.0.json`. El índice usa almacenamiento
`virtual_geotiff_collection`: las dimensiones del cubo se resuelven sobre los
GeoTIFF planos sin duplicar los valores.

`SeasonalCoverageDocument` 1.0.0 registra el QA observacional por período y
sensor sin convertir cobertura en riesgo ambiental. Su esquema es
`hls-seasonal-coverage-v1.0.0.json`.

El Paso 13 agrega `ForestBaselineConfig` 1.0.0, cuyo esquema canónico es
`forest-baseline-v1.0.0.json`. Este contrato define fecha de referencia,
ventana pre-corte, definición forestal, resolución benchmark y políticas de
conservación de incertidumbre.

`ForestBaselineFeatureMetadata` registra los composites HLS de años calendario
exactos, las bandas de atributos, el conteo de observaciones y la ausencia de
clasificación. `ForestBaselineProductMetadata` declara qué fuentes son núcleo
o apoyo, que el score es `uncalibrated_core_evidence_fraction` y que no se
generó una evaluación final.

El contrato de perturbaciones evoluciona a `DisturbanceDetectionConfig` 1.4.0
y al esquema `disturbance-detection-v1.4.0.json`. Fija estados y códigos de
calidad, declara los detectores y materializa dos rasters multibanda desde
14.6.

El submodelo `RobustSeasonalDetectorConfig` 1.1.0 versiona el mínimo de
referencias, la constante MAD, la dirección del cambio, la comparación por
igual estación, la política de escala cero, la preservación de faltantes, el
umbral benchmark `3.0`, el soporte mínimo de dos índices y la persistencia
mínima de dos períodos consecutivos. Sus parámetros participan del hash
científico y el umbral requiere calibración independiente futura.

`CcdcBenchmarkConfig` 1.0.0 conserva el perfil HLSL30-only, la colección densa
enmascarada, las bandas de ruptura, todos los argumentos oficiales de CCDC y
la desactivación de TMask cuando Fmask ya fue aplicado. El perfil combinado
HLSL30+HLSS30 queda declarado pero deshabilitado.

`CcdcBenchmarkResult` resume arrays sintéticos o materializados externamente
sin cambiar su semántica. Conserva elegibilidad y ajuste, primera ruptura
poscorte, índice del segmento, magnitud firmada, magnitud con caída positiva,
observaciones y `ccdc_breakpoint_pseudo_probability`. Este último campo
representa `changeProb`, una pseudoprobabilidad algorítmica de que la ruptura
sea real; no es una probabilidad de deforestación, conversión ni riesgo EUDR.
Los arrays del resultado local son inmutables.

`DetectorConvergenceConfig` 1.0.0 fija la comparación temporal exacta entre
detectores, la política de indisponibilidad y la conservación obligatoria del
desacuerdo. `DetectorConvergenceResult` conserva estado y motivo, detectores
disponibles y concordantes, soporte booleano, índices y tiempos originales,
evidencia robusta, pseudoprobabilidad y magnitud CCDC, y el OR de los bitmasks.
No existe un score combinado ni una probabilidad conjunta.

`RobustSeasonalDiagnostics` es una estructura local inmutable con matrices
`[período, índice, y, x]`: `reference_center`, `robust_scale`,
`raw_directional_delta`, `standardized_magnitude`,
`reference_valid_count` y `quality_flags_bitmask`. Son diagnósticos continuos,
no probabilidades ni estados de perturbación. `RobustSeasonalSignalResult`
aplica después la regla 14.3 sin recalcular estos diagnósticos y conserva el
primer índice de período con señal, magnitud máxima, racha máxima, soporte
multíndice, observaciones válidas, score no calibrado y bitmask. Los Pasos
14.2 y 14.3 mantienen estas operaciones puras; 14.6 selecciona sus diagnósticos
auditables para los dos GeoTIFF compactos.

Ambos GeoTIFF compactos usan la huella válida de la línea base como máscara:
fuera del AOI todos los canales son nodata y esos píxeles no participan de las
estadísticas JSON. Sus tags conservan `grid_sha256`. El rango evaluable omite
explícitamente cualquier estación que cruce el 31/12/2020, aunque el CSV la
retenga como período excluido para trazabilidad.

`DisturbanceStateCode` representa estados de detección, no clases de uso del
suelo. `PERSISTENT_CANDIDATE` significa que una señal merece continuar hacia
atribución; nunca equivale por sí mismo a `conversion_likely`.

El campo `disturbance_score` tiene semántica
`uncalibrated_disturbance_evidence_score`. No debe renombrarse como
probabilidad sin calibración independiente y versionado del contrato.

## Grilla raster temporal

`RasterGridSpec` conserva CRS métrico, resolución, dimensiones, transformación
afín, bounds, orientación, estrategia de alineación, nodata y la obligación de
aplicar la máscara exacta del AOI.

`grid_sha256` identifica únicamente los componentes espaciales. Excluye nodata
y la máscara porque ambos pertenecen a la codificación o validez del producto,
no a la correspondencia espacial entre píxeles.

El modelo valida que una grilla sea norte-arriba, sin rotación, y que sus
bounds coincidan con la transformación, el ancho y el alto. La derivación local
se describe en `docs/temporal_grid.md`.

## Serie anual HLS

`HlsSeriesMetadata` fija el rango inclusivo, años materializados, hash de
grilla, fuentes HLS, método de composite, presupuesto estimado y rutas de cada
producto anual. `final_assessment_generated` es siempre `false`.

`HlsSeriesCoverageDocument` informa por año el número de escenas y, dentro del
ROI exacto, cantidad y fracción de píxeles observados para el total, HLSL30 y
HLSS30. También conserva percentiles de conteos y banderas de calidad.

Los estados `complete_observed_coverage`, `partial_observed_coverage` y
`no_observed_coverage` describen disponibilidad de observaciones. NO son
estados de riesgo, cambio o cumplimiento.

## Entrada de establecimiento

`EstablishmentInput` conserva el identificador, la geometría declarada, su CRS,
fuente y versión. En este paso sólo se valida la estructura. La topología, la
ubicación en Argentina, las reparaciones y el cálculo de superficie pertenecen
a la etapa de preparación espacial.

## Resumen de análisis

Los campos de superficie se expresan en hectáreas y no pueden ser negativos.
`likely_conversion_area_ha` no puede superar `detected_change_area_ha`.

Los estados externos de la versión `1.0.0` son:

- `low_risk`: evidencia consistente con ausencia de conversión;
- `review_required`: alerta, desacuerdo o información insuficiente para una
  conclusión automática;
- `conversion_likely`: evidencia automática fuerte, pendiente de revisión;
- `insufficient_data`: geometría u observaciones inadecuadas.

El modelo automático no puede emitir `conversion_confirmed`. La confirmación
requiere una revisión humana documentada y un contrato posterior que la
represente explícitamente.

### Validación semántica previa a la exportación

`AnalysisSummary` es el contrato de transporte: valida tipos, rangos y algunas
relaciones estructurales. Antes de exportar evidencia, el productor automático
debe pasar esa instancia por `validate_analysis_summary`:

```python
from deforestation_pipeline.result_validation import validate_analysis_summary

summary = validate_analysis_summary(summary)
```

Esta frontera de dominio rechaza identificadores repetidos, eventos fuera del
período analizado, contradicciones entre estado y superficie probable, y
conversiones atribuidas a regeneración, perturbación temporal o uso
desconocido. Devuelve la misma instancia cuando es válida; si detecta problemas,
`AnalysisSummaryValidationError` expone todas las violaciones legibles en
`violations`.

Una superficie agregada de conversión probable debe estar respaldada por al
menos un evento con conversión probable, y viceversa. Esta correspondencia
valida la existencia de soporte, no la igualdad numérica de las superficies.

Los totales del resumen no se comparan con una suma directa de eventos. Los
eventos pueden solaparse espacialmente y esa suma inflaría la superficie. La
agregación espacial auditable pertenece a una etapa posterior.

## Fechas y procedencia

La fecha de corte ambiental es invariable: `2020-12-31`. `created_at` y la
fecha de registro de la geometría requieren zona horaria.

Cada dataset utilizado debe registrar proveedor, colección, versión, licencia,
fecha de acceso, atribución y restricciones. `data/licenses.yml` no debe
rellenarse con fuentes que todavía no hayan sido verificadas e incorporadas.

## Catálogo de fuentes candidatas

`data/catalog.yml` registra colecciones verificadas y su contrato tipado está
en `deforestation_pipeline.catalog`. Catalogar no equivale a acceder: los
planes locales conservan `remote_data_accessed: false`; una corrida remota
registra por separado los datasets efectivamente utilizados.

HLS v2 se representa mediante sus dos productos constituyentes, HLSL30 y
HLSS30. Cada producto conserva su propio mapeo de bandas hacia los roles rojo,
NIR, SWIR 1, SWIR 2 y calidad.

La familia `FOREST_BASELINE` registra JRC GFC2020 V3 y ESA WorldCover 2020
como fuentes núcleo independientes, y Hansen GFC v1.13 como evidencia derivada
de apoyo. Las reglas de binarización son datos versionados del catálogo, no
condicionales ocultos en el runner.
