# seq: the four-instruction sequencer

`src/seq.v` is the seed of the Referee engine: the first block where a protocol is a
*program* rather than fixed logic. It is a standalone module with its own tests and proofs;
it is not wired into `tt_um_shreyansh_referee` yet (that happens once the UART console's
register bus is on `main`, so the host can load programs through it).

## Instructions

| Opcode | Mnemonic | Encoding | Effect | Cycles |
| --- | --- | --- | --- | --- |
| `00` | `set pin, v` | `[13:11]` pin, `[0]` v | `pins[pin] <= v` | 1 |
| `01` | `wait n` | `[13:0]` n | nothing | max(n, 1) |
| `10` | `shout pin` | `[13:11]` pin | `pins[pin] <= sr[0]`, `sr <= {1, sr[7:1]}` | 1 |
| `11` | `jump addr` | `[13:0]` addr | `pc <= addr` | 1 |

The shift register `sr` refills with 1s, so shifting past the 8 data bits produces stop
bits.

## Timing contract

These are the rules firmware may rely on. `formal/seq.sby` proves them on the RTL for
every possible program (unbounded, by k-induction), with covers showing each path is
reachable and a mutant (a WAIT one cycle too long) that the proof must reject.

- One instruction issues per cycle; `wait n` delays the next issue by exactly
  max(n, 1) cycles. There is no branch penalty.
- A `set` or `shout` issued in cycle t is visible on `pins` from cycle t+1. Pins change at
  no other time and never glitch (they are flip-flops).
- The first cycle with `run` = 1 issues address 0. `run` = 0 halts: pc goes to 0, any
  wait is cancelled, pins keep their level.
- The program memory and `sr` can only be written while halted. Writes while running are
  ignored, so a running program's timing depends on nothing but the program.
- Program memory is not reset (it stands in for an SRAM macro). Nothing should run until
  a program is loaded.

## Toolchain

```bash
python3 tools/seqasm.py firmware/seq/uart_tx.s            # hex words
python3 tools/seqasm.py firmware/seq/uart_tx.s --timing   # issue cycle of every instruction
```

`tools/seqasm.py` also holds `Seq`, a cycle-level reference model written from the
contract above (not from the RTL).

## Verification

```bash
cd test && pytest seq -v       # cocotb: UART decode + RTL/model lockstep
cd formal && sby -f seq.sby    # prove, cover, mutant (the mutant must FAIL)
```

- `firmware/seq/uart_tx.s` sends all 256 byte values, decoded by `cocotbext-uart` at
  16 cycles per bit, and decodes at the real 115200 baud (347 cycles per bit at 40 MHz).
- At 347 cycles per bit, every one of the 3470 cycles of a frame matches the expected
  level: the frame is exactly 10 x 347 cycles and starts one cycle after `run` rises.
- 40 random programs with random halts and random (ignored or accepted) writes run
  12,000 cycles in lockstep with the model: pins, pc and issue agree every cycle.

## Area

Generic Yosys synthesis: about 1,400 cells, of which 512 flip-flops and ~470 muxes are
the 32 x 16 flip-flop program memory. That memory is the part an SRAM macro could replace.
Real numbers come from the gds run once seq is in the top module.

## Known limits (deliberate, for a first block)

- No conditional branches, loop counter or input reads: programs have exactly one
  execution path, which is what makes `--timing` a complete static analysis. The full
  engine adds `WAITD`/deadlines, waits on pins with timeouts and `REP` loops.
- `wait` is relative (n cycles from now), not deadline-based; with no branches the two
  are equivalent here.
- One byte per run: the host reloads `sr` while halted. A FIFO comes with the engine.
