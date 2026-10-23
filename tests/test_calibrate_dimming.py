"""Tests for `scripts/calibrate_dimming.py` (no hardware).

Only the pure parts are covered — level parsing, the gamma solve, the config
suggestion and the light-log report; the measuring loop needs a cube.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "calibrate_dimming.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("calibrate_dimming_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


calibrate = _load_script()


# ----- level parsing ------------------------------------------------------


def test_parse_levels_sorts_and_deduplicates() -> None:
    assert calibrate.parse_levels("0.6, 0.2,0.6,1") == [0.2, 0.6, 1.0]


@pytest.mark.parametrize("text", ["", "  ", "1.5", "-0.1"])
def test_parse_levels_rejects_nonsense(text) -> None:
    with pytest.raises(ValueError):
        calibrate.parse_levels(text)


# ----- gamma solve --------------------------------------------------------


def test_suggest_gamma_hits_the_requested_brightness() -> None:
    """The gamma it returns really does put `level` at `ambient`."""
    gamma = calibrate.suggest_gamma(110, 0.45, 20, 1300)
    assert gamma is not None and 0 < gamma < 1
    position = (110 - 20) / (1300 - 20)
    assert calibrate.SUGGEST_FLOOR + position ** gamma * (
        calibrate.SUGGEST_CEILING - calibrate.SUGGEST_FLOOR) == pytest.approx(0.45)


@pytest.mark.parametrize("ambient,low,high", [
    (10, 20, 1300),        # below the range
    (2000, 20, 1300),      # above it
    (100, 20, 20),         # empty range
])
def test_suggest_gamma_gives_up_gracefully(ambient, low, high) -> None:
    assert calibrate.suggest_gamma(ambient, 0.45, low, high) is None


# ----- config suggestion --------------------------------------------------


def test_suggest_config_offers_both_strategies() -> None:
    text = calibrate.suggest_config([(0.2, [100, 104]), (0.45, [110, 112])], 0.45)
    assert "ADAPT = True" in text
    assert "BRIGHTNESS_MIN = 0.45" in text
    assert "ADAPT = False" in text
    assert "LOW = 100" in text and "HIGH = 112" in text
    assert "GAMMA = " in text


def test_suggest_config_without_a_readable_level_says_so() -> None:
    text = calibrate.suggest_config([(0.2, [100])], None)
    assert "Nothing was marked readable" in text
    assert "BRIGHTNESS_MIN" not in text


def test_suggest_config_without_readings_only_offers_the_floor() -> None:
    text = calibrate.suggest_config([(0.45, [])], 0.45)
    assert "BRIGHTNESS_MIN = 0.45" in text
    assert "needs ambient readings" in text


# ----- report -------------------------------------------------------------


def test_report_buckets_the_log_by_hour(tmp_path, capsys) -> None:
    log = tmp_path / "light.csv"
    log.write_text(
        "timestamp,ambient,brightness,low,high\n"
        "2026-10-23T18:00:01,118,0.450,72.0,1869.0\n"
        "2026-10-23T18:02:01,120,0.450,72.0,1869.0\n"
        "2026-10-23T23:00:01,30,0.300,20.0,1869.0\n"
        "not-a-row\n"
    )
    assert calibrate.report(str(log)) == 0
    out = capsys.readouterr().out
    assert "  18     2          118     119     120" in out
    assert "  23     1           30      30      30" in out


def test_report_handles_a_missing_file(tmp_path, capsys) -> None:
    assert calibrate.report(str(tmp_path / "nope.csv")) == 1
    assert "cannot read" in capsys.readouterr().out
