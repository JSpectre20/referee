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
            if k == "vf" and int(d.vf_count.value) in (0, 8):
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
    a = refasm.Assembler(None)
    words = a.assemble((ROOT / "firmware" / "engine" / "uart_tx.s").read_text().replace(".equ BIT 521", ".equ BIT 23"))
    img = a.image(words)
    glob = refasm.global_image({0: (1, 1)})
    await ls.load(glob, img)
    await ls.tick(ops=[("run",)])
    for b in (0x55, 0xA3, 0x00):
        await ls.tick(ops=[("in", b)])
    for _ in range(3 * 11 * 23 + 300):
        await ls.tick(ui=0)  # CTS (ui_in[0]) low
    assert ls.md.state == "running"
