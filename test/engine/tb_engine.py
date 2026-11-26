# SPDX-License-Identifier: Apache-2.0
"""Engine lockstep: src/engine_sys.v against tools/refmodel.py, cycle by cycle.

Every cycle the RTL's state of BOTH engines (run state, pc, registers, flags,
snapshot S and epoch, LOST, FIFO levels, status) and the pads must equal the
model's. Programs for the Referee (engine 0) and the Player (engine 1), pin
ownership between them, bounds, inputs and host operations are random; the
firmware in firmware/engine also runs in lockstep, on either engine.
"""

import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, Timer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import isa_tables as T  # noqa: E402
import refasm  # noqa: E402
from refmodel import Model  # noqa: E402

ST = {"halted": 0, "check": 1, "running": 2}
STROBES = ["h_gwe", "h_gcrc_we", "h_we", "h_crc_we", "h_run", "h_halt", "h_in_push", "h_out_pop",
           "h_vf_pop", "h_stats_latch", "h_stats_clear", "h_lost_clear", "h_status_clear"]


def op_engine(op):
    """The engine a host op addresses (engine-0-only ops and global ops: 0)."""
    k = op[0]
    if k == "ew":
        return op[3] if len(op) > 3 else 0
    if k in ("ecrc", "in"):
        return op[2] if len(op) > 2 else 0
    if k in ("run", "halt", "out", "stclear"):
        return op[1] if len(op) > 1 else 0
    return 0


class Lockstep:
    def __init__(self, dut, rng):
        self.dut, self.rng = dut, rng
        self.md = Model()
        self.engs = [dut.u_sys.u_eng0, dut.u_sys.u_eng1]
        self.eng = self.engs[0]
        self.ui = self.uio = 0
        self.cycle = 0
        self.log = []  # recent (cycle, ops, model states) for failure messages
        self.latched = False  # a stats latch happened last cycle: read the shadow
        self.player_pcs = set()  # every pc the Player issued from (coverage)

    async def reset(self):
        d = self.dut
        d.rst_n.value = 0
        for s in STROBES:
            getattr(d, s).value = 0
        d.h_e.value = 0
        d.ui_in.value = 0
        d.uio_in.value = 0
        for _ in range(3):
            await RisingEdge(d.clk)
        await FallingEdge(d.clk)
        d.rst_n.value = 1
        # The RTL's first cycle out of reset runs on the next rising edge, with the
        # inputs set right here (all zero): the model takes the same cycle now.
        self.md.step(0, 0)
        self.cycle = 1

    def compare(self):
        d, md = self.dut, self.md
        uio_out, uio_oe, uo = md.pins()
        diff = {}
        pads = {"uio_out": (int(d.uio_out.value), uio_out), "uio_oe": (int(d.uio_oe.value), uio_oe),
                "uo": (int(d.uo_eng.value), uo)}
        diff.update({k: v for k, v in pads.items() if v[0] != v[1]})
        for idx, (e, me) in enumerate(zip(self.engs, md.eng)):
            ob = me.observe()
            rtl = {
                "state": int(e.st.value), "pc": int(e.pc.value), "X": int(e.X.value), "Y": int(e.Y.value),
                "SR": int(e.SR.value), "RC": int(e.RC.value), "DL": int(e.DL.value), "TO": int(e.TO.value),
                "T": int(e.T.value), "C": int(e.C.value), "E": int(e.E.value), "S": int(e.S.value),
                "epoch": int(e.epoch.value), "lost": int(e.lost.value),
                "n_in": int(e.fin_n.value), "n_out": int(e.fout_n.value), "n_v": int(e.vf_count.value),
                "status": int(e.status.value),
            }
            want = dict(ob, state=ST[ob["state"]], status=me.status())
            if me.state != "running":  # registers only matter while running
                for k in ("X", "Y", "SR", "RC", "DL", "TO", "T", "C", "E", "pc"):
                    rtl.pop(k)
                    want.pop(k)
            elif idx == 1 and int(e.issue.value):
                self.player_pcs.add(me.pc)
            diff.update({f"e{idx}.{k}": (rtl[k], want[k]) for k in want if rtl[k] != want[k]})
        if diff:  # (built outside the f-string: Python 3.11 in CI has no nested quotes in f-strings)
            recent = [x for x in self.log if x[1] not in (["latch"], ["gw"], ["ew"])][-8:]
            raise AssertionError(f"cycle {self.cycle} (now {md.now}): RTL != model: {diff}; recent: {recent}")

    async def tick(self, ui=None, uio=None, ops=(), read_shadow=False):
        """One cycle: compare, drive inputs and host strobes, step the model. All ops of
        one cycle address the same engine (one host command per cycle, as host_port)."""
        d, md = self.dut, self.md
        await FallingEdge(d.clk)
        self.compare()
        if read_shadow:  # the stats shadow through the host read port (combinational)
            for idx in range(5):
                d.h_stats_idx.value = idx
                await Timer(1, unit="ns")
                assert int(d.stats_rdata.value) == md.stats_read(idx), f"cycle {self.cycle}: stats idx {idx}"
        for s in STROBES:
            getattr(d, s).value = 0
        if ui is not None:
            self.ui = ui
        if uio is not None:
            self.uio = uio
        d.ui_in.value = self.ui
        d.uio_in.value = self.uio
        post = []
        live = []  # guards evaluated now, on the state this cycle starts from
        for op in ops:
            k, en = op[0], op_engine(op)
            e = self.engs[en]
            if k == "in" and int(e.in_full.value):
                continue
            if k == "out" and (int(e.out_empty.value) or int(e.fout_n.value) == 4):
                continue
            if k == "vf" and int(d.vf_count.value) == 0:
                continue
            if k == "run" and md.eng[en].state != "halted":
                continue
            live.append(op)
        ops = live
        sel = {op_engine(op) for op in ops if op[0] in ("ew", "ecrc", "in", "run", "halt", "out", "stclear")}
        assert len(sel) <= 1, f"ops for two engines in one cycle: {ops}"
        en = sel.pop() if sel else 0
        d.h_e.value = en
        e = self.engs[en]
        for op in ops:
            kind = op[0]
            if kind == "gw":
                d.h_gwe.value, d.h_gaddr.value, d.h_gwdata.value = 1, op[1], op[2]
                post.append(lambda a=op[1], w=op[2]: md.write_global(a, w))
            elif kind == "ew":
                d.h_we.value, d.h_addr.value, d.h_wdata.value = 1, op[1], op[2]
                post.append(lambda a=op[1], w=op[2]: md.write_engine(a, w, en))
            elif kind == "gcrc":
                d.h_gcrc_we.value, d.h_gcrc.value = 1, op[1]
                post.append(lambda v=op[1]: md.set_crc(0, v))
            elif kind == "ecrc":
                d.h_crc_we.value, d.h_crc.value = 1, op[1]
                post.append(lambda v=op[1]: md.set_crc(1 + en, v))
            elif kind == "run":
                d.h_run.value = 1
                post.append(lambda: md.run(en))
            elif kind == "halt":
                d.h_halt.value = 1
                post.append(lambda: md.halt(en))
            elif kind == "in":
                d.h_in_push.value, d.h_in_data.value = 1, op[1]
                post.append(lambda b=op[1]: md.in_push(b, en))
            elif kind == "out":  # pop before the model's step: it reads the pre-step head
                d.h_out_pop.value = 1
                got, want = int(e.out_data.value), md.out_pop(en)
                assert got == want, f"cycle {self.cycle}: engine {en} OUT pop {got:#x} != model {want}"
            elif kind == "vf":
                d.h_vf_pop.value = 1
                got, want = int(d.vf_data.value), md.verdict_pop()
                assert got == want, f"cycle {self.cycle}: VERDICT pop {got:#06x} != model {want}"
            elif kind == "latch":
                d.h_stats_latch.value, d.h_stats_slot.value = 1, op[1]
                md.stats_latch(op[1])
            elif kind == "sclear":
                d.h_stats_clear.value, d.h_stats_slot.value = 1, op[1]
                post.append(lambda sl=op[1]: md.stats_clear(sl))
            elif kind == "lclear":
                d.h_lost_clear.value = 1
                post.append(md.lost_clear)
            elif kind == "stclear":
                d.h_status_clear.value = 1
                post.append(lambda: md.status_clear(en))
        md.step(self.ui, self.uio)
        for f in post:
            f()
        if ops:
            self.log.append((self.cycle, [o[0] for o in ops], [m.state for m in md.eng]))
        self.cycle += 1

    async def load(self, glob, img, gcrc=None, ecrc=None, e=0):
        """Global config (if glob is not None) and engine e's image, with both CRCs."""
        if glob is not None:
            for a, w in enumerate(glob):
                await self.tick(ops=[("gw", a, w)])
        for a, w in enumerate(img):
            await self.tick(ops=[("ew", a, w, e)])
        if glob is not None:
            await self.tick(ops=[("gcrc", refasm.crc16(glob) if gcrc is None else gcrc)])
        await self.tick(ops=[("ecrc", refasm.crc16(img) if ecrc is None else ecrc, e)])


# -----------------------------------------------------------------------------
# Random programs
# -----------------------------------------------------------------------------


def random_word(rng, referee_heavy=False):
    names = [n for n in T.INSTR if n != "halt"] + (["halt"] if rng.random() < 0.05 else [])
    if referee_heavy and rng.random() < 0.5:
        names = list(refasm.REFEREE_ONLY)
    m = rng.choice(names)
    vals = {}
    for f, _ in T.INSTR[m]["operands"]:
        hi, lo = T.FIELDS[f]
        vals[f] = rng.randrange(1 << (hi - lo + 1))
    if m == "waitd":
        vals["n"] = rng.randrange(0, 12)
    if m in ("tmo", "tmon"):
        vals["t"], vals["x16"] = rng.randrange(0, 40), 0
    return refasm.encode_word(m, vals)


def random_config(rng):
    pins = {}
    for p in range(15):
        r = rng.random()
        owner = 1 if r < 0.45 else 2 if r < 0.85 else rng.choice([0, 3])  # 3 = none (reserved)
        mode = rng.choice([1, 2]) if p < 8 else rng.choice([1, 1, 1, 2])
        pins[p] = (owner, mode)
    mux = [0 if rng.random() < 0.7 else rng.randrange(1, 9) for _ in range(4)]
    return pins, mux


def random_image(rng, pins, e=0, referee_heavy=False):
    """A random image for engine e (Referee: 64 words + bounds; Player: 96 words)."""
    a = refasm.Assembler(None, e)
    if a.has_bounds:
        for b in range(16):
            a.bounds[b] = (rng.randrange(0, 600), rng.randrange(2), rng.randrange(2))
    prog = [random_word(rng, referee_heavy) for _ in range(a.words_max)]
    img = a.image(prog)
    # Claim only owned pins most of the time, so RUN usually passes its check.
    owned = {p for p, (o, m) in pins.items() if o == e + 1 and (p < 8 or m == 1)}
    if rng.random() < 0.85:
        img[-1] = img[-1] & sum(1 << p for p in owned)
    return img


async def random_traffic(ls, rng, cycles):
    for _ in range(cycles):
        ui, uio = ls.ui, ls.uio
        for b in range(8):
            if rng.random() < 0.04:
                ui ^= 1 << b
            if rng.random() < 0.04:
                uio ^= 1 << b
        ops = []
        r = rng.random()
        e = rng.randrange(2)  # the engine an engine command goes to
        if r < 0.06:
            ops.append(("in", rng.randrange(256), e))
        elif r < 0.14:
            ops.append(("out", e))
        elif r < 0.22:
            ops.append(("vf",))
        elif r < 0.23:
            ops.append(("latch", rng.randrange(4)))
        elif r < 0.235:
            ops.append(("sclear", rng.randrange(4)))
        elif r < 0.24:
            ops.append(("lclear",))
        elif r < 0.245:
            ops.append(("stclear", e))
        elif r < 0.247:
            ops.append(("halt", e))
        elif r < 0.25:
            ops.append(("run", e))
        await ls.tick(ui, uio, ops, read_shadow=ls.latched)
        ls.latched = bool(ops) and ops[0][0] == "latch"


async def halt_all(ls):
    for e in (0, 1):
        if ls.md.eng[e].state != "halted":
            await ls.tick(ops=[("halt", e)])
            await ls.tick()


@cocotb.test()
async def lockstep_random_programs(dut):
    """Random programs on both engines (the Referee's 64 words + bounds, the Player's 96),
    random pin ownership between them, inputs and host traffic to either engine: RTL ==
    model every cycle. Each round loads both images and runs both, one after the other;
    the host traffic halts and re-runs either engine at random."""
    seeds = int(os.environ.get("SEEDS", "25"))
    rng = random.Random(2026)
    ls = Lockstep(dut, rng)
    await ls.reset()
    refused, started = [0, 0], [0, 0]
    for prog in range(seeds):
        pins, mux = random_config(rng)
        glob = refasm.global_image(pins, mux)
        await halt_all(ls)
        imgs = [random_image(rng, pins, e) for e in (0, 1)]
        bad = [rng.random() < 0.1 for e in (0, 1)]
        await ls.load(glob, imgs[0], ecrc=(refasm.crc16(imgs[0]) ^ 1) if bad[0] else None, e=0)
        await ls.load(None, imgs[1], ecrc=(refasm.crc16(imgs[1]) ^ 1) if bad[1] else None, e=1)
        for e in rng.sample([0, 1], 2):  # either order; the second check runs while the first engine runs
            await ls.tick(ops=[("run", e)])
            for _ in range(110):
                await ls.tick()
            if ls.md.eng[e].state == "running":
                started[e] += 1
            else:
                refused[e] += 1
        await random_traffic(ls, rng, 400)
    hi = sorted(p for p in ls.player_pcs if p >= 64)
    dut._log.info("lockstep: %d rounds, started %s, refused %s, %d cycles, Player pcs >= 64: %d",
                  seeds, started, refused, ls.cycle, len(hi))
    assert min(started) > seeds // 2, "most random images should pass the RUN check"
    assert len(hi) > 16, "random Player programs must run in words 64..95"


@cocotb.test()
async def lockstep_firmware(dut):
    """The firmware in firmware/engine runs in lockstep too (UART TX with bytes queued),
    first on the Referee, then on the Player (uio[0] given to engine 1)."""
    rng = random.Random(5)
    ls = Lockstep(dut, rng)
    await ls.reset()
    src = (ROOT / "firmware" / "engine" / "uart_tx.s").read_text().replace(".equ BIT 1s/115200", ".equ BIT 23")
    for e in (0, 1):
        await halt_all(ls)
        a = refasm.Assembler(60e6, e)  # cycle-level test: the spec's 60 MHz numbers
        img = a.image(a.assemble(src))
        await ls.load(refasm.global_image({0: (e + 1, 1)}), img, e=e)
        await ls.tick(ops=[("run", e)])
        for b in (0x55, 0xA3, 0x00):
            await ls.tick(ops=[("in", b, e)])
        for _ in range(3 * 11 * 23 + 300):
            await ls.tick(ui=0)  # CTS (ui_in[0]) low
        assert ls.md.eng[e].state == "running"


@cocotb.test()
async def lockstep_i2c_setup_referee(dut):
    """The section 5.3 referee (firmware/engine/i2c_setup_referee.s) in lockstep, judging a
    synthetic bus: SDA (channel 0 = ui_in[4]) changes d cycles before each SCL rise
    (channel 1 = ui_in[5]). Bound: min 100 ns = 6 cycles at 60 MHz, u = 2 cycles.

    The trigger edge (the SCL rise) is reported in the trigger cycle r itself; it must NOT
    be flagged ORD, or `tsget Y, 1, rise` would make every verdict INDETERMINATE. So all
    three verdicts must appear: d >= 9 PASS, d <= 3 VIOLATION, d within ~2 cycles of the
    SCL rise INDETERMINATE (ORD)."""
    rng = random.Random(7)
    ls = Lockstep(dut, rng)
    await ls.reset()
    a = refasm.Assembler(60e6)  # cycle-level test: the spec's 60 MHz numbers
    img = a.image(a.assemble((ROOT / "firmware" / "engine" / "i2c_setup_referee.s").read_text()))
    await ls.load(refasm.global_image(), img)
    await ls.tick(ops=[("run",)])
    for _ in range(110):
        await ls.tick()
    assert ls.md.ref.state == "running"
    sda = scl = 0

    async def hold(n):
        for _ in range(n):
            await ls.tick(ui=(sda << 4) | (scl << 5))

    await hold(20)
    for d in [20, 12, 9, 3, 1, 0, 15, 2, 10, 4]:
        sda ^= 1
        await hold(d)
        scl = 1
        await hold(30)
        scl = 0
        await hold(30 - d if d < 30 else 0)
    await ls.tick(ops=[("latch", 0)])
    await ls.tick(read_shadow=True)
    n_pass, n_viol, n_indet = ls.md.ref.stats[0][:3]
    dut._log.info("i2c referee: %d PASS, %d VIOLATION, %d INDETERMINATE", n_pass, n_viol, n_indet)
    assert n_pass + n_viol + n_indet == 10
    assert n_pass >= 4 and n_viol >= 1 and n_indet >= 1


@cocotb.test()
async def no_writes_during_run_check(dut):
    """A13: from RUN until the engine runs or the check fails, image, global config and
    both expected-CRC writes are refused in hardware. Hammer all four write strobes with
    corrupting values in every cycle of the 105-cycle check: the check must still pass on
    the original image, and the image, config and expected CRCs must be unchanged
    (RTL == model every cycle, and read back directly). Done for the Referee's check, then
    for the Player's while the Referee runs (the running Referee's image is hammered too)."""
    rng = random.Random(13)
    ls = Lockstep(dut, rng)
    await ls.reset()
    src = (ROOT / "firmware" / "engine" / "uart_tx.s").read_text().replace(".equ BIT 1s/115200", ".equ BIT 23")
    imgs = [refasm.Assembler(60e6, e).image(refasm.Assembler(60e6, e).assemble(src)) for e in (0, 1)]
    glob = refasm.global_image({0: (1, 1), 1: (2, 1)})
    await ls.load(glob, imgs[0], e=0)
    # the Player's copy drives pin 1 instead of pin 0 (set 0, v -> set 1, v), claims pin 1
    imgs[1] = [(w | 1) if (w >> 11) == refasm.T.INSTR["set"]["opcode"] else w for w in imgs[1][:96]] + [0b10]
    await ls.load(None, imgs[1], e=1)
    # Engine 0's check, then engine 1's while engine 0 runs. Every write strobe is
    # hammered (to either engine, and the config) in every check cycle.
    for target in (0, 1):
        await ls.tick(ops=[("run", target)])
        hits = 0
        while ls.md.eng[target].state == "check":
            k = ls.cycle % 5
            op = [("ew", rng.randrange(97), rng.randrange(1 << 16), target), ("gw", rng.randrange(8), rng.randrange(1 << 16)),
                  ("gcrc", rng.randrange(1 << 16)), ("ecrc", rng.randrange(1 << 16), target),
                  ("ew", rng.randrange(97), rng.randrange(1 << 16), 1 - target)][k]
            if op[0] == "ew" and op[3] != target and ls.md.eng[op[3]].state == "halted":
                op = ("gw", rng.randrange(8), rng.randrange(1 << 16))  # (a halted engine may be written)
            await ls.tick(ops=[op])
            hits += 1
        assert hits >= 100, f"only {hits} check cycles hit"
        assert ls.md.eng[target].state == "running", "the check must pass on the original image"
    s = dut.u_sys
    for e in (0, 1):
        en, img = ls.engs[e], imgs[e]
        n = 64 if e == 0 else 96
        assert [int(en.prog[i].value) for i in range(n)] == img[:n]
        if e == 0:
            assert [int(en.bnd[i].value) for i in range(32)] == img[64:96]
        assert int(en.claims.value) == img[96]
        assert int(en.crc_exp_e.value) == refasm.crc16(img)
    assert [int(s.glob[i].value) for i in range(8)] == glob
    assert int(s.g_crc_exp.value) == refasm.crc16(glob)


@cocotb.test()
async def lockstep_back_to_back_judges(dut):
    """JUDGE is two pipeline cycles (src/engine.v): cycle 2 does the verdict, the push and
    its FIFO-full check, VERDICT_OVF, the stats update and the L-bit clear together. This
    runs eight JUDGEs in a row on every clock rise (mixed raw / push modes / stats and
    non-stats slots), so cycle 2 of one overlaps cycle 1 of the next, with clock rises
    close enough to lose triggers (L bit), and the host popping, latching and clearing
    stats and clearing status in the same cycles. RTL == model every cycle, and every
    popped verdict slot (header with L, and raw m) must equal the model's."""
    rng = random.Random(21)
    ls = Lockstep(dut, rng)
    await ls.reset()
    src = """
.bound 0, min, 100ns
.bound 1, max, 200ns
.bound 2, min, 50ns
.bound 6, max, 1us             ; slot 6: no stats
        trig 1, rise
loop:   tmon 1023, x16
        waits
        jt   loop
        tsget X, 0, last
        tsget Y, 1, rise
        judge 0, raw           ; eight JUDGEs back to back: up to 12 slots per rise
        judge 1
        judge 2, raw, nonpass
        judge 6, raw
        judge 0, none
        judge 1, raw
        judge 2
        judge 6, nonpass
        rel
        jmp  loop
"""
    a = refasm.Assembler(60e6)  # cycle-level test: the spec's 60 MHz numbers
    img = a.image(a.assemble(src))
    await ls.load(refasm.global_image(), img)
    await ls.tick(ops=[("run",)])
    for _ in range(110):
        await ls.tick()
    assert ls.md.ref.state == "running"

    # instrument the model: what the host pops, and when VERDICT_OVF rises
    popped = []
    pop0 = ls.md.verdict_pop

    def pop():
        v = pop0()
        popped.append(v)
        return v

    ls.md.verdict_pop = pop
    j2 = ls.md.ref._judge2
    l_reports = []  # cycle-2s that reported (and cleared) lost_since = 1

    def judge2(jp):
        l_reports.append(ls.md.ref.lost_since)
        j2(jp)

    ls.md.ref._judge2 = judge2
    ovf = 0
    sda = scl = 0
    for k in range(100):
        sda ^= rng.random() < 0.7
        gap = rng.choice([1, 3, 6, 12, 20])  # data edge this many cycles before the clock rise
        for c in range(gap + rng.choice([2, 4, 6, 10, 25, 40])):  # short periods lose triggers
            if c == gap:
                scl = 1
            elif c == gap + 2:
                scl = 0
            ops = []
            r = rng.random()
            if r < 0.30:
                ops.append(("vf",))
            elif r < 0.33:
                ops.append(("latch", rng.randrange(4)))
            elif r < 0.35:
                ops.append(("sclear", rng.randrange(4)))
            elif r < 0.37:
                ops.append(("stclear",))
            await ls.tick(ui=(sda << 4) | (scl << 5), ops=ops, read_shadow=ls.latched)
            ls.latched = bool(ops) and ops[0][0] == "latch"
            ovf += ls.md.ref.sticky["VERDICT_OVF"]
    hdrs = [v for v in popped if v is not None]
    dut._log.info("back-to-back judges: %d JUDGEs, %d with L = 1, %d slots popped, LOST %d, "
                  "%d cycles with VERDICT_OVF", len(l_reports), sum(l_reports), len(hdrs), ls.md.ref.lost, ovf)
    assert len(l_reports) >= 400, "eight JUDGEs per clock rise that wasn't lost"
    assert len(hdrs) > 100, "the host should drain plenty of verdicts"
    assert ovf > 0, "the FIFO should overflow (eight JUDGEs per clock rise)"
    assert ls.md.ref.lost > 0 and sum(l_reports) > 0, "close clock rises should lose triggers, reported in L"


@cocotb.test()
async def lockstep_player_alongside_referee(dut):
    """The Player's referee instructions while the Referee judges. The Referee runs the
    section 5.3 referee (firmware/engine/i2c_setup_referee.s) on a synthetic bus; the
    Player runs random 96-word programs in which half the words are TRIG, REL, TSGET,
    JUDGE or WAITS. RTL == model every cycle for both engines: the Player's TRIG must not
    arm anything, its WAITS ends only by its timeout, its TSGET writes 0, its JUDGE
    neither reaches the Referee's VERDICT FIFO and stats nor sets anything, and its RUN
    (it is re-run often) must not clear edge_ts's `missed` under the running Referee.
    The Player's pc must reach words 64..95 and wrap from 95 to 0."""
    rng = random.Random(31)
    ls = Lockstep(dut, rng)
    await ls.reset()
    a = refasm.Assembler(60e6)
    ref_img = a.image(a.assemble((ROOT / "firmware" / "engine" / "i2c_setup_referee.s").read_text()))
    pins, _ = random_config(rng)
    glob = refasm.global_image(pins)
    await ls.load(glob, ref_img, e=0)
    await ls.tick(ops=[("run", 0)])
    for _ in range(110):
        await ls.tick()
    assert ls.md.ref.state == "running"
    sda = scl = 0
    wraps = waits_to = player_runs = 0
    for rnd in range(12):
        img = random_image(rng, pins, 1, referee_heavy=True)
        # words 64..95 straight-line (no jump, branch, loop or halt) and an occasional
        # jump to 63, so the pc runs through the top bank and wraps from 95 to 0
        ctl = {T.INSTR[m]["opcode"] for m in ("jmp", "br", "loop", "halt")}
        for w in range(64, 96):
            if img[w] >> 11 in ctl:
                img[w] = 0  # nop
        for w in rng.sample(range(0, 63), 3):
            img[w] = refasm.encode_word("jmp", {"a": 63})
        if ls.md.eng[1].state != "halted":
            await ls.tick(ops=[("halt", 1)])
        await ls.load(None, img, e=1)
        await ls.tick(ops=[("run", 1)])
        for c in range(700):
            # the bus: SDA moves, then SCL rises 1..20 cycles later (ORD, PASS, VIOLATION)
            ph = c % 50
            if ph == 0:
                sda ^= 1
            if ph == rng.choice([1, 3, 8, 20]):
                scl = 1
            if ph == 35:
                scl = 0
            p1 = ls.md.eng[1]
            before = (p1.state, p1.pc)
            ops = [("in", rng.randrange(256), 1)] if rng.random() < 0.05 else []
            await ls.tick(ui=(sda << 4) | (scl << 5) | rng.randrange(16), ops=ops)
            if before[0] == "running" and before[1] == 95 and p1.pc == 0:
                wraps += 1
            if before[0] == "running" and p1.state == "running" and T.OPCODES.get(p1.img[before[1]] >> 11) == "waits" \
                    and p1.pc != before[1]:
                waits_to += 1
                assert p1.T == 1, "a Player WAITS can only end by its timeout"
        player_runs += ls.md.eng[1].ran
    st = ls.md.ref.stats[0]
    hi = sorted(p for p in ls.player_pcs if p >= 64)
    dut._log.info("player alongside referee: Referee %d PASS / %d VIOL / %d INDET, LOST %d; Player: %d pcs >= 64, "
                  "%d wraps 95 -> 0, %d WAITS timeouts", st[0], st[1], st[2], ls.md.ref.lost, len(hi), wraps, waits_to)
    assert ls.md.ref.state == "running", "the Referee kept running"
    assert st[0] > 0 and st[1] > 0 and st[2] > 0, "the Referee judged the bus (all three verdicts)"
    assert len(hi) > 16 and wraps > 0 and waits_to > 0, "the Player ran words 64..95, wrapped and timed out WAITS"
