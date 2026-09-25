"""Descarga y QA local de la línea base forestal en un bundle compacto."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Literal

import matplotlib
import numpy as np
from matplotlib.figure import Figure
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from deforestation_pipeline.catalog import ForestBaselineSourcePlan
from deforestation_pipeline.config import OutputConfig
from deforestation_pipeline.forest_baseline import forest_baseline_output_paths
from deforestation_pipeline.forest_baseline_features import (
    ForestBaselineFeatureMetadata,
)
from deforestation_pipeline.forest_baseline_products import ForestBaselineImages
from deforestation_pipeline.forest_rf import (
    FOREST_RF_CLASS_BAND,
    FOREST_RF_INPUT_COMPLETE_BAND,
    FOREST_RF_VOTE_FRACTION_BAND,
    ForestRfImages,
)
from deforestation_pipeline.forest_screening import (
    ForestScreeningConfig,
    ForestScreeningDomain,
    ScreeningDomainCode,
    build_forest_screening_domain,
)
from deforestation_pipeline.raster_products import (
    DirectDownloadEstimate,
    FetchBytes,
    RasterDownloadError,
    RasterValidation,
    materialize_ee_image_to_grid,
)
from deforestation_pipeline.schemas import RasterGridSpec

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt


@dataclass(frozen=True, slots=True)
class ForestBaselineMaterialization:
    """Artefactos, estimaciones y validaciones de la línea base."""

    files: Mapping[str, bytes]
    estimates: tuple[DirectDownloadEstimate, ...]
    validations: tuple[RasterValidation, ...]
    grid_spec: RasterGridSpec
    screening_domain: ForestScreeningDomain


def materialize_forest_baseline(
    *,
    images: ForestBaselineImages,
    rf_images: ForestRfImages,
    feature_metadata: ForestBaselineFeatureMetadata,
    source_plan: ForestBaselineSourcePlan,
    screening_config: ForestScreeningConfig,
    output_config: OutputConfig,
    grid_spec: RasterGridSpec,
    fetch_bytes: FetchBytes | None = None,
) -> ForestBaselineMaterialization:
    """Materializa baseline y RF 2020 en un bundle auditable común."""
    paths = forest_baseline_output_paths()
    specs: tuple[
        tuple[str, Any, tuple[str, ...], Literal["float32", "int16"]],
        ...,
    ] = (
        (
            "features",
            images.features,
            feature_metadata.feature_band_names,
            "float32",
        ),
        (
            "source_evidence",
            images.source_evidence,
            tuple(source.source_id for source in source_plan.sources),
            "int16",
        ),
        (
            "evidence_fraction",
            images.evidence_fraction,
            ("forest_evidence_fraction_2020",),
            "float32",
        ),
        (
            "consensus",
            images.consensus,
            ("forest_consensus_2020",),
            "int16",
        ),
        (
            "disagreement",
            images.disagreement,
            ("forest_disagreement_2020",),
            "int16",
        ),
        (
            "source_count",
            images.source_count,
            ("forest_core_source_count_2020",),
            "int16",
        ),
        (
            "rf_class",
            rf_images.forest_class,
            (FOREST_RF_CLASS_BAND,),
            "int16",
        ),
        (
            "rf_vote_fraction",
            rf_images.forest_vote_fraction,
            (FOREST_RF_VOTE_FRACTION_BAND,),
            "float32",
        ),
        (
            "rf_input_complete",
            rf_images.input_complete,
            (FOREST_RF_INPUT_COMPLETE_BAND,),
            "int16",
        ),
    )
    files: dict[str, bytes] = {}
    estimates: list[DirectDownloadEstimate] = []
    validations: list[RasterValidation] = []
    for key, image, band_names, output_type in specs:
        try:
            raster = materialize_ee_image_to_grid(
                image=image,
                band_names=band_names,
                artifact_path=paths[key],
                download_name=paths[key].rsplit("/", 1)[-1].removesuffix(".tif"),
                output_config=output_config,
                grid_spec=grid_spec,
                output_type=output_type,
                fetch_bytes=fetch_bytes,
            )
        except RasterDownloadError as error:
            raise RasterDownloadError(f"forest_baseline_{key}:{error.code}") from None
        files[paths[key]] = raster.content
        estimates.append(raster.estimate)
        validations.append(raster.validation)

    source_count_values, _ = _read_raster(files[paths["source_count"]])
    consensus_values, _ = _read_raster(files[paths["consensus"]])
    disagreement_values, _ = _read_raster(files[paths["disagreement"]])
    evidence_fraction_values, _ = _read_raster(files[paths["evidence_fraction"]])
    rf_vote_values, _ = _read_raster(files[paths["rf_vote_fraction"]])
    rf_complete_values, _ = _read_raster(files[paths["rf_input_complete"]])
    screening = build_forest_screening_domain(
        source_count=_float_values(source_count_values[0]),
        consensus_forest=_float_values(consensus_values[0]),
        disagreement=_float_values(disagreement_values[0]),
        evidence_fraction=_float_values(evidence_fraction_values[0]),
        rf_vote_fraction=_float_values(rf_vote_values[0]),
        rf_input_complete=_float_values(rf_complete_values[0]),
        grid_spec=grid_spec,
        config=screening_config,
    )
    for key, values, band_name in (
        (
            "automated_evaluable",
            screening.automated_evaluable,
            "automated_evaluable_2020",
        ),
        ("review_required", screening.review_required, "review_required_2020"),
        ("insufficient_data", screening.insufficient_data, "insufficient_data_2020"),
    ):
        files[paths[key]] = _binary_mask_tiff(
            values=values,
            aoi_footprint=screening.aoi_footprint,
            band_name=band_name,
            grid_spec=grid_spec,
        )
    files[paths["screening_figure"]] = _render_screening_qa(
        screening=screening,
        dpi=output_config.png_dpi,
    )

    files[paths["qa_figure"]] = _render_baseline_qa(
        evidence_fraction=files[paths["evidence_fraction"]],
        consensus=files[paths["consensus"]],
        disagreement=files[paths["disagreement"]],
        source_evidence=files[paths["source_evidence"]],
        rf_class=files[paths["rf_class"]],
        rf_vote_fraction=files[paths["rf_vote_fraction"]],
        rf_input_complete=files[paths["rf_input_complete"]],
        source_ids=tuple(source.source_id for source in source_plan.sources),
        dpi=output_config.png_dpi,
    )
    metadata = {
        **images.metadata.model_dump(mode="json"),
        "feature_stack": feature_metadata.model_dump(mode="json"),
        "grid": grid_spec.model_dump(mode="json"),
        "sources": [source.model_dump(mode="json") for source in source_plan.sources],
        "random_forest": {
            **rf_images.metadata.model_dump(mode="json"),
            "assets": {
                "class": paths["rf_class"],
                "vote_fraction": paths["rf_vote_fraction"],
                "input_complete": paths["rf_input_complete"],
            },
        },
        "screening": {
            "schema_version": screening_config.schema_version,
            "rules": screening_config.model_dump(mode="json"),
            "domain_code_semantics": {member.name: int(member) for member in ScreeningDomainCode},
            "metrics": screening.metrics.model_dump(mode="json"),
            "assets": {
                "automated_evaluable": paths["automated_evaluable"],
                "review_required": paths["review_required"],
                "insufficient_data": paths["insufficient_data"],
                "qa_figure": paths["screening_figure"],
            },
            "final_assessment_generated": False,
            "automatic_statement_scope": "automated_forest_high_confidence",
            "automatic_statement_policy": "only_when_no_signal_in_automated_forest",
        },
        "deferred_source_ids": list(source_plan.deferred_source_ids),
        "assets": paths,
        "download_method": "ee.Image.getDownloadURL",
        "resolution_degraded": False,
        "raster_validations": [validation.model_dump(mode="json") for validation in validations],
        "download_estimates": [estimate.model_dump(mode="json") for estimate in estimates],
        "limitations": [
            "La fracción de evidencia no es una probabilidad calibrada.",
            "El consenso no es una certificación ni una conclusión EUDR.",
            "La fracción de votos del RF no es una probabilidad calibrada.",
            (
                "El RF reproduce etiquetas proxy y no constituye evidencia "
                "independiente de sus fuentes."
            ),
            "El RF está limitado a Entre Ríos y al stack estacional completo de 2020.",
            (
                "El screening automatizado excluye de interpretación automática el bosque "
                "abierto, los píxeles mixtos y los casos de evidencia insuficiente."
            ),
            "El área forestal final requiere agregación espacial en CRS equivalente.",
        ],
    }
    files[paths["metadata"]] = (
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    return ForestBaselineMaterialization(
        files=files,
        estimates=tuple(estimates),
        validations=tuple(validations),
        grid_spec=grid_spec,
        screening_domain=screening,
    )


def _float_values(values: np.ma.MaskedArray) -> np.ndarray:
    masked = np.ma.asarray(values, dtype=np.float64)
    return np.asarray(np.ma.filled(masked, np.nan), dtype=np.float64)


def _binary_mask_tiff(
    *,
    values: np.ndarray,
    aoi_footprint: np.ndarray,
    band_name: str,
    grid_spec: RasterGridSpec,
) -> bytes:
    output = np.full(values.shape, int(grid_spec.nodata), dtype=np.int16)
    output[aoi_footprint] = values[aoi_footprint].astype(np.int16)
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=grid_spec.width,
            height=grid_spec.height,
            count=1,
            dtype="int16",
            crs=grid_spec.target_crs,
            transform=Affine(*grid_spec.transform),
            nodata=grid_spec.nodata,
            compress="deflate",
        ) as dataset:
            dataset.write(output, 1)
            dataset.set_band_description(1, band_name)
        return bytes(memory.read())


def _render_screening_qa(
    *,
    screening: ForestScreeningDomain,
    dpi: int,
) -> bytes:
    figure, axes = plt.subplots(1, 3, figsize=(13, 4.5), constrained_layout=True)
    panels = (
        (screening.automated_evaluable, "Área automáticamente evaluable", "Blues"),
        (screening.review_required, "Revisión requerida", "Oranges"),
        (screening.insufficient_data, "Datos insuficientes", "Greys"),
    )
    for axis, (values, title, colormap) in zip(axes, panels, strict=True):
        panel = np.ma.masked_where(~screening.aoi_footprint, values.astype(np.uint8))
        axis.imshow(
            panel,
            origin="upper",
            cmap=colormap,
            vmin=0,
            vmax=1,
            interpolation="nearest",
        )
        axis.set_title(title)
        axis.set_xlabel("x de grilla")
        axis.set_ylabel("y de grilla")
    figure.suptitle("Alcance automatizado 2020 — screening, no conclusión EUDR", fontsize=13)
    return _figure_bytes(figure, dpi)


def _render_baseline_qa(
    *,
    evidence_fraction: bytes,
    consensus: bytes,
    disagreement: bytes,
    source_evidence: bytes,
    rf_class: bytes,
    rf_vote_fraction: bytes,
    rf_input_complete: bytes,
    source_ids: tuple[str, ...],
    dpi: int,
) -> bytes:
    fraction, extent = _read_raster(evidence_fraction)
    consensus_values, _ = _read_raster(consensus)
    disagreement_values, _ = _read_raster(disagreement)
    source_values, _ = _read_raster(source_evidence)
    rf_class_values, _ = _read_raster(rf_class)
    rf_vote_values, _ = _read_raster(rf_vote_fraction)
    rf_complete_values, _ = _read_raster(rf_input_complete)
    figure, axes = plt.subplots(3, 3, figsize=(13, 11), constrained_layout=True)
    panels: tuple[tuple[np.ma.MaskedArray, str, str, float, float], ...] = (
        (
            fraction[0],
            "Fracción de evidencia core",
            "YlGn",
            0,
            1,
        ),
        (rf_class_values[0], "Clase RF (1=bosque)", "Greens", 0, 1),
        (
            rf_vote_values[0],
            "Fracción de votos RF (no calibrada)",
            "YlGn",
            0,
            1,
        ),
        (rf_complete_values[0], "Inputs RF completos", "Blues", 0, 1),
        (consensus_values[0], "Consenso forestal", "Greens", 0, 1),
        (disagreement_values[0], "Desacuerdo", "Reds", 0, 1),
        *tuple(
            (
                source_values[index],
                source_id,
                "Greens",
                0,
                1,
            )
            for index, source_id in enumerate(source_ids[:3])
        ),
    )
    for axis, (values, title, colormap, minimum, maximum) in zip(
        axes.flat,
        panels,
        strict=True,
    ):
        axis.imshow(
            values,
            extent=extent,
            origin="upper",
            cmap=colormap,
            vmin=minimum,
            vmax=maximum,
            interpolation="nearest",
        )
        axis.set_title(title)
        axis.set_xlabel("Este (m)")
        axis.set_ylabel("Norte (m)")
    figure.suptitle(
        "Línea base forestal 2020 — evidencia preliminar, no calibrada",
        fontsize=14,
    )
    return _figure_bytes(figure, dpi)


def _read_raster(content: bytes) -> tuple[np.ma.MaskedArray, tuple[float, float, float, float]]:
    with MemoryFile(content) as memory:
        with memory.open() as dataset:
            values = dataset.read(masked=True)
            extent = (
                float(dataset.bounds.left),
                float(dataset.bounds.right),
                float(dataset.bounds.bottom),
                float(dataset.bounds.top),
            )
    return values, extent


def _figure_bytes(figure: Figure, dpi: int) -> bytes:
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi)
    plt.close(figure)
    return buffer.getvalue()
