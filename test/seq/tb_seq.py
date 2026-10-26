# SPDX-License-Identifier: Apache-2.0
"""cocotb tests for src/seq.v, the four-instruction sequencer.

Two independent oracles:
  - cocotbext-uart decodes the UART frames that firmware/seq/uart_tx.s produces,
  - the Python model in tools/seqasm.py (written from the timing contract, not
    from the RTL) must agree with the RTL on pins, pc and issue, every cycle.
"""

import random
import re
import sys
from pathlib import Path

import cocotb
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer
from cocotbext.uart import UartSink

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import seqasm  # noqa: E402

CLK_HZ = 40_000_000  # set by the clock in hdl/seq_tb.v
ADDR_W = 5
UART_SRC = (ROOT / "firmware" / "seq" / "uart_tx.s").read_text()


def uart_program(bit):
    """The UART firmware, assembled with BIT cycles per bit."""
    return seqasm.assemble(re.sub(r"^\.equ BIT .*$", f".equ BIT {bit}", UART_SRC, flags=re.M))


async def reset(dut):
    dut.run.value = 0
    dut.prog_we.value = 0
    dut.sr_we.value = 0
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1


async def load(dut, words, sr=None):
    """Write the program (and optionally the shift register) while halted.

    Unused words are filled with `jump 0` so the memory never holds X.
    words=None leaves the program alone and only writes the shift register."""
    words = [] if words is None else list(words) + [seqasm.enc_jump(0, ADDR_W)] * ((1 << ADDR_W) - len(words))
    dut.run.value = 0
    for addr, w in enumerate(words):
        await FallingEdge(dut.clk)
        dut.prog_we.value = 1
        dut.prog_addr.value = addr
        dut.prog_data.value = w
    await FallingEdge(dut.clk)
    dut.prog_we.value = 0
    if sr is not None:
        dut.sr_we.value = 1
        dut.sr_data.value = sr
        await FallingEdge(dut.clk)
        dut.sr_we.value = 0


async def run_cycles(dut, n):
    """Set run=1 and return the tx level in each of the next n cycles."""
    await FallingEdge(dut.clk)
    dut.run.value = 1
    trace = []
    for _ in range(n):
        await FallingEdge(dut.clk)
        trace.append(int(dut.tx.value))
    return trace


# -----------------------------------------------------------------------------


@cocotb.test()
async def pins_idle_after_reset(dut):
    """Out of reset the pins take PINS_RESET (tx high) and nothing issues."""
    await reset(dut)
    await FallingEdge(dut.clk)
    assert int(dut.pins.value) == 0x01
    assert int(dut.issuing.value) == 0
    assert int(dut.pc_out.value) == 0


@cocotb.test()
async def uart_all_bytes_fast(dut):
    """All 256 byte values, sent by firmware at 16 cycles/bit, decode correctly."""
    bit = 16
    await reset(dut)
    sink = UartSink(dut.tx, baud=CLK_HZ / bit, bits=8)  # after reset: tx is X before it
    words = uart_program(bit)
    await load(dut, words)
    for b in range(256):
        await load(dut, None, sr=b)  # program stays; only the byte changes
        await run_cycles(dut, 10 * bit + 4)
        dut.run.value = 0
    await Timer(1, unit="us")
    got = bytes(sink.read_nowait(sink.count()))
    assert got == bytes(range(256)), f"decoded {got.hex()}"


@cocotb.test()
async def uart_exact_frame_timing(dut):
    """Real 115200 baud program: every bit edge lands on the cycle the contract says.

    Start bit begins the cycle after run rises (pin latency 1), each bit lasts
    exactly 347 cycles, the frame lasts exactly 10 x 347 cycles."""
    bit = 347
    await reset(dut)
    await load(dut, uart_program(bit), sr=0xA5)
    trace = await run_cycles(dut, 10 * bit + 10)
    # trace[i] is the level in cycle i+1 after run rose; SET issues in cycle 0
    # and is visible from cycle 1 = trace[0].
    want_bits = [0] + [(0xA5 >> i) & 1 for i in range(8)] + [1]
    expect = [lvl for lvl in want_bits for _ in range(bit)] + [1] * 10
    assert trace == expect, "first mismatch at cycle %d" % next(i for i, (a, b) in enumerate(zip(trace, expect)) if a != b)


@cocotb.test()
async def uart_real_baud_decodes(dut):
    """The same program at 347 cycles/bit decodes at 115200 baud (0.06% error)."""
    bit = 347
    await reset(dut)
    sink = UartSink(dut.tx, baud=115200, bits=8)
    await load(dut, uart_program(bit))
    for b in (0x00, 0x55, 0xFF, 0x3C):
        await load(dut, None, sr=b)
        await run_cycles(dut, 10 * bit + 4)
        dut.run.value = 0
    await Timer(10, unit="us")
    assert bytes(sink.read_nowait(sink.count())) == bytes([0x00, 0x55, 0xFF, 0x3C])


@cocotb.test()
async def writes_ignored_while_running(dut):
    """Program and shift-register writes during a run change nothing."""
    bit = 16
    await reset(dut)
    words = uart_program(bit)
    await load(dut, words, sr=0x5A)
    await FallingEdge(dut.clk)
    dut.run.value = 1
    trace = []
    for i in range(10 * bit + 4):
        await FallingEdge(dut.clk)
        trace.append(int(dut.tx.value))
        dut.prog_we.value = 1
        dut.prog_addr.value = i % (1 << ADDR_W)
        dut.prog_data.value = seqasm.enc_set(0, 0)
        dut.sr_we.value = 1
        dut.sr_data.value = 0x00
    dut.prog_we.value = 0
    dut.sr_we.value = 0
    want_bits = [0] + [(0x5A >> i) & 1 for i in range(8)] + [1]
    assert trace == [lvl for lvl in want_bits for _ in range(bit)] + [1] * 4


@cocotb.test()
async def wait_zero_and_one_cost_one_cycle(dut):
    """WAIT 0 and WAIT 1 both take exactly one cycle (documented edge case)."""
    for n in (0, 1, 2, 5):
        await reset(dut)
        prog = [seqasm.enc_set(0, 0), seqasm.enc_wait(n), seqasm.enc_set(0, 1), seqasm.enc_jump(3, ADDR_W)]
        await load(dut, prog)
        trace = await run_cycles(dut, 12)
        low = trace.index(1) - trace.index(0)
        assert low == 1 + max(n, 1), f"WAIT {n}: tx low for {low} cycles"


@cocotb.test()
async def lockstep_random_programs(dut):
    """Random programs, random halts and random writes: RTL == model every cycle."""
    rng = random.Random(1234)
    model = seqasm.Seq(addr_w=ADDR_W, pins_reset=0x01)
    await reset(dut)
    model.reset()

    def rand_word():
        op = rng.randrange(4)
        if op == seqasm.OP_SET:
            return seqasm.enc_set(rng.randrange(8), rng.randrange(2))
        if op == seqasm.OP_WAIT:
            return seqasm.enc_wait(rng.choice([0, 1, 2, 3, 7, 20]))
        if op == seqasm.OP_SHOUT:
            return seqasm.enc_shout(rng.randrange(8))
        return seqasm.enc_jump(rng.randrange(1 << ADDR_W), ADDR_W)

    cycles = 0
    for program in range(40):
        words = [rand_word() for _ in range(1 << ADDR_W)]
        await load(dut, words, sr=rng.randrange(256))
        model.load(words, sr=int(dut.dut.sr.value))  # sr was written with the load
        model.pc, model.busy_until = 0, 0
        run = 1
        for _ in range(300):
            await FallingEdge(dut.clk)
            assert int(dut.pins.value) == model.pins, f"cycle {cycles}: pins {int(dut.pins.value):02x} != model {model.pins:02x}"
            assert int(dut.pc_out.value) == model.pc, f"cycle {cycles}: pc {int(dut.pc_out.value)} != model {model.pc}"
            # Inputs for this cycle: mostly run, sometimes halt; random writes.
            if rng.random() < 0.03:
                run = 1 - run
            we, addr, data = rng.random() < 0.3, rng.randrange(1 << ADDR_W), rand_word()
            swe, sdata = rng.random() < 0.3, rng.randrange(256)
            dut.run.value = run
            dut.prog_we.value = int(we)
            dut.prog_addr.value = addr
            dut.prog_data.value = data
            dut.sr_we.value = int(swe)
            dut.sr_data.value = sdata
            await ReadOnly()
            assert int(dut.issuing.value) == int(run and model.busy_until == 0), f"cycle {cycles}: issuing"
            # The model takes the same cycle: execute (or halt), then the
            # halted-only writes the RTL accepts at this clock edge.
            model.step(bool(run))
            if not run:
                if we:
                    model.mem[addr] = data
                if swe:
                    model.sr = sdata
            cycles += 1
        await FallingEdge(dut.clk)  # leave the ReadOnly phase before writing
        model.pc, model.busy_until = 0, 0  # (the load below halts the sequencer)
        dut.prog_we.value = 0
        dut.sr_we.value = 0
    dut._log.info("lockstep: %d cycles, 40 random programs, no mismatch", cycles)
