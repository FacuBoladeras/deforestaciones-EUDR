"""Contenido estable y versionado de la plantilla del informe cliente."""

from __future__ import annotations

REPORT_TEMPLATE_VERSION = "1.1.0"

EUDR_CONTEXT_PARAGRAPHS = (
    "El Reglamento (UE) 2023/1115 busca evitar que determinadas materias primas y "
    "productos comercializados en la Unión Europea contribuyan a la deforestación o a la "
    "degradación forestal. Para la cadena bovina, la evaluación no termina en una imagen "
    "satelital: integra geolocalización, fecha de corte, legalidad, trazabilidad y debida "
    "diligencia a lo largo de la cadena de suministro.",
    "La fecha de corte es el 31 de diciembre de 2020. La pregunta geoespacial central es si "
    "las tierras vinculadas con la producción registraron después de esa fecha una conversión "
    "de bosque hacia otro uso. Esa pregunta debe responderse con evidencia trazable y sin "
    "confundir una anomalía temporal con una conversión permanente.",
)

DEFORESTATION_FREE_REQUIREMENTS = (
    (
        "Origen geolocalizado",
        "La parcela o unidad productiva debe estar identificada con precisión suficiente para "
        "vincular el producto con el territorio donde se produjo.",
    ),
    (
        "Sin conversión posterior al corte",
        "La tierra vinculada con la producción no debe haber sufrido deforestación después del "
        "31/12/2020. La perturbación temporal o la recuperación no equivalen por sí solas a "
        "deforestación.",
    ),
    (
        "Producción legal",
        "Deben verificarse las leyes pertinentes del país de producción. Este requisito no se "
        "resuelve mediante observación satelital.",
    ),
    (
        "Trazabilidad y debida diligencia",
        "El producto y, cuando corresponda, las materias primas pertinentes usadas en la "
        "alimentación deben poder relacionarse con sus orígenes y documentación respaldatoria.",
    ),
)

FOREST_DEFINITION = (
    "Como marco sectorial de referencia, el Protocolo VISEC Carne adopta una definición de "
    "bosque basada en FAO: tierras de al menos 0,5 ha, con árboles de al menos 5 m y cobertura "
    "de copa de al menos 10 %, o con potencial para alcanzar esos parámetros. Incluye ciertas "
    "cortinas, barreras y corredores arbóreos; excluye tierras de uso predominantemente "
    "agrícola o urbano y los sistemas productivos que el protocolo clasifica como uso agrícola."
)

DEFORESTATION_DEFINITION = (
    "La deforestación se entiende como la conversión de bosque a otro uso de la tierra. Una "
    "pérdida temporal de cobertura no basta: si el bosque afectado se regenera y no existe "
    "transformación hacia otro uso, no corresponde equiparar automáticamente la perturbación "
    "con deforestación. La detección de uso agrícola posterior aporta evidencia de atribución, "
    "pero sigue requiriendo evaluación conjunta y revisión."
)

CUTOFF_DEFINITION = (
    "La fecha de corte separa los cambios anteriores de aquellos relevantes para la evaluación "
    "posterior: 31/12/2020. El umbral de 0,5 ha forma parte de la definición de bosque y de la "
    "referencia operativa de este análisis; NO constituye un mínimo legal por debajo del cual "
    "una conversión pueda ignorarse. Por eso los candidatos subumbrales se conservan."
)

REPORT_SCOPE_PARAGRAPHS = (
    "Este informe presenta evidencia geoespacial sobre cambios de cobertura posteriores "
    "al 31 de diciembre de 2020 dentro de la geometría declarada del establecimiento. Su "
    "objetivo es ordenar la evidencia disponible para una revisión técnica y humana.",
    "El documento distingue tres niveles que no deben confundirse: la detección de un "
    "cambio, la atribución de evidencia compatible con conversión y la decisión humana "
    "documentada. Ninguna salida automática reemplaza verificaciones legales, documentales "
    "o de trazabilidad.",
)

CONCEPTUAL_SOURCE_NOTE = (
    "Marco conceptual elaborado a partir del Reglamento (UE) 2023/1115 y del Protocolo VISEC "
    "Carne Libre de Deforestación Argentina, octubre de 2025, suministrado como referencia. "
    "La síntesis es editorial y no reproduce una evaluación VISEC, no implica adhesión ni "
    "sustituye la consulta de las normas y documentos fuente vigentes."
)

METHOD_INTRODUCTION = (
    "La evaluación sigue una secuencia conservadora. Cada etapa conserva su trazabilidad y "
    "una ausencia de datos se informa como no evaluada; nunca se interpreta como evidencia "
    "negativa ni como superficie cero inferida."
)

METHOD_STEPS = (
    (
        "1. Geometría declarada",
        "Se valida el perímetro, se normaliza su representación y se calcula la superficie "
        "en un sistema de referencia métrico apropiado.",
    ),
    (
        "2. Bosque de referencia 2020",
        "Se estima el dominio forestal al cierre del 31/12/2020 mediante evidencia "
        "multifuente y clasificación automática no calibrada.",
    ),
    (
        "3. Cambios posteriores",
        "Se localizan perturbaciones posteriores al corte y se analiza su trayectoria "
        "temporal, persistencia o recuperación.",
    ),
    (
        "4. Uso posterior",
        "Se evalúa ocurrencia agrícola posterior mediante Dynamic World. Esta evidencia "
        "satisface sólo el criterio de evidencia agrícola y no confirma conversión por sí sola.",
    ),
    (
        "5. Conjunción espacial",
        "Se materializa la intersección entre bosque de referencia, pérdida, persistencia y "
        "evidencia agrícola, y se calcula su superficie con geometría local.",
    ),
    (
        "6. Interpretación",
        "Los resultados se clasifican como candidatos o eventos probables y permanecen "
        "sujetos a revisión humana documentada.",
    ),
)

INTERPRETATION_INTRODUCTION = (
    "La lectura del resultado debe considerar conjuntamente la evidencia que lo respalda, "
    "sus límites y las verificaciones que quedan fuera del expediente geoespacial."
)

SUPPORTED_INTERPRETATIONS = (
    "La ubicación y superficie de los cambios conservados en el inventario.",
    "La trayectoria temporal observada dentro del período analizado.",
    "La convergencia espacial entre bosque de referencia, pérdida persistente y uso posterior "
    "cuando los componentes correspondientes están disponibles.",
)

OUT_OF_SCOPE_INTERPRETATIONS = (
    "La legalidad de la producción o de la tenencia de la tierra.",
    "La trazabilidad de animales, productos o transacciones.",
    "Una certificación, una declaración de debida diligencia o una conclusión legal EUDR.",
    "La sustitución de inspección de campo, documentación independiente o revisión humana.",
)
