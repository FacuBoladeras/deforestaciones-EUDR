"""Pruebas de la base del paquete."""

from deforestation_pipeline import __version__


def test_package_exposes_version() -> None:
    """La versión pública debe coincidir con la versión inicial del proyecto."""
    assert __version__ == "0.1.0"
