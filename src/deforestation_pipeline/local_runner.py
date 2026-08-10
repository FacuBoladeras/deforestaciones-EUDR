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
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.area import AreaMeasurement, measure_area, select_projected_crs
from deforestation_pipeline.artifact_layout import (
    annual_figure,
    annual_json,
    annual_tiff,
    configuration_json,
    gee_json,
    geometry_json,
    input_json,
    published_raster_path,
    run_json,
    seasonal_figure,
    seasonal_json,
    seasonal_table,
)
from deforestation_pipeline.catalog import (
    BenchmarkSourcePlan,
    ForestBaselineSourcePlan,
    build_benchmark_source_plan,
    build_forest_baseline_source_plan,
    load_source_catalog,
)
from deforestation_pipeline.ccdc_benchmark import (
    CCDC_SCALAR_SUMMARY_BAND_NAMES,
    CcdcBenchmarkError,
    build_ccdc_remote_benchmark,
    build_ccdc_scalar_summary_image,
)
from deforestation_pipeline.change_detection import disturbance_detection_output_paths
from deforestation_pipeline.config import (
    ForestRandomForestConfig,
    execution_config_hash,
    load_config,
    load_forest_model_config,
    resolve_run_config,
    scientific_parameters_hash,
)
from deforestation_pipeline.disturbance_materialization import (
    DisturbanceCcdcProvenance,
    DisturbanceMaterialization,
    DisturbancePeriodRaster,
    ccdc_result_from_scalar_raster,
    materialize_disturbance_detection,
    read_baseline_domain_inputs,
    unavailable_ccdc_result,
)
from deforestation_pipeline.forest_baseline import forest_baseline_output_paths
from deforestation_pipeline.forest_baseline_features import (
    ForestBaselineFeatureProduct,
    build_forest_baseline_features,
)
from deforestation_pipeline.forest_baseline_materialization import (
    ForestBaselineMaterialization,
    materialize_forest_baseline,
)
from deforestation_pipeline.forest_baseline_products import (
    ForestBaselineImages,
    build_forest_baseline_images,
)
from deforestation_pipeline.forest_rf import ForestRfImages, build_forest_rf_images
from deforestation_pipeline.forest_rf_temporal import (
    FOREST_RF_TEMPORAL_BUNDLE_SCHEMA_VERSION,
    ForestRfTemporalMaterialization,
    forest_rf_temporal_output_paths,
    materialize_forest_rf_temporal,
)
from deforestation_pipeline.forest_screening import build_screening_temporal_domain
from deforestation_pipeline.gee import (
    GeeMetadataQuery,
    GeeMetadataQueryResult,
    authenticate_earth_engine,
    query_hls_scene_metadata,
)
from deforestation_pipeline.geometry import ValidatedGeometry, validate_geometry
from deforestation_pipeline.hls_composite import (
    HlsCompositeError,
    HlsCompositeImages,
    HlsCompositeRequest,
    build_hls_annual_composite,
)
from deforestation_pipeline.hls_seasonal import (
    HlsSeasonalMaterialization,
    HlsSeasonalRequest,
    materialize_hls_seasonal_series,
)
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.raster_products import (
    HlsRasterMaterialization,
    RasterDownloadError,
    materialize_ee_image_to_grid,
    materialize_hls_raster_products,
)
from deforestation_pipeline.rf_model_release import verify_local_rf_model_release
from deforestation_pipeline.schemas import DatasetRecord, RasterGridSpec
from deforestation_pipeline.seasonal_feature_stack import (
    build_yearly_seasonal_feature_stack,
)
from deforestation_pipeline.temporal_cube import build_meteorological_south_window_plan
from deforestation_pipeline.vector_ingestion import (
    ingest_local_vector,
    readable_vector_drivers,
)

LOCAL_BUNDLE_SCHEMA_VERSION = "3.1.0"
ATOMIC_PUBLISH_RETRY_DELAYS_SECONDS = (0.1, 0.2, 0.4, 0.8, 1.6)
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
    forest_model_config_path: Path | None = None,
    source_crs: str | None,
    establishment_id: str,
    analysis_end_date: date,
    created_at: datetime | None = None,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
    gee_query: GeeMetadataQuery | None = None,
    gee_credentials_path: Path | None = None,
    generate_hls_composite: bool = False,
    generate_forest_baseline: bool = False,
    generate_rf_deltas: bool = False,
    hls_seasonal_request: HlsSeasonalRequest | None = None,
    generate_disturbance_detection: bool = False,
    vector_layer: str | None = None,
    dissolve_all: bool = False,
) -> Path:
    """Ejecuta validación y medición local, y publica un paquete auditable."""
    run_created_at = created_at or datetime.now(UTC)
    if run_created_at.tzinfo is None or run_created_at.utcoffset() is None:
        raise ValueError("created_at debe incluir zona horaria")
    if not establishment_id.strip():
        raise LocalVectorInputError("establishment_id no puede estar vacío")

    ingestion = ingest_local_vector(
        input_path,
        declared_crs=source_crs,
        layer=vector_layer,
        dissolve_all=dissolve_all,
    )
    requested_analysis_end_date = analysis_end_date
    if hls_seasonal_request is not None:
        analysis_end_date = _seasonal_request_analysis_end_date(hls_seasonal_request)
        if requested_analysis_end_date < analysis_end_date:
            raise LocalVectorInputError(
                "requested_analysis_end_date_precedes_published_seasonal_range"
            )
    config = load_config(config_path)
    if forest_model_config_path is not None:
        config = config.model_copy(
            update={"forest_model": load_forest_model_config(forest_model_config_path)}
        )
    if generate_rf_deltas:
        analysis_end_date = _observation_years_analysis_end_date(
            config.forest_model.supported_observation_years
        )
        if requested_analysis_end_date < analysis_end_date:
            raise LocalVectorInputError("requested_analysis_end_date_precedes_rf_observation_range")
    resolved_config = resolve_run_config(config, analysis_end_date)
    catalog = load_source_catalog(catalog_path)
    source_plan = build_benchmark_source_plan(catalog, resolved_config)
    forest_source_plan = build_forest_baseline_source_plan(catalog, resolved_config)
    validated = validate_geometry(ingestion.geometry, ingestion.source_crs)
    measurement = measure_area(
        validated,
        resolved_config.spatial.area_crs_strategy,
    )
    gee_result: GeeMetadataQueryResult | None = None
    composite_product: HlsCompositeImages | None = None
    raster_materialization: HlsRasterMaterialization | None = None
    seasonal_materialization: HlsSeasonalMaterialization | None = None
    baseline_feature_product: ForestBaselineFeatureProduct | None = None
    baseline_images: ForestBaselineImages | None = None
    forest_rf_images: ForestRfImages | None = None
    forest_rf_temporal: ForestRfTemporalMaterialization | None = None
    baseline_materialization: ForestBaselineMaterialization | None = None
    disturbance_materialization: DisturbanceMaterialization | None = None
    disturbance_skip_reason: str | None = None
    support_materialization: HlsSeasonalMaterialization | None = None
    raster_grid: RasterGridSpec | None = None
    if generate_rf_deltas and forest_model_config_path is None:
        raise LocalVectorInputError(
            "la inferencia RF multianual requiere --forest-model-config explícito"
        )
    if forest_model_config_path is not None and not generate_rf_deltas:
        raise LocalVectorInputError(
            "--forest-model-config sólo se admite junto con la inferencia RF multianual"
        )
    if generate_rf_deltas and (
        gee_query is not None
        or generate_hls_composite
        or generate_forest_baseline
        or hls_seasonal_request is not None
        or generate_disturbance_detection
    ):
        raise LocalVectorInputError("la inferencia RF multianual es un modo exclusivo del MVP")
    if generate_rf_deltas and (
        resolved_config.forest_model.schema_version != "1.1.0"
        or resolved_config.forest_model.supported_observation_years
        != (2020, 2021, 2022, 2023, 2024)
    ):
        raise LocalVectorInputError(
            "la inferencia RF multianual requiere el contrato candidato 2020-2024"
        )
    if generate_rf_deltas and analysis_end_date != date(2024, 11, 30):
        raise LocalVectorInputError(
            "analysis_end_date efectiva del candidato debe cerrar en SON 2024 (2024-11-30)"
        )
    if generate_rf_deltas:
        verify_local_rf_model_release(
            resolved_config.forest_model,
            project_root=PROJECT_ROOT,
            load_bundle=False,
        )
    if generate_disturbance_detection and hls_seasonal_request is None:
        raise LocalVectorInputError(
            "la detección de perturbaciones requiere un rango estacional HLS"
        )
    if generate_disturbance_detection and not generate_forest_baseline:
        raise LocalVectorInputError(
            "la detección de perturbaciones requiere la línea base forestal 2020"
        )
    temporal_request = hls_seasonal_request is not None
    if temporal_request and (gee_query is not None or generate_hls_composite):
        raise LocalVectorInputError(
            "las series HLS no se combinan con la consulta o composite anual individual"
        )
    if generate_hls_composite and gee_query is None:
        raise LocalVectorInputError(
            "generate_hls_composite requiere una consulta GEE con fechas explícitas"
        )
    remote_requested = (
        gee_query is not None or temporal_request or generate_forest_baseline or generate_rf_deltas
    )
    if remote_requested:
        if gee_credentials_path is None:
            raise LocalVectorInputError(
                "gee_credentials_path es obligatorio cuando se solicita GEE"
            )
        gee_session = authenticate_earth_engine(gee_credentials_path)
    if gee_query is not None:
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
            raster_grid = derive_raster_grid_spec(
                aoi_wgs84=validated.analysis_geometry,
                target_crs=composite_request.target_crs,
                resolution_m=composite_request.scale_m,
                nodata=resolved_config.output.raster_nodata,
            )
            raster_materialization = materialize_hls_raster_products(
                product=composite_product,
                aoi_wgs84=validated.analysis_geometry,
                output_config=resolved_config.output,
                grid_spec=raster_grid,
            )
    if hls_seasonal_request is not None:
        raster_target_crs = select_projected_crs(
            validated,
            resolved_config.spatial.raster_crs_strategy,
        )
        raster_grid = derive_raster_grid_spec(
            aoi_wgs84=validated.analysis_geometry,
            target_crs=raster_target_crs,
            resolution_m=resolved_config.data.target_resolution_m,
            nodata=resolved_config.output.raster_nodata,
        )
        seasonal_materialization = materialize_hls_seasonal_series(
            session=gee_session,
            source_plan=source_plan,
            data_config=resolved_config.data,
            output_config=resolved_config.output,
            aoi_wgs84=validated.analysis_geometry,
            grid_spec=raster_grid,
            request=hls_seasonal_request,
            generated_at=run_created_at,
        )
    if generate_rf_deltas:
        raster_target_crs = select_projected_crs(
            validated,
            resolved_config.spatial.raster_crs_strategy,
        )
        raster_grid = derive_raster_grid_spec(
            aoi_wgs84=validated.analysis_geometry,
            target_crs=raster_target_crs,
            resolution_m=resolved_config.data.target_resolution_m,
            nodata=resolved_config.output.raster_nodata,
        )
        candidate_config = resolved_config.forest_model
        yearly_feature_stacks = {
            year: build_yearly_seasonal_feature_stack(
                session=gee_session,
                source_plan=source_plan,
                data_config=resolved_config.data,
                aoi_wgs84=validated.analysis_geometry,
                grid_spec=raster_grid,
                year=year,
                generated_at=run_created_at,
            )
            for year in candidate_config.supported_observation_years
        }
        yearly_rf_images = {
            year: build_forest_rf_images(
                module=gee_session.module,
                config=candidate_config,
                feature_stack=yearly_feature_stacks[year],
                observation_year=year,
                aoi_wgs84=validated.analysis_geometry,
                generated_at=run_created_at,
            )
            for year in candidate_config.supported_observation_years
        }
        p0_payload = candidate_config.model_dump(mode="python")
        p0_payload.update(
            {
                "schema_version": "1.0.0",
                "asset_id": candidate_config.comparison_asset_id,
                "model_training_scope": "p0_2020",
                "supported_observation_years": (2020,),
                "serialized_trees_sha256": None,
                "serialized_tree_multiset_sha256": None,
                "joblib_sha256": None,
                "model_registry_path": None,
                "model_registry_sha256": None,
                "inference_backend": "gee_decision_tree_ensemble",
                "local_joblib_path": None,
                "comparison_asset_id": None,
            }
        )
        p0_config = ForestRandomForestConfig.model_validate(p0_payload)
        p0_images = build_forest_rf_images(
            module=gee_session.module,
            config=p0_config,
            feature_stack=yearly_feature_stacks[2020],
            observation_year=2020,
            aoi_wgs84=validated.analysis_geometry,
            generated_at=run_created_at,
        )
        forest_rf_temporal = materialize_forest_rf_temporal(
            yearly_images=yearly_rf_images,
            yearly_feature_stacks=yearly_feature_stacks,
            p0_images=p0_images,
            config=candidate_config,
            output_config=resolved_config.output,
            grid_spec=raster_grid,
            generated_at=run_created_at,
        )
    if generate_forest_baseline:
        if raster_grid is None:
            raster_target_crs = select_projected_crs(
                validated,
                resolved_config.spatial.raster_crs_strategy,
            )
            raster_grid = derive_raster_grid_spec(
                aoi_wgs84=validated.analysis_geometry,
                target_crs=raster_target_crs,
                resolution_m=resolved_config.data.target_resolution_m,
                nodata=resolved_config.output.raster_nodata,
            )
        baseline_feature_product = build_forest_baseline_features(
            session=gee_session,
            hls_plan=source_plan,
            data_config=resolved_config.data,
            baseline_config=resolved_config.forest_baseline,
            aoi_wgs84=validated.analysis_geometry,
            target_crs=raster_grid.target_crs,
            generated_at=run_created_at,
        )
        baseline_images = build_forest_baseline_images(
            session=gee_session,
            source_plan=forest_source_plan,
            baseline_config=resolved_config.forest_baseline,
            feature_product=baseline_feature_product,
            aoi_wgs84=validated.analysis_geometry,
            generated_at=run_created_at,
        )
        rf_feature_stack = build_yearly_seasonal_feature_stack(
            session=gee_session,
            source_plan=source_plan,
            data_config=resolved_config.data,
            aoi_wgs84=validated.analysis_geometry,
            grid_spec=raster_grid,
            year=resolved_config.forest_model.reference_year,
            generated_at=run_created_at,
        )
        forest_rf_images = build_forest_rf_images(
            module=gee_session.module,
            config=resolved_config.forest_model,
            feature_stack=rf_feature_stack,
            aoi_wgs84=validated.analysis_geometry,
            generated_at=run_created_at,
        )
        baseline_materialization = materialize_forest_baseline(
            images=baseline_images,
            rf_images=forest_rf_images,
            feature_metadata=baseline_feature_product.metadata,
            source_plan=forest_source_plan,
            screening_config=resolved_config.forest_screening,
            output_config=resolved_config.output,
            grid_spec=raster_grid,
        )
    if generate_disturbance_detection:
        if (
            hls_seasonal_request is None
            or seasonal_materialization is None
            or baseline_materialization is None
            or raster_grid is None
        ):
            raise LocalVectorInputError("disturbance_prerequisites_unavailable")
        if (
            hls_seasonal_request.end_year
            < resolved_config.disturbance_detection.analysis_start_date.year
        ):
            disturbance_skip_reason = "requested_range_has_no_post_cutoff_period"
        else:
            support_end_year = min(hls_seasonal_request.start_year - 1, 2020)
            support_start_year = (
                resolved_config.disturbance_detection.reference_history_start_date.year
            )
            if support_end_year >= support_start_year:
                support_materialization = materialize_hls_seasonal_series(
                    session=gee_session,
                    source_plan=source_plan,
                    data_config=resolved_config.data,
                    output_config=resolved_config.output,
                    aoi_wgs84=validated.analysis_geometry,
                    grid_spec=raster_grid,
                    request=HlsSeasonalRequest(
                        start_year=support_start_year,
                        end_year=support_end_year,
                    ),
                    generated_at=run_created_at,
                )
            periods = _disturbance_period_rasters(
                published=seasonal_materialization,
                support=support_materialization,
            )
            baseline_paths = forest_baseline_output_paths()
            source_count, _, _, evidence_fraction = read_baseline_domain_inputs(
                files=baseline_materialization.files,
                paths=baseline_paths,
                grid_spec=raster_grid,
            )
            screening_domain = baseline_materialization.screening_domain
            domain = build_screening_temporal_domain(
                screening_domain,
            )
            last_closed_day = max(
                period.end_date_exclusive for period in periods if period.published
            )
            last_closed_day = date.fromordinal(last_closed_day.toordinal() - 1)
            ccdc_provenance = DisturbanceCcdcProvenance(
                status="scalar_summary_materialized",
            )
            try:
                ccdc_remote = build_ccdc_remote_benchmark(
                    session=gee_session,
                    plan=source_plan,
                    data_config=resolved_config.data,
                    aoi_wgs84=validated.analysis_geometry,
                    analysis_end_date=last_closed_day,
                    config=resolved_config.disturbance_detection,
                )
                ccdc_scalar_image = build_ccdc_scalar_summary_image(
                    session=gee_session,
                    remote=ccdc_remote,
                )
                ccdc_scalar = materialize_ee_image_to_grid(
                    image=ccdc_scalar_image,
                    band_names=CCDC_SCALAR_SUMMARY_BAND_NAMES,
                    artifact_path="internal/ccdc_scalar_summary.tif",
                    download_name="ccdc_scalar_summary",
                    output_config=resolved_config.output,
                    grid_spec=raster_grid,
                    output_type="float32",
                )
                ccdc_result = ccdc_result_from_scalar_raster(
                    content=ccdc_scalar.content,
                    domain=domain,
                    grid_spec=raster_grid,
                    config=resolved_config.disturbance_detection,
                )
            except (
                CcdcBenchmarkError,
                HlsCompositeError,
                RasterDownloadError,
            ) as error:
                ccdc_provenance = DisturbanceCcdcProvenance(
                    status="unavailable_global_failure",
                    failure_code=_sanitized_failure_code(error),
                )
                ccdc_result = unavailable_ccdc_result(
                    domain=domain,
                    index_count=len(
                        resolved_config.disturbance_detection.ccdc_benchmark.breakpoint_bands
                    ),
                )
            disturbance_materialization = materialize_disturbance_detection(
                periods=periods,
                domain=domain,
                screening_domain=screening_domain,
                baseline_evidence_fraction=evidence_fraction,
                spatial_footprint=np.isfinite(source_count),
                ccdc_result=ccdc_result,
                ccdc_provenance=ccdc_provenance,
                grid_spec=raster_grid,
                config=resolved_config.disturbance_detection,
                event_config=resolved_config.disturbance_events,
                scientific_parameters_hash=scientific_parameters_hash(resolved_config),
                requested_start_year=hls_seasonal_request.start_year,
                requested_end_year=hls_seasonal_request.end_year,
                generated_at=run_created_at,
                png_dpi=resolved_config.output.png_dpi,
            )

    input_sha256 = ingestion.input_sha256
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
                ingestion.source_crs,
                scientific_hash,
                catalog_sha256,
                _gee_query_identity(
                    gee_query,
                    generate_hls_composite,
                    hls_seasonal_request,
                    generate_forest_baseline,
                    generate_disturbance_detection,
                    generate_rf_deltas,
                ),
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
    remote_data_accessed = (
        gee_result is not None
        or seasonal_materialization is not None
        or baseline_materialization is not None
        or forest_rf_temporal is not None
    )
    pixel_data_accessed = (
        raster_materialization is not None
        or seasonal_materialization is not None
        or baseline_materialization is not None
        or forest_rf_temporal is not None
    )
    datasets = _dataset_records_for_query(
        source_plan=source_plan,
        access_date=run_created_at.date(),
        remote_data_accessed=remote_data_accessed,
        pixel_data_accessed=pixel_data_accessed,
    )
    if baseline_materialization is not None:
        datasets += _forest_dataset_records(
            source_plan=forest_source_plan,
            access_date=run_created_at.date(),
        )
    common_metadata = {
        "schema_version": (
            FOREST_RF_TEMPORAL_BUNDLE_SCHEMA_VERSION
            if forest_rf_temporal is not None
            else LOCAL_BUNDLE_SCHEMA_VERSION
        ),
        "run_id": run_id,
        "analysis_id": str(analysis_id),
        "establishment_id": establishment_id,
        "created_at": run_created_at.isoformat(),
        "analysis_end_date": analysis_end_date.isoformat(),
        "requested_analysis_end_date": requested_analysis_end_date.isoformat(),
        "input_sha256": input_sha256,
        "catalog_sha256": catalog_sha256,
        "scientific_parameters_hash": scientific_hash,
        "execution_config_hash": execution_hash,
        "remote_data_accessed": remote_data_accessed,
        "pixel_data_accessed": pixel_data_accessed,
    }
    if forest_rf_temporal is not None:
        access_limitation = (
            "Las clases RF anuales son evidencia aprendida experimental; los deltas "
            "no atribuyen causa ni uso posterior."
        )
    elif seasonal_materialization is not None:
        access_limitation = (
            "El cubo virtual contiene composites estacionales; cada período es una "
            "síntesis temporal y no una fecha de adquisición única."
        )
    elif baseline_materialization is not None:
        access_limitation = (
            "La línea base combina mapas forestales 2020 y atributos HLS "
            "pre-corte; ninguno constituye verdad de terreno."
        )
    elif pixel_data_accessed:
        access_limitation = (
            "El composite anual es una síntesis temporal y no representa una fecha "
            "de adquisición única."
        )
    elif remote_data_accessed:
        access_limitation = "Sólo se consultaron metadatos remotos; no se descargaron píxeles."
    else:
        access_limitation = "No se consultaron imágenes satelitales ni datasets remotos."
    limitations = [access_limitation]
    if baseline_materialization is None:
        limitations.append("No se construyó una línea base forestal 2020.")
    else:
        limitations.extend(
            [
                "La fracción forestal es convergencia no calibrada, no probabilidad.",
                "La línea base no detecta ni atribuye cambios posteriores.",
            ]
        )
    if disturbance_materialization is None:
        limitations.append("No se ejecutó detección de perturbaciones sobre un rango temporal.")
    else:
        limitations.extend(
            [
                "Se detectaron señales de perturbación sin atribuir su causa o uso posterior.",
                "Los scores robusto y CCDC conservan escalas independientes no calibradas.",
            ]
        )
    if forest_rf_temporal is not None:
        limitations.extend(
            [
                "La transferencia temporal del RF multianual no está validada independientemente.",
                "La fracción RAW de votos no es una probabilidad calibrada.",
                "Pérdida forestal candidata no equivale a deforestación.",
            ]
        )
    limitations.extend(
        [
            "No se ejecutó atribución de conversión ni se generó una conclusión final.",
            "No se determinó deforestación.",
            "Este resultado no determina cumplimiento EUDR.",
        ]
    )
    run_summary = {
        **common_metadata,
        "stage": (
            "forest_rf_deltas_2020_2024"
            if forest_rf_temporal is not None
            else (
                "disturbance_detection"
                if disturbance_materialization is not None
                else (
                    "forest_baseline_2020"
                    if baseline_materialization is not None
                    else (
                        "seasonal_temporal_cube"
                        if seasonal_materialization is not None
                        else (
                            "annual_hls_composite"
                            if pixel_data_accessed
                            else (
                                "scene_metadata_inventory"
                                if remote_data_accessed
                                else "spatial_preparation"
                            )
                        )
                    )
                )
            )
        ),
        "final_assessment_generated": False,
        "input": ingestion.provenance_payload(),
        "geometry": validation_payload,
        "area": area_payload,
        "catalog": {
            "source_plan_path": configuration_json("source_plan.json"),
            "products": [source.product for source in source_plan.sources],
            "forest_products": [source.product for source in forest_source_plan.sources],
            "remote_data_accessed": remote_data_accessed,
        },
        "datasets": [dataset.model_dump(mode="json") for dataset in datasets],
        "limitations": limitations,
    }
    if (
        baseline_images is not None
        and forest_rf_images is not None
        and baseline_materialization is not None
    ):
        baseline_paths = forest_baseline_output_paths()
        run_summary["forest_baseline"] = {
            **baseline_images.metadata.model_dump(mode="json"),
            "metadata_path": baseline_paths["metadata"],
            "qa_figure_path": baseline_paths["qa_figure"],
            "grid_sha256": baseline_materialization.grid_spec.grid_sha256,
            "assets": baseline_paths,
            "random_forest": forest_rf_images.metadata.model_dump(mode="json"),
            "screening": {
                "rules": resolved_config.forest_screening.model_dump(mode="json"),
                "metrics": (
                    baseline_materialization.screening_domain.metrics.model_dump(mode="json")
                ),
                "primary_interpretation_domain": "automated_forest",
                "final_assessment_generated": False,
            },
        }
    if forest_rf_temporal is not None:
        run_summary["status"] = "review_required"
        temporal_paths = forest_rf_temporal_output_paths(
            years=resolved_config.forest_model.supported_observation_years
        )
        run_summary["forest_rf_deltas"] = {
            "executed": True,
            "reference_year": 2020,
            "years": list(resolved_config.forest_model.supported_observation_years),
            "model": resolved_config.forest_model.model_dump(mode="json"),
            "grid_sha256": forest_rf_temporal.grid_spec.grid_sha256,
            "assets": temporal_paths,
            "per_pixel_observation_qa": {
                "band_count_per_year": 12,
                "years": {
                    str(year): temporal_paths[f"observation_qa_{year}"]
                    for year in resolved_config.forest_model.supported_observation_years
                },
                "metadata_path": temporal_paths["metadata"],
            },
            "yearly_summaries": list(forest_rf_temporal.yearly_summaries),
            "comparison_2020": dict(forest_rf_temporal.model_comparison_2020.summary),
            "scientific_status": "review_required",
            "temporal_transfer_validated": False,
            "gee_candidate_graph_diagnostic": {
                "point_reduce_region_succeeded": True,
                "fixed_grid_get_download_url_succeeded": False,
                "precise_limiting_component_isolated": False,
            },
            "attribution_generated": False,
            "final_assessment_generated": False,
        }
    disturbance_paths = disturbance_detection_output_paths()
    if disturbance_materialization is not None:
        run_summary["disturbance_detection"] = {
            "executed": True,
            "assets": disturbance_paths,
            "analysis_end_date": disturbance_materialization.analysis_end_date.isoformat(),
            "final_assessment_generated": False,
            "attribution_generated": False,
            "screening": disturbance_materialization.metadata["screening"],
            "persistent_events": {
                "metadata_path": "json/evidence/disturbance_events.json",
                "geojson_path": "json/evidence/disturbance_events.geojson",
                "table_path": "tables/evidence/disturbance_events.csv",
                "overview_figure_path": "figures/evidence/disturbance_events.png",
                "event_count": disturbance_materialization.events.event_count,
                "total_event_area_ha": (disturbance_materialization.events.total_event_area_ha),
                "area_threshold_event_count": (
                    disturbance_materialization.events.area_threshold_event_count
                ),
                "below_area_threshold_event_count": (
                    disturbance_materialization.events.below_area_threshold_event_count
                ),
                "automatic_final_assessment_generated": False,
                "attribution_generated": False,
            },
        }
    else:
        if disturbance_skip_reason is None and generate_forest_baseline:
            disturbance_skip_reason = (
                "single_year_mode_requires_range"
                if gee_query is not None
                else "seasonal_range_not_requested"
            )
        run_summary["disturbance_detection"] = {
            "executed": False,
            "skip_reason": disturbance_skip_reason or "not_requested",
            "final_assessment_generated": False,
            "attribution_generated": False,
        }
    if seasonal_materialization is not None:
        run_summary["gee"] = {
            "metadata_only": False,
            "mode": "seasonal_hls_temporal_cube",
            "start_year": seasonal_materialization.metadata.start_year,
            "end_year": seasonal_materialization.metadata.end_year,
            "period_count": len(seasonal_materialization.metadata.period_ids),
            "period_ids": list(seasonal_materialization.metadata.period_ids),
            "grid_sha256": seasonal_materialization.metadata.grid_sha256,
            "window_plan_sha256": (seasonal_materialization.metadata.window_plan_sha256),
            "cube_index_sha256": (seasonal_materialization.cube_index.cube_index_sha256),
            "window_plan_path": seasonal_json("window_plan.json"),
            "cube_spec_path": seasonal_json("cube_spec.json"),
            "cube_index_path": seasonal_json("cube_index.json"),
            "series_metadata_path": seasonal_json("series_metadata.json"),
            "coverage_path": seasonal_json("coverage.json"),
            "summary_table_path": seasonal_table("summary.csv"),
            "index_timeseries_path": seasonal_figure(None, "index_timeseries.png"),
            "observation_coverage_path": seasonal_figure(None, "observation_coverage.png"),
            "rgb_timeline_path": seasonal_figure(None, "rgb_timeline.png"),
        }
    elif gee_result is not None:
        gee_summary: dict[str, object] = {
            "query_result_path": gee_json("scene_metadata.json"),
            "metadata_only": not pixel_data_accessed,
            "start_date": gee_result.start_date.isoformat(),
            "end_date_exclusive": gee_result.end_date_exclusive.isoformat(),
        }
        if composite_product is not None:
            composite_year = composite_product.metadata.start_date.year
            gee_summary.update(
                {
                    "composite_metadata_path": annual_json(
                        "composite_metadata.json", year=composite_year
                    ),
                    "composite_input_inventory_path": annual_json(
                        "input_inventory.json", year=composite_year
                    ),
                    "reflectance_raster_path": annual_tiff(composite_year, "reflectance.tif"),
                    "indices_raster_path": annual_tiff(composite_year, "indices.tif"),
                    "valid_observation_count_raster_path": annual_tiff(
                        composite_year, "observation_count_total.tif"
                    ),
                    "l30_observation_count_raster_path": annual_tiff(
                        composite_year, "observation_count_l30.tif"
                    ),
                    "s30_observation_count_raster_path": annual_tiff(
                        composite_year, "observation_count_s30.tif"
                    ),
                    "raster_grid_path": annual_json("grid.json"),
                    "rgb_figure_path": annual_figure(composite_year, "rgb.png"),
                    "indices_panel_path": annual_figure(composite_year, "indices_panel.png"),
                }
            )
        run_summary["gee"] = gee_summary
    files = {
        input_json("converted.geojson"): ingestion.converted_geojson_bytes,
        input_json("ingestion.json"): _json_bytes(ingestion.provenance_payload()),
        configuration_json("source_plan.json"): _json_bytes(
            {
                "catalog_sha256": catalog_sha256,
                **source_plan.model_dump(mode="json"),
            }
        ),
        configuration_json("resolved.json"): _json_bytes(resolved_config.model_dump(mode="json")),
        geometry_json("original_interpreted.json"): _json_bytes(
            {
                "format": "shapely_mapping",
                "source_crs": validated.source_crs.to_string(),
                "geometry": mapping(validated.original_interpreted),
            }
        ),
        geometry_json("normalized_wgs84.geojson"): _geojson_bytes(
            validated.normalized_wgs84,
            representation="normalized_wgs84",
        ),
        geometry_json("analysis.geojson"): _geojson_bytes(
            validated.analysis_geometry,
            representation="analysis_geometry",
        ),
        geometry_json("validation.json"): _json_bytes(validation_payload),
        geometry_json("area.json"): _json_bytes(area_payload),
        run_json("environment.json"): _json_bytes(_environment_payload()),
        run_json("summary.json"): _json_bytes(run_summary),
    }
    files.update(ingestion.bundle_source_files())
    if gee_result is not None:
        files[gee_json("scene_metadata.json")] = _json_bytes(gee_result.model_dump(mode="json"))
    if composite_product is not None and raster_materialization is not None:
        composite_metadata = composite_product.metadata.model_dump(mode="json")
        source_inventory = composite_metadata.pop("sources")
        composite_year = composite_product.metadata.start_date.year
        inventory_path = annual_json("input_inventory.json", year=composite_year)
        files[inventory_path] = _json_bytes(
            {
                "complete_scene_inventory": True,
                "input_scene_count": composite_product.metadata.input_scene_count,
                "sources": source_inventory,
            }
        )
        files[annual_json("composite_metadata.json", year=composite_year)] = _json_bytes(
            {
                **composite_metadata,
                "input_inventory_path": inventory_path,
                "raster_materialization": _published_annual_raster_metadata(
                    raster_materialization, composite_year
                ),
            }
        )
        files[annual_json("grid.json")] = _json_bytes(
            raster_materialization.grid_spec.model_dump(mode="json")
        )
        files.update(
            {
                published_raster_path(
                    path, cadence="annual", period_id=str(composite_year)
                ): content
                for path, content in raster_materialization.files.items()
            }
        )
    if seasonal_materialization is not None:
        files.update(seasonal_materialization.files)
    if baseline_materialization is not None:
        files.update(baseline_materialization.files)
    if forest_rf_temporal is not None:
        files.update(forest_rf_temporal.files)
    if disturbance_materialization is not None:
        files.update(disturbance_materialization.files)
    _publish_bundle(
        run_directory=run_directory,
        files=files,
        manifest_metadata=common_metadata,
        remote_data_accessed=remote_data_accessed,
        datasets=tuple(dataset.model_dump(mode="json") for dataset in datasets),
    )
    return run_directory


def _inclusive_period_end_date(end_date_exclusive: date) -> date:
    return date.fromordinal(end_date_exclusive.toordinal() - 1)


def _seasonal_request_analysis_end_date(request: HlsSeasonalRequest) -> date:
    return _meteorological_range_analysis_end_date(
        start_year=request.start_year,
        end_year=request.end_year,
    )


def _observation_years_analysis_end_date(years: tuple[int, ...]) -> date:
    if not years or years != tuple(sorted(set(years))):
        raise ValueError("observation years debe ser único, no vacío y ordenado")
    return _meteorological_range_analysis_end_date(
        start_year=years[0],
        end_year=years[-1],
    )


def _meteorological_range_analysis_end_date(*, start_year: int, end_year: int) -> date:
    plan = build_meteorological_south_window_plan(
        start_year=start_year,
        end_year=end_year,
        generated_at=datetime(end_year + 1, 1, 1, tzinfo=UTC),
    )
    return max(_inclusive_period_end_date(window.end_date_exclusive) for window in plan.windows)


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


def _disturbance_period_rasters(
    *,
    published: HlsSeasonalMaterialization,
    support: HlsSeasonalMaterialization | None,
) -> tuple[DisturbancePeriodRaster, ...]:
    """Referencia rasters existentes sin publicar los artefactos de soporte."""
    result: list[DisturbancePeriodRaster] = []
    seen: set[str] = set()
    sources: list[tuple[HlsSeasonalMaterialization, bool]] = []
    if support is not None:
        sources.append((support, False))
    sources.append((published, True))
    for materialization, is_published in sources:
        for item in materialization.raster_items:
            period_id = item.window.period_id
            if period_id in seen:
                raise LocalVectorInputError("disturbance_period_overlap")
            seen.add(period_id)
            result.append(
                DisturbancePeriodRaster(
                    period_id=period_id,
                    season=item.window.season.value,
                    start_date=item.window.start_date,
                    end_date_exclusive=item.window.end_date_exclusive,
                    indices_raster=item.materialization.files["tiffs/hls_period_indices.tif"],
                    observation_count_raster=item.materialization.files[
                        "tiffs/hls_valid_observation_count.tif"
                    ],
                    published=is_published,
                )
            )
    return tuple(sorted(result, key=lambda item: (item.start_date, item.period_id)))


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


def _sanitized_failure_code(
    error: CcdcBenchmarkError | HlsCompositeError | RasterDownloadError,
) -> str:
    """Conserva sólo códigos internos estables; nunca mensajes o URLs remotas."""
    code = error.code
    if re.fullmatch(r"[a-z0-9_]{1,160}", code):
        return code
    return "unclassified_failure"


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _published_annual_raster_metadata(
    materialization: HlsRasterMaterialization,
    year: int,
) -> dict[str, object]:
    payload = materialization.metadata_payload()
    payload["raster_validations"] = [
        {
            **record.model_dump(mode="json"),
            "artifact_path": published_raster_path(
                record.artifact_path, cadence="annual", period_id=str(year)
            ),
        }
        for record in materialization.validations
    ]
    payload["visualizations"] = [
        {
            **record.model_dump(mode="json"),
            "path": published_raster_path(record.path, cadence="annual", period_id=str(year)),
            "source_raster": published_raster_path(
                record.source_raster, cadence="annual", period_id=str(year)
            ),
        }
        for record in materialization.visualizations
    ]
    return payload


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
        manifest_path = temporary_directory / run_json("manifest.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(_json_bytes(manifest))
        if run_directory.exists():
            raise FileExistsError(
                f"la carpeta de corrida ya existe y no se sobrescribe: {run_directory}"
            )
        _rename_directory_with_retry(temporary_directory, run_directory)
    except BaseException:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise


def _rename_directory_with_retry(
    source: Path,
    target: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Publica atómicamente tolerando bloqueos exclusivos transitorios de Windows."""
    for delay_seconds in (*ATOMIC_PUBLISH_RETRY_DELAYS_SECONDS, None):
        if target.exists():
            raise FileExistsError(f"la carpeta de corrida ya existe y no se sobrescribe: {target}")
        try:
            source.rename(target)
            return
        except PermissionError as error:
            is_windows_access_denied = getattr(error, "winerror", None) == 5
            if not is_windows_access_denied or delay_seconds is None:
                raise
            sleep(delay_seconds)


def _environment_payload() -> dict[str, object]:
    return {
        "python": sys.version.split()[0],
        "dependencies": {
            package: _package_version(package)
            for package in (
                "earthengine-api",
                "geopandas",
                "matplotlib",
                "numpy",
                "pydantic",
                "pyproj",
                "pyogrio",
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
    if path.suffix.lower() == ".csv":
        return "text/csv"
    return "application/json"


def _gee_query_identity(
    query: GeeMetadataQuery | None,
    generate_hls_composite: bool,
    seasonal_request: HlsSeasonalRequest | None,
    generate_forest_baseline: bool,
    generate_disturbance_detection: bool,
    generate_rf_deltas: bool,
) -> str:
    forest_suffix = ":forest_baseline=2020" if generate_forest_baseline else ""
    disturbance_suffix = ":disturbance_detection=14.6" if generate_disturbance_detection else ""
    rf_suffix = ":rf_deltas=multiyear_2020_2024_v1" if generate_rf_deltas else ""
    if seasonal_request is not None:
        return (
            f"gee:seasonal-series:{seasonal_request.start_year}:"
            f"{seasonal_request.end_year}:meteorological_south_v1"
            f"{forest_suffix}{disturbance_suffix}{rf_suffix}"
        )
    if query is None:
        return f"gee:not-requested{forest_suffix}{disturbance_suffix}{rf_suffix}"
    return (
        f"gee:{query.start_date.isoformat()}:{query.end_date.isoformat()}:"
        f"{query.max_scenes_per_source}:composite={generate_hls_composite}"
        f"{forest_suffix}{disturbance_suffix}{rf_suffix}"
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
                    "This run used raster pixels to build temporal composites."
                    if pixel_data_accessed
                    else "This run accessed metadata only; no raster pixels were downloaded."
                ),
            ],
        )
        for source in source_plan.sources
    )


def _forest_dataset_records(
    *,
    source_plan: ForestBaselineSourcePlan,
    access_date: date,
) -> tuple[DatasetRecord, ...]:
    return tuple(
        DatasetRecord(
            dataset_id=source.source_id,
            provider=source.provider,
            collection=source.collection_id,
            version=source.version,
            license=source.license,
            access_date=access_date,
            attribution=(f"Contains modified {source.product} data provided by {source.provider}."),
            restrictions=[
                *source.limitations,
                "Earth Engine platform terms and project registration apply separately.",
            ],
        )
        for source in source_plan.sources
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entrada CLI local de ``pruebas.py``."""
    parser = argparse.ArgumentParser(
        description=(
            "Convierte un vector local al contrato interno, valida su geometría "
            "y escribe un paquete auditable. Puede ejecutar todo lo disponible "
            "hasta la detección de perturbaciones del Paso 14."
        )
    )
    parser.add_argument(
        "vector",
        type=Path,
        nargs="?",
        help="archivo vectorial local legible por los drivers GDAL instalados",
    )
    parser.add_argument(
        "--list-vector-formats",
        action="store_true",
        help="lista los drivers vectoriales de lectura disponibles y termina",
    )
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
    parser.add_argument(
        "--source-crs",
        help="CRS sólo si falta en la fuente; si existe debe coincidir",
    )
    parser.add_argument(
        "--layer",
        help="capa a leer; es obligatoria cuando la fuente contiene varias",
    )
    parser.add_argument(
        "--dissolve-all",
        action="store_true",
        help="une explícitamente todas las entidades de la capa en un territorio",
    )
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
        "--full-pipeline",
        action="store_true",
        help=(
            "ejecuta línea base 2020 y, con un rango HLS, la serie estacional "
            "más la detección de perturbaciones"
        ),
    )
    parser.add_argument(
        "--rf-annual-deltas",
        action="store_true",
        help=(
            "ejecuta el RF multianual candidato 2020-2024 y deltas contra 2020; "
            "modo experimental exclusivo"
        ),
    )
    parser.add_argument(
        "--forest-model-config",
        type=Path,
        help="override YAML explícito del RF candidato; no modifica configs/default.yml",
    )
    parser.add_argument(
        "--seasonal",
        action="store_true",
        help="compatibilidad: los rangos de --full-pipeline ya son estacionales",
    )
    parser.add_argument(
        "--hls-year",
        type=int,
        help="un único año calendario cerrado para --full-pipeline",
    )
    parser.add_argument(
        "--hls-start-year",
        type=int,
        help="primer año cerrado del rango HLS inclusivo para --full-pipeline",
    )
    parser.add_argument(
        "--hls-end-year",
        type=int,
        help="último año cerrado del rango HLS inclusivo para --full-pipeline",
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
    if arguments.list_vector_formats:
        print("\n".join(readable_vector_drivers()))
        return 0
    if arguments.vector is None:
        parser.error("debe indicar un archivo vectorial")
    vector_path: Path = arguments.vector
    created_at = datetime.now(UTC)
    establishment_id = arguments.establishment_id or vector_path.stem
    analysis_end_date = arguments.analysis_end_date or created_at.date()
    gee_query = None
    generate_hls_composite = arguments.build_hls_composite
    generate_forest_baseline = False
    generate_rf_deltas = False
    hls_seasonal_request = None
    generate_disturbance_detection = False
    if arguments.rf_annual_deltas:
        if arguments.forest_model_config is None:
            parser.error("--rf-annual-deltas requiere --forest-model-config")
        if (
            arguments.full_pipeline
            or arguments.seasonal
            or arguments.query_gee
            or arguments.build_hls_composite
            or arguments.hls_year is not None
            or arguments.hls_start_year is not None
            or arguments.hls_end_year is not None
            or arguments.gee_start_date is not None
            or arguments.gee_end_date is not None
        ):
            parser.error("--rf-annual-deltas no se combina con otros modos GEE")
        generate_rf_deltas = True
    elif arguments.forest_model_config is not None:
        parser.error("--forest-model-config requiere --rf-annual-deltas")
    elif arguments.full_pipeline:
        generate_forest_baseline = True
        range_requested = arguments.hls_start_year is not None or arguments.hls_end_year is not None
        if arguments.hls_year is None and not range_requested:
            parser.error(
                "--full-pipeline requiere --hls-year o el rango completo "
                "--hls-start-year/--hls-end-year"
            )
        if arguments.hls_year is not None and range_requested:
            parser.error("--hls-year no se combina con un rango HLS")
        if arguments.seasonal and arguments.hls_year is not None:
            parser.error("--seasonal requiere un rango HLS, no --hls-year")
        if range_requested and (arguments.hls_start_year is None or arguments.hls_end_year is None):
            parser.error("el rango HLS requiere --hls-start-year y --hls-end-year")
        if (
            arguments.query_gee
            or arguments.build_hls_composite
            or arguments.gee_start_date is not None
            or arguments.gee_end_date is not None
        ):
            parser.error("--full-pipeline no se combina con flags GEE de bajo nivel")
        if arguments.hls_year is not None:
            if not 2015 <= arguments.hls_year < created_at.year:
                parser.error(
                    f"--hls-year debe ser un año cerrado entre 2015 y {created_at.year - 1}"
                )
            gee_query = GeeMetadataQuery(
                start_date=date(arguments.hls_year, 1, 1),
                end_date=date(arguments.hls_year + 1, 1, 1),
                max_scenes_per_source=arguments.max_scenes_per_source,
            )
            generate_hls_composite = True
        else:
            if not (2015 <= arguments.hls_start_year <= arguments.hls_end_year < created_at.year):
                parser.error(
                    "el rango HLS debe estar ordenado y contener años cerrados "
                    f"entre 2015 y {created_at.year - 1}"
                )
            hls_seasonal_request = HlsSeasonalRequest(
                start_year=arguments.hls_start_year,
                end_year=arguments.hls_end_year,
            )
            generate_disturbance_detection = True
    elif (
        arguments.hls_year is not None
        or arguments.hls_start_year is not None
        or arguments.hls_end_year is not None
    ):
        parser.error("--hls-year y los rangos HLS requieren --full-pipeline")
    elif arguments.seasonal:
        parser.error("--seasonal requiere --full-pipeline y un rango HLS")
    elif arguments.query_gee:
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
            input_path=vector_path,
            output_root=arguments.output_root,
            config_path=arguments.config,
            forest_model_config_path=arguments.forest_model_config,
            source_crs=arguments.source_crs,
            establishment_id=establishment_id,
            analysis_end_date=analysis_end_date,
            created_at=created_at,
            catalog_path=arguments.catalog,
            gee_query=gee_query,
            gee_credentials_path=(
                arguments.credentials
                if (
                    gee_query is not None
                    or hls_seasonal_request is not None
                    or generate_forest_baseline
                    or generate_rf_deltas
                )
                else None
            ),
            generate_hls_composite=generate_hls_composite,
            generate_forest_baseline=generate_forest_baseline,
            generate_rf_deltas=generate_rf_deltas,
            hls_seasonal_request=hls_seasonal_request,
            generate_disturbance_detection=generate_disturbance_detection,
            vector_layer=arguments.layer,
            dissolve_all=arguments.dissolve_all,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(2, f"Error: {error}\n")
    print(run_directory)
    return 0
