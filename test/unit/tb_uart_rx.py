# SPDX-License-Identifier: Apache-2.0
"""Unit tests for src/uart_rx.v (top level: hdl/uart_rx_tb.v).

The stimulus is a Python bit-banger with picosecond timing, so tests can skew
the baud rate, inject glitches and send bad stop bits. One test also uses
cocotbext-uart, an independent UART model, as a second opinion.

The clock comes from the Verilog wrapper. Python only wakes on timers and on
the receiver's output pulses, which keeps long tests fast.
"""

import random

import cocotb
from cocotb.triggers import FallingEdge, First, ReadOnly, RisingEdge, Timer
from cocotbext.uart import UartSource

CLK_PS = 25_000  # 40 MHz, set in hdl/uart_rx_tb.v


async def cycles(dut, n):
    await Timer(n * CLK_PS, unit="ps")
    await RisingEdge(dut.clk)


class RxMonitor:
    """Collects every byte and framing error the receiver reports."""

    def __init__(self, dut):
        self.dut = dut
        self.data = []
        self.frame_errs = 0
        cocotb.start_soon(self._run())

    def clear(self):
        self.data = []
        self.frame_errs = 0

    async def _run(self):
        while True:
            await First(RisingEdge(self.dut.valid), RisingEdge(self.dut.frame_err))
            await ReadOnly()
            if self.dut.valid.value == 1:
                self.data.append(int(self.dut.data.value))
            if self.dut.frame_err.value == 1:
                self.frame_errs += 1
            await FallingEdge(self.dut.clk)


class Bench:
    """One monitor per test; `reset` can be called many times."""

    def __init__(self, dut):
        self.dut = dut
        self.mon = RxMonitor(dut)

    async def reset(self, div):
        self.dut.div.value = div
        self.dut.rx.value = 1
        self.dut.rst_n.value = 0
        await cycles(self.dut, 5)
        self.dut.rst_n.value = 1
        await cycles(self.dut, 5)
        self.mon.clear()
        return self.mon


async def setup(dut, div):
    bench = Bench(dut)
    return await bench.reset(div)


def bit_time_ps(div, err=0.0):
    """Bit period of a host whose baud rate is off by `err` (0.03 = 3% fast)."""
    return round(div * CLK_PS / (1.0 + err))


async def send_byte(sig, value, bit_ps, stop_level=1):
    sig.value = 0
    await Timer(bit_ps, unit="ps")
    for k in range(8):
        sig.value = (value >> k) & 1
        await Timer(bit_ps, unit="ps")
    sig.value = stop_level
    await Timer(bit_ps, unit="ps")
    sig.value = 1


async def send_bytes(dut, data, div, err=0.0):
    # Random phase against the clock so edges don't always line up.
    await Timer(random.randrange(1, CLK_PS), unit="ps")
    for b in data:
        await send_byte(dut.rx, b, bit_time_ps(div, err))


async def settle(dut, div):
    await cycles(dut, 3 * div)


PATTERN = [0x00, 0xFF, 0x55, 0xAA, 0x0F, 0xF0, 0x01, 0x80]


@cocotb.test()
async def rx_random_bytes_back_to_back(dut):
    """200 random bytes with no idle time between them."""
    div = 16
    mon = await setup(dut, div)
    data = PATTERN + [random.randrange(256) for _ in range(200)]
    await send_bytes(dut, data, div)
    await settle(dut, div)
    assert mon.data == data
    assert mon.frame_errs == 0


@cocotb.test()
async def rx_default_divisor(dut):
    """The real setting: 115200 baud at 40 MHz (divisor 347)."""
    div = 347
    mon = await setup(dut, div)
    data = PATTERN + [random.randrange(256) for _ in range(40)]
    await send_bytes(dut, data, div)
    await settle(dut, div)
    assert mon.data == data
    assert mon.frame_errs == 0


@cocotb.test()
async def rx_odd_and_small_divisors(dut):
    bench = Bench(dut)
    for div in (4, 5, 7, 23, 100):
        mon = await bench.reset(div)
        data = PATTERN + [random.randrange(256) for _ in range(30)]
        await send_bytes(dut, data, div)
        await settle(dut, div)
        assert mon.data == data, f"div={div}"
        assert mon.frame_errs == 0, f"div={div}"


@cocotb.test()
async def rx_baud_tolerance(dut):
    """The host's baud rate can be a few percent off and still work."""
    bench = Bench(dut)
    for div, errors in ((347, (-0.04, -0.03, 0.03, 0.04)), (16, (-0.03, 0.03))):
        mon = await bench.reset(div)
        for err in errors:
            mon.clear()
            data = PATTERN + [random.randrange(256) for _ in range(20)]
            await send_bytes(dut, data, div, err)
            await settle(dut, div)
            assert mon.data == data, f"div={div} err={err:+.1%}"
            assert mon.frame_errs == 0, f"div={div} err={err:+.1%}"


@cocotb.test()
async def rx_tolerance_window(dut):
    """Measure how far off the host can be, and require at least +/-4%."""
    div = 347
    mon = await setup(dut, div)

    async def trial(err):
        mon.clear()
        await send_bytes(dut, PATTERN, div, err)
        await settle(dut, div)
        ok = mon.data == PATTERN and mon.frame_errs == 0
        await cycles(dut, 20 * div)  # recover from any mess
        return ok

    window = {}
    for sign in (+1, -1):
        last_ok = 0.0
        for step in range(1, 21):
            err = sign * step * 0.005
            if not await trial(err):
                break
            last_ok = err
        window[sign] = last_ok
    dut._log.info("RX accepts host baud error from %+.1f%% to %+.1f%%", 100 * window[-1], 100 * window[+1])
    assert window[+1] >= 0.04 and window[-1] <= -0.04


@cocotb.test()
async def rx_ignores_glitches(dut):
    """Short low pulses on an idle line must not produce bytes or errors.

    Glitches are spaced more than a frame apart: two glitches exactly one
    start-bit apart are indistinguishable from a real 0xFF byte.
    """
    bench = Bench(dut)
    for div in (16, 347):
        mon = await bench.reset(div)
        for _ in range(30):
            await Timer(random.randrange(12 * div * CLK_PS, 20 * div * CLK_PS), unit="ps")
            width = random.randrange(1, max(2, div // 4)) * CLK_PS
            dut.rx.value = 0
            await Timer(width, unit="ps")
            dut.rx.value = 1
        await settle(dut, div)
        assert mon.data == [], f"div={div}"
        assert mon.frame_errs == 0, f"div={div}"
        # Still works afterwards
        await send_bytes(dut, [0x5A, 0xC3], div)
        await settle(dut, div)
        assert mon.data == [0x5A, 0xC3]


@cocotb.test()
async def rx_frame_error(dut):
    """A low stop bit drops the byte and pulses frame_err once."""
    div = 16
    mon = await setup(dut, div)
    await send_byte(dut.rx, 0x00, bit_time_ps(div), stop_level=0)
    await settle(dut, div)
    assert mon.data == []
    assert mon.frame_errs == 1
    await send_bytes(dut, [0x42], div)
    await settle(dut, div)
    assert mon.data == [0x42]


@cocotb.test()
async def rx_break(dut):
    """A long break is one framing error, then the receiver recovers."""
    div = 16
    mon = await setup(dut, div)
    dut.rx.value = 0
    await cycles(dut, 35 * div)
    dut.rx.value = 1
    await settle(dut, div)
    assert mon.data == []
    assert mon.frame_errs == 1
    await send_bytes(dut, [0x99, 0x66], div)
    await settle(dut, div)
    assert mon.data == [0x99, 0x66]


@cocotb.test()
async def rx_against_cocotbext_uart(dut):
    """Second opinion from an independent UART model (1 Mbaud, divisor 40)."""
    div = 40
    mon = await setup(dut, div)
    src = UartSource(dut.rx, baud=40_000_000 // div, bits=8)
    data = bytes(random.randrange(256) for _ in range(64))
    await src.write(data)
    await src.wait()
    await settle(dut, div)
    assert bytes(mon.data) == data
    assert mon.frame_errs == 0
