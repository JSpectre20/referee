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

Proved by unbounded k-induction, for every input sequence that meets the sampling precondition
(each level held at least 4 cycles), with **no limit on the length of the interval**:

- minimum bound: VIOLATION => T < N, and PASS => T >= N
- maximum bound: VIOLATION => T > N, and PASS => T <= N

In other words, every verdict is **sound or flagged**. When a flag is set, the verdict is
INDETERMINATE, which claims nothing:

- **`rise_stale` / `fall_stale`** (per pin, in `edge_ts`): the reference timestamp is missing
  (nothing latched since reset) or older than a quarter of the timestamp range. Timestamps wrap,
  so an older one could alias; the flag turns "intervals must be short" from an assumption into
  something the hardware checks.
- **`missed`** (per pin, sticky): two transitions were in the pin's synchronizer at once, i.e. a
  level lasted about one sample period, at the edge of what can be observed.

`src/verdict.v` takes the OR of the relevant flags on its `invalid` input.

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
| `cover4` | every verdict, for min and max bounds; INDETERMINATE with the true interval on both sides of the bound; a measured error above one cycle in both directions; a long pulse flagged stale | all 13 reached |
| `u1` | negative control: u = 1 | **FAILS**, as it must; counterexample in `docs/results/verdict_u1_counterexample.md` |
| `glitch` | negative control: no hold precondition (levels may last one tick) | **FAILS**, as it must; counterexample in `docs/results/glitch_counterexample.md` |
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
  - **Each level held at least 4 cycles** (the `edge_ts` precondition). Some minimum is
    unavoidable: a level that begins and ends between two samples leaves no trace in any
    flip-flop, so no instrument can judge or flag it. The `glitch` task proves the point with
    a counterexample. The 4-cycle figure is what is proved today; the physical floor is about
    one sample period, and lowering the proved figure towards it is future work.
  - **No pin changes before the first sample after reset** (the harness keeps pins quiet
    for 6 cycles after reset). A change before the first post-reset sample is
    indistinguishable from the level the pin had at reset. Every later change is reported,
    and until then the reference timestamps are stale.
  - (Removed: the earlier "interval shorter than a quarter of the timestamp range"
    assumption. Long intervals are now flagged stale instead.)
- **Not modelled:** metastability lasting longer than one clock cycle (the second
  synchronizer flop is assumed to resolve), analog edge rates, and pad delays. These are
  silicon characterization questions, and a loopback self-test is planned to measure them
  (PLAN improvement 4).

## Why the harness's 3-tick look-ahead doesn't bias intervals

To decide whether a capture is "near an edge", the harness must know the pin level one tick
*after* the clock edge, at the moment it prepares the value the flop will sample. It does this by
treating its free inputs as running 3 ticks ahead of the physical pins: the physical level of
every pin is the free input delayed by exactly 3 ticks.

- **The shift is the same for every pin and every edge.** The whole outside world moves
  3 ticks later, rigidly. The true interval between any two edges is the difference of their
  times, so it is unchanged: T is counted from the free inputs, and the 3 ticks cancel.
- **The design never sees the free inputs**, only the delayed physical levels, through the
  capture model. No chip-side quantity is computed from the look-ahead.
- **The +3 shows up in exactly one place:** the per-edge latency D (K+2 .. 2K+4 ticks)
  measured from the free input. Intervals are differences of two such latencies, so it cancels
  there too. Measured from the physical edge, the latency is K-1 .. 2K+1 ticks, i.e.
  1 - 1/K .. 2 + 1/K cycles, which is the figure used below.

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
