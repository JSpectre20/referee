# SPDX-License-Identifier: Apache-2.0
"""Core checks of the ISA v1 candidates in the spike's assembler and model.

These test the mechanisms themselves against independent references (a textbook CRC, a
brute-force search over true intervals). They are NOT "real programs" for the two-program
rule in spike/README.md: only firmware in spike/firmware/ that passes a bus-level test counts.
"""

import random
import sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import asm_v1  # noqa: E402
import gen  # noqa: E402
import model_v1  # noqa: E402
from model_v1 import Model, verdict_rule  # noqa: E402

T = asm_v1.T


def test_generated_tables_are_up_to_date():
    assert (HERE.parent / "isa_tables_v1.py").read_text() == gen.build()


def test_encode_decode_round_trip():
    rng = random.Random(1)
    for mnemonic, spec in T.INSTR.items():
        for _ in range(50):
            vals = {f: rng.randrange(1 << (T.FIELDS[f][0] - T.FIELDS[f][1] + 1)) for f, _ in spec["operands"]}
            assert asm_v1.decode_word(asm_v1.encode_word(mnemonic, vals)) == (mnemonic, vals)


def test_branches_reach_128_words_and_all_14_conditions():
    a = asm_v1.Assembler(60e6)
    for name, code in T.ENUMS["c"].items():
        (w,) = a.assemble(f"br {name}, 127")
        assert asm_v1.decode_word(w) == ("br", {"c": code, "a": 127})


# ---------------------------------------------------------------- C2: CRC / parity

def crc15_can(bits):
    """CAN 2.0 CRC-15, straight from the standard's pseudo-code."""
    crc = 0
    for b in bits:
        nxt = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def crc8(bits, poly):
    crc = 0
    for b in bits:
        top = (crc >> 7) & 1
        crc = (crc << 1) & 0xFF
        if top ^ b:
            crc ^= poly
    return crc


def test_crc_register_matches_textbook_crcs():
    rng = random.Random(2)
    for _ in range(200):
        bits = [rng.randrange(2) for _ in range(rng.randrange(1, 90))]
        md = Model()
        md.POLY = 0x4599 << 1  # CRC-15, left-aligned
        for b in bits:
            md._feed(b)
        assert md.L >> 1 == crc15_can(bits) and md.L & 1 == 0
        # appending the CRC itself, MSB first, leaves zero (how a receiver checks: flag Z)
        for k in range(14, -1, -1):
            md._feed((crc15_can(bits) >> k) & 1)
        assert md.L == 0
        for poly in (0x07, 0x31):  # SMBus PEC; Dallas 1-Wire (bits in wire order)
            md = Model()
            md.POLY = poly << 8
            for b in bits:
                md._feed(b)
            assert md.L >> 8 == crc8(bits, poly) and md.L & 0xFF == 0


def test_parity_is_a_one_bit_crc():
    rng = random.Random(3)
    for _ in range(100):
        bits = [rng.randrange(2) for _ in range(8)]
        md = Model()
        md.POLY = 0x8000
        for b in bits:
            md._feed(b)
        assert md.L == (sum(bits) & 1) << 15  # top bit = even-parity bit; `sho p, crc` sends it


# ---------------------------------------------------------------- C3: stuffing

def can_stuff(bits):
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            out.append(1 - b)
            last, run = 1 - b, 1
    return out


def test_can_stuffing_rule_matches_reference():
    rng = random.Random(4)
    for _ in range(300):
        bits = [rng.randrange(2) if rng.random() < 0.5 else 0 for _ in range(rng.randrange(1, 80))]
        md = Model()
        md.st, md.tap = T.ENUMS["st"]["can"], T.ENUMS["tap"]["sho"]
        out = []
        for b in bits:
            md._run(b)
            out.append(b)
            if md._stuff_due():  # what `ju` sees; firmware then issues `sho p, stuff`
                s = 1 - md.lastbit
                md._run_restart(s)
                out.append(s)
        assert out == can_stuff(bits)


# ---------------------------------------------------------------- D: guard band

def test_guarded_verdict_is_sound_by_brute_force():
    """For random bounds and guards: whenever the rule says PASS or VIOLATION, every spec-defined
    interval consistent with the measurement agrees. Model of the physics, in 1/16 cycle:
    the pad interval T_pad satisfies |m - T_pad| < 2 cycles (proved for v0.3), and the
    spec-defined interval T_s satisfies T_pad - g_plus <= T_s <= T_pad + g_minus."""
    rng = random.Random(5)
    seen = set()
    for _ in range(3000):
        clock = rng.choice([60e6, 48e6])
        n_ns = rng.choice([0, 100, 600, 900, 1300])
        gm_ns, gp_ns = rng.choice([0, 30, 120]), rng.choice([0, 30, 300])
        kind = rng.choice(["min", "max"])
        a = asm_v1.Assembler(clock)
        a.assemble(f".bound 0, {kind}, {n_ns}ns, {gm_ns}ns, {gp_ns}ns\n nop")
        n_lo, width, is_max = a.bounds[0]
        if n_lo & 0x80000:
            n_lo -= 1 << 20
        cyc = Fraction(clock) / 10**9 * 16  # 1/16 cycles per ns
        N, gm, gp = n_ns * cyc, gm_ns * cyc, gp_ns * cyc
        for m in range(-6, 110):
            v = verdict_rule(m, n_lo, width, is_max, 0)
            seen.add((kind, v))
            if v == "INDET":
                continue
            # extreme pad intervals consistent with m (open interval, so stay strictly inside),
            # and the extreme spec intervals consistent with each
            eps = Fraction(1, 1000)
            for t_pad in (m * 16 - 32 + eps, m * 16 + 32 - eps):
                for t_s in (t_pad - gp, t_pad + gm):
                    meets = t_s >= N if kind == "min" else t_s <= N
                    assert meets == (v == "PASS"), (clock, kind, n_ns, gm_ns, gp_ns, m, v, float(t_s), float(N))
    assert len(seen) == 6  # every verdict reached for both kinds


def test_guard_band_rounding_never_shrinks_the_band():
    """Direct check of the assembler's rounding on a fine grid (the brute-force test above would
    not notice n_lo + width rounded DOWN when N + g_plus sits just above a 1/16-cycle step)."""
    rng = random.Random(6)
    for _ in range(4000):
        clock = rng.choice([60e6, 48e6, 40e6])
        n_ns, gm_ns, gp_ns = rng.uniform(0, 2000), rng.uniform(0, 300), rng.uniform(0, 300)
        a = asm_v1.Assembler(clock)
        a.assemble(f".bound 0, min, {n_ns:.3f}ns, {gm_ns:.3f}ns, {gp_ns:.3f}ns\n nop")
        n_lo, width, _ = a.bounds[0]
        n_lo -= (1 << 20) if n_lo & 0x80000 else 0
        cyc = Fraction(clock) / 10**9 * 16
        N, gm, gp = (Fraction(f"{x:.3f}") * cyc for x in (n_ns, gm_ns, gp_ns))
        assert n_lo <= N - gm < n_lo + 1 and n_lo + width - 1 < N + gp <= n_lo + width


def test_guard_band_too_wide_is_refused_not_clipped():
    a = asm_v1.Assembler(60e6)
    try:
        a.assemble(".bound 0, min, 600ns, 600ns, 600ns\n nop")  # 72 cycles wide > 63.9
    except asm_v1.AsmError as e:
        assert "Refused" in str(e)
    else:
        raise AssertionError("a guard band wider than the field must be refused")


def test_negative_n_lo_survives_the_image():
    """tHD;DAT min = 0 ns with a guard needs a negative n_lo; the model must read it back signed."""
    a = asm_v1.Assembler(60e6)
    img = a.image(a.assemble(".bound 3, min, 0ns, 50ns\n nop"))
    w_hi, w_lo = img[asm_v1.PROGRAM_WORDS + 6], img[asm_v1.PROGRAM_WORDS + 7]
    n_lo = (w_hi << 4) | (w_lo >> 12)
    assert n_lo & 0x80000 and n_lo - (1 << 20) == -48 and w_lo & 0x3FF == 96


# ---------------------------------------------------------------- review findings (13 Nov)

def test_bsc_off_assembles():
    """YAML 1.1 reads a bare `off` as False; the enum key is quoted in isa_v1.yaml."""
    a = asm_v1.Assembler(60e6)
    (w,) = a.assemble("bsc off")
    assert asm_v1.decode_word(w) == ("bsc", {"tap": 0, "init": 0, "st": 0, "inv": 0})


def run_program(src, cycles_before=300, words=64):
    a = asm_v1.Assembler(60e6)
    img = a.image(a.assemble(src))
    md = Model(words)
    g = asm_v1.global_image({})
    for i, w in enumerate(g):
        md.write_global(i, w)
    for i, w in enumerate(img):
        md.write_engine(i, w)
    md.set_crc(0, asm_v1.crc16(g))
    md.set_crc(1, asm_v1.crc16(img))
    assert md.run()
    for _ in range(cycles_before):
        md.step(0, 0)
    assert md.ran and not md.crc_fail  # the RUN check passed (the program may already have halted)
    return md


def serial_crc(bits, poly, width, init):
    crc = init
    for b in bits:
        top = (crc >> (width - 1)) & 1
        crc = (crc << 1) & ((1 << width) - 1)
        if top ^ b:
            crc ^= poly
    return crc


def test_init_ones_is_right_for_crcs_narrower_than_16_bits():
    rng = random.Random(7)
    for poly, width in ((0x1021, 16), (0x1D, 8), (0x05, 5)):
        left = (poly << (16 - width)) & 0xFFFF
        md = run_program(f"crcp {left & 0xFF}\n crcp {left >> 8}, hi\n bsc sho, ones\n halt", 320)
        assert md.L == ((1 << width) - 1) << (16 - width)
        for _ in range(50):
            bits = [rng.randrange(2) for _ in range(rng.randrange(1, 60))]
            md.L = ((1 << width) - 1) << (16 - width)
            for b in bits:
                md._feed(b)
            assert md.L >> (16 - width) == serial_crc(bits, poly, width, (1 << width) - 1)
            assert md.L & ((1 << (16 - width)) - 1) == 0


def test_trig_rejects_a_v03_channel_number_and_repeated_operands():
    for src, word in (("trig 1, rise", "mask"), ("bsc sho, zeros, ones", "repeats")):
        try:
            asm_v1.Assembler(60e6).assemble(src)
        except asm_v1.AsmError as e:
            assert word in str(e)
        else:
            raise AssertionError(f"{src!r} must be refused")


def tsts_after(wave, test):
    """Run `trig CH0|CH1, any; waits; <test>; halt` over a (ui_in per cycle) wave -> (C, A)."""
    md = run_program(f"trig CH0|CH1, any\n tmon 1023, x16\n waits\n {test}\n halt")
    for ui in wave + [wave[-1]] * 20:
        md.step(ui, 0)
    assert md.state == "halted" and md.T == 0
    return md.C, md.A


def test_tsts_reports_ambiguity_when_edges_are_too_close_to_order():
    ch0, ch1 = 1 << 4, 1 << 5
    alone = [0] * 10 + [ch0] * 30                       # CH0 rises alone
    assert tsts_after(alone, "tsts 0, trise") == (1, 0)
    assert tsts_after(alone, "tsts 1, level") == (0, 0)
    assert tsts_after(alone, "tsts 0, level") == (1, 0)  # its own trigger edge: the level after it
    adjacent = [0] * 10 + [ch0] + [ch0 | ch1] * 30      # CH1 rises one cycle later: order unknown
    assert tsts_after(adjacent, "tsts 0, trise") == (1, 1)
    assert tsts_after(adjacent, "tsts 1, level")[1] == 1
    together = [0] * 10 + [ch0 | ch1] * 30              # both in one cycle
    assert tsts_after(together, "tsts 0, trise") == (1, 1)
    assert tsts_after(together, "tsts 1, level")[1] == 1
    apart = [0] * 10 + [ch0] * 8 + [ch0 | ch1] * 30     # 8 cycles apart: the second is a lost trigger, no ambiguity
    assert tsts_after(apart, "tsts 0, trise") == (1, 0)
    assert tsts_after(apart, "tsts 1, level") == (0, 0)
