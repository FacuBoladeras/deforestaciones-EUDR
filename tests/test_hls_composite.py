"""Reglas científicas y orquestación remota de la composición anual HLS."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from shapely.geometry import Point, box

import deforestation_pipeline.hls_composite as hls_composite_module
from deforestation_pipeline.catalog import (
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.hls_composite import (
    HlsCompositeError,
    HlsCompositeRequest,
    HlsTemporalCompositeRequest,
    build_hls_annual_composite,
    build_hls_composite,
    hls_fmask_pixel_is_valid,
)
from deforestation_pipeline.hls_tiles import mgrs_tile_ids_from_hls_identifiers

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _InfoValue:
    def __init__(self, value: object) -> None:
        self.value = value

    def getInfo(self) -> object:
        return self.value


class _ScriptedInfoValue:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def getInfo(self) -> object:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _ScriptedInventoryCollection:
    def __init__(self, values: dict[str, _ScriptedInfoValue]) -> None:
        self.values = values

    def size(self) -> _ScriptedInfoValue:
        return self.values["size"]

    def aggregate_array(self, property_name: str) -> _ScriptedInfoValue:
        return self.values[property_name]


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

    def unmask(self, value: object, same_footprint: bool) -> _SymbolicImage:
        return self._next(f"unmask:{value}:{same_footprint}")

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


def _inventory_collection(
    *,
    identifiers: list[object] | None = None,
    identifier_outcomes: list[object] | None = None,
) -> _ScriptedInventoryCollection:
    image_ids = identifiers or [
        "T20HNJ_20230101T140000",
        "T20HNJ_20230201T140000",
    ]
    return _ScriptedInventoryCollection(
        {
            "size": _ScriptedInfoValue([len(image_ids)]),
            "system:index": _ScriptedInfoValue(identifier_outcomes or [image_ids]),
            "system:time_start": _ScriptedInfoValue([[1672531200000, 1675209600000]]),
            "CLOUD_COVERAGE": _ScriptedInfoValue([[10.0, 20.0]]),
        }
    )


def _complete_l30_inventory(
    collection: _ScriptedInventoryCollection,
) -> Any:
    plan, _ = _plan_and_config()
    return hls_composite_module._complete_inventory(
        collection,
        plan.sources[0],
        spatial_filter_strategy="filterBounds",
    )


def test_composite_request_requires_one_complete_calendar_year() -> None:
    request = HlsCompositeRequest(
        start_date=date(2023, 1, 1),
        end_date_exclusive=date(2024, 1, 1),
        target_crs="EPSG:6933",
        scale_m=30,
    )

    assert request.period_days == 365
    assert request.reducer == "median"


def test_temporal_composite_request_accepts_explicit_seasonal_window() -> None:
    request = HlsTemporalCompositeRequest(
        start_date=date(2022, 12, 1),
        end_date_exclusive=date(2023, 3, 1),
        target_crs="EPSG:6933",
        scale_m=30,
    )

    assert request.period_days == 90
    assert request.reducer == "median"


def test_temporal_composite_request_rejects_empty_or_reversed_window() -> None:
    with pytest.raises(ValidationError, match="anterior"):
        HlsTemporalCompositeRequest(
            start_date=date(2023, 3, 1),
            end_date_exclusive=date(2023, 3, 1),
            target_crs="EPSG:6933",
            scale_m=30,
        )


def test_temporal_composite_request_rejects_more_than_366_days() -> None:
    with pytest.raises(ValidationError, match="366"):
        HlsTemporalCompositeRequest(
            start_date=date(2022, 1, 1),
            end_date_exclusive=date(2023, 1, 3),
            target_crs="EPSG:6933",
            scale_m=30,
        )


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
    for count_image in (
        result.valid_observation_count,
        result.valid_observation_count_l30,
        result.valid_observation_count_s30,
    ):
        assert "unmask:0:False" in count_image.history
        assert count_image.history.index("unmask:0:False") < count_image.history.index("clip")
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
    assert result.metadata.indices_derived_from == "annual_median_reflectance"


@pytest.mark.parametrize(
    "stage",
    (
        "remote_aoi",
        "source_lookup",
        "l30_collection",
        "l30_inventory",
        "s30_collection",
        "s30_inventory",
        "normalize_l30",
        "normalize_s30",
        "merge",
        "reflectance",
        "total_count",
        "l30_count",
        "s30_count",
        "indices",
    ),
)
def test_composite_failure_identifies_period_stage_and_sanitized_category(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, config = _plan_and_config()
    records = {
        "NASA/HLS/HLSL30/v002": [
            _record("T20HNJ_20221215T140000", 1671112800000, 10.0),
        ],
        "NASA/HLS/HLSS30/v002": [
            _record("T20HNJ_20230201T140000", 1675252800000, 5.0),
        ],
    }
    original_run = hls_composite_module._run_composite_stage
    secret = "https://earthengine.example/task?token=SECRET&project=my-project"

    def injected_run(
        *,
        period_code: str,
        stage: str,
        operation: Any,
    ) -> Any:
        if stage == requested_stage:

            def fail() -> Any:
                raise RuntimeError(f"503 service unavailable {secret}")

            return original_run(
                period_code=period_code,
                stage=stage,
                operation=fail,
            )
        return original_run(
            period_code=period_code,
            stage=stage,
            operation=operation,
        )

    requested_stage = stage
    monkeypatch.setattr(hls_composite_module, "_run_composite_stage", injected_run)

    with pytest.raises(HlsCompositeError) as captured:
        build_hls_composite(
            session=GeeSession(module=_SymbolicEarthEngine(records)),
            plan=plan,
            data_config=config.data,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            request=HlsTemporalCompositeRequest(
                start_date=date(2022, 12, 1),
                end_date_exclusive=date(2023, 3, 1),
                target_crs="EPSG:6933",
                scale_m=30,
            ),
            period_id="2022-DJF",
        )

    assert captured.value.code == f"2022_djf_{stage}_service_unavailable"
    assert "http" not in str(captured.value).lower()
    assert "secret" not in str(captured.value).lower()
    assert "my-project" not in str(captured.value).lower()


def test_composite_preserves_contextual_inventory_error_inside_period_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, config = _plan_and_config()
    records = {
        "NASA/HLS/HLSL30/v002": [
            _record("T20HNJ_20221215T140000", 1671112800000, 10.0),
        ],
        "NASA/HLS/HLSS30/v002": [],
    }
    original_run = hls_composite_module._run_composite_stage

    def injected_run(
        *,
        period_code: str,
        stage: str,
        operation: Any,
    ) -> Any:
        if stage == "l30_inventory":

            def fail() -> Any:
                raise HlsCompositeError("hlsl30_inventory_system_index_service_unavailable")

            return original_run(
                period_code=period_code,
                stage=stage,
                operation=fail,
            )
        return original_run(
            period_code=period_code,
            stage=stage,
            operation=operation,
        )

    monkeypatch.setattr(hls_composite_module, "_run_composite_stage", injected_run)

    with pytest.raises(HlsCompositeError) as captured:
        build_hls_composite(
            session=GeeSession(module=_SymbolicEarthEngine(records)),
            plan=plan,
            data_config=config.data,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            request=HlsTemporalCompositeRequest(
                start_date=date(2022, 12, 1),
                end_date_exclusive=date(2023, 3, 1),
                target_crs="EPSG:6933",
                scale_m=30,
            ),
            period_id="2022-DJF",
        )

    assert captured.value.code == (
        "2022_djf_l30_inventory_hlsl30_inventory_system_index_service_unavailable"
    )


def test_composite_uses_annual_year_or_explicit_interval_as_error_period() -> None:
    plan, config = _plan_and_config()
    annual_request = HlsCompositeRequest(
        start_date=date(2023, 1, 1),
        end_date_exclusive=date(2024, 1, 1),
        target_crs="EPSG:6933",
        scale_m=30,
    )
    interval_request = HlsTemporalCompositeRequest(
        start_date=date(2022, 12, 1),
        end_date_exclusive=date(2023, 3, 1),
        target_crs="EPSG:6933",
        scale_m=30,
    )

    with pytest.raises(HlsCompositeError) as annual:
        build_hls_annual_composite(
            session=GeeSession(module=_FailingEarthEngine({})),
            plan=plan,
            data_config=config.data,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            request=annual_request,
        )
    with pytest.raises(HlsCompositeError) as interval:
        build_hls_composite(
            session=GeeSession(module=_FailingEarthEngine({})),
            plan=plan,
            data_config=config.data,
            aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
            request=interval_request,
        )

    assert annual.value.code == "2023_l30_collection_permission_denied"
    assert interval.value.code == "20221201_20230301_l30_collection_permission_denied"


def test_inventory_retries_only_failed_transient_field_then_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _inventory_collection(
        identifier_outcomes=[
            RuntimeError("503 backend unavailable"),
            [
                "T20HNJ_20230101T140000",
                "T20HNJ_20230201T140000",
            ],
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(hls_composite_module, "_metadata_retry_sleep", sleeps.append)

    inventory = _complete_l30_inventory(collection)
    expected = _complete_l30_inventory(_inventory_collection())

    assert inventory == expected
    assert tuple(scene.image_id for scene in inventory.scenes) == (
        "T20HNJ_20230101T140000",
        "T20HNJ_20230201T140000",
    )
    assert tuple(scene.cloud_coverage_pct for scene in inventory.scenes) == (10.0, 20.0)
    assert collection.values["size"].calls == 1
    assert collection.values["system:index"].calls == 2
    assert collection.values["system:time_start"].calls == 1
    assert collection.values["CLOUD_COVERAGE"].calls == 1
    assert sleeps == [hls_composite_module.METADATA_RETRY_BACKOFF_SECONDS]


def test_inventory_transient_failure_stops_after_two_attempts_and_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "https://earthengine.example/task?token=SECRET&project=my-project"
    collection = _inventory_collection(
        identifier_outcomes=[
            RuntimeError(f"503 service unavailable {secret}"),
            RuntimeError(f"503 service unavailable {secret}"),
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(hls_composite_module, "_metadata_retry_sleep", sleeps.append)

    with pytest.raises(HlsCompositeError) as captured:
        _complete_l30_inventory(collection)

    assert captured.value.code == "hlsl30_inventory_system_index_service_unavailable"
    assert collection.values["system:index"].calls == 2
    assert sleeps == [hls_composite_module.METADATA_RETRY_BACKOFF_SECONDS]
    assert "http" not in str(captured.value).lower()
    assert "secret" not in str(captured.value).lower()
    assert "my-project" not in str(captured.value).lower()


@pytest.mark.parametrize(
    ("operation", "message", "category"),
    [
        ("size", "deadline exceeded", "timeout"),
        ("system:index", "429 rate limit exceeded", "quota_or_rate_limit"),
        ("system:time_start", "connection reset during transfer", "connection_error"),
        ("CLOUD_COVERAGE", "500 internal backend error", "service_unavailable"),
    ],
)
def test_inventory_failure_code_includes_product_operation_and_category(
    operation: str,
    message: str,
    category: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _inventory_collection()
    collection.values[operation] = _ScriptedInfoValue(
        [RuntimeError(message), RuntimeError(message)]
    )
    monkeypatch.setattr(hls_composite_module, "_metadata_retry_sleep", lambda _: None)

    with pytest.raises(HlsCompositeError) as captured:
        _complete_l30_inventory(collection)

    operation_code = operation.lower().replace(":", "_")
    assert captured.value.code == f"hlsl30_inventory_{operation_code}_{category}"


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("403 permission denied", "permission_denied"),
        ("401 unauthenticated invalid token", "authentication_failed"),
        ("400 invalid argument", "invalid_argument"),
        ("user memory limit exceeded", "memory_limit"),
    ],
)
def test_inventory_does_not_retry_nonretryable_failures(
    message: str,
    category: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _inventory_collection(
        identifier_outcomes=[RuntimeError(message), ["must-not-be-read"]]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(hls_composite_module, "_metadata_retry_sleep", sleeps.append)

    with pytest.raises(HlsCompositeError) as captured:
        _complete_l30_inventory(collection)

    assert captured.value.code == f"hlsl30_inventory_system_index_{category}"
    assert collection.values["system:index"].calls == 1
    assert sleeps == []


def test_generic_composite_uses_exact_seasonal_bounds() -> None:
    plan, config = _plan_and_config()
    records = {
        "NASA/HLS/HLSL30/v002": [
            _record("T20HNJ_20221215T140000", 1671112800000, 10.0),
        ],
        "NASA/HLS/HLSS30/v002": [
            _record("T20HNJ_20230201T140000", 1675252800000, 5.0),
        ],
    }
    fake_ee = _SymbolicEarthEngine(records)

    result = build_hls_composite(
        session=GeeSession(module=fake_ee),
        plan=plan,
        data_config=config.data,
        aoi_wgs84=box(-63.0, -33.0, -62.99, -32.99),
        request=HlsTemporalCompositeRequest(
            start_date=date(2022, 12, 1),
            end_date_exclusive=date(2023, 3, 1),
            target_crs="EPSG:6933",
            scale_m=30,
        ),
        generated_at=datetime(2026, 7, 28, 15, tzinfo=UTC),
    )

    assert result.metadata.start_date == date(2022, 12, 1)
    assert result.metadata.end_date_exclusive == date(2023, 3, 1)
    assert result.metadata.period_days == 90
    assert result.metadata.indices_derived_from == "period_median_reflectance"
    assert fake_ee.collections["NASA/HLS/HLSL30/v002"].date_filter == (
        "2022-12-01",
        "2023-03-01",
    )


def test_sensor_without_scenes_is_explicit_zero_inside_clipped_aoi() -> None:
    plan, config = _plan_and_config()
    records = {
        "NASA/HLS/HLSL30/v002": [
            _record("T20HNJ_20230101T140000", 1672531200000, 10.0),
        ],
        "NASA/HLS/HLSS30/v002": [],
    }

    result = build_hls_annual_composite(
        session=GeeSession(module=_SymbolicEarthEngine(records)),
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

    assert tuple(source.scene_count for source in result.metadata.sources) == (1, 0)
    assert "multiply" in result.valid_observation_count_s30.history
    assert "unmask:0:False" in result.valid_observation_count_s30.history
    assert result.valid_observation_count_s30.history[-1] == "clip"


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
    assert captured.value.code == "2023_l30_collection_permission_denied"
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
