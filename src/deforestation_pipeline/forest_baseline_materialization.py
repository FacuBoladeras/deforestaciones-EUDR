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

from deforestation_pipeline.catalog import ForestBaselineSourcePlan
from deforestation_pipeline.config import OutputConfig
from deforestation_pipeline.forest_baseline import forest_baseline_output_paths
from deforestation_pipeline.forest_baseline_features import (
    ForestBaselineFeatureMetadata,
)
from deforestation_pipeline.forest_baseline_products import ForestBaselineImages
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


def materialize_forest_baseline(
    *,
    images: ForestBaselineImages,
    feature_metadata: ForestBaselineFeatureMetadata,
    source_plan: ForestBaselineSourcePlan,
    output_config: OutputConfig,
    grid_spec: RasterGridSpec,
    fetch_bytes: FetchBytes | None = None,
) -> ForestBaselineMaterialization:
    """Materializa seis rasters, un JSON y una única figura de QA."""
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

    files[paths["qa_figure"]] = _render_baseline_qa(
        evidence_fraction=files[paths["evidence_fraction"]],
        consensus=files[paths["consensus"]],
        disagreement=files[paths["disagreement"]],
        source_evidence=files[paths["source_evidence"]],
        source_ids=tuple(source.source_id for source in source_plan.sources),
        dpi=output_config.png_dpi,
    )
    metadata = {
        **images.metadata.model_dump(mode="json"),
        "feature_stack": feature_metadata.model_dump(mode="json"),
        "grid": grid_spec.model_dump(mode="json"),
        "sources": [source.model_dump(mode="json") for source in source_plan.sources],
        "deferred_source_ids": list(source_plan.deferred_source_ids),
        "assets": paths,
        "download_method": "ee.Image.getDownloadURL",
        "resolution_degraded": False,
        "raster_validations": [validation.model_dump(mode="json") for validation in validations],
        "download_estimates": [estimate.model_dump(mode="json") for estimate in estimates],
        "limitations": [
            "La fracción de evidencia no es una probabilidad calibrada.",
            "El consenso no es una certificación ni una conclusión EUDR.",
            "Las variables HLS todavía no participan en una clasificación supervisada.",
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
    )


def _render_baseline_qa(
    *,
    evidence_fraction: bytes,
    consensus: bytes,
    disagreement: bytes,
    source_evidence: bytes,
    source_ids: tuple[str, ...],
    dpi: int,
) -> bytes:
    fraction, extent = _read_raster(evidence_fraction)
    consensus_values, _ = _read_raster(consensus)
    disagreement_values, _ = _read_raster(disagreement)
    source_values, _ = _read_raster(source_evidence)
    figure, axes = plt.subplots(2, 3, figsize=(13, 8), constrained_layout=True)
    panels: tuple[tuple[np.ma.MaskedArray, str, str, float, float], ...] = (
        (
            fraction[0],
            "Fracción de evidencia core",
            "YlGn",
            0,
            1,
        ),
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
