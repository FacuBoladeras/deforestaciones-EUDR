"""Reglas científicas y orquestación remota de la composición anual HLS."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from shapely.geometry import Point, box

from deforestation_pipeline.catalog import (
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import (
    HlsCompositeError,
    HlsCompositeRequest,
    build_hls_annual_composite,
    hls_fmask_pixel_is_valid,
)
from deforestation_pipeline.hls_tiles import mgrs_tile_ids_from_hls_identifiers

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _InfoValue:
    def __init__(self, value: object) -> None:
        self.value = value

    def getInfo(self) -> object:
        return self.value


class _SymbolicImage:
    def __init__(self, bands: tuple[str, ...], history: tuple[str, ...] = ()) -> None:
        self.bands = bands
        self.history = history

    def _next(
        self,
        operation: str,
        *,
        bands: tuple[str, ...] | None = None,
    ) -> _SymbolicImage:
        return _SymbolicImage(bands or self.bands, (*self.history, operation))

    def select(
        self,
        selectors: str | list[str],
        names: list[str] | None = None,
    ) -> _SymbolicImage:
        selected = (selectors,) if isinstance(selectors, str) else tuple(selectors)
        return self._next("select", bands=tuple(names) if names is not None else selected)

    def rename(self, names: str | list[str]) -> _SymbolicImage:
        return self._next(
            "rename",
            bands=(names,) if isinstance(names, str) else tuple(names),
        )

    def multiply(self, other: object) -> _SymbolicImage:
        return self._next("multiply")

    def add(self, other: object) -> _SymbolicImage:
        return self._next("add")

    def subtract(self, other: object) -> _SymbolicImage:
        return self._next("subtract")

    def divide(self, other: object) -> _SymbolicImage:
        return self._next("divide")

    def pow(self, other: object) -> _SymbolicImage:
        return self._next("pow")

    def tanh(self) -> _SymbolicImage:
        return self._next("tanh")

    def abs(self) -> _SymbolicImage:
        return self._next("abs")

    def gt(self, other: object) -> _SymbolicImage:
        return self._next("gt")

    def neq(self, other: object) -> _SymbolicImage:
        return self._next("neq")

    def eq(self, other: object) -> _SymbolicImage:
        return self._next("eq")

    def And(self, other: object) -> _SymbolicImage:
        return self._next("And")

    def bitwiseAnd(self, other: object) -> _SymbolicImage:
        return self._next("bitwiseAnd")

    def rightShift(self, other: object) -> _SymbolicImage:
        return self._next("rightShift")

    def updateMask(self, other: object) -> _SymbolicImage:
        return self._next("updateMask")

    def copyProperties(
        self,
        image: object,
        properties: list[str],
    ) -> _SymbolicImage:
        return self._next("copyProperties")

    def toFloat(self) -> _SymbolicImage:
        return self._next("toFloat")

    def toUint16(self) -> _SymbolicImage:
        return self._next("toUint16")

    def clip(self, geometry: object) -> _SymbolicImage:
        return self._next("clip")

    def addBands(self, other: _SymbolicImage) -> _SymbolicImage:
        return self._next("addBands", bands=(*self.bands, *other.bands))


class _SymbolicCollection:
    def __init__(
        self,
        records: list[dict[str, object]],
        images: list[_SymbolicImage],
    ) -> None:
        self.records = records
        self.images = images
        self.date_filter: tuple[str, str] | None = None
        self.bounds_applied = False
        self.property_filter: tuple[str, tuple[str, ...]] | None = None

    def filterBounds(self, geometry: object) -> _SymbolicCollection:
        self.bounds_applied = geometry is not None
        return self

    def filterDate(self, start: str, end: str) -> _SymbolicCollection:
        self.date_filter = (start, end)
        return self

    def sort(self, property_name: str) -> _SymbolicCollection:
        assert property_name == "system:time_start"
        return self

    def filter(
        self,
        filter_expression: tuple[str, tuple[str, ...]],
    ) -> _SymbolicCollection:
        property_name, accepted_values = filter_expression
        self.property_filter = filter_expression
        accepted = set(accepted_values)
        self.records = [record for record in self.records if record.get(property_name) in accepted]
        self.images = self.images[: len(self.records)]
        return self

    def size(self) -> _InfoValue:
        return _InfoValue(len(self.records))

    def aggregate_array(self, property_name: str) -> _InfoValue:
        return _InfoValue([record[property_name] for record in self.records])

    def map(self, callback: Any) -> _SymbolicCollection:
        return _SymbolicCollection(
            self.records,
            [callback(image) for image in self.images],
        )

    def merge(self, other: _SymbolicCollection) -> _SymbolicCollection:
        return _SymbolicCollection(
            [*self.records, *other.records],
            [*self.images, *other.images],
        )

    def select(self, selectors: list[str]) -> _SymbolicCollection:
        return _SymbolicCollection(
            self.records,
            [image.select(selectors) for image in self.images],
        )

    def median(self) -> _SymbolicImage:
        return _SymbolicImage(self.images[0].bands, ("median",))

    def count(self) -> _SymbolicImage:
        return _SymbolicImage(self.images[0].bands, ("count",))


class _SymbolicEarthEngine:
    class Filter:
        @staticmethod
        def inList(
            property_name: str,
            values: list[str],
        ) -> tuple[str, tuple[str, ...]]:
            return property_name, tuple(values)

    def __init__(self, records: dict[str, list[dict[str, object]]]) -> None:
        self.records = records
        self.collections: dict[str, _SymbolicCollection] = {}
        self.geometry_payload: object | None = None

    def Geometry(self, payload: object) -> object:
        self.geometry_payload = payload
        return object()

    def ImageCollection(self, collection_id: str) -> _SymbolicCollection:
        records = self.records[collection_id]
        images = [
            _SymbolicImage(
                (
                    "B2",
                    "B3",
                    "B4",
                    "B5",
                    "B6",
                    "B7",
                    "B8A",
                    "B11",
                    "B12",
                    "Fmask",
                )
            )
            for _ in records
        ]
        collection = _SymbolicCollection(records, images)
        self.collections[collection_id] = collection
        return collection

    def Image(self, image: _SymbolicImage) -> _SymbolicImage:
        return image


class _FailingEarthEngine(_SymbolicEarthEngine):
    def ImageCollection(self, collection_id: str) -> _SymbolicCollection:
        raise RuntimeError(f"permission denied SECRET-{collection_id}")


def _plan_and_config() -> tuple[Any, Any]:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    plan = build_benchmark_source_plan(
        load_source_catalog(PROJECT_ROOT / "data" / "catalog.yml"),
        config,
    )
    return plan, config


def _record(identifier: str, timestamp: int, cloud: float) -> dict[str, object]:
    tile_id = identifier.split("_", maxsplit=1)[0].removeprefix("T")
    return {
        "system:index": identifier,
        "system:time_start": timestamp,
        "CLOUD_COVERAGE": cloud,
        "MGRS_TILE_ID": tile_id,
    }


def test_composite_request_requires_one_complete_calendar_year() -> None:
    request = HlsCompositeRequest(
        start_date=date(2023, 1, 1),
        end_date_exclusive=date(2024, 1, 1),
        target_crs="EPSG:6933",
        scale_m=30,
    )

    assert request.period_days == 365
    assert request.reducer == "median"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2023, 2, 1), date(2024, 2, 1)),
        (date(2023, 1, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 12, 31)),
    ],
)
def test_composite_request_rejects_non_calendar_years(start: date, end: date) -> None:
    with pytest.raises(ValidationError, match="año calendario completo"):
        HlsCompositeRequest(
            start_date=start,
            end_date_exclusive=end,
            target_crs="EPSG:6933",
            scale_m=30,
        )


@pytest.mark.parametrize("value", [0, 1 << 5, 1 << 6, 1 << 7])
def test_fmask_keeps_clear_land_water_and_non_high_aerosol(value: int) -> None:
    assert hls_fmask_pixel_is_valid(value) is True


@pytest.mark.parametrize(
    "value",
    [
        255,
        1 << 1,
        1 << 2,
        1 << 3,
        1 << 4,
        (1 << 6) | (1 << 7),
    ],
)
def test_fmask_rejects_fill_cloud_shadow_snow_and_high_aerosol(value: int) -> None:
    assert hls_fmask_pixel_is_valid(value) is False


def test_annual_composite_uses_complete_inventory_and_builds_requested_bands() -> None:
    plan, config = _plan_and_config()
    records = {
        "NASA/HLS/HLSL30/v002": [
            _record("T20HNJ_20230101T140000", 1672531200000, 10.0),
            _record("T20HNJ_20230201T140000", 1675209600000, 20.0),
        ],
        "NASA/HLS/HLSS30/v002": [
            _record("T20HNJ_20230301T140000", 1677628800000, 5.0),
            _record("T01FBF_20230301T140000", 1677628800000, 2.0),
        ],
    }
    fake_ee = _SymbolicEarthEngine(records)

    result = build_hls_annual_composite(
        session=GeeSession(module=fake_ee),
        plan=plan,
        data_config=config.data,
        aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
        request=HlsCompositeRequest(
            start_date=date(2023, 1, 1),
            end_date_exclusive=date(2024, 1, 1),
            target_crs="EPSG:6933",
            scale_m=30,
        ),
        generated_at=datetime(2026, 7, 24, 15, tzinfo=UTC),
    )

    assert result.reflectance.bands == (
        "blue",
        "green",
        "red",
        "nir",
        "swir1",
        "swir2",
    )
    assert result.indices.bands == tuple(index.value for index in config.data.indices)
    assert result.valid_observation_count.bands == ("valid_observation_count",)
    assert result.valid_observation_count_l30.bands == ("valid_observation_count_l30",)
    assert result.valid_observation_count_s30.bands == ("valid_observation_count_s30",)
    assert result.metadata.input_scene_count == 3
    assert result.metadata.complete_scene_inventory is True
    assert tuple(source.scene_count for source in result.metadata.sources) == (2, 1)
    assert tuple(
        scene.image_id for source in result.metadata.sources for scene in source.scenes
    ) == (
        "T20HNJ_20230101T140000",
        "T20HNJ_20230201T140000",
        "T20HNJ_20230301T140000",
    )
    assert result.metadata.sources[0].mgrs_tile_ids == ("20HNJ",)
    assert result.metadata.sources[1].mgrs_tile_ids == ("20HNJ",)
    assert result.metadata.sources[0].spatial_filter_strategy == "filterBounds"
    assert result.metadata.sources[1].spatial_filter_strategy == "MGRS_TILE_ID"
    assert result.metadata.ndmi_lswi_equivalent is True
    assert fake_ee.collections["NASA/HLS/HLSL30/v002"].date_filter == (
        "2023-01-01",
        "2024-01-01",
    )
    assert fake_ee.collections["NASA/HLS/HLSS30/v002"].property_filter == (
        "MGRS_TILE_ID",
        ("20HNJ",),
    )


def test_annual_composite_rejects_no_scenes_and_sanitizes_remote_errors() -> None:
    plan, config = _plan_and_config()
    request = HlsCompositeRequest(
        start_date=date(2023, 1, 1),
        end_date_exclusive=date(2024, 1, 1),
        target_crs="EPSG:6933",
        scale_m=30,
    )
    empty: dict[str, list[dict[str, object]]] = {
        "NASA/HLS/HLSL30/v002": [],
        "NASA/HLS/HLSS30/v002": [],
    }

    with pytest.raises(
        HlsCompositeError,
        match="cannot_resolve_mgrs_tiles_without_l30_scenes",
    ):
        build_hls_annual_composite(
            session=GeeSession(module=_SymbolicEarthEngine(empty)),
            plan=plan,
            data_config=config.data,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            request=request,
        )

    with pytest.raises(HlsCompositeError) as captured:
        build_hls_annual_composite(
            session=GeeSession(module=_FailingEarthEngine({})),
            plan=plan,
            data_config=config.data,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            request=request,
        )
    assert captured.value.code == "permission_denied"
    assert "SECRET-" not in str(captured.value)


def test_annual_composite_validates_aoi_configuration_and_timestamp_before_remote() -> None:
    plan, config = _plan_and_config()
    request = HlsCompositeRequest(
        start_date=date(2023, 1, 1),
        end_date_exclusive=date(2024, 1, 1),
        target_crs="EPSG:6933",
        scale_m=30,
    )

    with pytest.raises(HlsCompositeError, match="aoi_must_be_polygonal"):
        build_hls_annual_composite(
            session=GeeSession(module=object()),
            plan=plan,
            data_config=config.data,
            aoi_wgs84=Point(-63, -33),
            request=request,
        )

    monthly_config = config.data.model_copy(update={"composition_interval": "monthly"})
    with pytest.raises(HlsCompositeError, match="configuration_is_not_annual"):
        build_hls_annual_composite(
            session=GeeSession(module=object()),
            plan=plan,
            data_config=monthly_config,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            request=request,
        )

    with pytest.raises(HlsCompositeError, match="generated_at_requires_timezone"):
        build_hls_annual_composite(
            session=GeeSession(module=object()),
            plan=plan,
            data_config=config.data,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            request=request,
            generated_at=datetime(2026, 7, 24),
        )


def test_request_and_fmask_reject_invalid_values() -> None:
    with pytest.raises(ValidationError, match="proyectado"):
        HlsCompositeRequest(
            start_date=date(2023, 1, 1),
            end_date_exclusive=date(2024, 1, 1),
            target_crs="EPSG:4326",
            scale_m=30,
        )
    with pytest.raises(ValidationError, match="reconocible"):
        HlsCompositeRequest(
            start_date=date(2023, 1, 1),
            end_date_exclusive=date(2024, 1, 1),
            target_crs="NO-ES-CRS",
            scale_m=30,
        )
    with pytest.raises(ValueError, match="0 y 255"):
        hls_fmask_pixel_is_valid(256)


def test_mgrs_tiles_are_extracted_deterministically_from_hls_identifiers() -> None:
    assert mgrs_tile_ids_from_hls_identifiers(
        (
            "T21HUB_20230101T140000",
            "T20HNJ_20230101T140000",
            "T21HUB_20230201T140000",
        )
    ) == ("20HNJ", "21HUB")
    with pytest.raises(ValueError, match="tile MGRS"):
        mgrs_tile_ids_from_hls_identifiers(("identificador-inesperado",))
