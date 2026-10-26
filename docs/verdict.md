# Verdict soundness

`src/verdict.v` is the referee's three-valued rule. `formal/verdict.sby` proves the rule is
**sound** when it runs on timestamps from `src/edge_ts.v`, measured against the true interval
at sub-cycle resolution:

- **VIOLATION** means the true interval really broke the bound.
- **PASS** means the true interval really met it.
- Anything the instrument can't decide is **INDETERMINATE**.

## The rule

m is the measured interval in cycles (signed, from two edge timestamps). N is the contract's
bound, in fixed point with `FRAC_W` fractional bits, so a non-integer number of cycles is
represented exactly. u is the measurement uncertainty, 2 cycles.

| Bound | PASS if | VIOLATION if (default, `SHARP = 1`) | otherwise |
| --- | --- | --- | --- |
| minimum (interval must be >= N) | m - u >= N | m + u <= N | INDETERMINATE |
| maximum (interval must be <= N) | m + u <= N | m - u >= N | INDETERMINATE |

## What is proved

Proved for every possible input sequence, by unbounded k-induction:

- minimum bound: VIOLATION => T < N, and PASS => T >= N
- maximum bound: VIOLATION => T > N, and PASS => T <= N

T is the true interval in ticks, counted by the harness from the free inputs, independently of
the design. Two kinds of interval are judged:
- a pulse width on one pin (rise to fall);
- the time from a rising edge on pin 0 to a rising edge on pin 1 (e.g. data edge to clock
  edge, for a setup time).

Each is judged against a free bound N and a free choice of minimum or maximum bound.

| Task | What | Result |
| --- | --- | --- |
| `prove4` | u = 2, K = 4 ticks per cycle, unbounded, original (non-sharp) rule | PASS |
| `prove8` | u = 2, K = 8, unbounded, original rule | PASS |
| `sharp4`, `sharp8` | the sharp VIOLATION rule (the default), K = 4 and K = 8, unbounded | PASS |
| `cover4` | every verdict, for min and max bounds; INDETERMINATE with the true interval on both sides of the bound; a measured error above one cycle in both directions | all 12 reached |
| `u1` | negative control: u = 1 | **FAILS**, as it must; the counterexample is in `docs/results/` |
| `bmc4` | bounded cross-check, 96 ticks (also passed at 160, which takes 11 min) | PASS |

## The model (what the proof assumes)

- **Time.** A formal clock ticks K times per chip clock cycle, and the chip clock rises once
  every K ticks. Each pin's true level is a free input every tick, so an edge can occur at any
  tick. Bounds N are in ticks, so a bound of, say, 1.25 cycles is exact at K = 4.
- **Near-edge capture.** If a pin changes within one tick of a chip clock edge (just before,
  at, or just after it), the first synchronizer flop captures the old or the new level, a free
  choice each time. This models a sample inside the flop's setup/hold window resolving either
  way. Otherwise it captures the level just before the edge.
- **Assumptions**, which are stated limits, not hidden ones:
  - **Zero skew between pins:** every pin's synchronizer sees the same clock edge at the same
    instant. Real routing skew is a few tens of picoseconds; it would add to the error.
  - **No glitch filter.** `edge_ts` has none. A future 3-sample filter adds a fixed delay per
    pin and must be added to u.
  - **Each level held at least 4 cycles** (the `edge_ts` precondition). Shorter pulses may be
    missed and are not judged.
  - **Interval shorter than a quarter of the timestamp range**, so timestamp wrap-around
    can't alias. At 16-bit timestamps and 40 MHz that is 0.4 ms.
- **Not modelled:** metastability lasting longer than one clock cycle (the second
  synchronizer flop is assumed to resolve), analog edge rates, and pad delays. These are
  silicon characterization questions, and a loopback self-test is planned to measure them
  (PLAN improvement 4).

## Why u = 2 (and not 1)

The latency from a true edge to the start of the cycle that reports it is between K+2 and 2K+4
ticks: 1 - 1/K to 2 + 1/K cycles, plus the harness's fixed 3-tick look-ahead. The error of an
interval is the difference of two latencies, at most 1 + 2/K cycles. That is 1.5 cycles at
K = 4, and just over 1 cycle for any non-zero capture window. So:

- u = 2 is sound (proved).
- u = 1 is not sound. The counterexample is a true 1.25-cycle interval measured as 0 cycles;
  see `docs/results/verdict_u1_counterexample.md`.
- Because the error is strictly below 2 cycles, the sharper VIOLATION rule (m + u <= N for a
  minimum, m - u >= N for a maximum) is sound too, and it is the default (`SHARP = 1`). It
  turns the boundary case into a definite VIOLATION instead of INDETERMINATE. The original rule
  (`SHARP = 0`) is proved as well (`prove4`, `prove8`).

The bound also suggests a future refinement: an uncertainty of 1 cycle plus a few ticks,
expressed in fixed point (`U_FX`), would be sound and would shrink the INDETERMINATE band by
almost half. It needs a characterized capture window from silicon or the FPGA loopback first.

## How the proof closes

Inductive strengthening invariants in `formal/verdict_formal.sv` relate the synchronizer
pipeline and the latched timestamps to the ghost history:
- the per-edge latency D is between K+2 and 2K+4 ticks;
- the ghost intervals are equal to differences of per-pin ages;
- the delay-line and synchronizer state is fixed once a pin has been stable long enough.

Each invariant is itself asserted and proved, so none of them weakens the result.

```bash
cd formal && sby -f verdict.sby          # every task; CI runs the same (.github/workflows/verdict.yaml)
```
