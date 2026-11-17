# SPDX-License-Identifier: Apache-2.0
"""Hardware rule slots: a stateless alternative to the firmware referee, on the model only.

N independent slots sit directly on the edge-timestamp unit (model_v1.EdgeTs). There is no
firmware, no snapshot register and no snapshot queue: every slot looks at every edge, in parallel,
so no event is ever lost. A slot is configuration only:

  END    the edge that ends the interval: a channel and rise / fall / any, with an optional level
         qualifier (another channel at a given level), sampled in the cycle r the end edge is
         reported, exactly like `trigq` (candidate K), or in r-1 (QUAL_PRE, see below).
  START  the earlier edge: a channel and rise / fall / last (whichever was latched later), or
         LAST_PRE (see below).
  COND   optional order condition "edge A came after / before edge B", three-valued from the two
         timestamps with the same 2-cycle uncertainty as everything else: certainly after if
         tsA - tsB >= 2, certainly before if <= -2, otherwise unknown.
  BOUND  an index into the same bounds table the firmware uses (n_lo, width, is_max).

Timing mirrors model_v1.Model.step exactly: an END edge reported in cycle r is evaluated in cycle
r+2 from the timestamps as latched at the end of cycle r+1; any OTHER edge latched in r-1, r, r+1
is order-ambiguous (ORD) with it. The END timestamp is r itself (now - 2).

Evaluation of one slot whose END fires in cycle r:
  1. COND: a timestamp it uses is stale, its channel missed an edge, or it is ORD -> INDETERMINATE
     (flagged); the order is unknown (|tsA - tsB| < 2) -> INDETERMINATE (flagged); certainly false
     -> no verdict (the slot does not apply).
  2. m = ts(end) - ts(start); invalid if the start timestamp is stale / ORD or a channel used
     missed an edge -> model_v1.verdict_rule(m, bound, invalid), flagged when invalid.

The I2C receiver rule (UM10204: devices bridge the undefined region of SCL's falling edge, so an
SDA edge in the cycle SCL falls is a data change, never START / STOP; the oracle in
tests/test_ref_i2c.py reads the waveform the same way):
  - for slots ENDED by an SDA edge it needs nothing: the qualifier sampled in cycle r sees the
    levels AFTER that cycle's edges (as `trigq` does), so an SDA edge in the cycle SCL falls sees
    SCL low and is a data edge. Sampling it in r-1 instead is the negative control.
  - for slots ended by SCL's FALL, an SDA edge reported in r or r+1 is data that belongs to the
    next bit, not to the SCL high period being closed. Two small mechanisms (each kept only
    because a test fails without it; see tests/test_rule_slots.py):
      LAST_PRE  "SDA's last edge reported before cycle r": its polarity is SDA's level in r-1
                (one shared delay flop per channel), its timestamp the latched one of that
                polarity. An SDA edge in r-1 is still ORD (flagged): the oracle calls an SDA edge
                one cycle BEFORE SCL's fall a START / STOP, so it must not be skipped.
      QUAL_PRE  the qualifier sampled in r-1 (the same shared delay flop): "SDA was low before the
                SCL fall's cycle", so a 0 ns data hold edge does not hide a START.
Option `stale_old`: in COND only, a fresh timestamp is certainly after a stale one (stale = at
least 16384 cycles old, or never seen since reset; the stale bit is sticky, so this is exact).
Hardware: one XOR of the two stale bits per condition. It fixes the "long idle bus" weak spot:
after more than 273 us of idle bus the SCL rise is stale, and tHD;STA's condition "SDA's fall
after SCL's rise" would otherwise be unknown for every first START.
"""

import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import asm_v1  # noqa: E402
from model_v1 import EdgeTs, M16, s16, verdict_rule  # noqa: E402

SDA, SCL = 0, 1
RISE, FALL, ANY, LAST, LAST_PRE = 0, 1, 2, 3, 4   # RISE / FALL index EdgeTs.ts[ch]
AFTER, BEFORE = "after", "before"
QUAL_R, QUAL_PRE = "r", "r-1"           # the cycle the level qualifier is sampled in
U = 2                                   # cycles: the order uncertainty
BOUNDS_SRC = HERE / "firmware" / "i2c_fm_referee_p.s"   # the one file that declares all nine bounds
KIND = {RISE: "rise", FALL: "fall", ANY: "any", LAST: "last", LAST_PRE: "last before r"}
CH = {SDA: "SDA", SCL: "SCL"}


@dataclass(frozen=True)
class Slot:
    rule: int                      # bound index (= the oracle's rule number)
    end: tuple                     # (ch, RISE | FALL | ANY)
    start: tuple                   # (ch, RISE | FALL | LAST | LAST_PRE)
    qual: tuple = None             # (ch, level) or (ch, level, QUAL_R | QUAL_PRE)
    cond: tuple = None             # ((ch, sel), AFTER | BEFORE, (ch, sel))

    def describe(self):
        def e(x):
            return f"{CH[x[0]]} {KIND[x[1]]}"
        s = f"end {e(self.end)}"
        if self.qual:
            when = self.qual[2] if len(self.qual) > 2 else QUAL_R
            s += f" while {CH[self.qual[0]]} {'high' if self.qual[1] else 'low'}"
            s += " (in r-1)" if when == QUAL_PRE else ""
        s += f"; start {e(self.start)}"
        if self.cond:
            s += f"; only if {e(self.cond[0])} {self.cond[1]} {e(self.cond[2])}"
        return s


# The task's sketch. The list order is the fixed output priority within one cycle (the order the
# oracle lists the events of one cycle: SCL rise -> tLOW, tSU;DAT; SCL fall -> tHIGH / tHD;STA;
# then the SDA edge).
SKETCH_SLOTS = [
    Slot(3, end=(SCL, RISE), start=(SCL, FALL)),                                         # tLOW
    Slot(0, end=(SCL, RISE), start=(SDA, LAST)),                                         # tSU;DAT
    Slot(4, end=(SCL, FALL), start=(SCL, RISE), cond=((SDA, LAST), BEFORE, (SCL, RISE))),  # tHIGH
    Slot(5, end=(SCL, FALL), start=(SDA, FALL), cond=((SDA, FALL), AFTER, (SCL, RISE))),   # tHD;STA
    Slot(1, end=(SDA, ANY), qual=(SCL, 0), start=(SCL, FALL)),                           # tHD;DAT
    Slot(8, end=(SDA, FALL), qual=(SCL, 1), start=(SDA, RISE),
         cond=((SDA, RISE), AFTER, (SCL, RISE))),                                        # tBUF
    Slot(6, end=(SDA, FALL), qual=(SCL, 1), start=(SCL, RISE),
         cond=((SDA, RISE), BEFORE, (SCL, RISE))),                                       # tSU;STA
    Slot(7, end=(SDA, RISE), qual=(SCL, 1), start=(SCL, RISE)),                          # tSU;STO
]

# The corrected slots: the sketch with two changes, each forced by a failing test.
I2C_SLOTS = [
    SKETCH_SLOTS[0], SKETCH_SLOTS[1],
    Slot(4, end=(SCL, FALL), start=(SCL, RISE), cond=((SDA, LAST_PRE), BEFORE, (SCL, RISE))),  # tHIGH
    Slot(5, end=(SCL, FALL), qual=(SDA, 0, QUAL_PRE), start=(SDA, FALL),
         cond=((SDA, FALL), AFTER, (SCL, RISE))),                                              # tHD;STA
] + SKETCH_SLOTS[4:]
STALE_OLD = True   # the corrected design also uses the stale_old option


def bounds_assembler(clock_hz=60e6):
    """An Assembler holding the bounds table of i2c_fm_referee_p.s (its `.bound` lines only), so
    test_ref_i2c.check() reads exactly the bounds the firmware uses."""
    a = asm_v1.Assembler(clock_hz)
    lines = [ln for ln in BOUNDS_SRC.read_text().splitlines() if ln.lstrip().startswith(".bound")]
    assert a.assemble("\n".join(lines)) == []
    return a


def signed_bounds(a):
    return {b: (n_lo - ((1 << 20) if n_lo & 0x80000 else 0), width, is_max)
            for b, (n_lo, width, is_max) in enumerate(a.bounds)}


class RuleSlots:
    """The slots on one EdgeTs; `step(raw, tag)` is one clock cycle (raw = the 4 channel levels,
    tag = the wave cycle index, or None during the idle lead-in)."""

    def __init__(self, slots, bounds, stale_old=STALE_OLD):
        self.slots, self.bounds, self.stale_old = slots, bounds, stale_old
        self.ed = EdgeTs()
        self.now = 0
        self.hist = deque(maxlen=3)   # per cycle: (pulses, level s2, tag); [-1] = the cycle before this one
        self.outq = deque()           # verdicts waiting for the host (1 verdict = 2 words, 1 word per cycle)
        self.next_out = 0
        self.max_outq = 0             # the most verdicts waiting at once
        self.max_per_cycle = 0        # the most verdicts produced in one cycle
        self.verdicts = []            # in test_ref_i2c.run_referee's form
        self.log = []                 # (rule, wave cycle of the end edge, outcome, reason)

    def step(self, raw, tag=None):
        ed = self.ed
        pulses = ed.pulses()
        n0 = len(self.outq)
        if len(self.hist) == 3 and any(any(p) for p in self.hist[-2][0]):
            self._evaluate()
        self.max_per_cycle = max(self.max_per_cycle, len(self.outq) - n0)
        self.max_outq = max(self.max_outq, len(self.outq))
        if self.outq and tag is not None and tag >= self.next_out:   # the host pops header + raw word
            v = self.outq.popleft()
            v["out"] = tag
            self.verdicts.append(v)
            self.next_out = tag + 2
        self.hist.append((pulses, list(ed.s2), tag))
        ed.commit(self.now, pulses, raw, False)
        self.now = (self.now + 1) & M16

    def _evaluate(self):
        """Cycle r+2 for the edges reported in cycle r; ed holds what was latched at the end of r+1."""
        ed = self.ed
        (p_rm1, lv_rm1, _), (p_r, lv_r, end_tag), (p_r1, _, _) = self.hist
        lv = {QUAL_R: lv_r, QUAL_PRE: lv_rm1}
        ts_end = (self.now - 2) & M16
        for sl in self.slots:
            ech, ekind = sl.end
            pols = [p for p in (RISE, FALL) if ekind in (ANY, p) and p_r[ech][p]]
            if not pols:
                continue
            if sl.qual is not None:
                qch, qlv = sl.qual[:2]
                if lv[sl.qual[2] if len(sl.qual) > 2 else QUAL_R][qch] != qlv:
                    continue
            end = (ech, pols[0])

            def info(ch, sel):
                pol = {LAST: ed.last_fall[ch], LAST_PRE: 1 - lv_rm1[ch]}.get(sel, sel)
                ordb = p_r1[ch][pol] or p_rm1[ch][pol] or (p_r[ch][pol] and (ch, pol) != end)
                return dict(ts=ed.ts[ch][pol], stale=ed.stale[ch][pol], missed=ed.missed[ch], ord=bool(ordb))

            def out(outcome, reason, v=None, m=0):
                self.log.append((sl.rule, None if end_tag is None else end_tag - 2, outcome, reason))
                if v is not None and end_tag is not None:
                    self.outq.append({"b": sl.rule, "m": m, "v": v, "poisoned": 0, "flagged": outcome == "flagged"})

            if sl.cond is not None:
                A, B = info(*sl.cond[0]), info(*sl.cond[2])
                bad = next((k for k in ("missed", "ord") if A[k] or B[k]), None)
                after = None
                if bad is None:
                    if A["stale"] or B["stale"]:
                        if self.stale_old and A["stale"] != B["stale"]:
                            after = bool(B["stale"])  # the fresh one is certainly the later one
                        else:
                            bad = "stale"
                    else:
                        d = s16(A["ts"] - B["ts"])
                        after = True if d >= U else (False if d <= -U else None)
                if bad is None and after is None:
                    bad = "cond unknown"
                if bad is not None:
                    out("flagged", "cond: " + {"ord": "ORD"}.get(bad, bad), "INDET")
                    continue
                if after != (sl.cond[1] == AFTER):
                    out("n/a", "cond false")
                    continue
            S = info(*sl.start)
            m = s16(ts_end - S["ts"])
            bad = next((k for k in ("missed", "ord", "stale") if S[k]), None)
            if bad is None and ed.missed[ech]:
                bad = "missed"
            v = verdict_rule(m, *self.bounds[sl.rule], int(bad is not None))
            if bad is not None:
                out("flagged", {"ord": "ORD"}.get(bad, bad), v, m)
            else:
                out("judged", v, v, m)


def run_slots(sda, scl, slots=None, stale_old=STALE_OLD, idle=300, tail=200, a=None):
    """-> (assembler with the bounds, RuleSlots). The wave is preceded by `idle` cycles of an idle
    bus (as run_referee does after RUN) and followed by `tail`; `out` is the wave cycle index."""
    a = a or bounds_assembler()
    rs = RuleSlots(I2C_SLOTS if slots is None else slots, signed_bounds(a), stale_old)
    for _ in range(idle):
        rs.step([1, 1, 0, 0])
    wave = list(zip(sda, scl)) + [(sda[-1], scl[-1])] * tail
    for k, (s, c) in enumerate(wave):
        rs.step([s, c, 0, 0], k)
    assert not rs.outq, "verdicts left in the output queue"
    return a, rs


def unjudged_reasons(expected, rs, rules):
    """-> {rule: {reason: count}} for the oracle events that got no unflagged slot verdict, from
    the slot log (keyed by rule and the wave cycle of the end edge)."""
    by = {}
    for b, c, outcome, reason in rs.log:
        by.setdefault((b, c), []).append((outcome, reason))
    out = {b: {} for b in rules}
    for b, _, c in expected:
        if b not in rules:
            continue
        ent = by.get((b, c), [])
        if any(o == "judged" for o, _ in ent):
            continue
        why = "no slot fired" if not ent else (("flagged, " if ent[0][0] == "flagged" else "not applied, ")
                                               + ent[0][1])
        out[b][why] = out[b].get(why, 0) + 1
    return out


# ------------------------------------------------------------------ area estimate

CORE_UM2 = 1_208_170
FLOP_UM2 = 155_197 / 3_168       # measured average flop, routed (this design, sg13 synthesis)
VERDICT_UM2 = 1_600              # the whole three-valued verdict block (measured)
SNAPSHOT_UM2 = 13_800            # one snapshot register set (measured)
STATS4_UM2 = 36_200              # four stats slots (measured; slots need none of it)
BOUNDS_TABLE_UM2 = 45_900        # 16-entry bounds table (measured)
QUEUE_ENTRY_UM2 = 14_000         # one extra snapshot-queue entry (README finding 11)
# configuration bits per slot (all assumed held in flops, written by the host)
CONFIG_BITS = {"end ch 2 + kind 2": 4, "qualifier en 1 + ch 2 + level 1 + in r / r-1 1": 5,
               "start ch 2 + sel 2 (rise / fall / last / last before r)": 4,
               "cond en 1 + after/before 1 + A 4 + B 4": 10, "bound index 4": 4, "enable 1": 1}
SHARED_FLOPS = 8                 # each channel's level delayed 2 more cycles (LAST_PRE's polarity at r+2)
# assumptions (not measured), low / high, um2
SUB16 = (500, 900)               # one 16-bit subtractor
MUX8x16 = (1_000, 2_000)         # one 8:1 x 16-bit timestamp mux (2 channels x rise / fall + spare)
GLUE = (300, 800)                # flag muxes (stale / missed / ORD), the +-2 compare, control


def area_estimate():
    """-> (lines, numbers): the arithmetic, printed by the test."""
    cfg = sum(CONFIG_BITS.values())
    pipe = 2                     # "the END fired" in r, r+1 (the qualifier gates it in r: s2, or s3 for r-1)
    outreg = 2 + 16 + 1 + 1      # output register: verdict 2, m 16, flagged 1, valid 1
    flops = cfg + pipe + outreg
    bound_copy = 40 * FLOP_UM2                             # low: the slot keeps its own bound word
    bound_port = BOUNDS_TABLE_UM2 - 16 * 40 * FLOP_UM2     # high: its own read port on the table
    lines, nums = [], {}
    lines.append(f"flop = 155,197 / 3,168 = {FLOP_UM2:.1f} um2 (average, routed)")
    lines.append(f"per slot flops: config {cfg} ({'; '.join(f'{k}' for k in CONFIG_BITS)}) + pipeline {pipe}"
                 f" + output register {outreg} = {flops} flops = {flops * FLOP_UM2:,.0f} um2")
    lines.append(f"bound word: low = own 40-bit copy, {bound_copy:,.0f} um2; high = own read port on the shared "
                 f"table, 45,900 - 640 flops x {FLOP_UM2:.1f} = {bound_port:,.0f} um2")
    lines.append("datapath per slot: 3 timestamp muxes 8:1 x 16 bits (start, cond A, cond B; the end timestamp "
                 "is now - 2, shared), 2 subtractors (m, cond), 1 whole verdict block (all slots may fire in "
                 "one cycle); ORD history (3 x 8 pulse bits) and the edge unit already exist and are shared")
    lines.append(f"shared, once: {SHARED_FLOPS} flops for LAST_PRE = {SHARED_FLOPS * FLOP_UM2:,.0f} um2; the "
                 "qualifier in r-1 is the existing s3 level, no flop")
    lines.append(f"assumed (not measured): subtractor {SUB16[0]}-{SUB16[1]}, mux {MUX8x16[0]:,}-{MUX8x16[1]:,}, "
                 f"glue {GLUE[0]}-{GLUE[1]} um2")
    for i, (name, bound) in enumerate((("low", bound_copy), ("high", bound_port))):
        per = flops * FLOP_UM2 + bound + 3 * MUX8x16[i] + 2 * SUB16[i] + GLUE[i] + VERDICT_UM2
        nums[name] = per
        lines.append(f"per slot {name}: {flops * FLOP_UM2:,.0f} + bound {bound:,.0f} + muxes 3 x {MUX8x16[i]:,}"
                     f" + subtractors 2 x {SUB16[i]} + glue {GLUE[i]} + verdict 1,600 = {per:,.0f} um2"
                     f" ({100 * per / CORE_UM2:.2f}% of the 1,208,170 um2 core)")
    for n in (8, 9):
        lo, hi = (n * nums[k] + SHARED_FLOPS * FLOP_UM2 for k in ("low", "high"))
        nums[n] = (lo, hi)
        lines.append(f"{n} slots + shared: {lo:,.0f} - {hi:,.0f} um2 = {100 * lo / CORE_UM2:.1f}% - {100 * hi / CORE_UM2:.1f}%"
                     " of the core (an output arbiter into the verdict FIFO not counted; the engine, snapshot "
                     f"register, stats {STATS4_UM2:,} and bounds table are kept, not removed)")
    for d in (2, 4):
        q = (d - 1) * QUEUE_ENTRY_UM2
        nums[f"q{d}"] = q
        lines.append(f"for comparison, snapshot queue depth {d}: {d - 1} x ~14,000 = {q:,} um2 = "
                     f"{100 * q / CORE_UM2:.1f}% of the core")
    return lines, nums
