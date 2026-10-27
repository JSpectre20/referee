# Counterexample: no sampled instrument can judge pulses shorter than its sample period

Result of the negative control `glitch` in `formal/verdict.sby`. It is the full verdict harness,
with every flag available, but with the hold precondition dropped to a single tick: a pin level
may last just one tick (a quarter of a clock cycle at K = 4). The task **must fail**, and it does.
The trace is `glitch_counterexample.vcd` (two VCD timestamps per formal step, K = 4 ticks per
cycle; cycle numbers are those in the trace).

## What happens

- Pin 0 truly rises at cycle 31, tick 1. Pin 1 truly rises at cycle 31, tick 2, then has a
  **2-tick low pulse**: it falls at cycle 33, tick 0 and rises again at cycle 33, tick 2.
- The low pulse falls entirely between two samples of the synchronizer, so the chip never sees
  it. `missed` stays 0: nothing was sampled, so nothing can be flagged.
- The chip reports pin 1's first rise and measures pin 0 rise to pin 1 rise as m = 0 cycles.
  Against a **maximum** of N = 8 ticks (2 cycles) it says **PASS** (m + u = 2 cycles <= 2 cycles).
- In the true waveform, pin 1's latest rise came 9 ticks after pin 0's rise, which breaks the
  8-tick maximum. The edge the chip judged is no longer the latest true edge, and the chip cannot
  know that.

## Why this is fundamental

A synchronizer samples once per clock cycle. A level that begins and ends between two samples
leaves no trace in any flip-flop. No flag, counter, or rule can report it, and the same holds for
any clocked instrument, including a logic analyser. So "sound or flagged for *every* input
sequence" is impossible. The best possible statement is "sound or flagged for every input
sequence in which each level lasts long enough to be sampled". Referee states it with the
`edge_ts` precondition (each level held for at least 4 cycles). Below that, `missed` warns when
the pin is toggling at the sampling limit (two transitions in the synchronizer at once), but it
cannot catch pulses that vanish completely.

Reproduce: `cd formal && sby -f verdict.sby glitch` (it must report FAIL; CI checks that it does).
