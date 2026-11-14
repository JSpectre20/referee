#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""SPIKE copy of tools/refasm.py for the ISA v1 candidates (spike/isa_v1.yaml; tables in
spike/isa_tables_v1.py, made by spike/gen.py). `diff tools/refasm.py spike/asm_v1.py` shows
exactly what the candidates change in the assembler:
  - 128 program words (the Player's maximum; the Referee has 64): the size is reported;
  - optional enum operands in any order (sho p, crc / bsc sho, zeros, can);
  - trig takes a channel mask (CH0..CH3 predefined) and rise | fall | any;
  - .bound idx, min|max, time[, g_minus[, g_plus]]: the guard band (candidate D). The pad
    interval may read up to g_plus longer, or up to g_minus shorter, than the interval the
    spec defines; n_lo = floor(N - g_minus), width = ceil(N + g_plus) - n_lo, both in 1/16
    cycle; n_lo is signed; a width over 1023 is refused, never clipped.

Original docstring:

Assembler for the Referee engine (ISA in isa.yaml; encoding tables generated
into tools/isa_tables.py by tools/isagen.py).

Source syntax, one instruction per line, `;` or `#` starts a comment:

    .clock_hz 60e6             clock the image is built for (needed for time literals)
    .equ BIT 521               constant
    .bound 0, min, 100ns       bounds-table entry: index, min|max, time
    loop:                      label
        waitd 8.68us           time literals: ns, us, ms (converted with .clock_hz)
        tmon 250us             timeouts are rounded UP (and use x16 when needed)
        jnc  loop              aliases: jt jnt jc jnc je jne js jns  (= br c, a)
        tsget X, 0, last       enums: X/Y, rise/fall/last, all/nonpass/none
        judge 0, raw, nonpass  1-bit options as bare keywords: msb, hi, raw, x16

`waitd` longer than 2047 cycles is split into consecutive WAITDs (exact: each
one adds to the deadline); the assembler reports how many words that costs.

    python3 tools/refasm.py prog.s [--clock-hz 60e6]          words, one per line (hex)
    python3 tools/refasm.py prog.s --image                    the 97-word engine image + CRC
"""

import argparse
import ast
import operator
import re
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isa_tables_v1 as T  # noqa: E402

PROGRAM_WORDS = 128  # v1: the address field is 7 bits; the Referee engine has 64, the Player 96 or 128
WIDTH_MAX = (1 << 10) - 1  # guard-band width field, in 1/16 cycle
BOUNDS = 16
IMAGE_WORDS = PROGRAM_WORDS + 2 * BOUNDS + 1  # + claimed-pin mask
GLOBAL_WORDS = 8
FRAC = 16  # bounds are in 1/16 cycle
N_MAX = (1 << 11) - 1
T_MAX = (1 << 10) - 1
HALT_WORD = T.INSTR["halt"]["opcode"] << T.OPCODE_SHIFT


class AsmError(Exception):
    pass


# ----------------------------------------------------------------------------
# Numbers, times, expressions
# ----------------------------------------------------------------------------

_TIME = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(ns|us|ms|s)\b")
_UNIT = {"ns": Fraction(1, 10**9), "us": Fraction(1, 10**6), "ms": Fraction(1, 10**3), "s": Fraction(1)}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.LShift: operator.lshift, ast.RShift: operator.rshift,
        ast.BitOr: operator.or_, ast.BitAnd: operator.and_}


def evaluate(expr, names):
    """Exact (Fraction) value of a small expression. Time literals must already be replaced."""

    def walk(n):
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return Fraction(str(n.value)) if isinstance(n.value, float) else Fraction(n.value)
        if isinstance(n, ast.Name):
            if n.id not in names:
                raise AsmError(f"unknown name {n.id!r}")
            return Fraction(names[n.id])
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = walk(n.left), walk(n.right)
            if type(n.op) in (ast.LShift, ast.RShift, ast.BitOr, ast.BitAnd, ast.FloorDiv):
                if a.denominator != 1 or b.denominator != 1:
                    raise AsmError(f"integer operator on a non-integer in {expr!r}")
                return Fraction(_OPS[type(n.op)](int(a), int(b)))
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -walk(n.operand)
        raise AsmError(f"unsupported expression {expr!r}")

    try:
        return walk(ast.parse(expr.strip(), mode="eval"))
    except SyntaxError as e:
        raise AsmError(f"bad expression {expr!r}") from e


class Assembler:
    def __init__(self, clock_hz=None):
        self.clock = Fraction(str(clock_hz)) if clock_hz else None
        self.names = {"CH0": 1, "CH1": 2, "CH2": 4, "CH3": 8}  # v1: trig channel masks
        self.bounds = [(0, 0, 0)] * BOUNDS  # (n_lo, inexact, is_max)
        self.notes = []  # reports: splits, rounding

    # -- values -------------------------------------------------------------

    def cycles(self, expr):
        """Expression in cycles; time literals converted exactly with the clock."""

        def conv(m):
            if self.clock is None:
                raise AsmError(f"time literal {m.group(0)!r} needs .clock_hz or --clock-hz")
            c = Fraction(m.group(1)) * _UNIT[m.group(2)] * self.clock
            return f"({c.numerator}/{c.denominator})"

        return evaluate(_TIME.sub(conv, expr), self.names)

    def nearest(self, expr, lo, hi, what):
        """Like integer(), but a time literal may round to the nearest cycle (reported)."""
        v = self.cycles(expr)
        n = int(v + Fraction(1, 2)) if v >= 0 else -int(-v + Fraction(1, 2))
        if v.denominator != 1:
            err = float((n - v) / v) if v else 0.0
            self.notes.append(f"{expr}: {float(v):.3f} cycles -> {n} ({err:+.3%})")
        if not lo <= n <= hi:
            raise AsmError(f"{what} {n} out of range {lo}..{hi}")
        return n

    def integer(self, expr, lo, hi, what):
        v = self.cycles(expr)
        if v.denominator != 1:
            raise AsmError(f"{what} {expr!r} = {float(v)} is not an integer")
        v = int(v)
        if not lo <= v <= hi:
            raise AsmError(f"{what} {v} out of range {lo}..{hi}")
        return v

    # -- pass 1: sizes and labels -------------------------------------------

    def parse(self, text):
        lines = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = re.split(r"[;#]", raw, maxsplit=1)[0].strip()
            if line:
                lines.append((lineno, line))
        return lines

    def assemble(self, text):
        items = []  # (lineno, mnemonic, args, size)
        pc = 0
        for lineno, line in self.parse(text):
            try:
                if line.startswith("."):
                    self.directive(line)
                    continue
                m = re.match(r"^([A-Za-z_]\w*):\s*(.*)$", line)
                if m:
                    label, line = m.groups()
                    if label in self.names:
                        raise AsmError(f"{label!r} defined twice")
                    self.names[label] = pc
                    if not line:
                        continue
                mnemonic, _, rest = line.partition(" ")
                mnemonic = mnemonic.lower()
                args = [a.strip() for a in rest.split(",")] if rest.strip() else []
                size = self.size(mnemonic, args)
                items.append((lineno, mnemonic, args, size))
                pc += size
            except AsmError as e:
                raise AsmError(f"line {lineno}: {e}") from None
        if pc > PROGRAM_WORDS:
            raise AsmError(f"program is {pc} words; an engine holds {PROGRAM_WORDS}")
        words = []
        for lineno, mnemonic, args, size in items:
            try:
                ws = self.encode(mnemonic, args)
                assert len(ws) == size
                words += ws
            except AsmError as e:
                raise AsmError(f"line {lineno}: {e}") from None
        return words

    def directive(self, line):
        name, _, rest = line.partition(" ")
        if name == ".equ":
            key, expr = rest.split(None, 1)
            self.names[key] = self.cycles(expr)
        elif name == ".clock_hz":
            self.clock = Fraction(str(float(rest)))
        elif name == ".bound":
            parts = [a.strip() for a in rest.split(",")]
            if not 3 <= len(parts) <= 5:
                raise AsmError(".bound idx, min|max, time[, g_minus[, g_plus]]")
            idx, kind, t = parts[:3]
            idx = self.integer(idx, 0, BOUNDS - 1, "bound index")
            if kind not in ("min", "max"):
                raise AsmError("bound kind must be min or max")
            n = self.cycles(t) * FRAC  # exact, in 1/16 cycle
            # v1 (D): guard band. g_minus: how much SHORTER than the spec-defined interval the
            # interval at our pads can read; g_plus: how much LONGER. No guard given: both 0.
            # One guard given: it is used for both sides (the conservative reading).
            g_minus = self.cycles(parts[3]) * FRAC if len(parts) > 3 else Fraction(0)
            g_plus = self.cycles(parts[4]) * FRAC if len(parts) > 4 else g_minus
            if g_minus < 0 or g_plus < 0:
                raise AsmError("guard bands must be >= 0")
            lo, hi = n - g_minus, n + g_plus
            n_lo = lo.numerator // lo.denominator  # floor (also for negative values)
            n_hi = -((-hi.numerator) // hi.denominator)  # ceil
            width = n_hi - n_lo
            if not -(1 << 19) <= n_lo < (1 << 19):
                raise AsmError(f"bound {t} out of range")
            if width > WIDTH_MAX:
                raise AsmError(f"bound {idx}: guard band too wide ({width}/16 cycles; the limit is {WIDTH_MAX}/16)."
                               " Refused, not clipped: a clipped guard would make verdicts unsound")
            self.bounds[idx] = (n_lo & 0xFFFFF, width, int(kind == "max"))
            self.notes.append(f"bound {idx}: {t} = {float(n / FRAC):.4f} cycles -> n_lo {n_lo}/16, width {width}/16"
                              + (f" (guard -{float(g_minus / FRAC):.3f} / +{float(g_plus / FRAC):.3f} cycles)"
                                 if g_minus or g_plus else ""))
        else:
            raise AsmError(f"unknown directive {name}")

    def size(self, mnemonic, args):
        if mnemonic == "waitd" and args:
            try:
                n = self.nearest(args[0], 0, 1 << 30, "waitd")
            except AsmError:
                return 1  # label-dependent: must be small, checked in encode
            k = max(1, -(-n // N_MAX))
            if k > 1:
                self.notes.append(f"waitd {n}: split into {k} WAITDs (+{k - 1} words)")
            return k
        return 1

    # -- pass 2: encoding ---------------------------------------------------

    def encode(self, mnemonic, args):
        if mnemonic in T.ALIASES:
            return self.encode("br", [T.ALIASES[mnemonic]] + args)
        if mnemonic not in T.INSTR:
            raise AsmError(f"unknown instruction {mnemonic!r}")
        spec = T.INSTR[mnemonic]
        if mnemonic == "waitd":
            n = self.nearest(args[0], 0, 1 << 30, "waitd")
            parts = [N_MAX] * (n // N_MAX) + ([n % N_MAX] if n % N_MAX or n == 0 else [])
            return [encode_word("waitd", {"n": p}) for p in parts]
        values = {}
        required = [f for f, opt in spec["operands"] if not opt]
        optional = [f for f, opt in spec["operands"] if opt]
        if len(args) < len(required):
            raise AsmError(f"{mnemonic} needs {', '.join(required)}")
        for f, a in zip(required, args):
            values[f] = self.operand(mnemonic, f, a)
        for a in args[len(required):]:
            key = a.lower()
            enum_f = [f for f in optional if f in T.ENUMS and a in T.ENUMS[f]]  # v1: any optional enum
            if (key if key in T.FLAG_KEYWORDS else (enum_f or [None])[0]) in values:
                raise AsmError(f"{mnemonic}: operand {a!r} repeats or contradicts an earlier one")
            if key in T.FLAG_KEYWORDS and key in optional:
                values[key] = 1
            elif enum_f:
                values[enum_f[0]] = T.ENUMS[enum_f[0]][a]
            else:
                raise AsmError(f"{mnemonic}: unexpected operand {a!r}")
        if mnemonic in ("tmo", "tmon") and "x16" in values:
            values["t"] = self.integer(args[0], 0, T_MAX, "timeout (x16 units)")
        elif mnemonic in ("tmo", "tmon"):
            t = self.cycles(args[0])
            cyc = -(-t.numerator // t.denominator)  # round up: never time out early
            if cyc > T_MAX:
                units = -(-cyc // 16)
                if units > T_MAX:
                    raise AsmError(f"timeout {args[0]} = {cyc} cycles exceeds {T_MAX * 16}")
                self.notes.append(f"{mnemonic} {args[0]}: {cyc} cycles -> {units} x 16 = {units * 16}")
                values["t"], values["x16"] = units, 1
            else:
                values["t"] = cyc
        if mnemonic in ("trig", "trigq") and values.get("e") == T.ENUMS["e"]["last"]:
            raise AsmError("trig needs rise, fall or any")
        if mnemonic in ("trig", "trigq") and not re.fullmatch(r"\s*CH[0-3](\s*\|\s*CH[0-3])*\s*", args[0]):
            # v0.3 wrote a channel NUMBER here (trig 1, rise = channel 1); v1 takes a MASK. Only the
            # names CH0..CH3 joined by | are accepted, so an old source (or `.equ SCL 1`) cannot
            # silently select a different channel.
            raise AsmError("trig takes a channel mask in v1: write CH0, CH1, CH0|CH1 ... (not a channel number)")
        if mnemonic in ("trig", "trigq") and not values.get("tm"):
            raise AsmError("trig needs a non-empty channel mask (CH0 | CH1 ...)")
        if mnemonic == "tsget" and values.get("e") == T.ENUMS["e"]["any"]:
            raise AsmError("tsget needs rise, fall or last")
        if mnemonic == "shi" and values.get("sm") == T.ENUMS["sm"]["crc"]:
            raise AsmError("shi has no crc mode")
        return [encode_word(mnemonic, values)]

    def operand(self, mnemonic, f, a):
        hi, lo = T.FIELDS[f]
        width = hi - lo + 1
        if f in T.ENUMS and a in T.ENUMS[f]:
            return T.ENUMS[f][a]
        if f == "t" and mnemonic in ("tmo", "tmon"):
            return 0  # filled in by encode (rounding, x16)
        if f == "imm":
            return self.integer(a, -128, 255, "immediate") & 0xFF
        return self.integer(a, 0, (1 << width) - 1, f"operand {f}")

    # -- image --------------------------------------------------------------

    def image(self, words):
        prog = list(words) + [HALT_WORD] * (PROGRAM_WORDS - len(words))
        bw = []
        for n_lo, width, is_max in self.bounds:  # v1 (D): {n_lo[19:4]}; {n_lo[3:0], 0, is_max, width[9:0]}
            bw += [(n_lo >> 4) & 0xFFFF, ((n_lo & 0xF) << 12) | (is_max << 10) | width]
        img = prog + bw + [claimed_pins(words)]
        assert len(img) == IMAGE_WORDS
        return img


def encode_word(mnemonic, values):
    spec = T.INSTR[mnemonic]
    w = spec["opcode"] << T.OPCODE_SHIFT
    for f, _ in spec["operands"]:
        v = values.get(f, 0)
        hi, lo = T.FIELDS[f]
        if v >> (hi - lo + 1):
            raise AsmError(f"{mnemonic}: {f} = {v} doesn't fit in {hi - lo + 1} bits")
        w |= v << lo
    return w


def decode_word(w):
    """-> (mnemonic, {field: value})"""
    m = T.OPCODES.get(w >> T.OPCODE_SHIFT)
    if m is None:
        return None, {}
    vals = {}
    for f, _ in T.INSTR[m]["operands"]:
        hi, lo = T.FIELDS[f]
        vals[f] = (w >> lo) & ((1 << (hi - lo + 1)) - 1)
    return m, vals


def claimed_pins(words):
    mask = 0
    for w in words:
        m, v = decode_word(w)
        if m in ("set", "sho"):
            mask |= 1 << v["p"]
    return mask


def crc16_bytes(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final xor)."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def crc16(words):
    """CRC-16/CCITT-FALSE over 16-bit words, each word high byte first."""
    return crc16_bytes(b"".join(bytes([(w >> 8) & 0xFF, w & 0xFF]) for w in words))


def global_image(pins=None, mux=None, engines=1):
    """pins: {pin: (owner 0/1/2, mode 0 input/1 push-pull/2 open-drain)}; mux: 4 sources."""
    pins = pins or {}
    mux = mux or [0, 0, 0, 0]
    w = [0x5246, (4 << 8) | engines, 0, 0, 0, 0, 0, 0]  # v1: format 4 (the bound words changed meaning)
    for p, (owner, mode) in pins.items():
        nib = (owner << 2) | mode
        w[2 + p // 4] |= nib << (4 * (p % 4))
    for ch, src in enumerate(mux):
        w[6] |= src << (4 * ch)
    return w


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source")
    ap.add_argument("--clock-hz", type=float)
    ap.add_argument("--image", action="store_true", help="print the engine image and its CRC")
    args = ap.parse_args(argv)
    asm = Assembler(args.clock_hz)
    try:
        words = asm.assemble(Path(args.source).read_text())
    except AsmError as e:
        sys.exit(f"{args.source}: {e}")
    out = asm.image(words) if args.image else words
    for w in out:
        print(f"{w:04x}")
    if args.image:
        print(f"crc {crc16(out):04x}")
    for n in asm.notes:
        print(f"; {n}", file=sys.stderr)
    print(f"; {len(words)} of {PROGRAM_WORDS} words", file=sys.stderr)


if __name__ == "__main__":
    main()
