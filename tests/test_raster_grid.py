"""Pruebas sintéticas del contrato espacial temporal del Paso 11.0."""

from __future__ import annotations

import json
from math import isclose
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from pyproj import Transformer
from shapely.geometry import Point, Polygon, box
from shapely.ops import transform

from deforestation_pipeline.raster_grid import (
    RasterGridError,
    derive_raster_grid_spec,
)
from deforestation_pipeline.schemas import (
    RasterGridSpec,
    raster_grid_json_schema,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _synthetic_aoi_wgs84() -> Any:
    """Crea un ROI argentino desde coordenadas UTM conocidas y no alineadas."""
    projected = box(500_001, 6_499_991, 500_059, 6_500_059)
    transformer = Transformer.from_crs("EPSG:32720", "EPSG:4326", always_xy=True)
    return transform(transformer.transform, projected)


def _valid_grid_payload() -> dict[str, object]:
    spec = derive_raster_grid_spec(
        aoi_wgs84=_synthetic_aoi_wgs84(),
        target_crs="EPSG:32720",
        resolution_m=30,
        nodata=-9999,
    )
    return spec.model_dump(mode="json")


def test_grid_is_snapped_outward_and_deterministic() -> None:
    """El mismo ROI debe producir una única grilla alineada al origen del CRS."""
    first = derive_raster_grid_spec(
        aoi_wgs84=_synthetic_aoi_wgs84(),
        target_crs="EPSG:32720",
        resolution_m=30,
        nodata=-9999,
    )
    second = derive_raster_grid_spec(
        aoi_wgs84=_synthetic_aoi_wgs84(),
        target_crs="EPSG:32720",
        resolution_m=30.0,
        nodata=-9999.0,
    )

    assert first == second
    assert first.schema_version == "1.0.0"
    assert first.target_crs == "EPSG:32720"
    assert first.resolution_m == 30.0
    assert first.width == 3
    assert first.height == 3
    assert first.transform == pytest.approx((30.0, 0.0, 499_980.0, 0.0, -30.0, 6_500_070.0))
    assert first.bounds == pytest.approx((499_980.0, 6_499_980.0, 500_070.0, 6_500_070.0))
    assert first.alignment_strategy == "projected_crs_origin_outward_snap"
    assert first.pixel_orientation == "north_up"
    assert first.aoi_mask_required is True
    assert len(first.grid_sha256) == 64


def test_grid_bounds_cover_the_projected_aoi() -> None:
    """El snap siempre debe ampliar la envolvente, nunca recortar el territorio."""
    aoi = _synthetic_aoi_wgs84()
    spec = derive_raster_grid_spec(
        aoi_wgs84=aoi,
        target_crs="EPSG:32720",
        resolution_m=30,
        nodata=-9999,
    )
    transformer = Transformer.from_crs("EPSG:4326", spec.target_crs, always_xy=True)
    projected = transform(transformer.transform, aoi)
    left, bottom, right, top = spec.bounds
    min_x, min_y, max_x, max_y = projected.bounds

    assert left <= min_x
    assert bottom <= min_y
    assert right >= max_x
    assert top >= max_y
    assert isclose((right - left) / spec.resolution_m, spec.width)
    assert isclose((top - bottom) / spec.resolution_m, spec.height)


def test_grid_spatial_hash_excludes_product_nodata() -> None:
    """El identificador espacial no debe cambiar por la codificación de nodata."""
    first = derive_raster_grid_spec(
        aoi_wgs84=_synthetic_aoi_wgs84(),
        target_crs="EPSG:32720",
        resolution_m=30,
        nodata=-9999,
    )
    second = derive_raster_grid_spec(
        aoi_wgs84=_synthetic_aoi_wgs84(),
        target_crs="EPSG:32720",
        resolution_m=30,
        nodata=-32768,
    )

    assert first.grid_sha256 == second.grid_sha256
    assert first.nodata != second.nodata


@pytest.mark.parametrize(
    ("aoi", "target_crs", "resolution_m", "nodata", "message"),
    [
        (Point(-63, -32), "EPSG:32720", 30, -9999, "Polygon o MultiPolygon"),
        (Polygon(), "EPSG:32720", 30, -9999, "no puede estar vacío"),
        (
            Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]),
            "EPSG:32720",
            30,
            -9999,
            "topología válida",
        ),
        (box(181, 0, 182, 1), "EPSG:32720", 30, -9999, "rango EPSG:4326"),
        (
            box(-63, -32, -62.99, -31.99),
            "crs-inexistente",
            30,
            -9999,
            "CRS reconocible",
        ),
        (box(-63, -32, -62.99, -31.99), "EPSG:4326", 30, -9999, "proyectado"),
        (box(-63, -32, -62.99, -31.99), "EPSG:32720", 0, -9999, "positiva"),
        (
            box(-63, -32, -62.99, -31.99),
            "EPSG:32720",
            30,
            float("nan"),
            "nodata",
        ),
    ],
)
def test_grid_derivation_rejects_unsafe_inputs(
    aoi: Any,
    target_crs: str,
    resolution_m: float,
    nodata: float,
    message: str,
) -> None:
    """La derivación debe fallar sin producir una grilla parcialmente válida."""
    with pytest.raises(RasterGridError, match=message):
        derive_raster_grid_spec(
            aoi_wgs84=aoi,
            target_crs=target_crs,
            resolution_m=resolution_m,
            nodata=nodata,
        )


def test_grid_contract_rejects_tampered_transform_or_hash() -> None:
    """Dimensiones, transformación, bounds y hash son una sola identidad."""
    transform_payload = _valid_grid_payload()
    transform_payload["transform"] = [30, 0, 500_010, 0, -30, 6_500_070]

    with pytest.raises(ValidationError, match="bounds no coinciden"):
        RasterGridSpec.model_validate(transform_payload)

    hash_payload = _valid_grid_payload()
    hash_payload["grid_sha256"] = "a" * 64

    with pytest.raises(ValidationError, match="grid_sha256 no coincide"):
        RasterGridSpec.model_validate(hash_payload)


def test_grid_contract_forbids_unknown_fields() -> None:
    payload = _valid_grid_payload()
    payload["silent_resampling"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RasterGridSpec.model_validate(payload)


def test_raster_grid_error_requires_at_least_one_violation() -> None:
    with pytest.raises(ValueError, match="al menos una violación"):
        RasterGridError([])


def test_committed_raster_grid_schema_matches_pydantic_model() -> None:
    """El JSON Schema versionado no puede divergir del contrato ejecutable."""
    path = PROJECT_ROOT / "data" / "schemas" / "raster-grid-v1.0.0.json"
    committed_schema = json.loads(path.read_text(encoding="utf-8"))

    assert committed_schema == raster_grid_json_schema()
