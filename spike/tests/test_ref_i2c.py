# SPDX-License-Identifier: Apache-2.0
"""The I2C Fast-mode referees (spike/firmware/i2c_fm_referee*.s) against an independent oracle.

The test generates I2C traffic with every interval drawn at random around its bound, scans the
waveform itself (not the firmware's logic) to list which rule applies to which pair of edges and
what the true interval is, and then checks every verdict the model streams out:
  - a PASS or a VIOLATION must name a (rule, interval) pair the oracle also found, ending at most
    LATENCY cycles before the verdict came out, and must be what the rule gives for that interval;
  - a CAN'T TELL claims nothing. It is counted by its reason: the interval is within the
    measurement uncertainty of the bound (an ordinary, honest answer: "judged"), a timestamp was
    flagged (two edges too close to order, a stale edge: "flagged"), or the referee was poisoned.
Events with no verdict that claims something are "unjudged". That rate, per rule and per traffic
mix, is the coverage figure the spike is after, and the gate is on it.

Edges are on whole clock cycles here (the model's resolution), so a measured interval equals the
true one; sub-cycle soundness is the business of the RTL proofs, not of this test.
"""

import random
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import asm_v1  # noqa: E402
from model_v1 import Model, verdict_rule, QUARTER  # noqa: E402

FWDIR = HERE.parent / "firmware"
# the variants compared: the rules each one judges
VARIANTS = {"i2c_fm_referee_p.s": {0, 1, 3, 4, 5, 6, 7, 8},   # P only: every edge is an event
            "i2c_fm_referee.s": {0, 3, 4, 5, 6, 7, 8}}         # P + K: SCL edges, START, STOP
PK = "i2c_fm_referee.s"
CLOCK = 60e6
NS = CLOCK / 1e9  # cycles per ns
RULES = {0: "tSU;DAT", 1: "tHD;DAT", 3: "tLOW", 4: "tHIGH", 5: "tHD;STA",
         6: "tSU;STA", 7: "tSU;STO", 8: "tBUF"}
SDA, SCL = 0, 1
LATENCY = 32      # cycles from the edge that ends an interval to its verdict leaving the FIFO, queue empty
MIN_LEVEL = 4     # cycles: the proofs' minimum level length (ledger A1); shorter intervals cannot be judged
GATE_CYCLES = 5   # the gate: violating intervals of at least this many cycles


# ------------------------------------------------------------------ traffic generator

def i2c_wave(rng, n_transfers, spread, hd_dat_ns=(300, 800), floor=6):
    """-> (sda, scl): one level per clock cycle. `spread` scales how far each interval is drawn
    below its Fast-mode minimum (0: always legal; 0.5: down to half the minimum). `hd_dat_ns` is
    the range of the data hold time (SCL fall -> SDA change; 0 is legal in I2C). `floor`: no
    interval is shorter than this many cycles."""
    sda, scl = [1], [1]

    def hold(n):
        for _ in range(int(n)):
            sda.append(sda[-1])
            scl.append(scl[-1])

    def t(min_ns):  # an interval around a minimum bound, in cycles
        return max(floor, round(min_ns * rng.uniform(1 - spread, 1.6) * NS))

    def set_line(which, v):
        (sda if which == SDA else scl)[-1] = v

    hold(200)
    for _ in range(n_transfers):
        hold(1)
        set_line(SDA, 0)                       # START
        hold(t(600))                           # tHD;STA
        bits = [rng.randrange(2) for _ in range(9 * rng.randrange(1, 3))]
        ends = ["stop"] if rng.random() < 0.6 else ["restart", "stop"]
        for seg, end in enumerate(ends):
            for b in (bits if seg == 0 else [rng.randrange(2) for _ in range(9)]) + [end]:
                set_line(SCL, 0)
                low = t(1300)
                hd = min(low - floor, round(rng.uniform(*hd_dat_ns) * NS))  # 0: in the same cycle as SCL's fall
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
    """-> [(rule, true interval in cycles or None when the earlier edge never happened, end cycle)].

    An SDA change in the same cycle as SCL's fall is a data change after the fall (hold time 0),
    the way an I2C receiver reads it: UM10204 requires every device to hold SDA internally for at
    least 300 ns "to bridge the undefined region of the falling edge of SCL" (note to tHD;DAT in
    the bus-line characteristics table), so an SDA edge next to SCL's fall is never a START or STOP."""
    out = []
    last = {("sda", 0): None, ("sda", 1): None, ("scl", 0): None, ("scl", 1): None}  # (line, new level) -> cycle
    sda_any = None
    start_pending, idle = False, True

    def gap(now, then):
        return None if then is None else now - then

    for c in range(1, len(sda)):
        ds, dc = sda[c] != sda[c - 1], scl[c] != scl[c - 1]
        assert not (ds and dc and scl[c]), "generator: SDA never changes in the cycle SCL rises"
        n0 = len(out)
        if dc and scl[c]:                                   # SCL rise
            out += [(3, gap(c, last[("scl", 0)])), (0, gap(c, sda_any))]
        elif dc:                                            # SCL fall
            if start_pending:
                out.append((5, gap(c, last[("sda", 0)])))
                start_pending = False
            else:
                out.append((4, gap(c, last[("scl", 1)])))
            last[("scl", 0)] = c
        if ds and not scl[c]:                               # SDA changes while SCL is low (or as it falls)
            out.append((1, gap(c, last[("scl", 0)])))        # (tVD;DAT is not judged: see the .s files)
        elif ds and not sda[c]:                             # START
            out.append((8, gap(c, last[("sda", 1)])) if idle else (6, gap(c, last[("scl", 1)])))
            start_pending, idle = True, False
        elif ds:                                            # STOP
            out.append((7, gap(c, last[("scl", 1)])))
            idle, start_pending = True, False
        out[n0:] = [(b, m, c) for b, m in out[n0:]]          # + the cycle of the edge that ends the interval
        if ds:
            last[("sda", sda[c])] = sda_any = c
        if dc:
            last[("scl", scl[c])] = c
    return out


# ------------------------------------------------------------------ run the model

def run_referee(sda, scl, fw=PK, depth=1):
    a = asm_v1.Assembler(CLOCK)
    src = re.sub(r"\bjudge (\d+)", r"judge \1, raw", (FWDIR / fw).read_text())  # stream every verdict with its exact m
    words = a.assemble(src)
    img = a.image(words)
    md = Model(words=64, depth=depth)  # the Referee engine
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
    wave = list(zip(sda, scl)) + [(sda[-1], scl[-1])] * 200
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
        verdicts.append({"b": (hdr >> 4) & 0xF, "m": m, "v": ["INDET", "PASS", "VIOL"][hdr >> 14], "out": k,
                         "poisoned": hdr >> 10 & 1, "flagged": bool(hdr & 0x3808)})  # FX | FY | TAG | ORD
    return a, md, len(words), verdicts


class Result:
    def __init__(self, rules):
        # per rule: events, judged, PASS, VIOL, INDET (near the bound), flagged, poisoned JUDGEs
        self.table = {b: dict(events=0, judged=0, P=0, V=0, I=0, flagged=0, poisoned=0) for b in RULES}
        self.rules = rules
        self.gate = [0, 0]   # violating events of >= GATE_CYCLES cycles (of the rules judged): total, unjudged
        self.limit = [0, 0]  # events with an interval under MIN_LEVEL cycles: total, unjudged

    def unjudged(self):
        ev = sum(self.table[b]["events"] for b in self.rules)
        return ev - sum(self.table[b]["judged"] for b in self.rules), ev


def check(a, expected, verdicts, rules, latency=LATENCY):
    """Soundness: every PASS / VIOLATION, and every CAN'T TELL that is neither poisoned nor
    flagged, must match the oracle in order. Returns the coverage Result."""
    res = Result(rules)
    bounds = {}
    for b in RULES:
        n_lo, width, is_max = a.bounds[b]
        bounds[b] = (n_lo - ((1 << 20) if n_lo & 0x80000 else 0), width, is_max)
    for b, _, _ in expected:
        res.table[b]["events"] += 1
    matched = [False] * len(expected)
    i = 0
    for vd in verdicts:
        b, m, v = vd["b"], vd["m"], vd["v"]
        row = res.table[b]
        if vd["poisoned"] or (vd["flagged"] and v == "INDET"):
            # It claims nothing, and it may be about a pair of edges no rule applies to: that is
            # exactly what the poison and the flags are for.
            assert v == "INDET", f"a poisoned JUDGE answered {v} (rule {RULES[b]}, m = {m})"
            row["poisoned" if vd["poisoned"] else "flagged"] += 1
            continue
        # The oracle entry must be the same rule, the same interval, AND end at most `latency`
        # cycles before the verdict came out: a verdict about the wrong pair of edges cannot
        # match a later event of the same rule by coincidence.
        while i < len(expected) and not (expected[i][0] == b and (expected[i][1] is None or expected[i][1] == m)
                                         and 0 < vd["out"] - expected[i][2] <= latency):
            i += 1  # an event the chip did not judge
        assert i < len(expected), (f"verdict {v} (rule {RULES[b]}, m = {m}, out at cycle {vd['out']}) "
                                   "matches nothing the oracle found: a verdict about the wrong pair of edges")
        true = expected[i][1]
        matched[i] = True
        i += 1
        stale = true is None or true >= QUARTER
        want = verdict_rule(m, *bounds[b], int(stale))
        assert v == want, f"rule {RULES[b]}, interval {true} cycles: chip says {v}, the rule gives {want}"
        row["judged"] += 1
        row[{"PASS": "P", "VIOL": "V", "INDET": "I"}[v]] += 1
    for (b, true, _), hit in zip(expected, matched):
        if b not in rules or true is None:
            continue
        n_lo, _, is_max = bounds[b]
        if true < MIN_LEVEL:
            res.limit[0] += 1
            res.limit[1] += not hit
        elif true >= GATE_CYCLES and not is_max and true * 16 < n_lo:
            res.gate[0] += 1
            res.gate[1] += not hit
    return res


def pct(part, whole):
    return f"{100 * part / whole:.1f}%" if whole else "-"


def report(title, words, md, res):
    un, ev = res.unjudged()
    print(f"\n{title}: {words} words, queue depth {md.depth}, LOST counter = {md.lost}"
          f"{' (saturated)' if md.lost == 255 else ''}; {un} of {ev} events not judged ({pct(un, ev)}); "
          f"violating intervals >= {GATE_CYCLES} cycles not judged: {res.gate[1]} of {res.gate[0]} ({pct(res.gate[1], res.gate[0])}); "
          f"intervals < {MIN_LEVEL} cycles (below the minimum level, a physical limit): {res.limit[0]}")
    print(f"  {'rule':8} {'events':>7} {'judged':>7} {'unjudged':>9} {'PASS':>6} {'VIOL':>6} {'INDET':>6} {'flagged':>8} {'poisoned':>9}")
    for b, r in res.table.items():
        mark = "" if b in res.rules else "   (not judged by this variant)"
        print(f"  {RULES[b]:8} {r['events']:7} {r['judged']:7} {r['events'] - r['judged']:9} {r['P']:6} {r['V']:6} "
              f"{r['I']:6} {r['flagged']:8} {r['poisoned']:9}{mark}")


def run_mix(seed, n, spread, hd, fw=PK, depth=1, floor=6):
    rng = random.Random(seed)
    sda, scl = i2c_wave(rng, n, spread=spread, hd_dat_ns=hd, floor=floor)
    a, md, words, verdicts = run_referee(sda, scl, fw, depth)
    # a queued event waits for the ones before it: allow for that in the latency window
    res = check(a, oracle(sda, scl), verdicts, VARIANTS[fw], latency=LATENCY * (2 * depth - 1))
    return md, words, res


# the traffic mixes with violations: (name, seed, transfers, spread, data hold range in ns, shortest interval in cycles)
MIXES = [("intervals down to 70% of their minimum", 21, 60, 0.3, (0, 800), 6),
         ("down to 50%", 12, 60, 0.5, (0, 800), 6),
         ("down to 30%", 22, 60, 0.7, (0, 800), 6),
         ("down to 10%", 23, 60, 0.9, (0, 800), 5),
         ("down to 2% (many intervals under the 4-cycle minimum level)", 24, 60, 0.98, (0, 800), 2)]


# ------------------------------------------------------------------ tests

@pytest.mark.parametrize("fw", VARIANTS)
def test_legal_fast_mode_traffic_every_event_judged_every_verdict_right(fw, capsys):
    md, words, res = run_mix(11, 40, 0.0, (300, 800), fw)
    with capsys.disabled():
        report(f"{fw}: legal traffic (SDA changes 300-800 ns after SCL falls)", words, md, res)
    assert words <= 64, "the Referee engine holds 64 words"
    assert md.lost == 0
    for b, r in res.table.items():
        if b not in VARIANTS[fw]:
            assert r["judged"] == 0
            continue
        # the first START after power-up is judged while poisoned (RUN starts poisoned)
        assert r["judged"] == r["events"] - (1 if b == 8 else 0), f"{RULES[b]}: events not judged"
        assert r["V"] == 0, f"{RULES[b]}: a VIOLATION on legal traffic"


@pytest.mark.parametrize("hd", [(0, 0), (0, 33), (0, 100), (0, 800)])
def test_legal_traffic_with_data_hold_from_0_ns_never_poisons(hd, capsys):
    """I2C allows a data hold time of 0 ns: SDA may change as SCL falls. To the chip that is an
    SDA edge and an SCL edge too close to order. The program follows the I2C receiver rule
    (UM10204: devices hold SDA internally for 300 ns to bridge SCL's falling edge), so it keeps
    tracking the protocol; the verdict that uses the ambiguous edge may be CAN'T TELL (flagged),
    but nothing after it is poisoned and no legal traffic is called a VIOLATION."""
    md, words, res = run_mix(31, 40, 0.0, hd)
    with capsys.disabled():
        report(f"{PK}: legal traffic, data hold {hd[0]}-{hd[1]} ns", words, md, res)
    assert md.lost == 0
    # RUN starts poisoned, so exactly one JUDGE is poisoned: the first START's (the program takes
    # it for a repeated START; the oracle knows the bus was idle, so one tBUF event has no answer)
    assert sum(r["poisoned"] for r in res.table.values()) == 1
    for b in VARIANTS[PK]:
        r = res.table[b]
        assert r["V"] == 0, f"{RULES[b]}: a VIOLATION on legal traffic"
        assert r["judged"] + r["flagged"] == r["events"] - (1 if b == 8 else 0), f"{RULES[b]}: an event got no answer"
    # the only rule that can be CAN'T TELL here is the one that uses an SCL fall next to an SDA edge
    assert all(res.table[b]["flagged"] == 0 for b in (0, 3, 6, 7, 8))


@pytest.mark.parametrize("fw", VARIANTS)
def test_traffic_with_violations_never_a_verdict_about_the_wrong_pair_of_edges(fw, capsys):
    """Until 13 Nov this was the spike's known failure: a lost event left the program's state wrong
    and the next SCL fall was judged as tHD;STA on an SDA fall that was not a START. With the
    poison rule, check() finds no PASS or VIOLATION about the wrong pair of edges, for either
    variant, on every mix (data hold from 0 ns, intervals down to 2% of their minimum)."""
    viol = lost = 0
    for name, seed, n, spread, hd, floor in MIXES:
        md, words, res = run_mix(seed, n, spread, hd, fw, floor=floor)
        with capsys.disabled():
            report(f"{fw}: {name}", words, md, res)
        viol += sum(r["V"] for r in res.table.values())
        lost += md.lost
    assert viol > 50, "the traffic must contain violations that are caught"
    assert lost > 0, "the traffic must make the chip lose events (or the test shows nothing)"


def test_data_edges_soon_after_scl_falls_cost_coverage_only_when_they_are_events(capsys):
    """A device that changes SDA 100-200 ns after SCL falls (many do). With every edge as an event
    (P only) the data edge arrives while the SCL fall is being handled, is lost, and poisons the
    rest of the transfer. With START / STOP / SCL as the only events (P + K) nothing is lost."""
    cover = {}
    for fw in VARIANTS:
        md, words, res = run_mix(13, 30, 0.0, (100, 200), fw)
        with capsys.disabled():
            report(f"{fw}: legal traffic, SDA changes 100-200 ns after SCL falls", words, md, res)
        cover[fw] = (res.table[0]["judged"], res.table[0]["events"], md.lost)
    assert cover[PK][0] == cover[PK][1] and cover[PK][2] == 0
    assert cover["i2c_fm_referee_p.s"][0] < cover["i2c_fm_referee_p.s"][1] // 2


def test_snapshot_queue_depth_against_the_gate(capsys):
    """Candidate Q: a queue of snapshots, so an event that arrives while the previous one is being
    handled waits instead of being lost. Measured against the gate: violating intervals of at least
    5 cycles that get no verdict, per mix and in total, for depth 1 (today), 2 and 4."""
    totals = {}
    for depth in (1, 2, 4):
        tot = [0, 0]
        for name, seed, n, spread, hd, floor in MIXES:
            md, words, res = run_mix(seed, n, spread, hd, PK, depth, floor)
            with capsys.disabled():
                report(f"{PK}, depth {depth}: {name}", words, md, res)
            tot[0] += res.gate[0]
            tot[1] += res.gate[1]
        totals[depth] = tot
    with capsys.disabled():
        print("\nGATE (violating intervals of >= 5 cycles with no verdict, all mixes): "
              + "; ".join(f"depth {d}: {t[1]} of {t[0]} = {pct(t[1], t[0])}" for d, t in totals.items()))
    assert totals[2][1] <= totals[1][1] and totals[4][1] <= totals[2][1], "a deeper queue must not lose more"
