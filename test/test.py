# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""Chip-level tests: a host loads and runs the Referee engine over the UART.

These tests touch only the chip's pins, so the same file runs on the RTL and
on the gate-level netlist (make GATES=yes). The host's UART is cocotbext-uart,
an independent model; the packets come from tools/referee_host.py, the same
code that talks to real hardware. The clock is the one in info.yaml (60 MHz;
`power_up_baud_matches_clock` checks the testbench, the chip and the hardening
flow agree with it). Every test's first packet, at the 115200 power-up rate, is the
'B' command that switches the link to clock / 20 (3 Mbaud at 60 MHz), so
loading a 105-word image takes ~3 ms of simulated time instead of ~90 ms and
gl_test stays fast. `reset_state`, `power_up_baud_matches_clock`,
`identify_and_fast_baud` and `resync_after_partial_packet` stay at 115200.

The referee test is an INDEPENDENT ORACLE: cocotb drives the judged edges at
known times in ns and computes each true interval from its own stimulus,
never from the reference model or the RTL. Every verdict the chip streams is
checked against that truth (sound: PASS => the true interval meets the bound,
VIOLATION => it breaks it), and when the true margin is larger than u + 1
cycle the verdict must be the decisive one.
"""

import json
import logging
import math
import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import (ClockCycles, Edge, FallingEdge, First, ReadOnly, RisingEdge, SimTimeoutError, Timer,
                             with_timeout)
from cocotb.utils import get_sim_time
from cocotbext.uart import UartSink, UartSource

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import referee_host as host  # noqa: E402
from bus_models import I2CTarget, SPITarget  # noqa: E402

CLOCK_HZ = host.CLOCK_HZ
# One chip cycle as the testbench makes it: the clock's period rounded to tb.v's 1 ps
# resolution (16.667 ns at 60 MHz, as 8.333 + 8.334 ns), first rising edge at RISE0_NS.
# Timing in the tests uses these, not 1e9 / CLOCK_HZ: at 60 MHz the 0.3 ps difference
# adds up to ~80 ns over 4 ms of simulation, and the oracle's late-sample events aim
# within 1 ns of a clock edge.
P_NS = round(1e12 / CLOCK_HZ) / 1000
RISE0_NS = (round(1e12 / CLOCK_HZ) // 2) / 1000
U = 2  # the verdict rule's uncertainty, cycles
BAUD = host.BAUD
FAST_BAUD = CLOCK_HZ // host.FAST_DIV  # 3 Mbaud at 60 MHz
GL = os.environ.get("REFEREE_GL") == "1"  # gate-level: fewer events, skip timing-only tests
FW = ROOT / "firmware" / "engine"


def set_baud(uart, baud):
    # cocotbext-uart's `baud` setter recurses into itself; set the field and restart.
    uart._baud = baud
    uart._restart()


class Host:
    """The host end of the chip's UART command port."""

    def __init__(self, dut):
        self.dut = dut
        self.src = UartSource(dut.host_rx, baud=BAUD, bits=8)
        self.sink = UartSink(dut.host_tx, baud=BAUD, bits=8)
        for u in (self.src, self.sink):
            u.log.setLevel(logging.WARNING)
        self.baud = BAUD

    async def _read(self, n):
        out = bytearray()
        byte_ns = 10 * 1e9 / self.baud
        while len(out) < n:
            if self.sink.empty():
                await self.sink.wait(timeout=int(40 * byte_ns), timeout_unit="ns")
            if self.sink.empty():
                raise AssertionError(f"reply too short: {bytes(out).hex()}")
            out += self.sink.read_nowait(1)
        return out

    async def transact(self, pkt):
        """Send one packet; return (status, data, extra bytes of a drain reply)."""
        await self.src.write(pkt)
        await self.src.wait()
        status, data = await self._read(2)
        extra = b""
        if pkt[0] == 0x44 and status == host.ACK:
            extra = bytes(await self._read(2 * data))
        return status, data, extra

    async def must(self, pkt):
        status, data, _ = await self.transact(pkt)
        assert status == host.ACK, f"{pkt.hex()}: expected ACK, got {status:#04x} {data:#04x}"
        return data

    async def fast(self):
        """Switch both ends to clock / 20 baud (the chip switches once its reply is out)."""
        await self.must(host.pkt_baud(host.FAST_DIV))
        # The sink hands over a byte before its stop bit has ended; the chip
        # switches only when that stop bit is out. Wait a couple of old bit times.
        await Timer(20, unit="us")
        for u in (self.src, self.sink):
            set_baud(u, FAST_BAUD)
        self.baud = FAST_BAUD

    async def load(self, glob, img):
        for p in host.load_packets(glob, img):
            await self.must(p)

    async def run(self):
        status, _, _ = await self.transact(host.pkt_run(True))
        return status == host.ACK

    async def word16(self, pkt_lo, pkt_hi):
        return (await self.must(pkt_hi)) << 8 | await self.must(pkt_lo)

    async def drain(self):
        status, n, extra = await self.transact(host.pkt_drain(8))
        assert status == host.ACK
        return [extra[2 * i] << 8 | extra[2 * i + 1] for i in range(n)]

    async def stats(self, slot):
        await self.must(host.pkt_stats_latch(slot))
        names = ["pass", "viol", "indet", "min", "max"]
        return {n: await self.word16(host.pkt_stats_read(i, 0), host.pkt_stats_read(i, 1)) for i, n in enumerate(names)}


async def start(dut, fast=True):
    dut.ena.value = 1
    dut.ui_in_pins.value = 0
    dut.uio_in_pins.value = 0
    dut.uio_rx1.value = 1
    dut.i2c_scl_low.value = 0
    dut.i2c_sda_low.value = 0
    dut.spi_miso.value = 0
    dut.host_rx.value = 1
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    h = Host(dut)
    if fast:
        await h.fast()
    return h


async def load_and_run(h, source, pins=None, equ=None):
    glob, img, _ = host.build((FW / source).read_text(), pins, equ=equ)
    await h.load(glob, img)
    assert await h.run(), f"RUN of {source} refused: {host.decode_status(await h.must(host.pkt_status()))}"
    return glob, img


# -----------------------------------------------------------------------------


@cocotb.test()
async def reset_state(dut):
    """After reset: host TX idle high, no engine pin driven, every uio pin an input."""
    await start(dut, fast=False)
    assert int(dut.uo_out.value) == 0x10, f"uo_out = {int(dut.uo_out.value):#04x}: only uo_out[4] (UART TX, idle) high"
    assert int(dut.uio_oe.value) == 0x00, "uio pins must be inputs after reset"


@cocotb.test()
async def no_pin_driven_before_first_run(dut):
    """From reset until the first RUN check passes, nothing is driven: every uio pin is
    high-impedance and uo_out is 0 except uo_out[4] (host TX, idle high), even after the
    global config has given the engine push-pull and open-drain pins and while a RUN is
    refused. The first successful RUN then drives exactly what the config grants."""
    h = await start(dut)

    def undriven(when):
        assert int(dut.uio_oe.value) == 0x00, f"{when}: uio_oe = {int(dut.uio_oe.value):#04x}"
        assert int(dut.uio_out.value) == 0x00, f"{when}: uio_out = {int(dut.uio_out.value):#04x}"
        assert int(dut.uo_out.value) == 0x10, f"{when}: uo_out = {int(dut.uo_out.value):#04x}"

    undriven("after reset")
    # pin 0 = uio[0] push-pull, pin 1 = uio[1] open-drain, pin 8 = uo_out[0] push-pull
    src = """
        set  0, 1
        set  1, 1
        set  8, 1
loop:   jmp  loop
"""
    glob, img, _ = host.build(src, {0: (1, 1), 1: (1, 2), 8: (1, 1)})
    await h.load(glob, img)
    undriven("config loaded, before RUN")
    await h.must(host.pkt_crc(1, refasm_crc(img) ^ 1))  # a wrong expected CRC: RUN is refused
    assert not await h.run(), "RUN with a bad CRC must be refused"
    await ClockCycles(dut.clk, 20)
    undriven("after a refused RUN")
    await h.must(host.pkt_crc(1, refasm_crc(img)))
    assert await h.run()
    await ClockCycles(dut.clk, 20)
    assert int(dut.uio_oe.value) == 0x01, "after RUN: uio[0] push-pull driven, uio[1] open-drain released"
    assert int(dut.uio_out.value) == 0x01
    assert int(dut.uo_out.value) == 0x11, "after RUN: uo_out[0] high, uo_out[4] host TX idle"
    await h.must(host.pkt_run(False))
    await ClockCycles(dut.clk, 20)
    assert int(dut.uio_oe.value) == 0x01, "a halted engine keeps its push-pull levels (ISA 3.1)"


def refasm_crc(words):
    return host.refasm.crc16(words)


@cocotb.test()
async def power_up_baud_matches_clock(dut):
    """info.yaml's clock_hz, the testbench clock and the chip's power-up UART divisor agree:
    the chip answers at 115200 only if its divisor is round(clock_hz / 115200) for the clock
    the demo board will actually set (from info.yaml)."""
    h = await start(dut, fast=False)
    await RisingEdge(dut.clk)
    t1 = get_sim_time("ps")
    await RisingEdge(dut.clk)
    period_ps = get_sim_time("ps") - t1
    assert period_ps == round(1e12 / CLOCK_HZ), f"testbench clock {period_ps} ps, info.yaml says {CLOCK_HZ} Hz"
    # ... and the hardening flow times that clock: src/config.json CLOCK_PERIOD (ns) may be a
    # little tighter than the clock's period (never looser), within 1%
    sta_ns = json.loads((ROOT / "src" / "config.json").read_text())["CLOCK_PERIOD"]
    period_ns = 1e9 / CLOCK_HZ
    assert 0.99 * period_ns <= sta_ns <= period_ns, f"src/config.json CLOCK_PERIOD {sta_ns} ns, info.yaml says {CLOCK_HZ} Hz"
    # The reply to identify starts 06 = start bit, then bits 0, 1, 1, ...: the start bit's
    # falling edge to the next rising edge is exactly 2 bit periods.
    await h.src.write(host.pkt_id())
    # A divisor built for another clock never decodes the 115200 packet, so the chip never
    # replies: bound the wait (the packet takes ~350 us; allow 2 ms) and fail with the reason,
    # rather than hang until CI's job timeout.
    try:
        await with_timeout(FallingEdge(dut.host_tx), 2, "ms")
    except SimTimeoutError:
        assert False, (f"no reply at {BAUD} baud within 2 ms: the power-up divisor does not match "
                       f"info.yaml's clock ({CLOCK_HZ} Hz needs {round(CLOCK_HZ / BAUD)} cycles/bit)")
    t_fall = get_sim_time("ps")
    await RisingEdge(dut.host_tx)
    div = (get_sim_time("ps") - t_fall) / period_ps / 2
    assert div == host.POWER_UP_DIV == round(CLOCK_HZ / BAUD), (
        f"power-up divisor {div} cycles/bit; info.yaml clock {CLOCK_HZ} Hz needs {round(CLOCK_HZ / BAUD)}")
    assert (await h._read(2)) == bytes([host.ACK, host.ID])


@cocotb.test()
async def identify_and_fast_baud(dut):
    """Identify at 115200, switch to clock / 20 baud, identify again; nothing dropped."""
    h = await start(dut, fast=False)
    assert await h.must(host.pkt_id()) == host.ID
    await h.fast()
    assert await h.must(host.pkt_id()) == host.ID
    assert await h.must(host.pkt_status(port=True)) == 0x00, "no overrun"


@cocotb.test()
async def load_readback_and_run_check(dut):
    """Image writes, readback, the RUN check (CRC and pin claims), and write protection."""
    h = await start(dut)
    glob, img, _ = host.build((FW / "uart_tx.s").read_text(), {0: (1, 1)})
    await h.load(glob, img)
    for sel, addr, word in [(0, 0, glob[0]), (0, 2, glob[2]), (1, 0, img[0]), (1, 5, img[5]), (1, 96, img[96])]:
        got = await h.word16(host.pkt_read(sel, addr, 0), host.pkt_read(sel, addr, 1))
        assert got == word, f"readback sel {sel} addr {addr}: {got:#06x} != {word:#06x}"

    # A wrong expected CRC: RUN is refused (NAK after the check) and CRC_FAIL is set.
    await h.must(host.pkt_crc(1, host.refasm.crc16(img) ^ 1))
    assert not await h.run()
    st = host.decode_status(await h.must(host.pkt_status()))
    assert st["crc_fail"] and not st["running"], st

    # A claimed pin the global config doesn't give the engine: refused too.
    await h.must(host.pkt_crc(1, host.refasm.crc16(img)))
    await h.must(host.pkt_write_global(2, 0))  # pin 0 unowned
    await h.must(host.pkt_crc(0, host.refasm.crc16([glob[0], glob[1], 0] + glob[3:])))
    assert not await h.run()

    # Correct images: RUN is accepted; the computed CRCs read back after a halt.
    await h.must(host.pkt_write_global(2, glob[2]))
    await h.must(host.pkt_crc(0, host.refasm.crc16(glob)))
    assert await h.run()
    st = host.decode_status(await h.must(host.pkt_status()))
    assert st["running"] and st["ni"], st

    # While running, image writes, reads and expected-CRC writes are refused.
    for p in (host.pkt_write_engine(0, 0), host.pkt_write_global(0, 0), host.pkt_read(1, 0, 0), host.pkt_crc(1, 0)):
        assert (await h.transact(p))[0] == host.NAK, p.hex()
    await h.must(host.pkt_run(False))
    for sel, words in ((0, glob), (1, img)):
        crc = await h.word16(host.pkt_read(sel, 0x80, 0), host.pkt_read(sel, 0x80, 1))
        assert crc == host.refasm.crc16(words), f"computed CRC sel {sel}: {crc:#06x}"


@cocotb.test()
async def no_image_writes_around_run(dut):
    """A13 at chip level: a RUN followed back to back by image and expected-CRC writes.
    A packet takes far longer than the 105-cycle check, so it can't land inside it (the
    cycle-exact case is proved in formal/engine.sby and hammered in the engine lockstep);
    here every such write is refused (dropped while the RUN reply is pending, or NAK'd
    because the engine runs), and after a halt the image and CRCs are unchanged."""
    h = await start(dut)
    glob, img, _ = host.build((FW / "uart_tx.s").read_text(), {0: (1, 1)})
    await h.load(glob, img)
    writes = [host.pkt_write_engine(0, 0xFFFF), host.pkt_crc(1, 0x1234), host.pkt_write_global(2, 0), host.pkt_crc(0, 0x4321)]
    await h.src.write(host.pkt_run(True) + b"".join(writes))  # no gap: RUN, then the writes
    await h.src.wait()
    await Timer(50, unit="us")
    replies = bytes(h.sink.read_nowait(h.sink.count()))
    assert replies[:2] == bytes([host.ACK, 1]), f"RUN must pass on the original image: {replies.hex()}"
    for i in range(2, len(replies), 2):
        assert replies[i] == host.NAK, f"a write while not halted was accepted: {replies.hex()}"
    await h.must(host.pkt_run(False))
    for sel, addr, word in [(1, 0, img[0]), (0, 2, glob[2])]:
        assert await h.word16(host.pkt_read(sel, addr, 0), host.pkt_read(sel, addr, 1)) == word
    for sel, words in ((0, glob), (1, img)):
        crc = await h.word16(host.pkt_read(sel, 0x80, 0), host.pkt_read(sel, 0x80, 1))
        assert crc == host.refasm.crc16(words)
    assert await h.run(), "the expected CRCs are still the original ones"


@cocotb.test()
async def rejects_bad_commands(dut):
    h = await start(dut)
    assert (await h.transact(bytes([0x7A, 1, 2, 3])))[:2] == (host.NAK, 0x7A), "unknown command"
    assert (await h.transact(host.pkt_write_engine(97, 0)))[0] == host.NAK, "engine address 97 is out of range"
    assert (await h.transact(host.pkt_write_global(8, 0)))[0] == host.NAK, "global address 8 is out of range"
    assert (await h.transact(host.pkt_run(True, e=2)))[0] == host.NAK, "there is no engine 2"
    assert (await h.transact(host.pkt_write_engine(97, 0, e=1)))[0] == host.NAK, "Player address 97 is out of range"
    for p in (host.pkt_drain(8, e=1), host.pkt_stats_latch(0, e=1), host.pkt_stats_read(0, 0, e=1),
              host.pkt_stats_clear(0, e=1), host.pkt_lost(e=1), host.pkt_read(3, 0, 0), host.pkt_crc(3, 0)):
        assert (await h.transact(p))[0] == host.NAK, f"{p.hex()}: the Player has no verdicts / stats / LOST; no sel 3"
    assert (await h.transact(host.pkt_pop()))[0] == host.NAK, "OUT is empty"
    assert (await h.transact(host.pkt_baud(5)))[0] == host.NAK, "bit period below 6"
    assert await h.must(host.pkt_id()) == host.ID, "still works afterwards"


@cocotb.test()
async def engine_uart_tx(dut):
    """firmware/engine/uart_tx.s: bytes pushed into IN come out of uio[0] at 115200 8N1."""
    h = await start(dut)
    out = UartSink(dut.eng_pin0, baud=BAUD, bits=8)
    out.log.setLevel(logging.WARNING)
    await load_and_run(h, "uart_tx.s", {0: (1, 1)})  # pin 0 = uio[0], push-pull; CTS (ui_in[0]) low
    assert int(dut.uio_oe.value) & 1, "uio[0] driven by the engine"
    data = b"Ref" if GL else b"Referee\x00\xff"
    for b in data:
        while (await h.transact(host.pkt_push(b)))[0] != host.ACK:  # IN full: retry
            await Timer(20, unit="us")
    await Timer(int((len(data) + 1) * 10 * 1e6 / BAUD) + 50, unit="us")
    assert bytes(out.read_nowait(out.count())) == data


@cocotb.test()
async def engine_uart_rx(dut):
    """firmware/engine/uart_rx.s: a 115200 8N1 stream on uio_in[1] arrives in OUT."""
    h = await start(dut)
    src = UartSource(dut.uio_rx1, baud=BAUD, bits=8)
    src.log.setLevel(logging.WARNING)
    await load_and_run(h, "uart_rx.s")
    data = b"OK!" if GL else b"Hello\x00\xff\x55"
    got = bytearray()
    for b in data:  # one at a time: OUT holds 4 bytes
        await src.write(bytes([b]))
        await src.wait()
        await Timer(30, unit="us")
        status, byte, _ = await h.transact(host.pkt_pop())
        assert status == host.ACK, f"no byte in OUT after sending {b:#04x}"
        got.append(byte)
    assert bytes(got) == data


@cocotb.test()
async def referee_oracle(dut):
    """Independent oracle for the referee (firmware/engine/setup_oracle.s).

    Data = channel 0 (ui_in[4]), clock = channel 1 (ui_in[5]). For every clock
    rise the chip judges (data's latest edge -> clock rise) against a minimum
    bound of 400 ns (slot 0) and a maximum bound of 700 ns (slot 1) and streams
    both verdicts with the exact m. The test places the edges itself at
    arbitrary sub-cycle times and knows the true interval T exactly.
    """
    h = await start(dut)
    await load_and_run(h, "setup_oracle.s")
    rng = random.Random(2026)
    bounds = {0: ("min", 400.0), 1: ("max", 700.0)}
    sda = scl = 0
    last_sda_t = None  # time of data's latest edge (ns)

    def pins():
        dut.ui_in_pins.value = (sda << 4) | (scl << 5)

    async def at(t_ns):
        dt = round((t_ns - get_sim_time("ns")) * 1000)
        assert dt > 0
        await Timer(dt, unit="ps")

    # Interval plan: decisive for both bounds, near each bound, and near-coincident
    # data edges just before or just AFTER the clock rise (order-ambiguous: ORD).
    plan = [150, 600, 900, 250, 650, 1000, 120, 395, 405, 690, 715, 20, -10, -25, 8]
    # Late-resolving samples (see `edge` below): the worst case of the measurement, an
    # error just above one cycle, aimed right at each bound. u = 2 must stay sound here;
    # u = 1 must not (it is caught by these events).
    plan[3:3] = [("meta", "over", 0), ("meta", "over", 1), ("meta", "under", 0), ("meta", "under", 1)]
    n_events = 12 if GL else 40
    while len(plan) < n_events:
        plan.append(rng.choice([rng.uniform(60, 1200), rng.uniform(330, 470), rng.uniform(630, 770), rng.uniform(-40, 40)]))
    plan = plan[:n_events]

    # Sampling model. The chip's input flops sample at every rising clock edge. An input
    # that changes inside a flop's setup window may be captured as the OLD level (the
    # sample resolves late): the chip then sees the change one cycle later. The test
    # models this for chosen edges: the TRUE time (used for the truth) stays t, but the
    # pin changes just after the clock edge that follows t. APERTURE is the window.
    APERTURE = 1.0  # ns, < P_NS / 4

    def next_clock_edge(t):  # first rising edge at or after t (rising edges at RISE0 + kP)
        return RISE0_NS + math.ceil((t - RISE0_NS) / P_NS - 1e-9) * P_NS

    async def edge(t_true, late=False):
        if late:
            assert 0 < next_clock_edge(t_true) - t_true <= APERTURE
            await at(next_clock_edge(t_true) + 0.5)
        else:
            await at(t_true)

    # One data edge first, so the first judgment has a fresh reference.
    t = get_sim_time("ns") + 500
    await at(t)
    sda ^= 1
    pins()
    last_sda_t = t

    results = []
    max_err = 0.0  # largest measurement error seen on an unflagged verdict, in ns
    for k, ev in enumerate(plan):
        sda_late = scl_late = False
        if isinstance(ev, tuple):  # ("meta", over|under, bound slot)
            _, how, b = ev
            n = round(bounds[b][1] / P_NS)  # the bound in cycles (exact here)
            ea = next_clock_edge(get_sim_time("ns") + 1500)
            eb = ea + n * P_NS
            if how == "over":  # data sampled at once, clock sampled a cycle late: m = n + 1, T just below n
                t_sda, t_clk, scl_late = ea - 0.2, eb - 0.9, True
            else:  # data sampled a cycle late, clock at once: m = n - 1, T just above n
                t_sda, t_clk, sda_late = ea - 0.9, eb - 0.2, True
            T_plan = t_clk - t_sda
        else:
            T_plan = ev
            t_clk = get_sim_time("ns") + 1500 + rng.uniform(0, P_NS)  # any phase within a cycle
            t_sda = t_clk - T_plan
        if T_plan >= 0:
            await edge(t_sda, sda_late)
            sda ^= 1
            pins()
            last_sda_t = t_sda
            await edge(t_clk, scl_late)
            scl = 1
            pins()
            after = None
        else:  # data edge |T| ns AFTER the clock rise
            await at(t_clk)
            scl = 1
            pins()
            await at(t_clk - T_plan)
            after = t_clk - T_plan
        T_true = t_clk - last_sda_t  # data's latest edge truly before the clock rise
        if after is not None:
            sda ^= 1
            pins()
            last_sda_t = after
        await at(t_clk + 300)
        scl = 0
        pins()
        await Timer(1, unit="us")
        slots = await h.drain()
        verdicts = host.split_verdicts(slots)
        assert len(verdicts) == 2, f"event {k}: expected 2 verdicts, got {slots}"
        near = T_true <= 3 * P_NS or (after is not None and after - t_clk <= 3 * P_NS)
        results.append((k, T_true, near, verdicts))

    # ---- check every verdict against the truth -------------------------------
    seen = {(b, v): 0 for b in bounds for v in ("PASS", "VIOLATION", "INDETERMINATE")}
    decisive_expected = {(b, v): 0 for b in bounds for v in ("PASS", "VIOLATION")}
    for k, T, near, verdicts in results:
        for hd, m in verdicts:
            b = hd["b"]
            kind, N = bounds[b]
            v = hd["verdict"]
            seen[(b, v)] += 1
            where = f"event {k}: T = {T:.3f} ns, bound {b} ({kind} {N} ns), m = {m}, {hd}"
            # soundness
            if v == "PASS":
                assert (T >= N) if kind == "min" else (T <= N), "UNSOUND PASS: " + where
            if v == "VIOLATION":
                assert (T < N) if kind == "min" else (T > N), "UNSOUND VIOLATION: " + where
            # an unflagged measurement is within the proved error (< u = 2 cycles)
            flagged = hd["FX"] or hd["FY"] or hd["ORD"] or hd["TAG"]
            if not flagged:
                assert abs(m * P_NS - T) < U * P_NS, "measurement error >= 2 cycles: " + where
                max_err = max(max_err, abs(m * P_NS - T))
            # completeness: a margin above u + 1 cycle must be decided
            if not near and k > 0:
                margin = (T - N) if kind == "min" else (N - T)  # > 0: meets the bound
                if margin > (U + 1) * P_NS:
                    assert v == "PASS", "expected PASS: " + where
                    decisive_expected[(b, "PASS")] += 1
                elif margin < -(U + 1) * P_NS:
                    assert v == "VIOLATION", "expected VIOLATION: " + where
                    decisive_expected[(b, "VIOLATION")] += 1
    dut._log.info("oracle: %d events; verdicts %s; largest error %.2f cycles", len(results), seen, max_err / P_NS)
    assert max_err > P_NS, "the late-sample events must produce an error above one cycle (else u = 1 looks sound)"
    for key, n in decisive_expected.items():
        assert n > 0, f"no clear-margin case produced {key}: the plan must cover it"
    for b in bounds:
        assert seen[(b, "PASS")] and seen[(b, "VIOLATION")], f"bound {b}: PASS and VIOLATION must both appear"

    # ---- the stats agree with the streamed verdicts --------------------------
    for b in bounds:
        st = await h.stats(b)
        mine = [(hd, m) for _, _, _, vs in results for hd, m in vs if hd["b"] == b]
        assert st["pass"] == sum(hd["verdict"] == "PASS" for hd, _ in mine), (b, st)
        assert st["viol"] == sum(hd["verdict"] == "VIOLATION" for hd, _ in mine), (b, st)
        assert st["indet"] == sum(hd["verdict"] == "INDETERMINATE" for hd, _ in mine), (b, st)
        ms = [m for hd, m in mine if not (hd["FX"] or hd["FY"] or hd["ORD"] or hd["TAG"])]
        to16 = lambda x: x & 0xFFFF  # noqa: E731
        assert st["min"] == to16(min(ms)) and st["max"] == to16(max(ms)), (b, st, min(ms), max(ms))
    assert await h.must(host.pkt_lost()) == 0, "no trigger lost at this pace"


@cocotb.test(skip=GL)
async def resync_after_partial_packet(dut):
    """Half a packet, then silence: the chip drops it and the next packet works."""
    h = await start(dut, fast=False)
    await h.src.write(bytes([0x49, 0x00]))
    await h.src.wait()
    await Timer(round(1.1 * 2**17 / CLOCK_HZ * 1e6), unit="us")  # > 2**17 cycles
    assert h.sink.empty(), "a partial packet must not get a reply"
    assert await h.must(host.pkt_id()) == host.ID
    assert await h.must(host.pkt_status(port=True)) == 0x00, "no overrun"


# -----------------------------------------------------------------------------
# CP4: bit-serial controllers in firmware, against independent target models
# -----------------------------------------------------------------------------


async def push_in(h, byte, e=0):
    """Push one byte into engine e's IN, waiting while it is full."""
    for _ in range(200):
        if (await h.transact(host.pkt_push(byte, e)))[0] == host.ACK:
            return
        await Timer(5, unit="us")
    raise AssertionError("IN stayed full")


async def pop_out(h, timeout_us=2000, e=0):
    """Pop one byte from engine e's OUT, waiting until the firmware has pushed it."""
    t0 = get_sim_time("us")
    while get_sim_time("us") - t0 < timeout_us:
        status, byte, _ = await h.transact(host.pkt_pop(e))
        if status == host.ACK:
            return byte
        await Timer(5, unit="us")
    raise AssertionError("no byte in OUT")


async def serial_byte(h, byte, cmd=None, e=0):
    """One byte through i2c_controller.s ('X') or an SPI controller: spread in, gather out."""
    if cmd is not None:
        await push_in(h, cmd, e)
    for b in host.spread(byte):
        await push_in(h, b, e)
    return host.gather(await pop_out(h, e=e), await pop_out(h, e=e))


async def i2c_bit(h, cmd, e=0):
    """'A' (SDA released, sampled) or 'K' (SDA low): one clock; returns the SDA sample."""
    await push_in(h, cmd, e)
    await pop_out(h, e=e)
    return (await pop_out(h, e=e)) & 1


async def i2c_write(h, addr, ptr, data, e=0):
    """START, address + W, pointer, data, STOP; returns the target's ACK bits (0 = ACK)."""
    acks = []
    await push_in(h, host.I2C_START, e)
    for b in [addr << 1, ptr] + list(data):
        assert await serial_byte(h, b, host.I2C_XFER, e) == b, "SDA must read back what the controller sent"
        acks.append(await i2c_bit(h, host.I2C_NACK, e))  # release SDA and sample: the target's ACK
        if acks[-1]:
            break
    await push_in(h, host.I2C_STOP, e)
    return acks


async def i2c_read(h, addr, ptr, n, e=0):
    """START, address + W, pointer, repeated START, address + R, n bytes (ACK all but the
    last, NACK the last), STOP."""
    await push_in(h, host.I2C_START, e)
    for b in (addr << 1, ptr):
        await serial_byte(h, b, host.I2C_XFER, e)
        assert await i2c_bit(h, host.I2C_NACK, e) == 0, f"no ACK for {b:#04x}"
    await push_in(h, host.I2C_START, e)
    await serial_byte(h, addr << 1 | 1, host.I2C_XFER, e)
    assert await i2c_bit(h, host.I2C_NACK, e) == 0, "no ACK for the read address"
    got = []
    for k in range(n):
        got.append(await serial_byte(h, 0xFF, host.I2C_XFER, e))  # SDA released: the target drives it
        await i2c_bit(h, host.I2C_ACK if k < n - 1 else host.I2C_NACK, e)
    await push_in(h, host.I2C_STOP, e)
    return got


async def i2c_session(dut, stretch_ns):
    h = await start(dut)
    t = I2CTarget(dut, addr=0x50, stretch_ns=stretch_ns)
    await load_and_run(h, "i2c_controller.s", {2: (1, 2), 3: (1, 2)})  # SCL, SDA open-drain
    data = [0xA5, 0x3C] if GL else [0xA5, 0x00, 0xFF, 0x3C, 0x81]
    assert await i2c_write(h, 0x50, 0x10, data) == [0] * (len(data) + 2), "every byte ACKed"
    assert list(t.mem[0x10:0x10 + len(data)]) == data, "the EEPROM holds what was written"
    assert await i2c_read(h, 0x50, 0x10, len(data)) == data, "read back through a repeated START"
    assert await i2c_write(h, 0x51, 0x00, [0x55]) == [1], "a wrong address is NACKed"
    t.stop_model()
    assert not t.violations, "I2C Fast-mode timing: " + "; ".join(t.violations[:5])
    return t


@cocotb.test()
async def i2c_controller(dut):
    """firmware/engine/i2c_controller.s at 400 kHz against an EEPROM target model: write,
    read back through a repeated START, NACK for a wrong address. The model checks every
    Fast-mode timing limit on the bus wires; SDA reads back what the controller sent."""
    t = await i2c_session(dut, stretch_ns=0)
    starts = sum(1 for e in t.events if e[0] == "start")
    dut._log.info("i2c: %d STARTs, %d events, no timing violations", starts, len(t.events))


@cocotb.test()
async def i2c_controller_clock_stretching(dut):
    """The same with a target that holds SCL low for 3 us after every byte: the firmware
    waits for SCL to be seen high, so every high phase still lasts tHIGH."""
    t = await i2c_session(dut, stretch_ns=3000)
    assert t.stretches > 5, "the target must actually stretch"


@cocotb.test()
async def spi_controller_modes(dut):
    """firmware/engine/spi_cpha0.s / spi_cpha1.s in all four modes (CPOL by .equ) against an
    SPI target model: multi-byte full-duplex frames with CS held low; the model checks the
    clock idle level, half-periods (minimum, and equal within a byte), CS setup / hold and
    MOSI setup / hold."""
    h = await start(dut)
    rng = random.Random(4)
    half_ns = 500
    modes = [0, 3] if GL else [0, 1, 2, 3]
    for mode in modes:
        cpol, cpha = mode >> 1, mode & 1
        frames = [[0x9F, 0x00, 0x00]] if GL else [[0x9F, 0x00, 0x00, 0x00], [rng.randrange(256) for _ in range(5)]]
        replies = [[rng.randrange(256) for _ in f] for f in frames]
        t = SPITarget(dut, mode, replies, half_ns, P_NS)
        await load_and_run(h, f"spi_cpha{cpha}.s", {4: (1, 1), 5: (1, 1), 6: (1, 1)}, equ={"CPOL": cpol})
        await Timer(5, unit="us")
        assert int(dut.spi_sck.value) == cpol and int(dut.spi_cs.value) == 1, f"mode {mode}: idle bus"
        for f, r in zip(frames, replies):
            got = [await serial_byte(h, b) for b in f]
            await push_in(h, host.SPI_END)
            await Timer(5, unit="us")
            assert got == r, f"mode {mode}: received {[hex(x) for x in got]}, target sent {[hex(x) for x in r]}"
        t.stop_model()
        assert t.frames == frames, f"mode {mode}: target got {t.frames}, sent {frames}"
        assert not t.violations, f"mode {mode}: " + "; ".join(t.violations[:5])
        dut._log.info("spi mode %d: %d frames, %d bytes each way", mode, len(frames), sum(map(len, frames)))


# -----------------------------------------------------------------------------
# Two engines: the Referee (engine 0) and the Player (engine 1)
# -----------------------------------------------------------------------------


def assemble(src, e):
    return host.assemble(src, engine=e)[0]


async def run_engine(h, e):
    status, _, _ = await h.transact(host.pkt_run(True, e))
    return status == host.ACK


async def engine_status(h, e):
    return host.decode_status(await h.must(host.pkt_status(e=e)))


@cocotb.test()
async def pin_ownership_two_engines(dut):
    """Two engines can never drive one pin (A17). The global config gives uio[0] to the
    Referee and uio[1] to the Player, both push-pull. RUN(e) is refused while e's image
    claims the other engine's pin, and the refused engine drives nothing. The global
    config can't be written while either engine runs. With correct images both run, each
    driving only its own pin; a Player image whose claimed-pin mask is forged (CRC made to
    match) passes RUN but its SET to the Referee's pin has no effect and sets PIN_FAULT."""
    h = await start(dut)
    glob = host.refasm.global_image({0: (1, 1), 1: (2, 1)})
    ref_ok = assemble("        set  0, 1\nloop:   jmp  loop\n", 0)
    ply_ok = assemble("        set  1, 1\nloop:   jmp  loop\n", 1)
    ply_bad = assemble("        set  0, 0\nloop:   jmp  loop\n", 1)  # claims the Referee's pin
    ref_bad = assemble("        set  1, 0\nloop:   jmp  loop\n", 0)  # claims the Player's pin

    for p in host.load_system_packets(glob, {0: ref_ok, 1: ply_bad}):
        await h.must(p)
    assert not await run_engine(h, 1), "the Player claims uio[0], which the Referee owns: RUN refused"
    st = await engine_status(h, 1)
    assert st["crc_fail"] and not st["running"], st
    await ClockCycles(dut.clk, 20)
    assert int(dut.uio_oe.value) == 0, "a refused RUN drives nothing"
    assert await run_engine(h, 0)
    await ClockCycles(dut.clk, 20)
    assert int(dut.uio_oe.value) == 0x01 and int(dut.uio_out.value) & 1, "only the Referee's uio[0]"

    # while the Referee runs: no global config write, no Referee image write; the
    # Player (halted) can be reloaded on its own
    assert (await h.transact(host.pkt_write_global(2, 0)))[0] == host.NAK, "config written while an engine runs"
    assert (await h.transact(host.pkt_crc(0, 0)))[0] == host.NAK
    assert (await h.transact(host.pkt_write_engine(0, 0, e=0)))[0] == host.NAK
    for p in host.load_engine_packets(ply_ok, 1)[1:]:  # (skip the halt: the Player is halted)
        await h.must(p)
    assert await run_engine(h, 1), "the Player's own pin: accepted while the Referee keeps running"
    await ClockCycles(dut.clk, 20)
    assert int(dut.uio_oe.value) == 0x03 and int(dut.uio_out.value) & 3 == 3, "each engine drives its own pin"
    assert (await engine_status(h, 0))["running"], "the Player's RUN left the Referee running"

    # the Referee claims the Player's pin: refused, and a refused RUN changes no pin
    for p in host.load_engine_packets(ref_bad, 0):
        await h.must(p)
    assert not await run_engine(h, 0), "the Referee claims uio[1], which the Player owns: RUN refused"
    assert (await engine_status(h, 1))["running"]
    assert int(dut.uio_oe.value) == 0x03 and int(dut.uio_out.value) & 3 == 3, "a halted engine keeps its level"

    # second line of defence: a forged claimed-pin mask passes the check (claims nothing),
    # but SET to an unowned pin does nothing at run time and sets PIN_FAULT
    forged = list(ply_bad)
    forged[96] = 0
    for p in host.load_engine_packets(forged, 1):
        await h.must(p)
    assert await run_engine(h, 1), "a mask that claims nothing passes the check"
    await ClockCycles(dut.clk, 20)
    assert int(dut.uio_out.value) & 1 == 1, "the Player's SET 0, 0 must not move the Referee's uio[0]"
    st = await engine_status(h, 1)
    assert st["pin_fault"] and st["running"], st


@cocotb.test()
async def player_referee_instructions(dut):
    """The Player has no referee hardware; its referee instructions do what
    docs/isa-timing.md 0b diff 17 says: TRIG, REL and JUDGE do nothing, WAITS can only
    time out (T = 1), S is never set. A JUDGE on the Player produces no verdict, stats
    or VERDICT_OVF anywhere (not even in the Referee's FIFO), and the Player answers no
    drain, stats or LOST command. The program also runs past word 63 by falling through
    (words 64..95 exist only on the Player)."""
    h = await start(dut)
    src = """
        ldsr 0x11
        trig 1, rise         ; no trigger on the Player
        rel
        tsget X, 0, rise     ; X <- 0
        judge 0, raw         ; nothing
        tmon 10us
        waits                ; S never comes: ends by the timeout
        jnt  bad
        js   bad
        push                 ; 0x11: WAITS timed out, S = 0
        jmp  far
bad:    ldsr 0xEE
        push
        halt
far:    nop
""" + "        nop\n" * 47 + """        ldsr 0x22           ; word 63
        push                 ; word 64: reached by falling through
        judge 1
        ldsr 0x33
        push
        halt
"""
    img = assemble(src, 1)
    # the claimed-pin mask is empty; the Referee stays halted throughout
    for p in host.load_packets(host.refasm.global_image(), img, e=1):
        await h.must(p)
    await ClockCycles(dut.clk, 5)
    t_run = get_sim_time("ns")
    assert await run_engine(h, 1)
    first = await pop_out(h, e=1)
    t_first = get_sim_time("ns")
    assert first == 0x11, f"WAITS on the Player must time out with T = 1 and S = 0 (got {first:#04x})"
    assert t_first - t_run >= 10_000, "the WAITS waited for its whole 10 us timeout"
    assert await pop_out(h, e=1) == 0x22 and await pop_out(h, e=1) == 0x33, "words 63, 64.. ran in order"
    await ClockCycles(dut.clk, 20)
    st = await engine_status(h, 1)
    assert not st["running"] and not st["S"] and not st["verdict_ovf"] and not st["pin_fault"], st
    assert await h.drain() == [], "the Player's JUDGEs put nothing in the Referee's VERDICT FIFO"
    stats = await h.stats(0)
    assert stats == {"pass": 0, "viol": 0, "indet": 0, "min": 0, "max": 0}, stats
    for p in (host.pkt_drain(8, e=1), host.pkt_stats_latch(0, e=1), host.pkt_lost(e=1)):
        assert (await h.transact(p))[0] == host.NAK


async def watch_i2c(dut, rises):
    """Record, for every SCL rise on the bus wires, the time since SDA's latest edge (ns),
    i.e. the true interval the Referee's tsget X, 0, last / tsget Y, 1, rise measures."""
    scl, sda = int(dut.scl_bus.value), int(dut.sda_bus.value)
    t_sda = None
    while True:
        await First(Edge(dut.scl_bus), Edge(dut.sda_bus))
        await ReadOnly()
        now = get_sim_time("ns")
        nscl, nsda = int(dut.scl_bus.value), int(dut.sda_bus.value)
        if nsda != sda:
            t_sda = now
        if nscl and not scl:
            rises.append(None if t_sda is None else now - t_sda)
        scl, sda = nscl, nsda


@cocotb.test()
async def referee_judges_player_i2c(dut):
    """The Player speaks a bus and the Referee judges it, on one chip.

    The Player (engine 1) runs firmware/engine/i2c_controller.s on uio[2] (SCL) and
    uio[3] (SDA), open-drain, against the EEPROM model. The Referee (engine 0) watches
    the same wires through the judge-channel mux (channel 0 = uio_in[3] = SDA, channel 1 =
    uio_in[2] = SCL; not non-intrusive) and judges every SCL rise: the time since SDA's
    latest edge against Fast-mode tSU;DAT (min 100 ns, slot 0) and against a 1.26 us
    minimum (slot 1) that the bus meets on some rises and breaks on others. An
    independent monitor in the test records the true interval of every SCL rise from the
    bus wires; the Referee's stats must count exactly those rises, find no tSU;DAT
    violation, and agree with the truth on slot 1 (sound counts, decisive outside u + 1
    cycle of the bound, min / max m within u of the true extremes)."""
    h = await start(dut)
    referee = """
.bound 0, min, 100ns         ; Fast-mode tSU;DAT
.bound 1, min, 1260ns        ; a bound the bus meets on some rises and breaks on others
        trig 1, rise         ; channel 1 = SCL
loop:   tmon 1023, x16
        waits
        jt   loop
        tsget X, 0, last     ; channel 0 = SDA: its latest edge before this SCL rise
        tsget Y, 1, rise
        judge 0, nonpass
        judge 1, none
        rel
        jmp  loop
"""
    img0 = assemble(referee, 0)
    img1 = assemble((FW / "i2c_controller.s").read_text(), 1)
    glob = host.refasm.global_image({2: (2, 2), 3: (2, 2)}, host.mux_config("0:uio3,1:uio2"))
    for p in host.load_system_packets(glob, {0: img0, 1: img1}):
        await h.must(p)
    t = I2CTarget(dut, addr=0x50)
    assert await run_engine(h, 0), "the Referee claims no pin"
    assert await run_engine(h, 1), "the Player claims uio[2], uio[3], which it owns"
    rises = []
    mon = cocotb.start_soon(watch_i2c(dut, rises))
    data = [0xA5, 0x3C] if GL else [0xA5, 0x00, 0xFF, 0x3C]
    assert await i2c_write(h, 0x50, 0x10, data, e=1) == [0] * (len(data) + 2), "every byte ACKed"
    assert await i2c_read(h, 0x50, 0x10, len(data), e=1) == data, "read back through a repeated START"
    await Timer(20, unit="us")
    mon.cancel()
    t.stop_model()
    assert not t.violations, "I2C Fast-mode timing: " + "; ".join(t.violations[:5])
    st = await engine_status(h, 0)
    assert st["running"] and not st["ni"], st

    # the truth, from the bus wires
    true = [x for x in rises if x is not None]
    assert len(true) == len(rises), "SDA moved (the first START) before the first SCL rise"
    n = len(true)
    s0, s1 = await h.stats(0), await h.stats(1)
    dut._log.info("referee on the player's bus: %d SCL rises, true setup %.0f..%.0f ns; slot 0 %s; slot 1 %s",
                  n, min(true), max(true), s0, s1)
    assert s0["pass"] + s0["viol"] + s0["indet"] == n, "one judgment per SCL rise"
    assert s1["pass"] + s1["viol"] + s1["indet"] == n
    assert s0["viol"] == 0 and s0["indet"] == 0, "the Player's controller meets tSU;DAT on every rise"
    assert await h.drain() == [], "judge 0, nonpass: no non-PASS verdict"
    N = 1260.0  # between the ACK bits (setup ~1.20 us) and the controller's own (~1.32 us)
    must_pass = sum(x >= N + (U + 1) * P_NS for x in true)
    must_viol = sum(x <= N - (U + 1) * P_NS for x in true)
    assert must_pass > 0 and must_viol > 0, f"the 1.26 us bound must split the rises: {sorted(true)}"
    assert must_pass <= s1["pass"] <= sum(x >= N for x in true), (s1, must_pass)
    assert must_viol <= s1["viol"] <= sum(x < N for x in true), (s1, must_viol)
    to_s = lambda v: v - 0x10000 if v & 0x8000 else v  # noqa: E731
    assert abs(to_s(s1["min"]) * P_NS - min(true)) < U * P_NS and abs(to_s(s1["max"]) * P_NS - max(true)) < U * P_NS, s1
    assert await h.must(host.pkt_lost()) == 0, "no trigger lost at 400 kHz"
