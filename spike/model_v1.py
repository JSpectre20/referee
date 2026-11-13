# SPDX-License-Identifier: Apache-2.0
"""SPIKE copy of tools/refmodel.py with the ISA v1 candidates (spike/README.md).
`diff tools/refmodel.py spike/model_v1.py` is the behavioural spec diff of the candidates:
  A  separate shift registers SRO (sho, pull, ldsr) and SRI (shi, push, cmp)
  B  second loop counter (ldrc / loop ..., rc1)
  C1 7-bit addresses (128 words), 4-bit branch condition (new flags Z, U, A)
  C2 CRC / parity register L with polynomial POLY (crcp, bsc, sho ..., crc)
  C3 bit stuffing: run counter, flag U, sho / shi ..., stuff
  D  guard band per bound: signed n_lo and a width (verdict_rule)
  E1 trigger on a set of channels, rise / fall / any
  E2 tsts: line level as of the trigger, which edge triggered, flag A (ambiguous)
Not in the model yet: F (short-level taint), G (non-PASS pulse pin), NRZI.
Spike simplification: Model(words=64|96|128) only sets where the pc wraps; the image keeps the
128-word layout (bounds at word 128, RUN check 8 + 161 cycles) for every size.

Original docstring:

Cycle-accurate reference model of the Referee engine subsystem (one engine).

Written from docs/isa-timing.md (the timing contract), not from the RTL, so the
lockstep tests compare two independent descriptions. Encodings come from the
generated tools/isa_tables.py.

What it models, per clock cycle:
  - input synchronizers for engine inputs 0..11 (uio_in[0..7], ui_in[0..3])
  - the judge-channel mux and edge_ts (4 channels, 16-bit timestamps)
  - the engine's snapshot (trigger, one-cycle delay, ORD window r-1..r+1, epoch, LOST)
  - the engine: every instruction, deadlines DL/TO, flags, FIFOs, JUDGE + stats
    (JUDGE in two cycles: issue latches its inputs; the next cycle computes the
    verdict and does the push, VERDICT_OVF, stats and the L clear together)
  - images, the RUN check (CRC + pin claims), halt, open-drain release

Host operations are methods called between cycles; `step(ui_in, uio_in)` runs one
cycle and returns the observable state.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isa_tables_v1 as T  # noqa: E402
from asm_v1 import IMAGE_WORDS, GLOBAL_WORDS, PROGRAM_WORDS, crc16, decode_word  # noqa: E402

M16 = 0xFFFF
TS_W = 16
QUARTER = 1 << (TS_W - 2)
U_FX = 2 * 16  # u = 2 cycles, in 1/16 cycle
CHECK_CYCLES = GLOBAL_WORDS + IMAGE_WORDS  # 8 + 161 in the spike (105 in v0.3)
UO_PINS = [0, 1, 2, 3, 5, 6, 7]  # output pins 8..14 -> uo_out bits

E = T.ENUMS
OPC = {k: v["opcode"] for k, v in T.INSTR.items()}


def s16(v):
    v &= M16
    return v - 0x10000 if v & 0x8000 else v


def verdict_rule(m, n_lo, width, is_max, invalid):
    """-> 'PASS' | 'VIOL' | 'INDET'  (u = 2 cycles, sharp VIOLATION).

    v1 (D): n_lo is SIGNED (1/16 cycle) and `width` replaces the 1-bit `inexact`: the bound
    the spec means lies somewhere in [n_lo, n_lo + width] as seen from our pads (rounding,
    guard band). PASS is tested against the far end, VIOLATION against the near end."""
    if invalid:
        return "INDET"
    mk, n_hi = m * 16, n_lo + width
    if not is_max:
        if mk - U_FX >= n_hi:
            return "PASS"
        if mk + U_FX <= n_lo:
            return "VIOL"
    else:
        if mk + U_FX <= n_lo:
            return "PASS"
        if mk - U_FX >= n_hi:
            return "VIOL"
    return "INDET"


class EdgeTs:
    def __init__(self):
        self.s1 = [0] * 4
        self.s2 = [0] * 4
        self.s3 = [0] * 4
        self.warm = 0
        self.ts = [[0, 0] for _ in range(4)]  # [rise, fall]
        self.stale = [[1, 1] for _ in range(4)]
        self.missed = [0] * 4
        self.last_fall = [0] * 4

    def pulses(self):
        armed = self.warm == 3
        return [[armed and self.s2[i] and not self.s3[i], armed and not self.s2[i] and self.s3[i]] for i in range(4)]

    def next_latched(self, now, pulses):
        """Latched state at the end of this cycle (what a capture sees)."""
        ts = [list(t) for t in self.ts]
        stale = [list(s) for s in self.stale]
        last = list(self.last_fall)
        missed = list(self.missed)
        armed = self.warm == 3
        for i in range(4):
            for e in (0, 1):
                if pulses[i][e]:
                    ts[i][e] = now
                    stale[i][e] = 0
                elif (now - self.ts[i][e]) & M16 == QUARTER:
                    stale[i][e] = 1
            if pulses[i][0]:
                last[i] = 0
            if pulses[i][1]:
                last[i] = 1
            if armed and (self.s1[i] ^ self.s2[i]) and (self.s2[i] ^ self.s3[i]):
                missed[i] = 1
        return ts, stale, last, missed

    def commit(self, now, pulses, raw, clear_missed):
        ts, stale, last, missed = self.next_latched(now, pulses)
        self.ts, self.stale, self.last_fall = ts, stale, last
        self.missed = [0] * 4 if clear_missed else missed
        self.s3, self.s2, self.s1 = list(self.s2), list(self.s1), list(raw)
        if self.warm < 3:
            self.warm += 1


class Model:
    def __init__(self, words=PROGRAM_WORDS):
        assert words in (64, 96, 128)
        self.words = words  # v1: this engine's program size (Referee 64, Player 96 or 128); pc wraps here
        self.now = 0
        self.glob = [0] * GLOBAL_WORDS
        self.img = [0] * IMAGE_WORDS
        self.crc_exp = [0, 0]  # global, engine
        self.crc_got = [0, 0]
        self.edge = EdgeTs()
        self.sync1 = [0] * 12
        self.sync2 = [0] * 12
        self.state = "halted"  # halted | check | running
        self._state0 = "halted"  # the state at the start of the last step (gates host writes)
        self.check_idx = 0
        self.crc_fail = 0
        self.fifo_in, self.fifo_out, self.fifo_v = [], [], []
        self.stats = [[0, 0, 0, None, None] for _ in range(4)]  # pass viol indet min max
        self.lost = 0
        self.sticky = {"OUT_OVF": 0, "VERDICT_OVF": 0, "IN_UNDERRUN": 0, "PIN_FAULT": 0}
        self.out_val = [1] * 15
        self.ran = 0  # a RUN has started since reset: before that no pin is driven
        self.j_pend = None  # a JUDGE issued last cycle: its cycle 2 runs at the start of this step
        self._popped_v = 0  # a host verdict pop happened before this step (frees room only next cycle)
        self._reset_core()

    def _reset_core(self):
        self.pc = 0
        self.X = self.Y = 0
        self.SRO = self.SRI = 0  # v1 (A): separate output / input shift registers
        self.RC = [0, 0]  # v1 (B): two loop counters
        # v1 (C2/C3): bitstream unit
        self.L = self.POLY = 0  # CRC / parity register and polynomial (left-aligned, top term implied)
        self.tap = 0  # 0 off, 1 the bits SHO sends, 2 the bits SHI samples
        self.st = 0  # stuffing rule: 0 none, 1 can, 2 usb
        self.inv = 0
        self.runlen = 0  # consecutive equal stream bits (usb: consecutive ones)
        self.lastbit = 1
        self.A = 0  # v1 (E2): the last TSTS answer was ambiguous
        self.DL = self.TO = 0
        self.T = self.C = self.E = 0
        self.wd_stall = 0
        self.tag = {"X": [0, 0, 0], "Y": [0, 0, 0]}  # F, O, epoch
        self.S = 0
        self.valid = 0
        self.epoch = 0
        self.cap_stage = 0  # 0 idle, 1 = trigger was last cycle, 2 = capture this cycle
        self.trig = None  # v1 (E1): (channel mask, edge kind 0 rise / 1 fall / 3 any)
        self.trig_pend = set()  # the trigger edge(s) (ch, e) reported in r, while a capture is pending
        self.trig_armed = None  # the (mask, kind) that capture was armed with
        self.lost_since = 0
        self.snap = None
        self.hist = [[[0, 0] for _ in range(4)] for _ in range(3)]  # pulses at c-1, c-2, c-3

    # -- config decode ----------------------------------------------------

    def pin_cfg(self, p):
        nib = (self.glob[2 + p // 4] >> (4 * (p % 4))) & 0xF
        return nib >> 2, nib & 3  # owner, mode

    def mux(self, ch):
        return (self.glob[6] >> (4 * ch)) & 0xF

    def non_intrusive(self):
        return int(all(self.mux(ch) == 0 for ch in range(4)))

    def owned(self, p):
        owner, mode = self.pin_cfg(p)
        return owner == 1 and mode != 0 and (p < 8 or mode == 1)

    def claims_ok(self):
        mask = self.img[IMAGE_WORDS - 1]
        return all(self.owned(p) for p in range(15) if mask >> p & 1) and not mask >> 15

    # -- host operations (between cycles) ---------------------------------

    # Host writes are gated by the run state at the START of the cycle they
    # arrive in (as the RTL's `halted`), so none lands during a RUN check.
    def write_global(self, addr, word):
        if self._state0 != "halted":
            return False
        self.glob[addr] = word & M16
        return True

    def write_engine(self, addr, word):
        if self._state0 != "halted":
            return False
        self.img[addr] = word & M16
        return True

    def set_crc(self, sel, value):
        if self._state0 != "halted":
            return False
        self.crc_exp[sel] = value & M16
        return True

    def run(self):
        if self.state != "halted":
            return False
        self.state, self.check_idx = "check", 0  # called after the RUN cycle: the check starts next cycle
        return True

    def halt(self):
        if self.state == "running":
            self._halt()
        elif self.state == "check":
            self.state = "halted"

    def _halt(self):
        self.state = "halted"
        for p in range(8):  # release open-drain pins
            if self.pin_cfg(p)[1] == 2:
                self.out_val[p] = 1

    def in_push(self, b):
        if len(self.fifo_in) >= 4:
            return False
        self.fifo_in.append(b & 0xFF)
        return True

    def out_pop(self):
        return self.fifo_out.pop(0) if self.fifo_out else None

    def verdict_pop(self):
        if not self.fifo_v:
            return None
        self._popped_v = 1
        return self.fifo_v.pop(0)

    def stats_latch(self, slot):
        st = self.stats[slot]
        self.shadow = [st[0], st[1], st[2], (st[3] or 0) & M16, (st[4] or 0) & M16]

    def stats_read(self, idx):
        return getattr(self, "shadow", [0] * 5)[idx]

    def stats_clear(self, slot):
        self.stats[slot] = [0, 0, 0, None, None]

    def lost_clear(self):
        self.lost = 0

    def status_clear(self):
        for k in self.sticky:
            self.sticky[k] = 0
        self.crc_fail = 0

    def status(self):
        st = self.sticky
        return (int(self.state == "check") << 7 | int(self.state == "running") << 6 | self.S << 5
                | self.crc_fail << 4 | st["PIN_FAULT"] << 3 | st["IN_UNDERRUN"] << 2
                | st["VERDICT_OVF"] << 1 | st["OUT_OVF"])

    # -- observable outputs -----------------------------------------------

    def pins(self):
        """-> (uio_out, uio_oe, uo_out_bits) driven by the engine."""
        uio_out = uio_oe = uo = 0
        if not self.ran:  # nothing is driven before the first RUN since reset
            return 0, 0, 0
        for p in range(8):
            owner, mode = self.pin_cfg(p)
            if owner == 1 and mode == 1:
                uio_oe |= 1 << p
                uio_out |= self.out_val[p] << p
            elif owner == 1 and mode == 2 and not self.out_val[p]:
                uio_oe |= 1 << p
        for k, bit in enumerate(UO_PINS):
            p = 8 + k
            owner, mode = self.pin_cfg(p)
            if owner == 1 and mode == 1:
                uo |= self.out_val[p] << bit
        return uio_out, uio_oe, uo

    def observe(self):
        uio_out, uio_oe, uo = self.pins()
        return {
            "state": self.state, "pc": self.pc, "X": self.X, "Y": self.Y, "SRO": self.SRO, "SRI": self.SRI,
            "RC0": self.RC[0], "RC1": self.RC[1], "L": self.L, "runlen": self.runlen, "A": self.A, "DL": self.DL, "TO": self.TO, "T": self.T, "C": self.C, "E": self.E,
            "S": self.S, "epoch": self.epoch, "lost": self.lost,
            "uio_out": uio_out, "uio_oe": uio_oe, "uo": uo,
            "n_in": len(self.fifo_in), "n_out": len(self.fifo_out), "n_v": len(self.fifo_v),
        }

    # -- one clock cycle ----------------------------------------------------

    def step(self, ui_in, uio_in):
        self._state0 = self.state
        now = self.now
        ed = self.edge
        pulses = ed.pulses()
        in_now = self.sync2 + [ed.s2[ch] for ch in range(4)]  # engine inputs 0..15 this cycle
        raw_sync = [(uio_in >> i) & 1 for i in range(8)] + [(ui_in >> i) & 1 for i in range(4)]
        raw_ch = []
        for ch in range(4):
            src = self.mux(ch)
            raw_ch.append((ui_in >> (4 + ch)) & 1 if src == 0 else (uio_in >> (src - 1)) & 1)

        clear_missed = False
        start_core = False
        rel = 0
        # JUDGE cycle 2 for a JUDGE issued last cycle, on the state this cycle starts
        # from (lost_since, FIFO level before this cycle's pop); a trigger lost in this
        # same cycle sets lost_since again below (set wins over the clear)
        jp, self.j_pend = self.j_pend, None
        if jp is not None:
            self._judge2(jp)
        # Registered state at the start of the cycle: an instruction issued in this
        # cycle (trig, halt) only takes effect from the next one.
        was_running, trig_before = self.state == "running", self.trig
        if self.state == "check":
            if self.check_idx >= 0:
                if self.check_idx == CHECK_CYCLES - 1:
                    ok = (crc16(self.glob) == self.crc_exp[0] and crc16(self.img) == self.crc_exp[1]
                          and self.claims_ok())
                    self.crc_got = [crc16(self.glob), crc16(self.img)]
                    if ok:
                        start_core = True
                    else:
                        self.crc_fail = 1
                        self.state = "halted"
            self.check_idx += 1
        elif self.state == "running":
            rel = self._execute(now, in_now)

        # Snapshot logic. A trigger reported in cycle r is captured in cycle r+2 from
        # the edge_ts registers, which then hold exactly the state latched at the end
        # of r+1. ORD marks edges latched in r-1, r, r+1 (= pulses 3, 2, 1 cycles ago),
        # except the trigger edge's own report in r: its order relative to the trigger
        # is known (it is the trigger).
        snap_next = None
        if self.cap_stage == 2:
            ordb = [[int(self.hist[0][i][e] or (self.hist[1][i][e] and (i, e) not in self.trig_pend)
                         or self.hist[2][i][e]) for e in (0, 1)] for i in range(4)]
            # v1 (E2): "level" is each channel's level after every edge latched up to the end of
            # r+1 (= s3 in this cycle, r+2); "trig" is the set of trigger edges reported in r.
            snap_next = {"ts": [list(t) for t in ed.ts], "stale": [list(x) for x in ed.stale],
                         "last": list(ed.last_fall), "missed": list(ed.missed), "ord": ordb,
                         "level": list(ed.s3), "trig": set(self.trig_pend), "armed": self.trig_armed}
        # v1 (E1): the trigger is any selected edge of any channel in the mask
        hits = set()
        if was_running and trig_before is not None:
            mask, kind = trig_before
            for ch in range(4):
                for e in (0, 1):
                    if (mask >> ch) & 1 and (kind == 3 or kind == e) and pulses[ch][e]:
                        hits.add((ch, e))
        hit = bool(hits)
        lost_now = 0
        new_pending = 0
        if hit:
            if self.S or self.cap_stage:
                lost_now = 1
            else:
                new_pending = 1
                self.trig_pend = hits
                self.trig_armed = trig_before  # the (mask, kind) this capture was armed with
        if snap_next is not None:
            self.snap = snap_next
            self.epoch = (self.epoch + 1) & 3
            self.S, self.valid = 1, 1
        elif rel:
            self.S = 0
        self.cap_stage = 1 if new_pending else (2 if self.cap_stage == 1 else 0)
        if lost_now:
            self.lost = min(self.lost + 1, 255)
            self.lost_since = 1

        if start_core:
            self.ran = 1
            self.state = "running"
            self._reset_core()
            self.DL = self.TO = (now + 1) & M16
            for k in self.sticky:
                self.sticky[k] = 0
            self.crc_fail = 0
            clear_missed = True

        self.hist = [pulses, self.hist[0], self.hist[1]]
        ed.commit(now, pulses, raw_ch, clear_missed)
        self.sync2, self.sync1 = list(self.sync1), raw_sync
        self.now = (now + 1) & M16
        self._popped_v = 0

    # -- the engine ----------------------------------------------------------

    def _execute(self, now, inp):
        """Issue at most one instruction; returns 1 if it was REL."""
        if self.wd_stall and s16(now - self.DL) < 0:
            return 0
        self.wd_stall = 0
        m, f = decode_word(self.img[self.pc])
        nxt = (self.pc + 1) % self.words
        rel = 0
        if m is None or m == "nop":
            pass
        elif m == "halt":
            self._halt()
            return 0
        elif m in ("set", "sho"):
            if m == "set":
                bit = f["v"]
            elif f["sm"] == E["sm"]["crc"]:  # v1 (C2): send the CRC register's top bit
                bit = ((self.L >> 15) & 1) ^ self.inv
                self.L = (self.L << 1) & M16
                self._run(bit)
            elif f["sm"] == E["sm"]["stuff"]:  # v1 (C3): send a stuff bit; SRO and L untouched
                bit = 0 if self.st == E["st"]["usb"] else 1 - self.lastbit
                self._run_restart(bit)
            else:
                bit = (self.SRO >> 15) & 1 if f["msb"] else self.SRO & 1
                self.SRO = ((self.SRO << 1) | 1) & M16 if f["msb"] else (self.SRO >> 1) | 0x8000
                if self.tap == E["tap"]["sho"]:
                    self._feed(bit)
                    self._run(bit)
            if f["p"] < 15 and self.owned(f["p"]):
                self.out_val[f["p"]] = bit
            else:
                self.sticky["PIN_FAULT"] = 1
        elif m == "shi":
            b = inp[f["q"]]
            if f["sm"] == E["sm"]["stuff"]:  # v1 (C3): sample a stuff bit; C = 1 if it breaks the rule
                self.C = int(b == 1) if self.st == E["st"]["usb"] else int(b == self.lastbit)
                self._run_restart(b)
            else:
                self.C = b
                self.SRI = ((self.SRI << 1) | b) & M16 if f["msb"] else (b << 15) | (self.SRI >> 1)
                if self.tap == E["tap"]["shi"]:
                    self._feed(b)
                    self._run(b)
        elif m == "tstp":
            self.C = inp[f["q"]]
        elif m == "mark":
            self.DL = now
        elif m in ("tmo", "tmon"):
            base = self.DL if m == "tmo" else now
            self.TO = (base + f["t"] * (16 if f["x16"] else 1)) & M16
        elif m == "waitd":
            self.DL = (self.DL + f["n"]) & M16
            self.wd_stall = 1
        elif m in ("waitp", "waits"):
            ev = (inp[f["q"]] == f["v"]) if m == "waitp" else bool(self.S)
            if ev:
                self.T = 0
            elif s16(now - self.TO) >= 0:
                self.T = 1
            else:
                return 0  # keep waiting at this pc
        elif m == "jmp":
            nxt = f["a"]
        elif m == "br":
            flag = [self.T, self.C, self.E, self.S, int(self.L == 0), self._stuff_due(), self.A, 0][f["c"] >> 1]
            if flag ^ (f["c"] & 1):
                nxt = f["a"]
        elif m == "loop":
            if self.RC[f["rc1"]]:
                self.RC[f["rc1"]] -= 1
                nxt = f["a"]
        elif m == "ldrc":
            self.RC[f["rc1"]] = f["imm"]
        elif m == "ldsr":
            self.SRO = (self.SRO & 0x00FF) | (f["imm"] << 8) if f["hi"] else (self.SRO & 0xFF00) | f["imm"]
        elif m == "crcp":  # v1 (C2)
            self.POLY = (self.POLY & 0x00FF) | (f["imm"] << 8) if f["hi"] else (self.POLY & 0xFF00) | f["imm"]
        elif m == "bsc":  # v1 (C2/C3)
            self.tap, self.st, self.inv = f["tap"], f["st"], f["inv"]
            if f["init"] != E["init"]["keep"]:
                # ones: only the CRC's own bits (bit 15 down to POLY's lowest set bit); a 1 below
                # them would shift up into a CRC narrower than 16 bits
                ones = M16 & ~((self.POLY & -self.POLY) - 1) if self.POLY else M16
                self.L = ones if f["init"] == E["init"]["ones"] else 0
                self.runlen, self.lastbit = 0, 1
        elif m == "tsts":  # v1 (E2)
            sn = self.snap
            if sn is None or not self.valid:
                self.C, self.A = 0, 1
            elif f["w"] == E["w"]["level"]:
                ch = f["ch"]
                self.C = sn["level"][ch]
                # ambiguous: an edge of ch too close to the trigger to order (ORD), or ch itself
                # is one of several trigger edges of the same cycle (they are exempt from ORD)
                own_trig = len(sn["trig"]) > 1 and any(t[0] == ch for t in sn["trig"])
                self.A = int(sn["ord"][ch][0] or sn["ord"][ch][1] or sn["missed"][ch] or own_trig)
            else:
                edge = (f["ch"], 0 if f["w"] == E["w"]["trise"] else 1)
                self.C = int(edge in sn["trig"])
                # ambiguous: several trigger edges in the trigger cycle, or another edge the
                # trigger was armed for sits in the ORD window (it may truly have come first)
                mask, kind = sn["armed"]
                near = any(sn["ord"][c][e] for c in range(4) for e in (0, 1)
                           if (mask >> c) & 1 and (kind == 3 or kind == e))
                self.A = int(len(sn["trig"]) > 1 or near)
        elif m == "cmp":
            byte = (self.SRI >> 8) & 0xFF if f["hi"] else self.SRI & 0xFF
            self.C = int(byte == f["imm"])
        elif m == "pull":
            if self.fifo_in:
                b = self.fifo_in.pop(0)
                self.SRO = (self.SRO & 0x00FF) | (b << 8) if f["hi"] else (self.SRO & 0xFF00) | b
                self.E = 0
            else:
                self.E = 1
                self.sticky["IN_UNDERRUN"] = 1
        elif m == "push":
            b = (self.SRI >> 8) & 0xFF if f["hi"] else self.SRI & 0xFF
            if len(self.fifo_out) < 4:
                self.fifo_out.append(b)
            else:
                self.sticky["OUT_OVF"] = 1
        elif m == "trig":
            self.trig = (f["tm"], f["e"])  # v1 (E1): channel mask, rise / fall / any
        elif m == "rel":
            rel = 1
        elif m == "tsget":
            ch, e = f["ch"], f["e"]
            sn = self.snap
            if sn is None:
                val, F, O = 0, 1, 0
            else:
                edge = sn["last"][ch] if e == E["e"]["last"] else (e & 1)
                val = sn["ts"][ch][edge]
                F = int(sn["stale"][ch][edge] or sn["missed"][ch] or not self.valid)
                O = sn["ord"][ch][edge]
            reg = "Y" if f["r"] else "X"
            setattr(self, reg, val)
            self.tag[reg] = [F, O, self.epoch]
        elif m == "judge":
            self._judge(f)
        self.pc = nxt
        return rel

    # -- v1 (C2/C3): bitstream unit ------------------------------------------

    def _feed(self, bit):
        """One data bit of the tapped stream into the CRC / parity register."""
        fb = ((self.L >> 15) & 1) ^ bit ^ self.inv
        self.L = (self.L << 1) & M16
        if fb:
            self.L ^= self.POLY

    def _run(self, bit):
        """The run counter follows every data and crc bit of the stream (not while tap is off)."""
        if self.tap == E["tap"]["off"]:
            return
        if self.st == E["st"]["usb"]:
            self.runlen = self.runlen + 1 if bit else 0
        elif self.st == E["st"]["can"]:
            self.runlen = self.runlen + 1 if (self.runlen and bit == self.lastbit) else 1
        self.lastbit = bit

    def _run_restart(self, bit):
        """After a stuff bit: can counts it as the first bit of a new run; usb starts from zero."""
        if self.tap == E["tap"]["off"]:
            return
        self.runlen = 1 if self.st == E["st"]["can"] else 0
        self.lastbit = bit

    def _stuff_due(self):
        if self.tap == E["tap"]["off"]:
            return 0
        return int((self.st == E["st"]["can"] and self.runlen >= 5) or (self.st == E["st"]["usb"] and self.runlen >= 6))

    def _judge(self, f):
        """JUDGE cycle 1 (issue): latch m, the tags, bound b's words and the fields."""
        fx, ox, ex = self.tag["X"]
        fy, oy, ey = self.tag["Y"]
        b = f["b"]
        self.j_pend = {
            "m": s16(self.Y - self.X), "fx": fx, "fy": fy, "ord": int(ox or oy), "tagbad": int(ex != ey),
            "invalid": int(fx or fy or ox or oy or ex != ey), "b": b, "raw": f["raw"], "push": f["push"],
            "w_hi": self.img[PROGRAM_WORDS + 2 * b], "w_lo": self.img[PROGRAM_WORDS + 1 + 2 * b],
            "ni": self.non_intrusive(),
        }

    def _judge2(self, jp):
        """JUDGE cycle 2: verdict, push (room against the level at the start of this
        cycle), VERDICT_OVF, stats, and the L bit's report and clear."""
        m, invalid, b = jp["m"], jp["invalid"], jp["b"]
        w_hi, w_lo = jp["w_hi"], jp["w_lo"]
        n_lo = (w_hi << 4) | (w_lo >> 12)
        if n_lo & 0x80000:  # v1 (D): n_lo is signed
            n_lo -= 1 << 20
        width, is_max = w_lo & 0x3FF, (w_lo >> 10) & 1
        v = verdict_rule(m, n_lo, width, is_max, invalid)
        if b < 4:
            st = self.stats[b]
            k = {"PASS": 0, "VIOL": 1, "INDET": 2}[v]
            st[k] = min(st[k] + 1, M16)
            if not invalid:
                st[3] = m if st[3] is None else min(st[3], m)
                st[4] = m if st[4] is None else max(st[4], m)
        mode = jp["push"]
        push = mode == E["push"]["all"] or (mode == E["push"]["nonpass"] and v != "PASS")
        if push:
            need = 1 + jp["raw"]
            if len(self.fifo_v) + self._popped_v + need <= 8:
                vcode = {"INDET": 0, "PASS": 1, "VIOL": 2}[v]
                msat = max(-4, min(3, m)) & 7
                hdr = (vcode << 14 | jp["fx"] << 13 | jp["fy"] << 12 | jp["tagbad"] << 11 | self.lost_since << 10
                       | jp["ni"] << 9 | jp["raw"] << 8 | b << 4 | jp["ord"] << 3 | msat)
                self.fifo_v.append(hdr)
                if jp["raw"]:
                    self.fifo_v.append(m & M16)
            else:
                self.sticky["VERDICT_OVF"] = 1
        self.lost_since = 0
