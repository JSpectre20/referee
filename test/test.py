# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""Chip-level tests for the UART console.

These tests touch only the chip's pins, so the same file runs on the RTL and
on the gate-level netlist (make GATES=yes). The host side of the UART is
cocotbext-uart, an independent model, and the transcripts in transcripts/
are shared with host/referee.py, which runs them against real hardware.
"""

import logging
import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import Timer
from cocotbext.uart import UartSink, UartSource

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))
from referee_console import PROMPT, ConsoleError, expected_echo, parse_transcript, split_reply  # noqa: E402

CLK_HZ = 40_000_000  # set by the clock in tb.v
DEFAULT_DIV = 347  # 115200 baud at 40 MHz
FAST_DIV = 16  # 2.5 Mbaud, to keep long tests quick
TRANSCRIPTS = sorted((Path(__file__).resolve().parent / "transcripts").glob("*.txt"))

# Gate-level simulation is much slower. Tests that only probe baud-rate
# tolerance (pure timing, same logic as RTL) are skipped there.
GL = os.environ.get("REFEREE_GL") == "1"

# Register addresses (see src/regs.v)
ID, VERSION, SCRATCH, STATUS, DIV_LO, DIV_HI = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
GPIO_OUT, GPIO_OE, GPIO_IN, UI_IN, UO_OUT = 0x10, 0x11, 0x12, 0x13, 0x14


class SimConsole:
    """The host end of the UART, built on cocotbext-uart."""

    def __init__(self, dut, baud):
        self.dut = dut
        self.src = None
        self.sink = None
        for sig in (dut.uart_rx, dut.uart_tx):
            logging.getLogger(f"cocotb.{sig._path}").setLevel(logging.WARNING)
        self.set_baud(baud)

    def set_baud(self, baud):
        # cocotbext-uart's baud setter recurses forever, so make new instances.
        for old in (self.src, self.sink):
            if old is not None:
                old._run_cr.cancel()
        self.baud = baud
        self.src = UartSource(self.dut.uart_rx, baud=baud, bits=8)
        self.sink = UartSink(self.dut.uart_tx, baud=baud, bits=8)

    def timeout_ns(self, nbytes):
        return int(2 * (nbytes + 10) * 10e9 / self.baud)

    async def _next_byte(self, timeout_ns, got):
        if self.sink.empty():
            await self.sink.wait(timeout=timeout_ns, timeout_unit="ns")
        if self.sink.empty():
            raise ConsoleError(f"timeout; received so far: {bytes(got)!r}")
        return self.sink.read_nowait(1)

    async def read_until(self, suffix, timeout_ns):
        buf = bytearray()
        while not buf.endswith(suffix):
            buf += await self._next_byte(timeout_ns, buf)
        return bytes(buf)

    async def read_n(self, n, timeout_ns):
        buf = bytearray()
        while len(buf) < n:
            buf += await self._next_byte(timeout_ns, buf)
        return bytes(buf)

    async def exchange(self, data, echo):
        """Send raw bytes and return the reply body (checking echo and framing)."""
        await self.src.write(data)
        t = self.timeout_ns(len(data) + 8)
        raw = await self.read_n(len(echo), t) + await self.read_until(PROMPT, t)
        return split_reply(echo, raw)

    async def cmd(self, line):
        return await self.exchange(line.encode() + b"\r", expected_echo(line))

    async def read(self, addr):
        body = await self.cmd(f"r {addr:02x}")
        return int(body, 16)

    async def write(self, addr, data):
        assert await self.cmd(f"w {addr:02x} {data:02x}") == "ok"

    async def set_divisor(self, div):
        """Change the chip's baud rate, then follow it on the host side."""
        await self.write(DIV_LO, div & 0xFF)
        await self.write(DIV_HI, div >> 8)
        self.set_baud(CLK_HZ / max(div, 4))


async def start(dut, host_error=0.0):
    """Reset the chip and wait for its first prompt."""
    dut.ena.value = 1
    dut.ui_in_pins.value = 0
    dut.uio_in.value = 0
    dut.uart_rx.value = 1
    dut.rst_n.value = 0
    con = SimConsole(dut, CLK_HZ / DEFAULT_DIV * (1 + host_error))
    await Timer(500, unit="ns")
    dut.rst_n.value = 1
    banner = await con.read_until(PROMPT, con.timeout_ns(4))
    assert banner == PROMPT, f"expected the prompt after reset, got {banner!r}"
    return con


async def run_transcript(con, path):
    for step in parse_transcript(path):
        got = await con.cmd(step.line)
        assert got == step.expect, f"{step.where}: {step.line!r}: expected {step.expect!r}, got {got!r}"


async def send_bad_frame(dut, baud):
    """One frame with a low stop bit, bit-banged by hand."""
    bit_ps = round(1e12 / baud)
    for level in [0] + [0] * 8 + [0]:
        dut.uart_rx.value = level
        await Timer(bit_ps, unit="ps")
    dut.uart_rx.value = 1
    await Timer(3 * bit_ps, unit="ps")


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


@cocotb.test()
async def prompt_after_reset(dut):
    """The chip prints a prompt as soon as it comes out of reset."""
    await start(dut)


@cocotb.test()
async def transcripts(dut):
    """Every transcript in transcripts/, at the real 115200 baud."""
    con = await start(dut)
    assert TRANSCRIPTS, "no transcripts found"
    for path in TRANSCRIPTS:
        dut._log.info("transcript %s", path.name)
        await run_transcript(con, path)


@cocotb.test()
async def whitespace_and_trailing_spaces(dut):
    con = await start(dut)
    await con.set_divisor(FAST_DIV)
    assert await con.cmd("w 02 66") == "ok"
    assert await con.cmd("  r 02  ") == "66"
    assert await con.cmd("r 02 ") == "66"
    assert await con.cmd("   ") == ""


@cocotb.test()
async def gpio_pins(dut):
    """Register writes reach the pins, and pin levels can be read back."""
    con = await start(dut)
    await con.set_divisor(FAST_DIV)

    assert int(dut.uio_oe.value) == 0x00, "uio pins must be inputs after reset"
    await con.write(GPIO_OE, 0xFF)
    await con.write(GPIO_OUT, 0xA5)
    assert int(dut.uio_oe.value) == 0xFF
    assert int(dut.uio_out.value) == 0xA5
    await con.write(GPIO_OE, 0x0F)
    assert int(dut.uio_oe.value) == 0x0F

    for v in (0x3C, 0x00, 0xFF):
        dut.uio_in.value = v
        assert await con.read(GPIO_IN) == v

    dut.ui_in_pins.value = 0xF7
    assert await con.read(UI_IN) == 0xFF  # bit 3 is the idle (high) RX line
    dut.ui_in_pins.value = 0x00
    assert await con.read(UI_IN) == 0x08

    await con.write(UO_OUT, 0xFF)
    assert int(dut.uo_out.value) == 0xFF
    await con.write(UO_OUT, 0x00)
    assert int(dut.uo_out.value) == 0x10, "uo_out[4] is TX and must stay high when idle"


@cocotb.test()
async def cancel_line(dut):
    """Backspace, DEL, Ctrl-C and ESC throw away the line typed so far."""
    con = await start(dut)
    await con.set_divisor(FAST_DIV)
    await con.write(SCRATCH, 0x11)
    for key in (b"\x08", b"\x7f", b"\x03", b"\x1b"):
        assert await con.exchange(b"w 02 ff" + key, b"w 02 ff") == ""
        assert await con.read(SCRATCH) == 0x11


@cocotb.test()
async def overrun_is_flagged(dut):
    """A byte sent while a reply is going out is dropped and flagged; LF is not."""
    con = await start(dut)
    await con.set_divisor(FAST_DIV)
    await con.write(STATUS, 0x03)

    assert await con.exchange(b"r 00\r\n", b"r 00") == "52"
    assert await con.read(STATUS) == 0x00, "a CRLF line ending must not count as overrun"

    assert await con.exchange(b"r 00\rx", b"r 00") == "52"
    assert await con.read(STATUS) == 0x01
    await con.write(STATUS, 0x01)
    assert await con.read(STATUS) == 0x00


@cocotb.test()
async def framing_error_is_flagged(dut):
    con = await start(dut)
    await con.set_divisor(FAST_DIV)
    await con.write(STATUS, 0x03)
    await send_bad_frame(dut, con.baud)
    assert await con.read(STATUS) == 0x02
    await con.write(STATUS, 0x02)
    assert await con.read(STATUS) == 0x00


@cocotb.test()
async def baud_rate_change(dut):
    """The divisor applies after the reply, is clamped to 4, and can go back."""
    con = await start(dut)

    await con.set_divisor(FAST_DIV)  # 2.5 Mbaud
    assert await con.read(ID) == 0x52
    assert await con.read(DIV_LO) == FAST_DIV
    assert await con.read(DIV_HI) == 0x00

    await con.write(DIV_LO, 0x01)  # ask for 1, get the minimum of 4 (10 Mbaud)
    await con.write(DIV_HI, 0x00)
    con.set_baud(CLK_HZ / 4)
    assert await con.read(ID) == 0x52
    assert await con.read(DIV_LO) == 0x01, "reads return the value written"

    await con.set_divisor(DEFAULT_DIV)
    assert await con.read(ID) == 0x52


@cocotb.test(skip=GL)
async def host_baud_error(dut):
    """A host whose baud rate is 3% fast or slow still works both ways."""
    for err in (+0.03, -0.03):
        con = await start(dut, host_error=err)
        dut._log.info("host baud error %+.0f%%", 100 * err)
        await run_transcript(con, TRANSCRIPTS[0])
        assert await con.read(STATUS) == 0x00


@cocotb.test(skip=GL)
async def fast_typist(dut):
    """A host 3% fast sending a long line back to back loses no echo."""
    con = await start(dut, host_error=0.03)
    line = "w" + " " * 10 + "02" + " " * 10 + "5a"
    assert await con.cmd(line) == "ok"
    assert await con.read(SCRATCH) == 0x5A
    assert await con.read(STATUS) == 0x00


def random_format(cmd, *nums):
    """Same command, randomly spelled: case, digit count and spacing vary."""
    parts = [cmd.upper() if random.random() < 0.3 else cmd]
    for n in nums:
        s = f"{n:x}" if (n < 16 and random.random() < 0.5) else f"{n:02x}"
        parts.append(s.upper() if random.random() < 0.3 else s)
    seps = [" " * random.randint(1, 3) for _ in nums]
    line = parts[0] + "".join(sep + p for sep, p in zip(seps, parts[1:]))
    return " " * random.randint(0, 2) + line


@cocotb.test()
async def random_register_traffic(dut):
    """Random reads and writes checked against a Python model of the registers."""
    con = await start(dut)
    await con.set_divisor(FAST_DIV)

    model = {SCRATCH: 0, GPIO_OUT: 0, GPIO_OE: 0, UO_OUT: 0}
    uio_in = ui_pins = 0
    fixed = {ID: 0x52, VERSION: 0x01, STATUS: 0x00, DIV_LO: FAST_DIV, DIV_HI: 0x00}

    for _ in range(150):
        if random.random() < 0.3:
            uio_in, ui_pins = random.randrange(256), random.randrange(256)
            dut.uio_in.value = uio_in
            dut.ui_in_pins.value = ui_pins

        addr = random.choice(list(model) * 3 + [ID, VERSION, STATUS, GPIO_IN, UI_IN, DIV_LO, DIV_HI, random.randrange(0x15, 0x100)])
        if random.random() < 0.5 and addr not in (DIV_LO, DIV_HI):
            data = random.randrange(256)
            if addr == STATUS:
                data &= 0x03
            assert await con.cmd(random_format("w", addr, data)) == "ok"
            if addr in model:
                model[addr] = data
        else:
            if addr in model:
                want = model[addr]
            elif addr == GPIO_IN:
                want = uio_in
            elif addr == UI_IN:
                want = ui_pins | 0x08
            else:
                want = fixed.get(addr, 0x00)
            got = await con.cmd(random_format("r", addr))
            assert got == f"{want:02x}", f"read {addr:02x}: expected {want:02x}, got {got}"

        assert int(dut.uio_out.value) == model[GPIO_OUT]
        assert int(dut.uio_oe.value) == model[GPIO_OE]
        assert int(dut.uo_out.value) == (model[UO_OUT] & 0xEF) | 0x10
