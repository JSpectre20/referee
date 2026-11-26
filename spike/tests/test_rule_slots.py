# SPDX-License-Identifier: Apache-2.0
"""Hardware rule slots (spike/rule_slots.py) against the same oracle and the same check() as the
firmware referee (tests/test_ref_i2c.py), on the same traffic.

Rules compared. The slots judge eight of the oracle's rules: tSU;DAT (0), tHD;DAT (1), tLOW (3),
tHIGH (4), tHD;STA (5), tSU;STA (6), tSU;STO (7), tBUF (8). The P + K firmware judges the same
minus tHD;DAT, so every comparison with it uses the P + K rule set; the figure with tHD;DAT is
printed beside it. (tVD;DAT, rule 2, is in neither: it needs to know what comes next.)
"""

import random
import sys
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import rule_slots as R  # noqa: E402
import test_ref_i2c as T  # noqa: E402
from test_ref_i2c import check, oracle, i2c_wave, pct, MIXES, GATE_CYCLES, MIN_LEVEL, RULES  # noqa: E402

SLOT_RULES = {0, 1, 3, 4, 5, 6, 7, 8}
PK_RULES = T.VARIANTS[T.PK]                      # the firmware's rule set: SLOT_RULES minus tHD;DAT
LEGAL_HOLDS = [(300, 800), (100, 200), (0, 0), (0, 33), (0, 800)]
N_SEEDS = 100
GATE = 0.05                                      # the gate: under 5% of violating intervals unjudged


def wave(seed, n, spread, hd, floor):
    return i2c_wave(random.Random(seed), n, spread=spread, hd_dat_ns=hd, floor=floor)


def run(sda, scl, slots=None, stale_old=R.STALE_OLD):
    """-> (RuleSlots, Result on SLOT_RULES, Result on PK_RULES, oracle events). check() raises on
    any verdict about the wrong pair of edges or with the wrong value."""
    a, rs = R.run_slots(sda, scl, slots, stale_old)
    exp = oracle(sda, scl)
    return rs, check(a, exp, rs.verdicts, SLOT_RULES), check(a, exp, rs.verdicts, PK_RULES), exp


@lru_cache(maxsize=None)
def run_cached(seed, n, spread, hd, floor):
    return run(*wave(seed, n, spread, hd, floor))


def report(title, rs, res, exp):
    un, ev = res.unjudged()
    why = R.unjudged_reasons(exp, rs, res.rules)
    print(f"\n{title}: rule slots, {len(rs.slots)} slots; {un} of {ev} events not judged ({pct(un, ev)}); "
          f"violating intervals >= {GATE_CYCLES} cycles not judged: {res.gate[1]} of {res.gate[0]} "
          f"({pct(res.gate[1], res.gate[0])}); intervals < {MIN_LEVEL} cycles (below the minimum level, a "
          f"physical limit): {res.limit[0]}, {res.limit[1]} of them not judged; most verdicts waiting for the "
          f"host {rs.max_outq}, most in one cycle {rs.max_per_cycle}")
    print(f"  {'rule':8} {'events':>7} {'judged':>7} {'unjudged':>9} {'PASS':>6} {'VIOL':>6} {'INDET':>6} {'flagged':>8}"
          "   why unjudged")
    for b, r in res.table.items():
        if b not in res.rules:
            print(f"  {RULES[b]:8} {r['events']:7}   (not judged by the slots)")
            continue
        w = ", ".join(f"{k} {v}" for k, v in sorted(why[b].items())) or "-"
        print(f"  {RULES[b]:8} {r['events']:7} {r['judged']:7} {r['events'] - r['judged']:9} {r['P']:6} {r['V']:6} "
              f"{r['I']:6} {r['flagged']:8}   {w}")
    return why


def total_reasons(acc, why):
    for b, d in why.items():
        for k, v in d.items():
            acc.setdefault(b, {}).setdefault(k, 0)
            acc[b][k] += v


# ------------------------------------------------------------------ soundness and coverage

def test_slot_definitions(capsys):
    with capsys.disabled():
        print("\nRule slots (corrected; stale_old on):")
        for sl in R.I2C_SLOTS:
            print(f"  {RULES[sl.rule]:8} {sl.describe()}")
    assert {sl.rule for sl in R.I2C_SLOTS} == SLOT_RULES


@pytest.mark.parametrize("hd", LEGAL_HOLDS)
def test_legal_traffic_no_violation_and_every_event_answered(hd, capsys):
    """Legal Fast-mode traffic, data hold from 0 ns: no VIOLATION, and every event is judged or
    flagged CAN'T TELL (the only flags allowed: tHD;DAT when SDA moves within a cycle of SCL's fall,
    and the first START after power-up, whose tBUF has no earlier edge)."""
    rs, res, _, exp = run_cached(31, 40, 0.0, hd, 6)
    with capsys.disabled():
        why = report(f"legal traffic, data hold {hd[0]}-{hd[1]} ns", rs, res, exp)
    for b in SLOT_RULES:
        r = res.table[b]
        assert r["V"] == 0, f"{RULES[b]}: a VIOLATION on legal traffic"
        if b not in (1, 8):
            assert r["judged"] == r["events"], f"{RULES[b]}: an event not judged"
    assert set(why[1]) <= {"flagged, ORD"}, "tHD;DAT: only an SDA edge within a cycle of SCL's fall may go unjudged"
    assert why[8] == {"flagged, cond: stale": 1}, "tBUF: only the first START after power-up"


def test_traffic_mixes_sound_and_under_the_gate(capsys):
    """Every MIX of test_ref_i2c: check() finds no verdict about the wrong pair of edges and no
    wrong verdict; the gate holds on every mix."""
    reasons, viol = {}, 0
    for name, seed, n, spread, hd, floor in MIXES:
        rs, res, res_pk, exp = run_cached(seed, n, spread, hd, floor)
        with capsys.disabled():
            total_reasons(reasons, report(name, rs, res, exp))
        viol += sum(r["V"] for r in res.table.values())
        assert res_pk.gate[1] <= GATE * res_pk.gate[0], name
    with capsys.disabled():
        print("\nWhy events were unjudged, all mixes (flagged = CAN'T TELL with a flag; not applied = the slot's "
              "condition was certainly false; no slot fired = no END matched):")
        for b in sorted(reasons):
            print(f"  {RULES[b]:8} " + (", ".join(f"{k} {v}" for k, v in sorted(reasons[b].items())) or "-"))
    assert viol > 50, "the traffic must contain violations that are caught"


def test_random_seeds_sound(capsys):
    """N_SEEDS more traffic seeds, spread drawn in 0-0.98, floor 2-6, data hold 0-0 / 0-33 / 0-100 /
    0-800 / 300-800 ns. check() raises on any unsound verdict."""
    rng = random.Random(2024)
    gate, gate_all, limit, viol, why = [0, 0], [0, 0], [0, 0], 0, {}
    for k in range(N_SEEDS):
        spread, floor = rng.uniform(0, 0.98), rng.randint(2, 6)
        hd = rng.choice([(0, 0), (0, 33), (0, 100), (0, 800), (300, 800)])
        rs, res, res_pk, exp = run(*wave(5000 + k, 12, spread, hd, floor))
        gate = [g + x for g, x in zip(gate, res_pk.gate)]
        gate_all = [g + x for g, x in zip(gate_all, res.gate)]
        limit = [g + x for g, x in zip(limit, res.limit)]
        viol += sum(r["V"] for r in res.table.values())
        total_reasons(why, R.unjudged_reasons(exp, rs, SLOT_RULES))
    with capsys.disabled():
        print(f"\n{N_SEEDS} random seeds (12 transfers each): sound; {viol} violations reported; GATE "
              f"(P + K rules): {gate[1]} of {gate[0]} = {pct(gate[1], gate[0])}; with tHD;DAT: {gate_all[1]} of "
              f"{gate_all[0]}; intervals < {MIN_LEVEL} cycles: {limit[0]}, {limit[1]} not judged")
        for b in sorted(why):
            print(f"  {RULES[b]:8} " + (", ".join(f"{k} {v}" for k, v in sorted(why[b].items())) or "-"))
    assert viol > 100
    assert gate[1] <= GATE * gate[0]


# ------------------------------------------------------------------ against the firmware

def test_comparison_with_firmware_snapshot_queue(capsys):
    """One block: mix x {firmware queue depth 1, 2, 4, rule slots}: violating intervals of at least
    5 cycles with no verdict (the gate), on the P + K rule set; the slots also with tHD;DAT."""
    rows, tot = [], {k: [0, 0] for k in ("d1", "d2", "d4", "slots", "slots+1")}
    for name, seed, n, spread, hd, floor in MIXES:
        row = {}
        for d in (1, 2, 4):
            _, _, res = T.run_mix(seed, n, spread, hd, T.PK, d, floor)
            row[f"d{d}"] = res.gate
        _, res_all, res_pk, _ = run_cached(seed, n, spread, hd, floor)
        row["slots"], row["slots+1"] = res_pk.gate, res_all.gate
        row["limit"] = res_pk.limit
        rows.append((name, row))
        for k in tot:
            tot[k] = [tot[k][0] + row[k][0], tot[k][1] + row[k][1]]

    def cell(g):
        return f"{g[1]} of {g[0]} ({pct(g[1], g[0])})"
    with capsys.disabled():
        print("\nGATE: violating min-bound intervals >= 5 cycles with no verdict (P + K rule set; data hold 0-800 ns)")
        print(f"  {'mix':62} {'fw depth 1':>20} {'fw depth 2':>18} {'fw depth 4':>16} {'rule slots':>16}"
              f" {'slots + tHD;DAT':>16}  intervals < 4 cycles")
        for name, row in rows + [("all mixes", dict(tot, limit=["", ""]))]:
            print(f"  {name:62} {cell(row['d1']):>20} {cell(row['d2']):>18} {cell(row['d4']):>16} "
                  f"{cell(row['slots']):>16} {cell(row['slots+1']):>16}  {row['limit'][0]}")
    assert tot["slots"][1] <= tot["d4"][1] + GATE * tot["slots"][0]
    assert tot["slots"][0] == tot["d1"][0], "the same traffic and rules: the same violating intervals"


def test_area_arithmetic(capsys):
    lines, nums = R.area_estimate()
    with capsys.disabled():
        print("\nArea of the rule slots (sg13 numbers measured on this design; the rest assumed):")
        for ln in lines:
            print("  " + ln)
    assert nums["low"] < nums["high"]
    assert nums[8][0] > nums["q4"], "8 slots cost more than a depth-4 snapshot queue"


# ------------------------------------------------------------------ each mechanism: the failing case without it

def directed(events, n=600):
    """A hand-made waveform: (cycle, 'sda' | 'scl', level) from that cycle on; both lines start high."""
    sda, scl = [1] * n, [1] * n
    for c, line, v in events:
        arr = sda if line == "sda" else scl
        arr[c:] = [v] * (n - c)
    return sda, scl


BYTE = [(100, "sda", 0), (140, "scl", 0), (180, "scl", 1), (220, "scl", 0), (260, "scl", 1)]
DIRECTED = {
    # START and STOP in one SCL high period, then SCL falls: the oracle calls it tHIGH (the bus is idle)
    "START, STOP, then SCL falls": BYTE + [(290, "sda", 1), (300, "sda", 0), (310, "sda", 1), (330, "scl", 0),
                                           (370, "scl", 1)],
    "START one cycle before SCL falls": BYTE + [(290, "sda", 1), (329, "sda", 0), (330, "scl", 0), (370, "scl", 1)],
    "STOP one cycle before SCL falls": BYTE + [(329, "sda", 1), (330, "scl", 0), (370, "scl", 1)],
    "repeated START, then a data rise with 0 ns hold": BYTE + [(300, "sda", 1), (330, "sda", 0), (360, "scl", 0),
                                                               (360, "sda", 1), (400, "scl", 1)],
}


@pytest.mark.parametrize("name", DIRECTED)
def test_directed_corner_cases_sound(name):
    run(*directed(DIRECTED[name]))


def sketch_with(rule, slot_from):
    return [next(s for s in slot_from if s.rule == rule) if sl.rule == rule else sl for sl in R.I2C_SLOTS]


def test_why_each_mechanism_is_there(capsys):
    """The sketch with each correction undone, one at a time."""
    out = []
    # 1. tHD;STA's qualifier "SDA low in r-1": without any qualifier, START + STOP in one SCL high
    #    period and then an SCL fall is judged as tHD;STA (a verdict about the wrong pair).
    with pytest.raises(AssertionError, match="wrong pair"):
        run(*directed(DIRECTED["START, STOP, then SCL falls"]), slots=sketch_with(5, R.SKETCH_SLOTS))
    out.append("tHD;STA without the SDA-low qualifier (the sketch): START + STOP + SCL fall -> wrong-pair verdict")
    #    ... and sampled in r instead of r-1, a 0 ns data hold after a START hides the START
    q_r = [replace(s, qual=(R.SDA, 0)) if s.rule == 5 else s for s in R.I2C_SLOTS]
    g = {}
    for lab, sl in (("r-1", None), ("r", q_r)):
        g[lab] = [0, 0, 0, 0]
        for seed, hd in ((41, (0, 0)), (42, (0, 33))):
            _, _, res_pk, _ = run(*wave(seed, 40, 0.6, hd, 6), slots=sl)
            g[lab] = [g[lab][0] + res_pk.gate[0], g[lab][1] + res_pk.gate[1],
                      g[lab][2] + res_pk.table[5]["events"], g[lab][3] + res_pk.table[5]["judged"]]
    out.append(f"tHD;STA qualifier sampled in r instead of r-1 (hold 0-33 ns, down to 40%): tHD;STA judged "
               f"{g['r'][3]} of {g['r'][2]} instead of {g['r-1'][3]}; gate {g['r'][1]} of {g['r'][0]} instead of "
               f"{g['r-1'][1]} (sound either way; r-1 costs no flop, it is the existing s3 level)")
    assert g["r"][3] < g["r-1"][3]
    # 2. tHIGH's LAST_PRE: with SDA's plain last edge, every 0-1 cycle data hold flags tHIGH
    g = {}
    for lab, sl in (("LAST_PRE", None), ("LAST (sketch)", sketch_with(4, R.SKETCH_SLOTS))):
        g[lab] = [0, 0]
        for seed, hd in ((41, (0, 0)), (42, (0, 33))):
            _, _, res_pk, _ = run(*wave(seed, 40, 0.6, hd, 6), slots=sl)
            g[lab] = [g[lab][0] + res_pk.gate[0], g[lab][1] + res_pk.gate[1]]
    out.append(f"tHIGH with SDA's plain last edge (the sketch), hold 0-33 ns: gate {g['LAST (sketch)'][1]} of "
               f"{g['LAST (sketch)'][0]} = {pct(*g['LAST (sketch)'][::-1])} unjudged (fails the 5% gate); with "
               f"LAST_PRE: {g['LAST_PRE'][1]}")
    assert g["LAST (sketch)"][1] > GATE * g["LAST (sketch)"][0] and g["LAST_PRE"][1] <= GATE * g["LAST_PRE"][0]
    # 3. stale_old: transfers separated by an idle bus longer than 16384 cycles (273 us)
    parts = []
    for k in range(8):
        sda, scl = wave(700 + k, 2, 0.7, (300, 800), 6)
        parts.append((sda + [1] * 20000, scl + [1] * 20000))
    sda = sum((p[0] for p in parts), [])
    scl = sum((p[1] for p in parts), [])
    hd_sta = {}
    for so in (False, True):
        _, res, _, _ = run(sda, scl, stale_old=so)
        hd_sta[so] = (res.table[5]["judged"], res.table[5]["events"])
    out.append(f"stale_old, 8 transfers each after 333 us of idle bus: tHD;STA judged {hd_sta[False][0]} of "
               f"{hd_sta[False][1]} without it, {hd_sta[True][0]} of {hd_sta[True][1]} with it")
    assert hd_sta[True][0] == hd_sta[True][1] and hd_sta[False][0] <= hd_sta[False][1] - 8
    with capsys.disabled():
        print("\nWhy each mechanism beyond the sketch is there:")
        for ln in out:
            print("  " + ln)


# ------------------------------------------------------------------ negative controls: check() must reject these

def test_negative_control_thd_sta_without_its_condition():
    """tHD;STA with no order condition fires on every SCL fall after any SDA fall: wrong pairs."""
    bad = [replace(s, cond=None) if s.rule == 5 else s for s in R.I2C_SLOTS]
    with pytest.raises(AssertionError, match="wrong pair"):
        run(*wave(31, 40, 0.0, (300, 800), 6), slots=bad)


def test_negative_control_no_receiver_rule_on_0_ns_hold():
    """The SDA-ended slots with SCL's level sampled in r-1, i.e. before an SCL fall in the same cycle
    as the SDA edge: a 0 ns data hold edge is then taken for a START or STOP."""
    bad = [replace(s, qual=(R.SCL, s.qual[1], R.QUAL_PRE)) if s.end[0] == R.SDA else s for s in R.I2C_SLOTS]
    with pytest.raises(AssertionError, match="wrong pair"):
        run(*wave(31, 40, 0.0, (0, 0), 6), slots=bad)
    run(*wave(31, 40, 0.0, (300, 800), 6), slots=bad)   # harmless when SDA never moves near SCL's fall


def test_negative_control_wrong_start_edge():
    """tSU;STO measured from SCL's fall instead of its rise: wrong interval values."""
    bad = [replace(s, start=(R.SCL, R.FALL)) if s.rule == 7 else s for s in R.I2C_SLOTS]
    with pytest.raises(AssertionError):
        run(*wave(31, 40, 0.0, (300, 800), 6), slots=bad)
