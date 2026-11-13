# SPDX-License-Identifier: Apache-2.0
"""The I2C Fast-mode referee (spike/firmware/i2c_fm_referee.s) against an independent oracle.

The test generates I2C traffic with every interval drawn at random around its bound, scans the
waveform itself (not the firmware's logic) to list which rule applies to which pair of edges and
what the true interval is, and then checks every verdict the model streams out:
  - it names a (rule, interval) pair the oracle also found, in the same order;
  - its PASS / VIOLATION / INDETERMINATE is what the rule gives for that interval.
Events the chip did not judge are counted, per rule: that is the coverage figure this spike is
after. Edges are on whole clock cycles here (the model's resolution), so a measured interval
equals the true one; sub-cycle soundness is the business of the RTL proofs, not of this test.
"""

import random
import re
import sys

import pytest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import asm_v1  # noqa: E402
from model_v1 import Model, verdict_rule, QUARTER  # noqa: E402

FW = HERE.parent / "firmware" / "i2c_fm_referee.s"
CLOCK = 60e6
NS = CLOCK / 1e9  # cycles per ns
RULES = {0: "tSU;DAT", 1: "tHD;DAT", 3: "tLOW", 4: "tHIGH", 5: "tHD;STA",
         6: "tSU;STA", 7: "tSU;STO", 8: "tBUF"}
SDA, SCL = 0, 1
LATENCY = 32  # cycles from the edge that ends an interval to its verdict leaving the FIFO (measured: under 30)


# ------------------------------------------------------------------ traffic generator

def i2c_wave(rng, n_transfers, spread, hd_dat_ns=(300, 800)):
    """-> (sda, scl): one level per clock cycle. `spread` scales how far each interval is drawn
    below its Fast-mode minimum (0: always legal; 0.5: down to half the minimum)."""
    sda, scl = [1], [1]

    def hold(n):
        for _ in range(max(1, int(n))):
            sda.append(sda[-1])
            scl.append(scl[-1])

    def t(min_ns):  # an interval around a minimum bound, in cycles (never under 6 cycles)
        return max(6, round(min_ns * rng.uniform(1 - spread, 1.6) * NS))

    def set_line(which, v):
        (sda if which == SDA else scl)[-1] = v

    hold(200)
    for _ in range(n_transfers):
        set_line(SDA, 0)                       # START
        hold(t(600))                           # tHD;STA
        bits = [rng.randrange(2) for _ in range(9 * rng.randrange(1, 3))]
        ends = ["stop"] if rng.random() < 0.6 else ["restart", "stop"]
        for seg, end in enumerate(ends):
            for b in (bits if seg == 0 else [rng.randrange(2) for _ in range(9)]) + [end]:
                set_line(SCL, 0)
                low = t(1300)
                hd = min(low - 8, max(6, round(rng.uniform(*hd_dat_ns) * NS)))  # SDA changes this long after SCL falls
                hold(hd)
                set_line(SDA, {"stop": 0, "restart": 1}.get(b, b))
                hold(low - hd)
                set_line(SCL, 1)
                if b == "stop":
                    hold(t(600))               # tSU;STO
                    set_line(SDA, 1)
                    hold(min(t(1300), 12000))  # tBUF
                elif b == "restart":
                    hold(t(600))               # tSU;STA
                    set_line(SDA, 0)
                    hold(t(600))               # tHD;STA
                else:
                    hold(t(600))               # tHIGH
    hold(100)
    return sda, scl


# ------------------------------------------------------------------ oracle (from the waveform alone)

def oracle(sda, scl):
    """-> [(rule, true interval in cycles or None when the earlier edge never happened, end cycle)]."""
    out = []
    last = {("sda", 0): None, ("sda", 1): None, ("scl", 0): None, ("scl", 1): None}  # (line, new level) -> cycle
    sda_any = None
    start_pending, idle = False, True

    def gap(now, then):
        return None if then is None else now - then

    for c in range(1, len(sda)):
        ds, dc = sda[c] != sda[c - 1], scl[c] != scl[c - 1]
        assert not (ds and dc), "generator: SDA and SCL never change in the same cycle"
        n0 = len(out)
        if dc and scl[c]:                                   # SCL rise
            out += [(3, gap(c, last[("scl", 0)])), (0, gap(c, sda_any))]
        elif dc:                                            # SCL fall
            if start_pending:
                out.append((5, gap(c, last[("sda", 0)])))
                start_pending = False
            else:
                out.append((4, gap(c, last[("scl", 1)])))
        elif ds and not scl[c]:                             # SDA changes while SCL is low
            out.append((1, gap(c, last[("scl", 0)])))        # (tVD;DAT is not judged: see the .s file)
        elif ds and not sda[c]:                             # START
            out.append((8, gap(c, last[("sda", 1)])) if idle else (6, gap(c, last[("scl", 1)])))
            start_pending, idle = True, False
        elif ds:                                            # STOP
            out.append((7, gap(c, last[("scl", 1)])))
            idle = True
        out[n0:] = [(b, m, c) for b, m in out[n0:]]          # + the cycle of the edge that ends the interval
        if ds:
            last[("sda", sda[c])] = sda_any = c
        if dc:
            last[("scl", scl[c])] = c
    return out


# ------------------------------------------------------------------ run the model

def run_referee(sda, scl):
    a = asm_v1.Assembler(CLOCK)
    src = re.sub(r"\bjudge (\d+)", r"judge \1, raw", FW.read_text())  # stream every verdict with its exact m
    words = a.assemble(src)
    img = a.image(words)
    md = Model(words=64)  # the Referee engine
    g = asm_v1.global_image({})
    for i, w in enumerate(g):
        md.write_global(i, w)
    for i, w in enumerate(img):
        md.write_engine(i, w)
    md.set_crc(0, asm_v1.crc16(g))
    md.set_crc(1, asm_v1.crc16(img))
    assert md.run()
    idle_in = (1 << (4 + SDA)) | (1 << (4 + SCL))
    for _ in range(300):  # RUN check, then the bus idles high
        md.step(idle_in, 0)
    assert md.state == "running"
    slots = []  # (slot, index of the wave cycle it was popped in)
    wave = list(zip(sda, scl)) + [(sda[-1], scl[-1])] * 60
    for k, (s, c) in enumerate(wave):
        md.step((s << (4 + SDA)) | (c << (4 + SCL)), 0)
        v = md.verdict_pop()
        if v is not None:
            slots.append((v, k))
    assert not md.sticky["VERDICT_OVF"]
    verdicts = []
    for (hdr, k), (raw, _) in zip(slots[0::2], slots[1::2]):
        assert hdr >> 8 & 1, "every header announces a raw slot"
        m = raw - 0x10000 if raw & 0x8000 else raw
        verdicts.append(((hdr >> 4) & 0xF, m, ["INDET", "PASS", "VIOL"][hdr >> 14], hdr, k))
    return a, md, len(words), verdicts


def check(a, expected, verdicts):
    """Every verdict must match the oracle, in order; -> per-rule [judged, expected, P, V, I]."""
    table = {b: [0, 0, 0, 0, 0] for b in RULES}
    for b, _, _ in expected:
        table[b][1] += 1
    i = 0
    for b, m, v, hdr, popped in verdicts:
        # The oracle entry must be the same rule, the same interval, AND end at most LATENCY
        # cycles before the verdict came out: a verdict about the wrong pair of edges cannot
        # match a later event of the same rule by coincidence.
        while i < len(expected) and not (expected[i][0] == b and (expected[i][1] is None or expected[i][1] == m)
                                         and 0 < popped - expected[i][2] <= LATENCY):
            i += 1  # an event the chip did not judge
        assert i < len(expected), (f"verdict (rule {RULES[b]}, m = {m}, out at cycle {popped}) "
                                   "matches nothing the oracle found")
        _, true, _ = expected[i]
        i += 1
        n_lo, width, is_max = a.bounds[b]
        n_lo -= (1 << 20) if n_lo & 0x80000 else 0
        stale = true is None or true >= QUARTER
        want = verdict_rule(m, n_lo, width, is_max, int(stale))
        assert v == want, f"rule {RULES[b]}, interval {true} cycles: chip says {v}, the rule gives {want} (hdr {hdr:#06x})"
        table[b][0] += 1
        table[b][2 + ["PASS", "VIOL", "INDET"].index(v)] += 1
    return table


def report(title, words, md, table):
    print(f"\n{title}: {words} words, LOST = {md.lost}")
    print(f"  {'rule':8} {'judged':>7} {'events':>7} {'PASS':>6} {'VIOL':>6} {'INDET':>6}")
    for b, (j, n, p, v, i) in table.items():
        print(f"  {RULES[b]:8} {j:7} {n:7} {p:6} {v:6} {i:6}")


def test_legal_fast_mode_traffic_every_event_judged_every_verdict_right(capsys):
    rng = random.Random(11)
    sda, scl = i2c_wave(rng, 40, spread=0.0)
    a, md, words, verdicts = run_referee(sda, scl)
    table = check(a, oracle(sda, scl), verdicts)
    with capsys.disabled():
        report("legal traffic (SDA changes 300-800 ns after SCL falls)", words, md, table)
    assert words <= 64, "the Referee engine holds 64 words"
    assert md.lost == 0
    for b, (judged, n, p, v, i) in table.items():
        assert judged == n, f"{RULES[b]}: {n - judged} events not judged"
        assert v == 0, f"{RULES[b]}: a VIOLATION on legal traffic"


@pytest.mark.xfail(strict=True, reason=(
    "FINDING of the spike, kept as a failing test until the program is redesigned: when an interval "
    "is short enough that its end event arrives while the previous event is still being handled, the "
    "trigger is lost, and the two state flags (RC0 'START pending', RC1 'idle') go out of step with "
    "the bus. The next SCL fall is then judged as tHD;STA against an SDA fall that was not a START: "
    "a verdict about the wrong pair of edges. State kept across events is not safe when events can be "
    "lost. See spike/README.md, 'Findings'."))
def test_traffic_with_violations_no_wrong_verdict_and_coverage_is_reported(capsys):
    rng = random.Random(12)
    sda, scl = i2c_wave(rng, 60, spread=0.5)
    a, md, words, verdicts = run_referee(sda, scl)
    table = check(a, oracle(sda, scl), verdicts)   # asserts every verdict given is right
    with capsys.disabled():
        report("traffic with intervals down to half their minimum", words, md, table)
    assert sum(t[3] for t in table.values()) > 20, "the traffic must contain violations that are caught"


def test_sda_changing_soon_after_scl_falls_is_a_coverage_gap_not_a_wrong_verdict(capsys):
    """A device that changes SDA 100-200 ns after SCL falls: the chip is still handling the SCL
    fall, so the data-change event is lost (counted in LOST). tHD;DAT / tVD;DAT lose coverage;
    no verdict is wrong; tSU;DAT is still judged at the next SCL rise from SDA's timestamp."""
    rng = random.Random(13)
    sda, scl = i2c_wave(rng, 30, spread=0.0, hd_dat_ns=(100, 200))
    a, md, words, verdicts = run_referee(sda, scl)
    table = check(a, oracle(sda, scl), verdicts)
    with capsys.disabled():
        report("SDA changes 100-200 ns after SCL falls", words, md, table)
    assert table[0][0] == table[0][1], "tSU;DAT is still judged for every SCL rise"
