"""Pruebas numéricas de las fórmulas espectrales del primer composite."""

from __future__ import annotations

import math

import pytest

from deforestation_pipeline.config import SpectralIndex
from deforestation_pipeline.spectral_indices import (
    ReflectanceBands,
    calculate_spectral_indices,
)


def test_all_indices_are_calculated_from_scaled_reflectance() -> None:
    bands = ReflectanceBands(
        blue=0.10,
        green=0.12,
        red=0.20,
        nir=0.60,
        swir1=0.30,
        swir2=0.10,
    )

    indices = calculate_spectral_indices(bands, tuple(SpectralIndex))

    assert indices[SpectralIndex.NDVI] == pytest.approx(0.5)
    assert indices[SpectralIndex.EVI2] == pytest.approx(2.5 * 0.4 / (0.6 + 2.4 * 0.2 + 1))
    assert indices[SpectralIndex.NBR] == pytest.approx(0.5 / 0.7)
    assert indices[SpectralIndex.NDMI] == pytest.approx(1 / 3)
    assert indices[SpectralIndex.NMDI] == pytest.approx(0.5)
    assert indices[SpectralIndex.LSWI] == indices[SpectralIndex.NDMI]
    assert indices[SpectralIndex.NIRV] == pytest.approx(0.3)
    assert indices[SpectralIndex.KNDVI] == pytest.approx(math.tanh(0.5**2))


def test_indices_with_zero_denominator_are_reported_as_missing() -> None:
    bands = ReflectanceBands(
        blue=0.0,
        green=0.0,
        red=-0.5,
        nir=0.5,
        swir1=-0.5,
        swir2=-0.5,
    )

    indices = calculate_spectral_indices(
        bands,
        (
            SpectralIndex.NDVI,
            SpectralIndex.NBR,
            SpectralIndex.NDMI,
            SpectralIndex.LSWI,
        ),
    )

    assert all(value is None for value in indices.values())


def test_selected_index_order_is_preserved() -> None:
    bands = ReflectanceBands(
        blue=0.1,
        green=0.1,
        red=0.2,
        nir=0.6,
        swir1=0.3,
        swir2=0.1,
    )
    requested = (SpectralIndex.NBR, SpectralIndex.NDVI)

    result = calculate_spectral_indices(bands, requested)

    assert tuple(result) == requested
