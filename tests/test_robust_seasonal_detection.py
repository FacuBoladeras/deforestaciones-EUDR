"""Diagnósticos robustos estacionales del Paso 14.2."""

from __future__ import annotations

import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from deforestation_pipeline.change_detection import (
    DisturbanceQualityBit,
    ForestEvaluationDomain,
    RobustSeasonalDiagnostics,
    _safe_temporal_nanmedian,
    compute_robust_seasonal_diagnostics,
)
from deforestation_pipeline.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _domain(evaluable: np.ndarray) -> ForestEvaluationDomain:
    shape = evaluable.shape
    return ForestEvaluationDomain(
        domain_code=np.zeros(shape, dtype=np.uint8),
        evaluable=evaluable.astype(np.bool_),
        baseline_uncertain=np.zeros(shape, dtype=np.bool_),
        quality_flags_bitmask=np.zeros(shape, dtype=np.uint16),
    )


def _diagnose(
    *,
    reference_values: np.ndarray,
    reference_seasons: tuple[str, ...],
    reference_starts: tuple[date, ...] | None = None,
    reference_ends_exclusive: tuple[date, ...] | None = None,
    post_values: np.ndarray,
    post_seasons: tuple[str, ...],
    post_starts: tuple[date, ...] | None = None,
    post_ends: tuple[date, ...] | None = None,
    evaluable: np.ndarray | None = None,
) -> RobustSeasonalDiagnostics:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    season_months = {
        "DJF": (12, 1, 3, 1),
        "MAM": (3, 1, 6, 1),
        "JJA": (6, 1, 9, 1),
        "SON": (9, 1, 12, 1),
    }
    season_occurrences: dict[str, int] = {}
    generated_reference_starts: list[date] = []
    generated_reference_ends: list[date] = []
    for season in reference_seasons:
        occurrence = season_occurrences.get(season, 0)
        season_occurrences[season] = occurrence + 1
        season_year = 2018 + occurrence
        start_month, start_day, end_month, end_day = season_months[season]
        start_year = season_year - 1 if season == "DJF" else season_year
        generated_reference_starts.append(date(start_year, start_month, start_day))
        generated_reference_ends.append(date(season_year, end_month, end_day))
    post_count = post_values.shape[0]
    starts = post_starts or tuple(date(2021 + index, 3, 1) for index in range(post_count))
    ends = post_ends or tuple(date(2021 + index, 6, 1) for index in range(post_count))
    spatial_shape = post_values.shape[2:]
    domain_mask = np.ones(spatial_shape, dtype=np.bool_) if evaluable is None else evaluable
    return compute_robust_seasonal_diagnostics(
        reference_values=reference_values,
        reference_seasons=reference_seasons,
        reference_period_start_dates=reference_starts or tuple(generated_reference_starts),
        reference_period_end_dates_exclusive=reference_ends_exclusive
        or tuple(generated_reference_ends),
        post_cutoff_values=post_values,
        post_cutoff_seasons=post_seasons,
        post_period_start_dates=starts,
        post_period_end_dates_exclusive=ends,
        index_names=("NDVI",),
        domain=_domain(domain_mask),
        config=config.disturbance_detection,
    )


def test_stable_observation_has_zero_directional_delta() -> None:
    result = _diagnose(
        reference_values=np.array([[[[0.4]]], [[[0.5]]], [[[0.6]]]]),
        reference_seasons=("MAM", "MAM", "MAM"),
        post_values=np.array([[[[0.5]]]]),
        post_seasons=("MAM",),
    )

    np.testing.assert_allclose(result.reference_center, [[[[0.5]]]])
    np.testing.assert_allclose(result.raw_directional_delta, [[[[0.0]]]])
    np.testing.assert_allclose(result.robust_scale, [[[[0.14826]]]])
    np.testing.assert_allclose(result.standardized_magnitude, [[[[0.0]]]])
    np.testing.assert_array_equal(result.reference_valid_count, [[[[3]]]])


def test_known_vegetation_drop_is_positive_and_standardized() -> None:
    result = _diagnose(
        reference_values=np.array([[[[0.4]]], [[[0.5]]], [[[0.6]]]]),
        reference_seasons=("MAM", "MAM", "MAM"),
        post_values=np.array([[[[0.2]]]]),
        post_seasons=("MAM",),
    )

    np.testing.assert_allclose(result.raw_directional_delta, [[[[0.3]]]])
    np.testing.assert_allclose(
        result.standardized_magnitude,
        [[[[0.3 / 0.14826]]]],
    )
    assert not hasattr(result, "probability")
    assert not hasattr(result, "state")


def test_each_post_observation_uses_only_its_same_season_history() -> None:
    result = _diagnose(
        reference_values=np.array(
            [
                [[[0.4]]],
                [[[0.5]]],
                [[[0.6]]],
                [[[0.8]]],
                [[[0.9]]],
                [[[1.0]]],
            ]
        ),
        reference_seasons=("MAM", "MAM", "MAM", "JJA", "JJA", "JJA"),
        post_values=np.array([[[[0.5]]], [[[0.9]]]]),
        post_seasons=("MAM", "JJA"),
    )

    np.testing.assert_allclose(result.reference_center[:, 0, 0, 0], [0.5, 0.9])
    np.testing.assert_allclose(result.raw_directional_delta[:, 0, 0, 0], [0.0, 0.0])


def test_nan_reference_marks_insufficient_history_without_interpolation() -> None:
    result = _diagnose(
        reference_values=np.array([[[[0.4]]], [[[np.nan]]], [[[0.6]]]]),
        reference_seasons=("MAM", "MAM", "MAM"),
        post_values=np.array([[[[0.2]]]]),
        post_seasons=("MAM",),
    )

    assert np.isnan(result.reference_center[0, 0, 0, 0])
    assert np.isnan(result.standardized_magnitude[0, 0, 0, 0])
    assert result.reference_valid_count[0, 0, 0, 0] == 2
    insufficient_bit = np.uint16(1 << DisturbanceQualityBit.INSUFFICIENT_REFERENCE_HISTORY)
    assert result.quality_flags_bitmask[0, 0, 0, 0] & insufficient_bit


def test_safe_temporal_median_preserves_all_nan_slices_without_warning() -> None:
    values = np.array(
        [
            [[[np.nan, 1.0], [np.nan, 2.0]]],
            [[[np.nan, 3.0], [np.nan, np.nan]]],
            [[[np.nan, 5.0], [np.nan, 4.0]]],
        ],
        dtype=np.float64,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        result = _safe_temporal_nanmedian(values)

    assert np.isnan(result[0, 0, 0])
    assert np.isnan(result[0, 1, 0])
    np.testing.assert_allclose(result[0, :, 1], [3.0, 3.0])


def test_zero_mad_keeps_raw_delta_but_not_standardized_magnitude() -> None:
    result = _diagnose(
        reference_values=np.array([[[[0.5]]], [[[0.5]]], [[[0.5]]]]),
        reference_seasons=("MAM", "MAM", "MAM"),
        post_values=np.array([[[[0.2]]]]),
        post_seasons=("MAM",),
    )

    assert result.raw_directional_delta[0, 0, 0, 0] == pytest.approx(0.3)
    assert result.robust_scale[0, 0, 0, 0] == 0.0
    assert np.isnan(result.standardized_magnitude[0, 0, 0, 0])


def test_non_evaluable_domain_and_nan_observation_remain_nan() -> None:
    result = _diagnose(
        reference_values=np.array(
            [
                [[[0.4, 0.4]]],
                [[[0.5, 0.5]]],
                [[[0.6, 0.6]]],
            ]
        ),
        reference_seasons=("MAM", "MAM", "MAM"),
        post_values=np.array([[[[np.nan, 0.2]]]]),
        post_seasons=("MAM",),
        evaluable=np.array([[True, False]]),
    )

    assert np.isnan(result.raw_directional_delta).all()
    assert np.isnan(result.standardized_magnitude).all()
    assert result.reference_valid_count[0, 0, 0, 1] == 0


def test_2021_djf_straddling_cutoff_is_excluded_and_flagged() -> None:
    result = _diagnose(
        reference_values=np.array([[[[0.4]]], [[[0.5]]], [[[0.6]]]]),
        reference_seasons=("DJF", "DJF", "DJF"),
        post_values=np.array([[[[0.2]]]]),
        post_seasons=("DJF",),
        post_starts=(date(2020, 12, 1),),
        post_ends=(date(2021, 2, 28),),
    )

    assert np.isnan(result.raw_directional_delta).all()
    assert result.reference_valid_count[0, 0, 0, 0] == 0
    excluded_bit = np.uint16(1 << DisturbanceQualityBit.CUTOFF_STRADDLING_PERIOD_EXCLUDED)
    assert result.quality_flags_bitmask[0, 0, 0, 0] & excluded_bit


def test_diagnostics_are_immutable() -> None:
    result = _diagnose(
        reference_values=np.array([[[[0.4]]], [[[0.5]]], [[[0.6]]]]),
        reference_seasons=("MAM", "MAM", "MAM"),
        post_values=np.array([[[[0.5]]]]),
        post_seasons=("MAM",),
    )

    for array in (
        result.reference_center,
        result.robust_scale,
        result.raw_directional_delta,
        result.standardized_magnitude,
        result.reference_valid_count,
        result.quality_flags_bitmask,
    ):
        assert not array.flags.writeable


@pytest.mark.parametrize(
    ("reference_values", "post_values", "error"),
    [
        (np.ones((3, 1, 1)), np.ones((1, 1, 1, 1)), "cuatro dimensiones"),
        (np.ones((3, 1, 1, 1)), np.ones((1, 2, 1, 1)), "mismos índices"),
        (np.ones((3, 1, 1, 1)), np.ones((1, 1, 2, 1)), "forma espacial"),
    ],
)
def test_rejects_incompatible_array_shapes(
    reference_values: np.ndarray,
    post_values: np.ndarray,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        _diagnose(
            reference_values=reference_values,
            reference_seasons=("MAM",) * 3,
            post_values=post_values,
            post_seasons=("MAM",),
        )


def test_rejects_unknown_season_and_wrong_index_contract() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "default.yml")
    values = np.ones((3, 1, 1, 1))
    post = np.ones((1, 1, 1, 1))
    domain = _domain(np.ones((1, 1), dtype=np.bool_))

    with pytest.raises(ValueError, match="estación"):
        compute_robust_seasonal_diagnostics(
            reference_values=values,
            reference_seasons=("MAM", "MAM", "INVALID"),
            reference_period_start_dates=(
                date(2018, 3, 1),
                date(2019, 3, 1),
                date(2020, 3, 1),
            ),
            reference_period_end_dates_exclusive=(
                date(2018, 6, 1),
                date(2019, 6, 1),
                date(2020, 6, 1),
            ),
            post_cutoff_values=post,
            post_cutoff_seasons=("MAM",),
            post_period_start_dates=(date(2021, 3, 1),),
            post_period_end_dates_exclusive=(date(2021, 5, 31),),
            index_names=("NDVI",),
            domain=domain,
            config=config.disturbance_detection,
        )

    with pytest.raises(ValueError, match="detection_indices"):
        compute_robust_seasonal_diagnostics(
            reference_values=values,
            reference_seasons=("MAM",) * 3,
            reference_period_start_dates=(
                date(2018, 3, 1),
                date(2019, 3, 1),
                date(2020, 3, 1),
            ),
            reference_period_end_dates_exclusive=(
                date(2018, 6, 1),
                date(2019, 6, 1),
                date(2020, 6, 1),
            ),
            post_cutoff_values=post,
            post_cutoff_seasons=("MAM",),
            post_period_start_dates=(date(2021, 3, 1),),
            post_period_end_dates_exclusive=(date(2021, 5, 31),),
            index_names=("EVI2",),
            domain=domain,
            config=config.disturbance_detection,
        )


def test_rejects_period_fully_before_cutoff() -> None:
    with pytest.raises(ValueError, match="posteriores a la fecha de corte"):
        _diagnose(
            reference_values=np.array([[[[0.4]]], [[[0.5]]], [[[0.6]]]]),
            reference_seasons=("SON", "SON", "SON"),
            post_values=np.array([[[[0.2]]]]),
            post_seasons=("SON",),
            post_starts=(date(2020, 9, 1),),
            post_ends=(date(2020, 11, 30),),
        )


def test_rejects_reference_period_with_post_cutoff_leakage() -> None:
    with pytest.raises(ValueError, match="referencia debe ser completamente precorte"):
        _diagnose(
            reference_values=np.array([[[[0.4]]], [[[0.5]]], [[[0.6]]]]),
            reference_seasons=("MAM", "MAM", "MAM"),
            reference_ends_exclusive=(
                date(2018, 6, 1),
                date(2019, 6, 1),
                date(2021, 6, 1),
            ),
            post_values=np.array([[[[0.2]]]]),
            post_seasons=("MAM",),
        )


def test_rejects_empty_reference_period() -> None:
    with pytest.raises(ValueError, match="intervalo no vacío"):
        _diagnose(
            reference_values=np.array([[[[0.4]]], [[[0.5]]], [[[0.6]]]]),
            reference_seasons=("MAM", "MAM", "MAM"),
            reference_starts=(
                date(2018, 3, 1),
                date(2019, 3, 1),
                date(2020, 3, 1),
            ),
            reference_ends_exclusive=(
                date(2018, 3, 1),
                date(2019, 6, 1),
                date(2020, 6, 1),
            ),
            post_values=np.array([[[[0.2]]]]),
            post_seasons=("MAM",),
        )


def test_rejects_empty_post_cutoff_period() -> None:
    with pytest.raises(ValueError, match="intervalo no vacío"):
        _diagnose(
            reference_values=np.array([[[[0.4]]], [[[0.5]]], [[[0.6]]]]),
            reference_seasons=("MAM", "MAM", "MAM"),
            post_values=np.array([[[[0.2]]]]),
            post_seasons=("MAM",),
            post_starts=(date(2021, 3, 1),),
            post_ends=(date(2021, 3, 1),),
        )
