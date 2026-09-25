from __future__ import annotations

import hashlib
from io import BytesIO

import numpy as np
import pytest
from matplotlib import pyplot as plt
from matplotlib.figure import Figure
from PIL import Image
from rasterio import Affine
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from deforestation_pipeline.agricultural_visualization import (
    render_agricultural_persistence_evidence,
    render_likely_conversion_conjunction,
    render_monthly_agricultural_evidence,
    select_monthly_evidence_windows,
)


def _raster(
    values: np.ndarray,
    descriptions: tuple[str, ...],
    *,
    crs: str = "EPSG:6933",
    transform: Affine | None = None,
) -> bytes:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff",
            width=values.shape[2],
            height=values.shape[1],
            count=values.shape[0],
            dtype="float32",
            crs=crs,
            transform=transform or from_origin(0, 30, 10, 10),
            nodata=-9999.0,
        ) as dataset:
            dataset.write(values.astype("float32"))
            dataset.descriptions = descriptions
        return bytes(memory.read())


def _monthly(value: float | None) -> bytes:
    values = np.full((4, 3, 4), -9999.0, dtype="float32")
    if value is not None:
        values[0] = 4
        values[1] = 4 * value
        values[2] = min(1.0, value + 0.1)
        values[3] = value
    return _raster(
        values,
        (
            "valid_observation_count",
            "qualifying_crop_count",
            "mean_crop_probability",
            "crop_observation_fraction",
        ),
    )


def _footprint() -> bytes:
    values = np.ones((1, 3, 4), dtype="float32")
    values[0, 0, 0] = 0
    return _raster(values, ("event_footprint_fraction",))


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    specs = (
        ("2022-06", 0.0, 0.0, 1.0, None, 0),
        ("2022-07", 0.2, 0.7, 0.1, 0.25, 4),
        ("2022-08", 0.7, 0.2, 0.1, 0.8, 4),
        ("2022-09", 0.1, 0.8, 0.1, 0.15, 3),
        ("2022-10", 0.0, 0.9, 0.1, 0.05, 2),
    )
    for window, crop, unknown, nodata, mean_fraction, scenes in specs:
        for class_name, area in (("crop", crop), ("unknown", unknown), ("nodata", nodata)):
            rows.append(
                {
                    "event_id": "PDE-1",
                    "window_id": window,
                    "class_name": class_name,
                    "area_ha": area,
                    "event_area_ha": 1.0,
                    "matched_scene_count": scenes,
                    "mean_crop_probability": mean_fraction,
                    "mean_crop_observation_fraction": mean_fraction,
                    "raster_path": f"tiffs/{window}.tif",
                }
            )
    return rows


def test_monthly_selection_is_bounded_deterministic_and_not_peak_only() -> None:
    selection = select_monthly_evidence_windows(_rows())

    assert selection == ("2022-06", "2022-07", "2022-08", "2022-10")


def test_monthly_sheet_preserves_nodata_and_records_semantics() -> None:
    rasters = {
        f"tiffs/{window}.tif": _monthly(value)
        for window, value in {
            "2022-06": None,
            "2022-07": 0.25,
            "2022-08": 0.8,
            "2022-09": 0.15,
            "2022-10": 0.05,
        }.items()
    }
    first = render_monthly_agricultural_evidence(
        event_id="PDE-1",
        rows=_rows(),
        raster_contents=rasters,
        footprint_content=_footprint(),
        source_label="Dynamic World V1 · GOOGLE/DYNAMICWORLD/V1",
    )
    second = render_monthly_agricultural_evidence(
        event_id="PDE-1",
        rows=_rows(),
        raster_contents=rasters,
        footprint_content=_footprint(),
        source_label="Dynamic World V1 · GOOGLE/DYNAMICWORLD/V1",
    )

    assert first is not None and second is not None
    assert first.path == "figures/evidence/agricultural_monthly_evidence_PDE-1.png"
    assert hashlib.sha256(first.content).hexdigest() == hashlib.sha256(second.content).hexdigest()
    assert first.record["selected_window_ids"] == [
        "2022-06",
        "2022-07",
        "2022-08",
        "2022-10",
    ]
    assert first.record["selection_rule"] == (
        "first_scheduled+first_spatially_valid+peak_mean_crop_observation_fraction+last_scheduled"
    )
    assert first.record["nodata_semantics"] != first.record["zero_semantics"]
    assert first.record["sha256"] == hashlib.sha256(first.content).hexdigest()
    assert [item["path"] for item in first.record["source_rasters"]] == [
        "tiffs/2022-06.tif",
        "tiffs/2022-07.tif",
        "tiffs/2022-08.tif",
        "tiffs/2022-10.tif",
        "event_footprint_fraction.tif",
    ]
    with Image.open(BytesIO(first.content)) as image:
        assert image.format == "PNG"
        assert image.info["EventID"] == "PDE-1"
        assert "no confirma" in image.info["Disclaimer"]


def test_monthly_sheet_is_not_created_when_every_period_is_nodata() -> None:
    rows = _rows()
    for row in rows:
        row["area_ha"] = 1.0 if row["class_name"] == "nodata" else 0.0
        row["matched_scene_count"] = 0
        row["mean_crop_observation_fraction"] = None
        row["mean_crop_probability"] = None
    assert (
        render_monthly_agricultural_evidence(
            event_id="PDE-1",
            rows=rows,
            raster_contents={
                f"tiffs/{window}.tif": _monthly(None)
                for window in {str(row["window_id"]) for row in rows}
            },
            footprint_content=_footprint(),
            source_label="Dynamic World V1",
        )
        is None
    )


def test_persistence_and_conjunction_maps_are_only_emitted_with_positive_support() -> None:
    values = np.asarray([[[-9999.0, 0.0, 0.5, 1.0]]], dtype="float32")
    persistence = render_agricultural_persistence_evidence(
        event={
            "event_id": "PDE-1",
            "persistence_status": "persistent_crop_support",
            "post_change_window_start": "2022-06-01",
            "post_change_window_end": "2022-12-31",
            "event_area_ha": 1.0,
            "persistent_crop_area_ha": 0.5,
            "persistent_event_fraction": 0.5,
            "per_period_support": [
                {
                    "window_id": "2022-06",
                    "valid_area_ha": 1.0,
                    "qualifying_crop_area_ha": 0.5,
                    "nodata_area_ha": 0.0,
                }
            ],
            "sensitivity": [
                {
                    "crop_observation_fraction_threshold": 0.5,
                    "persistent_crop_area_ha": 0.5,
                }
            ],
        },
        raster_content=_raster(values, ("persistent_crop_event_fraction",)),
        source_label="Dynamic World V1",
    )
    conjunction = render_likely_conversion_conjunction(
        event={
            "event_id": "PDE-1",
            "likely_conversion_area_ha": 0.5,
            "persistent_agricultural_area_ha": 0.7,
            "automatic_status": "conversion_likely",
            "gates": {"all_conversion_likely_gates": True},
        },
        raster_content=_raster(
            values,
            ("likely_conversion_conjunctive_event_fraction",),
        ),
    )

    assert persistence is not None
    assert persistence.path.endswith("agricultural_persistence_PDE-1.png")
    assert conjunction is not None
    assert conjunction.path.endswith("likely_conversion_conjunction_PDE-1.png")
    assert "no es confirmación" in str(conjunction.record["disclaimer"])

    zero = np.asarray([[[-9999.0, 0.0, 0.0, 0.0]]], dtype="float32")
    assert (
        render_agricultural_persistence_evidence(
            event={"event_id": "PDE-1"},
            raster_content=_raster(zero, ("persistent_crop_event_fraction",)),
            source_label="Dynamic World V1",
        )
        is None
    )
    assert (
        render_likely_conversion_conjunction(
            event={"event_id": "PDE-1", "likely_conversion_area_ha": 0.0},
            raster_content=_raster(
                zero,
                ("likely_conversion_conjunctive_event_fraction",),
            ),
        )
        is None
    )


def test_renderer_rejects_malformed_or_wrong_semantic_tiff() -> None:
    with pytest.raises(ValueError, match="agricultural_visualization_invalid_raster"):
        render_likely_conversion_conjunction(
            event={"event_id": "PDE-1", "likely_conversion_area_ha": 0.5},
            raster_content=b"not-a-tiff",
        )
    wrong = np.ones((1, 2, 2), dtype="float32")
    with pytest.raises(ValueError, match="agricultural_visualization_band_semantics_mismatch"):
        render_likely_conversion_conjunction(
            event={"event_id": "PDE-1", "likely_conversion_area_ha": 0.5},
            raster_content=_raster(wrong, ("wrong",)),
        )


def test_renderer_rejects_nonmetric_or_rotated_grids_before_drawing_scale() -> None:
    values = np.asarray([[[0.5]]], dtype="float32")
    description = ("likely_conversion_conjunctive_event_fraction",)

    with pytest.raises(ValueError, match="agricultural_visualization_requires_metric_crs"):
        render_likely_conversion_conjunction(
            event={"event_id": "PDE-1", "likely_conversion_area_ha": 0.5},
            raster_content=_raster(values, description, crs="EPSG:2263"),
        )
    with pytest.raises(ValueError, match="agricultural_visualization_invalid_grid"):
        render_likely_conversion_conjunction(
            event={"event_id": "PDE-1", "likely_conversion_area_ha": 0.5},
            raster_content=_raster(
                values,
                description,
                transform=Affine(10, 1, 0, 0, -10, 30),
            ),
        )


def test_renderer_rejects_absolute_source_path_in_reversible_metadata() -> None:
    values = np.asarray([[[0.5]]], dtype="float32")

    with pytest.raises(ValueError, match="agricultural_visualization_source_path_unsafe"):
        render_likely_conversion_conjunction(
            event={"event_id": "PDE-1", "likely_conversion_area_ha": 0.5},
            raster_content=_raster(values, ("likely_conversion_conjunctive_event_fraction",)),
            source_raster_path="C:/private/likely.tif",
        )


def test_renderer_closes_figure_when_png_serialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = np.asarray([[[0.5]]], dtype="float32")

    def fail_savefig(self: Figure, *args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic save failure")

    monkeypatch.setattr(Figure, "savefig", fail_savefig)
    with pytest.raises(RuntimeError, match="synthetic save failure"):
        render_likely_conversion_conjunction(
            event={"event_id": "PDE-1", "likely_conversion_area_ha": 0.5},
            raster_content=_raster(values, ("likely_conversion_conjunctive_event_fraction",)),
        )

    assert plt.get_fignums() == []
