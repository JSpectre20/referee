# SPDX-License-Identifier: Apache-2.0
"""The SPI timing referee (spike/firmware/spi_referee.s) against an independent oracle.

Same method as test_ref_i2c.py: random SPI frames (modes 0 and 3) with every interval drawn around
its bound, an oracle that lists from the waveform alone which rule applies to which pair of edges,
and a check of every verdict the model streams out: a PASS or a VIOLATION (or a CAN'T TELL that is
neither poisoned nor flagged) must name a (rule, interval) the oracle also found, ending shortly
before the verdict came out, and must be what the rule gives for that interval.

The traffic also has "foreign" SCK and MOSI activity while CS is high (another target's frame): it
must never be judged (candidate K keeps it from being an event at all).

Edges are on whole clock cycles (the model's resolution); sub-cycle soundness belongs to the proofs.
"""

import math
import random
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import asm_v1  # noqa: E402
from model_v1 import Model, verdict_rule, QUARTER  # noqa: E402

FW = HERE.parent / "firmware" / "spi_referee.s"
CLOCK = 60e6
NS = CLOCK / 1e9
CS, SCK, MOSI = 0, 1, 2
RULES = {0: "tSU", 1: "tCH", 2: "tCL", 3: "tCSS", 4: "tCSH", 5: "tCSD"}
BOUND_NS = {0: 50, 1: 250, 2: 250, 3: 250, 4: 250, 5: 500}  # must match the .bound lines
LATENCY = 32


# ------------------------------------------------------------------ traffic generator

def spi_wave(rng, n_frames, mode, spread, floor=6, foreign=True):
    """-> (cs, sck, mosi), one level per clock cycle. Modes 0 and 3 (sample on SCK's rise).
    `spread`: how far below its minimum an interval may be drawn (0 = always legal)."""
    assert mode in (0, 3)
    idle = 0 if mode == 0 else 1
    cs, sck, mosi = [1], [idle], [0]

    def hold(n):
        for _ in range(int(n)):
            cs.append(cs[-1])
            sck.append(sck[-1])
            mosi.append(mosi[-1])

    def t(rule):  # an interval around rule's minimum, in cycles; never under `floor`
        return max(floor, math.ceil(BOUND_NS[rule] * rng.uniform(1 - spread, 1.6) * NS))

    def put(line, v):
        line[-1] = v

    def data_then_rise(low, bit):
        """MOSI changes `h` cycles into the low phase so that its setup before the rise is drawn
        around tSU; returns after the rise."""
        su = t(0)
        h = max(0, low - su)
        hold(h)
        put(mosi, bit)
        hold(low - h)
        put(sck, 1)

    hold(200)
    for _ in range(n_frames):
        if foreign and rng.random() < 0.5:  # another target's frame: SCK and MOSI move, CS stays high
            for _ in range(rng.randrange(2, 9)):
                hold(rng.randrange(floor, 40))
                put(sck, 1 - sck[-1])
                if rng.random() < 0.5:
                    put(mosi, rng.randrange(2))
            if sck[-1] != idle:
                hold(rng.randrange(floor, 40))
                put(sck, idle)
        hold(t(5))                                   # tCSD (and at least `floor` after any edge)
        put(cs, 0)                                   # CS falls
        bits = [rng.randrange(2) for _ in range(8 * rng.randrange(1, 3))]
        if mode == 0:
            data_then_rise(t(3), bits[0])            # tCSS, with bit 0 set up inside it
            hold(t(1))
            for b in bits[1:]:
                put(sck, 0)
                data_then_rise(t(2), b)
                hold(t(1))
            put(sck, 0)
            hold(t(4))                               # tCSH after the last (falling) edge
        else:
            hold(t(3))                               # tCSS to the first (falling) edge
            for k, b in enumerate(bits):
                put(sck, 0)
                data_then_rise(t(2), b)
                hold(t(1) if k < len(bits) - 1 else t(4))  # tCH, or tCSH after the last rise
        put(cs, 1)                                   # CS rises
    hold(100)
    return cs, sck, mosi


# ------------------------------------------------------------------ oracle (from the waveform alone)

def oracle(cs, sck, mosi):
    """-> [(rule, true interval in cycles or None, end cycle)], in the order the rules end
    (for one SCK rise: tCL / tCSS first, then tSU)."""
    out = []
    last = {}  # (line, new level) -> cycle
    mosi_any = None
    first = False  # the next SCK edge is the frame's first

    def gap(now, then):
        return None if then is None else now - then

    for c in range(1, len(cs)):
        dcs, dck, dmo = cs[c] != cs[c - 1], sck[c] != sck[c - 1], mosi[c] != mosi[c - 1]
        assert not (dcs and dck), "generator: CS and SCK never change in the same cycle"
        if dcs and not cs[c]:                         # CS falls
            out.append((5, gap(c, last.get(("cs", 1))), c))
            first = True
        elif dcs:                                     # CS rises
            if not first:
                out.append((4, gap(c, max(last.get(("sck", 0), -1), last.get(("sck", 1), -1))), c))
            first = False
        elif dck and not cs[c]:                       # an SCK edge inside a frame
            # MOSI's last edge at or before this cycle: a MOSI change in the same cycle as the rise
            # is a setup of 0 (the chip flags it as too close to order)
            m_last = c if dmo else mosi_any
            if first:
                out.append((3, gap(c, last.get(("cs", 0))), c))
                if sck[c]:
                    out.append((0, gap(c, m_last), c))
                first = False
            elif sck[c]:
                out.append((2, gap(c, last.get(("sck", 0))), c))
                out.append((0, gap(c, m_last), c))
            else:
                out.append((1, gap(c, last.get(("sck", 1))), c))
        if dcs:
            last[("cs", cs[c])] = c
        if dck:
            last[("sck", sck[c])] = c
        if dmo:
            mosi_any = c
    return out


# ------------------------------------------------------------------ run the model

def run_referee(cs, sck, mosi, src=None, depth=1):
    a = asm_v1.Assembler(CLOCK)
    src = FW.read_text() if src is None else src
    words = a.assemble(re.sub(r"\bjudge (\d+)", r"judge \1, raw", src))
    img = a.image(words)
    md = Model(words=64, depth=depth)
    g = asm_v1.global_image({})
    for i, w in enumerate(g):
        md.write_global(i, w)
    for i, w in enumerate(img):
        md.write_engine(i, w)
    md.set_crc(0, asm_v1.crc16(g))
    md.set_crc(1, asm_v1.crc16(img))
    assert md.run()

    def ui(c, k, m):
        return (c << (4 + CS)) | (k << (4 + SCK)) | (m << (4 + MOSI))

    for _ in range(300):
        md.step(ui(cs[0], sck[0], mosi[0]), 0)
    assert md.state == "running"
    slots = []
    wave = list(zip(cs, sck, mosi)) + [(cs[-1], sck[-1], mosi[-1])] * 200
    for k, lv in enumerate(wave):
        md.step(ui(*lv), 0)
        v = md.verdict_pop()
        if v is not None:
            slots.append((v, k))
    assert not md.sticky["VERDICT_OVF"]
    verdicts = []
    for (hdr, k), (raw, _) in zip(slots[0::2], slots[1::2]):
        assert hdr >> 8 & 1
        m = raw - 0x10000 if raw & 0x8000 else raw
        verdicts.append({"b": (hdr >> 4) & 0xF, "m": m, "v": ["INDET", "PASS", "VIOL"][hdr >> 14], "out": k,
                         "poisoned": hdr >> 10 & 1, "flagged": bool(hdr & 0x3808)})
    return a, md, len(words), verdicts


def check(a, expected, verdicts, latency=LATENCY):
    """Soundness, as test_ref_i2c.check: every PASS / VIOLATION, and every CAN'T TELL that is
    neither poisoned nor flagged, must match the oracle's next entry of the same rule and interval
    that ended at most `latency` cycles before the verdict came out. Returns per-rule counts."""
    table = {b: dict(events=0, judged=0, P=0, V=0, I=0, flagged=0, poisoned=0) for b in RULES}
    bounds = {}
    for b in RULES:
        n_lo, width, is_max = a.bounds[b]
        bounds[b] = (n_lo - ((1 << 20) if n_lo & 0x80000 else 0), width, is_max)
    for b, _, _ in expected:
        table[b]["events"] += 1
    i = 0
    for vd in verdicts:
        b, m, v = vd["b"], vd["m"], vd["v"]
        row = table[b]
        if vd["poisoned"] or (vd["flagged"] and v == "INDET"):
            assert v == "INDET", f"a poisoned JUDGE answered {v}"
            row["poisoned" if vd["poisoned"] else "flagged"] += 1
            continue
        while i < len(expected) and not (expected[i][0] == b and (expected[i][1] is None or expected[i][1] == m)
                                         and 0 < vd["out"] - expected[i][2] <= latency):
            i += 1
        assert i < len(expected), (f"verdict {v} (rule {RULES[b]}, m = {m}, out at cycle {vd['out']}) "
                                   "matches nothing the oracle found: a verdict about the wrong pair of edges")
        true = expected[i][1]
        i += 1
        want = verdict_rule(m, *bounds[b], int(true is None or true >= QUARTER))
        assert v == want, f"rule {RULES[b]}, interval {true} cycles: chip says {v}, the rule gives {want}"
        row["judged"] += 1
        row[{"PASS": "P", "VIOL": "V", "INDET": "I"}[v]] += 1
    return table


def report(title, words, md, table):
    ev = sum(r["events"] for r in table.values())
    un = ev - sum(r["judged"] for r in table.values())
    print(f"\n{title}: {words} words, depth {md.depth}, LOST {md.lost}; {un} of {ev} events not judged")
    print(f"  {'rule':6} {'events':>7} {'judged':>7} {'PASS':>6} {'VIOL':>6} {'INDET':>6} {'flagged':>8} {'poisoned':>9}")
    for b, r in table.items():
        print(f"  {RULES[b]:6} {r['events']:7} {r['judged']:7} {r['P']:6} {r['V']:6} {r['I']:6} "
              f"{r['flagged']:8} {r['poisoned']:9}")


def run_mix(seed, n, mode, spread, floor=6, depth=1, src=None, foreign=True):
    rng = random.Random(seed)
    cs, sck, mosi = spi_wave(rng, n, mode, spread, floor, foreign)
    a, md, words, verdicts = run_referee(cs, sck, mosi, src, depth)
    expected = oracle(cs, sck, mosi)
    return md, words, check(a, expected, verdicts, LATENCY * (2 * depth - 1)), expected


# ------------------------------------------------------------------ tests

@pytest.mark.parametrize("mode", [0, 3])
def test_legal_spi_traffic_every_event_judged_no_violation(mode, capsys):
    md, words, table, expected = run_mix(200 + mode, 40, mode, 0.0)
    with capsys.disabled():
        report(f"spi_referee.s, mode {mode}, legal traffic with foreign SCK activity while CS is high",
               words, md, table)
    assert words <= 64
    assert md.lost == 0
    no_earlier = sum(1 for b, m, _ in expected if m is None)  # the first tCSD: CS never rose before
    assert no_earlier == 1
    for b, r in table.items():
        assert r["V"] == 0, f"{RULES[b]}: a VIOLATION on legal traffic"
        assert r["poisoned"] == 0, f"{RULES[b]}: poisoned on legal traffic"
        assert r["judged"] == r["events"] - (1 if b == 5 else 0), f"{RULES[b]}: an event got no answer"


@pytest.mark.parametrize("mode", [0, 3])
@pytest.mark.parametrize("depth", [1, 2])
def test_spi_traffic_with_violations_never_a_wrong_pair(mode, depth, capsys):
    viol = 0
    for seed, spread, floor in ((210, 0.3, 6), (211, 0.6, 4), (212, 0.9, 2)):
        md, words, table, _ = run_mix(seed + 10 * mode, 40, mode, spread, floor, depth)
        with capsys.disabled():
            report(f"spi_referee.s, mode {mode}, depth {depth}, intervals down to {round(100 * (1 - spread))}%"
                   f" of their minimum (floor {floor} cycles)", words, md, table)
        viol += sum(r["V"] for r in table.values())
    assert viol > 30, "the traffic must contain violations that are caught"


def test_bounds_in_the_test_match_the_firmware():
    a = asm_v1.Assembler(CLOCK)
    a.assemble(FW.read_text())
    for b, ns in BOUND_NS.items():
        assert a.bounds[b][0] == math.floor(ns * NS * 16), RULES[b]


def test_speed_limit_sck_half_period_sweep(capsys):
    """Frames with every SCK half period exactly H cycles (the other intervals legal; under 15
    cycles tCH / tCL are violations, which is fine here): the fastest SCK the firmware referee
    follows without losing an event, at queue depth 1 and 2. Every verdict is still checked."""
    rows = {}
    for depth in (1, 2):
        for h in range(8, 17):
            cs, sck, mosi = spi_wave_fixed(random.Random(300 + h), 12, h)
            a, md, words, verdicts = run_referee(cs, sck, mosi, depth=depth)
            check(a, oracle(cs, sck, mosi), verdicts, LATENCY * (2 * depth - 1))
            rows[(depth, h)] = md.lost
    with capsys.disabled():
        print("\nSPI referee speed limit (SCK half period H cycles at 60 MHz, other intervals legal): LOST events")
        print("  H      " + " ".join(f"{h:5}" for h in range(8, 17)))
        for depth in (1, 2):
            print(f"  depth {depth}" + " ".join(f"{rows[(depth, h)]:5}" for h in range(8, 17)))
    # the limit stated in spi_referee.s: half periods of 14 cycles or more lose nothing
    assert all(rows[(d, h)] == 0 for d in (1, 2) for h in (14, 15, 16)), "the stated speed limit"
    assert rows[(1, 13)] > 0, "the sweep must reach the limit (or the stated limit is too cautious)"


def spi_wave_fixed(rng, n_frames, h, tcss=20, mode=0):
    """Frames with every SCK half period exactly h cycles and CS-to-first-edge exactly tcss; MOSI
    changes 1 cycle after each falling edge; tCSH 20 and tCSD 40 cycles (legal)."""
    idle = 0 if mode == 0 else 1
    cs, sck, mosi = [1], [idle], [0]

    def hold(n):
        for _ in range(n):
            cs.append(cs[-1])
            sck.append(sck[-1])
            mosi.append(mosi[-1])

    hold(200)
    for _ in range(n_frames):
        hold(40)
        cs[-1] = 0
        if mode == 0:
            hold(1)
            mosi[-1] = rng.randrange(2)
            hold(tcss - 1)
            for k in range(16):
                sck[-1] = 1
                hold(h)
                sck[-1] = 0
                if k < 15:
                    hold(1)
                    mosi[-1] = rng.randrange(2)
                    hold(h - 1)
        else:
            hold(tcss)
            for k in range(16):
                sck[-1] = 0
                hold(1)
                mosi[-1] = rng.randrange(2)
                hold(h - 1)
                sck[-1] = 1
                if k < 15:
                    hold(h)
        hold(20)
        cs[-1] = 1
    hold(100)
    return cs, sck, mosi


def test_speed_limit_cs_to_first_sck_edge_sweep(capsys):
    """The first SCK edge of a frame comes tCSS after CS falls, while the CS fall is still being
    handled: the shortest tCSS the program follows without losing that edge, per mode and depth
    (SCK half period 20 cycles, every verdict still checked)."""
    rows = {}
    for mode in (0, 3):
        for depth in (1, 2):
            for tcss in range(10, 19):
                cs, sck, mosi = spi_wave_fixed(random.Random(400 + tcss), 10, 20, tcss, mode)
                a, md, words, verdicts = run_referee(cs, sck, mosi, depth=depth)
                check(a, oracle(cs, sck, mosi), verdicts, LATENCY * (2 * depth - 1))
                rows[(mode, depth, tcss)] = md.lost
    with capsys.disabled():
        print("\nSPI referee, CS fall -> first SCK edge (tCSS, cycles at 60 MHz): LOST events (10 frames)")
        print("  tCSS             " + " ".join(f"{t:4}" for t in range(10, 19)))
        for mode in (0, 3):
            for depth in (1, 2):
                print(f"  mode {mode}, depth {depth}  " + " ".join(f"{rows[(mode, depth, t)]:4}" for t in range(10, 19)))
    # the limits stated in spi_referee.s
    # (before `rel` moved ahead of `judge 5` in cs_fall, depth 1 lost every frame's first edge at a
    # legal tCSS of 15 cycles: found by the second review, 17 Nov)
    assert all(rows[(m, 1, t)] == 0 for m in (0, 3) for t in range(14, 19))
    assert all(rows[(m, 1, 13)] > 0 for m in (0, 3)), "the sweep must reach the limit"
    assert all(rows[(m, 2, t)] == 0 for m in (0, 3) for t in range(10, 19))


def _frame_with_first_sck_edge_one_cycle_after_cs(mode=0):
    """Legal frames, except that in the third one the first SCK edge comes one cycle after CS
    falls: the SCK edge is reported inside the CS fall's snapshot (ORD), never as an event of its
    own. Only flag A tells the program; without it, the frame's SECOND edge would be taken for the
    first and judged as tCSS against the CS fall: a verdict about the wrong pair of edges."""
    rng = random.Random(500)
    parts = [spi_wave_fixed(rng, 2, 20, 20, mode), spi_wave_fixed(rng, 1, 20, 1 if mode == 0 else 1, mode),
             spi_wave_fixed(rng, 2, 20, 20, mode)]
    cs, sck, mosi = [], [], []
    for c, k, m in parts:
        cs += c
        sck += k
        mosi += m
    return cs, sck, mosi


@pytest.mark.parametrize("mode", [0, 3])
def test_cs_and_first_sck_edge_one_cycle_apart_loses_track_soundly(mode):
    cs, sck, mosi = _frame_with_first_sck_edge_one_cycle_after_cs(mode)
    a, md, words, verdicts = run_referee(cs, sck, mosi)
    check(a, oracle(cs, sck, mosi), verdicts)
    assert any(v["poisoned"] for v in verdicts), "flag A must make the program lose track (desync)"


def test_negative_control_without_flag_a():
    cs, sck, mosi = _frame_with_first_sck_edge_one_cycle_after_cs(0)
    src = FW.read_text().replace("        ja   lost\n", "        nop\n")
    assert src != FW.read_text()
    a, md, words, verdicts = run_referee(cs, sck, mosi, src=src)
    with pytest.raises(AssertionError, match="wrong pair|the rule gives"):
        check(a, oracle(cs, sck, mosi), verdicts)


def test_spi_referee_fuzz_many_seeds():
    """Soundness only, on 60 random mixes (mode, spread 0-0.98, floor 2-6, depth 1 / 2)."""
    rng = random.Random(7)
    for _ in range(60):
        run_mix(rng.randrange(1 << 30), 12, rng.choice((0, 3)), rng.uniform(0, 0.98),
                rng.randrange(2, 7), rng.choice((1, 2)))


def test_negative_control_without_the_first_edge_state():
    """Without RC0 the frame's first SCK edge is judged as tCL / tCH against the previous frame's
    edge: a verdict about the wrong pair of edges, which the check must catch."""
    src = FW.read_text().replace("loop first", "nop       ")
    with pytest.raises(AssertionError, match="wrong pair"):
        run_mix(220, 30, 0, 0.0, src=src)


def test_negative_control_without_the_level_qualified_trigger():
    """Without K, SCK edges while CS is high (another target's frame) are events and get judged."""
    src = FW.read_text().replace("trig  CH0, any", "trig  CH0|CH1, any").replace(
        "trigq CH1, any, 0, 0", "nop                 ")
    with pytest.raises(AssertionError, match="wrong pair|the rule gives"):
        run_mix(221, 30, 0, 0.0, src=src)
