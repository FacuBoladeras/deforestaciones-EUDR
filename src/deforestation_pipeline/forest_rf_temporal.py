"""Materialización anual y deltas RF contra 2020, sin atribución de conversión."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Final, Literal, cast

import matplotlib
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from deforestation_pipeline.artifact_layout import (
    evidence_figure,
    evidence_json,
    evidence_table,
    evidence_tiff,
)
from deforestation_pipeline.config import ForestRandomForestConfig, OutputConfig
from deforestation_pipeline.forest_rf import (
    ForestRfImages,
    canonicalize_forest_rf_feature_stack,
    forest_rf_output_band_names,
    forest_rf_predictor_band_names,
    forest_rf_predictor_columns_sha256,
    select_forest_rf_predictors,
)
from deforestation_pipeline.raster_products import (
    DirectDownloadEstimate,
    FetchBytes,
    RasterValidation,
    materialize_ee_image_to_grid,
)
from deforestation_pipeline.rf_model_release import verify_local_rf_model_release
from deforestation_pipeline.schemas import RasterGridSpec
from deforestation_pipeline.seasonal_feature_stack import (
    SEASONAL_FEATURE_SEASONS,
    SEASONAL_QA_COUNT_BANDS,
    SeasonalFeatureStack,
)

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

FOREST_RF_TEMPORAL_SCHEMA_VERSION: Final = "1.1.0"
FOREST_RF_TEMPORAL_BUNDLE_SCHEMA_VERSION: Final = "3.4.0"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


class ForestTransitionCode(IntEnum):
    """Transición candidata de clase RF; no equivale a deforestación."""

    INSUFFICIENT_DATA = 0
    STABLE_NONFOREST = 1
    CANDIDATE_FOREST_GAIN = 2
    CANDIDATE_FOREST_LOSS = 3
    STABLE_FOREST = 4


_TRANSITION_LABELS: Final = {
    ForestTransitionCode.INSUFFICIENT_DATA: "insufficient_data",
    ForestTransitionCode.STABLE_NONFOREST: "stable_nonforest",
    ForestTransitionCode.CANDIDATE_FOREST_GAIN: "candidate_forest_gain",
    ForestTransitionCode.CANDIDATE_FOREST_LOSS: "candidate_forest_loss",
    ForestTransitionCode.STABLE_FOREST: "stable_forest",
}


@dataclass(frozen=True, slots=True)
class ForestModelComparison:
    class_disagreement: np.ndarray
    vote_delta: np.ndarray
    summary: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LocalForestPrediction:
    forest_class: np.ndarray
    vote_fraction: np.ndarray


@dataclass(frozen=True, slots=True)
class ForestRfTemporalMaterialization:
    files: Mapping[str, bytes]
    estimates: tuple[DirectDownloadEstimate, ...]
    validations: tuple[RasterValidation, ...]
    grid_spec: RasterGridSpec
    yearly_summaries: tuple[Mapping[str, Any], ...]
    model_comparison_2020: ForestModelComparison


def forest_rf_temporal_output_paths(*, years: tuple[int, ...]) -> dict[str, str]:
    """Contrato plano dentro de los roots publicados ya existentes."""
    if not years or years != tuple(sorted(set(years))) or years[0] != 2020:
        raise ValueError("years debe ser único, ordenado y comenzar en 2020")
    paths: dict[str, str] = {}
    for year in years:
        paths[f"predictors_{year}"] = evidence_tiff(f"rf_predictor_stack_{year}.tif")
        paths[f"observation_qa_{year}"] = evidence_tiff(f"rf_observation_qa_{year}.tif")
        paths[f"class_{year}"] = evidence_tiff(f"rf_forest_class_{year}.tif")
        paths[f"vote_{year}"] = evidence_tiff(f"rf_forest_vote_fraction_{year}.tif")
        paths[f"complete_{year}"] = evidence_tiff(f"rf_forest_input_complete_{year}.tif")
        if year != 2020:
            paths[f"delta_{year}"] = evidence_tiff(f"rf_forest_vote_delta_{year}_vs_2020.tif")
            paths[f"transition_{year}"] = evidence_tiff(f"rf_forest_transition_{year}_vs_2020.tif")
    paths.update(
        {
            "p0_class_2020": evidence_tiff("rf_forest_p0_class_2020.tif"),
            "p0_vote_2020": evidence_tiff("rf_forest_p0_vote_fraction_2020.tif"),
            "p0_complete_2020": evidence_tiff("rf_forest_p0_input_complete_2020.tif"),
            "comparison_disagreement": evidence_tiff(
                "rf_model_class_disagreement_p0_vs_multiyear_2020.tif"
            ),
            "comparison_vote_delta": evidence_tiff(
                "rf_model_vote_delta_multiyear_minus_p0_2020.tif"
            ),
            "summary_csv": evidence_table("rf_forest_deltas_2020_2024.csv"),
            "summary_json": evidence_json("rf_forest_deltas_2020_2024.json"),
            "metadata": evidence_json("rf_forest_deltas_metadata.json"),
            "comparison_json": evidence_json("rf_model_comparison_2020.json"),
            "overview_figure": evidence_figure("rf_forest_deltas_2020_2024.png"),
            "comparison_figure": evidence_figure("rf_model_comparison_2020.png"),
        }
    )
    if len(paths) != len(set(paths.values())):
        raise RuntimeError("el contrato temporal RF contiene rutas duplicadas")
    return paths


def forest_rf_per_pixel_qa_band_names(year: int) -> tuple[str, ...]:
    """Contrato de QA espacial auditable: estación x total/L30/S30."""
    if year < 1980 or year > 2100:
        raise ValueError("year fuera del rango soportado")
    return tuple(
        f"{year}_{season}_{band}"
        for season in SEASONAL_FEATURE_SEASONS
        for band in SEASONAL_QA_COUNT_BANDS
    )


def predict_forest_rf_locally(
    *,
    predictor_cube: np.ndarray,
    predictor_band_names: tuple[str, ...],
    input_complete: np.ndarray,
    aoi_footprint: np.ndarray,
    model_bundle: Mapping[str, Any],
    nodata: float,
) -> LocalForestPrediction:
    """Aplica el joblib sklearn exacto sólo sobre píxeles completamente evaluables."""
    expected_names = forest_rf_predictor_band_names()
    if predictor_band_names != expected_names:
        raise ValueError("predictor_band_names no coincide con el contrato RF")
    cube = np.asarray(predictor_cube)
    complete = np.asarray(input_complete)
    aoi = np.asarray(aoi_footprint, dtype=bool)
    if cube.ndim != 3 or cube.shape[0] != len(expected_names):
        raise ValueError("predictor_cube debe tener shape (56, height, width)")
    if complete.shape != cube.shape[1:] or aoi.shape != cube.shape[1:]:
        raise ValueError("predictor_cube, input_complete y aoi_footprint no comparten grilla")
    if np.any((complete == 1) & ~aoi):
        raise ValueError("input_complete evaluable debe estar contenido en el AOI")
    supported = aoi & (complete == 1)
    values = np.moveaxis(cube, 0, -1)[supported]
    if not np.all(np.isfinite(values)) or np.any(values == nodata):
        raise ValueError("los predictores evaluables contienen nodata o no finitos")

    model = model_bundle.get("model")
    bundle_names = tuple(model_bundle.get("feature_columns", ()))
    if model is None or bundle_names != expected_names:
        raise ValueError("el joblib no contiene el modelo y orden de features esperados")
    if model_bundle.get("label_to_int") != {"non_forest": 0, "forest": 1}:
        raise ValueError("label_to_int del joblib no coincide con el contrato binario")
    if model_bundle.get("positive_class") != "forest":
        raise ValueError("positive_class del joblib debe ser forest")
    classes = np.asarray(getattr(model, "classes_", ()))
    if not np.array_equal(classes, np.asarray([0, 1])):
        raise ValueError("classes_ del modelo debe ser [0, 1]")
    model_feature_names = tuple(getattr(model, "feature_names_in_", ()))
    if model_feature_names and model_feature_names != expected_names:
        raise ValueError("feature_names_in_ del modelo no coincide con el contrato")

    forest_class = np.full(complete.shape, nodata, dtype=np.int16)
    vote_fraction = np.full(complete.shape, nodata, dtype=np.float32)
    if values.shape[0] == 0:
        return LocalForestPrediction(
            forest_class=forest_class,
            vote_fraction=vote_fraction,
        )
    named_values: Any = (
        pd.DataFrame(values, columns=expected_names) if model_feature_names else values
    )
    predicted = np.asarray(model.predict(named_values))
    if predicted.shape != (values.shape[0],) or not np.all(np.isin(predicted, (0, 1))):
        raise ValueError("predict devolvió clases incompatibles")
    estimators = tuple(getattr(model, "estimators_", ()))
    if not estimators:
        raise ValueError("el modelo no contiene árboles para derivar votos RAW")
    tree_votes = np.empty((len(estimators), values.shape[0]), dtype=np.float64)
    for index, estimator in enumerate(estimators):
        votes = np.asarray(estimator.predict(values))
        if votes.shape != (values.shape[0],) or not np.all(np.isin(votes, (0, 1))):
            raise ValueError("los árboles deben devolver votos binarios con shape compatible")
        tree_votes[index] = votes
    hard_vote_fraction = tree_votes.mean(axis=0)
    if not np.all(np.isfinite(hard_vote_fraction)):
        raise ValueError("la fracción de votos RAW contiene valores no finitos")
    forest_class[supported] = predicted.astype(np.int16)
    vote_fraction[supported] = hard_vote_fraction.astype(np.float32)
    return LocalForestPrediction(
        forest_class=forest_class,
        vote_fraction=vote_fraction,
    )


def compute_vote_delta(
    *,
    reference_vote_fraction: np.ndarray,
    current_vote_fraction: np.ndarray,
    common_support: np.ndarray,
    nodata: float,
) -> np.ndarray:
    """Calcula current-reference sólo donde ambos años son evaluables."""
    reference, current, common = _validate_same_shape(
        reference_vote_fraction,
        current_vote_fraction,
        common_support,
    )
    common_bool = np.asarray(common, dtype=bool)
    for name, values in (("reference", reference), ("current", current)):
        evaluated = np.asarray(values, dtype=np.float64)[common_bool]
        if not np.all(np.isfinite(evaluated)) or np.any((evaluated < 0) | (evaluated > 1)):
            raise ValueError(f"{name}_vote_fraction debe estar en [0, 1] sobre common_support")
    output = np.full(reference.shape, nodata, dtype=np.float32)
    output[common_bool] = (
        np.asarray(current, dtype=np.float64)[common_bool]
        - np.asarray(reference, dtype=np.float64)[common_bool]
    ).astype(np.float32)
    if np.any(output[common_bool] < -1) or np.any(output[common_bool] > 1):
        raise RuntimeError("vote_delta salió del rango [-1, 1]")
    return output


def classify_forest_transition(
    *,
    reference_class: np.ndarray,
    current_class: np.ndarray,
    common_support: np.ndarray,
    aoi_footprint: np.ndarray,
    nodata: int,
) -> np.ndarray:
    """Aplica la tabla de verdad sólo dentro de la máscara común exacta."""
    reference, current, common, aoi = _validate_same_shape(
        reference_class,
        current_class,
        common_support,
        aoi_footprint,
    )
    common_bool = np.asarray(common, dtype=bool)
    aoi_bool = np.asarray(aoi, dtype=bool)
    if np.any(common_bool & ~aoi_bool):
        raise ValueError("common_support debe estar contenido en aoi_footprint")
    for name, values in (("reference", reference), ("current", current)):
        evaluated = np.asarray(values)[common_bool]
        if not np.all(np.isin(evaluated, (0, 1))):
            raise ValueError(f"{name}_class debe ser binaria sobre common_support")

    output = np.full(reference.shape, nodata, dtype=np.int16)
    output[aoi_bool] = int(ForestTransitionCode.INSUFFICIENT_DATA)
    masks = (
        (
            (np.asarray(reference) == 0) & (np.asarray(current) == 0),
            ForestTransitionCode.STABLE_NONFOREST,
        ),
        (
            (np.asarray(reference) == 0) & (np.asarray(current) == 1),
            ForestTransitionCode.CANDIDATE_FOREST_GAIN,
        ),
        (
            (np.asarray(reference) == 1) & (np.asarray(current) == 0),
            ForestTransitionCode.CANDIDATE_FOREST_LOSS,
        ),
        (
            (np.asarray(reference) == 1) & (np.asarray(current) == 1),
            ForestTransitionCode.STABLE_FOREST,
        ),
    )
    for mask, code in masks:
        output[common_bool & mask] = int(code)
    return output


def summarize_forest_year(
    *,
    year: int,
    transition: np.ndarray,
    vote_delta: np.ndarray,
    common_support: np.ndarray,
    aoi_footprint: np.ndarray,
    grid_spec: RasterGridSpec,
) -> dict[str, Any]:
    """Resume píxeles y hectáreas sobre una grilla métrica declarada."""
    transition_values, delta_values, common, aoi = _validate_same_shape(
        transition,
        vote_delta,
        common_support,
        aoi_footprint,
    )
    common_bool = np.asarray(common, dtype=bool)
    aoi_bool = np.asarray(aoi, dtype=bool)
    pixel_area_ha = (grid_spec.resolution_m * grid_spec.resolution_m) / 10_000
    categories: dict[str, dict[str, int | float]] = {}
    for code, label in _TRANSITION_LABELS.items():
        count = int(np.count_nonzero(np.asarray(transition_values) == int(code)))
        categories[label] = {
            "code": int(code),
            "pixel_count": count,
            "area_ha": count * pixel_area_ha,
        }
    evaluated_delta = np.asarray(delta_values, dtype=np.float64)[common_bool]
    if evaluated_delta.size == 0:
        delta_summary: dict[str, float | None] = {
            key: None
            for key in ("minimum", "q05", "q25", "median", "mean", "q75", "q95", "maximum")
        }
    else:
        quantiles = np.quantile(evaluated_delta, (0.05, 0.25, 0.5, 0.75, 0.95))
        delta_summary = {
            "minimum": float(evaluated_delta.min()),
            "q05": float(quantiles[0]),
            "q25": float(quantiles[1]),
            "median": float(quantiles[2]),
            "mean": float(evaluated_delta.mean()),
            "q75": float(quantiles[3]),
            "q95": float(quantiles[4]),
            "maximum": float(evaluated_delta.max()),
        }
    return {
        "year": year,
        "aoi_pixel_count": int(np.count_nonzero(aoi_bool)),
        "aoi_area_ha": float(np.count_nonzero(aoi_bool) * pixel_area_ha),
        "evaluable_pixel_count": int(np.count_nonzero(common_bool)),
        "evaluable_area_ha": float(np.count_nonzero(common_bool) * pixel_area_ha),
        "pixel_area_ha": pixel_area_ha,
        "categories": categories,
        "vote_delta": delta_summary,
    }


def compare_forest_models_2020(
    *,
    p0_class: np.ndarray,
    candidate_class: np.ndarray,
    p0_vote_fraction: np.ndarray,
    candidate_vote_fraction: np.ndarray,
    p0_input_complete: np.ndarray,
    candidate_input_complete: np.ndarray,
    aoi_footprint: np.ndarray,
    grid_spec: RasterGridSpec,
) -> ForestModelComparison:
    """Compara ambos assets sobre el mismo stack 2020 y exige soporte idéntico."""
    arrays = _validate_same_shape(
        p0_class,
        candidate_class,
        p0_vote_fraction,
        candidate_vote_fraction,
        p0_input_complete,
        candidate_input_complete,
        aoi_footprint,
    )
    p0_complete = np.asarray(arrays[4])
    candidate_complete = np.asarray(arrays[5])
    if not np.array_equal(p0_complete, candidate_complete):
        raise ValueError("P0 y candidato deben usar exactamente el mismo stack y soporte")
    aoi = np.asarray(arrays[6], dtype=bool)
    common = aoi & (candidate_complete == 1)
    disagreement = np.full(common.shape, int(grid_spec.nodata), dtype=np.int16)
    disagreement[common] = (np.asarray(arrays[0])[common] != np.asarray(arrays[1])[common]).astype(
        np.int16
    )
    vote_delta = compute_vote_delta(
        reference_vote_fraction=np.asarray(arrays[2]),
        current_vote_fraction=np.asarray(arrays[3]),
        common_support=common,
        nodata=grid_spec.nodata,
    )
    pixel_area_ha = (grid_spec.resolution_m**2) / 10_000
    p0_evaluated = np.asarray(arrays[0])[common]
    candidate_evaluated = np.asarray(arrays[1])[common]
    vote_evaluated = vote_delta[common].astype(np.float64)
    summary = {
        "schema_version": FOREST_RF_TEMPORAL_SCHEMA_VERSION,
        "same_input_complete_mask": True,
        "evaluable_pixel_count": int(np.count_nonzero(common)),
        "evaluable_area_ha": float(np.count_nonzero(common) * pixel_area_ha),
        "class_disagreement_pixel_count": int(np.count_nonzero(disagreement[common] == 1)),
        "class_disagreement_area_ha": float(
            np.count_nonzero(disagreement[common] == 1) * pixel_area_ha
        ),
        "p0_class_distribution": {
            "nonforest_pixel_count": int(np.count_nonzero(p0_evaluated == 0)),
            "forest_pixel_count": int(np.count_nonzero(p0_evaluated == 1)),
        },
        "candidate_class_distribution": {
            "nonforest_pixel_count": int(np.count_nonzero(candidate_evaluated == 0)),
            "forest_pixel_count": int(np.count_nonzero(candidate_evaluated == 1)),
        },
        "vote_delta_candidate_minus_p0": {
            "minimum": float(vote_evaluated.min()) if vote_evaluated.size else None,
            "median": float(np.median(vote_evaluated)) if vote_evaluated.size else None,
            "mean": float(vote_evaluated.mean()) if vote_evaluated.size else None,
            "maximum": float(vote_evaluated.max()) if vote_evaluated.size else None,
        },
        "final_assessment_generated": False,
    }
    return ForestModelComparison(
        class_disagreement=disagreement,
        vote_delta=vote_delta,
        summary=summary,
    )


def materialize_forest_rf_temporal(
    *,
    yearly_images: Mapping[int, ForestRfImages],
    yearly_feature_stacks: Mapping[int, SeasonalFeatureStack],
    p0_images: ForestRfImages,
    config: ForestRandomForestConfig,
    output_config: OutputConfig,
    grid_spec: RasterGridSpec,
    generated_at: datetime,
    fetch_bytes: FetchBytes | None = None,
) -> ForestRfTemporalMaterialization:
    """Descarga clases/scores y deriva deltas locales sobre una única grilla."""
    years = tuple(config.supported_observation_years)
    if tuple(sorted(yearly_images)) != years or tuple(sorted(yearly_feature_stacks)) != years:
        raise ValueError("yearly_images y yearly_feature_stacks deben cubrir el rango configurado")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at debe incluir zona horaria")
    paths = forest_rf_temporal_output_paths(years=years)
    files: dict[str, bytes] = {}
    estimates: list[DirectDownloadEstimate] = []
    validations: list[RasterValidation] = []
    model_bundle = _load_local_model_bundle(config)
    candidate_arrays: dict[int, dict[str, np.ndarray]] = {}

    for year in years:
        names = forest_rf_output_band_names(year)
        images = yearly_images[year]
        feature_stack = yearly_feature_stacks[year]
        canonical = canonicalize_forest_rf_feature_stack(
            feature_stack=feature_stack,
            observation_year=year,
            config=config,
        )
        predictor_names = forest_rf_predictor_band_names()
        qa_names = forest_rf_per_pixel_qa_band_names(year)
        missing_qa = tuple(name for name in qa_names if name not in feature_stack.feature_columns)
        if missing_qa:
            raise ValueError(f"faltan bandas QA per-pixel para {year}: {missing_qa}")
        predictor_raster = materialize_ee_image_to_grid(
            image=select_forest_rf_predictors(canonical),
            band_names=predictor_names,
            artifact_path=paths[f"predictors_{year}"],
            download_name=f"rf_predictor_stack_{year}",
            output_config=output_config,
            grid_spec=grid_spec,
            output_type="float32",
            fetch_bytes=fetch_bytes,
        )
        qa_raster = materialize_ee_image_to_grid(
            image=feature_stack.image.select(list(qa_names)),
            band_names=qa_names,
            artifact_path=paths[f"observation_qa_{year}"],
            download_name=f"rf_observation_qa_{year}",
            output_config=output_config,
            grid_spec=grid_spec,
            output_type="int16",
            fetch_bytes=fetch_bytes,
        )
        complete_raster = materialize_ee_image_to_grid(
            image=images.input_complete,
            band_names=(names["input_complete"],),
            artifact_path=paths[f"complete_{year}"],
            download_name=f"rf_forest_input_complete_{year}",
            output_config=output_config,
            grid_spec=grid_spec,
            output_type="int16",
            fetch_bytes=fetch_bytes,
        )
        files[paths[f"predictors_{year}"]] = predictor_raster.content
        files[paths[f"observation_qa_{year}"]] = qa_raster.content
        files[paths[f"complete_{year}"]] = complete_raster.content
        estimates.extend((predictor_raster.estimate, qa_raster.estimate, complete_raster.estimate))
        validations.extend(
            (predictor_raster.validation, qa_raster.validation, complete_raster.validation)
        )
        predictor_cube = _read_multiband(
            predictor_raster.content,
            grid_spec=grid_spec,
            expected_band_names=predictor_names,
        )
        complete = _read_single_band(complete_raster.content, grid_spec)
        aoi_footprint = np.asarray(complete != grid_spec.nodata, dtype=bool)
        prediction = predict_forest_rf_locally(
            predictor_cube=predictor_cube,
            predictor_band_names=predictor_names,
            input_complete=complete,
            aoi_footprint=aoi_footprint,
            model_bundle=model_bundle,
            nodata=grid_spec.nodata,
        )
        files[paths[f"class_{year}"]] = _single_band_tiff(
            values=prediction.forest_class,
            band_name=names["class"],
            grid_spec=grid_spec,
            dtype="int16",
        )
        files[paths[f"vote_{year}"]] = _single_band_tiff(
            values=prediction.vote_fraction,
            band_name=names["vote_fraction"],
            grid_spec=grid_spec,
            dtype="float32",
        )
        candidate_arrays[year] = {
            "class": prediction.forest_class,
            "vote": prediction.vote_fraction,
            "complete": complete,
        }

    p0_names = forest_rf_output_band_names(2020)
    p0_specs: tuple[tuple[str, Any, str, Literal["float32", "int16"]], ...] = (
        ("p0_class_2020", p0_images.forest_class, p0_names["class"], "int16"),
        ("p0_vote_2020", p0_images.forest_vote_fraction, p0_names["vote_fraction"], "float32"),
        ("p0_complete_2020", p0_images.input_complete, p0_names["input_complete"], "int16"),
    )
    for key, image, band_name, output_type in p0_specs:
        raster = materialize_ee_image_to_grid(
            image=image,
            band_names=(band_name,),
            artifact_path=paths[key],
            download_name=paths[key].rsplit("/", 1)[-1].removesuffix(".tif"),
            output_config=output_config,
            grid_spec=grid_spec,
            output_type=output_type,
            fetch_bytes=fetch_bytes,
        )
        files[paths[key]] = raster.content
        estimates.append(raster.estimate)
        validations.append(raster.validation)

    reference = candidate_arrays[2020]
    aoi_footprint = np.asarray(reference["complete"] != grid_spec.nodata, dtype=bool)
    yearly_summaries: list[Mapping[str, Any]] = []
    transitions: dict[int, np.ndarray] = {}
    deltas: dict[int, np.ndarray] = {}
    for year in years:
        current = candidate_arrays[year]
        current_aoi = np.asarray(current["complete"] != grid_spec.nodata, dtype=bool)
        if not np.array_equal(current_aoi, aoi_footprint):
            raise ValueError(f"AOI footprint inconsistente en {year}")
        common = (
            aoi_footprint
            & (np.asarray(reference["complete"]) == 1)
            & (np.asarray(current["complete"]) == 1)
        )
        delta = compute_vote_delta(
            reference_vote_fraction=reference["vote"],
            current_vote_fraction=current["vote"],
            common_support=common,
            nodata=grid_spec.nodata,
        )
        transition = classify_forest_transition(
            reference_class=reference["class"],
            current_class=current["class"],
            common_support=common,
            aoi_footprint=aoi_footprint,
            nodata=int(grid_spec.nodata),
        )
        deltas[year] = delta
        transitions[year] = transition
        summary = summarize_forest_year(
            year=year,
            transition=transition,
            vote_delta=delta,
            common_support=common,
            aoi_footprint=aoi_footprint,
            grid_spec=grid_spec,
        )
        summary["input_complete_pixel_count"] = int(
            np.count_nonzero(np.asarray(current["complete"]) == 1)
        )
        yearly_summaries.append(summary)
        if year != 2020:
            files[paths[f"delta_{year}"]] = _single_band_tiff(
                values=delta,
                band_name=f"rf_forest_vote_delta_{year}_vs_2020",
                grid_spec=grid_spec,
                dtype="float32",
            )
            files[paths[f"transition_{year}"]] = _single_band_tiff(
                values=transition,
                band_name=f"rf_forest_transition_{year}_vs_2020",
                grid_spec=grid_spec,
                dtype="int16",
            )

    comparison = compare_forest_models_2020(
        p0_class=_read_single_band(files[paths["p0_class_2020"]], grid_spec),
        candidate_class=reference["class"],
        p0_vote_fraction=_read_single_band(files[paths["p0_vote_2020"]], grid_spec),
        candidate_vote_fraction=reference["vote"],
        p0_input_complete=_read_single_band(files[paths["p0_complete_2020"]], grid_spec),
        candidate_input_complete=reference["complete"],
        aoi_footprint=aoi_footprint,
        grid_spec=grid_spec,
    )
    files[paths["comparison_disagreement"]] = _single_band_tiff(
        values=comparison.class_disagreement,
        band_name="rf_model_class_disagreement_p0_vs_multiyear_2020",
        grid_spec=grid_spec,
        dtype="int16",
    )
    files[paths["comparison_vote_delta"]] = _single_band_tiff(
        values=comparison.vote_delta,
        band_name="rf_model_vote_delta_multiyear_minus_p0_2020",
        grid_spec=grid_spec,
        dtype="float32",
    )
    files[paths["summary_csv"]] = _summary_csv(tuple(yearly_summaries))
    files[paths["summary_json"]] = _json_bytes(
        {
            "schema_version": FOREST_RF_TEMPORAL_SCHEMA_VERSION,
            "reference_year": 2020,
            "years": list(years),
            "transition_codes": {label: int(code) for code, label in _TRANSITION_LABELS.items()},
            "mask_semantics": (
                "AOI intersection input_complete_2020 intersection input_complete_year"
            ),
            "scientific_status": "review_required",
            "yearly_summaries": yearly_summaries,
            "final_assessment_generated": False,
            "attribution_generated": False,
        }
    )
    files[paths["comparison_json"]] = _json_bytes(comparison.summary)
    qa_metadata = {
        str(year): _per_pixel_qa_metadata(
            year=year,
            path=paths[f"observation_qa_{year}"],
            content=files[paths[f"observation_qa_{year}"]],
            grid_spec=grid_spec,
        )
        for year in years
    }
    files[paths["metadata"]] = _json_bytes(
        {
            "schema_version": FOREST_RF_TEMPORAL_SCHEMA_VERSION,
            "scientific_status": "review_required",
            "generated_at": generated_at.isoformat(),
            "grid": grid_spec.model_dump(mode="json"),
            "candidate_model": config.model_dump(mode="json"),
            "p0_model": p0_images.metadata.model_dump(mode="json"),
            "yearly_model_outputs": {
                str(year): yearly_images[year].metadata.model_dump(mode="json") for year in years
            },
            "seasonal_support": {
                str(year): [
                    {
                        "period_id": period.period_id,
                        "start_date": period.start_date.isoformat(),
                        "end_date_exclusive": period.end_date_exclusive.isoformat(),
                        "input_scene_count": period.input_scene_count,
                    }
                    for period in yearly_feature_stacks[year].periods
                ]
                for year in years
            },
            "per_pixel_observation_qa": {
                "schema_version": "1.0.0",
                "band_count_per_year": 12,
                "dtype": "int16",
                "nodata": grid_spec.nodata,
                "semantics": (
                    "Conteo por píxel de observaciones HLS válidas después de QA, "
                    "separado por estación meteorológica austral y sensor"
                ),
                "years": qa_metadata,
            },
            "raster_materialization": {
                "download_method": "ee.Image.getDownloadURL",
                "signed_urls_persisted": False,
                "estimates": [item.model_dump(mode="json") for item in estimates],
                "raster_validations": [item.model_dump(mode="json") for item in validations],
            },
            "predictor_columns_sha256": forest_rf_predictor_columns_sha256(),
            "predictor_slot_year": 2020,
            "observed_years": list(years),
            "assets": paths,
            "mask_semantics": (
                "AOI ∩ input_complete_2020 ∩ input_complete_year; outside common support "
                "is insufficient_data inside AOI and nodata outside AOI"
            ),
            "inference_backend": "local_sklearn_joblib",
            "gee_candidate_graph_diagnostic": {
                "decision_tree_ensemble_constructed": True,
                "point_reduce_region_succeeded": True,
                "fixed_grid_get_download_url_succeeded": False,
                "failed_operation": "ee.Image.getDownloadURL",
                "failed_graph": "real_annual_hls_stack_plus_candidate_classification",
                "error_code": "description_length_exceeds_maximum",
                "precise_limiting_component_isolated": False,
            },
            "local_fallback_semantics": (
                "Los predictores HLS se descargan desde GEE sobre la grilla contractual "
                "y el joblib sklearn exacto se aplica localmente"
            ),
            "score_semantics": "uncalibrated_binary_tree_vote_fraction",
            "class_source": "direct_sklearn_random_forest_predict",
            "delta_semantics": "vote_year_minus_vote_2020_not_consecutive",
            "temporal_transfer_validated": False,
            "final_assessment_generated": False,
            "attribution_generated": False,
            "limitations": [
                "Las transiciones son candidatas y no prueban deforestación.",
                "La fracción RAW es el promedio de votos binarios de los árboles y no "
                "una probabilidad calibrada.",
                "La transferencia temporal no es validación regional independiente.",
            ],
        }
    )
    files[paths["overview_figure"]] = _render_overview(
        years=years,
        candidate_arrays=candidate_arrays,
        transitions=transitions,
        comparison=comparison,
        dpi=output_config.png_dpi,
        nodata=grid_spec.nodata,
        grid_spec=grid_spec,
    )
    files[paths["comparison_figure"]] = _render_comparison(
        p0_class=_read_single_band(files[paths["p0_class_2020"]], grid_spec),
        candidate_class=reference["class"],
        comparison=comparison,
        dpi=output_config.png_dpi,
        nodata=grid_spec.nodata,
    )
    return ForestRfTemporalMaterialization(
        files=files,
        estimates=tuple(estimates),
        validations=tuple(validations),
        grid_spec=grid_spec,
        yearly_summaries=tuple(yearly_summaries),
        model_comparison_2020=comparison,
    )


def _validate_same_shape(*arrays: np.ndarray) -> tuple[np.ndarray, ...]:
    normalized = tuple(np.asarray(array) for array in arrays)
    if not normalized or normalized[0].ndim != 2:
        raise ValueError("los rasters deben ser matrices 2D")
    shape = normalized[0].shape
    if any(array.ndim != 2 or array.shape != shape for array in normalized[1:]):
        raise ValueError("todos los rasters deben compartir shape")
    return normalized


def _load_local_model_bundle(config: ForestRandomForestConfig) -> Mapping[str, Any]:
    if config.inference_backend != "local_sklearn_joblib":
        raise ValueError("la materialización temporal requiere backend local_sklearn_joblib")
    verified = verify_local_rf_model_release(
        config,
        project_root=PROJECT_ROOT,
        load_bundle=True,
    )
    if verified.bundle is None:
        raise RuntimeError("el preflight no devolvió el bundle RF verificado")
    return verified.bundle


def _read_multiband(
    content: bytes,
    *,
    grid_spec: RasterGridSpec,
    expected_band_names: tuple[str, ...],
) -> np.ndarray:
    with MemoryFile(content) as memory:
        with memory.open() as dataset:
            if (
                dataset.count != len(expected_band_names)
                or dataset.width != grid_spec.width
                or dataset.height != grid_spec.height
                or dataset.crs is None
                or dataset.crs.to_string() != grid_spec.target_crs
                or tuple(dataset.transform)[:6] != tuple(Affine(*grid_spec.transform))[:6]
                or dataset.nodata != grid_spec.nodata
                or tuple(dataset.descriptions) != expected_band_names
            ):
                raise ValueError("GeoTIFF predictor RF no coincide con el contrato")
            return np.asarray(dataset.read())


def _read_single_band(content: bytes, grid_spec: RasterGridSpec) -> np.ndarray:
    with MemoryFile(content) as memory:
        with memory.open() as dataset:
            if (
                dataset.count != 1
                or dataset.width != grid_spec.width
                or dataset.height != grid_spec.height
                or dataset.crs is None
                or dataset.crs.to_string() != grid_spec.target_crs
                or tuple(dataset.transform)[:6] != tuple(Affine(*grid_spec.transform))[:6]
                or dataset.nodata != grid_spec.nodata
            ):
                raise ValueError("GeoTIFF RF no coincide con la grilla declarada")
            values = cast(np.ndarray, dataset.read(indexes=[1]))
            band: np.ndarray = values[0]
            return band


def _single_band_tiff(
    *,
    values: np.ndarray,
    band_name: str,
    grid_spec: RasterGridSpec,
    dtype: str,
) -> bytes:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=grid_spec.width,
            height=grid_spec.height,
            count=1,
            dtype=dtype,
            crs=grid_spec.target_crs,
            transform=Affine(*grid_spec.transform),
            nodata=grid_spec.nodata,
            compress="deflate",
        ) as dataset:
            dataset.write(np.asarray(values, dtype=dtype), 1)
            dataset.set_band_description(1, band_name)
        return bytes(memory.read())


def _summary_csv(summaries: tuple[Mapping[str, Any], ...]) -> bytes:
    stream = StringIO(newline="")
    fields = [
        "year",
        "aoi_pixel_count",
        "aoi_area_ha",
        "input_complete_pixel_count",
        "evaluable_pixel_count",
        "evaluable_area_ha",
        *(
            f"{label}_{suffix}"
            for label in _TRANSITION_LABELS.values()
            for suffix in ("pixel_count", "area_ha")
        ),
        *(
            f"vote_delta_{key}"
            for key in (
                "minimum",
                "q05",
                "q25",
                "median",
                "mean",
                "q75",
                "q95",
                "maximum",
            )
        ),
    ]
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for summary in summaries:
        row = {key: summary.get(key) for key in fields}
        categories = summary["categories"]
        for label in _TRANSITION_LABELS.values():
            row[f"{label}_pixel_count"] = categories[label]["pixel_count"]
            row[f"{label}_area_ha"] = categories[label]["area_ha"]
        for key, value in summary["vote_delta"].items():
            row[f"vote_delta_{key}"] = value
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def _per_pixel_qa_metadata(
    *,
    year: int,
    path: str,
    content: bytes,
    grid_spec: RasterGridSpec,
) -> dict[str, Any]:
    band_names = forest_rf_per_pixel_qa_band_names(year)
    bands = []
    for band_name in band_names:
        _, season, count_name = band_name.split("_", 2)
        sensor = (
            "combined_hls_l30_s30"
            if count_name == "valid_observation_count"
            else "hls_l30"
            if count_name.endswith("_l30")
            else "hls_s30"
        )
        bands.append(
            {
                "name": band_name,
                "season": season,
                "sensor_scope": sensor,
                "semantics": "valid_observation_count_per_pixel_after_hls_qa",
            }
        )
    return {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "grid_sha256": grid_spec.grid_sha256,
        "nodata": grid_spec.nodata,
        "dtype": "int16",
        "band_count": len(band_names),
        "band_names": list(band_names),
        "bands": bands,
    }


def _render_overview(
    *,
    years: tuple[int, ...],
    candidate_arrays: Mapping[int, Mapping[str, np.ndarray]],
    transitions: Mapping[int, np.ndarray],
    comparison: ForestModelComparison,
    dpi: int,
    nodata: float,
    grid_spec: RasterGridSpec,
) -> bytes:
    figure, axes = plt.subplots(
        2,
        len(years),
        figsize=(4 * len(years) + 3, 9),
        constrained_layout=True,
        squeeze=False,
    )
    class_cmap = ListedColormap(["#d9d9d9", "#176d2d"])
    transition_cmap = ListedColormap(["#bdbdbd", "#f0f0f0", "#41ab5d", "#d7301f", "#006d2c"])
    transition_norm = BoundaryNorm(np.arange(-0.5, 5.5, 1), transition_cmap.N)
    for column, year in enumerate(years):
        klass = np.ma.masked_where(
            candidate_arrays[year]["class"] == nodata,
            candidate_arrays[year]["class"],
        )
        axes[0, column].imshow(klass, cmap=class_cmap, vmin=0, vmax=1, interpolation="nearest")
        axes[0, column].set_title(f"Clase RF {year}")
        if year == 2020:
            panel = np.ma.masked_where(
                comparison.class_disagreement == nodata,
                comparison.class_disagreement,
            )
            axes[1, column].imshow(panel, cmap="Reds", vmin=0, vmax=1, interpolation="nearest")
            axes[1, column].set_title("Desacuerdo P0 vs candidato")
        else:
            panel = np.ma.masked_where(transitions[year] == nodata, transitions[year])
            axes[1, column].imshow(
                panel,
                cmap=transition_cmap,
                norm=transition_norm,
                interpolation="nearest",
            )
            axes[1, column].set_title(f"Transición {year} vs 2020")
        for row in (0, 1):
            axes[row, column].set_xticks([])
            axes[row, column].set_yticks([])
            _add_north_arrow_and_metric_scale(axes[row, column], grid_spec=grid_spec)
    axes[0, 0].legend(
        handles=(
            Patch(facecolor="#d9d9d9", edgecolor="black", label="No bosque"),
            Patch(facecolor="#176d2d", edgecolor="black", label="Bosque"),
        ),
        title="Clase RF",
        loc="upper left",
        bbox_to_anchor=(0, -0.08),
        frameon=True,
        ncols=2,
    )
    transition_axis = axes[1, 1 if len(years) > 1 else 0]
    transition_axis.legend(
        handles=(
            Patch(facecolor="#bdbdbd", edgecolor="black", label="Datos insuficientes"),
            Patch(facecolor="#f0f0f0", edgecolor="black", label="No bosque estable"),
            Patch(
                facecolor="#41ab5d",
                edgecolor="black",
                label="Ganancia forestal candidata",
            ),
            Patch(
                facecolor="#d7301f",
                edgecolor="black",
                label="Pérdida forestal candidata",
            ),
            Patch(facecolor="#006d2c", edgecolor="black", label="Bosque estable"),
        ),
        title="Transición vs 2020",
        loc="upper left",
        bbox_to_anchor=(0, -0.08),
        frameon=True,
        ncols=1,
    )
    figure.suptitle(
        "RF forestal multianual — clases y deltas candidatos, no deforestación",
        fontsize=15,
    )
    return _figure_bytes(figure, dpi)


def _add_north_arrow_and_metric_scale(axis: Any, *, grid_spec: RasterGridSpec) -> None:
    axis.annotate(
        "N",
        xy=(0.92, 0.84),
        xytext=(0.92, 0.64),
        xycoords="axes fraction",
        textcoords="axes fraction",
        ha="center",
        va="bottom",
        fontsize=9,
        fontweight="bold",
        arrowprops={"arrowstyle": "-|>", "color": "black", "linewidth": 1.4},
    )
    target_metres = max(grid_spec.resolution_m, grid_spec.width * grid_spec.resolution_m / 4)
    scale_metres = _nice_scale_metres(target_metres)
    scale_pixels = scale_metres / grid_spec.resolution_m
    x_start = grid_spec.width * 0.08
    x_end = min(grid_spec.width * 0.92, x_start + scale_pixels)
    actual_metres = (x_end - x_start) * grid_spec.resolution_m
    y = grid_spec.height * 0.91
    axis.plot(
        (x_start, x_end),
        (y, y),
        color="black",
        linewidth=3,
        solid_capstyle="butt",
    )
    label = f"{actual_metres / 1000:g} km" if actual_metres >= 1000 else f"{actual_metres:g} m"
    axis.text(
        (x_start + x_end) / 2,
        y - grid_spec.height * 0.04,
        label,
        ha="center",
        va="top",
        fontsize=7,
        bbox={"facecolor": "white", "alpha": 0.75, "edgecolor": "none", "pad": 1},
    )


def _nice_scale_metres(target_metres: float) -> float:
    if target_metres <= 0 or not np.isfinite(target_metres):
        raise ValueError("target_metres debe ser positivo y finito")
    exponent = 10 ** np.floor(np.log10(target_metres))
    normalized = target_metres / exponent
    factor = max(item for item in (1.0, 2.0, 5.0) if item <= normalized)
    return float(factor * exponent)


def _render_comparison(
    *,
    p0_class: np.ndarray,
    candidate_class: np.ndarray,
    comparison: ForestModelComparison,
    dpi: int,
    nodata: float,
) -> bytes:
    figure, axes = plt.subplots(1, 4, figsize=(16, 4.5), constrained_layout=True)
    panels = (
        (p0_class, "Clase P0 2020", "Greens", 0, 1),
        (candidate_class, "Clase multianual 2020", "Greens", 0, 1),
        (comparison.class_disagreement, "Desacuerdo de clase", "Reds", 0, 1),
        (comparison.vote_delta, "Voto candidato - P0", "RdBu", -1, 1),
    )
    for axis, (values, title, cmap, minimum, maximum) in zip(axes, panels, strict=True):
        panel = np.ma.masked_where(values == nodata, values)
        image = axis.imshow(
            panel,
            cmap=cmap,
            vmin=minimum,
            vmax=maximum,
            interpolation="nearest",
        )
        axis.set_title(title)
        axis.set_xticks([])
        axis.set_yticks([])
        figure.colorbar(image, ax=axis, shrink=0.75)
    figure.suptitle("Mismo stack y soporte 2020 — comparación técnica de modelos", fontsize=14)
    return _figure_bytes(figure, dpi)


def _figure_bytes(figure: Figure, dpi: int) -> bytes:
    stream = BytesIO()
    try:
        figure.savefig(stream, format="png", dpi=dpi, bbox_inches="tight")
        return stream.getvalue()
    finally:
        plt.close(figure)


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
