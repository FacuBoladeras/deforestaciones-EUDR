"""Tipos compartidos de selección espacial."""

from enum import StrEnum


class AreaCrsStrategy(StrEnum):
    """Estrategias admitidas para seleccionar un CRS de superficie."""

    AUTO_EQUAL_AREA = "auto_equal_area"
    LOCAL_UTM = "local_utm"
