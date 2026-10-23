"""Ambient-light dimming curve tests (no hardware)."""

from __future__ import annotations

import pytest

from pylumicube.dimming import (
    DEFAULT_HIGH,
    DEFAULT_LOW,
    MIN_SPAN,
    BrightnessCurve,
    scale_colour,
)


# ----- fixed range --------------------------------------------------------


def test_fixed_range_maps_endpoints_and_midpoint() -> None:
    curve = BrightnessCurve(low=100, high=1100, minimum=0.0, maximum=1.0,
                            smoothing=0.0)
    assert curve.update(100) == pytest.approx(0.0)
    assert curve.update(600) == pytest.approx(0.5)
    assert curve.update(1100) == pytest.approx(1.0)


def test_readings_are_clamped_to_the_range() -> None:
    curve = BrightnessCurve(low=100, high=1100, minimum=0.1, maximum=0.9,
                            smoothing=0.0)
    assert curve.update(0) == pytest.approx(0.1)
    assert curve.update(99999) == pytest.approx(0.9)


def test_minimum_and_maximum_bound_the_output() -> None:
    curve = BrightnessCurve(low=0, high=1000, minimum=0.05, maximum=0.8,
                            smoothing=0.0)
    assert curve.update(500) == pytest.approx(0.05 + 0.5 * 0.75)
    assert curve.minimum <= curve.value <= curve.maximum


def test_gamma_brightens_or_darkens_midtones() -> None:
    linear = BrightnessCurve(low=0, high=1000, minimum=0.0, maximum=1.0,
                             smoothing=0.0)
    brighter = BrightnessCurve(low=0, high=1000, minimum=0.0, maximum=1.0,
                               gamma=0.5, smoothing=0.0)
    darker = BrightnessCurve(low=0, high=1000, minimum=0.0, maximum=1.0,
                             gamma=2.0, smoothing=0.0)
    assert linear.update(500) == pytest.approx(0.5)
    assert brighter.update(500) == pytest.approx(0.5 ** 0.5)
    assert darker.update(500) == pytest.approx(0.25)
    assert darker.value < linear.value < brighter.value


def test_fixed_range_does_not_adapt() -> None:
    curve = BrightnessCurve(low=100, high=1100, adapt=False, smoothing=0.0)
    curve.update(5000)
    curve.update(-50)
    assert curve.range == (100.0, 1100.0)


# ----- smoothing ---------------------------------------------------------


def test_smoothing_ramps_towards_the_target() -> None:
    curve = BrightnessCurve(low=0, high=1000, minimum=0.0, maximum=1.0,
                            smoothing=0.5)
    first = curve.update(1000)          # first sample jumps to the target
    assert first == pytest.approx(1.0)
    second = curve.update(0)            # then it eases down
    assert 0.0 < second < 1.0
    for _ in range(50):
        second = curve.update(0)
    assert second == pytest.approx(0.0, abs=0.01)


def test_no_smoothing_follows_the_target_immediately() -> None:
    curve = BrightnessCurve(low=0, high=1000, minimum=0.0, maximum=1.0,
                            smoothing=0.0)
    curve.update(1000)
    assert curve.update(0) == pytest.approx(0.0)


def test_value_is_none_before_the_first_update() -> None:
    assert BrightnessCurve().value is None


# ----- auto-ranging ------------------------------------------------------


def test_auto_range_seeds_from_measured_defaults() -> None:
    curve = BrightnessCurve()
    assert curve.range == (DEFAULT_LOW, DEFAULT_HIGH)


def test_auto_range_widens_to_include_new_extremes() -> None:
    curve = BrightnessCurve(low=100, high=1000, adapt=True, smoothing=0.0)
    curve.update(2000)
    assert curve.range[1] == 2000.0
    curve.update(20)
    assert curve.range[0] == 20.0


def test_auto_range_decays_a_stale_range() -> None:
    curve = BrightnessCurve(low=0, high=1000, adapt=True, decay=0.01,
                            smoothing=0.0)
    for _ in range(100):
        curve.update(500)
    low, high = curve.range
    assert low > 0.0 and high < 1000.0        # bounds crept inward
    assert 0.0 < curve.value < 1.0            # and it stayed mid-range


def test_auto_range_holds_brightness_when_the_range_is_too_narrow() -> None:
    curve = BrightnessCurve(low=100, high=100 + MIN_SPAN / 2, adapt=False,
                            minimum=0.0, maximum=1.0, smoothing=0.0)
    first = curve.update(100)
    assert first == pytest.approx(0.5)        # midpoint for the first sample
    assert curve.update(100) == pytest.approx(0.5)


def test_dim_reading_dims_and_bright_reading_brightens() -> None:
    """The property the clock actually needs."""
    curve = BrightnessCurve(smoothing=0.0)
    dark = curve.update(60)
    bright = curve.update(1900)
    assert dark < 0.1
    assert bright > 0.9


# ----- validation and colour scaling -------------------------------------


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        BrightnessCurve(minimum=0.9, maximum=0.1)
    with pytest.raises(ValueError):
        BrightnessCurve(gamma=0)
    with pytest.raises(ValueError):
        BrightnessCurve(smoothing=1.0)
    with pytest.raises(ValueError):
        BrightnessCurve(low=1000, high=100)


def test_scale_colour_is_per_channel() -> None:
    assert scale_colour(0xFF8000, 1.0) == 0xFF8000
    assert scale_colour(0xFF8000, 0.5) == 0x7F4000
    assert scale_colour(0xFFFFFF, 0.0) == 0x000000
    assert scale_colour(0x123456, 2.0) == 0x123456      # clamped at 1.0
    assert scale_colour(0x0000FF, 0.5) == 0x00007F


def test_scale_colour_preserves_hue() -> None:
    scaled = scale_colour(0x00FF00, 0.3)
    assert scaled == 0x004C00
