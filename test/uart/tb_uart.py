# SPDX-License-Identifier: Apache-2.0
"""cocotb tests for src/uart_tx.v and src/uart_rx.v.

The independent oracle is cocotbext-uart (UartSink decodes our TX, UartSource
drives our RX). Hand-made waveforms cover what a well-behaved peer never
sends: glitches, spikes, framing errors and breaks. Runs at CLKS=16 (fast) and
at CLKS=347 (115200 baud at 40 MHz); see test_uart.py.
"""

import os
import random

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, Timer
from cocotbext.uart import UartSink, UartSource

CLK_HZ = 40_000_000
CLKS = int(os.environ.get("CLKS", "16"))
BIT_PS = CLKS * 25_000
FAST = CLKS <= 32  # fewer bytes at the real baud rate, to keep the run short


async def reset(dut):
    dut.rst_n.value = 0
    dut.tx_start.value = 0
    dut.rx_in.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1


async def send(dut, b):
    """Start one byte on the transmitter: wait until idle, pulse start for one cycle."""
    while int(dut.tx_busy.value):
        await FallingEdge(dut.clk)
    dut.tx_data.value = b
    dut.tx_start.value = 1
    await FallingEdge(dut.clk)
    dut.tx_start.value = 0
    dut.tx_data.value = (~b) & 0xFF  # change data mid-frame: must not matter


class Collector:
    """Records (kind, byte) for every valid / frame_err pulse of one receiver."""

    def __init__(self, dut, prefix):
        self.dut, self.items = dut, []
        self.valid = getattr(dut, f"{prefix}_valid")
        self.data = getattr(dut, f"{prefix}_data")
        self.ferr = getattr(dut, f"{prefix}_ferr")
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await FallingEdge(self.dut.clk)
            if int(self.valid.value):
                self.items.append(("byte", int(self.data.value)))
            if int(self.ferr.value):
                self.items.append(("ferr", None))

    def bytes(self):
        return bytes(b for k, b in self.items if k == "byte")


async def drive_levels(dut, levels, bit_ps=BIT_PS):
    for lvl in levels:
        dut.rx_in.value = lvl
        await Timer(bit_ps, unit="ps")


def frame(b, stop=1):
    return [0] + [(b >> i) & 1 for i in range(8)] + [stop]


def byte_set():
    edge = [0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80]
    if FAST:
        return list(range(256))
    rng = random.Random(7)
    return edge + [rng.randrange(256) for _ in range(10)]


# -----------------------------------------------------------------------------
# Transmitter
# -----------------------------------------------------------------------------


@cocotb.test()
async def tx_idle_high_through_reset(dut):
    """tx is 1 while in reset and after it; busy is 0."""
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    assert int(dut.tx.value) == 1, "tx must be high (idle) during reset"
    dut.rst_n.value = 1
    for _ in range(5):
        await FallingEdge(dut.clk)
        assert int(dut.tx.value) == 1 and int(dut.tx_busy.value) == 0


@cocotb.test()
async def tx_decoded_by_cocotbext(dut):
    """Bytes sent back to back decode correctly in an independent UART model."""
    await reset(dut)
    sink = UartSink(dut.tx, baud=CLK_HZ / CLKS, bits=8)
    data = byte_set()
    for b in data:
        await send(dut, b)
    while int(dut.tx_busy.value):
        await FallingEdge(dut.clk)
    await Timer(2 * BIT_PS, unit="ps")
    got = bytes(sink.read_nowait(sink.count()))
    assert got == bytes(data), f"decoded {got.hex()}"


@cocotb.test()
async def tx_exact_frame_and_busy(dut):
    """Start accepted in cycle t: frame on tx in cycles t+1 .. t+10*CLKS, busy exactly then."""
    await reset(dut)
    b = 0xC5
    await FallingEdge(dut.clk)
    dut.tx_data.value = b
    dut.tx_start.value = 1  # accepted in this cycle (t)
    await FallingEdge(dut.clk)
    dut.tx_start.value = 0
    trace, busy = [], []
    for _ in range(10 * CLKS + 5):
        trace.append(int(dut.tx.value))
        busy.append(int(dut.tx_busy.value))
        await FallingEdge(dut.clk)
    want = [lvl for lvl in frame(b) for _ in range(CLKS)] + [1] * 5
    assert trace == want, "frame bits or timing wrong"
    assert busy == [1] * (10 * CLKS) + [0] * 5, "busy must be high for exactly 10*CLKS cycles"


@cocotb.test()
async def tx_start_while_busy_ignored(dut):
    """A start pulse in the middle of a frame neither restarts nor queues anything."""
    await reset(dut)
    sink = UartSink(dut.tx, baud=CLK_HZ / CLKS, bits=8)
    await send(dut, 0x3C)
    for _ in range(4 * CLKS):
        await FallingEdge(dut.clk)
    dut.tx_data.value = 0x99
    dut.tx_start.value = 1
    await FallingEdge(dut.clk)
    dut.tx_start.value = 0
    await Timer(14 * BIT_PS, unit="ps")
    assert bytes(sink.read_nowait(sink.count())) == b"\x3c"


# -----------------------------------------------------------------------------
# Receiver
# -----------------------------------------------------------------------------


@cocotb.test()
async def rx_from_cocotbext(dut):
    """Bytes from an independent UART model are all received, in order."""
    await reset(dut)
    col = Collector(dut, "rx")
    src = UartSource(dut.rx_in, baud=CLK_HZ / CLKS, bits=8)
    data = byte_set()
    await src.write(bytes(data))
    await src.wait()
    await Timer(2 * BIT_PS, unit="ps")
    assert col.bytes() == bytes(data)
    assert ("ferr", None) not in col.items


@cocotb.test()
async def rx_tolerates_baud_error(dut):
    """A sender 3% fast or 3% slow is still received without errors."""
    for err in (+0.03, -0.03):
        await reset(dut)
        col = Collector(dut, "rx")
        src = UartSource(dut.rx_in, baud=CLK_HZ / CLKS * (1 + err), bits=8)
        rng = random.Random(int(err * 1000))
        data = bytes(rng.randrange(256) for _ in range(40 if FAST else 8)) + b"\x00\xff\x55"
        await src.write(data)
        await src.wait()
        await Timer(2 * BIT_PS, unit="ps")
        src._run_cr.cancel()
        assert col.bytes() == data, f"baud error {err:+.0%}"
        assert ("ferr", None) not in col.items


@cocotb.test()
async def rx_rejects_short_glitches(dut):
    """Low pulses shorter than ~half a bit on an idle line report nothing."""
    await reset(dut)
    col = Collector(dut, "rx")
    for width in sorted({1, 2, 3, CLKS // 2 - 3}):
        if width < 1:
            continue
        dut.rx_in.value = 0
        await Timer(width * 25_000, unit="ps")
        dut.rx_in.value = 1
        await Timer(2 * BIT_PS, unit="ps")
    assert col.items == [], f"glitches reported as data: {col.items}"
    # and the receiver still works afterwards
    await drive_levels(dut, frame(0x5A) + [1])
    assert col.bytes() == b"\x5a"


@cocotb.test()
async def rx_majority_outvotes_a_spike(dut):
    """A one-cycle inverted spike right at the middle of every bit changes nothing."""
    await reset(dut)
    col = Collector(dut, "rx")
    b = 0x96
    for lvl in frame(b):
        dut.rx_in.value = lvl
        await Timer(BIT_PS // 2, unit="ps")
        dut.rx_in.value = 1 - lvl
        await Timer(25_000, unit="ps")
        dut.rx_in.value = lvl
        await Timer(BIT_PS - BIT_PS // 2 - 25_000, unit="ps")
    dut.rx_in.value = 1
    await Timer(2 * BIT_PS, unit="ps")
    assert col.items == [("byte", b)]


@cocotb.test()
async def rx_framing_error_and_break(dut):
    """Low stop bit: one frame_err, byte dropped. A long break: still one frame_err."""
    await reset(dut)
    col = Collector(dut, "rx")
    await drive_levels(dut, frame(0x33, stop=0) + [1, 1])
    assert col.items == [("ferr", None)], col.items
    col.items.clear()
    await drive_levels(dut, [0] * 30 + [1, 1])  # break: 30 bit times low
    assert col.items == [("ferr", None)], col.items
    col.items.clear()
    await drive_levels(dut, frame(0xA7) + [1])  # and a good byte afterwards
    assert col.items == [("byte", 0xA7)], col.items


# -----------------------------------------------------------------------------
# Loopback
# -----------------------------------------------------------------------------


@cocotb.test()
async def loopback_tx_to_rx(dut):
    """Our transmitter into our receiver: every byte back, in order, no errors."""
    await reset(dut)
    col = Collector(dut, "lb")
    rng = random.Random(42)
    data = bytes(rng.randrange(256) for _ in range(200 if FAST else 12))
    for b in data:
        await send(dut, b)
    while int(dut.tx_busy.value):
        await FallingEdge(dut.clk)
    await Timer(2 * BIT_PS, unit="ps")
    assert col.bytes() == data
    assert ("ferr", None) not in col.items
