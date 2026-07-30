from __future__ import annotations

import pytest

from deforestation_pipeline.artifact_layout import (
    annual_figure,
    annual_json,
    annual_table,
    annual_tiff,
    configuration_json,
    geometry_json,
    input_json,
    run_json,
    seasonal_figure,
    seasonal_json,
    seasonal_table,
    seasonal_tiff,
)


def test_groups_run_input_geometry_and_configuration_json_by_domain() -> None:
    assert run_json("manifest.json") == "json/run/manifest.json"
    assert input_json("converted.geojson") == "json/input/converted.geojson"
    assert geometry_json("area.json") == "json/geometry/area.json"
    assert configuration_json("resolved.json") == "json/configuration/resolved.json"


def test_groups_annual_assets_by_cadence_period_and_role() -> None:
    assert annual_json("grid.json") == "json/temporal/annual/grid.json"
    assert (
        annual_json("composite_metadata.json", year=2022)
        == "json/temporal/annual/2022/composite_metadata.json"
    )
    assert annual_tiff(2022, "reflectance.tif") == "tiffs/annual/2022/reflectance.tif"
    assert annual_figure(2022, "rgb.png") == "figures/annual/2022/rgb.png"
    assert annual_figure(None, "coverage.png") == "figures/annual/qa/coverage.png"
    assert annual_table("summary.csv") == "tables/annual/summary.csv"


def test_groups_seasonal_assets_by_cadence_period_and_role() -> None:
    assert seasonal_json("cube_index.json") == "json/temporal/seasonal/cube_index.json"
    assert (
        seasonal_json("composite_metadata.json", period_id="2022-DJF")
        == "json/temporal/seasonal/2022-DJF/composite_metadata.json"
    )
    assert seasonal_tiff("2022-DJF", "indices.tif") == "tiffs/seasonal/2022-DJF/indices.tif"
    assert seasonal_figure("2022-DJF", "rgb.png") == "figures/seasonal/2022-DJF/rgb.png"
    assert seasonal_figure(None, "coverage.png") == "figures/seasonal/qa/coverage.png"
    assert seasonal_table("summary.csv") == "tables/seasonal/summary.csv"


@pytest.mark.parametrize(
    ("factory", "unsafe_name"),
    [
        (run_json, "../manifest.json"),
        (geometry_json, "nested/area.json"),
        (annual_table, r"..\summary.csv"),
    ],
)
def test_rejects_names_that_could_escape_the_layout(factory: object, unsafe_name: str) -> None:
    with pytest.raises(ValueError, match="artifact_name_not_safe"):
        factory(unsafe_name)  # type: ignore[operator]
