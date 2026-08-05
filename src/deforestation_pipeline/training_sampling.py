"""Muestreo provincial determinístico para un dataset proxy de bosque 2020."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import CRS
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from deforestation_pipeline.catalog import BenchmarkSourcePlan
from deforestation_pipeline.config import DataConfig
from deforestation_pipeline.gee import GeeSession
from deforestation_pipeline.schemas import RasterGridSpec
from deforestation_pipeline.seasonal_feature_stack import (
    SeasonalFeatureStack,
    build_yearly_seasonal_feature_stack,
)

TRAINING_SAMPLING_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
AOI_ASSET_ID: Literal["FAO/GAUL/2015/level1"] = "FAO/GAUL/2015/level1"
AOI_FILTERS = {"ADM0_NAME": "Argentina", "ADM1_NAME": "Entre Rios"}
PROVINCIAL_GRID_PARTITIONS: tuple[
    Literal["EPSG:32720"],
    Literal["EPSG:32721"],
] = ("EPSG:32720", "EPSG:32721")
LABEL_COLUMN: Literal["proxy_label"] = "proxy_label"
PROXY_LABEL_CODE_COLUMN = "internal_proxy_label_code"
PIXEL_X_COLUMN = "internal_pixel_x"
PIXEL_Y_COLUMN = "internal_pixel_y"
BLOCK_X_COLUMN = "internal_block_x"
BLOCK_Y_COLUMN = "internal_block_y"
SPLIT_CODE_COLUMN = "internal_split_code"
INTERNAL_BAND_COLUMNS = (
    PIXEL_X_COLUMN,
    PIXEL_Y_COLUMN,
    BLOCK_X_COLUMN,
    BLOCK_Y_COLUMN,
    SPLIT_CODE_COLUMN,
    PROXY_LABEL_CODE_COLUMN,
)
RAW_VOTE_COLUMNS = (
    "mapbiomas_vote",
    "jrc_vote",
    "worldcover_vote",
    "hansen_vote",
)
METADATA_COLUMNS = (
    "sample_id",
    "block_id",
    "split",
    "longitude",
    "latitude",
    "sample_year",
    "utm_zone",
    "provenance",
    *RAW_VOTE_COLUMNS,
    "forest_vote_count",
    "source_vote_count",
    PROXY_LABEL_CODE_COLUMN,
)
_SPLIT_NAMES = ("train", "validation", "test")
_PROXY_LABEL_NAMES = ("non_forest", "forest", "ambiguous")


class StrictTrainingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProxyLabel(StrEnum):
    FOREST = "forest"
    NON_FOREST = "non_forest"
    AMBIGUOUS = "ambiguous"


class ProxySourceAsset(StrictTrainingModel):
    """Fuente GEE explícita usada sólo para producir una etiqueta proxy."""

    asset_id: str = Field(min_length=1)
    access: Literal["image", "image_collection"]
    band_template: str = Field(min_length=1)
    forest_values: tuple[int, ...] = ()
    masked_pixels_are_non_forest: bool
    semantics: str = Field(min_length=1)

    def band_for_year(self, year: int) -> str:
        return self.band_template.format(year=year)


SOURCE_ASSETS: dict[str, ProxySourceAsset] = {
    "mapbiomas": ProxySourceAsset(
        asset_id=(
            "projects/mapbiomas-public/assets/argentina/lulc/collection2/"
            "mapbiomas_argentina_collection2_integration_v3"
        ),
        access="image",
        band_template="classification_{year}",
        forest_values=(3, 4, 6),
        masked_pixels_are_non_forest=False,
        semantics="mapbiomas_argentina_forest_classes_3_4_6_proxy",
    ),
    "jrc": ProxySourceAsset(
        asset_id="JRC/GFC2020/V3",
        access="image",
        band_template="Map",
        forest_values=(1,),
        masked_pixels_are_non_forest=True,
        semantics="jrc_gfc2020_map_class_1_forest_proxy",
    ),
    "worldcover": ProxySourceAsset(
        asset_id="ESA/WorldCover/v100",
        access="image_collection",
        band_template="Map",
        forest_values=(10, 95),
        masked_pixels_are_non_forest=True,
        semantics="esa_worldcover_tree_cover_or_mangrove_proxy",
    ),
    "hansen": ProxySourceAsset(
        asset_id="UMD/hansen/global_forest_change_2025_v1_13",
        access="image",
        band_template="treecover2000,lossyear",
        masked_pixels_are_non_forest=True,
        semantics="treecover_2000_gte_10_without_loss_through_2020_proxy",
    ),
}


class SamplingQuotas(StrictTrainingModel):
    """Cupos por clase proxy; el total P0 permanece deliberadamente pequeño."""

    forest: Annotated[int, Field(gt=0)]
    non_forest: Annotated[int, Field(gt=0)]
    ambiguous: Annotated[int, Field(gt=0)]

    @property
    def total(self) -> int:
        return self.forest + self.non_forest + self.ambiguous


class TrainingSamplingRequest(StrictTrainingModel):
    """Contrato completo para reconstruir el mismo universo de muestreo."""

    schema_version: Literal["1.0.0"] = TRAINING_SAMPLING_SCHEMA_VERSION
    year: Literal[2020]
    seed: Annotated[int, Field(ge=0)]
    grid_crs: str = Field(min_length=1)
    utm_zone: Literal[20, 21]
    scale_m: Literal[30]
    block_size_m: Annotated[int, Field(gt=0)]
    quotas: SamplingQuotas
    generated_at: datetime

    @model_validator(mode="after")
    def request_is_spatially_consistent(self) -> TrainingSamplingRequest:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at debe incluir zona horaria")
        try:
            crs = CRS.from_user_input(self.grid_crs)
        except Exception:
            raise ValueError("grid_crs debe ser reconocible") from None
        if not crs.is_projected:
            raise ValueError("grid_crs debe ser proyectado")
        units = {axis.unit_name.lower() for axis in crs.axis_info if axis.unit_name}
        if not units or not units <= {"metre", "meter"}:
            raise ValueError("grid_crs debe usar metros")
        expected_crs = f"EPSG:{32700 + self.utm_zone}"
        if crs.to_authority() != ("EPSG", str(32700 + self.utm_zone)):
            raise ValueError(f"grid_crs debe ser {expected_crs} para utm_zone")
        if self.block_size_m % self.scale_m:
            raise ValueError("block_size_m debe ser múltiplo de scale_m")
        return self

    @property
    def block_size_pixels(self) -> int:
        return self.block_size_m // self.scale_m


class ProvincialSamplingPlan(StrictTrainingModel):
    """Dos particiones UTM cuya suma conserva el presupuesto P0 provincial."""

    partitions: Annotated[tuple[TrainingSamplingRequest, ...], Field(min_length=2, max_length=2)]

    @property
    def total_quotas(self) -> SamplingQuotas:
        return SamplingQuotas(
            forest=sum(item.quotas.forest for item in self.partitions),
            non_forest=sum(item.quotas.non_forest for item in self.partitions),
            ambiguous=sum(item.quotas.ambiguous for item in self.partitions),
        )

    @model_validator(mode="after")
    def partitions_cover_both_zones_and_p0_budget(self) -> ProvincialSamplingPlan:
        if tuple(item.grid_crs for item in self.partitions) != PROVINCIAL_GRID_PARTITIONS:
            raise ValueError("las particiones deben ser EPSG:32720 y EPSG:32721, en ese orden")
        common = {
            (item.year, item.seed, item.scale_m, item.block_size_m, item.generated_at)
            for item in self.partitions
        }
        if len(common) != 1:
            raise ValueError("las particiones deben compartir año, semilla, escala y fecha")
        if not 2_000 <= self.total_quotas.total <= 5_000:
            raise ValueError("el total provincial P0 debe estar entre 2000 y 5000 puntos")
        return self


@dataclass(frozen=True, slots=True)
class ProxyLabelStack:
    image: Any = field(repr=False, compare=False)
    raw_vote_columns: tuple[str, ...] = RAW_VOTE_COLUMNS
    label_code_column: str = PROXY_LABEL_CODE_COLUMN
    label_semantics: str = "unanimous_forest_unanimous_non_forest_otherwise_ambiguous"


def classify_proxy_votes(votes: tuple[int, ...]) -> ProxyLabel:
    """Versión pura de la regla de unanimidad usada por la imagen GEE."""
    if not votes or any(vote not in {0, 1} for vote in votes):
        raise ValueError("votes debe contener al menos un voto binario")
    if all(vote == 1 for vote in votes):
        return ProxyLabel.FOREST
    if all(vote == 0 for vote in votes):
        return ProxyLabel.NON_FOREST
    return ProxyLabel.AMBIGUOUS


def deterministic_split_code(*, block_x: int, block_y: int, seed: int) -> int:
    """Asigna el bloque completo a 70/15/15 sin aleatoriedad por píxel."""
    bucket = abs(block_x * 73_856_093 + block_y * 19_349_663 + seed) % 100
    if bucket < 70:
        return 0
    if bucket < 85:
        return 1
    return 2


def build_entre_rios_aoi(module: Any) -> Any:
    """Carga el AOI operativo verificado sin materializar coordenadas localmente."""
    collection = module.FeatureCollection(AOI_ASSET_ID)
    for property_name, value in AOI_FILTERS.items():
        collection = collection.filter(module.Filter.eq(property_name, value))
    return collection


def build_seasonal_feature_stack(
    *,
    session: GeeSession,
    source_plan: BenchmarkSourcePlan,
    data_config: DataConfig,
    aoi_wgs84: BaseGeometry,
    grid_spec: RasterGridSpec,
    request: TrainingSamplingRequest,
) -> SeasonalFeatureStack:
    """Adapta la solicitud P0 al constructor compartido de entrenamiento/inferencia."""
    stack = build_yearly_seasonal_feature_stack(
        session=session,
        source_plan=source_plan,
        data_config=data_config,
        aoi_wgs84=aoi_wgs84,
        grid_spec=grid_spec,
        year=request.year,
        generated_at=request.generated_at,
    )
    if len(stack.feature_columns) != 68:
        raise ValueError("el stack P0 requiere exactamente 68 bandas")
    return stack


def build_multisource_proxy_label(
    *,
    module: Any,
    year: Literal[2020],
    aoi_wgs84: BaseGeometry,
) -> ProxyLabelStack:
    """Construye votos crudos y etiqueta proxy; nunca una conclusión EUDR."""
    remote_aoi = module.Geometry(mapping(aoi_wgs84))
    mapbiomas = _proxy_source_band(
        module.Image(SOURCE_ASSETS["mapbiomas"].asset_id),
        source=SOURCE_ASSETS["mapbiomas"],
        band=SOURCE_ASSETS["mapbiomas"].band_for_year(year),
    )
    jrc = _proxy_source_band(
        module.Image(SOURCE_ASSETS["jrc"].asset_id),
        source=SOURCE_ASSETS["jrc"],
        band="Map",
    )
    worldcover = _proxy_source_band(
        module.Image(module.ImageCollection(SOURCE_ASSETS["worldcover"].asset_id).first()),
        source=SOURCE_ASSETS["worldcover"],
        band="Map",
    )
    hansen = module.Image(SOURCE_ASSETS["hansen"].asset_id)

    votes = (
        _categorical_vote(
            mapbiomas,
            SOURCE_ASSETS["mapbiomas"].forest_values,
        ).rename(RAW_VOTE_COLUMNS[0]),
        _categorical_vote(jrc, SOURCE_ASSETS["jrc"].forest_values).rename(RAW_VOTE_COLUMNS[1]),
        _categorical_vote(
            worldcover,
            SOURCE_ASSETS["worldcover"].forest_values,
        ).rename(RAW_VOTE_COLUMNS[2]),
        _hansen_vote(hansen, year=year).rename(RAW_VOTE_COLUMNS[3]),
    )
    forest_vote_count = votes[0]
    for vote in votes[1:]:
        forest_vote_count = forest_vote_count.add(vote)
    forest_vote_count = forest_vote_count.rename("forest_vote_count").toUint8()
    source_vote_count = (
        module.Image.constant(len(votes))
        .updateMask(forest_vote_count.mask())
        .rename("source_vote_count")
        .toUint8()
    )
    label_code = (
        forest_vote_count.multiply(0)
        .add(2)
        .where(forest_vote_count.eq(len(votes)), 1)
        .where(forest_vote_count.eq(0), 0)
        .rename(PROXY_LABEL_CODE_COLUMN)
        .toUint8()
    )
    image = votes[0]
    for vote in votes[1:]:
        image = image.addBands(vote)
    image = (
        image.addBands(forest_vote_count)
        .addBands(source_vote_count)
        .addBands(label_code)
        .clip(remote_aoi)
    )
    return ProxyLabelStack(image=image)


def build_stratified_training_samples(
    *,
    module: Any,
    feature_image: Any,
    proxy_image: Any,
    aoi_wgs84: BaseGeometry,
    request: TrainingSamplingRequest,
) -> Any:
    """Muestrea por clase proxy y enriquece metadatos sin conservar geometrías."""
    remote_aoi = module.Geometry(mapping(aoi_wgs84))
    sampling_image = _build_sampling_image(
        module=module,
        feature_image=feature_image,
        proxy_image=proxy_image,
        request=request,
    )
    samples = sampling_image.stratifiedSample(
        numPoints=0,
        classBand=PROXY_LABEL_CODE_COLUMN,
        classValues=[0, 1, 2],
        classPoints=[
            request.quotas.non_forest,
            request.quotas.forest,
            request.quotas.ambiguous,
        ],
        region=remote_aoi,
        scale=request.scale_m,
        projection=request.grid_crs,
        seed=request.seed,
        dropNulls=True,
        geometries=False,
        tileScale=4,
    )
    return samples.map(lambda feature: _enrich_sample(module, feature, request))


def _build_sampling_image(
    *,
    module: Any,
    feature_image: Any,
    proxy_image: Any,
    request: TrainingSamplingRequest,
) -> Any:
    projection = module.Projection(request.grid_crs).atScale(request.scale_m)
    coordinates = module.Image.pixelCoordinates(projection)
    pixel_x = coordinates.select("x").rename(PIXEL_X_COLUMN).toInt64()
    pixel_y = coordinates.select("y").rename(PIXEL_Y_COLUMN).toInt64()
    block_x = pixel_x.divide(request.block_size_pixels).floor().rename(BLOCK_X_COLUMN).toInt64()
    block_y = pixel_y.divide(request.block_size_pixels).floor().rename(BLOCK_Y_COLUMN).toInt64()
    bucket = (
        block_x.multiply(73_856_093)
        .add(block_y.multiply(19_349_663))
        .add(request.seed)
        .abs()
        .mod(100)
    )
    split_code = (
        bucket.multiply(0)
        .where(bucket.gte(70), 1)
        .where(bucket.gte(85), 2)
        .rename(SPLIT_CODE_COLUMN)
        .toUint8()
    )
    lon_lat = module.Image.pixelLonLat().select(
        ["longitude", "latitude"],
        ["longitude", "latitude"],
    )
    return (
        feature_image.addBands(proxy_image)
        .addBands(pixel_x)
        .addBands(pixel_y)
        .addBands(block_x)
        .addBands(block_y)
        .addBands(split_code)
        .addBands(lon_lat)
    )


def _enrich_sample(module: Any, feature: Any, request: TrainingSamplingRequest) -> Any:
    pixel_x = module.Number(feature.get(PIXEL_X_COLUMN)).toInt()
    pixel_y = module.Number(feature.get(PIXEL_Y_COLUMN)).toInt()
    block_x = module.Number(feature.get(BLOCK_X_COLUMN)).toInt()
    block_y = module.Number(feature.get(BLOCK_Y_COLUMN)).toInt()
    split_code = module.Number(feature.get(SPLIT_CODE_COLUMN)).toInt()
    label_code = module.Number(feature.get(PROXY_LABEL_CODE_COLUMN)).toInt()
    sample_id = (
        module.String("s")
        .cat(module.Number(request.seed).format())
        .cat("_z")
        .cat(module.Number(request.utm_zone).format())
        .cat("_")
        .cat(pixel_x.format())
        .cat("_")
        .cat(pixel_y.format())
    )
    block_id = (
        module.String("z")
        .cat(module.Number(request.utm_zone).format())
        .cat("_b")
        .cat(block_x.format())
        .cat("_")
        .cat(block_y.format())
    )
    return feature.set(
        {
            "sample_id": sample_id,
            "block_id": block_id,
            "split": module.List(list(_SPLIT_NAMES)).get(split_code),
            LABEL_COLUMN: module.List(list(_PROXY_LABEL_NAMES)).get(label_code),
            "sample_year": request.year,
            "utm_zone": request.utm_zone,
            "provenance": "multisource_unanimity_proxy_v1",
        }
    )


def _categorical_vote(image: Any, values: tuple[int, ...]) -> Any:
    vote = image.eq(values[0])
    for value in values[1:]:
        vote = vote.Or(image.eq(value))
    return vote.toUint8()


def _proxy_source_band(
    image: Any,
    *,
    source: ProxySourceAsset,
    band: str,
) -> Any:
    selected = image.select(band)
    if source.masked_pixels_are_non_forest:
        selected = selected.unmask(0)
    return selected


def _hansen_vote(image: Any, *, year: int) -> Any:
    loss_cutoff = year - 2000
    tree_cover = image.select("treecover2000").unmask(0).gte(10)
    loss_year = image.select("lossyear").unmask(0)
    no_loss_through_year = loss_year.eq(0).Or(loss_year.gt(loss_cutoff))
    return tree_cover.And(no_loss_through_year).toUint8()
