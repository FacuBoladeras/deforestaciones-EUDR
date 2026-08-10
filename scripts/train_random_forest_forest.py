"""Entrena y selecciona un único RF multianual bosque/no bosque para Entre Ríos.

El script conserva la idea del entrenador P0 aportado por el usuario, pero hace
explícitos el contrato de 56 variables, el agrupamiento longitudinal por sitio y
la separación estricta entre selección (validation) y reporte final (test).

Ejemplo:
    python scripts/train_random_forest_forest.py \
        C:/ruta/deforestation-pipeline-training-p0 \
        --manifest outputs/training_p0/training-multiyear-2020-2024-validation.json \
        --output-dir outputs/models/rf_forest_multiyear_2020_2024_v1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

TARGET_COLUMN = "proxy_label"
SPLIT_COLUMN = "split"
SITE_COLUMN = "sample_id"
BLOCK_COLUMN = "block_id"
YEAR_COLUMN = "sample_year"
POSITIVE_LABEL = "forest"
NEGATIVE_LABEL = "non_forest"
AMBIGUOUS_LABEL = "ambiguous"
LABEL_TO_INT = {NEGATIVE_LABEL: 0, POSITIVE_LABEL: 1}
INT_TO_LABEL = {value: key for key, value in LABEL_TO_INT.items()}
EXPECTED_SPLITS = {"train", "validation", "test"}
EXPECTED_FEATURE_COUNT = 56
VARIANT_UNIFORM = "uniform"
VARIANT_SITE_WEIGHTED = "inverse_site_frequency"
MODEL_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class FeatureContract:
    predictor_columns: tuple[str, ...]
    predictor_columns_sha256: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Entrena RF multianual, compara pesos uniformes/inversos por sitio y "
            "reserva test para el candidato ganador."
        )
    )
    parser.add_argument(
        "csv_inputs",
        nargs="+",
        type=Path,
        help="CSV individuales o un directorio que contenga los shards anuales.",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--trees", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--max-baseline-regression",
        type=float,
        default=0.02,
        help="Regresión máxima admisible en balanced accuracy y peor F1 anual.",
    )
    return parser.parse_args()


def canonical_json_hash(values: list[str] | tuple[str, ...]) -> str:
    payload = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"No existe el JSON: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Se esperaba un objeto JSON en {path}")
    return loaded


def load_feature_contract(path: Path) -> FeatureContract:
    manifest = load_json(path)
    schema = manifest.get("canonical_feature_schema")
    if not isinstance(schema, dict):
        raise ValueError("El manifiesto no contiene canonical_feature_schema")
    raw_columns = schema.get("predictor_columns")
    expected_hash = schema.get("predictor_columns_sha256")
    if not isinstance(raw_columns, list) or not all(isinstance(x, str) for x in raw_columns):
        raise ValueError("predictor_columns debe ser una lista de strings")
    columns = tuple(raw_columns)
    if len(columns) != EXPECTED_FEATURE_COUNT or len(set(columns)) != len(columns):
        raise ValueError("El contrato debe contener 56 predictores únicos")
    actual_hash = canonical_json_hash(columns)
    if expected_hash != actual_hash:
        raise ValueError(
            "El hash de predictor_columns no coincide: "
            f"esperado={expected_hash}; calculado={actual_hash}"
        )
    return FeatureContract(columns, actual_hash)


def resolve_csv_inputs(inputs: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for item in inputs:
        if item.is_dir():
            resolved.extend(sorted(item.glob("*.csv")))
        elif item.is_file():
            resolved.append(item)
        else:
            raise FileNotFoundError(f"No existe la entrada CSV: {item}")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in resolved:
        absolute = path.resolve()
        if absolute not in seen:
            unique.append(absolute)
            seen.add(absolute)
    if not unique:
        raise ValueError("No se encontraron CSV de entrenamiento")
    return unique


def validate_input_hashes(csv_files: list[Path], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    concatenated = manifest.get("concatenated_csv", {})
    canonical_name = Path(str(concatenated.get("path", ""))).name
    if len(csv_files) == 1 and csv_files[0].name == canonical_name:
        expected = str(concatenated.get("sha256", ""))
        actual = sha256_file(csv_files[0])
        if actual != expected:
            raise ValueError("El hash del CSV canónico no coincide con el manifiesto")
        return [{"path": str(csv_files[0]), "sha256": actual}]

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise ValueError("El manifiesto no contiene la lista de shards")
    expected_by_name = {Path(str(item["path"])).name: item for item in raw_files}
    observed_names = [path.name for path in csv_files]
    if set(observed_names) != set(expected_by_name) or len(observed_names) != len(expected_by_name):
        missing = sorted(set(expected_by_name) - set(observed_names))
        extra = sorted(set(observed_names) - set(expected_by_name))
        raise ValueError(f"Shards incompletos o adicionales: faltan={missing}; extra={extra}")
    records: list[dict[str, Any]] = []
    for path in csv_files:
        actual = sha256_file(path)
        expected = str(expected_by_name[path.name]["sha256"])
        if actual != expected:
            raise ValueError(f"Hash inválido para {path.name}: {actual} != {expected}")
        records.append({"path": str(path), "sha256": actual})
    return records


def load_and_concatenate(csv_files: list[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for csv_file in csv_files:
        frame = pd.read_csv(csv_file)
        if frame.empty:
            raise ValueError(f"El archivo está vacío: {csv_file}")
        frames.append(frame)
        print(f"Leído: {csv_file.name} -> {frame.shape[0]} filas")
    return pd.concat(frames, axis=0, ignore_index=True)


def validate_dataset(frame: pd.DataFrame, feature_columns: tuple[str, ...]) -> pd.DataFrame:
    required = {
        TARGET_COLUMN,
        SPLIT_COLUMN,
        SITE_COLUMN,
        BLOCK_COLUMN,
        YEAR_COLUMN,
        *feature_columns,
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Faltan columnas requeridas: {missing}")
    feature_set = set(feature_columns)
    observed_feature_order = tuple(column for column in frame.columns if column in feature_set)
    if observed_feature_order != feature_columns:
        raise ValueError("El orden de predictores no coincide con el contrato canónico")
    normalized = frame.copy()
    normalized[SPLIT_COLUMN] = normalized[SPLIT_COLUMN].astype(str).str.strip().str.lower()
    observed_splits = set(normalized[SPLIT_COLUMN].unique())
    if observed_splits != EXPECTED_SPLITS:
        raise ValueError(
            f"Splits inválidos: esperados={sorted(EXPECTED_SPLITS)}; "
            f"observados={sorted(observed_splits)}"
        )
    if normalized.duplicated([YEAR_COLUMN, SITE_COLUMN]).any():
        raise ValueError("Hay observaciones duplicadas para (sample_year, sample_id)")
    for group_column in (SITE_COLUMN, BLOCK_COLUMN):
        leakage = normalized.groupby(group_column)[SPLIT_COLUMN].nunique()
        leaking = leakage[leakage > 1]
        if not leaking.empty:
            raise ValueError(f"Leakage: {len(leaking)} valores de {group_column} cruzan splits")
    numeric = normalized.loc[:, list(feature_columns)].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError("Los 56 predictores deben ser numéricos y finitos")
    normalized.loc[:, list(feature_columns)] = numeric
    binary = normalized[normalized[TARGET_COLUMN].isin(LABEL_TO_INT)]
    inconsistent = binary.groupby(SITE_COLUMN)[TARGET_COLUMN].nunique()
    if bool((inconsistent > 1).any()):
        raise ValueError("Hay etiquetas binarias inconsistentes dentro de sample_id")
    return normalized


def inverse_site_frequency_weights(sample_ids: pd.Series) -> pd.Series:
    counts = sample_ids.map(sample_ids.value_counts())
    return 1.0 / counts.astype(float)


def model_parameters(*, trees: int, seed: int) -> dict[str, Any]:
    return {
        "n_estimators": trees,
        "max_depth": None,
        "min_samples_split": 2,
        "min_samples_leaf": 1,
        "max_features": "sqrt",
        "bootstrap": True,
        "class_weight": "balanced_subsample",
        "oob_score": True,
        "n_jobs": -1,
        "random_state": seed,
    }


def new_model(*, trees: int, seed: int) -> RandomForestClassifier:
    return RandomForestClassifier(**model_parameters(trees=trees, seed=seed))


def binary_metrics(
    true_codes: np.ndarray, predicted_codes: np.ndarray, probabilities: np.ndarray
) -> dict[str, Any]:
    matrix = confusion_matrix(true_codes, predicted_codes, labels=[0, 1])
    return {
        "accuracy": float(accuracy_score(true_codes, predicted_codes)),
        "balanced_accuracy": float(balanced_accuracy_score(true_codes, predicted_codes)),
        "precision_forest": float(
            precision_score(true_codes, predicted_codes, pos_label=1, zero_division=0)
        ),
        "recall_forest": float(
            recall_score(true_codes, predicted_codes, pos_label=1, zero_division=0)
        ),
        "f1_forest": float(f1_score(true_codes, predicted_codes, pos_label=1)),
        "roc_auc": float(roc_auc_score(true_codes, probabilities)),
        "brier_score": float(brier_score_loss(true_codes, probabilities)),
        "confusion_matrix_labels_0_1": matrix.tolist(),
    }


def site_level_predictions(rows: pd.DataFrame) -> pd.DataFrame:
    required = {SITE_COLUMN, "true_code", "forest_vote_fraction"}
    missing = required.difference(rows.columns)
    if missing:
        raise ValueError(f"Faltan columnas para evaluación por sitio: {sorted(missing)}")
    label_counts = rows.groupby(SITE_COLUMN)["true_code"].nunique()
    if bool((label_counts > 1).any()):
        raise ValueError("Hay etiquetas inconsistentes dentro de un mismo sample_id")
    return (
        rows.groupby(SITE_COLUMN, as_index=False)
        .agg(
            true_code=("true_code", "first"),
            forest_vote_fraction=("forest_vote_fraction", "mean"),
            observation_count=("true_code", "size"),
        )
        .assign(predicted_code=lambda value: (value["forest_vote_fraction"] >= 0.5).astype(int))
    )


def evaluate_partition(
    model: RandomForestClassifier,
    rows: pd.DataFrame,
    feature_columns: tuple[str, ...],
) -> tuple[dict[str, Any], pd.DataFrame]:
    true_codes = rows[TARGET_COLUMN].map(LABEL_TO_INT).to_numpy(dtype=int)
    probabilities = model.predict_proba(rows.loc[:, list(feature_columns)])[:, 1]
    predicted_codes = (probabilities >= 0.5).astype(int)
    predictions = rows.loc[:, [SITE_COLUMN, YEAR_COLUMN, BLOCK_COLUMN, SPLIT_COLUMN]].copy()
    predictions["true_code"] = true_codes
    predictions["true_label"] = predictions["true_code"].map(INT_TO_LABEL)
    predictions["predicted_code"] = predicted_codes
    predictions["predicted_label"] = predictions["predicted_code"].map(INT_TO_LABEL)
    predictions["forest_vote_fraction"] = probabilities

    row_metrics = binary_metrics(true_codes, predicted_codes, probabilities)
    sites = site_level_predictions(predictions)
    site_metrics = binary_metrics(
        sites["true_code"].to_numpy(dtype=int),
        sites["predicted_code"].to_numpy(dtype=int),
        sites["forest_vote_fraction"].to_numpy(dtype=float),
    )
    by_year: dict[str, Any] = {}
    for year, group in predictions.groupby(YEAR_COLUMN, sort=True):
        by_year[str(int(year))] = {
            **binary_metrics(
                group["true_code"].to_numpy(dtype=int),
                group["predicted_code"].to_numpy(dtype=int),
                group["forest_vote_fraction"].to_numpy(dtype=float),
            ),
            "row_count": len(group),
            "site_count": int(group[SITE_COLUMN].nunique()),
            "block_count": int(group[BLOCK_COLUMN].nunique()),
        }
    summary = {
        **row_metrics,
        "row_count": len(rows),
        "site_count": int(rows[SITE_COLUMN].nunique()),
        "block_count": int(rows[BLOCK_COLUMN].nunique()),
        "site_level": {**site_metrics, "site_count": len(sites)},
        "by_year": by_year,
        "worst_year_f1_forest": min(float(metrics["f1_forest"]) for metrics in by_year.values()),
    }
    return summary, predictions


def select_candidate(
    candidates: dict[str, dict[str, Any]],
    baseline: dict[str, Any],
    *,
    max_regression: float,
) -> dict[str, Any]:
    baseline_validation = baseline["validation"]
    minimum_balanced_accuracy = float(baseline_validation["balanced_accuracy"]) - max_regression
    minimum_worst_year_f1 = float(baseline_validation["worst_year_f1_forest"]) - max_regression
    gates: dict[str, Any] = {}
    passing: list[str] = []
    for variant, payload in candidates.items():
        metrics = payload["validation"]
        passed = (
            float(metrics["balanced_accuracy"]) >= minimum_balanced_accuracy
            and float(metrics["worst_year_f1_forest"]) >= minimum_worst_year_f1
        )
        gates[variant] = {
            "passed": passed,
            "minimum_balanced_accuracy": minimum_balanced_accuracy,
            "minimum_worst_year_f1_forest": minimum_worst_year_f1,
        }
        if passed:
            passing.append(variant)

    tie_preference = {VARIANT_UNIFORM: 0, VARIANT_SITE_WEIGHTED: 1}
    selected = None
    if passing:
        selected = max(
            passing,
            key=lambda variant: (
                float(candidates[variant]["validation"]["worst_year_f1_forest"]),
                float(candidates[variant]["validation"]["balanced_accuracy"]),
                float(candidates[variant]["validation"]["f1_forest"]),
                tie_preference.get(variant, -1),
            ),
        )
    return {
        "gate_passed": selected is not None,
        "selected_variant": selected,
        "selection_used_test": False,
        "max_regression": max_regression,
        "criterion": (
            "validation max(worst_year_f1_forest, balanced_accuracy, f1_forest); "
            "tie -> inverse_site_frequency"
        ),
        "candidate_gates": gates,
    }


def git_provenance() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    try:
        revision = run("rev-parse", "HEAD")
        dirty = bool(run("status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = "unknown", True
    return {"git_revision": revision, "git_dirty": dirty}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    if args.trees < 1:
        raise ValueError("--trees debe ser positivo")
    if not 0 <= args.max_baseline_regression <= 1:
        raise ValueError("--max-baseline-regression debe estar entre 0 y 1")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_json(args.manifest)
    contract = load_feature_contract(args.manifest)
    csv_files = resolve_csv_inputs(args.csv_inputs)
    input_records = validate_input_hashes(csv_files, manifest)
    raw = validate_dataset(load_and_concatenate(csv_files), contract.predictor_columns)
    binary = raw[raw[TARGET_COLUMN].isin(LABEL_TO_INT)].copy()
    ambiguous = raw[raw[TARGET_COLUMN].eq(AMBIGUOUS_LABEL)].copy()

    train = binary[binary[SPLIT_COLUMN].eq("train")]
    validation = binary[binary[SPLIT_COLUMN].eq("validation")]
    test = binary[binary[SPLIT_COLUMN].eq("test")]
    if any(part.empty for part in (train, validation, test)):
        raise ValueError("train, validation y test deben tener filas binarias")

    parameters = model_parameters(trees=args.trees, seed=args.seed)
    candidate_metrics: dict[str, dict[str, Any]] = {}
    validation_predictions: list[pd.DataFrame] = []
    for variant in (VARIANT_UNIFORM, VARIANT_SITE_WEIGHTED):
        model = new_model(trees=args.trees, seed=args.seed)
        weights = (
            None
            if variant == VARIANT_UNIFORM
            else inverse_site_frequency_weights(train[SITE_COLUMN]).to_numpy()
        )
        model.fit(
            train.loc[:, list(contract.predictor_columns)],
            train[TARGET_COLUMN].map(LABEL_TO_INT),
            sample_weight=weights,
        )
        metrics, predictions = evaluate_partition(model, validation, contract.predictor_columns)
        candidate_metrics[variant] = {
            "training": {
                "row_count": len(train),
                "site_count": int(train[SITE_COLUMN].nunique()),
                "block_count": int(train[BLOCK_COLUMN].nunique()),
                "oob_accuracy": float(model.oob_score_),
                "sample_weight": "none" if weights is None else "1/site_frequency",
            },
            "validation": metrics,
        }
        predictions.insert(0, "variant", variant)
        validation_predictions.append(predictions)

    baseline_train = train[train[YEAR_COLUMN].eq(2020)]
    baseline_model = new_model(trees=args.trees, seed=args.seed)
    baseline_model.fit(
        baseline_train.loc[:, list(contract.predictor_columns)],
        baseline_train[TARGET_COLUMN].map(LABEL_TO_INT),
    )
    baseline_validation_metrics, baseline_predictions = evaluate_partition(
        baseline_model, validation, contract.predictor_columns
    )
    baseline = {
        "kind": "2020_only_same_hyperparameters",
        "training": {
            "row_count": len(baseline_train),
            "site_count": int(baseline_train[SITE_COLUMN].nunique()),
            "block_count": int(baseline_train[BLOCK_COLUMN].nunique()),
            "oob_accuracy": float(baseline_model.oob_score_),
        },
        "validation": baseline_validation_metrics,
    }
    baseline_predictions.insert(0, "variant", "baseline_2020_only")
    validation_predictions.append(baseline_predictions)
    pd.concat(validation_predictions, ignore_index=True).to_csv(
        args.output_dir / "candidate_validation_predictions.csv", index=False
    )

    selection = select_candidate(
        candidate_metrics,
        baseline,
        max_regression=args.max_baseline_regression,
    )
    comparison: dict[str, Any] = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "selection": selection,
        "candidates": candidate_metrics,
        "baseline_2020_only": baseline,
        "test_evaluations_before_selection": 0,
    }
    write_json(args.output_dir / "metrics_comparison.json", comparison)
    if not selection["gate_passed"]:
        print("Ningún candidato superó el gate; no se genera ni exporta un modelo.")
        return 2

    selected_variant = str(selection["selected_variant"])
    final_rows = binary[binary[SPLIT_COLUMN].isin(["train", "validation"])]
    final_weights = (
        None
        if selected_variant == VARIANT_UNIFORM
        else inverse_site_frequency_weights(final_rows[SITE_COLUMN]).to_numpy()
    )
    final_model = new_model(trees=args.trees, seed=args.seed)
    final_model.fit(
        final_rows.loc[:, list(contract.predictor_columns)],
        final_rows[TARGET_COLUMN].map(LABEL_TO_INT),
        sample_weight=final_weights,
    )

    # Única evaluación de test, deliberadamente posterior a la selección.
    test_metrics, test_predictions = evaluate_partition(
        final_model, test, contract.predictor_columns
    )
    test_predictions.insert(0, "variant", selected_variant)
    test_predictions.to_csv(args.output_dir / "test_predictions.csv", index=False)

    ambiguous_probabilities = final_model.predict_proba(
        ambiguous.loc[:, list(contract.predictor_columns)]
    )[:, 1]
    ambiguous_output = ambiguous.loc[
        :, [SITE_COLUMN, YEAR_COLUMN, BLOCK_COLUMN, SPLIT_COLUMN, TARGET_COLUMN]
    ].copy()
    ambiguous_output["forest_vote_fraction"] = ambiguous_probabilities
    ambiguous_output.to_csv(args.output_dir / "ambiguous_predictions.csv", index=False)

    importances = pd.DataFrame(
        {
            "feature": contract.predictor_columns,
            "importance": final_model.feature_importances_,
        }
    ).sort_values("importance", ascending=False, ignore_index=True)
    importances.to_csv(args.output_dir / "feature_importance.csv", index=False)

    dataset_path = (
        csv_files[0]
        if len(csv_files) == 1
        else Path(str(manifest["concatenated_csv"]["path"])).resolve()
    )
    dataset_hash = str(manifest["concatenated_csv"]["sha256"])
    manifest_hash = sha256_file(args.manifest)
    artifact_metadata = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "model_name": "rf_forest_multiyear_2020_2024_v1",
        "jurisdiction": "entre_rios",
        "training_years": [2020, 2021, 2022, 2023, 2024],
        "task": "stable_forest_non_forest_proxy_classification",
        "not_a_deforestation_model": True,
        "selected_variant": selected_variant,
        "sample_weight": "none" if final_weights is None else "1/site_frequency",
        "random_seed": args.seed,
        "hyperparameters": parameters,
        "feature_columns": list(contract.predictor_columns),
        "feature_columns_sha256": contract.predictor_columns_sha256,
        "dataset_path": str(dataset_path),
        "dataset_sha256": dataset_hash,
        "training_manifest_path": str(args.manifest.resolve()),
        "training_manifest_sha256": manifest_hash,
        "source_csvs": input_records,
        "label_to_int": LABEL_TO_INT,
        "positive_class": POSITIVE_LABEL,
        "ambiguous_policy": "excluded_from_fit_and_binary_metrics_retained_for_diagnostics",
        "split_policy": {
            "selection": "train_fit_validation_select",
            "final_fit": "train_plus_validation",
            "test": "evaluated_once_after_selection",
            "spatial_group": BLOCK_COLUMN,
            "site_group": SITE_COLUMN,
            "block_size_m": 3000,
            "limitation": "spatial blocks are not a buffered regional holdout",
        },
        "selection": selection,
        "validation_metrics_reference": "metrics_comparison.json",
        "test_metrics": test_metrics,
        "final_fit": {
            "row_count": len(final_rows),
            "site_count": int(final_rows[SITE_COLUMN].nunique()),
            "block_count": int(final_rows[BLOCK_COLUMN].nunique()),
            "oob_accuracy": float(final_model.oob_score_),
        },
        "test": {
            "row_count": len(test),
            "site_count": int(test[SITE_COLUMN].nunique()),
            "block_count": int(test[BLOCK_COLUMN].nunique()),
            "metrics": test_metrics,
        },
        "ambiguous_row_count": len(ambiguous),
        "sklearn_version": sklearn.__version__,
        **git_provenance(),
    }
    model_bundle = {
        "model": final_model,
        "feature_columns": list(contract.predictor_columns),
        "label_to_int": LABEL_TO_INT,
        "int_to_label": INT_TO_LABEL,
        "positive_class": POSITIVE_LABEL,
        "metadata": artifact_metadata,
    }
    model_path = args.output_dir / "random_forest_forest_multiyear_2020_2024.joblib"
    joblib.dump(model_bundle, model_path)
    artifact_metadata["joblib_path"] = str(model_path.resolve())
    artifact_metadata["joblib_sha256"] = sha256_file(model_path)
    write_json(args.output_dir / "model_manifest.json", artifact_metadata)
    write_json(args.output_dir / "test_metrics.json", test_metrics)

    summary_rows: list[dict[str, Any]] = []
    for variant, payload in {
        **candidate_metrics,
        "baseline_2020_only": baseline,
    }.items():
        validation_metrics = payload["validation"]
        for year, metrics in validation_metrics["by_year"].items():
            summary_rows.append(
                {
                    "stage": "validation",
                    "variant": variant,
                    "year": year,
                    **{key: value for key, value in metrics.items() if not isinstance(value, list)},
                }
            )
    for year, metrics in test_metrics["by_year"].items():
        summary_rows.append(
            {
                "stage": "test",
                "variant": selected_variant,
                "year": year,
                **{key: value for key, value in metrics.items() if not isinstance(value, list)},
            }
        )
    pd.DataFrame(summary_rows).to_csv(args.output_dir / "metrics_by_year.csv", index=False)

    print(json.dumps({"selection": selection, "test": test_metrics}, indent=2))
    print(f"Modelo final: {model_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
