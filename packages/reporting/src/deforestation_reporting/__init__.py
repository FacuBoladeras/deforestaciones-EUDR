"""Generación de informes técnicos desde contratos editoriales verificados."""

from deforestation_reporting.basemap import OSMBasemapProvider, OverpassOSMProvider
from deforestation_reporting.models import LEGAL_DISCLAIMER, ReportArtifact, ReportViewModel
from deforestation_reporting.renderer import render_technical_report
from deforestation_reporting.source import (
    ReportContractError,
    ReportIntegrityError,
    ReportPackage,
    build_report_view_model,
    load_report_package,
)

__version__ = "0.11.0"

__all__ = [
    "LEGAL_DISCLAIMER",
    "OSMBasemapProvider",
    "OverpassOSMProvider",
    "ReportArtifact",
    "ReportContractError",
    "ReportIntegrityError",
    "ReportPackage",
    "ReportViewModel",
    "build_report_view_model",
    "load_report_package",
    "render_technical_report",
]
