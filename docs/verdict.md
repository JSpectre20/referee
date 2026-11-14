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
  edge, for a setup time), **judged through the engine's snapshot** (`src/snapshot.v`), the way
  firmware does it: `trig 1, rise; waits; tsget X, 0, rise; tsget Y, 1, rise; judge`. X and Y
  come from the snapshot taken on pin 1's rise, in the cycle it becomes visible; ORD on either
  forces INDETERMINATE; REL is a free input, so snapshots can be held and triggers lost.

Each is judged against a free bound N and a free choice of minimum or maximum bound.

| Task | What | Result |
| --- | --- | --- |
| `prove4` | u = 2, K = 4 ticks per cycle, unbounded, original (non-sharp) rule | PASS |
| `prove8` | u = 2, K = 8, unbounded, original rule | PASS |
| `sharp4`, `sharp8` | the sharp VIOLATION rule (the default), K = 4 and K = 8, unbounded | PASS |
| `cover4` | every verdict, for min and max bounds; INDETERMINATE with the true interval on both sides of the bound; a measured error above one cycle in both directions; a long pulse flagged stale | all 17 reached (incl. ORD on both sides of the trigger, a pin-0 rise too late for the snapshot, a lost trigger) |
| `u1` | negative control: u = 1 | **FAILS**, as it must; counterexample in `docs/results/verdict_u1_counterexample.md` |
| `glitch` | negative control: no hold precondition (levels may last one tick) | **FAILS**, as it must; counterexample in `docs/results/glitch_counterexample.md` |
| `bmc4` | bounded cross-check, 96 ticks (also passed at 160, which takes 11 min) | PASS |
| `comb` | the combinational theorem: `verdict.v` alone, engine parameters, every m, n_lo, inexact, is_max and true T with \|m - T\| < 2 cycles | PASS |
| `comb_cover` | every verdict reachable in that theorem, with inexact bounds | all reached |
| `comb_onesided` | negative control: the block sees only the rounded-down bound | **FAILS**, as it must: two-sided rounding is needed |
| `comb_u1` | negative control: u = 1 in the theorem | **FAILS**, as it must |

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
  silicon characterization questions; a loopback self-test (drive an edge at a known cycle,
  read it back on an input) is planned to measure them.

## The snapshot lemma (ledger A16, removed 28 Oct)

Until engine-v0 CP2 the cross interval was judged in the cycle pin 1's rise was reported, and
only if pin 0's rise "had been reported already" (`!pend_r0 || rise[0]`). That was a condition
on the harness, not something hardware enforced, and it hid a real gap: report latencies differ
by up to 1 + 2/K cycles, so an earlier edge can be latched a cycle *after* the trigger.

Now the interval is judged from the engine's snapshot and there is no such condition. The
harness proves, in the cycle the snapshot becomes visible:

- **Y is the trigger edge**: its timestamp belongs to pin 1's last true rise, it is fresh, and
  it is **never ORD**. (Writing this proof found that the RTL flagged the trigger edge itself
  as ORD, which made every judgment that uses it INDETERMINATE. Fixed in `src/snapshot.v`;
  see `docs/isa-timing.md` §0b.)
- **Completeness**: X is never older than pin 0's last true rise at or before the trigger. Every
  edge that truly came before the trigger is in the snapshot.
- **Order**: if X is not ORD, X *is* that rise. An edge latched at r - 2 or earlier truly came
  before the trigger.
- If pin 0 never rose before the trigger, X is stale or ORD, so the verdict is INDETERMINATE.
- With these, the cross verdict is sound against the true interval (min and max bounds).

The cycle-level facts the lemma rests on (the capture equals the edge_ts state as of the end of
cycle r + 1; ORD is exactly the reports in r - 1..r + 1 other than the trigger edge; the
snapshot is constant while S) are proved separately with every input free (`formal/engine.sby`,
task `snap`).

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
- for the snapshot: pin 0's previous rise is still latched while its newest one is unreported,
  two rises of one pin are at least 2H ticks apart, and the trigger rise is recent while a
  capture is in flight;
- the delay-line and synchronizer state is fixed once a pin has been stable long enough.

Each invariant is itself asserted and proved, so none of them weakens the result.

```bash
cd formal && sby -f verdict.sby          # every task; CI runs the same (.github/workflows/verdict.yaml)
```
