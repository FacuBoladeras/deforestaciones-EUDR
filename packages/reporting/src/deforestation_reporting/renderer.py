"""Renderer ReportLab determinístico del informe técnico para clientes."""

from __future__ import annotations

import hashlib
import html
from io import BytesIO
from pathlib import Path
from typing import Any

import reportlab
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    Image as ReportImage,
)

from deforestation_reporting.basemap import OSMBasemapProvider, OverpassOSMProvider
from deforestation_reporting.graphics import (
    agricultural_map_bounds,
    agricultural_support_map,
    baseline_area_chart,
    events_overview_map,
    forest_trajectory_chart,
    sensitivity_chart,
)
from deforestation_reporting.models import (
    LEGAL_DISCLAIMER,
    ClientFigure,
    DatasetSummary,
    EventSummary,
    ReportArtifact,
    ReportViewModel,
)
from deforestation_reporting.source import ReportPackage, build_report_view_model

_PORTRAIT_PAGE = A4
_LANDSCAPE_PAGE = landscape(A4)
_BLUE = colors.HexColor("#17365D")
_GREEN = colors.HexColor("#2F6B4F")
_AMBER = colors.HexColor("#A65F00")
_RED = colors.HexColor("#991B1B")
_LIGHT_BLUE = colors.HexColor("#EAF1F8")
_LIGHT_GREEN = colors.HexColor("#EAF7EF")
_LIGHT_AMBER = colors.HexColor("#FFF7E6")
_LIGHT_GRAY = colors.HexColor("#F3F4F6")
_TEXT = colors.HexColor("#1F2937")
_MUTED = colors.HexColor("#6B7280")
_FONT_REGULAR = "ReportBody"
_FONT_BOLD = "ReportBody-Bold"


def _register_fonts() -> None:
    fonts_root = Path(reportlab.__file__).resolve().parent / "fonts"
    if _FONT_REGULAR not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT_REGULAR, fonts_root / "Vera.ttf"))
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, fonts_root / "VeraBd.ttf"))
        pdfmetrics.registerFontFamily(
            _FONT_REGULAR,
            normal=_FONT_REGULAR,
            bold=_FONT_BOLD,
            italic=_FONT_REGULAR,
            boldItalic=_FONT_BOLD,
        )


_register_fonts()


def render_technical_report(
    package: ReportPackage,
    output_path: Path,
    *,
    osm_basemap_provider: OSMBasemapProvider | None = None,
) -> ReportArtifact:
    """Renderiza el informe cliente sin mutar el expediente científico verificado."""
    view = build_report_view_model(package)
    output = output_path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    basemap_provider = osm_basemap_provider or OverpassOSMProvider(
        cache_dir=output.parent / ".osm-cache"
    )
    temporary = output.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    try:
        document = BaseDocTemplate(
            str(temporary),
            pagesize=_PORTRAIT_PAGE,
            leftMargin=1.7 * cm,
            rightMargin=1.7 * cm,
            topMargin=1.8 * cm,
            bottomMargin=1.8 * cm,
            title=view.title,
            author="Deforestation Evidence Pipeline",
            subject="Evidencia técnica geoespacial sujeta a revisión humana",
        )
        document.addPageTemplates(
            [
                _page_template("cover", view, cover=True, pagesize=_PORTRAIT_PAGE),
                _page_template("content", view, cover=False, pagesize=_PORTRAIT_PAGE),
                _page_template(
                    "landscape",
                    view,
                    cover=False,
                    pagesize=_LANDSCAPE_PAGE,
                ),
            ]
        )
        document.build(
            _story(view, basemap_provider),
            canvasmaker=_deterministic_canvas,
        )
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return ReportArtifact(path=output, sha256=_sha256(output), size_bytes=output.stat().st_size)


def _story(view: ReportViewModel, osm_basemap_provider: OSMBasemapProvider) -> list[Any]:
    styles = _styles()
    story: list[Any] = [
        Spacer(1, 1.35 * cm),
        Paragraph(view.title, styles["cover_title"]),
        Spacer(1, 0.45 * cm),
        Paragraph(
            f"Establecimiento: <b>{_escape(view.analysis.establishment_id)}</b>",
            styles["cover_subtitle"],
        ),
        Spacer(1, 0.25 * cm),
        Paragraph(f"ID de análisis: {_escape(view.analysis.analysis_id)}", styles["cover_meta"]),
        Paragraph(
            f"Período analizado hasta: {view.analysis.analysis_end_date.isoformat()}",
            styles["cover_meta"],
        ),
        Spacer(1, 0.45 * cm),
        Paragraph("Qué clase de informe es", styles["cover_science_heading"]),
        Paragraph(
            "Este informe reúne evidencia geoespacial para apoyar la revisión de cambios "
            "de cobertura posteriores al 31/12/2020. Parte de una línea base de cobertura "
            "forestal al 31/12/2020, analiza series multitemporales para identificar cambios "
            "persistentes y evalúa el uso posterior mediante convergencia de evidencias. "
            "Sus resultados expresan estimaciones científicas sujetas a incertidumbre y "
            "revisión humana.",
            styles["cover_science"],
        ),
        Spacer(1, 0.45 * cm),
        Paragraph(
            f"Resultado automático: <b>{_result_status_label(view.result_status)}</b>",
            styles["cover_status"],
        ),
        Spacer(1, 0.45 * cm),
        Paragraph(
            "Resultado técnico automático. Toda alerta y toda evidencia compatible con "
            "conversión requieren revisión humana documentada.",
            styles["cover_note"],
        ),
        Spacer(1, 0.35 * cm),
        Paragraph(LEGAL_DISCLAIMER, styles["disclaimer"]),
        Spacer(1, 0.45 * cm),
        Paragraph("Índice de contenidos", styles["cover_index_heading"]),
        _section_index_table(view, styles),
        NextPageTemplate("content"),
        PageBreak(),
    ]
    story.extend(_executive_section(view, styles))
    story.extend(_events_overview_section(view, styles))
    story.extend(_global_evidence_sections(view, styles))
    for event in view.events:
        if event.selected_for_detail:
            story.extend(
                _event_sections(
                    view,
                    event,
                    _event_client_figure(view, event),
                    styles,
                    osm_basemap_provider,
                )
            )
    story.extend(_quality_section(view, styles))
    story.extend(_limitations_section(view, styles))
    return story


def _executive_section(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> list[Any]:
    result_text = (
        "Se identificó al menos un evento con evidencia automática compatible con "
        "conversión de bosque a uso agrícola. El expediente requiere revisión humana."
        if (view.metrics.likely_conversion_area_ha or 0) > 0
        else "No se generó una conclusión automática final; el expediente conserva los "
        "eventos y limitaciones que deben revisarse."
    )
    return [
        Paragraph("1. Resumen ejecutivo", styles["heading"]),
        Paragraph("Cómo leer este informe", styles["subheading"]),
        Paragraph(
            "El documento separa la línea base forestal, los cambios detectados y la "
            "atribución del uso posterior. Las superficies y figuras son evidencia técnica "
            "automática: orientan la revisión, pero no sustituyen una conclusión documentada.",
            styles["body"],
        ),
        Paragraph(result_text, styles["lead"]),
        Spacer(1, 0.2 * cm),
        _metrics_summary_table(view, styles),
        Spacer(1, 0.55 * cm),
        Paragraph("Cobertura forestal al corte del 31/12/2020", styles["subheading"]),
        baseline_area_chart(view.baseline, width=15.3 * cm, height=4.0 * cm),
        Spacer(1, 0.15 * cm),
        Paragraph(
            "La superficie declarada se calculó sobre la geometría vectorial. Las categorías "
            "forestales se midieron sobre una grilla raster; por eso sus denominadores no se "
            "mezclan para producir un porcentaje único.",
            styles["fine_left"],
        ),
    ]


def _events_overview_section(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> list[Any]:
    return [
        PageBreak(),
        Paragraph("2. Cambios detectados después de la fecha de corte", styles["heading"]),
        Paragraph(
            "El mapa muestra la ubicación relativa de todos los eventos persistentes. Los "
            "colores expresan su interpretación automática y no una confirmación legal.",
            styles["body"],
        ),
        Paragraph("Referencia espacial", styles["subheading"]),
        Paragraph(
            "El perímetro declarado se usa como contexto local. Coordenadas WGS 84: la "
            "grilla, la flecha norte y la escala aproximada complementan la lectura "
            "espacial del inventario.",
            styles["body"],
        ),
        events_overview_map(
            view.events,
            establishment_geometry=view.establishment_geometry,
            width=15.3 * cm,
            height=8.4 * cm,
        ),
        Spacer(1, 0.35 * cm),
        Paragraph("Inventario completo de eventos", styles["subheading"]),
        _event_inventory_table(view.events, styles),
        Spacer(1, 0.2 * cm),
        Paragraph(
            "Los eventos menores a 0,5 ha se conservan como evidencia intermedia, pero no "
            "satisfacen por sí solos el umbral informativo de superficie.",
            styles["fine_left"],
        ),
    ]


def _global_evidence_sections(
    view: ReportViewModel, styles: dict[str, ParagraphStyle]
) -> list[Any]:
    figures = [
        figure
        for figure in view.client_figures
        if figure.kind in {"annual_forest_change", "rgb_timeline"}
    ]
    if not figures:
        return []
    result: list[Any] = [NextPageTemplate("landscape")]
    for figure in figures:
        for page in _global_figure_pages(figure, view, styles):
            result.append(PageBreak())
            result.extend(page)
    result.append(NextPageTemplate("content"))
    return result


def _global_figure_pages(
    figure: ClientFigure,
    view: ReportViewModel,
    styles: dict[str, ParagraphStyle],
) -> list[list[Any]]:
    if figure.kind == "annual_forest_change":
        return _annual_forest_pages(figure, styles)
    return _rgb_timeline_pages(figure, view.seasonal_period_ids, styles)


def _annual_forest_pages(
    figure: ClientFigure, styles: dict[str, ParagraphStyle]
) -> list[list[Any]]:
    with PILImage.open(figure.absolute_path) as source_image:
        source = source_image.convert("RGB")
        width, height = source.size
        column_width = width / 5
        annual_panels = [
            (
                source.crop(
                    (
                        round(index * column_width + column_width * 0.02),
                        round(height * 0.045),
                        round((index + 1) * column_width - column_width * 0.02),
                        round(height * 0.405),
                    )
                ),
                f"Cobertura automática {2020 + index}",
            )
            for index in range(5)
        ]
        transition_panels = [
            (
                source.crop(
                    (
                        round(index * column_width + column_width * 0.02),
                        round(height * 0.465),
                        round((index + 1) * column_width - column_width * 0.02),
                        round(height * 0.845),
                    )
                ),
                f"{2020 + index} respecto de 2020",
            )
            for index in range(1, 5)
        ]
    annual_grid = _compose_panel_grid(annual_panels, columns=3)
    transition_grid = _compose_panel_grid(transition_panels, columns=2)
    return [
        [
            Paragraph(
                "2.1.a. Cobertura forestal anual y pérdidas candidatas: clases",
                styles["landscape_heading"],
            ),
            Paragraph(
                "Cada panel representa la clasificación forestal automática del mismo "
                "establecimiento entre 2020 y 2024. Se amplían los paneles y se reconstruyen "
                "las leyendas para evitar las superposiciones de la figura científica original.",
                styles["landscape_body"],
            ),
            _pil_report_image(annual_grid, max_width=26.2 * cm, max_height=12.7 * cm),
            _rf_legend("annual", width=26.2 * cm),
            Paragraph(
                "Lectura técnica: la clase bosque es binaria y no constituye una probabilidad "
                "calibrada. La comparación anual sirve para localizar cambios persistentes.",
                styles["landscape_caption"],
            ),
        ],
        [
            Paragraph(
                "2.1.b. Pérdidas candidatas respecto de 2020",
                styles["landscape_heading"],
            ),
            Paragraph(
                "Los cuatro paneles comparan 2021-2024 contra la línea base 2020. El panel "
                "interno de votos candidatos se excluye del informe cliente porque no aporta "
                "una lectura final y producía una leyenda superpuesta.",
                styles["landscape_body"],
            ),
            _pil_report_image(transition_grid, max_width=26.2 * cm, max_height=12.7 * cm),
            _rf_legend("transition", width=26.2 * cm),
            Paragraph(
                "Lectura técnica: el rojo indica una pérdida forestal candidata respecto de "
                "2020, no una confirmación de deforestación. La transferencia RF vigente se "
                "limita a 2020-2024.",
                styles["landscape_caption"],
            ),
        ],
    ]


def _rgb_timeline_pages(
    figure: ClientFigure,
    period_ids: tuple[str, ...],
    styles: dict[str, ParagraphStyle],
) -> list[list[Any]]:
    with PILImage.open(figure.absolute_path) as source_image:
        source = source_image.convert("RGB")
        width, height = source.size
        column_width = width / 4
        row_height = height / 6
        panels: list[tuple[PILImage.Image, str]] = []
        for index in range(24):
            row, column = divmod(index, 4)
            panel = source.crop(
                (
                    round(column * column_width + column_width * 0.025),
                    round(row * row_height + row_height * 0.15),
                    round((column + 1) * column_width - column_width * 0.025),
                    round((row + 1) * row_height - row_height * 0.03),
                )
            )
            label = period_ids[index] if index < len(period_ids) else f"Período {index + 1}"
            panels.append((panel, label.replace("-", " · ")))
    first_grid = _compose_panel_grid(panels[:12], columns=4)
    second_grid = _compose_panel_grid(panels[12:], columns=4)
    common_caption = (
        "Lectura técnica: las variaciones de tono pueden responder a estacionalidad, humedad, "
        "manejo del cultivo o disponibilidad observacional. La serie aporta contexto visual, "
        "pero no atribuye por sí sola una conversión."
    )
    return [
        [
            Paragraph(
                "2.2.a. Evolución visual estacional en color natural: 2020-2022",
                styles["landscape_heading"],
            ),
            Paragraph(
                "Las composiciones RGB HLS usan la misma grilla y escala. Dividir la serie en "
                "dos páginas permite comparar cada estación sin reducir los paneles a miniaturas.",
                styles["landscape_body"],
            ),
            _pil_report_image(
                first_grid,
                max_width=26.2 * cm,
                max_height=13.1 * cm,
                photographic=True,
            ),
            Paragraph(common_caption, styles["landscape_caption"]),
        ],
        [
            Paragraph(
                "2.2.b. Evolución visual estacional: 2023-2025",
                styles["landscape_heading"],
            ),
            Paragraph(
                "La segunda mitad de la serie conserva el mismo encuadre para que los cambios "
                "de cobertura puedan distinguirse de la variación estacional normal.",
                styles["landscape_body"],
            ),
            _pil_report_image(
                second_grid,
                max_width=26.2 * cm,
                max_height=13.1 * cm,
                photographic=True,
            ),
            Paragraph(common_caption, styles["landscape_caption"]),
        ],
    ]


def _compose_panel_grid(
    panels: list[tuple[PILImage.Image, str]], *, columns: int
) -> PILImage.Image:
    canvas_width = 1800
    margin_x, margin_y = 35, 28
    column_gap, row_gap = 28, 34
    label_height = 44
    cell_width = (canvas_width - 2 * margin_x - (columns - 1) * column_gap) // columns
    image_height = round(cell_width * 0.66)
    rows = (len(panels) + columns - 1) // columns
    canvas_height = 2 * margin_y + rows * (label_height + image_height) + (rows - 1) * row_gap
    canvas = PILImage.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)
    font = _pil_font(28, bold=True)
    for index, (panel, label) in enumerate(panels):
        row, column = divmod(index, columns)
        x = margin_x + column * (cell_width + column_gap)
        y = margin_y + row * (label_height + image_height + row_gap)
        label_box = draw.textbbox((0, 0), label, font=font)
        label_width = label_box[2] - label_box[0]
        draw.text((x + (cell_width - label_width) / 2, y), label, font=font, fill="#17365D")
        resized = panel.copy()
        resized.thumbnail((cell_width, image_height), PILImage.Resampling.LANCZOS)
        image_x = x + (cell_width - resized.width) // 2
        image_y = y + label_height + (image_height - resized.height) // 2
        canvas.paste(resized, (image_x, image_y))
        draw.rectangle(
            (image_x, image_y, image_x + resized.width - 1, image_y + resized.height - 1),
            outline="#94A3B8",
            width=2,
        )
    return canvas


def _pil_font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont:
    fonts_root = Path(reportlab.__file__).resolve().parent / "fonts"
    filename = "VeraBd.ttf" if bold else "Vera.ttf"
    return ImageFont.truetype(str(fonts_root / filename), size=size)


def _pil_report_image(
    image: PILImage.Image,
    *,
    max_width: float,
    max_height: float,
    photographic: bool = False,
) -> ReportImage:
    stream = BytesIO()
    if photographic:
        image.save(
            stream,
            format="JPEG",
            quality=90,
            optimize=True,
            progressive=False,
            subsampling=0,
        )
    else:
        image.save(stream, format="PNG", compress_level=9, optimize=True)
    stream.seek(0)
    scale = min(max_width / image.width, max_height / image.height)
    report_image = ReportImage(stream, width=image.width * scale, height=image.height * scale)
    report_image.hAlign = "CENTER"
    return report_image


def _rf_legend(kind: str, *, width: float) -> Any:
    from reportlab.graphics.shapes import Drawing, Rect, String

    drawing = Drawing(width, 24)
    items: tuple[tuple[str, Any], ...]
    if kind == "annual":
        items = (
            ("Bosque automático", colors.HexColor("#0B6B2B")),
            ("No bosque automático", colors.HexColor("#D9D9D9")),
        )
    else:
        items = (
            ("Bosque estable", colors.HexColor("#006D2C")),
            ("Pérdida forestal candidata", colors.HexColor("#EF3B2C")),
            ("Ganancia forestal candidata", colors.HexColor("#41AB5D")),
            ("No bosque estable", colors.white),
            ("Datos insuficientes", colors.HexColor("#BDBDBD")),
        )
    spacing = width / len(items)
    for index, (label, color) in enumerate(items):
        x = index * spacing + 4
        drawing.add(Rect(x, 8, 10, 10, fillColor=color, strokeColor=_TEXT, strokeWidth=0.4))
        drawing.add(
            String(
                x + 14,
                10,
                label,
                fontName=_FONT_REGULAR,
                fontSize=6.2,
                fillColor=_TEXT,
            )
        )
    return drawing


def _event_sections(
    view: ReportViewModel,
    event: EventSummary,
    client_figure: ClientFigure | None,
    styles: dict[str, ParagraphStyle],
    osm_basemap_provider: OSMBasemapProvider,
) -> list[Any]:
    map_width, map_height = 15.3 * cm, 9.4 * cm
    basemap = (
        osm_basemap_provider.get(
            agricultural_map_bounds(
                event.event_geometry,
                width=map_width,
                height=map_height,
            )
        )
        if event.event_geometry
        else None
    )
    result: list[Any] = [
        PageBreak(),
        Paragraph(
            f"3.{event.ordinal}. Evento E{event.ordinal}: {_event_status_label(event)}",
            styles["heading"],
        ),
        Paragraph(
            f"Identificador técnico: {_escape(event.event_id)}. Inicio estimado: "
            f"{_event_onset(event)}.",
            styles["body"],
        ),
        _event_summary_table(event, styles),
        Spacer(1, 0.4 * cm),
        Paragraph("Interpretación del evento", styles["subheading"]),
        Paragraph(_event_interpretation(event), styles["body"]),
        Paragraph("Condiciones evaluadas", styles["subheading"]),
        _gates_table(event, styles),
        Spacer(1, 0.45 * cm),
        Paragraph("Evolución de la cobertura forestal dentro del evento", styles["subheading"]),
        forest_trajectory_chart(event, width=15.3 * cm, height=5.0 * cm),
        Paragraph(
            "La fracción anual representa una clasificación binaria dentro del evento; no es "
            "una probabilidad calibrada de deforestación ni de incumplimiento EUDR.",
            styles["fine_left"],
        ),
    ]
    if client_figure is not None:
        result.extend(
            [
                NextPageTemplate("landscape"),
                PageBreak(),
                Paragraph(
                    f"Evidencia espectral complementaria del evento E{event.ordinal}",
                    styles["landscape_heading"],
                ),
                Paragraph(
                    "La figura compara NDVI previo al corte con la ventana estimada de inicio, "
                    "muestra su diferencia espacial y resume la trayectoria media de índices "
                    "dentro del evento.",
                    styles["landscape_body"],
                ),
                Spacer(1, 0.15 * cm),
                _scaled_report_image(
                    client_figure,
                    max_width=26.2 * cm,
                    max_height=14.2 * cm,
                ),
                Spacer(1, 0.2 * cm),
                Paragraph(
                    "Esta evidencia respalda la detección temporal del cambio. No determina "
                    "por sí sola el uso posterior, la causa del cambio ni una conclusión legal.",
                    styles["landscape_caption"],
                ),
                NextPageTemplate("content"),
                PageBreak(),
            ]
        )
    else:
        result.append(PageBreak())
    result.extend(
        [
            Paragraph(
                f"3.{event.ordinal}.1. Uso posterior observado en E{event.ordinal}",
                styles["heading"],
            ),
            Paragraph(
                _agricultural_interpretation(event),
                styles["lead"],
            ),
            agricultural_support_map(
                event,
                establishment_geometry=view.establishment_geometry,
                basemap=basemap,
                width=map_width,
                height=map_height,
            ),
            Paragraph(
                "Referencia espacial: el mapa principal amplía el evento sobre contexto "
                "vectorial OpenStreetMap al 50 % de opacidad; el recuadro lo ubica dentro de "
                "la geometría declarada. El mapa base sólo aporta orientación: no interviene "
                "en la detección, atribución ni medición científica.",
                styles["fine_left"],
            ),
            Spacer(1, 0.35 * cm),
            _agricultural_table(event, styles),
            Spacer(1, 0.35 * cm),
            Paragraph("Sensibilidad al umbral técnico", styles["subheading"]),
            sensitivity_chart(event, width=15.3 * cm, height=2.2 * cm),
            Paragraph(
                "El rango de sensibilidad muestra cómo cambia la superficie al variar el umbral "
                "técnico; no es un intervalo de confianza estadístico. Dynamic World V1 es la "
                "única fuente temporal agrícola operativa de este MVP.",
                styles["fine_left"],
            ),
        ]
    )
    return result


def _event_client_figure(view: ReportViewModel, event: EventSummary) -> ClientFigure | None:
    return next(
        (
            figure
            for figure in view.client_figures
            if figure.kind == "event_spectral_evidence" and figure.event_id == event.event_id
        ),
        None,
    )


def _scaled_report_image(
    figure: ClientFigure, *, max_width: float, max_height: float
) -> ReportImage:
    pixel_width, pixel_height = ImageReader(str(figure.absolute_path)).getSize()
    scale = min(max_width / pixel_width, max_height / pixel_height)
    image = ReportImage(
        str(figure.absolute_path),
        width=pixel_width * scale,
        height=pixel_height * scale,
    )
    image.hAlign = "CENTER"
    return image


def _quality_section(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> list[Any]:
    quality = view.quality
    observation_range = (
        "no disponible"
        if quality.minimum_mean_observation_count is None
        or quality.maximum_mean_observation_count is None
        else f"{quality.minimum_mean_observation_count:.1f} a "
        f"{quality.maximum_mean_observation_count:.1f}"
    ).replace(".", ",")
    coverage = (
        "no disponible"
        if quality.minimum_valid_pixel_fraction is None
        else _percent(quality.minimum_valid_pixel_fraction)
    )
    quality_rows = [
        ("Composiciones estacionales", str(quality.seasonal_period_count)),
        ("Cobertura espacial válida mínima", coverage),
        (
            "Períodos de detección evaluables",
            f"{quality.evaluable_detection_period_count} de {quality.detection_period_count}",
        ),
        ("Observaciones medias por período", observation_range),
    ]
    return [
        PageBreak(),
        Paragraph("4. Calidad y fuentes", styles["heading"]),
        Paragraph(
            "La calidad observacional se informa por separado del resultado temático. Una "
            "cobertura alta reduce vacíos de datos, pero no elimina la incertidumbre del modelo.",
            styles["body"],
        ),
        _two_column_table(quality_rows, styles),
        Spacer(1, 0.55 * cm),
        Paragraph("Geometría y cálculo de superficie", styles["subheading"]),
        Paragraph(
            "La geometría se intercambia en WGS 84, pero las superficies se calculan en un "
            "CRS equivalente para evitar distorsiones. Cualquier reparación topológica se "
            "informa de manera explícita.",
            styles["body"],
        ),
        _two_column_table(
            [
                ("Geometría analizada", view.geometry.geometry_type),
                ("CRS de intercambio", view.geometry.normalized_crs),
                ("CRS de cálculo", view.geometry.calculation_crs),
                ("Reparación topológica", "sí" if view.geometry.repair_performed else "no"),
                ("Umbral informativo", _ha(view.geometry.threshold_ha)),
            ],
            styles,
        ),
        Spacer(1, 0.55 * cm),
        Paragraph("Fuentes utilizadas", styles["subheading"]),
        Paragraph(
            "La tabla identifica proveedor, versión, fecha de acceso y licencia de cada fuente "
            "que interviene en la lectura presentada. Las fuentes cumplen roles distintos y no "
            "se fusionan como si fueran observaciones equivalentes.",
            styles["body"],
        ),
        _datasets_table(view.datasets, styles),
        Paragraph(
            "Las fuentes globales y los modelos de cobertura son evidencia complementaria; "
            "ninguna se interpreta como verdad de terreno individual.",
            styles["fine_left"],
        ),
    ]


def _limitations_section(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> list[Any]:
    return [
        PageBreak(),
        Paragraph("5. Alcance, limitaciones y revisión", styles["heading"]),
        Paragraph(
            "La salida automática conserva incertidumbre y no reemplaza una decisión "
            "documentada del operador, auditor o certificador.",
            styles["lead"],
        ),
        *[_bullet(item, styles) for item in view.limitations],
        Spacer(1, 0.55 * cm),
        Paragraph("Estado de procesamiento", styles["subheading"]),
        _components_table(view, styles),
        Spacer(1, 0.55 * cm),
        Paragraph("Nota legal", styles["subheading"]),
        Paragraph(LEGAL_DISCLAIMER, styles["disclaimer"]),
        Spacer(1, 0.55 * cm),
        Paragraph("Trazabilidad del documento", styles["subheading"]),
        Paragraph(
            "Generado desde report_dataset.json y datos estructurados declarados en "
            f"report_assets/index.json. Los {view.verified_asset_count} assets del expediente "
            "se verifican por tamaño y SHA-256, aunque las figuras internas de QA no se "
            "muestran en el cuerpo cliente.",
            styles["body"],
        ),
        Paragraph(
            f"Contrato editorial: {view.schema_version}. Registrado: "
            f"{view.analysis.recorded_at.isoformat()}.",
            styles["body"],
        ),
        Paragraph(
            "El contexto cartográfico OpenStreetMap se consulta y cachea por extensión para "
            "la presentación. No forma parte de la evidencia científica ni modifica sus "
            "resultados.",
            styles["body"],
        ),
    ]


def _section_index_table(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> Table:
    global_page_count = sum(
        2
        for figure in view.client_figures
        if figure.kind in {"annual_forest_change", "rgb_timeline"}
    )
    detailed_events = [event for event in view.events if event.selected_for_detail]
    event_page_count = sum(
        2 + (1 if _event_client_figure(view, event) is not None else 0) for event in detailed_events
    )
    detail_start_page = 4 + global_page_count
    quality_page = detail_start_page + event_page_count
    rows = [
        ("1", "Resumen ejecutivo", 2),
        ("2", "Cambios detectados y evidencia global", 3),
        ("3", "Detalle e interpretación de eventos", detail_start_page),
        ("4", "Calidad, geometría y fuentes", quality_page),
        ("5", "Alcance, limitaciones y revisión", quality_page + 1),
    ]
    data = [
        [
            Paragraph(number, styles["cover_index_number"]),
            Paragraph(label, styles["cover_index_cell"]),
            Paragraph(str(page), styles["cover_index_page"]),
        ]
        for number, label, page in rows
    ]
    table = Table(data, colWidths=[0.8 * cm, 10.8 * cm, 1.0 * cm], hAlign="CENTER")
    table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#D7DEE7")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _metrics_summary_table(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> Table:
    metrics = view.metrics
    rows = [
        ("Superficie declarada", _ha(metrics.establishment_area_ha)),
        ("Bosque automático 2020", _ha(metrics.forest_area_2020_ha)),
        ("Eventos detectados", _number(metrics.detected_event_count)),
        ("Superficie con eventos", _ha(metrics.detected_event_area_ha)),
        ("Área compatible con conversión", _ha(metrics.likely_conversion_area_ha)),
        ("Eventos con esa evidencia", _number(metrics.conversion_likely_count)),
    ]
    return _two_column_table(rows, styles)


def _event_inventory_table(
    events: tuple[EventSummary, ...], styles: dict[str, ParagraphStyle]
) -> Table:
    data: list[list[Any]] = [
        [
            Paragraph("ID", styles["table_header"]),
            Paragraph("Inicio", styles["table_header"]),
            Paragraph("Área", styles["table_header"]),
            Paragraph("Agricultura persistente", styles["table_header"]),
            Paragraph("Área compatible", styles["table_header"]),
            Paragraph("Resultado", styles["table_header"]),
        ]
    ]
    for event in events:
        data.append(
            [
                Paragraph(f"E{event.ordinal}", styles["table_cell_bold"]),
                Paragraph(_escape(_event_onset(event)), styles["table_cell"]),
                Paragraph(_ha(event.area_ha), styles["table_cell"]),
                Paragraph(_ha(event.persistent_agricultural_area_ha), styles["table_cell"]),
                Paragraph(_ha(event.likely_conversion_area_ha), styles["table_cell"]),
                Paragraph(_escape(_event_status_label(event)), styles["table_cell"]),
            ]
        )
    table = Table(
        data,
        colWidths=[1.3 * cm, 2.7 * cm, 1.8 * cm, 3.1 * cm, 2.5 * cm, 3.9 * cm],
        repeatRows=1,
    )
    table.setStyle(_table_style())
    return table


def _event_summary_table(event: EventSummary, styles: dict[str, ParagraphStyle]) -> Table:
    agreement = "no disponible"
    if event.dual_detector_pixel_count is not None and event.pixel_count:
        agreement = _percent(event.dual_detector_pixel_count / event.pixel_count)
    return _two_column_table(
        [
            ("Resultado automático", _event_status_label(event)),
            ("Superficie del evento", _ha(event.area_ha)),
            ("Área compatible con conversión", _ha(event.likely_conversion_area_ha)),
            ("Uso posterior", _post_use_label(event.post_change_use)),
            ("Coincidencia espacial de dos detectores", agreement),
            ("Confianza numérica", _confidence(event)),
            ("Revisión humana", "requerida" if event.human_review_required else "no requerida"),
        ],
        styles,
    )


def _gates_table(event: EventSummary, styles: dict[str, ParagraphStyle]) -> Table:
    data: list[list[Any]] = [
        [
            Paragraph("Condición", styles["table_header"]),
            Paragraph("Resultado", styles["table_header"]),
            Paragraph("Lectura", styles["table_header"]),
        ]
    ]
    for gate in event.gates:
        data.append(
            [
                Paragraph(_escape(_gate_label(gate.key)), styles["table_cell"]),
                Paragraph("Sí" if gate.passed else "No", styles["table_cell_bold"]),
                Paragraph(
                    _escape(_gate_interpretation(gate.key, gate.passed)), styles["table_cell"]
                ),
            ]
        )
    table = Table(data, colWidths=[5.0 * cm, 1.7 * cm, 8.6 * cm], repeatRows=1)
    table.setStyle(_table_style())
    return table


def _agricultural_table(event: EventSummary, styles: dict[str, ParagraphStyle]) -> Table:
    fraction = (
        "no disponible"
        if event.persistent_event_fraction is None
        else _percent(event.persistent_event_fraction)
    )
    sensitivity = (
        "no aplicable"
        if event.sensitivity_min_area_ha is None
        or event.sensitivity_max_area_ha is None
        or event.sensitivity_max_area_ha <= 0
        else f"{_ha(event.sensitivity_min_area_ha)} a {_ha(event.sensitivity_max_area_ha)}"
    )
    return _two_column_table(
        [
            ("Soporte agrícola persistente", _ha(event.persistent_agricultural_area_ha)),
            ("Fracción del evento", fraction),
            ("Períodos válidos", _number(event.observed_period_count)),
            ("Períodos calificantes", _number(event.qualifying_period_count)),
            ("Períodos faltantes", _number(event.gap_period_count)),
            ("Rango de sensibilidad", sensitivity),
        ],
        styles,
    )


def _datasets_table(
    datasets: tuple[DatasetSummary, ...], styles: dict[str, ParagraphStyle]
) -> Table:
    data: list[list[Any]] = [
        [
            Paragraph("Fuente", styles["table_header"]),
            Paragraph("Proveedor", styles["table_header"]),
            Paragraph("Versión", styles["table_header"]),
            Paragraph("Acceso", styles["table_header"]),
            Paragraph("Licencia", styles["table_header"]),
        ]
    ]
    for dataset in datasets:
        resolution = (
            "" if dataset.spatial_resolution_m is None else f" ({dataset.spatial_resolution_m:g} m)"
        )
        data.append(
            [
                Paragraph(
                    _escape(_dataset_label(dataset.dataset_id) + resolution), styles["table_cell"]
                ),
                Paragraph(_escape(dataset.provider), styles["table_cell"]),
                Paragraph(_escape(dataset.version), styles["table_cell"]),
                Paragraph(dataset.access_date.isoformat(), styles["table_cell"]),
                Paragraph(_escape(dataset.license), styles["table_cell"]),
            ]
        )
    table = Table(
        data,
        colWidths=[3.4 * cm, 4.0 * cm, 1.25 * cm, 2.1 * cm, 4.55 * cm],
        repeatRows=1,
    )
    table.setStyle(_table_style())
    return table


def _components_table(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> Table:
    data: list[list[Any]] = [
        [
            Paragraph("Componente", styles["table_header"]),
            Paragraph("Estado", styles["table_header"]),
        ]
    ]
    for component in view.components:
        data.append(
            [
                Paragraph(_escape(_component_label(component.name)), styles["table_cell"]),
                Paragraph(_escape(_component_status_label(component.status)), styles["table_cell"]),
            ]
        )
    table = Table(data, colWidths=[8.4 * cm, 6.9 * cm], repeatRows=1)
    table.setStyle(_table_style())
    return table


def _two_column_table(rows: list[tuple[str, str]], styles: dict[str, ParagraphStyle]) -> Table:
    data = [
        [
            Paragraph(_escape(label), styles["table_cell_bold"]),
            Paragraph(_escape(value), styles["table_cell"]),
        ]
        for label, value in rows
    ]
    table = Table(data, colWidths=[8.2 * cm, 7.1 * cm])
    table.setStyle(
        TableStyle(
            [
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, _LIGHT_BLUE]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _bullet(text: str, styles: dict[str, ParagraphStyle]) -> Paragraph:
    return Paragraph(f"- {_escape(text)}", styles["bullet"])


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle(
            "CoverTitle",
            parent=base["Title"],
            fontName=_FONT_BOLD,
            fontSize=25,
            leading=31,
            textColor=_BLUE,
            alignment=TA_CENTER,
        ),
        "cover_subtitle": ParagraphStyle(
            "CoverSubtitle",
            parent=base["Heading2"],
            fontName=_FONT_REGULAR,
            fontSize=14,
            leading=19,
            textColor=_TEXT,
            alignment=TA_CENTER,
        ),
        "cover_meta": ParagraphStyle(
            "CoverMeta",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=9,
            leading=13,
            textColor=_MUTED,
            alignment=TA_CENTER,
        ),
        "cover_science_heading": ParagraphStyle(
            "CoverScienceHeading",
            parent=base["Heading2"],
            fontName=_FONT_BOLD,
            fontSize=9.5,
            leading=12,
            textColor=_GREEN,
            alignment=TA_CENTER,
            spaceAfter=3,
        ),
        "cover_science": ParagraphStyle(
            "CoverScience",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=8.2,
            leading=11.2,
            textColor=_TEXT,
            alignment=TA_CENTER,
            leftIndent=0.65 * cm,
            rightIndent=0.65 * cm,
        ),
        "cover_note": ParagraphStyle(
            "CoverNote",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=9.2,
            leading=13.5,
            textColor=_TEXT,
            alignment=TA_CENTER,
        ),
        "cover_status": ParagraphStyle(
            "CoverStatus",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=11,
            leading=15,
            textColor=_TEXT,
            alignment=TA_CENTER,
        ),
        "cover_index_heading": ParagraphStyle(
            "CoverIndexHeading",
            parent=base["Heading2"],
            fontName=_FONT_BOLD,
            fontSize=10.5,
            leading=14,
            textColor=_GREEN,
            alignment=TA_CENTER,
            spaceAfter=4,
        ),
        "cover_index_number": ParagraphStyle(
            "CoverIndexNumber",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=7.5,
            leading=9,
            textColor=_GREEN,
        ),
        "cover_index_cell": ParagraphStyle(
            "CoverIndexCell",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=7.5,
            leading=9,
            textColor=_TEXT,
        ),
        "cover_index_page": ParagraphStyle(
            "CoverIndexPage",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=7.5,
            leading=9,
            textColor=_MUTED,
            alignment=TA_CENTER,
        ),
        "heading": ParagraphStyle(
            "Heading",
            parent=base["Heading1"],
            fontName=_FONT_BOLD,
            fontSize=16,
            leading=20,
            textColor=_BLUE,
            spaceAfter=10,
        ),
        "subheading": ParagraphStyle(
            "Subheading",
            parent=base["Heading2"],
            fontName=_FONT_BOLD,
            fontSize=11,
            leading=15,
            textColor=_GREEN,
            spaceBefore=4,
            spaceAfter=5,
        ),
        "lead": ParagraphStyle(
            "Lead",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=10,
            leading=14.5,
            textColor=_TEXT,
            alignment=TA_LEFT,
            spaceAfter=7,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=9.2,
            leading=13.2,
            textColor=_TEXT,
            alignment=TA_LEFT,
            spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=8.7,
            leading=12,
            leftIndent=10,
            firstLineIndent=-7,
            textColor=_TEXT,
            spaceAfter=3,
        ),
        "fine_left": ParagraphStyle(
            "FineLeft",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=6.8,
            leading=9.2,
            textColor=_MUTED,
            alignment=TA_LEFT,
        ),
        "landscape_heading": ParagraphStyle(
            "LandscapeHeading",
            parent=base["Heading1"],
            fontName=_FONT_BOLD,
            fontSize=14,
            leading=17,
            textColor=_BLUE,
            spaceAfter=5,
        ),
        "landscape_body": ParagraphStyle(
            "LandscapeBody",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=8.2,
            leading=10.5,
            textColor=_TEXT,
            spaceAfter=3,
        ),
        "landscape_caption": ParagraphStyle(
            "LandscapeCaption",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=7.2,
            leading=9.2,
            textColor=_MUTED,
        ),
        "disclaimer": ParagraphStyle(
            "Disclaimer",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=9,
            leading=13,
            textColor=_AMBER,
            alignment=TA_CENTER,
            borderColor=_AMBER,
            borderWidth=0.7,
            borderPadding=8,
            backColor=_LIGHT_AMBER,
        ),
        "table_header": ParagraphStyle(
            "TableHeader",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=7.2,
            leading=9,
            textColor=colors.white,
        ),
        "table_cell": ParagraphStyle(
            "TableCell",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=6.8,
            leading=8.7,
            textColor=_TEXT,
        ),
        "table_cell_bold": ParagraphStyle(
            "TableCellBold",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=6.8,
            leading=8.7,
            textColor=_TEXT,
        ),
    }


def _table_style() -> TableStyle:
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), _BLUE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_BLUE]),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
    )


def _page_template(
    template_id: str,
    view: ReportViewModel,
    *,
    cover: bool,
    pagesize: tuple[float, float],
) -> PageTemplate:
    width, height = pagesize
    horizontal_margin = 1.4 * cm if width > height else 1.7 * cm
    vertical_margin = 1.4 * cm if width > height else 1.8 * cm
    frame = Frame(
        horizontal_margin,
        vertical_margin,
        width - 2 * horizontal_margin,
        height - 2 * vertical_margin,
        id=f"{template_id}-frame",
    )
    return PageTemplate(
        id=template_id,
        frames=[frame],
        pagesize=pagesize,
        onPage=lambda pdf, doc: _decorate_page(
            pdf,
            doc,
            view,
            cover=cover,
            pagesize=pagesize,
        ),
    )


def _decorate_page(
    pdf: canvas.Canvas,
    _doc: Any,
    view: ReportViewModel,
    *,
    cover: bool,
    pagesize: tuple[float, float],
) -> None:
    page_width, page_height = pagesize
    horizontal_margin = 1.4 * cm if page_width > page_height else 1.7 * cm
    pdf.saveState()
    pdf.setTitle(view.title)
    pdf.setAuthor("Deforestation Evidence Pipeline")
    pdf.setSubject("Evidencia técnica geoespacial sujeta a revisión humana")
    if not cover:
        pdf.setStrokeColor(colors.HexColor("#D1D5DB"))
        pdf.line(
            horizontal_margin,
            page_height - 1.05 * cm,
            page_width - horizontal_margin,
            page_height - 1.05 * cm,
        )
        pdf.setFont(_FONT_REGULAR, 7)
        pdf.setFillColor(_MUTED)
        pdf.drawString(
            horizontal_margin,
            page_height - 0.85 * cm,
            view.analysis.establishment_id,
        )
        pdf.drawRightString(
            page_width - horizontal_margin,
            page_height - 0.85 * cm,
            view.analysis.analysis_id,
        )
    pdf.setFont(_FONT_REGULAR, 7)
    pdf.setFillColor(_MUTED)
    pdf.drawCentredString(page_width / 2, 0.85 * cm, f"Página {pdf.getPageNumber()}")
    pdf.restoreState()


def _deterministic_canvas(filename: str, **kwargs: Any) -> canvas.Canvas:
    kwargs["invariant"] = 1
    kwargs["pageCompression"] = 1
    return canvas.Canvas(filename, **kwargs)


def _result_status_label(status: str) -> str:
    return {
        "review_required": "Revisión humana requerida",
        "conversion_likely": "Evidencia compatible con conversión",
        "low_risk": "Riesgo técnico bajo",
        "no_change_detected": "Sin cambio relevante detectado",
        "insufficient_data": "Datos insuficientes",
    }.get(status, "Revisión humana requerida")


def _event_status_label(event: EventSummary) -> str:
    if not event.area_threshold_met:
        return "Revisión requerida - evento menor a 0,5 ha"
    return {
        "conversion_likely": "Evidencia compatible con conversión",
        "review_required": "Revisión requerida",
        "low_risk": "Riesgo técnico bajo",
        "insufficient_data": "Datos insuficientes",
    }.get(event.automatic_status, "Revisión requerida")


def _post_use_label(value: str) -> str:
    return {
        "agriculture_likely": "Uso agrícola probable",
        "cropland": "Cultivo probable",
        "unknown": "Uso posterior no determinado",
    }.get(value, value.replace("_", " "))


def _gate_label(key: str) -> str:
    return {
        "forest_at_cutoff": "Bosque al 31/12/2020",
        "post_cutoff_loss": "Pérdida posterior al corte",
        "persistent_change": "Cambio persistente",
        "agricultural_or_livestock_post_use": "Uso agrícola o ganadero posterior",
        "defensible_area_and_geometry": "Superficie y geometría defendibles",
        "strong_alternative_explanation_absent": "Sin explicación alternativa respaldada",
    }[key]


def _gate_interpretation(key: str, passed: bool) -> str:
    if passed:
        return {
            "forest_at_cutoff": "La superficie estaba dentro del dominio forestal automático.",
            "post_cutoff_loss": "La pérdida fue observada después de la fecha de corte.",
            "persistent_change": "La señal no se limitó a una perturbación transitoria.",
            "agricultural_or_livestock_post_use": "Se observó soporte persistente de uso agrícola.",
            "defensible_area_and_geometry": (
                "La conjunción espacial y el área resultan suficientes."
            ),
            "strong_alternative_explanation_absent": (
                "No se registró una explicación alternativa fuerte."
            ),
        }[key]
    return {
        "forest_at_cutoff": "No se estableció bosque automático al corte.",
        "post_cutoff_loss": "No se estableció pérdida posterior al corte.",
        "persistent_change": "La persistencia no quedó respaldada.",
        "agricultural_or_livestock_post_use": (
            "Falta evidencia persistente de uso agrícola o ganadero."
        ),
        "defensible_area_and_geometry": "El área o la conjunción espacial requieren revisión.",
        "strong_alternative_explanation_absent": (
            "Existe una explicación alternativa que debe evaluarse."
        ),
    }[key]


def _event_interpretation(event: EventSummary) -> str:
    failed = [_gate_label(gate.key) for gate in event.gates if not gate.passed]
    if event.automatic_status == "conversion_likely":
        return (
            "La línea base forestal, la pérdida posterior al corte, la persistencia y el uso "
            "agrícola posterior convergen espacialmente. Por eso el evento se clasifica como "
            "evidencia compatible con conversión, siempre sujeta a revisión humana."
        )
    if failed:
        return (
            "La evidencia disponible no satisface todas las condiciones necesarias para una "
            f"atribución fuerte. Requieren revisión: {', '.join(failed).lower()}."
        )
    return (
        "El evento conserva evidencia automática de cambio, pero su interpretación final "
        "permanece sujeta a revisión humana documentada."
    )


def _agricultural_interpretation(event: EventSummary) -> str:
    if event.persistent_agricultural_area_ha > 0:
        return (
            f"Se identificaron {_ha(event.persistent_agricultural_area_ha)} con soporte "
            "agrícola persistente dentro del evento. Esta evidencia es automática y no "
            "determina por sí sola la finalidad legal del uso del suelo."
        )
    return (
        "No se identificó soporte agrícola persistente suficiente. El uso posterior "
        "permanece sin determinar y requiere revisión."
    )


def _dataset_label(dataset_id: str) -> str:
    return {
        "hls_l30_v2": "HLS Landsat",
        "hls_s30_v2": "HLS Sentinel-2",
        "jrc_gfc2020_v3": "JRC Forest Cover 2020",
        "esa_worldcover_2020_v100": "ESA WorldCover 2020",
        "hansen_gfc_2025_v1_13": "Hansen Global Forest Change",
        "dynamic_world_v1": "Dynamic World V1",
    }.get(dataset_id, dataset_id)


def _component_label(name: str) -> str:
    return {
        "full_pipeline": "Pipeline principal",
        "hampel_benchmark": "Diagnóstico estacional Hampel",
        "rf_annual_deltas": "Trayectoria forestal anual",
        "agricultural_collection": "Recolección agrícola mensual",
        "agricultural_persistence": "Persistencia agrícola",
        "post_change_attribution": "Atribución post-cambio",
    }.get(name, name.replace("_", " "))


def _component_status_label(status: str) -> str:
    return {
        "completed": "Completado",
        "diagnostic_unavailable": "Diagnóstico complementario no disponible",
        "partial": "Parcial",
        "failed": "Falló",
    }.get(status, status.replace("_", " "))


def _event_onset(event: EventSummary) -> str:
    if event.estimated_onset_window_start and event.estimated_onset_window_end:
        return (
            f"{event.estimated_onset_window_start.strftime('%d/%m/%Y')} a "
            f"{event.estimated_onset_window_end.strftime('%d/%m/%Y')}"
        )
    return event.estimated_onset_period_id


def _confidence(event: EventSummary) -> str:
    if event.confidence is not None:
        return f"{event.confidence:.2f}"
    return "No calibrada"


def _ha(value: float | None) -> str:
    return "no disponible" if value is None else f"{_decimal_es(value)} ha"


def _decimal_es(value: float) -> str:
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _number(value: int | None) -> str:
    return "no disponible" if value is None else str(value)


def _percent(value: float) -> str:
    return f"{value * 100:.2f} %".replace(".", ",")


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
