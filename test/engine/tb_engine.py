# SPDX-License-Identifier: Apache-2.0
"""Engine lockstep: src/engine_sys.v against tools/refmodel.py, cycle by cycle.

Every cycle the RTL's state (run state, pc, registers, flags, snapshot S and
epoch, LOST, pads, FIFO levels, status) must equal the model's. Programs, pin
ownership, bounds, inputs and host operations are random; the firmware in
firmware/engine also runs in lockstep.
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


class Lockstep:
    def __init__(self, dut, rng):
        self.dut, self.rng = dut, rng
        self.md = Model()
        self.eng = dut.u_sys.u_eng
        self.ui = self.uio = 0
        self.cycle = 0
        self.log = []  # recent (cycle, ops, model state) for failure messages
        self.latched = False  # a stats latch happened last cycle: read the shadow

    async def reset(self):
        d = self.dut
        d.rst_n.value = 0
        for s in STROBES:
            getattr(d, s).value = 0
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
        e, d, md = self.eng, self.dut, self.md
        ob = md.observe()
        rtl = {
            "state": int(e.st.value), "pc": int(e.pc.value), "X": int(e.X.value), "Y": int(e.Y.value),
            "SR": int(e.SR.value), "RC": int(e.RC.value), "DL": int(e.DL.value), "TO": int(e.TO.value),
            "T": int(e.T.value), "C": int(e.C.value), "E": int(e.E.value), "S": int(e.S.value),
            "epoch": int(e.epoch.value), "lost": int(e.lost.value),
            "uio_out": int(d.uio_out.value), "uio_oe": int(d.uio_oe.value), "uo": int(d.uo_eng.value),
            "n_in": int(e.fin_n.value), "n_out": int(e.fout_n.value), "n_v": int(e.fv_n.value),
            "status": int(d.status.value),
        }
        want = dict(ob, state=ST[ob["state"]], status=md.status())
        if md.state != "running":  # registers only matter while running
            for k in ("X", "Y", "SR", "RC", "DL", "TO", "T", "C", "E", "pc"):
                rtl.pop(k)
                want.pop(k)
        diff = {k: (rtl[k], want[k]) for k in want if rtl[k] != want[k]}
        if diff:  # (built outside the f-string: Python 3.11 in CI has no nested quotes in f-strings)
            recent = [x for x in self.log if x[1] not in (["latch"], ["gw"], ["ew"])][-8:]
            raise AssertionError(f"cycle {self.cycle} (now {md.now}): RTL != model: {diff}; recent: {recent}")

    async def tick(self, ui=None, uio=None, ops=(), read_shadow=False):
        """One cycle: compare, drive inputs and host strobes, step the model."""
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
        e = self.eng
        live = []  # guards evaluated now, on the state this cycle starts from
        for op in ops:
            k = op[0]
            if k == "in" and int(d.in_full.value):
                continue
            if k == "out" and (int(d.out_empty.value) or int(e.fout_n.value) == 4):
                continue
            if k == "vf" and int(d.vf_count.value) == 0:
                continue
            if k == "run" and md.state != "halted":
                continue
            live.append(op)
        ops = live
        for op in ops:
            kind = op[0]
            if kind == "gw":
                d.h_gwe.value, d.h_gaddr.value, d.h_gwdata.value = 1, op[1], op[2]
                post.append(lambda a=op[1], w=op[2]: md.write_global(a, w))
            elif kind == "ew":
                d.h_we.value, d.h_addr.value, d.h_wdata.value = 1, op[1], op[2]
                post.append(lambda a=op[1], w=op[2]: md.write_engine(a, w))
            elif kind == "gcrc":
                d.h_gcrc_we.value, d.h_gcrc.value = 1, op[1]
                post.append(lambda v=op[1]: md.set_crc(0, v))
            elif kind == "ecrc":
                d.h_crc_we.value, d.h_crc.value = 1, op[1]
                post.append(lambda v=op[1]: md.set_crc(1, v))
            elif kind == "run":
                d.h_run.value = 1
                post.append(md.run)
            elif kind == "halt":
                d.h_halt.value = 1
                post.append(md.halt)
            elif kind == "in":
                d.h_in_push.value, d.h_in_data.value = 1, op[1]
                post.append(lambda b=op[1]: md.in_push(b))
            elif kind == "out":  # pop before the model's step: it reads the pre-step head
                d.h_out_pop.value = 1
                got, want = int(d.out_data.value), md.out_pop()
                assert got == want, f"cycle {self.cycle}: OUT pop {got:#x} != model {want}"
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
                post.append(md.status_clear)
        md.step(self.ui, self.uio)
        for f in post:
            f()
        if ops:
            self.log.append((self.cycle, [o[0] for o in ops], md.state))
        self.cycle += 1

    async def load(self, glob, img, gcrc=None, ecrc=None):
        for a, w in enumerate(glob):
            await self.tick(ops=[("gw", a, w)])
        for a, w in enumerate(img):
            await self.tick(ops=[("ew", a, w)])
        await self.tick(ops=[("gcrc", refasm.crc16(glob) if gcrc is None else gcrc)])
        await self.tick(ops=[("ecrc", refasm.crc16(img) if ecrc is None else ecrc)])


# -----------------------------------------------------------------------------
# Random programs
# -----------------------------------------------------------------------------


def random_word(rng):
    names = [n for n in T.INSTR if n != "halt"] + (["halt"] if rng.random() < 0.05 else [])
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
        owner = 1 if rng.random() < 0.7 else rng.choice([0, 2])
        mode = rng.choice([1, 2]) if p < 8 else rng.choice([1, 1, 1, 2])
        pins[p] = (owner, mode)
    mux = [0 if rng.random() < 0.7 else rng.randrange(1, 9) for _ in range(4)]
    return pins, mux


def random_image(rng, pins):
    a = refasm.Assembler(None)
    for b in range(16):
        a.bounds[b] = (rng.randrange(0, 600), rng.randrange(2), rng.randrange(2))
    prog = [random_word(rng) for _ in range(64)]
    img = a.image(prog)
    # Claim only owned pins most of the time, so RUN usually passes its check.
    owned = {p for p, (o, m) in pins.items() if o == 1 and (p < 8 or m == 1)}
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
        if r < 0.06:
            ops.append(("in", rng.randrange(256)))
        elif r < 0.14:
            ops.append(("out",))
        elif r < 0.22:
            ops.append(("vf",))
        elif r < 0.23:
            ops.append(("latch", rng.randrange(4)))
        elif r < 0.235:
            ops.append(("sclear", rng.randrange(4)))
        elif r < 0.24:
            ops.append(("lclear",))
        elif r < 0.245:
            ops.append(("stclear",))
        elif r < 0.247:
            ops.append(("halt",))
        elif r < 0.25:
            ops.append(("run",))
        await ls.tick(ui, uio, ops, read_shadow=ls.latched)
        ls.latched = bool(ops) and ops[0][0] == "latch"


@cocotb.test()
async def lockstep_random_programs(dut):
    """Random programs, pin configs, bounds, inputs and host traffic: RTL == model every cycle."""
    seeds = int(os.environ.get("SEEDS", "25"))
    rng = random.Random(2026)
    ls = Lockstep(dut, rng)
    await ls.reset()
    refused = started = 0
    for prog in range(seeds):
        pins, mux = random_config(rng)
        glob = refasm.global_image(pins, mux)
        img = random_image(rng, pins)
        if ls.md.state != "halted":
            await ls.tick(ops=[("halt",)])
            await ls.tick()
        bad = rng.random() < 0.1
        await ls.load(glob, img, ecrc=(refasm.crc16(img) ^ 1) if bad else None)
        await ls.tick(ops=[("run",)])
        for _ in range(110):
            await ls.tick()
        if ls.md.state == "running":
            started += 1
        else:
            refused += 1
        await random_traffic(ls, rng, 400)
    dut._log.info("lockstep: %d programs, %d started, %d refused, %d cycles", seeds, started, refused, ls.cycle)
    assert started > seeds // 2, "most random images should pass the RUN check"


@cocotb.test()
async def lockstep_firmware(dut):
    """The firmware in firmware/engine runs in lockstep too (UART TX with bytes queued)."""
    rng = random.Random(5)
    ls = Lockstep(dut, rng)
    await ls.reset()
    a = refasm.Assembler(60e6)  # cycle-level test: the spec's 60 MHz numbers
    words = a.assemble((ROOT / "firmware" / "engine" / "uart_tx.s").read_text().replace(".equ BIT 1s/115200", ".equ BIT 23"))
    img = a.image(words)
    glob = refasm.global_image({0: (1, 1)})
    await ls.load(glob, img)
    await ls.tick(ops=[("run",)])
    for b in (0x55, 0xA3, 0x00):
        await ls.tick(ops=[("in", b)])
    for _ in range(3 * 11 * 23 + 300):
        await ls.tick(ui=0)  # CTS (ui_in[0]) low
    assert ls.md.state == "running"


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
    assert ls.md.state == "running"
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
    n_pass, n_viol, n_indet = ls.md.stats[0][:3]
    dut._log.info("i2c referee: %d PASS, %d VIOLATION, %d INDETERMINATE", n_pass, n_viol, n_indet)
    assert n_pass + n_viol + n_indet == 10
    assert n_pass >= 4 and n_viol >= 1 and n_indet >= 1


@cocotb.test()
async def no_writes_during_run_check(dut):
    """A13: from RUN until the engine runs or the check fails, image, global config and
    both expected-CRC writes are refused in hardware. Hammer all four write strobes with
    corrupting values in every cycle of the 105-cycle check: the check must still pass on
    the original image, and the image, config and expected CRCs must be unchanged
    (RTL == model every cycle, and read back directly)."""
    rng = random.Random(13)
    ls = Lockstep(dut, rng)
    await ls.reset()
    a = refasm.Assembler(60e6)  # cycle-level test: the spec's 60 MHz numbers
    img = a.image(a.assemble((ROOT / "firmware" / "engine" / "uart_tx.s").read_text().replace(".equ BIT 1s/115200", ".equ BIT 23")))
    glob = refasm.global_image({0: (1, 1)})
    await ls.load(glob, img)
    await ls.tick(ops=[("run",)])
    hits = 0
    while ls.md.state == "check":
        k = ls.cycle % 4
        op = [("ew", rng.randrange(97), rng.randrange(1 << 16)), ("gw", rng.randrange(8), rng.randrange(1 << 16)),
              ("gcrc", rng.randrange(1 << 16)), ("ecrc", rng.randrange(1 << 16))][k]
        await ls.tick(ops=[op])
        hits += 1
    assert hits >= 100, f"only {hits} check cycles hit"
    assert ls.md.state == "running", "the check must pass on the original image"
    e, s = dut.u_sys.u_eng, dut.u_sys
    assert [int(e.prog[i].value) for i in range(64)] == img[:64]
    assert [int(e.bnd[i].value) for i in range(32)] == img[64:96]
    assert int(e.claims.value) == img[96]
    assert [int(s.glob[i].value) for i in range(8)] == glob
    assert int(e.crc_exp_e.value) == refasm.crc16(img) and int(s.g_crc_exp.value) == refasm.crc16(glob)


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
    assert ls.md.state == "running"

    # instrument the model: what the host pops, and when VERDICT_OVF rises
    popped = []
    pop0 = ls.md.verdict_pop

    def pop():
        v = pop0()
        popped.append(v)
        return v

    ls.md.verdict_pop = pop
    j2 = ls.md._judge2
    l_reports = []  # cycle-2s that reported (and cleared) lost_since = 1

    def judge2(jp):
        l_reports.append(ls.md.lost_since)
        j2(jp)

    ls.md._judge2 = judge2
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
            ovf += ls.md.sticky["VERDICT_OVF"]
    hdrs = [v for v in popped if v is not None]
    dut._log.info("back-to-back judges: %d JUDGEs, %d with L = 1, %d slots popped, LOST %d, "
                  "%d cycles with VERDICT_OVF", len(l_reports), sum(l_reports), len(hdrs), ls.md.lost, ovf)
    assert len(l_reports) >= 400, "eight JUDGEs per clock rise that wasn't lost"
    assert len(hdrs) > 100, "the host should drain plenty of verdicts"
    assert ovf > 0, "the FIFO should overflow (eight JUDGEs per clock rise)"
    assert ls.md.lost > 0 and sum(l_reports) > 0, "close clock rises should lose triggers, reported in L"
