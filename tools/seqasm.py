#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Assembler, reference model and timing listing for the seq sequencer (src/seq.v).

Assembly syntax, one instruction per line, `;` or `#` starts a comment:

    .equ  BIT 16          constant (later lines may use it in expressions)
    start:                label (may share a line with an instruction)
          set   0, 0      pins[0] <= 0
          wait  BIT-1     occupy BIT-1 cycles
          shout 0         pins[0] <= sr[0]; sr shifts right, refilling with 1
    done: jump  done      park forever

Operands are integers (decimal, 0x.., 0b..), constants, labels, or + - * //
expressions of those.

    python3 tools/seqasm.py prog.s              print hex words, one per line
    python3 tools/seqasm.py prog.s --timing     also list each instruction's issue cycle

The model in `Seq` is written from the timing contract in the header of
src/seq.v, not from the RTL, so the cocotb lockstep test compares two
independent descriptions of the same machine.
"""

import argparse
import ast
import operator
import re
import sys

OP_SET, OP_WAIT, OP_SHOUT, OP_JUMP = 0, 1, 2, 3
WAIT_MAX = (1 << 14) - 1
DEFAULT_ADDR_W = 5


class AsmError(Exception):
    pass


# ----------------------------------------------------------------------------
# Encoding
# ----------------------------------------------------------------------------


def enc_set(pin, v):
    _check(0 <= pin <= 7, f"pin {pin} out of range 0..7")
    _check(v in (0, 1), f"SET value must be 0 or 1, got {v}")
    return (OP_SET << 14) | (pin << 11) | v


def enc_wait(n):
    _check(0 <= n <= WAIT_MAX, f"WAIT {n} out of range 0..{WAIT_MAX}")
    return (OP_WAIT << 14) | n


def enc_shout(pin):
    _check(0 <= pin <= 7, f"pin {pin} out of range 0..7")
    return (OP_SHOUT << 14) | (pin << 11)


def enc_jump(addr, addr_w=DEFAULT_ADDR_W):
    _check(0 <= addr < (1 << addr_w), f"JUMP target {addr} outside the {1 << addr_w}-word memory")
    return (OP_JUMP << 14) | addr


def decode(word):
    """Return (op, pin, value_or_imm) exactly as the hardware reads the fields."""
    op = (word >> 14) & 3
    return op, (word >> 11) & 7, word & 0x3FFF


def _check(cond, msg):
    if not cond:
        raise AsmError(msg)


# ----------------------------------------------------------------------------
# Assembler (two passes: labels first, then encoding)
# ----------------------------------------------------------------------------

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.FloorDiv: operator.floordiv}


def _eval(expr, names):
    """Evaluate a small integer expression without using eval()."""

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise AsmError(f"unknown name {node.id!r}")
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        raise AsmError(f"unsupported expression {expr!r}")

    try:
        return walk(ast.parse(expr.strip(), mode="eval"))
    except SyntaxError as e:
        raise AsmError(f"bad expression {expr!r}") from e


def assemble(text, addr_w=DEFAULT_ADDR_W):
    """Assemble source text into a list of 16-bit words."""
    lines = []
    names = {}
    pc = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = re.split(r"[;#]", raw, maxsplit=1)[0].strip()
        if not line:
            continue
        try:
            if line.lower().startswith(".equ"):
                _, name, expr = line.split(None, 2)
                names[name] = _eval(expr, names)
                continue
            m = re.match(r"^([A-Za-z_]\w*):\s*(.*)$", line)
            if m:
                label, line = m.groups()
                _check(label not in names, f"{label!r} defined twice")
                names[label] = pc
                if not line:
                    continue
            mnemonic, _, args = line.partition(" ")
            lines.append((lineno, mnemonic.lower(), [a for a in args.split(",") if a.strip()]))
            pc += 1
        except AsmError as e:
            raise AsmError(f"line {lineno}: {e}") from None

    _check(pc <= (1 << addr_w), f"program has {pc} words; memory holds {1 << addr_w}")

    words = []
    for lineno, mnemonic, args in lines:
        try:
            vals = [_eval(a, names) for a in args]
            want = {"set": 2, "wait": 1, "shout": 1, "jump": 1}
            _check(mnemonic in want, f"unknown instruction {mnemonic!r}")
            _check(len(vals) == want[mnemonic], f"{mnemonic} takes {want[mnemonic]} operand(s)")
            if mnemonic == "set":
                words.append(enc_set(*vals))
            elif mnemonic == "wait":
                words.append(enc_wait(vals[0]))
            elif mnemonic == "shout":
                words.append(enc_shout(vals[0]))
            else:
                words.append(enc_jump(vals[0], addr_w))
        except AsmError as e:
            raise AsmError(f"line {lineno}: {e}") from None
    return words


# ----------------------------------------------------------------------------
# Reference model (cycle by cycle, from the timing contract)
# ----------------------------------------------------------------------------


class Seq:
    """One clock cycle per call to step(). Mirrors the ports of src/seq.v."""

    def __init__(self, addr_w=DEFAULT_ADDR_W, pins_reset=0x00):
        self.addr_w = addr_w
        self.mem = [0] * (1 << addr_w)
        self.pins_reset = pins_reset
        self.reset()

    def reset(self):
        self.pc = 0
        self.busy_until = 0  # cycles before the next instruction may issue
        self.sr = 0xFF
        self.pins = self.pins_reset

    def load(self, words, sr=None):
        for i, w in enumerate(words):
            self.mem[i] = w
        if sr is not None:
            self.sr = sr & 0xFF

    def step(self, run=True):
        """Advance one cycle; return True if an instruction issued in it."""
        if not run:
            self.pc, self.busy_until = 0, 0
            return False
        if self.busy_until:
            self.busy_until -= 1
            return False
        op, pin, imm = decode(self.mem[self.pc])
        nxt = (self.pc + 1) % (1 << self.addr_w)
        if op == OP_SET:
            self.pins = (self.pins & ~(1 << pin)) | ((imm & 1) << pin)
        elif op == OP_WAIT:
            self.busy_until = max(imm, 1) - 1  # WAIT n costs max(n, 1) cycles
        elif op == OP_SHOUT:
            self.pins = (self.pins & ~(1 << pin)) | ((self.sr & 1) << pin)
            self.sr = (self.sr >> 1) | 0x80
        else:
            nxt = imm & ((1 << self.addr_w) - 1)
        self.pc = nxt
        return True


# ----------------------------------------------------------------------------
# Static timing listing
# ----------------------------------------------------------------------------


def timing(words, addr_w=DEFAULT_ADDR_W):
    """Issue cycle of each instruction along the (branch-free) execution path.

    The sequencer has no conditional branches, so a program has exactly one
    path; follow it until an address repeats. Returns [(addr, cycle)] and the
    address where the path loops back.
    """
    seen, out, pc, t = set(), [], 0, 0
    while pc not in seen:
        seen.add(pc)
        out.append((pc, t))
        op, _, imm = decode(words[pc] if pc < len(words) else 0)
        t += max(imm, 1) if op == OP_WAIT else 1
        pc = (imm & ((1 << addr_w) - 1)) if op == OP_JUMP else (pc + 1) % (1 << addr_w)
    return out, pc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source")
    ap.add_argument("--addr-w", type=int, default=DEFAULT_ADDR_W)
    ap.add_argument("--timing", action="store_true", help="list each instruction's issue cycle")
    args = ap.parse_args(argv)
    with open(args.source) as f:
        try:
            words = assemble(f.read(), args.addr_w)
        except AsmError as e:
            sys.exit(f"{args.source}: {e}")
    if args.timing:
        path, loop = timing(words, args.addr_w)
        for addr, t in path:
            print(f"{addr:3d}  {words[addr] if addr < len(words) else 0:04x}  issues at cycle {t}")
        print(f"     then loops to address {loop}")
    else:
        for w in words:
            print(f"{w:04x}")


if __name__ == "__main__":
    main()
