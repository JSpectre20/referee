# edge_ts: the edge-timestamp unit

`src/edge_ts.v` watches N input pins (N = 2 by default). For each pin it latches, every
cycle and without firmware, the timestamp of the last rising edge and of the last falling
edge, plus a sticky "toggled since clear" bit, and gives one-cycle `rise` / `fall` pulses that
an engine can wait on. It is the hardware the referee's verdicts rest on: a setup time becomes
one subtraction (clock-rise timestamp minus data's last-edge timestamp), and no edge is lost
while firmware is busy.

It is a standalone module, not wired into `tt_um_shreyansh_referee` yet.

## Contract

`now` is a free-running counter that increments every cycle; cycle c is the cycle with
now == c. An external edge at real time t inside cycle c is reported in cycle c+2 (c+3 if the
first synchronizer flop goes metastable and resolves to the old level), and the timestamp is
`now` of that cycle. Hence:

| Claim | Bound |
| --- | --- |
| Latency of one edge, timestamp - t | 1 to 3 cycles |
| Error of an interval between two external edges, on one pin or across pins | at most 2 cycles (the referee's u = 2) |
| Phantom edges (reported, but no real edge) | none |
| Missed edges | none |

Preconditions and rules:
- Each pin holds each level for at least 4 cycles. Shorter pulses may be missed or merged by
  the synchronizer; nothing is claimed about them.
- Out of reset, nothing is reported until the synchronizer holds the real level (3 cycles), so
  a pin that is already high is a level, not a rising edge.
- If `clear[i]` and an edge on pin i land in the same cycle, the edge wins.
- Timestamps wrap at 2^TS_W cycles; compare them by difference.

## How it is verified

**Formal** (`formal/edge_ts.sby`, harness `formal/edge_ts_formal.sv`). The harness models the
outside world below the clock: each cycle is P ticks, a pin may change at any tick of any
cycle, and a change in the last tick before the sampling edge is metastable and may resolve
either way (a free choice every time). The harness records the true time of every change on
its own and proves, by unbounded k-induction, every row of the table above, for every sequence
of edges, phases and metastable outcomes, including across counter wrap:

- `prove`: P = 4 ticks per cycle; `prove8`: P = 8, which shows the bounds don't come from the
  phase resolution.
- `cover`: the slowest (metastable) and fastest reports, a pulse width, a cross-pin interval,
  an edge beating a clear, and a measurement across counter wrap are all reachable.
- `mutant`: falling edges stamped one cycle late. The proof must fail, and it does.

This is the first step of the verdict-soundness proof: PASS and VIOLATION verdicts will be
proved sound on top of these timestamp bounds.

**Simulation** (`test/edge_ts`, cocotb). 800 edges on two pins at random picosecond times
(never aligned to the clock) are each checked against their true time. Nothing goes
metastable in simulation, so the tighter simulation bounds are checked: latency in (1, 2]
cycles (worst seen 1.999) and interval error below 1 cycle (worst seen 0.994). Further tests:
no phantom edge out of reset, latches hold between edges, and an edge beats a clear in the
same cycle.

```bash
cd test && pytest edge_ts -v
cd formal && sby -f edge_ts.sby       # prove, prove8, cover, mutant (must FAIL)
```

## Area

Generic Yosys synthesis, N = 2, TS_W = 16: about 94 cells, 74 of them flip-flops (about 37 per
pin: two 16-bit latches, three synchronizer flops, the sticky bit; plus a 2-bit warm-up
counter shared by all pins).
