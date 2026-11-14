# SPDX-License-Identifier: Apache-2.0
"""Checks of the bench itself (spike/bench.py) that the UART and SPI tests do not reach:
open-drain nets with a pull-up shared by two engines (as I2C will need), driver conflicts, and
the sticky-status guard."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench import Bench, BenchError  # noqa: E402

PULSE = """
        mark
        waitd 20
        set  2, 0
        waitd 10
        set  2, 1
        halt
"""


def test_open_drain_wired_and_and_one_cycle_latency():
    b = Bench()
    b.net("sda", pull=1)
    a = b.engine("A", {"uio2": "sda"}).load(PULSE, {"uio2": "od"})
    c = b.engine("B", {"uio5": "sda"}).load(PULSE.replace("20", "25").replace("set  2", "set  5"), {"uio5": "od"})
    b.run(400)
    w = b.wave("sda")
    low = [i for i, v in enumerate(w) if v == 0]
    start = low[0]
    # A pulls low for 10 cycles from start, B 5 cycles later for 10: the wired AND is 15 cycles low
    assert low == list(range(start, start + 15))
    assert a.model.sticky["PIN_FAULT"] == 0 and c.model.sticky["PIN_FAULT"] == 0


def test_push_pull_conflict_is_an_error():
    b = Bench()
    b.engine("A", {"uio0": "x"}).load("set 0, 1\nhalt", {"uio0": "pp"})
    b.engine("B", {"uio0": "x"}).load("set 0, 0\nhalt", {"uio0": "pp"})
    with pytest.raises(BenchError, match="driven to both levels"):
        b.run(400)


def test_forbidden_sticky_status_fails_loudly():
    b = Bench()
    b.engine("A", {}).load("pull\nhalt", {}, forbid=("IN_UNDERRUN",))
    with pytest.raises(BenchError, match="IN_UNDERRUN"):
        b.run(400)
