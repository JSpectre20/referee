# Timing probes (side branches), and what they taught us

Three side branches were pushed in October 2026 only to run the hardening flow with a
different clock or flow configuration. None of them is for merging. **No more side-branch
probes from now on (decided 29 Oct):** each one cost a full CI run (gds ~1-2.5 h, precheck
~1.5 h, gl_test ~15 min), and the private repo's Actions minutes ran out in three days.
Probes now run locally (`docs/local-flow.md`).

Corners: slow = nom_slow_1p08V_125C, typ = nom_typ_1p20V_25C, fast = nom_fast_1p32V_m40C.
WS = worst setup slack. **The clock is chosen by the slow corner.**

## The probes

| Branch (commit) | What it changed | Clock | Setup WS slow / typ / fast (ns) | Slow-corner violating endpoints | Util. | Run |
| --- | --- | --- | --- | --- | --- | --- |
| `sta-60mhz` (`f41d010`) | CP3 tree (`f2f3bdb`), CLOCK_PERIOD 16.6 | 60 MHz | **-6.789** / +1.933 / +7.094 | 782 (TNS -3913) | 44.0% | 40757384069 |
| `sta-flow` (`094d8f9`) | `c74ee47` + flow settings (below), 16.6 | 60 MHz | **-2.380** / +4.497 / +8.690 | 510 (TNS -541.5) | 44.9% | 40778571484 |
| `sta-rtl` (`844957c`) | `801e728` (RTL timing fixes) + the same flow settings, 16.6 | 60 MHz | **+1.367** / +6.934 / +10.233 | **0** | 44.1% | 40781747674 |

Flow settings tried (`src/config.json`): `RUN_POST_GRT_RESIZER_TIMING: 1`,
`RUN_POST_GRT_DESIGN_REPAIR: 1`, `PL_TIMING_DRIVEN: 1`, `SYNTH_STRATEGY: "DELAY 4"`.

For comparison, the regular CI runs on `engine-v0` with the default flow:

| Commit | Clock (CLOCK_PERIOD) | Setup WS slow / typ / fast | Slow violations | Util. |
| --- | --- | --- | --- | --- |
| `f2f3bdb` (CP3) | 50 MHz (20 ns) | -4.614 / +4.672 / +10.011 | 737 | 44.0% |
| `c74ee47` (pre-CP4) | 50 MHz (20 ns) | -5.043 / +4.385 / +9.846 | 695 | 44.1% |
| `801e728` (RTL timing fixes) | 40 MHz (25 ns) | +1.013 / +9.962 / +15.268 | 0 | 43.0% |

Hold was clean everywhere (worst +0.100 ns or better, 0 violations).

## What we learned

1. **CI was not timing the clock we specified.** `CLOCK_PERIOD` was the template's 20 ns
   (50 MHz) while info.yaml said 40 MHz. Now `test/test.py` checks that they match.
2. **The typical corner hides the problem.** At 60 MHz the CP3 tree passed typ (+1.9 ns) and
   missed slow by 6.8 ns. Always read the slow corner first.
3. **Routing added ~7 ns after the last timing repair.** The default flow repairs timing
   after CTS, on placement estimates (typ est. +5.6 ns), and never again after routing (real
   +1.9 ns). Small `buf_1` fanout chains and `_1` gates driving routed wires made 2.5 ns slews.
   About 9.5 ns of each failing 22 ns path was buffer chains.
4. **The reset was not the problem** (it is a common one: a TeslaCoilerOW report). Our
   `rst_n` already goes through a 2-flop synchronizer and fans out from a flip-flop; no
   violating path started there.
5. **Two real path groups**, found by mapping the anonymous STA instances back to RTL names
   through the synthesis netlist:
   - fetch (a pc bit through 9 buffers into the 64:1 program mux) -> decode -> bounds mux ->
     verdict compare -> verdict FIFO / stats write (depth ~40);
   - the RUN check's last cycle: read mux -> CRC-16 -> compare -> a *combinational* start
     that cleared ~700 registers (a second reset driven from deep logic).
6. **Neither fix alone was enough; together they closed 60 MHz.** Flow settings alone:
   -6.8 -> -2.4 ns. RTL fixes (`801e728`: registered RUN start, pipelined JUDGE, shared
   program port, four pc copies) plus the flow settings: +1.37 ns, 0 violations.
7. **Yosys merges duplicated registers.** `(* keep *)` on the reg kept only the wire name
   (`assign pc_q0 = pc`); one `(* keep *)` always-block per copy kept the flip-flops. Always
   check the netlist, not the RTL.
8. **Gate-level on the probe netlist is real evidence.** `sta-rtl`'s gl_test passed every
   chip test except the clock check (which fails there by design: 16.6 ns vs info.yaml).
9. **Cost.** Each probe was ~4-5 runner-hours. Three branches pushing in parallel used the
   month's 2,000 minutes in three days.
