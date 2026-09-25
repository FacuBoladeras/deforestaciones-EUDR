"""Pruebas de la frontera multiformato previa al contrato GeoJSON interno."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from deforestation_pipeline.vector_ingestion import (
    VectorIngestionError,
    ingest_local_vector,
)


def _geodataframe(
    *geometries: object,
    crs: str | None = "EPSG:4326",
) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": [f"feature-{index}" for index in range(len(geometries))]},
        geometry=list(geometries),
        crs=crs,
    )


def test_geojson_is_converted_to_the_single_internal_geometry_contract(
    tmp_path: Path,
) -> None:
    source = tmp_path / "territorio.geojson"
    _geodataframe(box(-63, -33, -62.99, -32.99)).to_file(
        source,
        driver="GeoJSON",
        engine="pyogrio",
    )

    result = ingest_local_vector(source)

    converted = json.loads(result.converted_geojson_bytes)
    assert result.source_format == "GeoJSON"
    assert result.source_crs == "EPSG:4326"
    assert result.feature_count == 1
    assert result.dissolved is False
    assert result.selected_layer == "territorio"
    assert result.available_layers == ("territorio",)
    assert result.geometry.type == "Polygon"
    assert converted["type"] == "Feature"
    assert converted["properties"] == {}
    assert converted["geometry"]["type"] == "Polygon"
    assert result.source_files == {"territorio.geojson": source.read_bytes()}
    assert result.provenance_payload()["converted_path"] == "json/input/converted.geojson"
    assert result.bundle_source_files() == {"source/territorio.geojson": source.read_bytes()}
    assert len(result.input_sha256) == 64


def test_shapefile_uses_embedded_crs_and_preserves_every_sidecar(
    tmp_path: Path,
) -> None:
    source = tmp_path / "territorio.shp"
    _geodataframe(
        box(500_000, 6_348_000, 501_000, 6_349_000),
        crs="EPSG:32720",
    ).to_file(source, driver="ESRI Shapefile", engine="pyogrio")

    result = ingest_local_vector(source)

    assert result.source_format == "ESRI Shapefile"
    assert result.source_crs == "EPSG:32720"
    assert {"territorio.shp", "territorio.shx", "territorio.dbf", "territorio.prj"} <= set(
        result.source_files
    )


def test_zipped_shapefile_is_read_without_extracting_it_to_the_workspace(
    tmp_path: Path,
) -> None:
    shape_directory = tmp_path / "shape"
    shape_directory.mkdir()
    shapefile = shape_directory / "territorio.shp"
    _geodataframe(box(-63, -33, -62.99, -32.99)).to_file(
        shapefile,
        driver="ESRI Shapefile",
        engine="pyogrio",
    )
    archive = tmp_path / "territorio.zip"
    with zipfile.ZipFile(archive, "w") as target:
        for sidecar in shape_directory.iterdir():
            target.write(sidecar, arcname=sidecar.name)

    result = ingest_local_vector(archive)

    assert result.source_format == "ESRI Shapefile"
    assert result.source_files == {"territorio.zip": archive.read_bytes()}
    assert result.source_crs == "EPSG:4326"


def test_multilayer_geopackage_requires_an_explicit_layer(
    tmp_path: Path,
) -> None:
    source = tmp_path / "territorios.gpkg"
    _geodataframe(box(-63, -33, -62.99, -32.99)).to_file(
        source,
        layer="norte",
        driver="GPKG",
        engine="pyogrio",
    )
    _geodataframe(box(-64, -34, -63.99, -33.99)).to_file(
        source,
        layer="sur",
        driver="GPKG",
        engine="pyogrio",
        mode="a",
    )

    with pytest.raises(VectorIngestionError, match="múltiples capas"):
        ingest_local_vector(source)

    result = ingest_local_vector(source, layer="sur")

    assert result.source_format == "GPKG"
    assert result.selected_layer == "sur"
    assert result.available_layers == ("norte", "sur")
    assert result.feature_count == 1


def test_multiple_features_require_an_explicit_dissolve(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partes.geojson"
    _geodataframe(
        box(-63, -33, -62.995, -32.99),
        box(-62.995, -33, -62.99, -32.99),
    ).to_file(source, driver="GeoJSON", engine="pyogrio")

    with pytest.raises(VectorIngestionError, match="--dissolve-all"):
        ingest_local_vector(source)

    result = ingest_local_vector(source, dissolve_all=True)

    assert result.feature_count == 2
    assert result.dissolved is True
    assert result.geometry.type == "Polygon"


def test_declared_crs_cannot_silently_override_embedded_crs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "territorio.gpkg"
    _geodataframe(
        box(500_000, 6_348_000, 501_000, 6_349_000),
        crs="EPSG:32720",
    ).to_file(source, layer="territorio", driver="GPKG", engine="pyogrio")

    with pytest.raises(VectorIngestionError, match="no coincide"):
        ingest_local_vector(source, declared_crs="EPSG:4326")


def test_source_crs_is_required_only_when_the_file_does_not_embed_one(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sin_crs.shp"
    with pytest.warns(UserWarning, match="'crs' was not provided"):
        _geodataframe(box(-63, -33, -62.99, -32.99), crs=None).to_file(
            source,
            driver="ESRI Shapefile",
            engine="pyogrio",
        )

    with pytest.raises(VectorIngestionError, match="--source-crs"):
        ingest_local_vector(source)

    result = ingest_local_vector(source, declared_crs="EPSG:4326")

    assert result.source_crs == "EPSG:4326"
    assert result.source_crs_origin == "declared"


def test_missing_or_non_vector_files_fail_before_pipeline_execution(
    tmp_path: Path,
) -> None:
    with pytest.raises(VectorIngestionError, match="no es un archivo"):
        ingest_local_vector(tmp_path / "inexistente.gpkg")

    text_file = tmp_path / "territorio.txt"
    text_file.write_text("esto no es un vector", encoding="utf-8")
    with pytest.raises(VectorIngestionError, match="no reconocido por GDAL"):
        ingest_local_vector(text_file)


def test_unknown_layer_and_non_polygonal_geometry_are_rejected(
    tmp_path: Path,
) -> None:
    polygon_source = tmp_path / "territorio.gpkg"
    _geodataframe(box(-63, -33, -62.99, -32.99)).to_file(
        polygon_source,
        layer="parcelas",
        driver="GPKG",
        engine="pyogrio",
    )
    with pytest.raises(VectorIngestionError, match="capa solicitada no existe"):
        ingest_local_vector(polygon_source, layer="otra")

    point_source = tmp_path / "punto.geojson"
    _geodataframe(Point(-63, -33)).to_file(
        point_source,
        driver="GeoJSON",
        engine="pyogrio",
    )
    with pytest.raises(VectorIngestionError, match="Polygon o MultiPolygon"):
        ingest_local_vector(point_source)
