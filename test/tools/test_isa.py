# SPDX-License-Identifier: Apache-2.0
"""Tests for the ISA toolchain: generator, assembler, model (no simulator needed).

    cd test && pytest tools -v
"""

import random
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import isa_tables as T  # noqa: E402
import refasm  # noqa: E402
from refmodel import Model, verdict_rule  # noqa: E402

FW = ROOT / "firmware" / "engine"


def test_generated_files_are_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "isagen.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_opcodes_unique_and_reserved_unused():
    ops = [v["opcode"] for v in T.INSTR.values()]
    assert len(ops) == len(set(ops))
    assert not set(ops) & set(T.RESERVED_OPCODES)
    assert T.RESERVED_OPCODES == [0x18, 0x19, 0x1A, 0x1B]


def test_encode_decode_round_trip():
    rng = random.Random(1)
    for m, spec in T.INSTR.items():
        for _ in range(50):
            vals = {}
            for f, _opt in spec["operands"]:
                hi, lo = T.FIELDS[f]
                vals[f] = rng.randrange(1 << (hi - lo + 1))
            w = refasm.encode_word(m, vals)
            m2, vals2 = refasm.decode_word(w)
            assert (m2, vals2) == (m, vals), (m, vals, hex(w))


def asm(src, clock=60e6):
    a = refasm.Assembler(clock)
    return a, a.assemble(src)


def test_time_literals_round_to_nearest_cycle():
    a, w = asm("waitd 8.68us")
    assert refasm.decode_word(w[0]) == ("waitd", {"n": 521})  # 520.8 -> 521
    assert any("520.800" in n for n in a.notes)


def test_long_waitd_is_split_exactly():
    a, w = asm("waitd 6250")
    assert [refasm.decode_word(x)[1]["n"] for x in w] == [2047, 2047, 2047, 109]
    assert any("+3 words" in n for n in a.notes)


def test_timeouts_round_up_and_use_x16():
    _, w = asm("tmon 250us")  # 15000 cycles -> 938 x 16 = 15008
    assert refasm.decode_word(w[0]) == ("tmon", {"t": 938, "x16": 1})
    _, w = asm("tmo 1000")
    assert refasm.decode_word(w[0]) == ("tmo", {"t": 1000, "x16": 0})
    _, w = asm("tmon 1023, x16")
    assert refasm.decode_word(w[0]) == ("tmon", {"t": 1023, "x16": 1})
    with pytest.raises(refasm.AsmError):
        asm("tmon 2ms")  # 120000 cycles > 16368


def test_aliases_and_enums():
    _, w = asm("x: jnc x\njt x\ntsget Y, 3, last\njudge 5, raw, nonpass\ntrig 2, fall")
    assert refasm.decode_word(w[0]) == ("br", {"c": T.ENUMS["c"]["nC"], "a": 0})
    assert refasm.decode_word(w[1]) == ("br", {"c": T.ENUMS["c"]["T"], "a": 0})
    assert refasm.decode_word(w[2]) == ("tsget", {"r": 1, "ch": 3, "e": 2})
    assert refasm.decode_word(w[3]) == ("judge", {"b": 5, "raw": 1, "push": 1})
    with pytest.raises(refasm.AsmError):
        asm("trig 0, last")


def test_bounds_two_sided_rounding():
    a, _ = asm(".bound 0, min, 100ns\n.bound 1, max, 600ns", clock=60e6)
    assert a.bounds[0] == (96, 0, 0)  # 6 cycles exactly = 96/16
    assert a.bounds[1] == (576, 0, 1)
    a, _ = asm(".bound 2, min, 600ns", clock=48e6)  # 28.8 cycles = 460.8/16
    assert a.bounds[2] == (460, 1, 0)


def test_crc16_ccitt_false_vector():
    assert refasm.crc16_bytes(b"123456789") == 0x29B1
    assert refasm.crc16([0x3132, 0x3334]) == refasm.crc16_bytes(b"1234")


def test_claimed_pins_mask():
    a, w = asm("set 0, 1\nsho 9\nset 3, 0")
    assert a.image(w)[-1] == (1 << 0) | (1 << 9) | (1 << 3)


def test_firmware_examples_assemble():
    for f in FW.glob("*.s"):
        a = refasm.Assembler(None)
        words = a.assemble(f.read_text())
        assert 0 < len(words) <= 64, f.name


def test_verdict_rule_is_sound_by_brute_force():
    """For |m - T| < 2 cycles and any true bound in [n_lo, n_lo + inexact/16]: PASS/VIOL never lie."""
    rng = random.Random(7)
    for _ in range(20000):
        n_lo = rng.randrange(0, 400)
        inexact = rng.randrange(2)
        n_true = Fraction(n_lo) + (Fraction(rng.randrange(1, 64), 64) if inexact else 0)  # in 1/16 cycle
        t = Fraction(rng.randrange(0, 64 * 40), 64)  # true interval, cycles
        m = int(t) + rng.choice([-1, 0, 1, 2])  # any m with |m - T| < 2
        if abs(m - t) >= 2:
            continue
        is_max = rng.randrange(2)
        v = verdict_rule(m, n_lo, inexact, is_max, invalid=False)
        meets = (t * 16 >= n_true) if not is_max else (t * 16 <= n_true)
        if v == "PASS":
            assert meets, (m, t, n_lo, inexact, is_max)
        if v == "VIOL":
            assert not meets, (m, t, n_lo, inexact, is_max)


# -----------------------------------------------------------------------------
# Model
# -----------------------------------------------------------------------------


def load_and_run(src, pins, clock=60e6, extra_glob=None):
    a = refasm.Assembler(clock)
    words = a.assemble(src)
    img = a.image(words)
    md = Model()
    g = refasm.global_image(pins)
    for i, w in enumerate(g):
        md.write_global(i, w)
    for i, w in enumerate(img):
        md.write_engine(i, w)
    md.set_crc(0, refasm.crc16(g))
    md.set_crc(1, refasm.crc16(img))
    assert md.run()
    return md


def test_model_refuses_bad_crc_and_unowned_pins():
    md = Model()
    md.set_crc(0, 1)
    md.run()
    for _ in range(120):
        md.step(0, 0)
    assert md.state == "halted" and md.crc_fail
    a = refasm.Assembler(60e6)
    img = a.image(a.assemble("set 3, 1"))  # claims pin 3, not owned
    md = Model()
    g = refasm.global_image({})
    for i, w in enumerate(img):
        md.write_engine(i, w)
    md.set_crc(0, refasm.crc16(g))
    md.set_crc(1, refasm.crc16(img))
    md.run()
    for _ in range(120):
        md.step(0, 0)
    assert md.state == "halted" and md.crc_fail


def test_model_uart_tx_edges_at_t0_plus_2_plus_k_bit():
    src = (FW / "uart_tx.s").read_text()
    md = load_and_run(src, {0: (1, 1)})  # pin 0: engine 0, push-pull
    md.in_push(0xA5)
    cts_low = 0x00  # ui_in[0] = 0: CTS asserted
    trace, mark_cycle = [], None
    for c in range(200 + 10 * 521 + 50):
        before = md.observe()
        md.step(cts_low, 0)
        trace.append(md.pins()[0] & 1)
        if mark_cycle is None and before["state"] == "running" and md.DL == md.now - 1 and before["pc"] == 5:
            mark_cycle = md.now - 1  # cycle in which MARK issued (pc 5 in uart_tx.s)
    assert mark_cycle is not None
    # trace[c] is the pad level in cycle c+1. Expected: start bit from t0+2, bit k from t0+2+k*BIT.
    bits = [0] + [(0xA5 >> i) & 1 for i in range(8)] + [1]
    for k, lvl in enumerate(bits):
        c0 = mark_cycle + 2 + k * 521
        assert trace[c0 - 1] == lvl, f"bit {k} at cycle {c0}"
        assert trace[c0 - 2] != lvl or k == 0 or bits[k - 1] == lvl, f"edge of bit {k} not exactly at {c0}"
