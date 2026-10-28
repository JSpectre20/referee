# SPDX-License-Identifier: Apache-2.0
"""cocotb tests for src/edge_ts.v, the edge-timestamp unit.

The pins are driven at random picosecond times, never aligned to the clock,
and every reported timestamp is checked against the true time of its edge.
In simulation nothing goes metastable, so the tighter simulation bounds apply:
latency in (1, 2] cycles and interval error below 1 cycle. The formal proof
(formal/edge_ts.sby) covers metastable samples and gives the contract bounds
[1, 3] and 2.
"""

import random

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer
from cocotb.utils import get_sim_time

PERIOD_PS = 25_000  # 40 MHz, set in hdl/edge_ts_tb.v
FIRST_EDGE_PS = 12_500  # first rising clock edge


def true_cycle(dut, t_ps):
    """Real time t as a fractional cycle index: cycle c is when now == c."""
    phase = ((t_ps - FIRST_EDGE_PS) % PERIOD_PS) / PERIOD_PS
    return int(dut.now.value) + phase


async def reset(dut, pins=0):
    dut.rst_n.value = 0
    dut.pins.value = pins
    dut.clear.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1


async def wait_pulse(dut, sig, mask, limit=10):
    """Wait (at falling edges) until sig & mask is set; fail after `limit` cycles."""
    for _ in range(limit):
        await FallingEdge(dut.clk)
        if int(sig.value) & mask:
            return
    raise AssertionError(f"no pulse on {sig._name} & {mask:#04b} within {limit} cycles")


class Monitor:
    """Records every reported edge: (pin, 'rise'|'fall', timestamp)."""

    def __init__(self, dut):
        self.dut = dut
        self.events = []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await FallingEdge(self.dut.clk)  # mid-cycle: outputs are stable
            rise, fall = int(self.dut.rise.value), int(self.dut.fall.value)
            for i in range(2):
                for kind, bits in (("rise", rise), ("fall", fall)):
                    if (bits >> i) & 1:
                        self.events.append((i, kind, int(self.dut.now.value)))


async def drive_pin(dut, pin, n_edges, rng, log):
    """Toggle one pin n_edges times at random sub-cycle times, holding each level 4-12 cycles."""
    for _ in range(n_edges):
        # Hold at least 4 full cycles (the unit's precondition), plus a random
        # fraction that puts the edge anywhere inside a cycle.
        hold_ps = rng.randrange(4 * PERIOD_PS + 1, 12 * PERIOD_PS)
        await Timer(hold_ps, unit="ps")
        t = get_sim_time(unit="ps")
        if (t - FIRST_EDGE_PS) % PERIOD_PS == 0:  # never exactly on a clock edge
            await Timer(1, unit="ps")
            t += 1
        cur = (int(dut.pins.value) >> pin) & 1
        log.append((pin, "fall" if cur else "rise", true_cycle(dut, t)))
        dut.pins.value = int(dut.pins.value) ^ (1 << pin)


# -----------------------------------------------------------------------------


@cocotb.test()
async def no_phantom_edge_out_of_reset(dut):
    """A pin that is high through reset is a level, not a rising edge."""
    await reset(dut, pins=0b11)
    mon = Monitor(dut)
    for _ in range(10):
        await RisingEdge(dut.clk)
    assert mon.events == [], f"phantom edges: {mon.events}"
    assert int(dut.toggled.value) == 0
    assert int(dut.level.value) == 0b11


@cocotb.test()
async def timestamps_match_true_time(dut):
    """Random edges on both pins: every edge reported once, with honest timestamps."""
    rng = random.Random(2026)
    await reset(dut)
    mon = Monitor(dut)
    truth = []
    n = 400
    d0 = cocotb.start_soon(drive_pin(dut, 0, n, rng, truth))
    d1 = cocotb.start_soon(drive_pin(dut, 1, n, rng, truth))
    await d0
    await d1
    await Timer(10 * PERIOD_PS, unit="ps")

    worst = [0.0, 0.0]
    for pin in (0, 1):
        want = [(k, t) for p, k, t in truth if p == pin]
        got = [(k, ts) for p, k, ts in mon.events if p == pin]
        assert len(got) == len(want), f"pin {pin}: {len(got)} reported, {len(want)} real edges"
        for (wk, wt), (gk, gts) in zip(want, got):
            assert wk == gk, f"pin {pin}: edge kinds out of step"
            # The 16-bit counter does not wrap in this test (< 65536 cycles).
            lat = gts - wt
            assert 1.0 < lat <= 2.0, f"pin {pin} {wk} at {wt:.3f}: latency {lat:.3f} cycles"
            worst[0] = max(worst[0], lat)

    # Intervals: every pulse width on each pin, and pin 0 -> pin 1 edge distances.
    for pin in (0, 1):
        want = [t for p, _, t in truth if p == pin]
        got = [ts for p, _, ts in mon.events if p == pin]
        for i in range(1, len(want)):
            err = (got[i] - got[i - 1]) - (want[i] - want[i - 1])
            assert abs(err) < 1.0, f"pin {pin} interval {i}: error {err:.3f} cycles"
            worst[1] = max(worst[1], abs(err))
    t0 = [t for p, _, t in truth if p == 0]
    g0 = [ts for p, _, ts in mon.events if p == 0]
    t1 = [t for p, _, t in truth if p == 1]
    g1 = [ts for p, _, ts in mon.events if p == 1]
    for a, ga in zip(t0, g0):
        for b, gb in zip(t1, g1):
            if abs(b - a) < 50:
                err = (gb - ga) - (b - a)
                assert abs(err) < 1.0, f"cross-pin error {err:.3f} cycles"
                worst[1] = max(worst[1], abs(err))
    dut._log.info("%d edges: worst latency %.3f cycles, worst interval error %.3f cycles", 2 * n, *worst)


@cocotb.test()
async def latches_hold_and_report_now(dut):
    """rise_ts/fall_ts change only on a reported edge, to `now` of that cycle."""
    await reset(dut)
    await Timer(3 * PERIOD_PS + 7_000, unit="ps")
    dut.pins.value = 0b01
    await wait_pulse(dut, dut.rise, 0b01)
    ts = int(dut.now.value)
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    assert int(dut.rise_ts.value) & 0xFFFF == ts
    for _ in range(20):
        await FallingEdge(dut.clk)
        assert int(dut.rise_ts.value) & 0xFFFF == ts, "latch moved without an edge"
    assert (int(dut.fall_ts.value) & 0xFFFF) == 0


@cocotb.test()
async def edge_beats_clear(dut):
    """`toggled` is sticky; clear removes it, except when an edge lands in the same cycle."""
    await reset(dut)
    for _ in range(4):  # past the 3-cycle warm-up, during which an edge is only a level
        await FallingEdge(dut.clk)
    dut.pins.value = 0b10
    await wait_pulse(dut, dut.rise, 0b10)
    await FallingEdge(dut.clk)
    assert int(dut.toggled.value) == 0b10

    dut.clear.value = 0b10  # plain clear
    await FallingEdge(dut.clk)
    dut.clear.value = 0
    assert int(dut.toggled.value) == 0b00

    for _ in range(4):
        await FallingEdge(dut.clk)
    dut.pins.value = 0b00
    await wait_pulse(dut, dut.fall, 0b10)
    dut.clear.value = 0b10  # clear in the same cycle as the edge: the edge wins
    await FallingEdge(dut.clk)
    dut.clear.value = 0
    assert int(dut.toggled.value) == 0b10


# -----------------------------------------------------------------------------
# Flags: stale timestamps and missed edges
# -----------------------------------------------------------------------------

QUARTER = 1 << 14  # 2**(TS_W-2) with TS_W = 16


async def settle(dut, cycles=6):
    """Get past the 3-cycle warm-up after reset."""
    for _ in range(cycles):
        await FallingEdge(dut.clk)


@cocotb.test()
async def stale_out_of_reset_and_after_a_quarter_range(dut):
    """Timestamps are stale until an edge is latched, fresh after it, stale again QUARTER cycles later."""
    await reset(dut)
    await settle(dut)
    assert int(dut.rise_stale.value) == 0b11 and int(dut.fall_stale.value) == 0b11, "nothing latched yet"

    dut.pins.value = 0b01
    await wait_pulse(dut, dut.rise, 0b01)
    ts = int(dut.now.value)
    await FallingEdge(dut.clk)
    assert int(dut.rise_stale.value) & 1 == 0, "fresh right after the rise is latched"
    assert int(dut.fall_stale.value) & 1 == 1, "no fall latched yet"

    # Age = now - ts. The flag must still be clear at age QUARTER and set from QUARTER + 1.
    await ClockCycles(dut.clk, QUARTER - 2)
    while (int(dut.now.value) - ts) % (1 << 16) != QUARTER:
        await FallingEdge(dut.clk)
    assert int(dut.rise_stale.value) & 1 == 0, "age QUARTER: still fresh"
    await FallingEdge(dut.clk)
    assert int(dut.rise_stale.value) & 1 == 1, "age QUARTER + 1: stale"

    dut.pins.value = 0b00  # a new edge (the fall) clears fall_stale, not rise_stale
    await wait_pulse(dut, dut.fall, 0b01)
    await FallingEdge(dut.clk)
    assert int(dut.fall_stale.value) & 1 == 0 and int(dut.rise_stale.value) & 1 == 1


@cocotb.test()
async def missed_set_by_a_one_sample_level(dut):
    """A level that lasts exactly one sample period puts two transitions in the synchronizer: missed."""
    await reset(dut)
    await settle(dut)
    # Mid-cycle (falling clock edge) to mid-cycle: sampled high at exactly one rising edge.
    dut.pins.value = 0b01
    await FallingEdge(dut.clk)
    dut.pins.value = 0b00
    for _ in range(6):
        await FallingEdge(dut.clk)
    assert int(dut.missed.value) == 0b01, f"missed = {int(dut.missed.value):#04b}"

    # Sticky until cleared; a clear removes it.
    for _ in range(10):
        await FallingEdge(dut.clk)
    assert int(dut.missed.value) == 0b01, "missed is sticky"
    dut.clear.value = 0b01
    await FallingEdge(dut.clk)
    dut.clear.value = 0
    assert int(dut.missed.value) == 0b00


@cocotb.test()
async def missed_not_set_by_legal_pulses(dut):
    """Pulses that respect the 4-cycle hold, at any sub-cycle offset, never set missed."""
    rng = random.Random(99)
    await reset(dut)
    await settle(dut)
    log = []
    await drive_pin(dut, 0, 200, rng, log)  # holds >= 4 cycles + a random fraction
    await Timer(10 * PERIOD_PS, unit="ps")
    assert int(dut.missed.value) == 0


@cocotb.test()
async def pulse_between_samples_is_invisible(dut):
    """The documented limit: a 5 ns pulse between two clock edges leaves no trace at all."""
    await reset(dut)
    await settle(dut)
    await FallingEdge(dut.clk)  # mid-cycle: the next sample is 12.5 ns away
    dut.pins.value = 0b01
    await Timer(5_000, unit="ps")
    dut.pins.value = 0b00
    for _ in range(8):
        await FallingEdge(dut.clk)
        assert int(dut.rise.value) == 0 and int(dut.fall.value) == 0
    assert int(dut.missed.value) == 0 and int(dut.toggled.value) == 0, "nothing was sampled, so nothing can be flagged"
