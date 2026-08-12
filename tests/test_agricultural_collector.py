from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from shapely.geometry import shape

from deforestation_pipeline.agricultural_collector import (
    DYNAMIC_WORLD_RASTER_BANDS,
    CollectedWindowRaster,
    DynamicWorldWindowQuery,
    collect_dynamic_world_window,
    load_agricultural_collector_config,
    materialize_agricultural_evidence_collection,
)
from deforestation_pipeline.catalog import (
    build_agricultural_evidence_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import load_config
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.raster_products import RasterDownloadError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREATED = datetime(2026, 8, 11, 15, tzinfo=UTC)
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_agricultural_collector.py"


def _event_feature(*, event_id: str = "PDE-1", onset: bool = True) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-60.0000, -31.0000],
                    [-59.9990, -31.0000],
                    [-59.9990, -30.9992],
                    [-60.0000, -31.0000],
                ]
            ],
        },
        "properties": {
            "event_id": event_id,
            "event_type": "persistent_disturbance_event",
            "estimated_onset_window_end": "2022-05-31" if onset else None,
            "area_ha": 0.5,
            "area_threshold_met": True,
        },
    }


def _event_bundle(tmp_path: Path, features: list[dict[str, Any]]) -> Path:
    bundle = tmp_path / "events"
    relative = "json/evidence/disturbance_events.geojson"
    artifact = bundle / relative
    artifact.parent.mkdir(parents=True)
    content = json.dumps({"type": "FeatureCollection", "features": features}).encode()
    artifact.write_bytes(content)
    manifest = {
        "schema_version": "test",
        "analysis_id": "11111111-1111-5111-8111-111111111111",
        "created_at": CREATED.isoformat(),
        "input_sha256": "c" * 64,
        "artifacts": [
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    manifest_path = bundle / "json/run/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def _raster_bytes(query: DynamicWorldWindowQuery, *, mode: str) -> bytes:
    height, width = query.grid.height, query.grid.width
    nodata = query.grid.nodata
    values = np.full((4, height, width), nodata, dtype=np.float32)
    if mode == "crop":
        values[0] = 4.0
        values[1] = 4.0
        values[2] = 0.85
        values[3] = 1.0
    elif mode == "unknown":
        values[0] = 4.0
        values[1] = 0.0
        values[2] = 0.25
        values[3] = 0.0
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 4,
        "dtype": "float32",
        "crs": query.grid.target_crs,
        "transform": rasterio.Affine(*query.grid.transform),
        "nodata": nodata,
    }
    with MemoryFile() as memory:
        with memory.open(**profile) as dataset:
            dataset.write(values)
            dataset.descriptions = DYNAMIC_WORLD_RASTER_BANDS
        return bytes(memory.read())


def _run(
    tmp_path: Path,
    provider: Any,
    *,
    features: list[dict[str, Any]] | None = None,
) -> Path:
    return materialize_agricultural_evidence_collection(
        source_event_bundle=_event_bundle(tmp_path, features or [_event_feature()]),
        output_root=tmp_path / "out",
        establishment_id="synthetic-field",
        analysis_end_date=date(2022, 7, 31),
        config_path=PROJECT_ROOT / "configs/agricultural-collector.yml",
        catalog_path=PROJECT_ROOT / "data/catalog.yml",
        licenses_path=PROJECT_ROOT / "data/licenses.yml",
        created_at=CREATED,
        raster_provider=provider,
    )


def test_production_collector_config_is_versioned_and_bounded() -> None:
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")

    assert config.schema_version == "1.0.0"
    assert config.source_id == "dynamic_world_v1"
    assert config.target_crs == "EPSG:6933"
    assert config.resolution_m == 10
    assert config.temporal_cadence == "calendar_month"
    assert config.maximum_events <= 100
    assert config.maximum_windows_per_event <= 72
    assert config.minimum_top1_probability == pytest.approx(0.6)


def test_collector_queries_only_post_onset_months_and_materializes_auditable_bundle(
    tmp_path: Path,
) -> None:
    queries: list[DynamicWorldWindowQuery] = []

    def provider(query: DynamicWorldWindowQuery) -> CollectedWindowRaster:
        queries.append(query)
        mode = "crop" if query.window_id == "2022-06" else "nodata"
        return CollectedWindowRaster(
            content=_raster_bytes(query, mode=mode),
            matched_scene_count=4 if mode == "crop" else 0,
            retrieved_at=CREATED,
        )

    output = _run(tmp_path, provider)

    assert [(item.start_date, item.end_date_exclusive) for item in queries] == [
        (date(2022, 6, 1), date(2022, 7, 1)),
        (date(2022, 7, 1), date(2022, 8, 1)),
    ]
    csv_path = output / "tables/evidence/agricultural_evidence.csv"
    rows = list(csv.DictReader(StringIO(csv_path.read_text(encoding="utf-8"))))
    assert len(rows) == 6
    by_key = {(row["window_id"], row["class_name"]): row for row in rows}
    june_crop = float(by_key[("2022-06", "crop")]["area_ha"])
    june_unknown = float(by_key[("2022-06", "unknown")]["area_ha"])
    june_nodata = float(by_key[("2022-06", "nodata")]["area_ha"])
    july_nodata = float(by_key[("2022-07", "nodata")]["area_ha"])
    event_area = float(by_key[("2022-06", "crop")]["event_area_ha"])
    assert june_crop == pytest.approx(event_area, abs=1e-6)
    assert june_unknown == pytest.approx(0.0, abs=1e-6)
    assert june_nodata == pytest.approx(0.0, abs=1e-6)
    assert july_nodata == pytest.approx(event_area, abs=1e-6)
    assert all(
        sum(
            float(by_key[(window, class_name)]["area_ha"])
            for class_name in ("crop", "unknown", "nodata")
        )
        == pytest.approx(event_area, abs=1e-6)
        for window in ("2022-06", "2022-07")
    )

    metadata = json.loads(
        (output / "json/evidence/agricultural_evidence_metadata.json").read_text("utf-8")
    )
    assert metadata["source"]["source_id"] == "dynamic_world_v1"
    assert metadata["source"]["evidence_role"] == "candidate_independent"
    assert metadata["spatial_method"]["event_footprint"] == (
        "exact_polygon_pixel_intersection_fraction"
    )
    assert metadata["spatial_method"]["resampling"] == "nearest"
    assert metadata["query_count"] == 2
    assert metadata["signed_urls_persisted"] is False

    geojson = json.loads(
        (output / "json/evidence/agricultural_evidence.geojson").read_text("utf-8")
    )
    assert len(geojson["features"]) == 1
    assert geojson["features"][0]["properties"]["collection_status"] == "collected"
    manifest = json.loads((output / "json/run/manifest.json").read_text("utf-8"))
    assert manifest["source_event_bundle"]["input_sha256"] == "c" * 64
    assert manifest["remote_data_accessed"] is True
    assert all((output / item["path"]).is_file() for item in manifest["artifacts"])
    assert any(
        item["path"].endswith("event_footprint_fraction.tif") for item in manifest["artifacts"]
    )
    assert sum(item["path"].endswith("dynamic_world.tif") for item in manifest["artifacts"]) == 2
    pngs = [
        item
        for item in manifest["artifacts"]
        if item["path"] == "figures/evidence/agricultural_monthly_evidence_PDE-1.png"
    ]
    assert len(pngs) == 1
    assert manifest["visualizations"][0]["sha256"] == pngs[0]["sha256"]
    assert manifest["visualizations"][0]["selected_window_ids"] == ["2022-06", "2022-07"]
    assert manifest["visualizations"][0]["source_rasters"]


def test_collector_preserves_missing_onset_without_querying(tmp_path: Path) -> None:
    calls: list[DynamicWorldWindowQuery] = []

    def provider(query: DynamicWorldWindowQuery) -> CollectedWindowRaster:
        calls.append(query)
        raise AssertionError("no debe consultar un evento sin onset")

    output = _run(tmp_path, provider, features=[_event_feature(onset=False)])

    assert calls == []
    payload = json.loads((output / "json/evidence/agricultural_evidence.json").read_text("utf-8"))
    assert payload["events"][0]["collection_status"] == "insufficient_onset"
    assert payload["events"][0]["quality_flags"] == ["estimated_onset_window_end_missing"]
    assert payload["rows"] == []


def test_collector_rejects_provider_raster_on_wrong_grid(tmp_path: Path) -> None:
    def provider(query: DynamicWorldWindowQuery) -> CollectedWindowRaster:
        content = _raster_bytes(query, mode="crop")
        with MemoryFile(content) as source_memory:
            with source_memory.open() as source:
                values = source.read()
                profile = source.profile.copy()
        profile["transform"] = rasterio.Affine.translation(10, 0) * profile["transform"]
        with MemoryFile() as target_memory:
            with target_memory.open(**profile) as target:
                target.write(values)
            broken = bytes(target_memory.read())
        return CollectedWindowRaster(
            content=broken,
            matched_scene_count=4,
            retrieved_at=CREATED,
        )

    with pytest.raises(RasterDownloadError, match="grid_transform_mismatch"):
        _run(tmp_path, provider)


def test_collector_rejects_impossible_dynamic_world_counts(tmp_path: Path) -> None:
    def provider(query: DynamicWorldWindowQuery) -> CollectedWindowRaster:
        height, width = query.grid.height, query.grid.width
        values = np.zeros((4, height, width), dtype=np.float32)
        values[0] = 2.0
        values[1] = 3.0
        values[2] = 0.8
        values[3] = 1.5
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 4,
            "dtype": "float32",
            "crs": query.grid.target_crs,
            "transform": rasterio.Affine(*query.grid.transform),
            "nodata": query.grid.nodata,
        }
        with MemoryFile() as memory:
            with memory.open(**profile) as dataset:
                dataset.write(values)
            content = bytes(memory.read())
        return CollectedWindowRaster(content=content, matched_scene_count=2, retrieved_at=CREATED)

    with pytest.raises(ValueError, match="dynamic_world_raster_values_invalid"):
        _run(tmp_path, provider)


def test_collector_rejects_tampered_source_manifest_artifact(tmp_path: Path) -> None:
    bundle = _event_bundle(tmp_path, [_event_feature()])
    (bundle / "json/evidence/disturbance_events.geojson").write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match=r"source_artifact_(size|sha256)_mismatch"):
        materialize_agricultural_evidence_collection(
            source_event_bundle=bundle,
            output_root=tmp_path / "out",
            establishment_id="synthetic-field",
            analysis_end_date=date(2022, 7, 31),
            config_path=PROJECT_ROOT / "configs/agricultural-collector.yml",
            catalog_path=PROJECT_ROOT / "data/catalog.yml",
            licenses_path=PROJECT_ROOT / "data/licenses.yml",
            created_at=CREATED,
            raster_provider=lambda _: pytest.fail("no debe consultar"),
        )


class _Scalar:
    def __init__(self, value: int) -> None:
        self.value = value

    def getInfo(self) -> int:
        return self.value


class _Image:
    def __init__(self, expression: str) -> None:
        self.expression = expression

    def select(self, band: str) -> _Image:
        return _Image(f"{self.expression}.select({band})")

    def eq(self, value: object) -> _Image:
        return _Image(f"{self.expression}.eq({value})")

    def gte(self, value: object) -> _Image:
        return _Image(f"{self.expression}.gte({value})")

    def And(self, other: _Image) -> _Image:
        return _Image(f"{self.expression}.And({other.expression})")

    def rename(self, name: str) -> _Image:
        return _Image(f"{self.expression}.rename({name})")

    def divide(self, other: _Image) -> _Image:
        return _Image(f"{self.expression}.divide({other.expression})")

    def addBands(self, other: _Image) -> _Image:
        return _Image(f"{self.expression}.addBands({other.expression})")

    def updateMask(self, other: _Image) -> _Image:
        return _Image(f"{self.expression}.updateMask({other.expression})")

    def gt(self, value: object) -> _Image:
        return _Image(f"{self.expression}.gt({value})")

    def toFloat(self) -> _Image:
        return _Image(f"{self.expression}.toFloat()")


class _Collection:
    def __init__(self, module: _FakeEe, expression: str) -> None:
        self.module = module
        self.expression = expression

    def filterBounds(self, geometry: object) -> _Collection:
        self.module.bounds = geometry
        return self

    def filterDate(self, start: str, end: str) -> _Collection:
        self.module.date_filter = (start, end)
        return self

    def size(self) -> _Scalar:
        return _Scalar(self.module.scene_count)

    def select(self, band: str) -> _Collection:
        return _Collection(self.module, f"{self.expression}.select({band})")

    def count(self) -> _Image:
        return _Image(f"{self.expression}.count()")

    def mean(self) -> _Image:
        return _Image(f"{self.expression}.mean()")

    def map(self, function: Any) -> _Collection:
        self.module.mapped_expression = function(_Image("scene")).expression
        return _Collection(self.module, f"{self.expression}.map(qualifying_crop)")

    def sum(self) -> _Image:
        return _Image(f"{self.expression}.sum()")


class _FakeEe:
    def __init__(self, scene_count: int = 3) -> None:
        self.scene_count = scene_count
        self.collection_id: str | None = None
        self.bounds: object | None = None
        self.date_filter: tuple[str, str] | None = None
        self.mapped_expression: str | None = None

    def Geometry(self, payload: object) -> object:
        return payload

    def ImageCollection(self, collection_id: str) -> _Collection:
        self.collection_id = collection_id
        return _Collection(self, collection_id)

    def Image(self, image: _Image) -> _Image:
        return image


def _dynamic_world_query() -> DynamicWorldWindowQuery:
    feature = _event_feature()
    geometry = shape(feature["geometry"])
    config = load_agricultural_collector_config(PROJECT_ROOT / "configs/agricultural-collector.yml")
    source = build_agricultural_evidence_source_plan(
        load_source_catalog(PROJECT_ROOT / "data/catalog.yml")
    ).candidate_source
    from deforestation_pipeline.raster_grid import derive_raster_grid_spec

    grid = derive_raster_grid_spec(
        aoi_wgs84=geometry,
        target_crs=config.target_crs,
        resolution_m=float(config.resolution_m),
        nodata=config.raster_nodata,
    )
    return DynamicWorldWindowQuery(
        event_id="PDE-1",
        window_id="2022-06",
        start_date=date(2022, 6, 1),
        end_date_exclusive=date(2022, 7, 1),
        geometry_wgs84=geometry,
        grid=grid,
        source=source,
        minimum_top1_probability=0.6,
    )


def test_gee_provider_filters_event_and_builds_top1_crop_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    module = _FakeEe()
    captured: dict[str, Any] = {}

    def materialize(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return type("Result", (), {"content": b"normalized"})()

    monkeypatch.setattr(collector_module, "materialize_ee_image_to_grid", materialize)
    result = collect_dynamic_world_window(
        query=_dynamic_world_query(),
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )

    assert result.content == b"normalized"
    assert result.matched_scene_count == 3
    assert module.collection_id == "GOOGLE/DYNAMICWORLD/V1"
    assert module.bounds is not None
    assert module.date_filter == ("2022-06-01", "2022-07-01")
    assert module.mapped_expression == (
        "scene.select(label).eq(4).And(scene.select(crops).gte(0.6)).rename(qualifying_crop)"
    )
    assert captured["band_names"] == DYNAMIC_WORLD_RASTER_BANDS
    assert captured["grid_spec"].grid_sha256 == _dynamic_world_query().grid.grid_sha256
    assert cast(_Image, captured["image"]).expression.endswith(".toFloat()")


def test_gee_provider_skips_download_when_month_has_no_scenes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deforestation_pipeline.agricultural_collector as collector_module

    module = _FakeEe(scene_count=0)
    monkeypatch.setattr(
        collector_module,
        "materialize_ee_image_to_grid",
        lambda **_: pytest.fail("no debe descargar una colección vacía"),
    )
    result = collect_dynamic_world_window(
        query=_dynamic_world_query(),
        session=GeeSession(module=module),
        output_config=load_config(PROJECT_ROOT / "configs/default.yml").output,
        retrieved_at=CREATED,
    )

    assert result.content is None
    assert result.matched_scene_count == 0


def test_collector_cli_builds_authenticated_provider_and_forwards_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("run_agricultural_collector_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    event_bundle = tmp_path / "events"
    event_bundle.mkdir()
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")
    session = object()
    provider = object()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(script, "authenticate_earth_engine", lambda _: session)
    monkeypatch.setattr(
        script,
        "make_dynamic_world_raster_provider",
        lambda **kwargs: provider if kwargs["session"] is session else pytest.fail("bad session"),
    )

    def materialize(**kwargs: Any) -> Path:
        captured.update(kwargs)
        return tmp_path / "published"

    monkeypatch.setattr(script, "materialize_agricultural_evidence_collection", materialize)
    exit_code = script.main(
        [
            str(event_bundle),
            "--establishment-id",
            "field",
            "--analysis-end-date",
            "2025-12-31",
            "--credentials",
            str(credentials),
        ]
    )

    assert exit_code == 0
    assert captured["source_event_bundle"] == event_bundle
    assert captured["establishment_id"] == "field"
    assert captured["analysis_end_date"] == date(2025, 12, 31)
    assert captured["raster_provider"] is provider
