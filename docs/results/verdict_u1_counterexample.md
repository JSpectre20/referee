# Counterexample: u = 1 cycle is not a sound uncertainty

Result of the negative control `u1` in `formal/verdict.sby`: the same harness that proves the
verdict rule sound with u = 2 cycles **fails** with u = 1. The full trace is
`verdict_u1_counterexample.vcd`, which opens in GTKWave or Surfer. It has two VCD timestamps
per formal step, and the formal clock runs K = 4 ticks per chip cycle. The cycle counter starts at an
arbitrary value, so the cycle numbers below are those in this trace.

## What happens

| | Pin 0 (rises first) | Pin 1 (rises second) |
| --- | --- | --- |
| True rise (free input) | cycle 62, tick 1 | cycle 63, tick 2 |
| Physical edge (3-tick model delay, same for both pins) | cycle 63, tick 0: exactly at the clock edge | cycle 0, tick 1: one tick after a clock edge (the 6-bit cycle counter has just wrapped) |
| Sampled by the first synchronizer flop at the edge starting cycle 63 | old level (allowed: change within one tick of the edge) | n/a |
| Sampled at the edge starting cycle 0 | new level | new level (allowed: change within one tick of the edge) |
| Reported by `edge_ts` | cycle 1 | cycle 1 |

- **True interval** T = 5 ticks = 1.25 cycles.
- **Measured interval** m = 1 - 1 = 0 cycles.
- **Bound** (free choice of the solver): a maximum of N = 4 ticks = 1 cycle.
  The true interval *breaks* it (5 > 4).
- **Verdict with u = 1:** m + u = 0 + 1 cycle = 4 ticks <= N = 4 ticks, so the rule says
  **PASS**, which is wrong. (The failed assertion is `max_cross && pass_x -> t_cross <= n_cross`
  in `formal/verdict_formal.sv`.) An earlier version of the harness produced the mirror image:
  a minimum bound wrongly judged VIOLATION.

## Why

Each edge's report latency depends on where it falls relative to the clock edge. An edge just
before a clock edge can be captured one cycle late. An edge just after one can be captured
"early", within the same flop's setup/hold window. At K ticks per cycle with a window of one
tick each side, the latency of a single edge lies in [1 - 1/K, 2 + 1/K] cycles. The error of an
interval between two external edges can therefore reach 1 + 2/K cycles: 1.5 cycles at K = 4,
and slightly more than 1 cycle for any real window. So **no u of 1 cycle or less is sound**,
and the smallest whole-cycle uncertainty that is sound is **u = 2**, which the harness proves.

Reproduce: `cd formal && sby -f verdict.sby u1` (it must report FAIL; CI checks that it does).
