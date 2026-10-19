# SPDX-License-Identifier: Apache-2.0
"""Connect the simulated chip's UART to a virtual serial port (a Linux pty).

Real host software can then talk to the simulation exactly as it would to the
chip: host/referee.py, pyserial scripts, or a terminal like screen/picocom.

    cd test && make pty                  # leave running; prints the device
    python host/referee.py --port test/sim.tty --timeout 20 run test/transcripts/*.txt
    picocom test/sim.tty                 # or type at it by hand

The simulation runs far slower than real time (a reply takes about a second),
so give host tools a generous timeout. The bridge always talks at the reset
baud rate, so don't change the divisor through it.
Set PTY_SECONDS to stop after that many wall-clock seconds (default: run
until killed).
"""

import errno
import logging
import os
import pty
import time
import tty
from pathlib import Path

import cocotb
from cocotb.triggers import Timer
from cocotbext.uart import UartSink, UartSource

CLK_HZ = 40_000_000
BAUD = CLK_HZ / 347
POLL_NS = 20_000  # check the pty every 20 us of simulated time


@cocotb.test()
async def serve(dut):
    dut.ena.value = 1
    dut.ui_in_pins.value = 0
    dut.uio_in.value = 0
    dut.uart_rx.value = 1
    dut.rst_n.value = 0

    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    device = os.ttyname(slave)
    link = Path(__file__).resolve().parent / "sim.tty"
    link.unlink(missing_ok=True)
    link.symlink_to(device)

    for sig in (dut.uart_rx, dut.uart_tx):
        logging.getLogger(f"cocotb.{sig._path}").setLevel(logging.WARNING)
    src = UartSource(dut.uart_rx, baud=BAUD, bits=8)
    sink = UartSink(dut.uart_tx, baud=BAUD, bits=8)

    await Timer(500, unit="ns")
    dut.rst_n.value = 1
    dut._log.info("Simulated chip's UART is on %s (link: %s)", device, link)

    limit = float(os.environ.get("PTY_SECONDS", "0"))
    start = time.monotonic()
    try:
        while not limit or time.monotonic() - start < limit:
            await Timer(POLL_NS, unit="ns")
            try:
                data = os.read(master, 256)
            except BlockingIOError:
                data = b""
            except OSError as e:
                if e.errno != errno.EIO:  # EIO: no client attached right now
                    raise
                data = b""
            if data:
                await src.write(data)
            out = sink.read_nowait()
            if out:
                os.write(master, bytes(out))
    finally:
        link.unlink(missing_ok=True)
        os.close(master)
        os.close(slave)
