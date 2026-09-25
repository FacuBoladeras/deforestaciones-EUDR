"""Colección HLS densa y enmascarada para detectores temporales."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import box

from deforestation_pipeline.catalog import (
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import (
    HlsCompositeError,
    build_masked_hls_dense_collection,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Element:
    """Imita la pérdida de tipo Image que aplica ee.Element.copyProperties."""

    def __init__(self, image: _Image) -> None:
        self.bands = image.bands
        self.history = image.history


class _Image:
    def __init__(self, bands: tuple[str, ...], history: tuple[str, ...] = ()) -> None:
        self.bands = bands
        self.history = history

    def _next(self, operation: str, bands: tuple[str, ...] | None = None) -> _Image:
        return _Image(bands or self.bands, (*self.history, operation))

    def select(self, selectors: str | list[str], names: list[str] | None = None) -> _Image:
        selected = (selectors,) if isinstance(selectors, str) else tuple(selectors)
        return self._next("select", tuple(names) if names is not None else selected)

    def multiply(self, other: object) -> _Image:
        return self._next("multiply")

    def add(self, other: object) -> _Image:
        return self._next("add")

    def subtract(self, other: object) -> _Image:
        return self._next("subtract")

    def divide(self, other: object) -> _Image:
        return self._next("divide")

    def pow(self, other: object) -> _Image:
        return self._next("pow")

    def tanh(self) -> _Image:
        return self._next("tanh")

    def abs(self) -> _Image:
        return self._next("abs")

    def gt(self, other: object) -> _Image:
        return self._next("gt")

    def neq(self, other: object) -> _Image:
        return self._next("neq")

    def eq(self, other: object) -> _Image:
        return self._next("eq")

    def And(self, other: object) -> _Image:
        return self._next("And")

    def bitwiseAnd(self, other: object) -> _Image:
        return self._next("bitwiseAnd")

    def rightShift(self, other: object) -> _Image:
        return self._next("rightShift")

    def updateMask(self, other: object) -> _Image:
        return self._next("updateMask")

    def copyProperties(self, image: object, properties: list[str]) -> _Element:
        return _Element(self._next("copyProperties"))

    def toFloat(self) -> _Image:
        return self._next("toFloat")

    def clip(self, geometry: object) -> _Image:
        return self._next("clip")

    def rename(self, name: str) -> _Image:
        return self._next("rename", (name,))

    def addBands(self, other: _Image) -> _Image:
        return self._next("addBands", (*self.bands, *other.bands))


class _Collection:
    def __init__(self, images: list[_Image | _Element]) -> None:
        self.images = images
        self.bounds_filtered = False
        self.date_filter: tuple[str, str] | None = None
        self.sorted_by: str | None = None

    def filterBounds(self, geometry: object) -> _Collection:
        self.bounds_filtered = geometry is not None
        return self

    def filterDate(self, start: str, end: str) -> _Collection:
        self.date_filter = (start, end)
        return self

    def sort(self, property_name: str) -> _Collection:
        self.sorted_by = property_name
        return self

    def map(self, callback: Any) -> _Collection:
        self.images = [callback(image) for image in self.images]
        return self


class _EarthEngine:
    def __init__(self) -> None:
        self.collection_id: str | None = None
        self.collection = _Collection(
            [
                _Image(("B2", "B3", "B4", "B5", "B6", "B7", "Fmask")),
                _Image(("B2", "B3", "B4", "B5", "B6", "B7", "Fmask")),
            ]
        )

    def Geometry(self, payload: object) -> object:
        return payload

    def ImageCollection(self, collection_id: str) -> _Collection:
        self.collection_id = collection_id
        return self.collection

    def Image(self, image: _Image) -> _Image:
        return image


def test_dense_builder_preserves_each_l30_observation_and_reuses_fmask() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    plan = build_benchmark_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    l30 = next(source for source in plan.sources if source.product == "HLSL30")
    fake = _EarthEngine()

    result = build_masked_hls_dense_collection(
        session=GeeSession(module=fake),
        source=l30,
        data_config=config.data,
        aoi_wgs84=box(-60.1, -31.1, -60.0, -31.0),
        start_date=date(2017, 1, 1),
        end_date_exclusive=date(2025, 1, 1),
        requested_indices=("NDVI", "NBR", "NDMI", "NIRv"),
    )

    assert result.collection is fake.collection
    assert result.source_product == "HLSL30"
    assert result.collection_id == "NASA/HLS/HLSL30/v002"
    assert result.input_temporal_granularity == "dense_masked_observations"
    assert result.qa_policy == "hls_fmask_preapplied"
    assert fake.collection_id == "NASA/HLS/HLSL30/v002"
    assert fake.collection.bounds_filtered
    assert fake.collection.date_filter == ("2017-01-01", "2025-01-01")
    assert fake.collection.sorted_by == "system:time_start"
    assert len(fake.collection.images) == 2
    assert all("updateMask" in image.history for image in fake.collection.images)
    assert all(
        image.bands[-4:] == ("NDVI", "NBR", "NDMI", "NIRv") for image in fake.collection.images
    )
    assert all("median" not in image.history for image in fake.collection.images)


def test_dense_builder_contextualizes_remote_stage_and_interval() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    plan = build_benchmark_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    l30 = next(source for source in plan.sources if source.product == "HLSL30")

    class _UnavailableEarthEngine(_EarthEngine):
        def ImageCollection(self, collection_id: str) -> _Collection:
            raise RuntimeError("synthetic service unavailable with secret=https://signed.invalid")

    with pytest.raises(HlsCompositeError) as captured:
        build_masked_hls_dense_collection(
            session=GeeSession(module=_UnavailableEarthEngine()),
            source=l30,
            data_config=config.data,
            aoi_wgs84=box(-60.1, -31.1, -60.0, -31.0),
            start_date=date(2017, 1, 1),
            end_date_exclusive=date(2025, 1, 1),
            requested_indices=("NDVI", "NBR", "NDMI", "NIRv"),
        )

    assert captured.value.code == ("dense_20170101_20250101_source_collection_service_error")
    assert "signed.invalid" not in captured.value.code
