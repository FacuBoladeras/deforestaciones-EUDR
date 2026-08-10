"""Convierte un Random Forest de scikit-learn y lo exporta como asset de GEE.

El script espera, de forma predeterminada, el bundle creado por
``train_random_forest_forest.py``::

    {
        "model": RandomForestClassifier(...),
        "feature_columns": [...],
        ...
    }

La conversión utiliza ``geemap.ml.rf_to_strings``. Los árboles se guardan
localmente en el formato de ``geemap.ml.trees_to_csv`` y, opcionalmente, se
exportan como una ``ee.FeatureCollection`` compatible con
``geemap.ml.fc_to_classifier``.

Instalación:
    python -m pip install --upgrade geemap earthengine-api scikit-learn joblib

Ejemplo — convertir y subir:
    python export_rf_to_gee_asset.py \
        rf_outputs/random_forest_forest.joblib \
        --project MI_PROYECTO_GCP \
        --asset-id models/rf_forest_2020 \
        --wait

El asset final será:
    projects/MI_PROYECTO_GCP/assets/models/rf_forest_2020

Seguridad:
    joblib puede ejecutar código al cargar un archivo. Use únicamente modelos
    propios o provenientes de una fuente confiable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import re
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import joblib
import numpy as np

TERMINAL_TASK_STATES = {"COMPLETED", "FAILED", "CANCELLED", "CANCELED"}
# ee.data impone 10 MiB al request JSON. Dejamos margen porque el payload real
# agrega geometrías, propiedades y estructura además del texto de los árboles.
GEE_CLIENT_LIMIT_MIB = 10.0
SAFE_PAYLOAD_MIB = 8.0
# Conservador frente a clientes/API históricos de Earth Engine: sólo ASCII
# alfanumérico, guion y underscore, con un máximo de 100 caracteres.
GEE_TASK_DESCRIPTION_MAX_CHARS = 100
GEE_TASK_DESCRIPTION_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def tree_multiset_sha256(trees: list[str]) -> str:
    """Identidad estable del contenido, independiente del orden del FeatureCollection."""
    tree_hashes = sorted(
        hashlib.sha256(tree.replace("\n", "#").encode("utf-8")).hexdigest() for tree in trees
    )
    payload = "\n".join(tree_hashes) + "\n"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def parse_args() -> argparse.Namespace:
    default_processes = max(1, min(4, (mp.cpu_count() or 2) - 1))

    parser = argparse.ArgumentParser(
        description=(
            "Convierte un RandomForest de scikit-learn a árboles compatibles "
            "con Earth Engine y los exporta como FeatureCollection asset."
        )
    )
    parser.add_argument(
        "model_path",
        type=Path,
        help="Ruta al .joblib generado por el script de entrenamiento.",
    )
    parser.add_argument(
        "--project",
        help=(
            "ID del proyecto de Google Cloud habilitado para Earth Engine. "
            "Es obligatorio al subir el asset."
        ),
    )
    parser.add_argument(
        "--asset-id",
        help=(
            "ID completo del asset o ruta relativa al root del proyecto. "
            "Ej.: models/rf_forest_2020 o "
            "projects/mi-proyecto/assets/models/rf_forest_2020."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("gee_rf_export"),
        help="Directorio para árboles, metadatos y ejemplos de uso.",
    )
    parser.add_argument(
        "--features-json",
        type=Path,
        help=(
            "JSON opcional con una lista de bandas o con la clave "
            "feature_columns. Solo se necesita si el .joblib no es un bundle."
        ),
    )
    parser.add_argument(
        "--output-mode",
        choices=("classification", "probability"),
        default="classification",
        help=(
            "Salida serializada: clase 0/1 o probabilidad de la clase positiva. "
            "Predeterminado: classification."
        ),
    )
    parser.add_argument(
        "--processes",
        type=int,
        default=default_processes,
        help=f"Procesos usados por geemap para convertir árboles (pred.: {default_processes}).",
    )
    parser.add_argument(
        "--description",
        help="Descripción de la tarea de exportación de Earth Engine.",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("gcloud", "localhost", "notebook", "appdefault"),
        help="Modo de ee.Authenticate() si todavía no hay credenciales válidas.",
    )
    parser.add_argument(
        "--convert-only",
        action="store_true",
        help="Genera los archivos locales, pero no inicia una exportación a GEE.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help=(
            "Espera, verifica staging y promueve el asset. Es obligatorio para "
            "exportaciones remotas seguras."
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=10,
        help="Intervalo de consulta de la tarea cuando se usa --wait.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Reemplazo transaccional best-effort: staging verificado, backup, "
            "rename y rollback. GEE no ofrece atomicidad entre esas llamadas."
        ),
    )
    parser.add_argument(
        "--allow-large-model",
        action="store_true",
        help=(
            "Opción legacy sin bypass: GEE limita cada request a 10 MiB y el "
            "script siempre aplica multipart automático con margen de 8 MiB."
        ),
    )
    return parser.parse_args()


def load_feature_names(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        features = data
    elif isinstance(data, dict) and isinstance(data.get("feature_columns"), list):
        features = data["feature_columns"]
    else:
        raise ValueError("--features-json debe contener una lista o un objeto con feature_columns.")
    return [str(feature) for feature in features]


def load_model_and_features(
    model_path: Path, features_json: Path | None
) -> tuple[Any, list[str], dict[str, Any]]:
    if not model_path.exists():
        raise FileNotFoundError(f"No existe el modelo: {model_path}")

    loaded = joblib.load(model_path)
    bundle_metadata: dict[str, Any] = {}

    if isinstance(loaded, dict) and "model" in loaded:
        model = loaded["model"]
        feature_names = loaded.get("feature_columns")
        bundle_metadata = {
            key: value for key, value in loaded.items() if key not in {"model", "feature_columns"}
        }
    else:
        model = loaded
        feature_names = None

    if features_json is not None:
        feature_names = load_feature_names(features_json)

    if not feature_names:
        raise ValueError(
            "No se encontró feature_columns dentro del joblib. Proporcione --features-json."
        )

    feature_names = [str(name) for name in feature_names]
    return model, feature_names, bundle_metadata


def validate_model(model: Any, feature_names: list[str], output_mode: str) -> None:
    required_attributes = ("estimators_", "classes_", "n_features_in_")
    missing = [name for name in required_attributes if not hasattr(model, name)]
    if missing:
        raise TypeError(
            f"El objeto no parece un RandomForestClassifier ajustado. Faltan atributos: {missing}"
        )

    if len(feature_names) != int(model.n_features_in_):
        raise ValueError(
            f"El modelo espera {model.n_features_in_} variables, pero se encontraron "
            f"{len(feature_names)} nombres de bandas."
        )

    if len(feature_names) != len(set(feature_names)):
        duplicates = sorted({name for name in feature_names if feature_names.count(name) > 1})
        raise ValueError(f"Hay nombres de bandas duplicados: {duplicates}")

    classes = np.asarray(model.classes_)
    if output_mode == "probability" and classes.size != 2:
        raise ValueError("El modo probability solo admite clasificación binaria.")

    if output_mode == "classification":
        try:
            numeric_classes = classes.astype(float)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Earth Engine requiere etiquetas numéricas en los nodos terminales."
            ) from exc

        if not np.all(np.isfinite(numeric_classes)) or not np.all(
            numeric_classes == np.floor(numeric_classes)
        ):
            raise ValueError(
                f"Las clases deben ser enteros numéricos; se encontraron {classes.tolist()}."
            )


def validate_pipeline_output_mode(output_mode: str) -> None:
    """Protege la semántica vigente: clase por árbol y fracción RAW en inferencia."""
    if output_mode != "classification":
        raise ValueError(
            "Este pipeline requiere --output-mode classification; el consumidor "
            "deriva la fracción de votos con classifier.setOutputMode('RAW')."
        )


def encoded_tree_bytes(trees: list[str]) -> int:
    """Tamaño mínimo del texto serializado con el separador usado por geemap."""
    return sum(len(tree.replace("\n", "#").encode("utf-8")) + 1 for tree in trees)


def plan_tree_chunks(trees: list[str], *, max_payload_bytes: int) -> list[list[str]]:
    """Agrupa árboles en orden sin superar el presupuesto seguro por request."""
    if max_payload_bytes < 1:
        raise ValueError("max_payload_bytes debe ser positivo")
    chunks: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for tree in trees:
        tree_size = encoded_tree_bytes([tree])
        if tree_size > max_payload_bytes:
            raise ValueError(
                "Un árbol individual supera el límite seguro de payload: "
                f"{tree_size} > {max_payload_bytes} bytes"
            )
        if current and current_size + tree_size > max_payload_bytes:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(tree)
        current_size += tree_size
    if current:
        chunks.append(current)
    return chunks


def new_run_token() -> str:
    """Token UUID sin separadores para aislar staging y partes de cada ejecución."""
    return uuid.uuid4().hex[:16]


def make_task_description(base: str, run_token: str, suffix: str) -> str:
    """Crea una descripción GEE segura que identifica inequívocamente el run."""
    if not GEE_TASK_DESCRIPTION_PATTERN.fullmatch(run_token):
        raise ValueError("run_token contiene caracteres inválidos")

    safe_base = re.sub(r"[^A-Za-z0-9_-]+", "_", base.strip()).strip("_-") or "gee"
    safe_suffix = re.sub(r"[^A-Za-z0-9_-]+", "_", suffix.strip()).strip("_-")
    tail = run_token if not safe_suffix else f"{run_token}_{safe_suffix}"
    if len(tail) >= GEE_TASK_DESCRIPTION_MAX_CHARS:
        raise ValueError("run_token y suffix exceden el límite de descripción GEE")

    base_budget = GEE_TASK_DESCRIPTION_MAX_CHARS - len(tail) - 1
    safe_base = safe_base[:base_budget].rstrip("_-") or "gee"
    description = f"{safe_base}_{tail}"
    if (
        len(description) > GEE_TASK_DESCRIPTION_MAX_CHARS
        or GEE_TASK_DESCRIPTION_PATTERN.fullmatch(description) is None
    ):
        raise RuntimeError("No se pudo construir una descripción de tarea GEE válida")
    return description


def run_scoped_asset_ids(
    destination_asset_id: str, run_token: str, part_count: int
) -> dict[str, Any]:
    """Deriva nombres temporales que nunca reutilizan los de otro reintento."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_token):
        raise ValueError("run_token contiene caracteres inválidos")
    if part_count < 0:
        raise ValueError("part_count no puede ser negativo")
    staging = f"{destination_asset_id}__staging_{run_token}"
    return {
        "staging": staging,
        "backup": f"{destination_asset_id}__backup_{run_token}",
        "parts": [f"{staging}__part_{index:03d}" for index in range(1, part_count + 1)],
    }


def cleanup_owned_assets(module: Any, asset_ids: list[str]) -> dict[str, Any]:
    """Limpia sólo IDs registrados por el run; tolera faltantes y reintentos."""
    deleted: list[str] = []
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for asset_id in asset_ids:
        if asset_id in seen:
            continue
        seen.add(asset_id)
        try:
            if get_asset_or_none(module, asset_id) is None:
                missing.append(asset_id)
                continue
            module.data.deleteAsset(asset_id)
            deleted.append(asset_id)
        except Exception as exc:
            errors.append({"asset_id": asset_id, "error": str(exc)})
    return {"deleted": deleted, "missing": missing, "errors": errors}


def verify_tree_asset(
    module: Any,
    asset_id: str,
    expected_tree_count: int,
    expected_tree_multiset_sha256: str | None = None,
) -> dict[str, Any]:
    """Valida tipo, propiedad, conteo y fingerprint de los árboles remotos."""
    asset_info = module.data.getAsset(asset_id)
    if str(asset_info.get("type")) != "TABLE":
        raise RuntimeError(f"El asset {asset_id} no es TABLE")
    collection = module.FeatureCollection(asset_id)
    tree_count = int(collection.size().getInfo())
    if tree_count != expected_tree_count:
        raise RuntimeError(
            f"El asset {asset_id} contiene {tree_count} árboles; se esperaban {expected_tree_count}"
        )
    properties = list(module.Feature(collection.first()).propertyNames().getInfo())
    if "tree" not in properties:
        raise RuntimeError(f"El asset {asset_id} no contiene la propiedad 'tree'")
    remote_trees = list(collection.aggregate_array("tree").getInfo())
    if len(remote_trees) != expected_tree_count or not all(
        isinstance(tree, str) for tree in remote_trees
    ):
        raise RuntimeError(f"El asset {asset_id} no devolvió árboles serializados válidos")
    actual_fingerprint = tree_multiset_sha256(remote_trees)
    if (
        expected_tree_multiset_sha256 is not None
        and actual_fingerprint != expected_tree_multiset_sha256
    ):
        raise RuntimeError(f"El contenido de árboles del asset {asset_id} no coincide")
    return {
        "asset": asset_info,
        "tree_count": tree_count,
        "expected_tree_count": expected_tree_count,
        "tree_property": "tree",
        "tree_multiset_sha256": actual_fingerprint,
        "expected_tree_multiset_sha256": expected_tree_multiset_sha256,
        "content_verified": expected_tree_multiset_sha256 is not None,
        "first_feature_properties": properties,
        "verified": True,
    }


def promote_staging_asset(
    *,
    module: Any,
    staging_asset_id: str,
    destination_asset_id: str,
    overwrite: bool,
    run_token: str,
    expected_tree_count: int,
    verify_asset: Callable[[str, int], dict[str, Any]],
    owned_asset_ids: list[str],
) -> dict[str, Any]:
    """Promueve staging; overwrite usa backup+rename+rollback, NO atomicidad real."""
    existing = get_asset_or_none(module, destination_asset_id)
    if existing is not None and not overwrite:
        raise FileExistsError(
            f"El asset destino ya existe y no será modificado: {destination_asset_id}"
        )

    if existing is None:
        module.data.renameAsset(staging_asset_id, destination_asset_id)
        try:
            verification = verify_asset(destination_asset_id, expected_tree_count)
        except Exception:
            if get_asset_or_none(module, destination_asset_id) is not None:
                module.data.deleteAsset(destination_asset_id)
            raise
        return {
            "strategy": "rename_new_destination",
            "atomic": False,
            "rollback_required": False,
            "verification": verification,
        }

    backup_asset_id = f"{destination_asset_id}__backup_{run_token}"
    if get_asset_or_none(module, backup_asset_id) is not None:
        raise FileExistsError(f"El backup del run ya existe: {backup_asset_id}")
    module.data.renameAsset(destination_asset_id, backup_asset_id)
    try:
        module.data.renameAsset(staging_asset_id, destination_asset_id)
        verification = verify_asset(destination_asset_id, expected_tree_count)
    except Exception as promotion_error:
        try:
            if get_asset_or_none(module, destination_asset_id) is not None:
                module.data.deleteAsset(destination_asset_id)
            if get_asset_or_none(module, backup_asset_id) is None:
                raise RuntimeError("el backup desapareció antes del rollback")
            module.data.renameAsset(backup_asset_id, destination_asset_id)
        except Exception as rollback_error:
            raise RuntimeError(
                "Falló la promoción y también el rollback: "
                f"promotion={promotion_error}; rollback={rollback_error}"
            ) from promotion_error
        raise
    owned_asset_ids.append(backup_asset_id)
    return {
        "strategy": "backup_then_rename_with_best_effort_rollback",
        "atomic": False,
        "rollback_required": True,
        "backup_asset_id": backup_asset_id,
        "verification": verification,
    }


def import_gee_modules() -> tuple[Any, Any]:
    try:
        import ee
        from geemap import ml
    except ImportError as exc:
        raise RuntimeError(
            "Faltan dependencias. Ejecute:\n"
            "python -m pip install --upgrade geemap earthengine-api "
            "scikit-learn joblib"
        ) from exc
    return ee, ml


def normalize_asset_id(asset_id: str, project: str | None) -> str:
    cleaned = asset_id.strip().strip("/")
    if cleaned.startswith("projects/") or cleaned.startswith("users/"):
        return cleaned
    if not project:
        raise ValueError("Una ruta relativa en --asset-id requiere también --project.")
    return f"projects/{project}/assets/{cleaned}"


def initialize_earth_engine(ee: Any, project: str, auth_mode: str | None) -> None:
    try:
        ee.Initialize(project=project)
        return
    except Exception as first_error:
        print("No se pudo inicializar con las credenciales actuales.")
        print(f"Detalle inicial: {first_error}")

    auth_kwargs = {"auth_mode": auth_mode} if auth_mode else {}
    ee.Authenticate(**auth_kwargs)
    ee.Initialize(project=project)


def get_asset_or_none(ee: Any, asset_id: str) -> dict[str, Any] | None:
    try:
        return cast(dict[str, Any], ee.data.getAsset(asset_id))
    except Exception as exc:
        message = str(exc).lower()
        if "not found" in message or "does not exist" in message or "404" in message:
            return None
        raise


def ensure_project_folders(ee: Any, asset_id: str) -> None:
    """Crea carpetas intermedias para assets modernos basados en proyectos."""
    match = re.fullmatch(r"(projects/[^/]+/assets)(?:/(.+))?", asset_id)
    if not match:
        # Los assets legacy se dejan a cargo del usuario para evitar asumir su root.
        return

    root, relative = match.groups()
    if not relative or "/" not in relative:
        return

    folder_parts = relative.split("/")[:-1]
    current = root
    for part in folder_parts:
        current = f"{current}/{part}"
        if get_asset_or_none(ee, current) is None:
            print(f"Creando carpeta de asset: {current}")
            ee.data.createAsset({"type": "FOLDER"}, current)


def find_task_by_description(ee: Any, description: str) -> dict[str, Any] | None:
    tasks = ee.data.getTaskList()
    matches = [task for task in tasks if task.get("description") == description]
    if not matches:
        return None

    def timestamp(task: dict[str, Any]) -> int:
        return int(task.get("creation_timestamp_ms") or task.get("update_timestamp_ms") or 0)

    return cast(dict[str, Any], max(matches, key=timestamp))


def wait_for_task_registration(
    ee: Any, description: str, timeout_seconds: int = 30
) -> dict[str, Any] | None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        task = find_task_by_description(ee, description)
        if task is not None:
            return task
        time.sleep(1)
    return None


def wait_for_task(ee: Any, task_id: str, poll_seconds: int) -> dict[str, Any]:
    while True:
        statuses = ee.data.getTaskStatus(task_id)
        status = statuses[0] if statuses else {"state": "UNKNOWN"}
        state = str(status.get("state", "UNKNOWN")).upper()
        print(f"Estado de la tarea: {state}")

        if state in TERMINAL_TASK_STATES:
            return status
        time.sleep(max(1, poll_seconds))


def json_safe(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def write_usage_examples(
    output_dir: Path,
    asset_id: str | None,
    project: str | None,
    feature_names: list[str],
    output_mode: str,
) -> None:
    asset_literal = asset_id or "projects/MI_PROYECTO/assets/models/MI_MODELO_RF"
    project_literal = project or "MI_PROYECTO"
    output_band = "forest_class" if output_mode == "classification" else "forest_probability"

    python_code = f'''"""Ejemplo de uso del Random Forest persistido en Earth Engine."""
import ee
from geemap import ml

ee.Initialize(project={project_literal!r})

ASSET_ID = {asset_literal!r}
FEATURES = {json.dumps(feature_names, indent=4, ensure_ascii=False)}

# Reemplace esta imagen por el cubo predictor con las 56 bandas exactas.
predictor_image = ee.Image("REEMPLAZAR/ASSET/DE/IMAGEN")

rf_fc = ee.FeatureCollection(ASSET_ID)
classifier = ml.fc_to_classifier(rf_fc)

# El orden y los nombres deben coincidir exactamente con el entrenamiento.
result = (
    predictor_image
    .select(FEATURES)
    .classify(classifier)
    .rename({output_band!r})
)

print("Bandas faltantes:", ee.List(FEATURES).removeAll(predictor_image.bandNames()).getInfo())
print(result.getInfo())
'''
    (output_dir / "gee_use_classifier_python.py").write_text(python_code, encoding="utf-8")

    js_features = json.dumps(feature_names, indent=2, ensure_ascii=False)
    javascript_code = f"""// Ejemplo para el Code Editor de Google Earth Engine.
var assetId = {json.dumps(asset_literal)};
var features = {js_features};

var rfFc = ee.FeatureCollection(assetId);
var treeStrings = rfFc.aggregate_array('tree').map(function(tree) {{
  return ee.String(tree).replace('#', '\\n', 'g');
}});
var classifier = ee.Classifier.decisionTreeEnsemble(treeStrings);

// Reemplace por el cubo predictor con las mismas bandas y escalas.
var predictorImage = ee.Image('REEMPLAZAR/ASSET/DE/IMAGEN');
var result = predictorImage
  .select(features)
  .classify(classifier)
  .rename({json.dumps(output_band)});

print('Bandas faltantes', ee.List(features).removeAll(predictorImage.bandNames()));
Map.addLayer(result, {{min: 0, max: 1}}, {json.dumps(output_band)});
"""
    (output_dir / "gee_use_classifier_code_editor.js").write_text(javascript_code, encoding="utf-8")


def main() -> int:
    args = parse_args()

    if args.processes < 1:
        raise ValueError("--processes debe ser al menos 1.")
    if args.poll_seconds < 1:
        raise ValueError("--poll-seconds debe ser al menos 1.")
    if not args.convert_only and not args.project:
        raise ValueError("--project es obligatorio salvo que use --convert-only.")
    if not args.convert_only and not args.asset_id:
        raise ValueError("--asset-id es obligatorio salvo que use --convert-only.")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    model, feature_names, bundle_metadata = load_model_and_features(
        args.model_path, args.features_json
    )
    validate_model(model, feature_names, args.output_mode)
    validate_pipeline_output_mode(args.output_mode)

    print(f"Modelo: {args.model_path.resolve()}")
    print(f"Árboles: {len(model.estimators_)}")
    print(f"Variables: {len(feature_names)}")
    print(f"Clases sklearn: {np.asarray(model.classes_).tolist()}")
    print(f"Modo de salida: {args.output_mode}")

    ee, ml = import_gee_modules()

    print("Convirtiendo árboles con geemap.ml.rf_to_strings...")
    trees = ml.rf_to_strings(
        model,
        feature_names,
        processes=args.processes,
        output_mode=args.output_mode.upper(),
    )

    if len(trees) != len(model.estimators_):
        raise RuntimeError(
            f"Se esperaban {len(model.estimators_)} árboles y se obtuvieron {len(trees)}."
        )
    expected_tree_multiset_sha256 = tree_multiset_sha256(trees)

    trees_path = args.output_dir / f"rf_trees_{args.output_mode}.csv"
    ml.trees_to_csv(trees, str(trees_path))

    encoded_bytes = encoded_tree_bytes(trees)
    encoded_mib = encoded_bytes / (1024**2)
    upload_chunks = plan_tree_chunks(
        trees,
        max_payload_bytes=int(SAFE_PAYLOAD_MIB * 1024**2),
    )

    print(f"Árboles serializados: {trees_path.resolve()}")
    print(f"Tamaño serializado aproximado: {encoded_mib:.2f} MiB")

    if len(upload_chunks) > 1:
        print(
            "El modelo excede el payload seguro de una llamada; se subirán "
            f"{len(upload_chunks)} partes temporales y se unirán del lado servidor."
        )
    if encoded_mib >= GEE_CLIENT_LIMIT_MIB and args.allow_large_model:
        print(
            "--allow-large-model no desactiva el límite remoto; se mantiene la "
            "carga por partes para preservar los 500 árboles."
        )

    normalized_asset_id = None
    if args.asset_id:
        normalized_asset_id = normalize_asset_id(args.asset_id, args.project)

    metadata = {
        "created_utc": datetime.now(UTC).isoformat(),
        "source_model": str(args.model_path.resolve()),
        "asset_id": normalized_asset_id,
        "project": args.project,
        "output_mode": args.output_mode,
        "n_trees": len(trees),
        "n_features": len(feature_names),
        "feature_columns": feature_names,
        "classes": np.asarray(model.classes_).tolist(),
        "serialized_size_bytes": encoded_bytes,
        "serialized_size_mib": encoded_mib,
        "gee_request_limit_mib": GEE_CLIENT_LIMIT_MIB,
        "safe_chunk_payload_mib": SAFE_PAYLOAD_MIB,
        "upload_chunk_count": len(upload_chunks),
        "upload_chunk_tree_counts": [len(chunk) for chunk in upload_chunks],
        "upload_chunk_sizes_bytes": [encoded_tree_bytes(chunk) for chunk in upload_chunks],
        "tree_multiset_sha256": expected_tree_multiset_sha256,
        "large_model_policy": (
            "automatic_multipart_per_request_at_or_below_8_mib; "
            "gee_request_limit_10_mib_cannot_be_bypassed"
        ),
        "overwrite_semantics": (
            "verified_staging_then_backup_rename_with_best_effort_rollback_not_atomic"
        ),
        "bundle_metadata": bundle_metadata,
    }
    metadata_path = args.output_dir / "rf_gee_metadata.json"
    metadata_path.write_text(
        json.dumps(json_safe(metadata), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    write_usage_examples(
        args.output_dir,
        normalized_asset_id,
        args.project,
        feature_names,
        args.output_mode,
    )

    if args.convert_only:
        print("Conversión local finalizada. No se inició ninguna tarea en GEE.")
        return 0

    if not args.wait:
        raise ValueError(
            "Las exportaciones remotas requieren --wait: el destino sólo se "
            "promueve después de verificar staging y completar cleanup."
        )

    assert args.project is not None
    assert normalized_asset_id is not None

    initialize_earth_engine(ee, args.project, args.auth_mode)
    ensure_project_folders(ee, normalized_asset_id)

    existing = get_asset_or_none(ee, normalized_asset_id)
    if existing is not None and not args.overwrite:
        raise FileExistsError(
            f"El asset ya existe y no será modificado: {normalized_asset_id}. "
            "Use otro nombre o --overwrite para staging+backup+rollback best-effort."
        )

    run_token = new_run_token()
    scoped_ids = run_scoped_asset_ids(
        normalized_asset_id,
        run_token,
        len(upload_chunks) if len(upload_chunks) > 1 else 0,
    )
    staging_asset_id = str(scoped_ids["staging"])
    planned_part_assets = [str(value) for value in scoped_ids["parts"]]
    description_base = args.description or (
        "geemap_rf_" + re.sub(r"[^A-Za-z0-9_-]+", "_", normalized_asset_id.rsplit("/", 1)[-1])
    )

    task_info_path = args.output_dir / "gee_export_task.json"
    run_info_path = args.output_dir / f"gee_export_run_{run_token}.json"
    part_assets: list[str] = []
    part_tasks: list[dict[str, Any]] = []
    owned_asset_ids: list[str] = []
    cleanup_report: dict[str, Any] = {"deleted": [], "missing": [], "errors": []}
    verification: dict[str, Any] | None = None
    operation_error: Exception | None = None
    run_record: dict[str, Any] = {
        "schema_version": "1.0.0",
        "run_token": run_token,
        "started_at": datetime.now(UTC).isoformat(),
        "destination_asset_id": normalized_asset_id,
        "staging_asset_id": staging_asset_id,
        "planned_part_asset_ids": planned_part_assets,
        "overwrite_requested": bool(args.overwrite),
        "overwrite_atomic": False,
        "overwrite_strategy": ("verified_staging_then_backup_rename_with_best_effort_rollback"),
        "task_description_base": description_base,
        "effective_task_descriptions": [],
        "status": "running",
    }
    try:
        if len(upload_chunks) == 1:
            stage_description = make_task_description(description_base, run_token, "staging")
            run_record["effective_task_descriptions"].append(stage_description)
            owned_asset_ids.append(staging_asset_id)
            print(f"Subiendo staging: {staging_asset_id}")
            ml.export_trees_to_fc(trees, staging_asset_id, description=stage_description)
            registered = wait_for_task_registration(ee, stage_description)
            if registered is None:
                raise RuntimeError(f"No se registró la tarea {stage_description}")
            task_id = str(registered.get("id") or registered.get("task_id"))
            final_status = wait_for_task(ee, task_id, args.poll_seconds)
        else:
            for index, (chunk, part_asset) in enumerate(
                zip(upload_chunks, planned_part_assets, strict=True), start=1
            ):
                part_description = make_task_description(
                    description_base, run_token, f"part_{index:03d}"
                )
                run_record["effective_task_descriptions"].append(part_description)
                owned_asset_ids.append(part_asset)
                print(
                    f"Subiendo parte {index}/{len(upload_chunks)}: "
                    f"{len(chunk)} árboles -> {part_asset}"
                )
                ml.export_trees_to_fc(chunk, part_asset, description=part_description)
                registered = wait_for_task_registration(ee, part_description)
                if registered is None:
                    raise RuntimeError(f"No se registró la tarea {part_description}")
                part_task_id = str(registered.get("id") or registered.get("task_id"))
                part_status = wait_for_task(ee, part_task_id, args.poll_seconds)
                part_tasks.append(part_status)
                part_state = str(part_status.get("state", "UNKNOWN")).upper()
                if part_state != "COMPLETED":
                    raise RuntimeError(
                        f"La parte {index} terminó con estado {part_state}: "
                        f"{part_status.get('error_message', '')}"
                    )
                part_assets.append(part_asset)
                (args.output_dir / "gee_export_parts.json").write_text(
                    json.dumps(json_safe(part_tasks), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            merged = ee.FeatureCollection(part_assets[0])
            for part_asset in part_assets[1:]:
                merged = merged.merge(ee.FeatureCollection(part_asset))
            stage_description = make_task_description(description_base, run_token, "staging")
            run_record["effective_task_descriptions"].append(stage_description)
            owned_asset_ids.append(staging_asset_id)
            print(f"Uniendo partes en staging: {staging_asset_id}")
            stage_task = ee.batch.Export.table.toAsset(
                collection=merged,
                description=stage_description,
                assetId=staging_asset_id,
            )
            stage_task.start()
            task_id = str(stage_task.id)
            final_status = wait_for_task(ee, task_id, args.poll_seconds)

        task_info_path.write_text(
            json.dumps(json_safe(final_status), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        final_state = str(final_status.get("state", "UNKNOWN")).upper()
        if final_state != "COMPLETED":
            raise RuntimeError(
                f"La exportación a staging terminó {final_state}: "
                f"{final_status.get('error_message', '')}"
            )

        staging_verification = verify_tree_asset(
            ee,
            staging_asset_id,
            len(trees),
            expected_tree_multiset_sha256,
        )
        promotion = promote_staging_asset(
            module=ee,
            staging_asset_id=staging_asset_id,
            destination_asset_id=normalized_asset_id,
            overwrite=bool(args.overwrite),
            run_token=run_token,
            expected_tree_count=len(trees),
            verify_asset=lambda asset_id, count: verify_tree_asset(
                ee,
                asset_id,
                count,
                expected_tree_multiset_sha256,
            ),
            owned_asset_ids=owned_asset_ids,
        )
        final_verification = cast(dict[str, Any], promotion["verification"])
        verification = {
            **final_verification,
            "feature_columns": feature_names,
            "upload_chunk_count": len(upload_chunks),
            "temporary_part_assets": part_assets,
            "run_token": run_token,
            "staging_verification": staging_verification,
            "promotion": {key: value for key, value in promotion.items() if key != "verification"},
        }
        run_record["status"] = "completed"
    except Exception as exc:
        operation_error = exc
        run_record["status"] = "failed"
        run_record["error"] = str(exc)
        raise
    finally:
        cleanup_report = cleanup_owned_assets(ee, owned_asset_ids)
        run_record["finished_at"] = datetime.now(UTC).isoformat()
        run_record["owned_asset_ids"] = owned_asset_ids
        run_record["cleanup"] = cleanup_report
        run_info_path.write_text(
            json.dumps(json_safe(run_record), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if cleanup_report["errors"] and operation_error is None:
            raise RuntimeError(
                f"La promoción terminó, pero el cleanup dejó errores: {cleanup_report['errors']}"
            )

    assert verification is not None
    verification["cleanup"] = cleanup_report
    (args.output_dir / "gee_asset_verification.json").write_text(
        json.dumps(json_safe(verification), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Asset creado y verificado: {normalized_asset_id}")
    print(f"Árboles en el asset: {verification['tree_count']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Proceso cancelado por el usuario.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
