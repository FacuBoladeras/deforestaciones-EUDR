"""Fórmulas ópticas explícitas para reflectancia ya escalada."""

from __future__ import annotations

import math
from dataclasses import dataclass

from deforestation_pipeline.config import SpectralIndex

DENOMINATOR_EPSILON = 1e-12

INDEX_FORMULAS: dict[SpectralIndex, str] = {
    SpectralIndex.NDVI: "(NIR - RED) / (NIR + RED)",
    SpectralIndex.EVI2: "2.5 * (NIR - RED) / (NIR + 2.4 * RED + 1)",
    SpectralIndex.NBR: "(NIR - SWIR2) / (NIR + SWIR2)",
    SpectralIndex.NDMI: "(NIR - SWIR1) / (NIR + SWIR1)",
    SpectralIndex.NMDI: "(NIR - (SWIR1 - SWIR2)) / (NIR + (SWIR1 - SWIR2))",
    SpectralIndex.LSWI: "(NIR - SWIR1) / (NIR + SWIR1)",
    SpectralIndex.NIRV: "NDVI * NIR",
    SpectralIndex.KNDVI: "tanh(NDVI^2)",
}


@dataclass(frozen=True, slots=True)
class ReflectanceBands:
    """Reflectancias físicas necesarias por las fórmulas del benchmark."""

    blue: float
    green: float
    red: float
    nir: float
    swir1: float
    swir2: float

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.blue,
                self.green,
                self.red,
                self.nir,
                self.swir1,
                self.swir2,
            )
        ):
            raise ValueError("las reflectancias deben ser finitas")


def calculate_spectral_indices(
    bands: ReflectanceBands,
    requested: tuple[SpectralIndex, ...],
) -> dict[SpectralIndex, float | None]:
    """Calcula índices en orden, conservando divisiones indefinidas como ``None``."""
    if len(requested) != len(set(requested)):
        raise ValueError("requested no puede contener índices repetidos")

    ndvi = _safe_ratio(bands.nir - bands.red, bands.nir + bands.red)
    ndmi = _safe_ratio(bands.nir - bands.swir1, bands.nir + bands.swir1)
    swir_difference = bands.swir1 - bands.swir2
    all_values: dict[SpectralIndex, float | None] = {
        SpectralIndex.NDVI: ndvi,
        SpectralIndex.EVI2: _safe_ratio(
            2.5 * (bands.nir - bands.red),
            bands.nir + 2.4 * bands.red + 1,
        ),
        SpectralIndex.NBR: _safe_ratio(
            bands.nir - bands.swir2,
            bands.nir + bands.swir2,
        ),
        SpectralIndex.NDMI: ndmi,
        SpectralIndex.NMDI: _safe_ratio(
            bands.nir - swir_difference,
            bands.nir + swir_difference,
        ),
        SpectralIndex.LSWI: ndmi,
        SpectralIndex.NIRV: None if ndvi is None else ndvi * bands.nir,
        SpectralIndex.KNDVI: None if ndvi is None else math.tanh(ndvi**2),
    }
    return {index: all_values[index] for index in requested}


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if abs(denominator) <= DENOMINATOR_EPSILON:
        return None
    return numerator / denominator
