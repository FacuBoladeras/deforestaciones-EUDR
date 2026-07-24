"""Corte vertical local para inspeccionar las capacidades espaciales actuales."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.area import AreaMeasurement, measure_area, select_projected_crs
from deforestation_pipeline.catalog import (
    BenchmarkSourcePlan,
    build_benchmark_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.config import (
    execution_config_hash,
    load_config,
    resolve_run_config,
    scientific_parameters_hash,
)
from deforestation_pipeline.gee import (
    GeeMetadataQuery,
    GeeMetadataQueryResult,
    authenticate_earth_engine,
    query_hls_scene_metadata,
)
from deforestation_pipeline.geometry import ValidatedGeometry, validate_geometry
from deforestation_pipeline.hls_composite import (
    HlsCompositeImages,
    HlsCompositeRequest,
    build_hls_annual_composite,
)
from deforestation_pipeline.raster_products import (
    HlsRasterMaterialization,
    materialize_hls_raster_products,
)
from deforestation_pipeline.schemas import DatasetRecord, GeoJSONGeometry

LOCAL_BUNDLE_SCHEMA_VERSION = "1.1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "default.yml"
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.yml"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "local_tests"
DEFAULT_GEE_CREDENTIALS_PATH = PROJECT_ROOT / "credentials.json"


class LocalVectorInputError(ValueError):
    """Entrada local ilegible o incompatible con el corte vertical actual."""


def run_local_vector_pipeline(
    *,
    input_path: Path,
    output_root: Path,
    config_path: Path,
    source_crs: str,
    establishment_id: str,
    analysis_end_date: date,
    created_at: datetime | None = None,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
    gee_query: GeeMetadataQuery | None = None,
    gee_credentials_path: Path | None = None,
    generate_hls_composite: bool = False,
) -> Path:
    """Ejecuta validación y medición local, y publica un paquete auditable."""
    run_created_at = created_at or datetime.now(UTC)
    if run_created_at.tzinfo is None or run_created_at.utcoffset() is None:
        raise ValueError("created_at debe incluir zona horaria")
    if not establishment_id.strip():
        raise LocalVectorInputError("establishment_id no puede estar vacío")

    input_bytes, geometry_contract = _load_local_geojson(input_path)
    config = load_config(config_path)
    resolved_config = resolve_run_config(config, analysis_end_date)
    catalog = load_source_catalog(catalog_path)
    source_plan = build_benchmark_source_plan(catalog, resolved_config)
    validated = validate_geometry(geometry_contract, source_crs)
    measurement = measure_area(
        validated,
        resolved_config.spatial.area_crs_strategy,
    )
    gee_result: GeeMetadataQueryResult | None = None
    composite_product: HlsCompositeImages | None = None
    raster_materialization: HlsRasterMaterialization | None = None
    if generate_hls_composite and gee_query is None:
        raise LocalVectorInputError(
            "generate_hls_composite requiere una consulta GEE con fechas explícitas"
        )
    if gee_query is not None:
        if gee_credentials_path is None:
            raise LocalVectorInputError(
                "gee_credentials_path es obligatorio cuando se solicita GEE"
            )
        gee_session = authenticate_earth_engine(gee_credentials_path)
        gee_result = query_hls_scene_metadata(
            session=gee_session,
            plan=source_plan,
            aoi_wgs84=validated.analysis_geometry,
            query=gee_query,
            queried_at=run_created_at,
        )
        if generate_hls_composite:
            raster_target_crs = select_projected_crs(
                validated,
                resolved_config.spatial.raster_crs_strategy,
            )
            composite_request = HlsCompositeRequest(
                start_date=gee_query.start_date,
                end_date_exclusive=gee_query.end_date,
                target_crs=raster_target_crs,
                scale_m=resolved_config.data.target_resolution_m,
            )
            composite_product = build_hls_annual_composite(
                session=gee_session,
                plan=source_plan,
                data_config=resolved_config.data,
                aoi_wgs84=validated.analysis_geometry,
                request=composite_request,
                generated_at=run_created_at,
            )
            raster_materialization = materialize_hls_raster_products(
                product=composite_product,
                aoi_wgs84=validated.analysis_geometry,
                output_config=resolved_config.output,
                target_crs=composite_request.target_crs,
                scale_m=composite_request.scale_m,
            )

    input_sha256 = _sha256(input_bytes)
    catalog_sha256 = _sha256(catalog_path.read_bytes())
    scientific_hash = scientific_parameters_hash(resolved_config)
    execution_hash = execution_config_hash(resolved_config)
    analysis_id = uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "deforestation-pipeline:local-vector",
                establishment_id,
                input_sha256,
                source_crs,
                scientific_hash,
                catalog_sha256,
                _gee_query_identity(gee_query, generate_hls_composite),
            )
        ),
    )
    run_id = _run_identifier(establishment_id, run_created_at, str(analysis_id))
    run_directory = output_root.resolve() / run_id
    if run_directory.exists():
        raise FileExistsError(
            f"la carpeta de corrida ya existe y no se sobrescribe: {run_directory}"
        )

    area_payload = _area_payload(measurement)
    validation_payload = _validation_payload(validated)
    remote_data_accessed = gee_result is not None
    pixel_data_accessed = raster_materialization is not None
    datasets = _dataset_records_for_query(
        source_plan=source_plan,
        access_date=run_created_at.date(),
        remote_data_accessed=remote_data_accessed,
        pixel_data_accessed=pixel_data_accessed,
    )
    common_metadata = {
        "schema_version": LOCAL_BUNDLE_SCHEMA_VERSION,
        "run_id": run_id,
        "analysis_id": str(analysis_id),
        "establishment_id": establishment_id,
        "created_at": run_created_at.isoformat(),
        "analysis_end_date": analysis_end_date.isoformat(),
        "input_sha256": input_sha256,
        "catalog_sha256": catalog_sha256,
        "scientific_parameters_hash": scientific_hash,
        "execution_config_hash": execution_hash,
        "remote_data_accessed": remote_data_accessed,
        "pixel_data_accessed": pixel_data_accessed,
    }
    if pixel_data_accessed:
        access_limitation = (
            "El composite anual es una síntesis temporal y no representa una fecha "
            "de adquisición única."
        )
    elif remote_data_accessed:
        access_limitation = "Sólo se consultaron metadatos remotos; no se descargaron píxeles."
    else:
        access_limitation = "No se consultaron imágenes satelitales ni datasets remotos."
    limitations = [
        access_limitation,
        "No se construyó una línea base forestal 2020.",
        "No se ejecutó lógica de deforestación ni se detectaron o atribuyeron cambios.",
        "Este resultado no determina cumplimiento EUDR.",
    ]
    run_summary = {
        **common_metadata,
        "stage": (
            "annual_hls_composite"
            if pixel_data_accessed
            else ("scene_metadata_inventory" if remote_data_accessed else "spatial_preparation")
        ),
        "final_assessment_generated": False,
        "geometry": validation_payload,
        "area": area_payload,
        "catalog": {
            "source_plan_path": "catalog/source_plan.json",
            "products": [source.product for source in source_plan.sources],
            "remote_data_accessed": remote_data_accessed,
        },
        "datasets": [dataset.model_dump(mode="json") for dataset in datasets],
        "limitations": limitations,
    }
    if gee_result is not None:
        gee_summary: dict[str, object] = {
            "query_result_path": "gee/scene_metadata.json",
            "metadata_only": not pixel_data_accessed,
            "start_date": gee_result.start_date.isoformat(),
            "end_date_exclusive": gee_result.end_date_exclusive.isoformat(),
        }
        if composite_product is not None:
            gee_summary.update(
                {
                    "composite_metadata_path": "gee/composite_metadata.json",
                    "composite_input_inventory_path": ("gee/composite_input_inventory.json"),
                    "reflectance_raster_path": ("rasters/hls_annual_reflectance.tif"),
                    "indices_raster_path": "rasters/hls_annual_indices.tif",
                    "valid_observation_count_raster_path": (
                        "rasters/hls_valid_observation_count.tif"
                    ),
                    "rgb_figure_path": "figures/rgb.png",
                    "indices_panel_path": "figures/indices_panel.png",
                }
            )
        run_summary["gee"] = gee_summary
    files = {
        "input/source.geojson": input_bytes,
        "catalog/source_plan.json": _json_bytes(
            {
                "catalog_sha256": catalog_sha256,
                **source_plan.model_dump(mode="json"),
            }
        ),
        "config/resolved_config.json": _json_bytes(resolved_config.model_dump(mode="json")),
        "geometry/original_interpreted.json": _json_bytes(
            {
                "format": "shapely_mapping",
                "source_crs": validated.source_crs.to_string(),
                "geometry": mapping(validated.original_interpreted),
            }
        ),
        "geometry/normalized_wgs84.geojson": _geojson_bytes(
            validated.normalized_wgs84,
            representation="normalized_wgs84",
        ),
        "geometry/analysis_geometry.geojson": _geojson_bytes(
            validated.analysis_geometry,
            representation="analysis_geometry",
        ),
        "geometry/validation.json": _json_bytes(validation_payload),
        "measurements/area.json": _json_bytes(area_payload),
        "environment.json": _json_bytes(_environment_payload()),
        "run_summary.json": _json_bytes(run_summary),
    }
    if gee_result is not None:
        files["gee/scene_metadata.json"] = _json_bytes(gee_result.model_dump(mode="json"))
    if composite_product is not None and raster_materialization is not None:
        composite_metadata = composite_product.metadata.model_dump(mode="json")
        source_inventory = composite_metadata.pop("sources")
        files["gee/composite_input_inventory.json"] = _json_bytes(
            {
                "complete_scene_inventory": True,
                "input_scene_count": composite_product.metadata.input_scene_count,
                "sources": source_inventory,
            }
        )
        files["gee/composite_metadata.json"] = _json_bytes(
            {
                **composite_metadata,
                "input_inventory_path": "gee/composite_input_inventory.json",
                "raster_materialization": raster_materialization.metadata_payload(),
            }
        )
        files.update(raster_materialization.files)
    _publish_bundle(
        run_directory=run_directory,
        files=files,
        manifest_metadata=common_metadata,
        remote_data_accessed=remote_data_accessed,
        datasets=tuple(dataset.model_dump(mode="json") for dataset in datasets),
    )
    return run_directory


def _load_local_geojson(path: Path) -> tuple[bytes, GeoJSONGeometry]:
    if path.suffix.lower() not in {".geojson", ".json"}:
        raise LocalVectorInputError("el runner local admite únicamente GeoJSON (.geojson o .json)")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise LocalVectorInputError(f"no se pudo leer {path}: {error}") from error
    try:
        document: object = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LocalVectorInputError(f"GeoJSON inválido en {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise LocalVectorInputError("el nodo raíz del GeoJSON debe ser un objeto")

    geometry_payload = _extract_geometry_payload(document)
    try:
        geometry = GeoJSONGeometry.model_validate(geometry_payload)
    except ValidationError as error:
        raise LocalVectorInputError(f"geometría GeoJSON incompatible: {error}") from error
    return content, geometry


def _extract_geometry_payload(document: Mapping[str, object]) -> object:
    document_type = document.get("type")
    if document_type == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list) or len(features) != 1:
            raise LocalVectorInputError(
                "FeatureCollection debe contener exactamente una Feature por corrida"
            )
        feature = features[0]
        if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
            raise LocalVectorInputError("features[0] debe ser una Feature GeoJSON")
        return _feature_geometry(feature)
    if document_type == "Feature":
        return _feature_geometry(document)
    return document


def _feature_geometry(feature: Mapping[str, object]) -> object:
    geometry = feature.get("geometry")
    if not isinstance(geometry, Mapping):
        raise LocalVectorInputError("la Feature debe contener una geometría no nula")
    return geometry


def _area_payload(measurement: AreaMeasurement) -> dict[str, Any]:
    return {
        "total_area_ha": measurement.total_area_ha,
        "component_areas_ha": measurement.component_areas_ha,
        "calculation_crs": measurement.calculation_crs,
        "strategy": measurement.strategy.value,
        "threshold_relation": measurement.threshold_relation.value,
        "threshold_ha": measurement.threshold_ha,
        "numeric_tolerance_ha": measurement.numeric_tolerance_ha,
        "method": measurement.method,
        "transform_always_xy": measurement.transform_always_xy,
        "source_geometry_field": measurement.source_geometry_field,
        "utm_zone": measurement.utm_zone,
        "utm_hemisphere": measurement.utm_hemisphere,
    }


def _validation_payload(validated: ValidatedGeometry) -> dict[str, Any]:
    return {
        "source_crs": validated.source_crs.to_string(),
        "normalized_crs": "EPSG:4326",
        "original_geometry_type": validated.original_interpreted.geom_type,
        "analysis_geometry_type": validated.analysis_geometry.geom_type,
        "repair_performed": validated.repair_performed,
        "warnings": validated.warnings,
        "quality_flags": tuple(flag.value for flag in validated.quality_flags),
    }


def _geojson_bytes(geometry: BaseGeometry, *, representation: str) -> bytes:
    return _json_bytes(
        {
            "type": "Feature",
            "properties": {
                "representation": representation,
                "crs": "EPSG:4326",
            },
            "geometry": mapping(geometry),
        }
    )


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _publish_bundle(
    *,
    run_directory: Path,
    files: Mapping[str, bytes],
    manifest_metadata: Mapping[str, object],
    remote_data_accessed: bool,
    datasets: tuple[Mapping[str, object], ...],
) -> None:
    output_root = run_directory.parent
    output_root.mkdir(parents=True, exist_ok=True)
    temporary_directory = Path(tempfile.mkdtemp(prefix=f".{run_directory.name}.", dir=output_root))
    try:
        for relative_path, content in files.items():
            destination = temporary_directory / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)

        artifacts = []
        for path in sorted(item for item in temporary_directory.rglob("*") if item.is_file()):
            relative_path = path.relative_to(temporary_directory).as_posix()
            artifacts.append(
                {
                    "path": relative_path,
                    "sha256": _sha256(path.read_bytes()),
                    "size_bytes": path.stat().st_size,
                    "media_type": _media_type(path),
                }
            )
        manifest = {
            **manifest_metadata,
            "manifest_self_excluded": True,
            "remote_data_accessed": remote_data_accessed,
            "datasets": datasets,
            "artifacts": artifacts,
        }
        (temporary_directory / "manifest.json").write_bytes(_json_bytes(manifest))
        if run_directory.exists():
            raise FileExistsError(
                f"la carpeta de corrida ya existe y no se sobrescribe: {run_directory}"
            )
        temporary_directory.rename(run_directory)
    except BaseException:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise


def _environment_payload() -> dict[str, object]:
    return {
        "python": sys.version.split()[0],
        "dependencies": {
            package: _package_version(package)
            for package in (
                "earthengine-api",
                "matplotlib",
                "numpy",
                "pydantic",
                "pyproj",
                "PyYAML",
                "rasterio",
                "shapely",
            )
        },
        "code": _git_metadata(),
    }


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "not-installed"


def _git_metadata() -> dict[str, object]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "working_tree_dirty": None}
    return {"commit": commit, "working_tree_dirty": bool(status)}


def _run_identifier(establishment_id: str, created_at: datetime, analysis_id: str) -> str:
    normalized = unicodedata.normalize("NFKD", establishment_id)
    ascii_identifier = normalized.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", ascii_identifier).strip("-_")
    if not slug:
        slug = "establishment"
    timestamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{slug}__{timestamp}__{analysis_id[:12]}"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _media_type(path: Path) -> str:
    if path.suffix.lower() == ".geojson":
        return "application/geo+json"
    if path.suffix.lower() in {".tif", ".tiff"}:
        return "image/tiff"
    if path.suffix.lower() == ".png":
        return "image/png"
    return "application/json"


def _gee_query_identity(
    query: GeeMetadataQuery | None,
    generate_hls_composite: bool,
) -> str:
    if query is None:
        return "gee:not-requested"
    return (
        f"gee:{query.start_date.isoformat()}:{query.end_date.isoformat()}:"
        f"{query.max_scenes_per_source}:composite={generate_hls_composite}"
    )


def _dataset_records_for_query(
    *,
    source_plan: BenchmarkSourcePlan,
    access_date: date,
    remote_data_accessed: bool,
    pixel_data_accessed: bool,
) -> tuple[DatasetRecord, ...]:
    if not remote_data_accessed:
        return ()
    return tuple(
        DatasetRecord(
            dataset_id=source.source_id,
            provider=source.provider,
            collection=source.collection_id,
            version=source.version,
            license="NASA full and open data sharing",
            access_date=access_date,
            attribution=(
                f"Contains modified HLS {source.product} v{source.version} data "
                "provided by NASA LP DAAC."
            ),
            restrictions=[
                "Earth Engine platform terms and project registration apply separately.",
                (
                    "This run used raster pixels to build an annual composite."
                    if pixel_data_accessed
                    else "This run accessed metadata only; no raster pixels were downloaded."
                ),
            ],
        )
        for source in source_plan.sources
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entrada CLI local de ``pruebas.py``."""
    parser = argparse.ArgumentParser(
        description=(
            "Valida un GeoJSON local, mide su superficie y escribe un paquete "
            "auditable. El acceso GEE y la descarga raster son explícitos."
        )
    )
    parser.add_argument("vector", type=Path, help="GeoJSON local con una geometría")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"carpeta raíz de corridas (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"configuración YAML (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG_PATH,
        help=f"catálogo local YAML (default: {DEFAULT_CATALOG_PATH})",
    )
    parser.add_argument("--source-crs", default="EPSG:4326", help="CRS declarado del vector")
    parser.add_argument(
        "--establishment-id",
        help="identificador; por defecto se usa el nombre del archivo",
    )
    parser.add_argument(
        "--analysis-end-date",
        type=date.fromisoformat,
        help="fecha final YYYY-MM-DD; por defecto se usa la fecha UTC actual",
    )
    parser.add_argument(
        "--query-gee",
        action="store_true",
        help="consulta metadatos HLS en GEE; no descarga píxeles",
    )
    parser.add_argument(
        "--build-hls-composite",
        action="store_true",
        help=(
            "construye un composite HLS de año calendario, descarga GeoTIFF "
            "pequeños y genera PNG locales"
        ),
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_GEE_CREDENTIALS_PATH,
        help="credencial de cuenta de servicio local",
    )
    parser.add_argument(
        "--gee-start-date",
        type=date.fromisoformat,
        help="inicio inclusivo de la consulta GEE",
    )
    parser.add_argument(
        "--gee-end-date",
        type=date.fromisoformat,
        help="fin exclusivo de la consulta GEE",
    )
    parser.add_argument(
        "--max-scenes-per-source",
        type=int,
        default=25,
        help="máximo de escenas devueltas por colección (1..50)",
    )
    arguments = parser.parse_args(argv)
    created_at = datetime.now(UTC)
    establishment_id = arguments.establishment_id or arguments.vector.stem
    analysis_end_date = arguments.analysis_end_date or created_at.date()
    gee_query = None
    if arguments.query_gee:
        if arguments.gee_start_date is None or arguments.gee_end_date is None:
            parser.error("--query-gee requiere --gee-start-date y --gee-end-date")
        gee_query = GeeMetadataQuery(
            start_date=arguments.gee_start_date,
            end_date=arguments.gee_end_date,
            max_scenes_per_source=arguments.max_scenes_per_source,
        )
    elif arguments.gee_start_date is not None or arguments.gee_end_date is not None:
        parser.error("las fechas GEE requieren --query-gee")
    if arguments.build_hls_composite and not arguments.query_gee:
        parser.error("--build-hls-composite requiere --query-gee")
    try:
        run_directory = run_local_vector_pipeline(
            input_path=arguments.vector,
            output_root=arguments.output_root,
            config_path=arguments.config,
            source_crs=arguments.source_crs,
            establishment_id=establishment_id,
            analysis_end_date=analysis_end_date,
            created_at=created_at,
            catalog_path=arguments.catalog,
            gee_query=gee_query,
            gee_credentials_path=(arguments.credentials if arguments.query_gee else None),
            generate_hls_composite=arguments.build_hls_composite,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(2, f"Error: {error}\n")
    print(run_directory)
    return 0
