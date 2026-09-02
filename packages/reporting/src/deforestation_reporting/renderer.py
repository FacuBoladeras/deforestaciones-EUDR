"""Renderer ReportLab determinístico del informe técnico para clientes."""

from __future__ import annotations

import hashlib
import html
import os
from datetime import datetime
from functools import partial
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
from reportlab.lib.utils import ImageReader, TimeStamp
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
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
    events_overview_map,
    forest_trajectory_chart,
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
_EUDR_CUTOFF = "31/12/2020"
_PRIORITY_EVENT_LIMIT = 4


class _AuditDocTemplate(BaseDocTemplate):  # type: ignore[misc]
    """Agrega marcadores PDF a los títulos editoriales declarados."""

    def afterFlowable(self, flowable: Any) -> None:
        bookmark = getattr(flowable, "_audit_bookmark", None)
        level = getattr(flowable, "_audit_level", None)
        if not isinstance(bookmark, str) or not isinstance(level, int):
            return
        title = getattr(flowable, "_audit_title", None)
        if not isinstance(title, str):
            title = flowable.getPlainText() if isinstance(flowable, Paragraph) else str(bookmark)
        self.canv.bookmarkPage(bookmark)
        self.canv.addOutlineEntry(title, bookmark, level=level, closed=False)


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
    issued_at: datetime | None = None,
) -> ReportArtifact:
    """Renderiza el informe principal cliente sin incluir los anexos técnicos."""
    view = build_report_view_model(package)
    emission_time = issued_at or view.analysis.recorded_at
    basemap_provider = osm_basemap_provider or OverpassOSMProvider(
        cache_dir=output_path.resolve().parent / ".osm-cache"
    )
    return _render_document(
        view,
        _main_story(view, basemap_provider, emission_time),
        output_path,
        emission_time,
    )


def render_technical_appendix(
    package: ReportPackage,
    output_path: Path,
    *,
    issued_at: datetime | None = None,
) -> ReportArtifact:
    """Renderiza los anexos auditables en un PDF separado del informe principal."""
    view = build_report_view_model(package)
    emission_time = issued_at or view.analysis.recorded_at
    return _render_document(
        view,
        _appendix_story(view),
        output_path,
        emission_time,
    )


def _render_document(
    view: ReportViewModel,
    story: list[Any],
    output_path: Path,
    emission_time: datetime,
) -> ReportArtifact:
    output = output_path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    try:
        document = _AuditDocTemplate(
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
                _page_template("cover", view, emission_time, cover=True, pagesize=_PORTRAIT_PAGE),
                _page_template(
                    "content", view, emission_time, cover=False, pagesize=_PORTRAIT_PAGE
                ),
                _page_template(
                    "landscape",
                    view,
                    emission_time,
                    cover=False,
                    pagesize=_LANDSCAPE_PAGE,
                ),
            ]
        )
        document.build(
            story,
            canvasmaker=partial(_dated_canvas, creation_time=emission_time),
        )
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return ReportArtifact(path=output, sha256=_sha256(output), size_bytes=output.stat().st_size)


def _main_story(
    view: ReportViewModel,
    osm_basemap_provider: OSMBasemapProvider,
    emission_time: datetime,
) -> list[Any]:
    styles = _styles()
    priority = _priority_events(view)
    story: list[Any] = [
        Spacer(1, 1.9 * cm),
        _section_heading(view.title, styles["cover_title"], "cover", outline_title="Portada"),
        Spacer(1, 1.1 * cm),
        _cover_metadata_table(view, emission_time, styles),
        Spacer(1, 0.9 * cm),
        Paragraph(_headline_messages(view)[0], styles["cover_result"]),
        Spacer(1, 1.0 * cm),
        Paragraph(LEGAL_DISCLAIMER, styles["disclaimer"]),
        NextPageTemplate("content"),
        PageBreak(),
    ]
    story.extend(_executive_section(view, styles))
    story.extend(_identification_section(view, styles))
    story.extend(_eudr_scope_section(styles))
    story.extend(_events_overview_section(view, priority, styles))
    story.extend(_priority_event_sections(view, priority, styles, osm_basemap_provider))
    story.extend(_temporal_evidence_section(view, styles))
    story.extend(_conclusion_section(view, styles))
    return story


def _appendix_story(view: ReportViewModel) -> list[Any]:
    styles = _styles()
    return _technical_annexes(view, _priority_events(view), styles, standalone=True)


def _executive_section(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> list[Any]:
    messages = _headline_messages(view)
    return [
        _section_heading(
            "2. Resumen ejecutivo",
            styles["heading"],
            "executive",
            outline_title="Resumen ejecutivo",
        ),
        Paragraph(messages[0], styles["headline_primary"]),
        Paragraph(messages[1], styles["headline_secondary"]),
        Paragraph(messages[2], styles["headline_secondary"]),
        Paragraph(messages[3], styles["headline_secondary"]),
        Spacer(1, 0.35 * cm),
        _dashboard_table(view, styles),
        Spacer(1, 0.55 * cm),
        Paragraph("Tres niveles de interpretación", styles["subheading"]),
        _interpretation_layers_table(view, styles),
        Spacer(1, 0.4 * cm),
        Paragraph(
            "Los niveles anteriores no son equivalentes: detectar un cambio no implica "
            "atribuir conversión, y ninguna salida automática reemplaza la decisión humana "
            "documentada.",
            styles["body"],
        ),
    ]


def _section_heading(
    text: str,
    style: ParagraphStyle,
    bookmark: str,
    *,
    level: int = 0,
    outline_title: str | None = None,
) -> Paragraph:
    paragraph = Paragraph(text, style)
    paragraph._audit_bookmark = bookmark
    paragraph._audit_level = level
    paragraph._audit_title = outline_title or text
    return paragraph


def _cover_metadata_table(
    view: ReportViewModel,
    emission_time: datetime,
    styles: dict[str, ParagraphStyle],
) -> Table:
    rows = [
        ("Establecimiento", view.analysis.establishment_id),
        ("ID del análisis", view.analysis.analysis_id),
        ("Fecha de emisión", emission_time.date().isoformat()),
        ("Fecha de corte EUDR", _EUDR_CUTOFF),
        ("Período analizado", f"01/01/2020 a {view.analysis.analysis_end_date:%d/%m/%Y}"),
        ("Resultado técnico", _headline_messages(view)[0]),
    ]
    data = [
        [
            Paragraph(_escape(label), styles["cover_field_label"]),
            Paragraph(_escape(value), styles["cover_field_value"]),
        ]
        for label, value in rows
    ]
    table = Table(data, colWidths=[5.2 * cm, 9.2 * cm], hAlign="CENTER")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), _LIGHT_BLUE),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D7DEE7")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def _headline_messages(view: ReportViewModel) -> tuple[str, str, str, str]:
    attribution_available = _component_available(view, "post_change_attribution")
    likely_area = view.metrics.likely_conversion_area_ha
    detected_count = view.metrics.spectral_candidate_count or 0
    if not attribution_available:
        primary = "La evidencia disponible es incompleta para evaluar conversión."
    elif likely_area is not None and likely_area > 0:
        primary = "Se identificó evidencia compatible con conversión."
    else:
        primary = "No se identificó evidencia suficiente de conversión."
    if detected_count > 0:
        changes = "Se detectaron eventos de cambio que requieren revisión."
    else:
        changes = "No se detectaron eventos de cambio en el período evaluado."
    area = f"Superficie compatible con conversión: {_ha(likely_area)}."
    review = f"Estado de revisión humana: {_review_state(view)}."
    return primary, changes, area, review


def _review_state(view: ReportViewModel) -> str:
    if not _component_available(view, "post_change_attribution"):
        return "Pendiente por evidencia incompleta"
    if view.metrics.automatic_final_assessment_generated:
        return "Automática disponible; validación humana pendiente"
    if any(event.human_review_required for event in view.events):
        return "Pendiente"
    return "Sin revisión documentada"


def _dashboard_table(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> Table:
    baseline = view.baseline
    metrics = view.metrics
    event_summary = (
        f"{_number(metrics.spectral_candidate_count)} registros · "
        f"{_ha(metrics.spectral_candidate_area_ha)}"
    )
    cards = [
        ("Superficie declarada", _ha(metrics.establishment_area_ha), "blue"),
        ("Bosque 2020", _ha(baseline.automated_forest_area_ha), "green"),
        ("No bosque", _ha(baseline.automated_nonforest_area_ha), "gray"),
        ("Revisión requerida", _ha(baseline.review_required_area_ha), "amber"),
        ("Sin datos", _ha(baseline.insufficient_data_area_ha), "gray"),
        ("Eventos de cambio", event_summary, "amber"),
        (
            "Área compatible con conversión",
            _ha(metrics.likely_conversion_area_ha),
            "red" if (metrics.likely_conversion_area_ha or 0) > 0 else "green",
        ),
        ("Estado de revisión", _review_state(view), "amber"),
    ]
    data = [
        [_dashboard_card(*cards[index], styles) for index in range(row, row + 2)]
        for row in range(0, len(cards), 2)
    ]
    table = Table(data, colWidths=[7.45 * cm, 7.45 * cm], rowHeights=[2.0 * cm] * 4)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _dashboard_card(
    label: str,
    value: str,
    tone: str,
    styles: dict[str, ParagraphStyle],
) -> Table:
    palette = {
        "blue": (_LIGHT_BLUE, _BLUE),
        "green": (_LIGHT_GREEN, _GREEN),
        "amber": (_LIGHT_AMBER, _AMBER),
        "red": (colors.HexColor("#FDECEC"), _RED),
        "gray": (_LIGHT_GRAY, _MUTED),
    }
    background, accent = palette[tone]
    table = Table(
        [
            [Paragraph(_escape(label), styles["dashboard_label"])],
            [Paragraph(_escape(value), styles["dashboard_value"])],
        ]
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 1.0, accent),
                ("LINEBEFORE", (0, 0), (0, -1), 4.0, accent),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, 0), 7),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 7),
            ]
        )
    )
    return table


def _interpretation_layers_table(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> Table:
    detected = view.metrics.spectral_candidate_count or 0
    compatible = view.metrics.conversion_likely_count or 0
    rows = [
        (
            "1 · Cambio de cobertura detectado",
            f"{detected} registros, {_ha(view.metrics.spectral_candidate_area_ha)}",
            "Señal espacial posterior al corte; no atribuye causa.",
        ),
        (
            "2 · Evidencia compatible con conversión",
            f"{compatible} eventos, {_ha(view.metrics.likely_conversion_area_ha)}",
            "Exige convergencia de bosque, pérdida, persistencia, uso y superficie.",
        ),
        (
            "3 · Decisión humana y estado de revisión",
            _review_state(view),
            "La conclusión documentada corresponde al operador, auditor o certificador.",
        ),
    ]
    data = [
        [
            Paragraph(_escape(label), styles["table_cell_bold"]),
            Paragraph(_escape(value), styles["table_cell_bold"]),
            Paragraph(_escape(explanation), styles["table_cell"]),
        ]
        for label, value, explanation in rows
    ]
    table = Table(data, colWidths=[5.2 * cm, 3.5 * cm, 6.6 * cm])
    table.setStyle(_table_style(header=False))
    return table


def _identification_section(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> list[Any]:
    return [
        PageBreak(),
        _section_heading(
            "3. Identificación y geolocalización",
            styles["heading"],
            "identification",
            outline_title="Identificación y geolocalización",
        ),
        _two_column_table(
            [
                ("Establecimiento", view.analysis.establishment_id),
                ("ID abreviado del análisis", view.analysis.analysis_id[:8]),
                ("Tipo de geometría", view.geometry.geometry_type),
                ("CRS de intercambio", view.geometry.normalized_crs),
                ("CRS de cálculo de superficie", view.geometry.calculation_crs),
                ("Superficie declarada", _ha(view.metrics.establishment_area_ha)),
                ("Reparación topológica", "Sí" if view.geometry.repair_performed else "No"),
            ],
            styles,
        ),
        Spacer(1, 0.45 * cm),
        events_overview_map(
            (),
            establishment_geometry=view.establishment_geometry,
            width=15.3 * cm,
            height=9.5 * cm,
        ),
        Paragraph(
            "La geometría se intercambia en WGS 84 (EPSG:4326). Las superficies se calculan "
            f"en {view.geometry.calculation_crs} para evitar distorsiones métricas.",
            styles["fine_left"],
        ),
    ]


def _eudr_scope_section(styles: dict[str, ParagraphStyle]) -> list[Any]:
    rows = [
        ("Geolocalización", "Cubierta", "Geometría declarada y validada espacialmente."),
        ("Bosque de referencia 2020", "Cubierto", "Línea base automática al 31/12/2020."),
        ("Cambios posteriores", "Cubiertos", "Serie temporal posterior a la fecha de corte."),
        (
            "Conversión a uso agropecuario",
            "Evaluada técnicamente",
            "Atribución automática sujeta a evidencia y revisión.",
        ),
        ("Legalidad", "No evaluada", "Requiere fuentes y procedimientos externos."),
        (
            "Trazabilidad de animales o productos",
            "No evaluada",
            "No forma parte del expediente geoespacial.",
        ),
        ("Certificación EUDR", "Fuera del alcance", "El informe no certifica cumplimiento."),
    ]
    data = [
        [
            Paragraph("Aspecto", styles["table_header"]),
            Paragraph("Cobertura", styles["table_header"]),
            Paragraph("Alcance de esta evidencia", styles["table_header"]),
        ]
    ]
    data.extend(
        [
            Paragraph(_escape(aspect), styles["table_cell_bold"]),
            Paragraph(_escape(status), styles["table_cell_bold"]),
            Paragraph(_escape(scope), styles["table_cell"]),
        ]
        for aspect, status, scope in rows
    )
    table = Table(data, colWidths=[5.3 * cm, 3.6 * cm, 6.4 * cm], repeatRows=1)
    table.setStyle(_table_style())
    return [
        PageBreak(),
        _section_heading(
            "4. Alcance EUDR",
            styles["heading"],
            "eudr-scope",
            outline_title="Alcance EUDR",
        ),
        Paragraph(
            "La tabla distingue lo cubierto por el análisis geoespacial de aquello que "
            "requiere verificaciones legales, documentales o de trazabilidad independientes.",
            styles["body"],
        ),
        table,
        Spacer(1, 0.6 * cm),
        Paragraph(LEGAL_DISCLAIMER, styles["disclaimer"]),
    ]


def _component_available(view: ReportViewModel, name: str) -> bool:
    return any(component.name == name and component.available for component in view.components)


def _events_overview_section(
    view: ReportViewModel,
    priority: tuple[EventSummary, ...],
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    labelled = frozenset(event.candidate_id for event in priority)
    remaining = max(len(view.events) - len(priority), 0)
    return [
        PageBreak(),
        _section_heading(
            "5. Resultado cartográfico",
            styles["heading"],
            "cartography",
            outline_title="Resultado cartográfico",
        ),
        Paragraph(
            "El mapa representa todos los cambios conservados en el inventario. Sólo se "
            "rotulan los registros prioritarios para mantener legibilidad; los restantes se "
            "muestran sin etiqueta y permanecen trazables en el anexo.",
            styles["body"],
        ),
        events_overview_map(
            view.events,
            establishment_geometry=view.establishment_geometry,
            label_candidate_ids=labelled,
            width=15.3 * cm,
            height=12.2 * cm,
        ),
        Spacer(1, 0.3 * cm),
        _cartographic_summary_table(view, len(priority), remaining, styles),
        Spacer(1, 0.25 * cm),
        Paragraph(
            "La simbología combina color, rótulo y descripción textual. El mapa base es "
            "contexto de presentación y no forma parte de la evidencia científica.",
            styles["fine_left"],
        ),
    ]


def _cartographic_summary_table(
    view: ReportViewModel,
    priority_count: int,
    remaining_count: int,
    styles: dict[str, ParagraphStyle],
) -> Table:
    return _two_column_table(
        [
            ("Registros detectados", _number(view.metrics.spectral_candidate_count)),
            ("Superficie detectada", _ha(view.metrics.spectral_candidate_area_ha)),
            (
                "Candidatos sobre la referencia de 0,5 ha",
                f"{_number(view.metrics.candidate_episode_count)} · "
                f"{_ha(view.metrics.candidate_episode_area_ha)}",
            ),
            ("Eventos prioritarios rotulados", str(priority_count)),
            ("Registros restantes sin rótulo", str(remaining_count)),
            ("Área compatible con conversión", _ha(view.metrics.likely_conversion_area_ha)),
        ],
        styles,
    )


def _priority_events(view: ReportViewModel) -> tuple[EventSummary, ...]:
    ranked = sorted(
        view.events,
        key=lambda event: (
            event.record_type == "conversion_likely_event",
            event.likely_conversion_area_ha or 0.0,
            event.area_ha,
            -event.ordinal,
        ),
        reverse=True,
    )
    return tuple(ranked[:_PRIORITY_EVENT_LIMIT])


def _priority_event_sections(
    view: ReportViewModel,
    priority: tuple[EventSummary, ...],
    styles: dict[str, ParagraphStyle],
    osm_basemap_provider: OSMBasemapProvider,
) -> list[Any]:
    result: list[Any] = [
        NextPageTemplate("landscape"),
        PageBreak(),
        _section_heading(
            "6. Eventos prioritarios",
            styles["landscape_heading"],
            "priority",
            outline_title="Eventos prioritarios",
        ),
        Paragraph(
            "La prioridad se determina de forma reproducible: primero evidencia compatible "
            "con conversión y luego mayor superficie observada. La prioridad ordena la "
            "revisión; no modifica el resultado científico.",
            styles["landscape_body"],
        ),
    ]
    if not priority:
        result.extend(
            [
                Paragraph("No hay registros prioritarios para presentar.", styles["lead"]),
                NextPageTemplate("content"),
            ]
        )
        return result
    for index, event in enumerate(priority, start=1):
        # Cada ficha es una unidad editorial indivisible.  Dejamos la portada
        # de la sección en su propia página para que el encabezado no consuma
        # el espacio reservado a la primera ficha.
        result.append(PageBreak())
        result.append(
            KeepTogether(
                _priority_event_page(
                    view,
                    event,
                    index,
                    _event_client_figure(view, event),
                    styles,
                    osm_basemap_provider,
                )
            )
        )
    result.extend([NextPageTemplate("content")])
    return result


def _priority_event_page(
    view: ReportViewModel,
    event: EventSummary,
    priority_ordinal: int,
    client_figure: ClientFigure | None,
    styles: dict[str, ParagraphStyle],
    osm_basemap_provider: OSMBasemapProvider,
) -> list[Any]:
    # El contenido completo (incluidas las filas de occurrence v2) debe caber
    # en el frame landscape de una sola página.  Estas dimensiones conservan
    # legibilidad y dejan margen para IDs y estados largos.
    map_width, map_height = 12.5 * cm, 4.7 * cm
    geometry = event.candidate_geometry or event.event_geometry
    basemap = (
        osm_basemap_provider.get(
            agricultural_map_bounds(geometry, width=map_width, height=map_height)
        )
        if geometry
        else None
    )
    map_flowable = agricultural_support_map(
        event,
        establishment_geometry=view.establishment_geometry,
        basemap=basemap,
        width=map_width,
        height=map_height,
    )
    evidence_flowable: Any
    if client_figure is None:
        evidence_flowable = Paragraph(
            "Comparación antes/después no disponible en los assets seleccionados.",
            styles["landscape_body"],
        )
    else:
        evidence_flowable = _scaled_report_image(
            client_figure,
            max_width=12.5 * cm,
            max_height=4.7 * cm,
        )
    evidence_table = Table(
        [
            [
                Paragraph("Mapa y uso posterior", styles["panel_heading"]),
                Paragraph("Evidencia espectral y serie temporal", styles["panel_heading"]),
            ],
            [map_flowable, evidence_flowable],
        ],
        colWidths=[12.9 * cm, 12.9 * cm],
    )
    evidence_table.setStyle(_panel_table_style())
    lower_table = Table(
        [
            [
                _priority_summary_table(event, styles),
                forest_trajectory_chart(event, width=12.5 * cm, height=2.8 * cm),
            ]
        ],
        colWidths=[12.9 * cm, 12.9 * cm],
    )
    lower_table.setStyle(_panel_table_style())
    return [
        _section_heading(
            f"6.{priority_ordinal}. {_record_title(event)}",
            styles["landscape_subheading"],
            f"priority-{priority_ordinal}",
            level=1,
        ),
        Paragraph(
            f"{_event_status_label(event)} · {_ha(event.area_ha)} · inicio estimado "
            f"{_event_onset(event)} · revisión humana {_review_label(event)}.",
            styles["status_banner"],
        ),
        Spacer(1, 0.15 * cm),
        evidence_table,
        Spacer(1, 0.12 * cm),
        lower_table,
        Paragraph(
            f"Interpretación técnica: {_event_interpretation(event)}",
            styles["landscape_caption"],
        ),
    ]


def _priority_summary_table(event: EventSummary, styles: dict[str, ParagraphStyle]) -> Table:
    if not event.gates:
        alternative = "No evaluada"
    else:
        alternative = (
            "No respaldada"
            if any(
                gate.key == "strong_alternative_explanation_absent" and gate.passed
                for gate in event.gates
            )
            else "Presente o pendiente de evaluación"
        )
    rows = [
        ("ID técnico", event.candidate_id),
        ("Estado", _event_status_label(event)),
        ("Área y fecha", f"{_ha(event.area_ha)} · {_event_onset(event)}"),
        ("Uso posterior", _event_post_use_label(event)),
        ("Área compatible", _ha(event.likely_conversion_area_ha)),
        ("Explicación alternativa", alternative),
        ("Estado de revisión", _review_label(event)),
        ("Condiciones", _compact_gate_result(event)),
    ]
    rows[4:4] = _agricultural_occurrence_rows(event)
    return _two_column_table(rows, styles, widths=(4.2 * cm, 8.0 * cm), compact=True)


def _compact_gate_result(event: EventSummary) -> str:
    if not event.gates:
        return "no evaluadas"
    passed = sum(gate.passed for gate in event.gates)
    return f"{passed} de {len(event.gates)} satisfechas"


def _review_label(event: EventSummary) -> str:
    return "pendiente" if event.human_review_required else "sin revisión requerida por el modelo"


def _temporal_evidence_section(
    view: ReportViewModel, styles: dict[str, ParagraphStyle]
) -> list[Any]:
    figures = [
        figure
        for figure in view.client_figures
        if figure.kind
        in {"annual_forest_change", "rgb_timeline", "dynamic_world_annual_land_cover"}
    ]
    if not figures:
        return [
            NextPageTemplate("content"),
            PageBreak(),
            _section_heading(
                "7. Evidencia temporal",
                styles["heading"],
                "temporal",
                outline_title="Evidencia temporal",
            ),
            Paragraph(
                "No hay figuras temporales disponibles en el contrato editorial verificado.",
                styles["body"],
            ),
        ]
    result: list[Any] = [NextPageTemplate("landscape")]
    first_page = True
    for figure in figures:
        for page in _global_figure_pages(figure, view, styles):
            result.append(PageBreak())
            if first_page:
                result.extend(
                    [
                        _section_heading(
                            "7. Evidencia temporal",
                            styles["landscape_heading"],
                            "temporal",
                            outline_title="Evidencia temporal",
                        ),
                        Paragraph(
                            "La lectura principal prioriza referencia 2020, ventana del cambio, "
                            "período posterior y situación reciente. Las series exhaustivas y la "
                            "calidad observacional se documentan en los anexos.",
                            styles["landscape_body"],
                        ),
                    ]
                )
                first_page = False
            result.extend(page)
    result.append(NextPageTemplate("content"))
    return result


def _conclusion_section(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> list[Any]:
    messages = _headline_messages(view)
    pending = [
        "Revisar los registros prioritarios y las explicaciones alternativas.",
        "Contrastar con documentación del establecimiento y, cuando corresponda, "
        "evidencia de campo.",
        "Documentar la conclusión humana sin sustituir las verificaciones de legalidad "
        "y trazabilidad.",
    ]
    return [
        PageBreak(),
        _section_heading(
            "8. Conclusión y revisión humana",
            styles["heading"],
            "conclusion",
            outline_title="Conclusión y revisión humana",
        ),
        Paragraph("Resultado automático", styles["subheading"]),
        Paragraph(messages[0], styles["headline_primary"]),
        Paragraph(messages[1], styles["body"]),
        Paragraph(messages[2], styles["body"]),
        Paragraph("Interpretación técnica", styles["subheading"]),
        Paragraph(
            "El resultado distingue cambio detectado, atribución compatible con conversión "
            "y decisión humana. La ausencia de evidencia suficiente de conversión no equivale "
            "a certificación de cumplimiento.",
            styles["body"],
        ),
        Paragraph("Aspectos pendientes", styles["subheading"]),
        *[_bullet(item, styles) for item in pending],
        Paragraph("Estado de revisión humana", styles["subheading"]),
        _review_fields_table(view, styles),
        Spacer(1, 0.5 * cm),
        Paragraph(LEGAL_DISCLAIMER, styles["disclaimer"]),
    ]


def _review_fields_table(view: ReportViewModel, styles: dict[str, ParagraphStyle]) -> Table:
    rows = [
        ("Estado", _review_state(view)),
        ("Revisor", "________________________________________"),
        ("Fecha", "____ / ____ / ________"),
        ("Observaciones", "\n\n\n"),
    ]
    data = [
        [
            Paragraph(_escape(label), styles["table_cell_bold"]),
            Paragraph(_escape(value), styles["table_cell"]),
        ]
        for label, value in rows
    ]
    table = Table(
        data,
        colWidths=[5.0 * cm, 10.0 * cm],
        rowHeights=[None, None, None, 2.4 * cm],
        hAlign="LEFT",
    )
    table.setStyle(_table_style(header=False, compact=True))
    return table


def _technical_annexes(
    view: ReportViewModel,
    priority: tuple[EventSummary, ...],
    styles: dict[str, ParagraphStyle],
    *,
    standalone: bool = False,
) -> list[Any]:
    section_prefix = "A" if standalone else "9"
    result: list[Any] = [
        _section_heading(
            "Anexos técnicos" if standalone else "9. Anexos técnicos",
            styles["heading"],
            "annexes",
            outline_title="Anexos técnicos",
        ),
        Paragraph(
            "Los anexos conservan el inventario operativo, el resumen subumbral, las fichas "
            "secundarias, las series temporales, la calidad observacional, las fuentes y la "
            "trazabilidad técnica.",
            styles["body"],
        ),
        _section_heading(
            f"{section_prefix}.1. Inventario operativo y resumen subumbral",
            styles["subheading"],
            "annex-inventory",
            level=1,
        ),
        Paragraph(
            "El PDF individualiza los candidatos estrictamente mayores a 0,5 ha. Los "
            "candidatos en o bajo el umbral se condensan para evitar sobrecargar la lectura.",
            styles["body"],
        ),
        _event_inventory_table(
            tuple(event for event in view.events if event.area_ha > event.area_threshold_ha),
            styles,
        ),
        Spacer(1, 0.3 * cm),
        Paragraph("Resumen de candidatos subumbrales", styles["subheading"]),
        _subthreshold_summary_table(view.events, styles),
    ]
    if not standalone:
        result.insert(0, PageBreak())
    result.extend(_secondary_event_annex(view, priority, styles, section_prefix=section_prefix))
    result.extend(_complete_temporal_annex(view, styles, section_prefix=section_prefix))
    result.extend(_methodology_quality_annex(view, styles, section_prefix=section_prefix))
    result.extend(_sources_annex(view, styles, section_prefix=section_prefix))
    result.extend(_traceability_annex(view, styles, section_prefix=section_prefix))
    return result


def _secondary_event_annex(
    view: ReportViewModel,
    priority: tuple[EventSummary, ...],
    styles: dict[str, ParagraphStyle],
    *,
    section_prefix: str = "9",
) -> list[Any]:
    priority_ids = {event.candidate_id for event in priority}
    secondary = [
        event
        for event in view.events
        if event.candidate_id not in priority_ids and event.area_ha > event.area_threshold_ha
    ]
    result: list[Any] = [
        PageBreak(),
        _section_heading(
            f"{section_prefix}.2. Fichas detalladas de eventos secundarios",
            styles["subheading"],
            "annex-secondary",
            level=1,
        ),
    ]
    if not secondary:
        result.append(Paragraph("No hay registros secundarios.", styles["body"]))
        return result
    for event in secondary:
        alternative = (
            "No respaldada"
            if any(
                gate.key == "strong_alternative_explanation_absent" and gate.passed
                for gate in event.gates
            )
            else "Presente o pendiente"
        )
        result.extend(
            [
                PageBreak(),
                KeepTogether(
                    [
                        Paragraph(
                            f"{_record_title(event)} · {_event_status_label(event)}",
                            styles["annex_event_heading"],
                        ),
                        Spacer(1, 0.2 * cm),
                        _compact_event_table(event, alternative, styles),
                    ]
                ),
            ]
        )
    return result


def _compact_event_table(
    event: EventSummary,
    alternative: str,
    styles: dict[str, ParagraphStyle],
    *,
    narrow: bool = False,
) -> Table:
    fields = [
        ("ID técnico", event.candidate_id),
        ("Área / inicio", f"{_ha(event.area_ha)} · {_event_onset(event)}"),
        ("Uso posterior", _event_post_use_label(event)),
        ("Área compatible", _ha(event.likely_conversion_area_ha)),
        ("Condiciones", _compact_gate_result(event)),
        ("Explicación alternativa", alternative),
        ("Revisión", _review_label(event)),
    ]
    fields[3:3] = _agricultural_occurrence_rows(event)
    data = [
        [
            Paragraph(_escape(label), styles["annex_label"]),
            Paragraph(_escape(value), styles["annex_value"]),
        ]
        for label, value in fields
    ]
    widths = [2.15 * cm, 5.1 * cm] if narrow else [4.1 * cm, 11.2 * cm]
    table = Table(data, colWidths=widths)
    table.setStyle(_table_style(header=False, compact=True))
    return table


def _complete_temporal_annex(
    view: ReportViewModel,
    styles: dict[str, ParagraphStyle],
    *,
    section_prefix: str = "9",
) -> list[Any]:
    specs = {
        "spectral_index_timeline": "Serie completa de índices espectrales",
        "observation_coverage": "Calidad observacional temporal",
        "disturbance_detection": "Detección temporal de perturbaciones",
        "post_change_attribution": "Síntesis espacial de atribución post-cambio",
    }
    result: list[Any] = [
        NextPageTemplate("landscape"),
        PageBreak(),
        _section_heading(
            f"{section_prefix}.3. Series temporales completas",
            styles["landscape_heading"],
            "annex-temporal",
            level=1,
        ),
    ]
    figures = [figure for figure in view.client_figures if figure.kind in specs]
    if not figures:
        result.append(Paragraph("No hay figuras temporales adicionales.", styles["body"]))
    for index, figure in enumerate(figures):
        if index:
            result.append(PageBreak())
        result.extend(
            [
                Paragraph(specs[figure.kind], styles["landscape_subheading"]),
                _scaled_report_image(figure, max_width=26.2 * cm, max_height=15.0 * cm),
                Paragraph(
                    "Figura técnica íntegra conservada para auditoría. Su lectura debe "
                    "considerar las fuentes, resolución y limitaciones documentadas.",
                    styles["landscape_caption"],
                ),
            ]
        )
    result.append(NextPageTemplate("content"))
    return result


def _methodology_quality_annex(
    view: ReportViewModel,
    styles: dict[str, ParagraphStyle],
    *,
    section_prefix: str = "9",
) -> list[Any]:
    quality = view.quality
    coverage = (
        "No disponible"
        if quality.minimum_valid_pixel_fraction is None
        else _percent(quality.minimum_valid_pixel_fraction)
    )
    observation_range = (
        "No disponible"
        if quality.minimum_mean_observation_count is None
        or quality.maximum_mean_observation_count is None
        else (
            f"{quality.minimum_mean_observation_count:.1f} a "
            f"{quality.maximum_mean_observation_count:.1f}"
        ).replace(".", ",")
    )
    return [
        PageBreak(),
        _section_heading(
            f"{section_prefix}.4. Metodología y calidad observacional",
            styles["subheading"],
            "annex-methodology",
            level=1,
        ),
        Paragraph(
            "El análisis utiliza una línea base forestal 2020, detección multitemporal, "
            "evaluación de persistencia y atribución del uso posterior. Este anexo resume "
            "parámetros de lectura sin reemplazar los artefactos estructurados del expediente.",
            styles["body"],
        ),
        _two_column_table(
            [
                ("Fecha de corte", _EUDR_CUTOFF),
                ("Períodos estacionales", str(quality.seasonal_period_count)),
                ("Cobertura válida mínima", coverage),
                (
                    "Períodos de detección evaluables",
                    f"{quality.evaluable_detection_period_count} de "
                    f"{quality.detection_period_count}",
                ),
                ("Observaciones medias por período", observation_range),
                ("Umbral operativo de superficie", _ha(view.geometry.threshold_ha)),
                ("CRS de cálculo", view.geometry.calculation_crs),
                ("Método de superficie", view.geometry.area_method),
            ],
            styles,
        ),
        Spacer(1, 0.45 * cm),
        Paragraph("Limitaciones", styles["subheading"]),
        *[_bullet(item, styles) for item in view.limitations],
    ]


def _sources_annex(
    view: ReportViewModel,
    styles: dict[str, ParagraphStyle],
    *,
    section_prefix: str = "9",
) -> list[Any]:
    return [
        PageBreak(),
        _section_heading(
            f"{section_prefix}.5. Fuentes y licencias",
            styles["subheading"],
            "annex-sources",
            level=1,
        ),
        _datasets_table(view.datasets, styles),
        Spacer(1, 0.35 * cm),
        Paragraph(
            "Las fuentes cumplen roles distintos. Ningún producto global se interpreta como "
            "verdad de terreno individual.",
            styles["body"],
        ),
    ]


def _traceability_annex(
    view: ReportViewModel,
    styles: dict[str, ParagraphStyle],
    *,
    section_prefix: str = "9",
) -> list[Any]:
    return [
        PageBreak(),
        _section_heading(
            f"{section_prefix}.6. Trazabilidad técnica",
            styles["subheading"],
            "annex-traceability",
            level=1,
        ),
        _two_column_table(
            [
                ("ID completo del análisis", view.analysis.analysis_id),
                ("Contrato editorial", view.schema_version),
                ("Fecha registrada", view.analysis.recorded_at.isoformat()),
                ("Assets verificados", str(view.verified_asset_count)),
                ("CRS de intercambio", view.geometry.normalized_crs),
                ("CRS de cálculo", view.geometry.calculation_crs),
            ],
            styles,
        ),
        Spacer(1, 0.5 * cm),
        Paragraph("Estado de componentes", styles["subheading"]),
        _components_table(view, styles),
        Spacer(1, 0.45 * cm),
        Paragraph(
            "Los archivos declarados por report_assets/index.json se verifican por tamaño y "
            "SHA-256 antes del renderizado. Los hashes individuales, versiones, fuentes y "
            "artefactos estructurados permanecen en el expediente descargable.",
            styles["body"],
        ),
        Paragraph(LEGAL_DISCLAIMER, styles["disclaimer"]),
    ]


def _global_figure_pages(
    figure: ClientFigure,
    view: ReportViewModel,
    styles: dict[str, ParagraphStyle],
) -> list[list[Any]]:
    if figure.kind == "annual_forest_change":
        return _annual_forest_pages(figure, styles)
    if figure.kind == "dynamic_world_annual_land_cover":
        return _dynamic_world_annual_land_cover_pages(figure, styles)
    return _rgb_timeline_pages(figure, view.seasonal_period_ids, styles)


def _dynamic_world_annual_land_cover_pages(
    figure: ClientFigure, styles: dict[str, ParagraphStyle]
) -> list[list[Any]]:
    return [
        [
            Paragraph(
                "7.5. Cobertura/uso del suelo anual Dynamic World",
                styles["landscape_heading"],
            ),
            Paragraph(
                "Cada año resume por píxel la clase top-1 más frecuente (moda) dentro del "
                "dominio de candidatos RF-first. Es contexto descriptivo de cobertura/uso "
                "del suelo y no reemplaza los gates de atribución agrícola.",
                styles["landscape_body"],
            ),
            _scaled_report_image(figure, max_width=26.2 * cm, max_height=14.2 * cm),
            Paragraph(
                "Códigos y paleta oficial: 0 agua, 1 árboles, 2 pastizal, 3 vegetación "
                "inundada, 4 cultivos, 5 arbustos/matorral, 6 construido, 7 suelo desnudo y "
                "8 nieve/hielo. Fuente: GOOGLE/DYNAMICWORLD/V1, 10 m, CC-BY-4.0. "
                "Referencia: Brown et al. (2022), doi:10.1038/s41597-022-01307-4.",
                styles["landscape_caption"],
            ),
        ]
    ]


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
                "7.1. Cobertura forestal anual: referencia y trayectoria",
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
                "7.2. Cambios anuales respecto de la referencia 2020",
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
                "7.3. Referencia 2020 y primeras ventanas de cambio",
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
                "7.4. Período posterior y situación reciente",
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


def _event_client_figure(view: ReportViewModel, event: EventSummary) -> ClientFigure | None:
    return next(
        (
            figure
            for figure in view.client_figures
            if figure.kind == "event_spectral_evidence" and figure.event_id == event.candidate_id
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


def _event_inventory_table(
    events: tuple[EventSummary, ...], styles: dict[str, ParagraphStyle]
) -> Table:
    occurrence_contract = any(_uses_agricultural_occurrence_contract(event) for event in events)
    data: list[list[Any]] = [
        [
            Paragraph("ID", styles["table_header"]),
            Paragraph("Inicio", styles["table_header"]),
            Paragraph("Área", styles["table_header"]),
            Paragraph(
                "Evidencia agrícola post-evento"
                if occurrence_contract
                else "Agricultura persistente (legado v1)",
                styles["table_header"],
            ),
            Paragraph("Área compatible", styles["table_header"]),
            Paragraph("Resultado", styles["table_header"]),
        ]
    ]
    for event in events:
        data.append(
            [
                Paragraph(_record_code(event), styles["table_cell_bold"]),
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


def _subthreshold_summary_table(
    events: tuple[EventSummary, ...], styles: dict[str, ParagraphStyle]
) -> Table:
    subthreshold = [event for event in events if event.area_ha <= event.area_threshold_ha]
    if not subthreshold:
        rows = [("Estado", "No se detectaron candidatos en o bajo 0,5 ha.")]
    else:
        areas = sorted(event.area_ha for event in subthreshold)
        middle = len(areas) // 2
        median = areas[middle] if len(areas) % 2 else (areas[middle - 1] + areas[middle]) / 2
        with_agriculture = sum((event.qualifying_period_count or 0) > 0 for event in subthreshold)
        review_required = sum(event.human_review_required for event in subthreshold)
        noun = "candidato" if len(subthreshold) == 1 else "candidatos"
        rows = [
            ("Registros agrupados", f"{len(subthreshold)} {noun} ≤ 0,5 ha"),
            ("Área acumulada", _ha(sum(areas))),
            ("Mediana / rango", f"{_ha(median)} / {_ha(areas[0])} a {_ha(areas[-1])}"),
            ("Con ocurrencia agrícola", f"{with_agriculture} con al menos un mes calificante"),
            ("Revisión requerida", f"{review_required} de {len(subthreshold)}"),
            (
                "Detalle íntegro",
                "report_assets/annex/tables/subthreshold_candidates.csv",
            ),
        ]
    data = [
        [
            Paragraph(_escape(label), styles["table_cell_bold"]),
            Paragraph(_escape(value), styles["table_cell"]),
        ]
        for label, value in rows
    ]
    table = Table(data, colWidths=[5.0 * cm, 10.3 * cm], hAlign="LEFT")
    table.setStyle(_table_style(header=False, compact=True))
    return table


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
            Paragraph("Disponibilidad / motivo", styles["table_header"]),
        ]
    ]
    for component in view.components:
        data.append(
            [
                Paragraph(
                    _escape(
                        _component_label(
                            component.name,
                            occurrence_contract=any(
                                _uses_agricultural_occurrence_contract(event)
                                for event in view.events
                            ),
                        )
                    ),
                    styles["table_cell"],
                ),
                Paragraph(_escape(_component_status_label(component.status)), styles["table_cell"]),
                Paragraph(
                    _escape(
                        "Disponible"
                        if component.available
                        else f"No disponible: {component.reason or component.status}"
                    ),
                    styles["table_cell"],
                ),
            ]
        )
    table = Table(data, colWidths=[5.0 * cm, 3.7 * cm, 6.6 * cm], repeatRows=1)
    table.setStyle(_table_style())
    return table


def _two_column_table(
    rows: list[tuple[str, str]],
    styles: dict[str, ParagraphStyle],
    *,
    widths: tuple[float, float] = (8.2 * cm, 7.1 * cm),
    compact: bool = False,
) -> Table:
    data = [
        [
            Paragraph(_escape(label), styles["table_cell_bold"]),
            Paragraph(_escape(value), styles["table_cell"]),
        ]
        for label, value in rows
    ]
    table = Table(data, colWidths=list(widths))
    padding = 3 if compact else 5
    table.setStyle(
        TableStyle(
            [
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, _LIGHT_BLUE]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), padding),
                ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
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
        "cover_field_label": ParagraphStyle(
            "CoverFieldLabel",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=9.5,
            leading=12.5,
            textColor=_BLUE,
        ),
        "cover_field_value": ParagraphStyle(
            "CoverFieldValue",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=9.5,
            leading=12.5,
            textColor=_TEXT,
        ),
        "cover_result": ParagraphStyle(
            "CoverResult",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=15,
            leading=20,
            textColor=_GREEN,
            alignment=TA_CENTER,
            borderColor=_GREEN,
            borderWidth=1,
            borderPadding=12,
            backColor=_LIGHT_GREEN,
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
            fontSize=9.7,
            leading=14.0,
            textColor=_TEXT,
            alignment=TA_LEFT,
            spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=9.5,
            leading=13.5,
            leftIndent=10,
            firstLineIndent=-7,
            textColor=_TEXT,
            spaceAfter=3,
        ),
        "fine_left": ParagraphStyle(
            "FineLeft",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=7.8,
            leading=10.2,
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
            fontSize=9.5,
            leading=12.5,
            textColor=_TEXT,
            spaceAfter=3,
        ),
        "landscape_caption": ParagraphStyle(
            "LandscapeCaption",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=8.0,
            leading=10.2,
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
            fontSize=8.0,
            leading=9.8,
            textColor=colors.white,
        ),
        "table_cell": ParagraphStyle(
            "TableCell",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=7.8,
            leading=9.7,
            textColor=_TEXT,
        ),
        "table_cell_bold": ParagraphStyle(
            "TableCellBold",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=7.8,
            leading=9.7,
            textColor=_TEXT,
        ),
        "headline_primary": ParagraphStyle(
            "HeadlinePrimary",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=14,
            leading=18,
            textColor=_BLUE,
            spaceAfter=8,
        ),
        "headline_secondary": ParagraphStyle(
            "HeadlineSecondary",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=10,
            leading=14,
            textColor=_TEXT,
            spaceAfter=3,
        ),
        "dashboard_label": ParagraphStyle(
            "DashboardLabel",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=8.2,
            leading=10,
            textColor=_MUTED,
        ),
        "dashboard_value": ParagraphStyle(
            "DashboardValue",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=12,
            leading=15,
            textColor=_TEXT,
        ),
        "landscape_subheading": ParagraphStyle(
            "LandscapeSubheading",
            parent=base["Heading2"],
            fontName=_FONT_BOLD,
            fontSize=12.5,
            leading=15,
            textColor=_BLUE,
            spaceAfter=4,
        ),
        "panel_heading": ParagraphStyle(
            "PanelHeading",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=8.5,
            leading=10.5,
            textColor=_BLUE,
            alignment=TA_CENTER,
        ),
        "status_banner": ParagraphStyle(
            "StatusBanner",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=9.2,
            leading=12,
            textColor=_AMBER,
            backColor=_LIGHT_AMBER,
            borderColor=_AMBER,
            borderWidth=0.6,
            borderPadding=5,
        ),
        "annex_event_heading": ParagraphStyle(
            "AnnexEventHeading",
            parent=base["Heading3"],
            fontName=_FONT_BOLD,
            fontSize=9.5,
            leading=12,
            textColor=_BLUE,
            spaceAfter=3,
        ),
        "annex_label": ParagraphStyle(
            "AnnexLabel",
            parent=base["BodyText"],
            fontName=_FONT_BOLD,
            fontSize=7.2,
            leading=8.7,
            textColor=_MUTED,
        ),
        "annex_value": ParagraphStyle(
            "AnnexValue",
            parent=base["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=7.2,
            leading=8.7,
            textColor=_TEXT,
        ),
    }


def _table_style(*, header: bool = True, compact: bool = False) -> TableStyle:
    first_data_row = 1 if header else 0
    padding = 2 if compact else 4
    commands: list[tuple[Any, ...]] = [
        ("ROWBACKGROUNDS", (0, first_data_row), (-1, -1), [colors.white, _LIGHT_BLUE]),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
    ]
    if header:
        commands.insert(0, ("BACKGROUND", (0, 0), (-1, 0), _BLUE))
    return TableStyle(commands)


def _panel_table_style() -> TableStyle:
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), _LIGHT_BLUE),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D7DEE7")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
    )


def _page_template(
    template_id: str,
    view: ReportViewModel,
    emission_time: datetime,
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
            emission_time,
            cover=cover,
            pagesize=pagesize,
        ),
    )


def _decorate_page(
    pdf: canvas.Canvas,
    _doc: Any,
    view: ReportViewModel,
    emission_time: datetime,
    *,
    cover: bool,
    pagesize: tuple[float, float],
) -> None:
    page_width, page_height = pagesize
    horizontal_margin = 1.4 * cm if page_width > page_height else 1.7 * cm
    pdf.saveState()
    pdf.setTitle(view.title)
    pdf.setAuthor("Sistema de evidencia geoespacial EUDR")
    pdf.setCreator("Renderer de informes geoespaciales auditables")
    pdf.setSubject("Evidencia técnica geoespacial sujeta a revisión humana")
    pdf.setKeywords("EUDR, evidencia geoespacial, deforestación, auditoría, revisión humana")
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
            f"Análisis {view.analysis.analysis_id[:8]}",
        )
    pdf.setFont(_FONT_REGULAR, 7)
    pdf.setFillColor(_MUTED)
    pdf.drawString(horizontal_margin, 0.72 * cm, f"Contrato editorial {view.schema_version}")
    pdf.drawCentredString(page_width / 2, 0.72 * cm, emission_time.date().isoformat())
    pdf.drawRightString(
        page_width - horizontal_margin,
        0.72 * cm,
        f"Página {pdf.getPageNumber()}",
    )
    pdf.restoreState()


def _dated_canvas(
    filename: str,
    *,
    creation_time: datetime,
    **kwargs: Any,
) -> canvas.Canvas:
    kwargs["invariant"] = 1
    kwargs["pageCompression"] = 1
    pdf = canvas.Canvas(filename, **kwargs)
    previous_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    try:
        os.environ["SOURCE_DATE_EPOCH"] = str(int(creation_time.timestamp()))
        pdf._doc._timeStamp = TimeStamp(invariant=True)
    finally:
        if previous_epoch is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = previous_epoch
    return pdf


def _event_status_label(event: EventSummary) -> str:
    if event.record_type == "conversion_likely_event":
        return "Evidencia compatible con conversión"
    if _event_component_unavailable(event, "post_change_attribution"):
        return "Candidato persistente; atribución no disponible"
    return {
        "candidate_only": "Candidato sin persistencia suficiente",
        "temporary_or_recovered": "Candidato temporal o recuperado",
        "persistent_unattributed": "Candidato persistente sin uso atribuido",
        "insufficient_data": "Candidato con datos insuficientes",
        "subthreshold_conversion_evidence": (
            "Evidencia conjunta bajo umbral operativo; no es evento probable"
        ),
    }.get(event.interpretation_status, "Candidato de perturbación")


def _post_use_label(value: str) -> str:
    return {
        "agriculture_likely": "Uso agrícola probable",
        "cropland": "Cultivo probable",
        "forest_recovery": "Recuperación forestal",
        "forest_or_regeneration": "Bosque o regeneración",
        "pasture": "Pastura",
        "bare_soil": "Suelo desnudo persistente",
        "infrastructure": "Infraestructura",
        "water": "Agua",
        "temporary_disturbance": "Perturbación temporal",
        "unknown": "Uso posterior no determinado",
    }.get(value, value.replace("_", " ").capitalize())


def _event_component_unavailable(event: EventSummary, component_name: str) -> bool:
    return f"{component_name}_unavailable" in event.quality_flags


def _event_post_use_label(event: EventSummary) -> str:
    if _event_component_unavailable(event, "post_change_attribution"):
        return "Uso posterior no evaluado"
    return _post_use_label(event.post_change_use)


def _gate_label(key: str) -> str:
    return {
        "forest_at_cutoff": "Bosque al 31/12/2020",
        "post_cutoff_loss": "Pérdida posterior al corte",
        "persistent_change": "Cambio persistente",
        "agricultural_or_livestock_post_use": "Uso agrícola o ganadero posterior",
        "defensible_area_and_geometry": "Superficie y geometría defendibles",
        "strong_alternative_explanation_absent": "Sin explicación alternativa respaldada",
        "visec_operational_area_strictly_greater_than_threshold": (
            "Conjunción estrictamente mayor a 0,5 ha"
        ),
    }[key]


def _event_interpretation(event: EventSummary) -> str:
    if _event_component_unavailable(event, "post_change_attribution"):
        return (
            "La perturbación se conserva como candidato persistente. La atribución "
            "post-cambio no está disponible; no se evaluaron el uso posterior ni las "
            "condiciones de conversión; requiere revisión humana."
        )
    failed = [_gate_label(gate.key) for gate in event.gates if not gate.passed]
    if event.automatic_status == "conversion_likely":
        return (
            "La línea base forestal, la pérdida posterior al corte, la persistencia y el uso "
            "agrícola posterior convergen espacialmente. Por eso el evento se clasifica como "
            "evidencia compatible con conversión, siempre sujeta a revisión humana."
        )
    if event.interpretation_status == "temporary_or_recovered":
        return (
            "La perturbación se conserva como candidato auditable, pero la trayectoria indica "
            "recuperación o carácter temporal. No se promueve a evento probable."
        )
    if event.interpretation_status == "candidate_only":
        return (
            "La señal no presenta persistencia suficiente para constituir un evento. Se conserva "
            "como candidato espectral y no modifica por sí sola el estado del establecimiento."
        )
    if event.interpretation_status == "subthreshold_conversion_evidence":
        return (
            "La perturbación reúne las condiciones científicas conjuntas, pero la superficie "
            "materializada no supera estrictamente 0,5 ha. Se conserva para revisión como "
            "candidata y no se presenta como evento probable."
        )
    if failed:
        return (
            "La evidencia disponible no satisface todas las condiciones necesarias para una "
            f"atribución fuerte. Requieren revisión: {', '.join(failed).lower()}."
        )
    return (
        "El candidato conserva evidencia automática de cambio, pero su interpretación final "
        "permanece sujeta a revisión humana documentada."
    )


def _record_code(event: EventSummary) -> str:
    prefix = "E" if event.record_type == "conversion_likely_event" else "C"
    return f"{prefix}{event.ordinal}"


def _record_title(event: EventSummary) -> str:
    noun = "Evento probable" if event.record_type == "conversion_likely_event" else "Candidato"
    return f"{noun} {_record_code(event)}"


def _dataset_label(dataset_id: str) -> str:
    return {
        "hls_l30_v2": "HLS Landsat",
        "hls_s30_v2": "HLS Sentinel-2",
        "jrc_gfc2020_v3": "JRC Forest Cover 2020",
        "esa_worldcover_2020_v100": "ESA WorldCover 2020",
        "hansen_gfc_2025_v1_13": "Hansen Global Forest Change",
        "dynamic_world_v1": "Dynamic World V1",
    }.get(dataset_id, dataset_id)


def _component_label(name: str, *, occurrence_contract: bool = False) -> str:
    return {
        "full_pipeline": "Pipeline principal",
        "hampel_benchmark": "Diagnóstico estacional Hampel",
        "rf_annual_deltas": "Trayectoria forestal anual",
        "agricultural_collection": "Recolección agrícola mensual",
        "agricultural_persistence": (
            "Evidencia agrícola post-evento"
            if occurrence_contract
            else "Persistencia agrícola (legado v1)"
        ),
        "post_change_attribution": "Atribución post-cambio",
    }.get(name, name.replace("_", " "))


def _uses_agricultural_occurrence_contract(event: EventSummary) -> bool:
    return event.agricultural_evidence_schema_version in {"2.0.0", "2.1.0"}


def _strength_adjective(signal_strength: str) -> str:
    return {
        "weak": "débil",
        "moderate": "moderada",
        "strong": "fuerte",
    }[signal_strength]


def _agricultural_strength_label(event: EventSummary) -> str:
    if event.agricultural_signal_strength is None:
        return "no disponible"
    return {
        "weak": "Débil (ordinal, no calibrada)",
        "moderate": "Moderada (ordinal, no calibrada)",
        "strong": "Fuerte (ordinal, no calibrada)",
    }[event.agricultural_signal_strength]


def _agricultural_occurrence_rows(event: EventSummary) -> list[tuple[str, str]]:
    if not _uses_agricultural_occurrence_contract(event):
        return []
    effective_date = (
        "no disponible"
        if event.agricultural_effective_observation_date is None
        else event.agricultural_effective_observation_date.strftime("%d/%m/%Y")
    )
    rows = [
        ("Intensidad agrícola", _agricultural_strength_label(event)),
        ("Fecha agrícola efectiva", effective_date),
        ("Meses con ocurrencia", _number(event.qualifying_period_count)),
    ]
    if event.candidate_agricultural_coverage_fraction is not None:
        rows.append(
            (
                "Cobertura agrícola máxima",
                _percent(event.candidate_agricultural_coverage_fraction),
            )
        )
    if event.candidate_agricultural_coverage_threshold is not None:
        gate = (
            "satisfecho"
            if event.candidate_agricultural_coverage_gate_met is True
            else "no satisfecho"
        )
        rows.append(
            (
                "Umbral de cobertura del candidato",
                f"{_percent(event.candidate_agricultural_coverage_threshold)} · {gate}",
            )
        )
    rows.extend(
        (f"Área {_strength_adjective(item.signal_strength)}", _ha(item.area_ha))
        for item in event.agricultural_strength_areas
    )
    return rows


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
