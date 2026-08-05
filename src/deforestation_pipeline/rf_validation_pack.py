 """Materialización reproducible del paquete ciego de validación RF 2020."""

from __future__ import annotations

import csv
import hashlib
import json
import urllib.request
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import Transformer

from deforestation_pipeline.config import load_config
from deforestation_pipeline.forest_rf import (
    build_geemap_tree_classifier,
    forest_rf_predictor_band_names,
)
from deforestation_pipeline.gee import authenticate_earth_engine_user_oauth
from deforestation_pipeline.rf_validation import (
    BLIND_FIELDS,
    GateThresholds,
    SampleQuotas,
    build_blind_and_sealed_rows,
    compute_validation_report,
    read_csv_rows,
    select_stratified_rows,
    write_csv_rows,
)

PACK_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
PRIVATE_ADDITIONAL_FIELDS = ("rf_prediction", "rf_vote_fraction")
QUALITY_FIELDS = (
    "blind_id",
    "valid_observation_count_2019",
    "valid_observation_count_2020",
    "valid_observation_count_2021",
    "visual_status",
)


class StrictPackModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhaseConfig(StrictPackModel):
    split: Literal["validation", "test"]
    quotas_per_utm: SampleQuotas


class ImageryConfig(StrictPackModel):
    collection_id: Literal["COPERNICUS/S2_SR_HARMONIZED"]
    years: tuple[Literal[2019, 2020, 2021], Literal[2019, 2020, 2021], Literal[2019, 2020, 2021]]
    reference_year: Literal[2020]
    context_radius_m: Annotated[int, Field(ge=240, le=1000)]
    scale_m: Literal[10]
    dimensions: str = Field(pattern=r"^[0-9]+x[0-9]+$")
    maximum_scene_cloud_pct: Annotated[int, Field(ge=0, le=100)]
    mask_method: Literal["SCL_and_QA60_v1"]
    rgb_bands: tuple[Literal["B4"], Literal["B3"], Literal["B2"]]
    false_color_bands: tuple[Literal["B8"], Literal["B4"], Literal["B3"]]
    rgb_min: float
    rgb_max: float
    false_color_min: float
    false_color_max: float
    gamma: Annotated[float, Field(gt=0)]
    concurrent_downloads: Annotated[int, Field(ge=1, le=12)]

    @model_validator(mode="after")
    def imagery_contract_is_consistent(self) -> ImageryConfig:
        if self.years != (2019, 2020, 2021):
            raise ValueError("years debe permanecer congelado en 2019, 2020 y 2021")
        if self.rgb_min >= self.rgb_max or self.false_color_min >= self.false_color_max:
            raise ValueError("los rangos de visualización deben ser crecientes")
        return self


class RfValidationPackConfig(StrictPackModel):
    schema_version: Literal["1.0.0"]
    pipeline_config_path: Path
    input_csv_paths: Annotated[tuple[Path, ...], Field(min_length=2, max_length=2)]
    output_directory: Path
    earth_engine_project: str = Field(min_length=1)
    seed: Annotated[int, Field(ge=0)]
    pilot: PhaseConfig
    test: PhaseConfig
    imagery: ImageryConfig
    gate: GateThresholds

    @model_validator(mode="after")
    def phases_are_frozen(self) -> RfValidationPackConfig:
        if self.pilot.split != "validation" or self.test.split != "test":
            raise ValueError("pilot debe usar validation y test debe usar test")
        if self.pilot.quotas_per_utm.total != 25:
            raise ValueError("el piloto debe seleccionar 25 puntos por UTM")
        if self.test.quotas_per_utm.total != 100:
            raise ValueError("el test debe seleccionar 100 puntos por UTM")
        return self


class ValidationPackResult(StrictPackModel):
    pack_directory: Path
    pilot_count: int
    test_count: int
    card_count: int
    contact_sheet_count: int
    manifest_path: Path
    private_table_path: Path
    pilot_adjudication_path: Path
    test_adjudication_path: Path


class ValidationEvaluationResult(StrictPackModel):
    report_path: Path
    confusion_matrix_path: Path
    passed: bool


def load_pack_config(path: Path) -> RfValidationPackConfig:
    """Carga el protocolo congelado sin mezclarlo con la config científica general."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("la configuración de validación debe ser un mapping YAML")
    return RfValidationPackConfig.model_validate(payload)


def prepare_validation_pack(
    config_path: Path,
    *,
    generated_at: datetime | None = None,
) -> ValidationPackResult:
    """Selecciona, predice y materializa 250 fichas Sentinel-2 completamente ciegas."""
    config = load_pack_config(config_path)
    created_at = generated_at or datetime.now(UTC)
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("generated_at debe incluir zona horaria")
    rows = read_csv_rows(config.input_csv_paths)
    pilot_selected = select_stratified_rows(
        rows,
        split=config.pilot.split,
        quotas=config.pilot.quotas_per_utm,
        seed=config.seed,
    )
    test_selected = select_stratified_rows(
        rows,
        split=config.test.split,
        quotas=config.test.quotas_per_utm,
        seed=config.seed,
    )
    if len(pilot_selected) != 50 or len(test_selected) != 200:
        raise ValueError(
            "los CSV reales no soportan el diseño congelado: "
            f"pilot={len(pilot_selected)}, test={len(test_selected)}"
        )
    pilot_blind, pilot_sealed = build_blind_and_sealed_rows(
        pilot_selected,
        phase="pilot",
        seed=config.seed,
    )
    test_blind, test_sealed = build_blind_and_sealed_rows(
        test_selected,
        phase="test",
        seed=config.seed,
    )
    sealed = [*pilot_sealed, *test_sealed]
    if {row["sample_id"] for row in pilot_sealed} & {row["sample_id"] for row in test_sealed}:
        raise ValueError("pilot y test no pueden compartir muestras")

    session = authenticate_earth_engine_user_oauth(project=config.earth_engine_project)
    module = session.module
    pipeline_config = load_config(config.pipeline_config_path)
    predictions = _predict_rf_rows(
        module=module,
        rows=sealed,
        forest_model_config=pipeline_config.forest_model,
    )
    for row in sealed:
        row.update(predictions[row["blind_id"]])

    annual_images = _build_sentinel2_yearly_images(module=module, config=config.imagery)
    quality = _sample_valid_counts(module=module, rows=sealed, annual_images=annual_images)
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    config_hash = _sha256(config_path)
    pack_directory = config.output_directory / f"rf-validation-2020__{stamp}__{config_hash[:10]}"
    public_directory = pack_directory / "public_blind_review"
    private_directory = pack_directory / "private_sealed_do_not_open_during_review"
    if pack_directory.exists():
        raise FileExistsError(f"el paquete ya existe: {pack_directory}")

    pilot_adjudication = public_directory / "pilot" / "adjudication_template.csv"
    test_adjudication = public_directory / "test" / "adjudication_template.csv"
    write_csv_rows(pilot_adjudication, pilot_blind, fieldnames=BLIND_FIELDS)
    write_csv_rows(test_adjudication, test_blind, fieldnames=BLIND_FIELDS)
    sealed_fields = tuple(sealed[0])
    write_csv_rows(private_directory / "sealed_sample.csv", sealed, fieldnames=sealed_fields)
    _write_rubric(public_directory / "REVIEW_INSTRUCTIONS.txt", config)

    card_paths, visual_status = _generate_all_cards(
        module=module,
        rows=sealed,
        quality=quality,
        annual_images=annual_images,
        imagery=config.imagery,
        public_directory=public_directory,
    )
    quality_rows = [
        {
            "blind_id": row["blind_id"],
            **quality[row["blind_id"]],
            "visual_status": visual_status[row["blind_id"]],
        }
        for row in sealed
    ]
    write_csv_rows(
        public_directory / "imagery_quality.csv",
        quality_rows,
        fieldnames=QUALITY_FIELDS,
    )
    sheets = _generate_contact_sheets(card_paths, public_directory=public_directory)
    manifest_path = pack_directory / "pack_manifest.json"
    _write_manifest(
        manifest_path,
        config_path=config_path,
        config=config,
        created_at=created_at,
        rows=sealed,
        quality=quality,
        visual_status=visual_status,
        card_paths=card_paths,
        contact_sheets=sheets,
        pack_directory=pack_directory,
        public_directory=public_directory,
        private_directory=private_directory,
    )
    return ValidationPackResult(
        pack_directory=pack_directory,
        pilot_count=len(pilot_blind),
        test_count=len(test_blind),
        card_count=len(card_paths),
        contact_sheet_count=len(sheets),
        manifest_path=manifest_path,
        private_table_path=private_directory / "sealed_sample.csv",
        pilot_adjudication_path=pilot_adjudication,
        test_adjudication_path=test_adjudication,
    )


def evaluate_validation_pack(
    pack_directory: Path,
    *,
    adjudication_path: Path | None = None,
) -> ValidationEvaluationResult:
    """Evalúa sólo el test bloqueado después de recibir adjudicaciones completas."""
    manifest_path = pack_directory / "pack_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise ValueError("pack_manifest.json debe contener un objeto")
    thresholds = GateThresholds.model_validate(manifest.get("gate"))
    sealed_path = pack_directory / "private_sealed_do_not_open_during_review" / "sealed_sample.csv"
    completed_path = adjudication_path or (
        pack_directory / "public_blind_review" / "test" / "adjudication_template.csv"
    )
    sealed = [row for row in _read_csv_table(sealed_path) if row.get("phase") == "test"]
    adjudications = _read_csv_table(completed_path)
    report = compute_validation_report(
        sealed_rows=sealed,
        adjudication_rows=adjudications,
        expected_test_size=200,
        thresholds=thresholds,
    )
    evaluation_directory = pack_directory / "evaluation"
    evaluation_directory.mkdir(parents=True, exist_ok=True)
    report_path = evaluation_directory / "validation_report.json"
    report_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    confusion_matrix_path = evaluation_directory / "confusion_matrix.png"
    _write_confusion_matrix(confusion_matrix_path, report.global_metrics)
    return ValidationEvaluationResult(
        report_path=report_path,
        confusion_matrix_path=confusion_matrix_path,
        passed=report.passed,
    )


def _predict_rf_rows(
    *,
    module: Any,
    rows: Sequence[Mapping[str, str]],
    forest_model_config: Any,
) -> dict[str, dict[str, str]]:
    predictors = forest_rf_predictor_band_names()
    features = [
        module.Feature(
            None,
            {
                "blind_id": row["blind_id"],
                **{name: float(row[name]) for name in predictors},
            },
        )
        for row in rows
    ]
    classifier = build_geemap_tree_classifier(module=module, config=forest_model_config)
    classified = (
        module.FeatureCollection(features)
        .classify(classifier, "rf_prediction")
        .classify(classifier.setOutputMode("RAW"), "raw_votes")
        .select(["blind_id", "rf_prediction", "raw_votes"])
        .getInfo()
    )
    remote_features = classified.get("features") if isinstance(classified, Mapping) else None
    if not isinstance(remote_features, list) or len(remote_features) != len(rows):
        raise RuntimeError("GEE no devolvió todas las predicciones RF selladas")
    predictions: dict[str, dict[str, str]] = {}
    for feature in remote_features:
        properties = feature.get("properties", {})
        blind_id = str(properties["blind_id"])
        votes = properties["raw_votes"]
        if not isinstance(votes, list) or len(votes) != forest_model_config.expected_tree_count:
            raise RuntimeError("RAW no devolvió exactamente 500 votos por muestra")
        prediction = int(properties["rf_prediction"])
        vote_fraction = sum(int(vote) for vote in votes) / len(votes)
        predictions[blind_id] = {
            "rf_prediction": str(prediction),
            "rf_vote_fraction": f"{vote_fraction:.6f}",
        }
    return predictions


def _build_sentinel2_yearly_images(*, module: Any, config: ImageryConfig) -> dict[int, Any]:
    images: dict[int, Any] = {}
    for year in config.years:
        collection = (
            module.ImageCollection(config.collection_id)
            .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
            .filter(module.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", config.maximum_scene_cloud_pct))
            .map(lambda image: _mask_sentinel2(module, image))
        )
        composite = collection.median().set("validation_year", year)
        valid_count = collection.select("B4").count().rename(f"valid_observation_count_{year}")
        images[year] = composite.addBands(valid_count)
    return images


def _mask_sentinel2(module: Any, image: Any) -> Any:
    scl = image.select("SCL")
    qa60 = image.select("QA60")
    valid = (
        scl.neq(0)
        .And(scl.neq(1))
        .And(scl.neq(3))
        .And(scl.neq(7))
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
        .And(qa60.bitwiseAnd(1 << 10).eq(0))
        .And(qa60.bitwiseAnd(1 << 11).eq(0))
    )
    return image.updateMask(valid).select(["B2", "B3", "B4", "B8"]).multiply(0.0001)


def _sample_valid_counts(
    *,
    module: Any,
    rows: Sequence[Mapping[str, str]],
    annual_images: Mapping[int, Any],
) -> dict[str, dict[str, str]]:
    count_image = annual_images[2019].select("valid_observation_count_2019")
    for year in (2020, 2021):
        count_image = count_image.addBands(
            annual_images[year].select(f"valid_observation_count_{year}")
        )
    points = module.FeatureCollection(
        [
            module.Feature(
                module.Geometry.Point([float(row["longitude"]), float(row["latitude"])]),
                {"blind_id": row["blind_id"]},
            )
            for row in rows
        ]
    )
    sampled = count_image.sampleRegions(
        collection=points,
        properties=["blind_id"],
        scale=10,
        geometries=False,
        tileScale=4,
    ).getInfo()
    features = sampled.get("features") if isinstance(sampled, Mapping) else None
    if not isinstance(features, list) or len(features) != len(rows):
        raise RuntimeError("GEE no devolvió QA óptica para todas las muestras")
    quality: dict[str, dict[str, str]] = {}
    for feature in features:
        props = feature["properties"]
        blind_id = str(props["blind_id"])
        quality[blind_id] = {
            f"valid_observation_count_{year}": str(int(props[f"valid_observation_count_{year}"]))
            for year in (2019, 2020, 2021)
        }
    return quality


def _generate_all_cards(
    *,
    module: Any,
    rows: Sequence[Mapping[str, str]],
    quality: Mapping[str, Mapping[str, str]],
    annual_images: Mapping[int, Any],
    imagery: ImageryConfig,
    public_directory: Path,
) -> tuple[list[Path], dict[str, str]]:
    results: list[Path] = []
    statuses: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=imagery.concurrent_downloads) as executor:
        futures = {
            executor.submit(
                _generate_card,
                module=module,
                row=row,
                quality=quality[row["blind_id"]],
                annual_images=annual_images,
                imagery=imagery,
                output_path=(
                    public_directory
                    / ("pilot" if row["phase"] == "pilot" else "test")
                    / "cards"
                    / f"{row['blind_id']}.png"
                ),
            ): (
                row["blind_id"],
                public_directory
                / ("pilot" if row["phase"] == "pilot" else "test")
                / "cards"
                / f"{row['blind_id']}.png",
            )
            for row in rows
        }
        for future in as_completed(futures):
            blind_id, expected_path = futures[future]
            try:
                results.append(future.result())
                statuses[blind_id] = "generated"
            except Exception:
                _write_failed_card(expected_path, blind_id=blind_id)
                results.append(expected_path)
                statuses[blind_id] = "failed"
    return sorted(results), statuses


def _generate_card(
    *,
    module: Any,
    row: Mapping[str, str],
    quality: Mapping[str, str],
    annual_images: Mapping[int, Any],
    imagery: ImageryConfig,
    output_path: Path,
) -> Path:
    lon = float(row["longitude"])
    lat = float(row["latitude"])
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    center_x, center_y = transformer.transform(lon, lat)
    radius = imagery.context_radius_m
    source = module.Geometry.Rectangle(
        [center_x - radius, center_y - radius, center_x + radius, center_y + radius],
        proj="EPSG:3857",
        geodesic=False,
    )
    outer = module.Geometry.Rectangle(
        [center_x - 4 * radius, center_y - radius, center_x + 4 * radius, center_y + radius],
        proj="EPSG:3857",
        geodesic=False,
    )
    rgb = [
        annual_images[year].visualize(
            bands=list(imagery.rgb_bands),
            min=imagery.rgb_min,
            max=imagery.rgb_max,
            gamma=imagery.gamma,
        )
        for year in imagery.years
    ]
    false_color = annual_images[imagery.reference_year].visualize(
        bands=list(imagery.false_color_bands),
        min=imagery.false_color_min,
        max=imagery.false_color_max,
        gamma=imagery.gamma,
    )
    offsets = (-3 * radius, -radius, radius, 3 * radius)
    panels = [
        image.clip(source)
        .reproject(crs="EPSG:3857", scale=imagery.scale_m)
        .translate(offset, 0, "meters")
        for image, offset in zip((*rgb, false_color), offsets, strict=True)
    ]
    thumbnail = module.ImageCollection.fromImages(panels).mosaic()
    url = thumbnail.getThumbURL(
        {
            "region": outer,
            "dimensions": imagery.dimensions,
            "crs": "EPSG:3857",
            "format": "png",
        }
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = output_path.with_suffix(".raw.png")
    _download_once(url, raw_path)
    try:
        _annotate_card(
            raw_path=raw_path,
            output_path=output_path,
            blind_id=row["blind_id"],
            quality=quality,
        )
    finally:
        raw_path.unlink(missing_ok=True)
    return output_path


def _download_once(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "rf-validation-pack/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        target.write_bytes(response.read())
    if target.stat().st_size < 1000:
        target.unlink(missing_ok=True)
        raise OSError("thumbnail GEE demasiado pequeño")


def _write_failed_card(path: Path, *, blind_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (1024, 318), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((20, 20), f"ID ciego: {blind_id}", fill="black")
    draw.text(
        (20, 150),
        "FICHA NO DISPONIBLE — marcar insufficient_evidence; no inferir cobertura.",
        fill="red",
    )
    canvas.save(path, format="PNG")


def _annotate_card(
    *,
    raw_path: Path,
    output_path: Path,
    blind_id: str,
    quality: Mapping[str, str],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw = Image.open(raw_path).convert("RGB")
    top = 34
    bottom = 28
    canvas = Image.new("RGB", (raw.width, raw.height + top + bottom), "white")
    canvas.paste(raw, (0, top))
    draw = ImageDraw.Draw(canvas)
    titles = ("RGB 2019", "RGB 2020", "RGB 2021", "NIR-R-G 2020")
    panel_width = raw.width / 4
    draw.text((8, 8), f"ID ciego: {blind_id}", fill="black")
    for index, title in enumerate(titles):
        x = int((index + 0.5) * panel_width)
        draw.text((x, 8), title, fill="black", anchor="ma")
        draw.line(
            (int(index * panel_width), top, int(index * panel_width), top + raw.height),
            fill="white",
            width=2,
        )
        center_y = top + raw.height // 2
        draw.line((x - 8, center_y, x + 8, center_y), fill="yellow", width=2)
        draw.line((x, center_y - 8, x, center_y + 8), fill="yellow", width=2)
    counts = ", ".join(
        f"{year}: {quality[f'valid_observation_count_{year}']} obs. válidas"
        for year in (2019, 2020, 2021)
    )
    draw.text((8, top + raw.height + 8), counts, fill="black")
    canvas.save(output_path, format="PNG", optimize=True)


def _generate_contact_sheets(card_paths: Sequence[Path], *, public_directory: Path) -> list[Path]:
    sheets: list[Path] = []
    grouped: dict[str, list[Path]] = {"pilot": [], "test": []}
    for card in card_paths:
        grouped["pilot" if "pilot" in card.parts else "test"].append(card)
    for phase, paths in grouped.items():
        for page, start in enumerate(range(0, len(paths), 25), start=1):
            subset = paths[start : start + 25]
            cells: list[Image.Image] = []
            for card_path in subset:
                card_image = Image.open(card_path).convert("RGB")
                cells.append(ImageOps.contain(card_image, (400, 124)))
            sheet = Image.new("RGB", (2000, 620), "white")
            for index, cell in enumerate(cells):
                x = (index % 5) * 400
                y = (index // 5) * 124
                sheet.paste(cell, (x, y))
            output = public_directory / phase / "contact_sheets" / f"page-{page:02d}.jpg"
            output.parent.mkdir(parents=True, exist_ok=True)
            sheet.save(output, format="JPEG", quality=88, optimize=True)
            sheets.append(output)
    return sheets


def _write_rubric(path: Path, config: RfValidationPackConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "VALIDACIÓN VISUAL CIEGA DEL RF FORESTAL 2020 — ENTRE RÍOS",
                "",
                "No abras private_sealed_do_not_open_during_review hasta cerrar las etiquetas.",
                "Las fichas no muestran RF, pseudolabels, productos forestales ni coordenadas.",
                "Cada cruz amarilla indica el píxel central evaluado; el contexto mide "
                f"{config.imagery.context_radius_m * 2} m de lado.",
                "Los paneles son medianas anuales Sentinel-2 SR Harmonized con máscara SCL+QA60;",
                "no son escenas únicas. Los conteos son observaciones válidas del píxel central.",
                "",
                "reference_label: forest | non_forest | uncertain",
                "confidence: high | medium | low",
                "reason_code: closed_woody | open_woody | pasture | crop | mixed_pixel |",
                "             water | infrastructure | insufficient_evidence | other",
                "",
                "Usá uncertain cuando la evidencia visual no permita una adjudicación defendible.",
                "Esta es una referencia visual de cobertura, no una determinación legal EUDR.",
                "El piloto congela la rúbrica y nunca participa de las métricas finales.",
                "El test permanece bloqueado y no se usa para ajustar el RF ni su umbral.",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _write_manifest(
    path: Path,
    *,
    config_path: Path,
    config: RfValidationPackConfig,
    created_at: datetime,
    rows: Sequence[Mapping[str, str]],
    quality: Mapping[str, Mapping[str, str]],
    visual_status: Mapping[str, str],
    card_paths: Sequence[Path],
    contact_sheets: Sequence[Path],
    pack_directory: Path,
    public_directory: Path,
    private_directory: Path,
) -> None:
    counts = Counter((row["phase"], row["utm_zone"], row["proxy_label"]) for row in rows)
    valid_counts = [
        int(quality[row["blind_id"]][f"valid_observation_count_{year}"])
        for row in rows
        for year in (2019, 2020, 2021)
    ]
    payload = {
        "schema_version": PACK_SCHEMA_VERSION,
        "created_at": created_at.isoformat(),
        "jurisdiction": "entre_rios",
        "reference_year": 2020,
        "selection_seed": config.seed,
        "selection_uses_rf_prediction": False,
        "pilot_used_in_final_metrics": False,
        "test_used_for_threshold_tuning": False,
        "blind_public_fields": list(BLIND_FIELDS),
        "private_fields_include": list(PRIVATE_ADDITIONAL_FIELDS),
        "sample_counts": {
            f"{phase}_utm{zone}_{label}": count
            for (phase, zone, label), count in sorted(counts.items())
        },
        "imagery": config.imagery.model_dump(mode="json"),
        "quality_summary": {
            "minimum_valid_observations": min(valid_counts),
            "maximum_valid_observations": max(valid_counts),
            "zero_valid_observation_values": sum(value == 0 for value in valid_counts),
            "generated_cards": sum(status == "generated" for status in visual_status.values()),
            "failed_cards": sum(status == "failed" for status in visual_status.values()),
        },
        "gate": config.gate.model_dump(mode="json"),
        "cards": {
            "count": len(card_paths),
            "sha256": {
                str(card.relative_to(public_directory)).replace("\\", "/"): _sha256(card)
                for card in card_paths
            },
        },
        "contact_sheets": {
            str(sheet.relative_to(public_directory)).replace("\\", "/"): _sha256(sheet)
            for sheet in contact_sheets
        },
        "files": {
            "config": {"path": str(config_path), "sha256": _sha256(config_path)},
            "public_adjudication_pilot": _file_record(
                public_directory / "pilot" / "adjudication_template.csv", pack_directory
            ),
            "public_adjudication_test": _file_record(
                public_directory / "test" / "adjudication_template.csv", pack_directory
            ),
            "public_quality": _file_record(
                public_directory / "imagery_quality.csv", pack_directory
            ),
            "private_sealed_sample": _file_record(
                private_directory / "sealed_sample.csv", pack_directory
            ),
        },
        "limitations": [
            "visual_reference_not_regulatory_forest_ground_truth",
            "annual_median_can_hide_short_lived_conditions",
            "tree_height_and_predominant_land_use_not_directly_observed",
            "rf_score_is_uncalibrated_vote_fraction",
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _file_record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _read_csv_table(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_confusion_matrix(path: Path, metrics: Any) -> None:
    cell_size = 220
    margin = 130
    canvas = Image.new("RGB", (margin + cell_size * 2, margin + cell_size * 2), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 20), "Predicción RF", fill="black")
    draw.text((10, margin), "Referencia", fill="black")
    draw.text((margin + cell_size // 2, 70), "no bosque", fill="black", anchor="ma")
    draw.text((margin + cell_size + cell_size // 2, 70), "bosque", fill="black", anchor="ma")
    values = (
        (metrics.true_negative, metrics.false_positive),
        (metrics.false_negative, metrics.true_positive),
    )
    labels = ("no bosque", "bosque")
    maximum = max((value for row in values for value in row), default=1) or 1
    for row_index, (label, row) in enumerate(zip(labels, values, strict=True)):
        draw.text(
            (10, margin + row_index * cell_size + cell_size // 2),
            label,
            fill="black",
            anchor="lm",
        )
        for column_index, value in enumerate(row):
            intensity = 245 - int(130 * value / maximum)
            x0 = margin + column_index * cell_size
            y0 = margin + row_index * cell_size
            draw.rectangle(
                (x0, y0, x0 + cell_size, y0 + cell_size),
                fill=(intensity, intensity, 255),
                outline="black",
                width=2,
            )
            draw.text(
                (x0 + cell_size // 2, y0 + cell_size // 2),
                str(value),
                fill="black",
                anchor="mm",
            )
    canvas.save(path, format="PNG")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
