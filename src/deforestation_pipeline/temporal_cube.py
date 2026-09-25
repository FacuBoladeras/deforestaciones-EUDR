"""Contratos temporales determinísticos para el cubo estacional del Paso 12."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deforestation_pipeline.config import SpectralIndex
from deforestation_pipeline.hls_composite import REFLECTANCE_BAND_NAMES
from deforestation_pipeline.schemas import RasterGridSpec

TEMPORAL_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
TEMPORAL_WINDOW_PLAN_SCHEMA_ID = "urn:deforestation-pipeline:temporal-window-plan:1.0.0"
TEMPORAL_CUBE_SPEC_SCHEMA_ID = "urn:deforestation-pipeline:temporal-cube-spec:1.0.0"
Sha256Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class TemporalCubeError(RuntimeError):
    """Fallo legible al definir ventanas o identidad del cubo."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Contrato temporal fallido: {code}")


class StrictTemporalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeasonCode(StrEnum):
    DJF = "DJF"
    MAM = "MAM"
    JJA = "JJA"
    SON = "SON"


class SeasonDefinition(StrictTemporalModel):
    """Regla calendaria versionada para una estación meteorológica."""

    code: SeasonCode
    label_es: Literal["verano", "otoño", "invierno", "primavera"]
    start_month: int = Field(ge=1, le=12)
    start_day: int = Field(ge=1, le=31)
    start_year_offset: Literal[-1, 0]
    end_month: int = Field(ge=1, le=12)
    end_day: int = Field(ge=1, le=31)
    end_year_offset: Literal[0]


METEOROLOGICAL_SOUTH_V1: tuple[
    SeasonDefinition,
    SeasonDefinition,
    SeasonDefinition,
    SeasonDefinition,
] = (
    SeasonDefinition(
        code=SeasonCode.DJF,
        label_es="verano",
        start_month=12,
        start_day=1,
        start_year_offset=-1,
        end_month=3,
        end_day=1,
        end_year_offset=0,
    ),
    SeasonDefinition(
        code=SeasonCode.MAM,
        label_es="otoño",
        start_month=3,
        start_day=1,
        start_year_offset=0,
        end_month=6,
        end_day=1,
        end_year_offset=0,
    ),
    SeasonDefinition(
        code=SeasonCode.JJA,
        label_es="invierno",
        start_month=6,
        start_day=1,
        start_year_offset=0,
        end_month=9,
        end_day=1,
        end_year_offset=0,
    ),
    SeasonDefinition(
        code=SeasonCode.SON,
        label_es="primavera",
        start_month=9,
        start_day=1,
        start_year_offset=0,
        end_month=12,
        end_day=1,
        end_year_offset=0,
    ),
)


class TemporalWindow(StrictTemporalModel):
    """Período cerrado, ordenable y sin ambigüedad de límites."""

    schema_version: Literal["1.0.0"] = TEMPORAL_SCHEMA_VERSION
    period_id: Annotated[str, Field(pattern=r"^\d{4}-(DJF|MAM|JJA|SON)$")]
    season_year: int = Field(ge=2015)
    season: SeasonCode
    start_date: date
    end_date_exclusive: date
    expected_days: int = Field(gt=0)
    complete_period: Literal[True] = True
    crosses_calendar_year: bool

    @model_validator(mode="after")
    def window_is_consistent(self) -> TemporalWindow:
        if self.period_id != f"{self.season_year}-{self.season.value}":
            raise ValueError("period_id no coincide con season_year y season")
        if self.start_date >= self.end_date_exclusive:
            raise ValueError("start_date debe ser anterior a end_date_exclusive")
        if self.expected_days != (self.end_date_exclusive - self.start_date).days:
            raise ValueError("expected_days no coincide con los límites")
        crosses = self.start_date.year != self.end_date_exclusive.year
        if self.crosses_calendar_year != crosses:
            raise ValueError("crosses_calendar_year no coincide con los límites")
        return self


class TemporalWindowPlan(StrictTemporalModel):
    """Plan completo que separa años solicitados de fechas auxiliares."""

    schema_version: Literal["1.0.0"] = TEMPORAL_SCHEMA_VERSION
    generated_at: datetime
    temporal_scheme: Literal["meteorological_south_v1"]
    season_definitions: tuple[
        SeasonDefinition,
        SeasonDefinition,
        SeasonDefinition,
        SeasonDefinition,
    ]
    requested_start_year: int = Field(ge=2015)
    requested_end_year: int = Field(ge=2015)
    support_start_date: date
    support_end_date_exclusive: date
    windows: tuple[TemporalWindow, ...] = Field(min_length=4)
    plan_sha256: Sha256Digest

    @model_validator(mode="after")
    def plan_is_consistent(self) -> TemporalWindowPlan:
        _require_timezone(self.generated_at, "generated_at")
        if self.requested_start_year > self.requested_end_year:
            raise ValueError("requested_start_year no puede superar requested_end_year")
        if self.requested_end_year >= self.generated_at.year:
            raise ValueError("requested_end_year debe ser un año calendario cerrado")
        if self.season_definitions != METEOROLOGICAL_SOUTH_V1:
            raise ValueError("season_definitions no coincide con temporal_scheme")
        expected_windows = _build_windows(
            self.requested_start_year,
            self.requested_end_year,
            self.season_definitions,
        )
        if self.windows != expected_windows:
            raise ValueError("windows no coincide con el esquema y rango solicitados")
        if self.support_start_date != expected_windows[0].start_date:
            raise ValueError("support_start_date no coincide con la primera ventana")
        if self.support_end_date_exclusive != expected_windows[-1].end_date_exclusive:
            raise ValueError("support_end_date_exclusive no coincide con la última ventana")
        expected_hash = temporal_window_plan_sha256(
            temporal_scheme=self.temporal_scheme,
            season_definitions=self.season_definitions,
            requested_start_year=self.requested_start_year,
            requested_end_year=self.requested_end_year,
            support_start_date=self.support_start_date,
            support_end_date_exclusive=self.support_end_date_exclusive,
            windows=self.windows,
        )
        if self.plan_sha256 != expected_hash:
            raise ValueError("plan_sha256 no coincide con la identidad temporal")
        return self


class TemporalCubeSpec(StrictTemporalModel):
    """Contrato lógico del cubo; todavía no implica persistencia Zarr."""

    schema_version: Literal["1.0.0"] = TEMPORAL_SCHEMA_VERSION
    created_at: datetime
    grid: RasterGridSpec
    window_plan: TemporalWindowPlan
    window_plan_sha256: Sha256Digest
    reflectance_band_names: tuple[
        Literal["blue"],
        Literal["green"],
        Literal["red"],
        Literal["nir"],
        Literal["swir1"],
        Literal["swir2"],
    ]
    spectral_indices: tuple[SpectralIndex, ...] = Field(min_length=1)
    sensor_names: tuple[
        Literal["total"],
        Literal["HLSL30"],
        Literal["HLSS30"],
    ] = ("total", "HLSL30", "HLSS30")
    reflectance_dimensions: tuple[
        Literal["time"],
        Literal["reflectance_band"],
        Literal["y"],
        Literal["x"],
    ] = ("time", "reflectance_band", "y", "x")
    spectral_index_dimensions: tuple[
        Literal["time"],
        Literal["index"],
        Literal["y"],
        Literal["x"],
    ] = ("time", "index", "y", "x")
    observation_count_dimensions: tuple[
        Literal["time"],
        Literal["sensor"],
        Literal["y"],
        Literal["x"],
    ] = ("time", "sensor", "y", "x")
    time_coordinate: Literal["period_start"] = "period_start"
    time_bounds_semantics: Literal["start_inclusive_end_exclusive"] = (
        "start_inclusive_end_exclusive"
    )
    composite_method: Literal["median_reflectance_then_indices"] = "median_reflectance_then_indices"
    missing_data_policy: Literal["preserve_missing_no_interpolation"] = (
        "preserve_missing_no_interpolation"
    )
    interpolation_allowed: Literal[False] = False

    @property
    def windows(self) -> tuple[TemporalWindow, ...]:
        return self.window_plan.windows

    @model_validator(mode="after")
    def cube_spec_is_consistent(self) -> TemporalCubeSpec:
        _require_timezone(self.created_at, "created_at")
        if self.window_plan_sha256 != self.window_plan.plan_sha256:
            raise ValueError("window_plan_sha256 no coincide con window_plan")
        if self.reflectance_band_names != REFLECTANCE_BAND_NAMES:
            raise ValueError("reflectance_band_names no coincide con HLS")
        if len(self.spectral_indices) != len(set(self.spectral_indices)):
            raise ValueError("spectral_indices no puede contener duplicados")
        return self


def build_meteorological_south_window_plan(
    *,
    start_year: int,
    end_year: int,
    generated_at: datetime,
) -> TemporalWindowPlan:
    """Genera DJF/MAM/JJA/SON para años cerrados del hemisferio sur."""
    _require_runtime_timezone(generated_at)
    if start_year < 2015:
        raise TemporalCubeError("temporal_plan_minimum_year_is_2015")
    if start_year > end_year:
        raise TemporalCubeError("temporal_plan_years_not_ordered")
    if end_year >= generated_at.year:
        raise TemporalCubeError("temporal_plan_requires_closed_calendar_years")
    windows = _build_windows(start_year, end_year, METEOROLOGICAL_SOUTH_V1)
    support_start = windows[0].start_date
    support_end = windows[-1].end_date_exclusive
    plan_hash = temporal_window_plan_sha256(
        temporal_scheme="meteorological_south_v1",
        season_definitions=METEOROLOGICAL_SOUTH_V1,
        requested_start_year=start_year,
        requested_end_year=end_year,
        support_start_date=support_start,
        support_end_date_exclusive=support_end,
        windows=windows,
    )
    return TemporalWindowPlan(
        generated_at=generated_at,
        temporal_scheme="meteorological_south_v1",
        season_definitions=METEOROLOGICAL_SOUTH_V1,
        requested_start_year=start_year,
        requested_end_year=end_year,
        support_start_date=support_start,
        support_end_date_exclusive=support_end,
        windows=windows,
        plan_sha256=plan_hash,
    )


def build_temporal_cube_spec(
    *,
    grid_spec: RasterGridSpec,
    window_plan: TemporalWindowPlan,
    spectral_indices: tuple[SpectralIndex, ...],
    created_at: datetime,
) -> TemporalCubeSpec:
    """Compone la identidad espacial, temporal y semántica del futuro cubo."""
    reflectance_bands = cast(
        tuple[
            Literal["blue"],
            Literal["green"],
            Literal["red"],
            Literal["nir"],
            Literal["swir1"],
            Literal["swir2"],
        ],
        REFLECTANCE_BAND_NAMES,
    )
    return TemporalCubeSpec(
        created_at=created_at,
        grid=grid_spec,
        window_plan=window_plan,
        window_plan_sha256=window_plan.plan_sha256,
        reflectance_band_names=reflectance_bands,
        spectral_indices=spectral_indices,
    )


def temporal_window_plan_sha256(
    *,
    temporal_scheme: str,
    season_definitions: tuple[SeasonDefinition, ...],
    requested_start_year: int,
    requested_end_year: int,
    support_start_date: date,
    support_end_date_exclusive: date,
    windows: tuple[TemporalWindow, ...],
) -> str:
    payload = {
        "requested_end_year": requested_end_year,
        "requested_start_year": requested_start_year,
        "season_definitions": [
            definition.model_dump(mode="json") for definition in season_definitions
        ],
        "support_end_date_exclusive": support_end_date_exclusive.isoformat(),
        "support_start_date": support_start_date.isoformat(),
        "temporal_scheme": temporal_scheme,
        "windows": [window.model_dump(mode="json") for window in windows],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def temporal_window_plan_json_schema() -> dict[str, Any]:
    schema = TemporalWindowPlan.model_json_schema(mode="serialization")
    schema["$id"] = TEMPORAL_WINDOW_PLAN_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = TEMPORAL_SCHEMA_VERSION
    return schema


def temporal_cube_spec_json_schema() -> dict[str, Any]:
    schema = TemporalCubeSpec.model_json_schema(mode="serialization")
    schema["$id"] = TEMPORAL_CUBE_SPEC_SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["x-schema-version"] = TEMPORAL_SCHEMA_VERSION
    return schema


def _build_windows(
    start_year: int,
    end_year: int,
    definitions: tuple[SeasonDefinition, ...],
) -> tuple[TemporalWindow, ...]:
    return tuple(
        _window_from_definition(season_year, definition)
        for season_year in range(start_year, end_year + 1)
        for definition in definitions
    )


def _window_from_definition(
    season_year: int,
    definition: SeasonDefinition,
) -> TemporalWindow:
    start = date(
        season_year + definition.start_year_offset,
        definition.start_month,
        definition.start_day,
    )
    end = date(
        season_year + definition.end_year_offset,
        definition.end_month,
        definition.end_day,
    )
    return TemporalWindow(
        period_id=f"{season_year}-{definition.code.value}",
        season_year=season_year,
        season=definition.code,
        start_date=start,
        end_date_exclusive=end,
        expected_days=(end - start).days,
        crosses_calendar_year=start.year != end.year,
    )


def _require_runtime_timezone(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TemporalCubeError("generated_at_requires_timezone")


def _require_timezone(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} debe incluir zona horaria")
