"""Convergencia transparente de evidencias forestales del Paso 13.3."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from shapely.geometry import box

from deforestation_pipeline.catalog import (
    build_forest_baseline_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.forest_baseline_features import (
    FOREST_BASELINE_FEATURE_BAND_NAMES,
    ForestBaselineFeatureProduct,
)
from deforestation_pipeline.forest_baseline_products import (
    _build_source_evidence,
    build_forest_baseline_images,
)
from deforestation_pipeline.gee import GeeSession

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeImage:
    def __init__(self, bands: tuple[str, ...], expression: str) -> None:
        self.bands = bands
        self.expression = expression

    def mask(self) -> FakeImage:
        return FakeImage(self.bands, f"mask({self.expression})")

    def select(self, band: str) -> FakeImage:
        return FakeImage((band,), f"select({self.expression},{band})")

    def gt(self, value: int) -> FakeImage:
        return FakeImage(self.bands, f"gt({self.expression},{value})")

    def gte(self, value: int) -> FakeImage:
        return FakeImage(self.bands, f"gte({self.expression},{value})")

    def lt(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"lt({self.expression},{other.expression})")

    def eq(self, other: object) -> FakeImage:
        return FakeImage(self.bands, f"eq({self.expression},{other})")

    def And(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"and({self.expression},{other.expression})")

    def Or(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"or({self.expression},{other.expression})")

    def unmask(self, value: int) -> FakeImage:
        return FakeImage(self.bands, f"unmask({self.expression},{value})")

    def add(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"add({self.expression},{other.expression})")

    def divide(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"divide({self.expression},{other.expression})")

    def updateMask(self, mask: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"masked({self.expression},{mask.expression})")

    def rename(self, bands: str | list[str]) -> FakeImage:
        names = (bands,) if isinstance(bands, str) else tuple(bands)
        return FakeImage(names, self.expression)

    def addBands(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands + other.bands, f"{self.expression}+{other.expression}")

    def toUint8(self) -> FakeImage:
        return FakeImage(self.bands, f"uint8({self.expression})")

    def toFloat(self) -> FakeImage:
        return FakeImage(self.bands, f"float({self.expression})")

    def clip(self, _: object) -> FakeImage:
        return FakeImage(self.bands, f"clip({self.expression})")


class FakeImageCollection:
    def __init__(self, collection_id: str) -> None:
        self.collection_id = collection_id

    def first(self) -> FakeImage:
        return FakeImage(("Map",), self.collection_id)


class FakeModule:
    @staticmethod
    def Image(value: str | FakeImage) -> FakeImage:
        return value if isinstance(value, FakeImage) else FakeImage(("raw",), value)

    @staticmethod
    def ImageCollection(collection_id: str) -> FakeImageCollection:
        return FakeImageCollection(collection_id)


def _feature_product() -> ForestBaselineFeatureProduct:
    return cast(
        ForestBaselineFeatureProduct,
        SimpleNamespace(
            image=FakeImage(FOREST_BASELINE_FEATURE_BAND_NAMES, "hls-features"),
            metadata=SimpleNamespace(input_scene_count=50),
        ),
    )


def test_consensus_uses_only_independent_core_sources(monkeypatch: Any) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    source_plan = build_forest_baseline_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )

    def fake_source_image(*, source: Any, **_: Any) -> FakeImage:
        return FakeImage((source.source_id,), source.source_id)

    monkeypatch.setattr(
        "deforestation_pipeline.forest_baseline_products._build_source_evidence",
        fake_source_image,
    )
    feature_product = _feature_product()

    product = build_forest_baseline_images(
        session=GeeSession(module=SimpleNamespace(Geometry=lambda value: value)),
        source_plan=source_plan,
        baseline_config=config.forest_baseline,
        feature_product=feature_product,
        aoi_wgs84=box(-59.1, -33.1, -59.0, -33.0),
        generated_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )

    assert product.source_evidence.bands == (
        "jrc_gfc2020_v3",
        "esa_worldcover_2020_v100",
        "hansen_gfc_2025_v1_13",
    )
    assert "jrc_gfc2020_v3" in product.evidence_fraction.expression
    assert "esa_worldcover_2020_v100" in product.evidence_fraction.expression
    assert "hansen_gfc_2025_v1_13" not in product.evidence_fraction.expression
    assert product.features is feature_product.image


def test_metadata_does_not_misrepresent_evidence_fraction_as_calibrated_probability(
    monkeypatch: Any,
) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    source_plan = build_forest_baseline_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    monkeypatch.setattr(
        "deforestation_pipeline.forest_baseline_products._build_source_evidence",
        lambda *, source, **_: FakeImage((source.source_id,), source.source_id),
    )

    product = build_forest_baseline_images(
        session=GeeSession(module=SimpleNamespace(Geometry=lambda value: value)),
        source_plan=source_plan,
        baseline_config=config.forest_baseline,
        feature_product=_feature_product(),
        aoi_wgs84=box(-59.1, -33.1, -59.0, -33.0),
        generated_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )

    assert product.metadata.score_semantics == "uncalibrated_core_evidence_fraction"
    assert product.metadata.calibrated_probability is False
    assert product.metadata.features_used_for_classification is False
    assert product.metadata.final_assessment_generated is False
    assert product.metadata.core_source_ids == (
        "jrc_gfc2020_v3",
        "esa_worldcover_2020_v100",
    )
    assert product.metadata.supporting_source_ids == ("hansen_gfc_2025_v1_13",)
    assert product.metadata.deferred_source_ids == ("mapbiomas_chaco_deferred",)


def test_consensus_outputs_have_explicit_single_band_names(monkeypatch: Any) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    source_plan = build_forest_baseline_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    monkeypatch.setattr(
        "deforestation_pipeline.forest_baseline_products._build_source_evidence",
        lambda *, source, **_: FakeImage((source.source_id,), source.source_id),
    )

    product = build_forest_baseline_images(
        session=GeeSession(module=SimpleNamespace(Geometry=lambda value: value)),
        source_plan=source_plan,
        baseline_config=config.forest_baseline,
        feature_product=_feature_product(),
        aoi_wgs84=box(-59.1, -33.1, -59.0, -33.0),
        generated_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )

    assert product.evidence_fraction.bands == ("forest_evidence_fraction_2020",)
    assert product.consensus.bands == ("forest_consensus_2020",)
    assert product.disagreement.bands == ("forest_disagreement_2020",)
    assert product.source_count.bands == ("forest_core_source_count_2020",)


def test_catalog_rules_are_applied_explicitly_to_each_remote_source() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    source_plan = build_forest_baseline_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    expressions = {
        source.source_id: _build_source_evidence(
            module=FakeModule(),
            source=source,
            remote_aoi=object(),
        ).expression
        for source in source_plan.sources
    }

    assert "eq(unmask(select(JRC/GFC2020/V3,Map),0),1)" in (expressions["jrc_gfc2020_v3"])
    assert (
        "eq(unmask(select(ESA/WorldCover/v100,Map),0),10)"
        in (expressions["esa_worldcover_2020_v100"])
    )
    assert (
        "eq(unmask(select(ESA/WorldCover/v100,Map),0),95)"
        in (expressions["esa_worldcover_2020_v100"])
    )
    assert "gte(" in expressions["hansen_gfc_2025_v1_13"]
    assert "treecover2000" in expressions["hansen_gfc_2025_v1_13"]
    assert "lossyear" in expressions["hansen_gfc_2025_v1_13"]
