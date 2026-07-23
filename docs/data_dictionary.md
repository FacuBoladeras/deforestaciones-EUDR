# Diccionario de datos

## Versionado

El contrato del resumen por establecimiento comienza en la versión `1.0.0`.
El archivo canónico es `data/schemas/analysis-summary-v1.0.0.json` y se deriva
del modelo `AnalysisSummary`.

Todo cambio incompatible exige un nuevo archivo de esquema y una decisión
explícita de migración. `analysis_version` identifica el código o modelo usado;
no reemplaza la versión del esquema.

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

## Fechas y procedencia

La fecha de corte ambiental es invariable: `2020-12-31`. `created_at` y la
fecha de registro de la geometría requieren zona horaria.

Cada dataset utilizado debe registrar proveedor, colección, versión, licencia,
fecha de acceso, atribución y restricciones. `data/licenses.yml` no debe
rellenarse con fuentes que todavía no hayan sido verificadas e incorporadas.
