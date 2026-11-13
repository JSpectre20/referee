# SPDX-License-Identifier: Apache-2.0
"""UART firmware (spike/firmware/uart_*.s) against independent UART devices (spike/bench.py).

The devices know only the protocol (8N1 / 8E1, bit period, tolerance), not the firmware. Random data
with fixed seeds. Negative control: a transmitter whose bit time is 3% long must fail the
receiver device's edge-tolerance check.
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench import Bench, UartRx, UartTx  # noqa: E402

BIT = 60e6 / 115200  # the ideal bit at 115200 baud, in cycles (520.83); the firmware rounds to 521
TIMEOUT = 13 * 600  # cycles per frame, generously


def tx_bench(prog, parity, equ=None):
    b = Bench()
    b.net("line", pull=1)  # an idle UART line is high
    b.net("cts", pull=0)  # CTS asserted (active low)
    e = b.engine("A", {"uio0": "line", "ui0": "cts"}).load(prog, {"uio0": "pp"}, equ=equ)
    rx = b.add(UartRx("line", BIT, parity))
    return b, e, rx


def rx_bench(prog, parity, gap=0, bit=BIT, equ=None):
    b = Bench()
    tx = b.add(UartTx("line", bit, parity, gap=gap, start=300))
    e = b.engine("B", {"uio1": "line"}).load(prog, {}, equ=equ)
    return b, e, tx


def run_tx(prog, parity, data, equ=None):
    b, e, rx = tx_bench(prog, parity, equ)
    e.send(data)
    b.run_until(lambda: len(rx.frames) == len(data), TIMEOUT * len(data) + 2000)
    b.run(int(BIT))  # the last stop bit
    return rx


def test_uart_tx_8n1_random_bytes():
    data = random.Random(10).randbytes(16)
    rx = run_tx("uart_tx.s", None, data)
    assert rx.errors == []
    assert rx.frames == [(d, True, True) for d in data]


def test_uart_tx_8e1_random_bytes():
    data = random.Random(11).randbytes(16)
    rx = run_tx("uart_tx_8e1.s", "even", data)
    assert rx.errors == []
    assert rx.frames == [(d, True, True) for d in data]


def test_uart_tx_8n1_edges_on_the_bit_grid():
    """Every edge within 1 cycle of start + k * 521 (the firmware's rounded bit): the device's
    2% tolerance is not hiding a drift."""
    b, e, rx = tx_bench("uart_tx.s", None)
    e.send(0x55, 0xA3)
    b.run_until(lambda: len(rx.frames) == 2, 2 * TIMEOUT)
    edges = b.edges("line")
    s = edges[0][0]  # the first start bit
    frame = [c for c, _ in edges if s <= c < s + 10 * 521]
    assert len(frame) > 2 and all((c - s) % 521 == 0 for c in frame), frame


def test_negative_control_uart_tx_bit_time_3_percent_long_fails():
    data = random.Random(12).randbytes(4)
    rx = run_tx("uart_tx.s", None, data, equ={"BIT": "1s/115200*103/100"})
    assert any("off the bit grid" in m for m in rx.errors), rx.errors


def test_uart_rx_8n1_random_bytes_and_framing_error_dropped():
    rng = random.Random(20)
    data = rng.randbytes(12)
    b, e, tx = rx_bench("uart_rx.s", None, gap=int(2 * BIT))
    for i, d in enumerate(data):
        tx.send(d, "framing" if i == 5 else None)
    b.run_until(tx.idle, TIMEOUT * len(data) + 2000)
    b.run(2 * BIT)
    assert e.out() == list(data[:5] + data[6:])


def decode_8e1(out):
    """OUT bytes of uart_rx_8e1.s -> [(byte, "ok" | "parity" | "framing")] (its documented protocol)."""
    res, i = [], 0
    while i < len(out):
        d, s = out[i], out[i + 1]
        if s == 0xFF:
            res.append((d, "ok"))
            i += 2
        else:
            assert s == 0x00, f"bad status byte {s:#x}"
            res.append((d, "parity" if out[i + 2] == 0xFF else "framing"))
            assert out[i + 2] in (0x00, 0xFF)
            i += 3
    return res


def test_uart_rx_8e1_random_bytes_with_parity_and_framing_errors():
    rng = random.Random(21)
    frames = [(rng.randrange(256), rng.choice([None, None, "parity", "framing"])) for _ in range(16)]
    b, e, tx = rx_bench("uart_rx_8e1.s", "even", gap=int(2 * BIT))
    for d, fault in frames:
        tx.send(d, fault)
    b.run_until(tx.idle, TIMEOUT * len(frames) + 2000)
    b.run(2 * BIT)
    assert decode_8e1(e.out()) == [(d, fault or "ok") for d, fault in frames]
    assert {f for _, f in frames} == {None, "parity", "framing"}  # the seed covers all three


@pytest.mark.parametrize("bit", [BIT, 70])
def test_uart_rx_8e1_back_to_back_frames(bit):
    """No idle time between frames (good and parity errors): the status work fits in the stop
    bit, also at 70 cycles per bit (857 kbaud at 60 MHz), the shortest the header states."""
    rng = random.Random(22)
    frames = [(rng.randrange(256), rng.choice([None, None, "parity"])) for _ in range(12)]
    equ = None if bit == BIT else {"BIT": f"{bit}*1s/60000000", "HALF": f"{bit}*1s/60000000/2"}
    b, e, tx = rx_bench("uart_rx_8e1.s", "even", gap=0, bit=bit, equ=equ)
    for d, fault in frames:
        tx.send(d, fault)
    b.run_until(tx.idle, TIMEOUT * len(frames) + 2000)
    b.run(2 * bit)
    assert decode_8e1(e.out()) == [(d, fault or "ok") for d, fault in frames]


def two_engines(tx_prog, rx_prog, parity, data):
    """Engine A runs a UART TX, engine B a UART RX on the same line; a device watches the line."""
    b = Bench()
    b.net("line", pull=1)
    b.net("cts", pull=0)
    a = b.engine("A", {"uio0": "line", "ui0": "cts"}).load(tx_prog, {"uio0": "pp"})
    r = b.engine("B", {"uio1": "line"}).load(rx_prog, {})
    mon = b.add(UartRx("line", BIT, parity))
    a.send(data)
    b.run_until(lambda: len(mon.frames) == len(data), TIMEOUT * len(data) + 2000)
    b.run(2 * BIT)
    assert mon.errors == []
    return r.out()


def test_two_engines_uart_8n1():
    data = random.Random(30).randbytes(10)
    assert two_engines("uart_tx.s", "uart_rx.s", None, data) == list(data)


def test_two_engines_uart_8e1():
    data = random.Random(31).randbytes(10)
    out = two_engines("uart_tx_8e1.s", "uart_rx_8e1.s", "even", data)
    assert decode_8e1(out) == [(d, "ok") for d in data]
