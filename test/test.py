# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""Chip-level tests: a host loads and runs seq over the UART.

These tests touch only the chip's pins, so the same file runs on the RTL and
on the gate-level netlist (make GATES=yes). The host's UART is cocotbext-uart,
an independent model, at a real 115200 baud; the packets come from
tools/referee_host.py, the same code that talks to real hardware.
"""

import os
import re
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, Timer
from cocotbext.uart import UartSink, UartSource

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import referee_host as host  # noqa: E402
import seqasm  # noqa: E402

BAUD = 115200
BYTE_NS = round(10 * 1e9 / BAUD)  # one 8N1 byte, in whole ns
GL = os.environ.get("REFEREE_GL") == "1"  # gate-level: skip slow timing-only tests
UART_FW = (ROOT / "firmware" / "seq" / "uart_tx.s").read_text()


class Host:
    """The host end of the chip's UART."""

    def __init__(self, dut):
        self.dut = dut
        self.src = UartSource(dut.host_rx, baud=BAUD, bits=8)
        self.sink = UartSink(dut.host_tx, baud=BAUD, bits=8)

    async def transact(self, pkt):
        """Send one packet, return (status, data) of the 2-byte reply."""
        await self.src.write(pkt)
        await self.src.wait()
        reply = bytearray()
        deadline = 4 * BYTE_NS
        while len(reply) < 2:
            if self.sink.empty():
                await self.sink.wait(timeout=deadline, timeout_unit="ns")
            if self.sink.empty():
                raise AssertionError(f"no reply to {pkt.hex()}; got {bytes(reply).hex()}")
            reply += self.sink.read_nowait(1)
        return reply[0], reply[1]

    async def must(self, pkt):
        status, data = await self.transact(pkt)
        assert status == host.ACK, f"{pkt.hex()}: expected ACK, got {status:#04x} {data:#04x}"
        return data


async def start(dut):
    dut.ena.value = 1
    dut.ui_in_pins.value = 0
    dut.uio_in.value = 0
    dut.host_rx.value = 1
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    return Host(dut)


def uart_program():
    return seqasm.assemble(UART_FW)  # BIT = 347: 115200 baud on uio[0]


# -----------------------------------------------------------------------------


@cocotb.test()
async def reset_state(dut):
    """After reset: host TX line idle high, seq stopped, every uio pin an input."""
    await start(dut)
    assert int(dut.uo_out.value) & 0x10, "uo_out[4] (UART TX) must idle high"
    assert int(dut.uo_out.value) & 0x01 == 0, "seq must not run after reset"
    assert int(dut.uio_oe.value) == 0x00, "uio pins must be inputs after reset"


@cocotb.test()
async def identify(dut):
    h = await start(dut)
    assert await h.must(host.pkt_id()) == host.ID


@cocotb.test()
async def load_and_run_uart_firmware(dut):
    """Load the UART firmware over the UART, run it, decode its output on uio[0]."""
    h = await start(dut)
    out = UartSink(dut.seq_pin0, baud=BAUD, bits=8)
    for p in host.load_packets(uart_program(), sr=0xA5, oe=0x01, run=True):
        await h.must(p)
    assert int(dut.uio_oe.value) == 0x01
    assert int(dut.uo_out.value) & 0x01, "uo_out[0] shows seq running"
    await Timer(12 * BYTE_NS, unit="ns")
    assert bytes(out.read_nowait(out.count())) == b"\xa5"

    st = host.decode_status(await h.must(host.pkt_status()))
    assert st["running"] and st["pc"] == 20, f"should be parked at 'done' (20): {st}"

    # Writes are refused while running...
    status, _ = await h.transact(host.pkt_write(0, 0x0000))
    assert status == host.NAK
    status, _ = await h.transact(host.pkt_sr(0x00))
    assert status == host.NAK

    # ...and accepted after a halt: send another byte with the same program.
    await h.must(host.pkt_run(False))
    await h.must(host.pkt_sr(0x3C))
    await h.must(host.pkt_run(True))
    await Timer(12 * BYTE_NS, unit="ns")
    assert bytes(out.read_nowait(out.count())) == b"\x3c"


@cocotb.test()
async def rejects_bad_commands(dut):
    h = await start(dut)
    assert await h.transact(bytes([0x7A, 1, 2, 3])) == (host.NAK, 0x7A), "unknown command"
    assert (await h.transact(host.pkt_write(32, 0x1234)))[0] == host.NAK, "address 32 is out of range"
    assert await h.must(host.pkt_id()) == host.ID, "still works afterwards"


@cocotb.test(skip=GL)
async def resync_after_partial_packet(dut):
    """Half a packet, then silence: the chip drops it and the next packet works."""
    h = await start(dut)
    await h.src.write(bytes([0x49, 0x00]))
    await h.src.wait()
    await Timer(3.5, unit="ms")  # > 2**17 cycles at 40 MHz
    assert h.sink.empty(), "a partial packet must not get a reply"
    assert await h.must(host.pkt_id()) == host.ID
    st = host.decode_status(await h.must(host.pkt_status()))
    assert not st["overrun"]
