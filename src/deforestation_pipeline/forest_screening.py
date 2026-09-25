"""Screening conservador de la línea base forestal y escalamiento explícito."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Annotated, Any, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from deforestation_pipeline.change_detection import (
    DisturbanceQualityBit,
    ForestEvaluationDomain,
    ForestEvaluationDomainCode,
)
from deforestation_pipeline.schemas import RasterGridSpec


class ForestScreeningConfig(BaseModel):
    """Reglas congeladas que delimitan el alcance automatizado del MVP."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1.0"]
    minimum_core_source_count: Annotated[int, Field(ge=2, strict=True)]
    expected_tree_count: Literal[500]
    forest_vote_fraction_minimum: Annotated[float, Field(ge=0, le=1, strict=True)]
    nonforest_vote_fraction_maximum: Annotated[float, Field(ge=0, le=1, strict=True)]
    core_nonforest_max_evidence_fraction: Annotated[float, Field(ge=0, le=0, strict=True)]
    forest_rule: Literal[
        "rf_complete_and_core_sufficient_and_unanimous_forest_and_vote_gte_threshold"
    ]
    nonforest_rule: Literal[
        "rf_complete_and_core_sufficient_and_unanimous_nonforest_and_vote_lte_threshold"
    ]
    temporal_signal_scope: Literal["entire_aoi_primary_interpretation_automated_forest_only"]
    score_semantics: Literal["uncalibrated_binary_tree_vote_fraction"]
    automatic_final_assessment_allowed: Literal[False]

    @model_validator(mode="after")
    def thresholds_leave_a_review_interval(self) -> ForestScreeningConfig:
        if self.nonforest_vote_fraction_maximum >= self.forest_vote_fraction_minimum:
            raise ValueError("los umbrales deben dejar una franja intermedia de revisión")
        return self


class ScreeningDomainCode(IntEnum):
    """Partición exhaustiva de la grilla, con códigos estables y no normativos."""

    OUTSIDE_AOI = 0
    AUTOMATED_FOREST = 1
    AUTOMATED_NON_FOREST = 2
    REVIEW_REQUIRED = 3
    INSUFFICIENT_DATA = 4


class ForestScreeningMetrics(BaseModel):
    """Superficies sobre la grilla métrica vigente, no sobre conteos asumidos."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    area_method: Literal["projected_grid_affine_determinant"]
    area_crs: str
    pixel_area_ha: Annotated[float, Field(gt=0)]
    aoi_raster_area_ha: Annotated[float, Field(gt=0)]
    automated_evaluable_area_ha: Annotated[float, Field(ge=0)]
    automated_forest_area_ha: Annotated[float, Field(ge=0)]
    automated_nonforest_area_ha: Annotated[float, Field(ge=0)]
    review_required_area_ha: Annotated[float, Field(ge=0)]
    insufficient_data_area_ha: Annotated[float, Field(ge=0)]
    automated_evaluable_fraction: Annotated[float, Field(ge=0, le=1)]
    automated_forest_fraction: Annotated[float, Field(ge=0, le=1)]
    automated_nonforest_fraction: Annotated[float, Field(ge=0, le=1)]
    review_required_fraction: Annotated[float, Field(ge=0, le=1)]
    insufficient_data_fraction: Annotated[float, Field(ge=0, le=1)]


@dataclass(frozen=True, slots=True)
class ForestScreeningDomain:
    """Máscaras mutuamente excluyentes; ninguna constituye conclusión EUDR."""

    domain_code: NDArray[np.uint8]
    aoi_footprint: NDArray[np.bool_]
    automated_evaluable: NDArray[np.bool_]
    automated_forest: NDArray[np.bool_]
    automated_nonforest: NDArray[np.bool_]
    review_required: NDArray[np.bool_]
    insufficient_data: NDArray[np.bool_]
    metrics: ForestScreeningMetrics


def build_forest_screening_domain(
    *,
    source_count: NDArray[Any],
    consensus_forest: NDArray[Any],
    disagreement: NDArray[Any],
    evidence_fraction: NDArray[Any],
    rf_vote_fraction: NDArray[Any],
    rf_input_complete: NDArray[Any],
    grid_spec: RasterGridSpec,
    config: ForestScreeningConfig,
) -> ForestScreeningDomain:
    """Aplica reglas conservadoras y conserva como revisión todo caso no inequívoco."""
    arrays = tuple(
        np.asarray(item, dtype=np.float64)
        for item in (
            source_count,
            consensus_forest,
            disagreement,
            evidence_fraction,
            rf_vote_fraction,
            rf_input_complete,
        )
    )
    expected_shape = (grid_spec.height, grid_spec.width)
    if any(array.shape != expected_shape for array in arrays):
        raise ValueError("screening_input_shape_mismatch")
    counts, consensus, disagrees, fraction, rf_votes, rf_complete_values = arrays
    aoi = np.isfinite(counts)
    if not np.any(aoi):
        raise ValueError("screening_aoi_footprint_empty")
    if np.any((counts[aoi] < 0) | (counts[aoi] != np.floor(counts[aoi]))):
        raise ValueError("screening_source_count_invalid")

    sufficient = aoi & (counts >= config.minimum_core_source_count)
    _validate_binary_where(consensus, sufficient, "consensus_forest")
    _validate_binary_where(disagrees, sufficient, "disagreement")
    _validate_fraction_where(fraction, sufficient, "evidence_fraction")
    _validate_binary_where(rf_complete_values, aoi, "rf_input_complete")
    rf_complete = aoi & np.isfinite(rf_complete_values) & (rf_complete_values == 1)
    _validate_fraction_where(rf_votes, rf_complete, "rf_vote_fraction")
    quantized_rf_votes = np.rint(rf_votes * config.expected_tree_count) / config.expected_tree_count

    consensus_mask = sufficient & np.isfinite(consensus) & (consensus == 1)
    disagreement_mask = sufficient & np.isfinite(disagrees) & (disagrees == 1)
    if np.any(consensus_mask & disagreement_mask):
        raise ValueError("screening_consensus_and_disagreement_overlap")
    core_nonforest = (
        sufficient
        & np.isfinite(fraction)
        & (fraction <= config.core_nonforest_max_evidence_fraction)
        & ~disagreement_mask
        & ~consensus_mask
    )
    automated_forest = (
        sufficient
        & rf_complete
        & consensus_mask
        & ~disagreement_mask
        & (quantized_rf_votes >= config.forest_vote_fraction_minimum)
    )
    automated_nonforest = (
        sufficient
        & rf_complete
        & core_nonforest
        & (quantized_rf_votes <= config.nonforest_vote_fraction_maximum)
    )
    automated = automated_forest | automated_nonforest
    insufficient = aoi & (~sufficient | ~rf_complete)
    review = aoi & sufficient & rf_complete & ~automated
    if (
        np.any(automated & review)
        or np.any(automated & insufficient)
        or np.any(review & insufficient)
    ):
        raise RuntimeError("screening_domains_overlap")
    if not np.array_equal(automated | review | insufficient, aoi):
        raise RuntimeError("screening_domains_not_exhaustive")

    domain_code = np.full(expected_shape, ScreeningDomainCode.OUTSIDE_AOI, dtype=np.uint8)
    domain_code[automated_forest] = ScreeningDomainCode.AUTOMATED_FOREST
    domain_code[automated_nonforest] = ScreeningDomainCode.AUTOMATED_NON_FOREST
    domain_code[review] = ScreeningDomainCode.REVIEW_REQUIRED
    domain_code[insufficient] = ScreeningDomainCode.INSUFFICIENT_DATA
    metrics = _area_metrics(
        grid_spec=grid_spec,
        aoi=aoi,
        automated=automated,
        automated_forest=automated_forest,
        automated_nonforest=automated_nonforest,
        review=review,
        insufficient=insufficient,
    )
    readonly_arrays = (
        domain_code,
        aoi,
        automated,
        automated_forest,
        automated_nonforest,
        review,
        insufficient,
    )
    for array in readonly_arrays:
        array.setflags(write=False)
    return ForestScreeningDomain(
        domain_code=domain_code,
        aoi_footprint=aoi,
        automated_evaluable=automated,
        automated_forest=automated_forest,
        automated_nonforest=automated_nonforest,
        review_required=review,
        insufficient_data=insufficient,
        metrics=metrics,
    )


def build_screening_temporal_domain(
    screening: ForestScreeningDomain,
) -> ForestEvaluationDomain:
    """Permite calcular señales en todo el AOI y marca fuera del bosque automático."""
    aoi = np.asarray(screening.aoi_footprint, dtype=np.bool_)
    automated_forest = np.asarray(screening.automated_forest, dtype=np.bool_)
    baseline_uncertain = aoi & ~automated_forest
    domain_code = np.full(
        aoi.shape,
        ForestEvaluationDomainCode.NOT_EVALUABLE_INSUFFICIENT_SOURCES,
        dtype=np.uint8,
    )
    domain_code[aoi] = ForestEvaluationDomainCode.EVALUABLE_BASELINE_UNCERTAIN
    domain_code[automated_forest] = ForestEvaluationDomainCode.EVALUABLE_CONSENSUS_FOREST
    quality_flags = np.zeros(aoi.shape, dtype=np.uint16)
    quality_flags[baseline_uncertain] |= np.uint16(1 << DisturbanceQualityBit.BASELINE_UNCERTAIN)
    evaluable = aoi.copy()
    for array in (domain_code, evaluable, baseline_uncertain, quality_flags):
        array.setflags(write=False)
    return ForestEvaluationDomain(
        domain_code=domain_code,
        evaluable=evaluable,
        baseline_uncertain=baseline_uncertain,
        quality_flags_bitmask=quality_flags,
    )


def _validate_binary_where(values: NDArray[np.float64], mask: NDArray[np.bool_], name: str) -> None:
    selected = values[mask]
    if np.any(~np.isfinite(selected)) or not np.all(np.isin(selected, (0, 1))):
        raise ValueError(f"screening_{name}_invalid")


def _validate_fraction_where(
    values: NDArray[np.float64],
    mask: NDArray[np.bool_],
    name: str,
) -> None:
    selected = values[mask]
    if np.any(~np.isfinite(selected)) or np.any((selected < 0) | (selected > 1)):
        raise ValueError(f"screening_{name}_invalid")


def _area_metrics(
    *,
    grid_spec: RasterGridSpec,
    aoi: NDArray[np.bool_],
    automated: NDArray[np.bool_],
    automated_forest: NDArray[np.bool_],
    automated_nonforest: NDArray[np.bool_],
    review: NDArray[np.bool_],
    insufficient: NDArray[np.bool_],
) -> ForestScreeningMetrics:
    x_scale, x_shear, _, y_shear, y_scale, _ = grid_spec.transform
    pixel_area_ha = abs(x_scale * y_scale - x_shear * y_shear) / 10_000.0
    aoi_pixels = int(np.count_nonzero(aoi))

    def area(mask: NDArray[np.bool_]) -> float:
        return float(np.count_nonzero(mask) * pixel_area_ha)

    def fraction(mask: NDArray[np.bool_]) -> float:
        return float(np.count_nonzero(mask) / aoi_pixels)

    return ForestScreeningMetrics(
        area_method="projected_grid_affine_determinant",
        area_crs=grid_spec.target_crs,
        pixel_area_ha=pixel_area_ha,
        aoi_raster_area_ha=area(aoi),
        automated_evaluable_area_ha=area(automated),
        automated_forest_area_ha=area(automated_forest),
        automated_nonforest_area_ha=area(automated_nonforest),
        review_required_area_ha=area(review),
        insufficient_data_area_ha=area(insufficient),
        automated_evaluable_fraction=fraction(automated),
        automated_forest_fraction=fraction(automated_forest),
        automated_nonforest_fraction=fraction(automated_nonforest),
        review_required_fraction=fraction(review),
        insufficient_data_fraction=fraction(insufficient),
    )
