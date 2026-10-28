# SPDX-License-Identifier: Apache-2.0
"""Cycle-accurate reference model of the Referee engine subsystem (one engine).

Written from docs/isa-timing.md (the timing contract), not from the RTL, so the
lockstep tests compare two independent descriptions. Encodings come from the
generated tools/isa_tables.py.

What it models, per clock cycle:
  - input synchronizers for engine inputs 0..11 (uio_in[0..7], ui_in[0..3])
  - the judge-channel mux and edge_ts (4 channels, 16-bit timestamps)
  - the engine's snapshot (trigger, one-cycle delay, ORD window r-1..r+1, epoch, LOST)
  - the engine: every instruction, deadlines DL/TO, flags, FIFOs, JUDGE + stats
  - images, the RUN check (CRC + pin claims), halt, open-drain release

Host operations are methods called between cycles; `step(ui_in, uio_in)` runs one
cycle and returns the observable state.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isa_tables as T  # noqa: E402
from refasm import IMAGE_WORDS, GLOBAL_WORDS, crc16, decode_word  # noqa: E402

M16 = 0xFFFF
TS_W = 16
QUARTER = 1 << (TS_W - 2)
U_FX = 2 * 16  # u = 2 cycles, in 1/16 cycle
CHECK_CYCLES = GLOBAL_WORDS + IMAGE_WORDS  # 105
UO_PINS = [0, 1, 2, 3, 5, 6, 7]  # output pins 8..14 -> uo_out bits

E = T.ENUMS
OPC = {k: v["opcode"] for k, v in T.INSTR.items()}


def s16(v):
    v &= M16
    return v - 0x10000 if v & 0x8000 else v


def verdict_rule(m, n_lo, inexact, is_max, invalid):
    """-> 'PASS' | 'VIOL' | 'INDET'  (u = 2 cycles, sharp VIOLATION, two-sided rounding)."""
    if invalid:
        return "INDET"
    mk, n_hi = m * 16, n_lo + inexact
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
    def __init__(self):
        self.now = 0
        self.glob = [0] * GLOBAL_WORDS
        self.img = [0] * IMAGE_WORDS
        self.crc_exp = [0, 0]  # global, engine
        self.crc_got = [0, 0]
        self.edge = EdgeTs()
        self.sync1 = [0] * 12
        self.sync2 = [0] * 12
        self.state = "halted"  # halted | check | running
        self.check_idx = 0
        self.crc_fail = 0
        self.fifo_in, self.fifo_out, self.fifo_v = [], [], []
        self.stats = [[0, 0, 0, None, None] for _ in range(4)]  # pass viol indet min max
        self.lost = 0
        self.sticky = {"OUT_OVF": 0, "VERDICT_OVF": 0, "IN_UNDERRUN": 0, "PIN_FAULT": 0}
        self.out_val = [1] * 15
        self._reset_core()

    def _reset_core(self):
        self.pc = 0
        self.X = self.Y = self.SR = self.RC = 0
        self.DL = self.TO = 0
        self.T = self.C = self.E = 0
        self.wd_stall = 0
        self.tag = {"X": [0, 0, 0], "Y": [0, 0, 0]}  # F, O, epoch
        self.S = 0
        self.valid = 0
        self.epoch = 0
        self.cap_stage = 0  # 0 idle, 1 = trigger was last cycle, 2 = capture this cycle
        self.trig = None  # (ch, e)
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

    def write_global(self, addr, word):
        if self.state != "halted":
            return False
        self.glob[addr] = word & M16
        return True

    def write_engine(self, addr, word):
        if self.state != "halted":
            return False
        self.img[addr] = word & M16
        return True

    def set_crc(self, sel, value):
        self.crc_exp[sel] = value & M16

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
        return self.fifo_v.pop(0) if self.fifo_v else None

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
            "state": self.state, "pc": self.pc, "X": self.X, "Y": self.Y, "SR": self.SR,
            "RC": self.RC, "DL": self.DL, "TO": self.TO, "T": self.T, "C": self.C, "E": self.E,
            "S": self.S, "epoch": self.epoch, "lost": self.lost,
            "uio_out": uio_out, "uio_oe": uio_oe, "uo": uo,
            "n_in": len(self.fifo_in), "n_out": len(self.fifo_out), "n_v": len(self.fifo_v),
        }

    # -- one clock cycle ----------------------------------------------------

    def step(self, ui_in, uio_in):
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
        # of r+1. ORD marks edges latched in r-1, r, r+1 (= pulses 3, 2, 1 cycles ago).
        snap_next = None
        if self.cap_stage == 2:
            ordb = [[int(self.hist[0][i][e] or self.hist[1][i][e] or self.hist[2][i][e]) for e in (0, 1)] for i in range(4)]
            snap_next = {"ts": [list(t) for t in ed.ts], "stale": [list(x) for x in ed.stale],
                         "last": list(ed.last_fall), "missed": list(ed.missed), "ord": ordb}
        hit = was_running and trig_before is not None and pulses[trig_before[0]][trig_before[1]]
        lost_now = 0
        new_pending = 0
        if hit:
            if self.S or self.cap_stage:
                lost_now = 1
            else:
                new_pending = 1
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

    # -- the engine ----------------------------------------------------------

    def _execute(self, now, inp):
        """Issue at most one instruction; returns 1 if it was REL."""
        if self.wd_stall and s16(now - self.DL) < 0:
            return 0
        self.wd_stall = 0
        m, f = decode_word(self.img[self.pc])
        nxt = (self.pc + 1) & 63
        rel = 0
        if m is None or m == "nop":
            pass
        elif m == "halt":
            self._halt()
            return 0
        elif m in ("set", "sho"):
            if m == "set":
                bit = f["v"]
            else:
                bit = (self.SR >> 15) & 1 if f["msb"] else self.SR & 1
                self.SR = ((self.SR << 1) | 1) & M16 if f["msb"] else (self.SR >> 1) | 0x8000
            if f["p"] < 15 and self.owned(f["p"]):
                self.out_val[f["p"]] = bit
            else:
                self.sticky["PIN_FAULT"] = 1
        elif m == "shi":
            b = inp[f["q"]]
            self.C = b
            self.SR = ((self.SR << 1) | b) & M16 if f["msb"] else (b << 15) | (self.SR >> 1)
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
            flag = [self.T, self.C, self.E, self.S][f["c"] >> 1]
            if flag ^ (f["c"] & 1):
                nxt = f["a"]
        elif m == "loop":
            if self.RC:
                self.RC -= 1
                nxt = f["a"]
        elif m == "ldrc":
            self.RC = f["imm"]
        elif m == "ldsr":
            self.SR = (self.SR & 0x00FF) | (f["imm"] << 8) if f["hi"] else (self.SR & 0xFF00) | f["imm"]
        elif m == "cmp":
            byte = (self.SR >> 8) & 0xFF if f["hi"] else self.SR & 0xFF
            self.C = int(byte == f["imm"])
        elif m == "pull":
            if self.fifo_in:
                b = self.fifo_in.pop(0)
                self.SR = (self.SR & 0x00FF) | (b << 8) if f["hi"] else (self.SR & 0xFF00) | b
                self.E = 0
            else:
                self.E = 1
                self.sticky["IN_UNDERRUN"] = 1
        elif m == "push":
            b = (self.SR >> 8) & 0xFF if f["hi"] else self.SR & 0xFF
            if len(self.fifo_out) < 4:
                self.fifo_out.append(b)
            else:
                self.sticky["OUT_OVF"] = 1
        elif m == "trig":
            self.trig = (f["ch"], f["e"] & 1)
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

    def _judge(self, f):
        m = s16(self.Y - self.X)
        fx, ox, ex = self.tag["X"]
        fy, oy, ey = self.tag["Y"]
        tagbad = int(ex != ey)
        invalid = int(fx or fy or ox or oy or tagbad)
        b = f["b"]
        w_hi, w_lo = self.img[64 + 2 * b], self.img[65 + 2 * b]
        n_lo = (w_hi << 4) | (w_lo >> 12)
        inexact, is_max = (w_lo >> 11) & 1, (w_lo >> 10) & 1
        v = verdict_rule(m, n_lo, inexact, is_max, invalid)
        if b < 4:
            st = self.stats[b]
            k = {"PASS": 0, "VIOL": 1, "INDET": 2}[v]
            st[k] = min(st[k] + 1, M16)
            if not invalid:
                st[3] = m if st[3] is None else min(st[3], m)
                st[4] = m if st[4] is None else max(st[4], m)
        mode = f["push"]
        push = mode == E["push"]["all"] or (mode == E["push"]["nonpass"] and v != "PASS")
        if push:
            need = 1 + f["raw"]
            if len(self.fifo_v) + need <= 8:
                vcode = {"INDET": 0, "PASS": 1, "VIOL": 2}[v]
                msat = max(-4, min(3, m)) & 7
                hdr = (vcode << 14 | fx << 13 | fy << 12 | tagbad << 11 | self.lost_since << 10
                       | self.non_intrusive() << 9 | f["raw"] << 8 | b << 4 | (ox or oy) << 3 | msat)
                self.fifo_v.append(hdr)
                if f["raw"]:
                    self.fifo_v.append(m & M16)
            else:
                self.sticky["VERDICT_OVF"] = 1
        self.lost_since = 0
