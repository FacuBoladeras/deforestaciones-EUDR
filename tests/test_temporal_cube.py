"""Contratos temporales y del cubo estacional del Paso 12.0."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from shapely.geometry import box

from deforestation_pipeline.config import SpectralIndex
from deforestation_pipeline.raster_grid import derive_raster_grid_spec
from deforestation_pipeline.temporal_cube import (
    TemporalCubeError,
    TemporalCubeSpec,
    TemporalWindowPlan,
    build_meteorological_south_window_plan,
    build_temporal_cube_spec,
    temporal_cube_spec_json_schema,
    temporal_window_plan_json_schema,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
GRID = derive_raster_grid_spec(
    aoi_wgs84=box(-63.0, -33.0, -62.999, -32.999),
    target_crs="EPSG:32720",
    resolution_m=30,
    nodata=-9999,
)


def test_meteorological_south_plan_has_explicit_cross_year_seasons() -> None:
    plan = build_meteorological_south_window_plan(
        start_year=2019,
        end_year=2020,
        generated_at=FIXED_NOW,
    )

    assert plan.temporal_scheme == "meteorological_south_v1"
    assert plan.support_start_date == date(2018, 12, 1)
    assert plan.support_end_date_exclusive == date(2020, 12, 1)
    assert tuple(window.period_id for window in plan.windows) == (
        "2019-DJF",
        "2019-MAM",
        "2019-JJA",
        "2019-SON",
        "2020-DJF",
        "2020-MAM",
        "2020-JJA",
        "2020-SON",
    )
    first = plan.windows[0]
    assert first.start_date == date(2018, 12, 1)
    assert first.end_date_exclusive == date(2019, 3, 1)
    assert first.expected_days == 90
    assert first.crosses_calendar_year is True
    leap_summer = plan.windows[4]
    assert leap_summer.start_date == date(2019, 12, 1)
    assert leap_summer.end_date_exclusive == date(2020, 3, 1)
    assert leap_summer.expected_days == 91


def test_window_plan_requires_closed_years_and_timezone() -> None:
    with pytest.raises(
        TemporalCubeError,
        match="temporal_plan_requires_closed_calendar_years",
    ):
        build_meteorological_south_window_plan(
            start_year=2025,
            end_year=2026,
            generated_at=FIXED_NOW,
        )
    with pytest.raises(TemporalCubeError, match="generated_at_requires_timezone"):
        build_meteorological_south_window_plan(
            start_year=2020,
            end_year=2021,
            generated_at=datetime(2026, 7, 28, 12),
        )


def test_window_plan_rejects_tampered_identity_and_unknown_fields() -> None:
    plan = build_meteorological_south_window_plan(
        start_year=2020,
        end_year=2021,
        generated_at=FIXED_NOW,
    )
    payload = plan.model_dump(mode="json")
    payload["plan_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="plan_sha256"):
        TemporalWindowPlan.model_validate(payload)

    valid_payload = plan.model_dump(mode="json")
    valid_payload["silent_interpolation"] = True
    with pytest.raises(ValidationError, match="silent_interpolation"):
        TemporalWindowPlan.model_validate(valid_payload)


def test_cube_spec_defines_dimensions_variables_and_missing_data_policy() -> None:
    plan = build_meteorological_south_window_plan(
        start_year=2020,
        end_year=2021,
        generated_at=FIXED_NOW,
    )
    spec = build_temporal_cube_spec(
        grid_spec=GRID,
        window_plan=plan,
        spectral_indices=(SpectralIndex.NDVI, SpectralIndex.NBR),
        created_at=FIXED_NOW,
    )

    assert spec.grid.grid_sha256 == GRID.grid_sha256
    assert spec.window_plan_sha256 == plan.plan_sha256
    assert spec.reflectance_dimensions == (
        "time",
        "reflectance_band",
        "y",
        "x",
    )
    assert spec.spectral_index_dimensions == ("time", "index", "y", "x")
    assert spec.observation_count_dimensions == ("time", "sensor", "y", "x")
    assert spec.sensor_names == ("total", "HLSL30", "HLSS30")
    assert spec.missing_data_policy == "preserve_missing_no_interpolation"
    assert spec.interpolation_allowed is False
    assert len(spec.windows) == 8

    payload = spec.model_dump(mode="json")
    payload["window_plan_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="window_plan_sha256"):
        TemporalCubeSpec.model_validate(payload)


@pytest.mark.parametrize(
    ("relative_path", "schema_factory"),
    (
        (
            "data/schemas/temporal-window-plan-v1.0.0.json",
            temporal_window_plan_json_schema,
        ),
        (
            "data/schemas/temporal-cube-spec-v1.0.0.json",
            temporal_cube_spec_json_schema,
        ),
    ),
)
def test_committed_temporal_schemas_match_models(
    relative_path: str,
    schema_factory: Callable[[], dict[str, object]],
) -> None:
    committed = json.loads((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))

    assert committed == schema_factory()
