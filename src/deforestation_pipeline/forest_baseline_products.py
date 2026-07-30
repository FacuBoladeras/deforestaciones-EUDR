"""Convergencia explícita de mapas forestales para la línea base 2020."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.catalog import (
    ForestBaselineSourcePlan,
    ForestCatalogSource,
    ForestRuleKind,
)
from deforestation_pipeline.forest_baseline import ForestBaselineConfig
from deforestation_pipeline.forest_baseline_features import ForestBaselineFeatureProduct
from deforestation_pipeline.gee import GeeSession

FOREST_BASELINE_PRODUCT_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"


class ForestBaselineProductMetadata(BaseModel):
    """Semántica y procedencia que impiden presentar el score como probabilidad calibrada."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0.0"] = FOREST_BASELINE_PRODUCT_SCHEMA_VERSION
    generated_at: datetime
    reference_date: date
    core_source_ids: Annotated[tuple[str, ...], Field(min_length=2)]
    supporting_source_ids: tuple[str, ...]
    deferred_source_ids: tuple[str, ...]
    score_semantics: Literal["uncalibrated_core_evidence_fraction"]
    consensus_rule: Literal["all_core_sources_forest_minimum_two"]
    resampling_method: Literal["nearest_on_declared_target_grid"]
    calibrated_probability: Literal[False] = False
    features_used_for_classification: Literal[False] = False
    final_assessment_generated: Literal[False] = False
    input_scene_count: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def metadata_is_auditable(self) -> ForestBaselineProductMetadata:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        if len(self.core_source_ids) != len(set(self.core_source_ids)):
            raise ValueError("core_source_ids contiene duplicados")
        if set(self.core_source_ids) & set(self.supporting_source_ids):
            raise ValueError("una fuente no puede ser core y supporting")
        return self


@dataclass(frozen=True, slots=True)
class ForestBaselineImages:
    """Imágenes GEE perezosas; ninguna representa una conclusión EUDR."""

    source_evidence: Any
    evidence_fraction: Any
    consensus: Any
    disagreement: Any
    source_count: Any
    features: Any
    metadata: ForestBaselineProductMetadata


def build_forest_baseline_images(
    *,
    session: GeeSession,
    source_plan: ForestBaselineSourcePlan,
    baseline_config: ForestBaselineConfig,
    feature_product: ForestBaselineFeatureProduct,
    aoi_wgs84: BaseGeometry,
    generated_at: datetime | None = None,
) -> ForestBaselineImages:
    """Construye score, consenso y desacuerdo sin entrenar ni atribuir cambios."""
    generation_time = generated_at or datetime.now(UTC)
    remote_aoi = session.module.Geometry(mapping(aoi_wgs84))
    evidence_by_id = {
        source.source_id: _build_source_evidence(
            module=session.module,
            source=source,
            remote_aoi=remote_aoi,
        )
        for source in source_plan.sources
    }
    ordered_evidence = tuple(evidence_by_id[source.source_id] for source in source_plan.sources)
    source_evidence = ordered_evidence[0]
    for image in ordered_evidence[1:]:
        source_evidence = source_evidence.addBands(image)

    core_evidence = tuple(evidence_by_id[source.source_id] for source in source_plan.core_sources)
    vote_sum = _sum_images(tuple(image.unmask(0).toUint8() for image in core_evidence))
    source_count = _sum_images(
        tuple(image.mask().gt(0).unmask(0).toUint8() for image in core_evidence)
    ).rename("forest_core_source_count_2020")
    sufficient = source_count.gte(baseline_config.minimum_independent_sources)
    evidence_fraction = (
        vote_sum.toFloat()
        .divide(source_count)
        .updateMask(sufficient)
        .rename("forest_evidence_fraction_2020")
    )
    consensus = (
        vote_sum.eq(source_count)
        .And(sufficient)
        .updateMask(sufficient)
        .rename("forest_consensus_2020")
        .toUint8()
    )
    disagreement = (
        vote_sum.gt(0)
        .And(vote_sum.lt(source_count))
        .updateMask(sufficient)
        .rename("forest_disagreement_2020")
        .toUint8()
    )
    return ForestBaselineImages(
        source_evidence=source_evidence,
        evidence_fraction=evidence_fraction,
        consensus=consensus,
        disagreement=disagreement,
        source_count=source_count,
        features=feature_product.image,
        metadata=ForestBaselineProductMetadata(
            generated_at=generation_time,
            reference_date=baseline_config.reference_date,
            core_source_ids=tuple(source.source_id for source in source_plan.core_sources),
            supporting_source_ids=tuple(
                source.source_id for source in source_plan.supporting_sources
            ),
            deferred_source_ids=source_plan.deferred_source_ids,
            score_semantics="uncalibrated_core_evidence_fraction",
            consensus_rule="all_core_sources_forest_minimum_two",
            resampling_method="nearest_on_declared_target_grid",
            input_scene_count=feature_product.metadata.input_scene_count,
        ),
    )


def _build_source_evidence(
    *,
    module: Any,
    source: ForestCatalogSource,
    remote_aoi: Any,
) -> Any:
    raw = (
        module.Image(source.collection_id)
        if source.asset_type == "image"
        else module.Image(module.ImageCollection(source.collection_id).first())
    )
    rule = source.rule
    if rule.kind is ForestRuleKind.CATEGORICAL_VALUES:
        band = raw.select(rule.source_band).unmask(0)
        evidence = band.eq(rule.forest_values[0])
        for value in rule.forest_values[1:]:
            evidence = evidence.Or(band.eq(value))
    else:
        tree_cover = raw.select(rule.tree_cover_band).unmask(0)
        loss_year = raw.select(rule.loss_year_band).unmask(0)
        no_loss_through_cutoff = loss_year.eq(0).Or(loss_year.gt(rule.loss_year_cutoff_code))
        evidence = tree_cover.gte(rule.minimum_tree_cover_percent).And(no_loss_through_cutoff)
    return evidence.rename(source.source_id).toUint8().clip(remote_aoi)


def _sum_images(images: tuple[Any, ...]) -> Any:
    if not images:
        raise ValueError("se requiere al menos una imagen")
    result = images[0]
    for image in images[1:]:
        result = result.add(image)
    return result
