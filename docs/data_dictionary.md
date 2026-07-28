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

Los contratos temporales `HlsSeriesMetadata` y
`HlsSeriesCoverageDocument` usan versión `1.0.0`; sus esquemas canónicos son
`hls-series-metadata-v1.0.0.json` y
`hls-series-coverage-v1.0.0.json`.

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

`data/catalog.yml` registra colecciones candidatas verificadas pero todavía no
utilizadas. Su contrato tipado está en `deforestation_pipeline.catalog`.
Catalogar no equivale a acceder: el plan local conserva
`remote_data_accessed: false` y no agrega entradas a `data/licenses.yml`.

HLS v2 se representa mediante sus dos productos constituyentes, HLSL30 y
HLSS30. Cada producto conserva su propio mapeo de bandas hacia los roles rojo,
NIR, SWIR 1, SWIR 2 y calidad.
