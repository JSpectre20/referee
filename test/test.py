# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""Chip-level tests: a host loads and runs the Referee engine over the UART.

These tests touch only the chip's pins, so the same file runs on the RTL and
on the gate-level netlist (make GATES=yes). The host's UART is cocotbext-uart,
an independent model; the packets come from tools/referee_host.py, the same
code that talks to real hardware. The clock is the one in info.yaml (40 MHz
until CP6; `power_up_baud_matches_clock` checks the testbench and the chip
agree with it). Every test's first packet, at the 115200 power-up rate, is the
'B' command that switches the link to clock / 20 (2 Mbaud at 40 MHz), so
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
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, SimTimeoutError, Timer, with_timeout
from cocotb.utils import get_sim_time
from cocotbext.uart import UartSink, UartSource

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import referee_host as host  # noqa: E402
from bus_models import I2CTarget, SPITarget  # noqa: E402

CLOCK_HZ = host.CLOCK_HZ
P_NS = 1e9 / CLOCK_HZ  # one chip cycle (25 ns at 40 MHz)
U = 2  # the verdict rule's uncertainty, cycles
BAUD = host.BAUD
FAST_BAUD = CLOCK_HZ // host.FAST_DIV  # 2 Mbaud at 40 MHz
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
    # ... and the hardening flow times that same clock (src/config.json CLOCK_PERIOD, in ns)
    sta_ns = json.loads((ROOT / "src" / "config.json").read_text())["CLOCK_PERIOD"]
    assert abs(sta_ns - 1e9 / CLOCK_HZ) < 0.01, f"src/config.json CLOCK_PERIOD {sta_ns} ns, info.yaml says {CLOCK_HZ} Hz"
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
    assert (await h.transact(host.pkt_run(True, e=1)))[0] == host.NAK, "there is no engine 1"
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

    def next_clock_edge(t):  # first rising edge at or after t (rising edges at P/2 + kP)
        return P_NS / 2 + math.ceil((t - P_NS / 2) / P_NS - 1e-9) * P_NS

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


async def push_in(h, byte):
    """Push one byte into IN, waiting while it is full."""
    for _ in range(200):
        if (await h.transact(host.pkt_push(byte)))[0] == host.ACK:
            return
        await Timer(5, unit="us")
    raise AssertionError("IN stayed full")


async def pop_out(h, timeout_us=2000):
    """Pop one byte from OUT, waiting until the firmware has pushed it."""
    t0 = get_sim_time("us")
    while get_sim_time("us") - t0 < timeout_us:
        status, byte, _ = await h.transact(host.pkt_pop())
        if status == host.ACK:
            return byte
        await Timer(5, unit="us")
    raise AssertionError("no byte in OUT")


async def serial_byte(h, byte, cmd=None):
    """One byte through i2c_controller.s ('X') or an SPI controller: spread in, gather out."""
    if cmd is not None:
        await push_in(h, cmd)
    for b in host.spread(byte):
        await push_in(h, b)
    return host.gather(await pop_out(h), await pop_out(h))


async def i2c_bit(h, cmd):
    """'A' (SDA released, sampled) or 'K' (SDA low): one clock; returns the SDA sample."""
    await push_in(h, cmd)
    await pop_out(h)
    return (await pop_out(h)) & 1


async def i2c_write(h, addr, ptr, data):
    """START, address + W, pointer, data, STOP; returns the target's ACK bits (0 = ACK)."""
    acks = []
    await push_in(h, host.I2C_START)
    for b in [addr << 1, ptr] + list(data):
        assert await serial_byte(h, b, host.I2C_XFER) == b, "SDA must read back what the controller sent"
        acks.append(await i2c_bit(h, host.I2C_NACK))  # release SDA and sample: the target's ACK
        if acks[-1]:
            break
    await push_in(h, host.I2C_STOP)
    return acks


async def i2c_read(h, addr, ptr, n):
    """START, address + W, pointer, repeated START, address + R, n bytes (ACK all but the
    last, NACK the last), STOP."""
    await push_in(h, host.I2C_START)
    for b in (addr << 1, ptr):
        await serial_byte(h, b, host.I2C_XFER)
        assert await i2c_bit(h, host.I2C_NACK) == 0, f"no ACK for {b:#04x}"
    await push_in(h, host.I2C_START)
    await serial_byte(h, addr << 1 | 1, host.I2C_XFER)
    assert await i2c_bit(h, host.I2C_NACK) == 0, "no ACK for the read address"
    got = []
    for k in range(n):
        got.append(await serial_byte(h, 0xFF, host.I2C_XFER))  # SDA released: the target drives it
        await i2c_bit(h, host.I2C_ACK if k < n - 1 else host.I2C_NACK)
    await push_in(h, host.I2C_STOP)
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
    clock idle level, half-periods, CS setup / hold and MOSI setup / hold."""
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
