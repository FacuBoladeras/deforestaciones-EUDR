"""Núcleo determinístico del muestreo provincial para entrenamiento."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast

from deforestation_pipeline.config import SpectralIndex
from deforestation_pipeline.seasonal_feature_stack import seasonal_feature_columns
from deforestation_pipeline.training_sampling import (
    AOI_ASSET_ID,
    AOI_FILTERS,
    INTERNAL_BAND_COLUMNS,
    LABEL_COLUMN,
    METADATA_COLUMNS,
    PROVINCIAL_GRID_PARTITIONS,
    PROXY_LABEL_CODE_COLUMN,
    SOURCE_ASSETS,
    ProvincialSamplingPlan,
    ProxyLabel,
    ProxyLabelStack,
    SamplingQuotas,
    TrainingSamplingRequest,
    build_entre_rios_aoi,
    build_multisource_proxy_label,
    build_stratified_training_samples,
    classify_proxy_votes,
    deterministic_split_code,
)


def test_seasonal_feature_columns_are_exactly_the_reconstructible_68_bands() -> None:
    columns = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))

    assert len(columns) == 68
    assert len(set(columns)) == 68
    assert columns[:6] == (
        "2020_DJF_blue",
        "2020_DJF_green",
        "2020_DJF_red",
        "2020_DJF_nir",
        "2020_DJF_swir1",
        "2020_DJF_swir2",
    )
    assert columns[-3:] == (
        "2020_SON_valid_observation_count",
        "2020_SON_valid_observation_count_l30",
        "2020_SON_valid_observation_count_s30",
    )
    assert LABEL_COLUMN not in columns
    assert not set(columns) & set(METADATA_COLUMNS)


def test_internal_gee_band_names_start_with_a_letter_and_are_not_model_features() -> None:
    valid_gee_name = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
    features = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))

    assert all(valid_gee_name.fullmatch(name) for name in INTERNAL_BAND_COLUMNS)
    assert not set(INTERNAL_BAND_COLUMNS) & set(features)


def test_proxy_vote_policy_preserves_disagreement_as_ambiguous() -> None:
    assert classify_proxy_votes((1, 1, 1, 1)) is ProxyLabel.FOREST
    assert classify_proxy_votes((0, 0, 0, 0)) is ProxyLabel.NON_FOREST
    assert classify_proxy_votes((1, 0, 1, 0)) is ProxyLabel.AMBIGUOUS


def test_sampling_request_enforces_p0_budget_and_block_alignment() -> None:
    request = TrainingSamplingRequest(
        year=2020,
        seed=73,
        grid_crs="EPSG:32720",
        utm_zone=20,
        scale_m=30,
        block_size_m=3000,
        quotas=SamplingQuotas(forest=900, non_forest=700, ambiguous=400),
        generated_at=datetime(2026, 7, 30, 18, tzinfo=UTC),
    )

    assert request.quotas.total == 2000
    assert request.block_size_pixels == 100
    assert PROVINCIAL_GRID_PARTITIONS == ("EPSG:32720", "EPSG:32721")
    assert AOI_ASSET_ID == "FAO/GAUL/2015/level1"
    assert AOI_FILTERS == {"ADM0_NAME": "Argentina", "ADM1_NAME": "Entre Rios"}


def test_partition_requests_can_be_joined_into_one_2000_point_p0_plan() -> None:
    generated_at = datetime(2026, 7, 30, 18, tzinfo=UTC)
    requests = tuple(
        TrainingSamplingRequest(
            year=2020,
            seed=73,
            grid_crs=f"EPSG:{32700 + zone}",
            utm_zone=cast(Literal[20, 21], zone),
            scale_m=30,
            block_size_m=3000,
            quotas=SamplingQuotas(forest=450, non_forest=350, ambiguous=200),
            generated_at=generated_at,
        )
        for zone in (20, 21)
    )

    plan = ProvincialSamplingPlan(partitions=requests)

    assert plan.total_quotas == SamplingQuotas(
        forest=900,
        non_forest=700,
        ambiguous=400,
    )
    assert plan.total_quotas.total == 2000


def test_source_assets_are_explicit_and_mapbiomas_is_only_proxy_evidence() -> None:
    assert SOURCE_ASSETS["mapbiomas"].asset_id == (
        "projects/mapbiomas-public/assets/argentina/lulc/collection2/"
        "mapbiomas_argentina_collection2_integration_v3"
    )
    assert SOURCE_ASSETS["mapbiomas"].band_for_year(2020) == "classification_2020"
    assert SOURCE_ASSETS["mapbiomas"].forest_values == (3, 4, 6)
    assert SOURCE_ASSETS["mapbiomas"].masked_pixels_are_non_forest is False
    assert SOURCE_ASSETS["jrc"].asset_id == "JRC/GFC2020/V3"
    assert SOURCE_ASSETS["jrc"].masked_pixels_are_non_forest is True
    assert SOURCE_ASSETS["worldcover"].asset_id == "ESA/WorldCover/v100"
    assert SOURCE_ASSETS["worldcover"].masked_pixels_are_non_forest is True
    assert SOURCE_ASSETS["hansen"].asset_id.startswith("UMD/hansen/global_forest_change_")
    assert SOURCE_ASSETS["hansen"].masked_pixels_are_non_forest is True
    features = seasonal_feature_columns(year=2020, indices=tuple(SpectralIndex))
    assert all("mapbiomas" not in column for column in features)


def test_proxy_unmasks_only_sources_whose_mask_means_non_forest() -> None:
    unmasked: list[str] = []

    class TrackingImage:
        def __init__(self, expression: str, bands: tuple[str, ...] = ("raw",)) -> None:
            self.expression = expression
            self.bands = bands

        def select(self, band: str) -> TrackingImage:
            return TrackingImage(f"{self.expression}:{band}", (band,))

        def unmask(self, value: int) -> TrackingImage:
            unmasked.append(f"{self.expression}={value}")
            return TrackingImage(f"unmask({self.expression},{value})", self.bands)

        def eq(self, _: object) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def gt(self, _: object) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def gte(self, _: object) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def Or(self, _: TrackingImage) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def And(self, _: TrackingImage) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def toUint8(self) -> TrackingImage:
            return self

        def rename(self, band: str) -> TrackingImage:
            return TrackingImage(self.expression, (band,))

        def add(self, _: object) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def multiply(self, _: object) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def where(self, _: TrackingImage, __: int) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def mask(self) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def updateMask(self, _: TrackingImage) -> TrackingImage:
            return TrackingImage(self.expression, self.bands)

        def addBands(self, other: TrackingImage) -> TrackingImage:
            return TrackingImage(self.expression, self.bands + other.bands)

        def clip(self, _: object) -> TrackingImage:
            return self

    class TrackingImageFactory:
        def __call__(self, value: object) -> TrackingImage:
            if isinstance(value, TrackingImage):
                return value
            return TrackingImage(str(value))

        @staticmethod
        def constant(value: int) -> TrackingImage:
            return TrackingImage(f"constant({value})")

    module = SimpleNamespace(
        Geometry=lambda value: value,
        Image=TrackingImageFactory(),
        ImageCollection=lambda asset: SimpleNamespace(first=lambda: TrackingImage(str(asset))),
    )
    aoi = cast(
        Any,
        SimpleNamespace(__geo_interface__={"type": "Polygon", "coordinates": []}),
    )

    build_multisource_proxy_label(module=module, year=2020, aoi_wgs84=aoi)

    assert unmasked == [
        "JRC/GFC2020/V3:Map=0",
        "ESA/WorldCover/v100:Map=0",
        "UMD/hansen/global_forest_change_2025_v1_13:treecover2000=0",
        "UMD/hansen/global_forest_change_2025_v1_13:lossyear=0",
    ]
    assert classify_proxy_votes((0, 0, 0, 0)) is ProxyLabel.NON_FOREST


def test_operational_aoi_uses_both_verified_gaul_filters() -> None:
    class FakeCollection:
        def __init__(self, asset_id: str) -> None:
            self.asset_id = asset_id
            self.filters: list[tuple[str, str]] = []

        def filter(self, value: tuple[str, str]) -> FakeCollection:
            self.filters.append(value)
            return self

    collection = FakeCollection("")

    def feature_collection(asset_id: str) -> FakeCollection:
        collection.asset_id = asset_id
        return collection

    module = SimpleNamespace(
        FeatureCollection=feature_collection,
        Filter=SimpleNamespace(eq=lambda name, value: (name, value)),
    )

    result = build_entre_rios_aoi(module)

    assert result is collection
    assert collection.asset_id == AOI_ASSET_ID
    assert collection.filters == [
        ("ADM0_NAME", "Argentina"),
        ("ADM1_NAME", "Entre Rios"),
    ]


def test_proxy_image_keeps_all_raw_votes_and_ambiguous_code_band() -> None:
    class FakeImage:
        def __init__(self, bands: tuple[str, ...]) -> None:
            self.bands = bands

        def select(self, bands: str) -> FakeImage:
            return FakeImage((bands,))

        def unmask(self, _: int) -> FakeImage:
            return FakeImage(self.bands)

        def eq(self, _: object) -> FakeImage:
            return FakeImage(self.bands)

        def gt(self, _: object) -> FakeImage:
            return FakeImage(self.bands)

        def gte(self, _: object) -> FakeImage:
            return FakeImage(self.bands)

        def Or(self, _: FakeImage) -> FakeImage:
            return FakeImage(self.bands)

        def And(self, _: FakeImage) -> FakeImage:
            return FakeImage(self.bands)

        def toUint8(self) -> FakeImage:
            return self

        def rename(self, bands: str) -> FakeImage:
            return FakeImage((bands,))

        def add(self, _: object) -> FakeImage:
            return FakeImage(self.bands)

        def multiply(self, _: object) -> FakeImage:
            return FakeImage(self.bands)

        def where(self, _: FakeImage, __: int) -> FakeImage:
            return FakeImage(self.bands)

        def mask(self) -> FakeImage:
            return FakeImage(self.bands)

        def updateMask(self, _: FakeImage) -> FakeImage:
            return FakeImage(self.bands)

        def addBands(self, other: FakeImage) -> FakeImage:
            return FakeImage(self.bands + other.bands)

        def clip(self, _: object) -> FakeImage:
            return self

    class FakeImageFactory:
        def __call__(self, _: object) -> FakeImage:
            return FakeImage(("raw",))

        @staticmethod
        def constant(_: int) -> FakeImage:
            return FakeImage(("constant",))

    module = SimpleNamespace(
        Geometry=lambda value: value,
        Image=FakeImageFactory(),
        ImageCollection=lambda _: SimpleNamespace(first=lambda: FakeImage(("Map",))),
    )
    aoi = SimpleNamespace(__geo_interface__={"type": "Polygon", "coordinates": []})

    result = build_multisource_proxy_label(
        module=module,
        year=2020,
        aoi_wgs84=cast(Any, aoi),
    )

    assert isinstance(result, ProxyLabelStack)
    assert result.image.bands == (
        *result.raw_vote_columns,
        "forest_vote_count",
        "source_vote_count",
        PROXY_LABEL_CODE_COLUMN,
    )


def test_split_is_deterministic_at_block_level() -> None:
    assert deterministic_split_code(block_x=10, block_y=-3, seed=73) == (
        deterministic_split_code(block_x=10, block_y=-3, seed=73)
    )
    assert {
        deterministic_split_code(block_x=x, block_y=y, seed=73) for x in range(5) for y in range(5)
    } <= {0, 1, 2}


def test_stratified_sampling_uses_explicit_quotas_and_no_geometries(
    monkeypatch: Any,
) -> None:
    from deforestation_pipeline import training_sampling

    class FakeCollection:
        def __init__(self) -> None:
            self.mapper: object | None = None

        def map(self, mapper: object) -> FakeCollection:
            self.mapper = mapper
            return self

    class FakeSamplingImage:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None
            self.collection = FakeCollection()

        def stratifiedSample(self, **kwargs: object) -> FakeCollection:
            self.kwargs = kwargs
            return self.collection

    fake_image = FakeSamplingImage()
    monkeypatch.setattr(
        training_sampling,
        "_build_sampling_image",
        lambda **_: fake_image,
    )
    request = TrainingSamplingRequest(
        year=2020,
        seed=73,
        grid_crs="EPSG:32720",
        utm_zone=20,
        scale_m=30,
        block_size_m=3000,
        quotas=SamplingQuotas(forest=900, non_forest=700, ambiguous=400),
        generated_at=datetime(2026, 7, 30, 18, tzinfo=UTC),
    )
    module = SimpleNamespace(Geometry=lambda value: value)

    result = build_stratified_training_samples(
        module=module,
        feature_image=object(),
        proxy_image=object(),
        aoi_wgs84=cast(
            Any,
            SimpleNamespace(__geo_interface__={"type": "Polygon", "coordinates": []}),
        ),
        request=request,
    )

    assert result is fake_image.collection
    assert fake_image.kwargs is not None
    assert fake_image.kwargs["classBand"] == PROXY_LABEL_CODE_COLUMN
    assert fake_image.kwargs["classValues"] == [0, 1, 2]
    assert fake_image.kwargs["classPoints"] == [700, 900, 400]
    assert fake_image.kwargs["geometries"] is False
    assert fake_image.kwargs["dropNulls"] is True
    assert fake_image.kwargs["seed"] == 73


def test_build_feature_stack_reuses_the_existing_seasonal_builder(
    monkeypatch: Any,
) -> None:
    from deforestation_pipeline import seasonal_feature_stack, training_sampling

    class FakeImage:
        def __init__(self, bands: tuple[str, ...]) -> None:
            self.bands = bands

        def rename(self, bands: list[str]) -> FakeImage:
            return FakeImage(tuple(bands))

        def addBands(self, other: FakeImage) -> FakeImage:
            return FakeImage(self.bands + other.bands)

    items = []
    for season in ("DJF", "MAM", "JJA", "SON"):
        composite = SimpleNamespace(
            reflectance=FakeImage(("blue", "green", "red", "nir", "swir1", "swir2")),
            indices=FakeImage(tuple(index.value for index in SpectralIndex)),
            valid_observation_count=FakeImage(("valid_observation_count",)),
            valid_observation_count_l30=FakeImage(("valid_observation_count_l30",)),
            valid_observation_count_s30=FakeImage(("valid_observation_count_s30",)),
        )
        items.append(
            SimpleNamespace(
                window=SimpleNamespace(period_id=f"2020-{season}"),
                composite=composite,
            )
        )
    monkeypatch.setattr(
        seasonal_feature_stack,
        "build_hls_seasonal_composites",
        lambda **_: SimpleNamespace(items=tuple(items)),
    )
    request = TrainingSamplingRequest(
        year=2020,
        seed=73,
        grid_crs="EPSG:32720",
        utm_zone=20,
        scale_m=30,
        block_size_m=3000,
        quotas=SamplingQuotas(forest=900, non_forest=700, ambiguous=400),
        generated_at=datetime(2026, 7, 30, 18, tzinfo=UTC),
    )
    fake_data_config = SimpleNamespace(indices=tuple(SpectralIndex))

    result = training_sampling.build_seasonal_feature_stack(
        session=cast(Any, SimpleNamespace()),
        source_plan=cast(Any, SimpleNamespace()),
        data_config=cast(Any, fake_data_config),
        aoi_wgs84=cast(Any, SimpleNamespace()),
        grid_spec=cast(Any, SimpleNamespace()),
        request=request,
    )

    assert result.feature_columns == seasonal_feature_columns(
        year=2020,
        indices=tuple(SpectralIndex),
    )
    assert result.image.bands == result.feature_columns
