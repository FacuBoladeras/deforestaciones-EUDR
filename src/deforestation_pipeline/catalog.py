"""Catálogo local y tipado de fuentes candidatas, sin acceso remoto."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from deforestation_pipeline.config import PipelineConfig, SpectralIndex

NonEmptyString = Annotated[str, Field(min_length=1)]
HttpsUrl = Annotated[str, Field(pattern=r"^https://")]


class SourceCatalogError(ValueError):
    """Catálogo local ilegible o estructuralmente inválido."""


class CatalogCompatibilityError(ValueError):
    """La fuente catalogada no satisface la configuración científica."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(violations)
        if not self.violations:
            raise ValueError("CatalogCompatibilityError requiere al menos una violación")
        super().__init__("Catálogo incompatible: " + "; ".join(self.violations))


class StrictCatalogModel(BaseModel):
    """Base inmutable que rechaza metadatos no declarados."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceAccessStatus(StrEnum):
    """Distingue catalogar una fuente de haber accedido efectivamente a ella."""

    CATALOGED_NOT_ACCESSED = "cataloged_not_accessed"


class ForestEvidenceRole(StrEnum):
    """Función científica de una fuente dentro de la línea base."""

    CORE_FOREST_MAP = "core_forest_map"
    CORE_LAND_COVER = "core_land_cover"
    SUPPORTING_DERIVED = "supporting_derived"


class ForestRuleKind(StrEnum):
    """Transformaciones auditables desde una fuente hacia evidencia binaria."""

    CATEGORICAL_VALUES = "categorical_values"
    HANSEN_2020_RECONSTRUCTION = "hansen_2020_reconstruction"


class BandRole(StrEnum):
    """Roles espectrales comunes usados por el benchmark."""

    BLUE = "blue"
    GREEN = "green"
    RED = "red"
    NIR = "nir"
    SWIR1 = "swir1"
    SWIR2 = "swir2"
    QA = "qa"


class BandBinding(StrictCatalogModel):
    """Vincula un rol científico con la banda propia de un producto."""

    role: BandRole
    source_band: NonEmptyString


class FmaskBits(StrictCatalogModel):
    """Bits de calidad documentados para HLS v2 en Earth Engine."""

    cloud: Literal[1]
    adjacent_to_cloud_or_shadow: Literal[2]
    cloud_shadow: Literal[3]
    snow_or_ice: Literal[4]
    water: Literal[5]
    aerosol_first_bit: Literal[6]
    aerosol_last_bit: Literal[7]


class CatalogSource(StrictCatalogModel):
    """Descriptor local de una colección candidata."""

    source_id: NonEmptyString
    family: Literal["HLS"]
    product: Literal["HLSL30", "HLSS30"]
    provider: Literal["NASA LP DAAC"]
    collection_id: NonEmptyString
    version: Literal["2.0"]
    spatial_resolution_m: Literal[30]
    availability_start: date
    availability_end: None
    access_status: SourceAccessStatus
    catalog_checked_at: date
    doi_url: HttpsUrl
    catalog_url: HttpsUrl
    documentation_url: HttpsUrl
    data_terms_summary: NonEmptyString
    platform_terms_separate: Literal[True]
    bands: Annotated[tuple[BandBinding, ...], Field(min_length=1)]
    qa_mask_bits: FmaskBits

    @model_validator(mode="after")
    def band_roles_are_unique(self) -> CatalogSource:
        roles = tuple(binding.role for binding in self.bands)
        if len(roles) != len(set(roles)):
            raise ValueError("bands contiene roles repetidos")
        return self

    @property
    def band_map(self) -> dict[BandRole, str]:
        """Devuelve una copia mutable; el descriptor interno permanece inmutable."""
        return {binding.role: binding.source_band for binding in self.bands}


class ForestEvidenceRule(StrictCatalogModel):
    """Regla declarativa para convertir una fuente en evidencia de bosque 2020."""

    kind: ForestRuleKind
    source_band: str | None = None
    forest_values: tuple[int, ...] = ()
    tree_cover_band: str | None = None
    loss_year_band: str | None = None
    minimum_tree_cover_percent: float | None = None
    loss_year_cutoff_code: int | None = None

    @model_validator(mode="after")
    def fields_match_rule_kind(self) -> ForestEvidenceRule:
        if self.kind is ForestRuleKind.CATEGORICAL_VALUES:
            if not self.source_band or not self.forest_values:
                raise ValueError("categorical_values requiere source_band y forest_values")
            if any(
                value is not None
                for value in (
                    self.tree_cover_band,
                    self.loss_year_band,
                    self.minimum_tree_cover_percent,
                    self.loss_year_cutoff_code,
                )
            ):
                raise ValueError("categorical_values no admite parámetros Hansen")
            return self

        if self.source_band is not None or self.forest_values:
            raise ValueError("hansen_2020_reconstruction no admite valores categóricos")
        if not self.tree_cover_band or not self.loss_year_band:
            raise ValueError("hansen_2020_reconstruction requiere bandas de cobertura y pérdida")
        if self.minimum_tree_cover_percent != 10.0:
            raise ValueError("minimum_tree_cover_percent debe ser 10")
        if self.loss_year_cutoff_code != 20:
            raise ValueError("loss_year_cutoff_code debe representar 2020 con el valor 20")
        return self


class ForestCatalogSource(StrictCatalogModel):
    """Descriptor verificado de una fuente candidata de bosque 2020."""

    source_id: NonEmptyString
    family: Literal["FOREST_BASELINE"]
    product: NonEmptyString
    provider: NonEmptyString
    collection_id: NonEmptyString
    version: NonEmptyString
    asset_type: Literal["image", "image_collection"]
    spatial_resolution_m: Annotated[float, Field(gt=0)]
    reference_year: Literal[2020]
    access_status: SourceAccessStatus
    catalog_checked_at: date
    doi_url: HttpsUrl
    catalog_url: HttpsUrl
    documentation_url: HttpsUrl
    data_terms_summary: NonEmptyString
    license: NonEmptyString
    platform_terms_separate: Literal[True]
    evidence_role: ForestEvidenceRole
    independence_group: NonEmptyString
    eligible_for_core_consensus: bool
    rule: ForestEvidenceRule
    limitations: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def role_matches_consensus_eligibility(self) -> ForestCatalogSource:
        core_role = self.evidence_role in {
            ForestEvidenceRole.CORE_FOREST_MAP,
            ForestEvidenceRole.CORE_LAND_COVER,
        }
        if self.eligible_for_core_consensus != core_role:
            raise ValueError("eligible_for_core_consensus no coincide con evidence_role")
        return self


class SourceCatalog(StrictCatalogModel):
    """Catálogo versionado de fuentes candidatas todavía no utilizadas."""

    schema_version: Literal["2.0.0"]
    sources: Annotated[
        tuple[CatalogSource | ForestCatalogSource, ...],
        Field(min_length=1),
    ]

    @model_validator(mode="after")
    def source_identifiers_are_unique(self) -> SourceCatalog:
        source_ids = tuple(source.source_id for source in self.sources)
        collection_ids = tuple(source.collection_id for source in self.sources)
        violations: list[str] = []
        if len(source_ids) != len(set(source_ids)):
            violations.append("source_id contiene duplicados")
        if len(collection_ids) != len(set(collection_ids)):
            violations.append("collection_id contiene duplicados")
        if violations:
            raise ValueError("; ".join(violations))
        return self


class BenchmarkSourcePlan(StrictCatalogModel):
    """Selección local de fuentes requerida por la configuración vigente."""

    sensor_family: Literal["HLS"]
    target_resolution_m: Literal[30]
    sources: tuple[CatalogSource, ...]
    required_band_roles: tuple[BandRole, ...]
    requested_indices: tuple[SpectralIndex, ...]
    remote_data_accessed: Literal[False] = False


class ForestBaselineSourcePlan(StrictCatalogModel):
    """Selección local de fuentes forestales, todavía sin abrir GEE."""

    reference_year: Literal[2020]
    target_resolution_m: Literal[30]
    sources: tuple[ForestCatalogSource, ...]
    core_sources: Annotated[tuple[ForestCatalogSource, ...], Field(min_length=2)]
    supporting_sources: tuple[ForestCatalogSource, ...]
    deferred_source_ids: tuple[NonEmptyString, ...]
    remote_data_accessed: Literal[False] = False


REQUIRED_BENCHMARK_BAND_ROLES = (
    BandRole.BLUE,
    BandRole.GREEN,
    BandRole.RED,
    BandRole.NIR,
    BandRole.SWIR1,
    BandRole.SWIR2,
    BandRole.QA,
)
REQUIRED_HLS_PRODUCTS = ("HLSL30", "HLSS30")


def load_source_catalog(path: Path) -> SourceCatalog:
    """Carga el descriptor YAML local sin consultar ningún catálogo remoto."""
    try:
        document: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SourceCatalogError(f"no se pudo leer {path}: {error}") from error
    except yaml.YAMLError as error:
        raise SourceCatalogError(f"YAML inválido en {path}: {error}") from error
    if not isinstance(document, dict):
        raise SourceCatalogError(f"el nodo raíz de {path} debe ser un objeto YAML")
    return SourceCatalog.model_validate(document)


def build_benchmark_source_plan(
    catalog: SourceCatalog,
    config: PipelineConfig,
) -> BenchmarkSourcePlan:
    """Selecciona y valida fuentes sin abrir conexiones ni listar escenas."""
    sources = tuple(source for source in catalog.sources if isinstance(source, CatalogSource))
    violations: list[str] = []
    products = tuple(source.product for source in sources)
    if products != REQUIRED_HLS_PRODUCTS:
        violations.append(
            f"HLS requiere los productos ordenados HLSL30 y HLSS30; se encontraron: {products}"
        )
    for source in sources:
        if source.spatial_resolution_m != config.data.target_resolution_m:
            violations.append(
                f"{source.product} usa {source.spatial_resolution_m} m y la configuración "
                f"requiere {config.data.target_resolution_m} m"
            )
        missing_roles = tuple(
            role for role in REQUIRED_BENCHMARK_BAND_ROLES if role not in source.band_map
        )
        if missing_roles:
            missing = ", ".join(role.value for role in missing_roles)
            violations.append(f"{source.product} carece de roles requeridos: {missing}")
        if source.access_status is not SourceAccessStatus.CATALOGED_NOT_ACCESSED:
            violations.append(f"{source.product} tiene un estado de acceso no admitido")
    if violations:
        raise CatalogCompatibilityError(violations)

    return BenchmarkSourcePlan(
        sensor_family=config.data.benchmark_sensor,
        target_resolution_m=config.data.target_resolution_m,
        sources=sources,
        required_band_roles=REQUIRED_BENCHMARK_BAND_ROLES,
        requested_indices=config.data.indices,
    )


def build_forest_baseline_source_plan(
    catalog: SourceCatalog,
    config: PipelineConfig,
) -> ForestBaselineSourcePlan:
    """Selecciona fuentes 2020 independientes sin acceder a datos remotos."""
    sources = tuple(source for source in catalog.sources if isinstance(source, ForestCatalogSource))
    core_sources = tuple(source for source in sources if source.eligible_for_core_consensus)
    supporting_sources = tuple(
        source for source in sources if not source.eligible_for_core_consensus
    )
    violations: list[str] = []
    minimum_sources = config.forest_baseline.minimum_independent_sources
    if len(core_sources) < minimum_sources:
        violations.append(
            f"se requieren al menos {minimum_sources} fuentes forestales independientes"
        )
    independence_groups = tuple(source.independence_group for source in core_sources)
    if len(independence_groups) != len(set(independence_groups)):
        violations.append("las fuentes core deben pertenecer a grupos independientes")
    if any(source.reference_year != 2020 for source in sources):
        violations.append("todas las fuentes forestales deben referir al año 2020")
    if violations:
        raise CatalogCompatibilityError(violations)

    return ForestBaselineSourcePlan(
        reference_year=2020,
        target_resolution_m=config.forest_baseline.benchmark_resolution_m,
        sources=sources,
        core_sources=core_sources,
        supporting_sources=supporting_sources,
        deferred_source_ids=("mapbiomas_chaco_deferred",),
    )
