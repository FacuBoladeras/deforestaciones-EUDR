"""Contrato compartido del stack HLS estacional usado por entrenamiento e inferencia."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.catalog import BenchmarkSourcePlan
from deforestation_pipeline.config import DataConfig, SpectralIndex
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import REFLECTANCE_BAND_NAMES
from deforestation_pipeline.hls_seasonal import (
    HlsSeasonalRequest,
    build_hls_seasonal_composites,
)
from deforestation_pipeline.schemas import RasterGridSpec

SEASONAL_FEATURE_SEASONS = ("DJF", "MAM", "JJA", "SON")
SEASONAL_QA_COUNT_BANDS = (
    "valid_observation_count",
    "valid_observation_count_l30",
    "valid_observation_count_s30",
)


@dataclass(frozen=True, slots=True)
class SeasonalFeatureStack:
    image: Any = field(repr=False, compare=False)
    feature_columns: tuple[str, ...]


def seasonal_feature_columns(
    *,
    year: int,
    indices: tuple[SpectralIndex, ...],
) -> tuple[str, ...]:
    """Nombres reconstruibles desde cuatro composites estacionales HLS."""
    return tuple(
        f"{year}_{season}_{band}"
        for season in SEASONAL_FEATURE_SEASONS
        for band in (
            *REFLECTANCE_BAND_NAMES,
            *(index.value for index in indices),
            *SEASONAL_QA_COUNT_BANDS,
        )
    )


def build_yearly_seasonal_feature_stack(
    *,
    session: GeeSession,
    source_plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    aoi_wgs84: BaseGeometry,
    grid_spec: RasterGridSpec,
    year: int,
    generated_at: datetime,
) -> SeasonalFeatureStack:
    """Construye una única representación usada tanto al entrenar como al inferir."""
    window_plan = HlsSeasonalRequest(
        start_year=year,
        end_year=year,
    ).build_window_plan(generated_at=generated_at)
    seasonal = build_hls_seasonal_composites(
        session=session,
        source_plan=source_plan,
        data_config=data_config,
        aoi_wgs84=aoi_wgs84,
        grid_spec=grid_spec,
        window_plan=window_plan,
        generated_at=generated_at,
    )
    expected_period_ids = tuple(f"{year}-{season}" for season in SEASONAL_FEATURE_SEASONS)
    actual_period_ids = tuple(item.window.period_id for item in seasonal.items)
    if actual_period_ids != expected_period_ids:
        raise ValueError("los composites estacionales no coinciden con el año solicitado")

    stack: Any | None = None
    for item, season in zip(seasonal.items, SEASONAL_FEATURE_SEASONS, strict=True):
        prefix = f"{year}_{season}_"
        composite = item.composite
        period = composite.reflectance.rename(
            [f"{prefix}{band}" for band in REFLECTANCE_BAND_NAMES]
        )
        period = period.addBands(
            composite.indices.rename([f"{prefix}{index.value}" for index in data_config.indices])
        )
        period = period.addBands(
            composite.valid_observation_count.rename([f"{prefix}valid_observation_count"])
        )
        period = period.addBands(
            composite.valid_observation_count_l30.rename([f"{prefix}valid_observation_count_l30"])
        )
        period = period.addBands(
            composite.valid_observation_count_s30.rename([f"{prefix}valid_observation_count_s30"])
        )
        stack = period if stack is None else stack.addBands(period)
    if stack is None:
        raise ValueError("no se construyeron composites estacionales")
    columns = seasonal_feature_columns(year=year, indices=data_config.indices)
    return SeasonalFeatureStack(image=stack, feature_columns=columns)
