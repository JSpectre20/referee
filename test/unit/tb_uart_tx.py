# SPDX-License-Identifier: Apache-2.0
"""Unit tests for src/uart_tx.v (top level: hdl/uart_tx_tb.v).

Every change on the TX line is timestamped, then turned back into one level
per clock cycle and decoded strictly: each bit must last exactly `div`
cycles, start low, stop high, and back-to-back frames must have no gap.
cocotbext-uart is used as an independent second check.
"""

import random

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, Timer, ValueChange
from cocotb.utils import get_sim_time
from cocotbext.uart import UartSink

CLK_PS = 25_000  # 40 MHz, set in hdl/uart_tx_tb.v. Falling edges are at multiples of CLK_PS.


async def cycles(dut, n):
    await Timer(n * CLK_PS, unit="ps")
    await RisingEdge(dut.clk)


class EdgeRecorder:
    """Timestamps every change of a 1-bit signal."""

    def __init__(self, sig):
        self.sig = sig
        self.t0 = get_sim_time(unit="ps")
        self.v0 = int(sig.value)
        self.edges = []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await ValueChange(self.sig)
            self.edges.append((get_sim_time(unit="ps"), int(self.sig.value)))

    def levels(self):
        """Signal level in each clock cycle (sampled at the falling edge)."""
        t_end = get_sim_time(unit="ps")
        out = []
        v = self.v0
        i = 0
        t = (self.t0 // CLK_PS + 1) * CLK_PS
        while t < t_end:
            while i < len(self.edges) and self.edges[i][0] <= t:
                v = self.edges[i][1]
                i += 1
            out.append(v)
            t += CLK_PS
        return out

    def rise_times(self):
        return [t for t, v in self.edges if v == 1]


def decode(levels, div):
    """Return [(start_cycle, byte)], asserting exact 8N1 timing."""
    frames = []
    i = 0
    while i < len(levels):
        if levels[i] == 1:
            i += 1
            continue
        assert i + 10 * div <= len(levels), f"frame at cycle {i} cut off"
        bits = []
        for k in range(10):
            chunk = levels[i + k * div : i + (k + 1) * div]
            assert len(set(chunk)) == 1, f"bit {k} of frame at cycle {i} is not stable for {div} cycles: {chunk}"
            bits.append(chunk[0])
        assert bits[0] == 0, f"bad start bit at cycle {i}"
        assert bits[9] == 1, f"bad stop bit at cycle {i}"
        frames.append((i, sum(b << k for k, b in enumerate(bits[1:9]))))
        i += 10 * div
    return frames


async def reset(dut, div):
    dut.div.value = div
    dut.wr_en.value = 0
    dut.wr_data.value = 0
    dut.rst_n.value = 0
    await cycles(dut, 3)
    dut.rst_n.value = 1
    await cycles(dut, 3)


async def write_stream(dut, data, gap_chance=0.0):
    """Write bytes as fast as the holding register allows (or with random gaps)."""
    queue = list(data)
    while queue:
        await FallingEdge(dut.clk)
        if dut.hold_empty.value == 1 and random.random() >= gap_chance:
            dut.wr_data.value = queue.pop(0)
            dut.wr_en.value = 1
        else:
            dut.wr_en.value = 0
            if dut.hold_empty.value == 0:
                await RisingEdge(dut.hold_empty)
    await FallingEdge(dut.clk)
    dut.wr_en.value = 0


async def wait_idle(dut):
    if dut.idle.value != 1:
        await RisingEdge(dut.idle)


@cocotb.test()
async def tx_exact_timing_back_to_back(dut):
    for div in (4, 5, 16, 347):
        await reset(dut, div)
        line = EdgeRecorder(dut.tx)
        data = [0x00, 0xFF, 0x55, 0xAA] + [random.randrange(256) for _ in range(40)]
        await write_stream(dut, data)
        await wait_idle(dut)
        await cycles(dut, 2 * div)
        frames = decode(line.levels(), div)
        assert [b for _, b in frames] == data, f"div={div}"
        starts = [s for s, _ in frames]
        gaps = {b - a for a, b in zip(starts, starts[1:])}
        assert gaps == {10 * div}, f"div={div}: frames not back to back, spacing {gaps}"


@cocotb.test()
async def tx_with_random_gaps(dut):
    div = 7
    await reset(dut, div)
    line = EdgeRecorder(dut.tx)
    data = [random.randrange(256) for _ in range(80)]
    await write_stream(dut, data, gap_chance=0.97)
    await wait_idle(dut)
    await cycles(dut, 2 * div)
    assert [b for _, b in decode(line.levels(), div)] == data


@cocotb.test()
async def tx_idle_only_after_stop_bit(dut):
    """`idle` must not rise until the stop bit has fully gone out."""
    div = 16
    await reset(dut, div)
    line = EdgeRecorder(dut.tx)
    idle = EdgeRecorder(dut.idle)
    await write_stream(dut, [0x3C])
    await wait_idle(dut)
    await cycles(dut, 2 * div)
    start_t = next(t for t, v in line.edges if v == 0)
    frame_end_t = start_t + 10 * div * CLK_PS
    idle_t = idle.rise_times()[-1]
    assert idle_t >= frame_end_t, f"idle rose at {idle_t} ps, frame ends at {frame_end_t} ps"
    assert line.edges[-1][1] == 1 and line.edges[-1][0] < frame_end_t


@cocotb.test()
async def tx_quiet_at_last_stop_bit(dut):
    """`quiet` rises exactly when the last queued byte's stop bit starts."""
    div = 16
    await reset(dut, div)
    line = EdgeRecorder(dut.tx)
    quiet = EdgeRecorder(dut.quiet)
    await write_stream(dut, [0x00, 0x00])  # bit 7 low, so the stop bit is a rising edge
    await wait_idle(dut)
    await cycles(dut, 2 * div)
    starts = [t for t, v in line.edges if v == 0]
    assert len(starts) == 2
    stop_bit_t = starts[1] + 9 * div * CLK_PS
    rises = [t for t in quiet.rise_times() if t > starts[0]]
    assert rises == [stop_bit_t], f"quiet rose at {rises}, last stop bit starts at {stop_bit_t}"


@cocotb.test()
async def tx_against_cocotbext_uart(dut):
    """Second opinion from an independent UART model (1 Mbaud, divisor 40)."""
    div = 40
    await reset(dut, div)
    sink = UartSink(dut.tx, baud=40_000_000 // div, bits=8)
    data = bytes(random.randrange(256) for _ in range(64))
    await write_stream(dut, data)
    await wait_idle(dut)
    await cycles(dut, 2 * div)
    assert bytes(sink.read_nowait()) == data
