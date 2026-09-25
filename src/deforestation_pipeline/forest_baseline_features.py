"""Variables espectrales pre-corte para la línea base forestal 2020."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.catalog import BenchmarkSourcePlan
from deforestation_pipeline.config import DataConfig, SpectralIndex
from deforestation_pipeline.forest_baseline import ForestBaselineConfig
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import (
    REFLECTANCE_BAND_NAMES,
    HlsTemporalCompositeRequest,
    build_hls_composite,
)

FOREST_BASELINE_FEATURE_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"


def forest_baseline_feature_band_names(
    indices: tuple[SpectralIndex, ...],
    *,
    start_year: int,
    end_year: int,
) -> tuple[str, ...]:
    """Nombres determinísticos del stack multianual."""
    suffix = f"{start_year}_{end_year}"
    return (
        *(f"{band}_mean_{suffix}" for band in REFLECTANCE_BAND_NAMES),
        *(f"{band}_range_{suffix}" for band in REFLECTANCE_BAND_NAMES),
        *(f"{index.value}_mean_{suffix}" for index in indices),
        *(f"{index.value}_range_{suffix}" for index in indices),
        f"valid_observation_count_{suffix}",
    )


FOREST_BASELINE_FEATURE_BAND_NAMES = forest_baseline_feature_band_names(
    tuple(SpectralIndex),
    start_year=2019,
    end_year=2020,
)


class ForestBaselineFeatureMetadata(BaseModel):
    """Procedencia del stack sin confundir variables con clasificación."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = FOREST_BASELINE_FEATURE_SCHEMA_VERSION
    generated_at: datetime
    reference_date: date
    feature_start_date: date
    feature_end_date_exclusive: date
    calendar_years: Annotated[tuple[int, ...], Field(min_length=2)]
    input_scene_count: Annotated[int, Field(gt=0)]
    feature_band_names: Annotated[tuple[str, ...], Field(min_length=1)]
    aggregation_method: Literal["annual_medians_then_multiannual_mean_and_range"]
    post_cutoff_observations_used: Literal[False] = False
    final_assessment_generated: Literal[False] = False

    @model_validator(mode="after")
    def metadata_is_temporally_consistent(self) -> ForestBaselineFeatureMetadata:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        expected_years = tuple(
            range(self.feature_start_date.year, self.feature_end_date_exclusive.year)
        )
        if self.calendar_years != expected_years:
            raise ValueError("calendar_years no coincide con la ventana de atributos")
        if self.feature_end_date_exclusive != self.reference_date.replace(
            year=self.reference_date.year + 1,
            month=1,
            day=1,
        ):
            raise ValueError("feature_end_date_exclusive no coincide con reference_date")
        return self


@dataclass(frozen=True, slots=True)
class ForestBaselineFeatureProduct:
    """Imagen GEE perezosa y metadatos de variables pre-corte."""

    image: Any
    metadata: ForestBaselineFeatureMetadata


def build_forest_baseline_features(
    *,
    session: GeeSession,
    hls_plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    baseline_config: ForestBaselineConfig,
    aoi_wgs84: BaseGeometry,
    target_crs: str,
    generated_at: datetime | None = None,
) -> ForestBaselineFeatureProduct:
    """Resume años calendario completos sin usar observaciones posteriores al corte."""
    generation_time = generated_at or datetime.now(UTC)
    years = tuple(
        range(
            baseline_config.feature_start_date.year,
            baseline_config.feature_end_date_exclusive.year,
        )
    )
    composites = tuple(
        build_hls_composite(
            session=session,
            plan=hls_plan,
            data_config=data_config,
            aoi_wgs84=aoi_wgs84,
            request=HlsTemporalCompositeRequest(
                start_date=date(year, 1, 1),
                end_date_exclusive=date(year + 1, 1, 1),
                target_crs=target_crs,
                scale_m=baseline_config.benchmark_resolution_m,
            ),
            generated_at=generation_time,
        )
        for year in years
    )
    suffix = f"{years[0]}_{years[-1]}"
    reflectance_mean, reflectance_range = _mean_and_range(
        tuple(product.reflectance for product in composites),
        mean_names=tuple(f"{band}_mean_{suffix}" for band in REFLECTANCE_BAND_NAMES),
        range_names=tuple(f"{band}_range_{suffix}" for band in REFLECTANCE_BAND_NAMES),
    )
    index_mean, index_range = _mean_and_range(
        tuple(product.indices for product in composites),
        mean_names=tuple(f"{index.value}_mean_{suffix}" for index in data_config.indices),
        range_names=tuple(f"{index.value}_range_{suffix}" for index in data_config.indices),
    )
    observation_count = composites[0].valid_observation_count
    for product in composites[1:]:
        observation_count = observation_count.add(product.valid_observation_count)
    observation_count = observation_count.rename([f"valid_observation_count_{suffix}"])
    image = (
        reflectance_mean.addBands(reflectance_range)
        .addBands(index_mean)
        .addBands(index_range)
        .addBands(observation_count)
        .toFloat()
    )
    band_names = forest_baseline_feature_band_names(
        data_config.indices,
        start_year=years[0],
        end_year=years[-1],
    )
    return ForestBaselineFeatureProduct(
        image=image,
        metadata=ForestBaselineFeatureMetadata(
            generated_at=generation_time,
            reference_date=baseline_config.reference_date,
            feature_start_date=baseline_config.feature_start_date,
            feature_end_date_exclusive=baseline_config.feature_end_date_exclusive,
            calendar_years=years,
            input_scene_count=sum(product.metadata.input_scene_count for product in composites),
            feature_band_names=band_names,
            aggregation_method="annual_medians_then_multiannual_mean_and_range",
        ),
    )


def _mean_and_range(
    images: tuple[Any, ...],
    *,
    mean_names: tuple[str, ...],
    range_names: tuple[str, ...],
) -> tuple[Any, Any]:
    if len(images) < 2:
        raise ValueError("la línea base requiere al menos dos años completos")
    total = images[0]
    minimum = images[0]
    maximum = images[0]
    for image in images[1:]:
        total = total.add(image)
        minimum = minimum.min(image)
        maximum = maximum.max(image)
    mean_image = total.divide(len(images)).rename(list(mean_names))
    range_image = maximum.subtract(minimum).rename(list(range_names))
    return mean_image, range_image
