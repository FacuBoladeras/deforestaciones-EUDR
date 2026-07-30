"""Variables espectrales pre-corte exactas del Paso 13.2."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from shapely.geometry import box

from deforestation_pipeline.catalog import (
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.forest_baseline_features import (
    FOREST_BASELINE_FEATURE_BAND_NAMES,
    build_forest_baseline_features,
)
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import REFLECTANCE_BAND_NAMES

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeImage:
    def __init__(self, bands: tuple[str, ...], expression: str) -> None:
        self.bands = bands
        self.expression = expression

    def add(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"add({self.expression},{other.expression})")

    def divide(self, value: int) -> FakeImage:
        return FakeImage(self.bands, f"divide({self.expression},{value})")

    def min(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"min({self.expression},{other.expression})")

    def max(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"max({self.expression},{other.expression})")

    def subtract(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands, f"subtract({self.expression},{other.expression})")

    def rename(self, bands: list[str]) -> FakeImage:
        return FakeImage(tuple(bands), self.expression)

    def addBands(self, other: FakeImage) -> FakeImage:
        return FakeImage(self.bands + other.bands, f"{self.expression}+{other.expression}")

    def toFloat(self) -> FakeImage:
        return FakeImage(self.bands, f"float({self.expression})")


def _fake_product(year: int, indices: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        reflectance=FakeImage(REFLECTANCE_BAND_NAMES, f"reflectance-{year}"),
        indices=FakeImage(indices, f"indices-{year}"),
        valid_observation_count=FakeImage(
            ("valid_observation_count",),
            f"count-{year}",
        ),
        metadata=SimpleNamespace(input_scene_count=20 + year - 2019),
    )


def test_feature_stack_uses_exact_full_calendar_years_without_future_data(
    monkeypatch: Any,
) -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    plan = build_benchmark_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    requests = []

    def fake_builder(**kwargs: Any) -> SimpleNamespace:
        request = kwargs["request"]
        requests.append(request)
        return _fake_product(
            request.start_date.year,
            tuple(index.value for index in config.data.indices),
        )

    monkeypatch.setattr(
        "deforestation_pipeline.forest_baseline_features.build_hls_composite",
        fake_builder,
    )

    product = build_forest_baseline_features(
        session=GeeSession(module=object()),
        hls_plan=plan,
        data_config=config.data,
        baseline_config=config.forest_baseline,
        aoi_wgs84=box(-59.1, -33.1, -59.0, -33.0),
        target_crs="EPSG:32721",
        generated_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )

    assert tuple((request.start_date, request.end_date_exclusive) for request in requests) == (
        (date(2019, 1, 1), date(2020, 1, 1)),
        (date(2020, 1, 1), date(2021, 1, 1)),
    )
    assert product.metadata.calendar_years == (2019, 2020)
    assert product.metadata.input_scene_count == 41
    assert product.metadata.post_cutoff_observations_used is False
    assert product.metadata.final_assessment_generated is False
    assert product.image.bands == FOREST_BASELINE_FEATURE_BAND_NAMES


def test_feature_stack_contains_mean_range_and_observation_support() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")

    assert FOREST_BASELINE_FEATURE_BAND_NAMES == (
        *(f"{band}_mean_2019_2020" for band in REFLECTANCE_BAND_NAMES),
        *(f"{band}_range_2019_2020" for band in REFLECTANCE_BAND_NAMES),
        *(f"{index.value}_mean_2019_2020" for index in config.data.indices),
        *(f"{index.value}_range_2019_2020" for index in config.data.indices),
        "valid_observation_count_2019_2020",
    )
