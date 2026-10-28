# HANDOFF-NOW — checkpoint: engine v0 CP2 complete (2026-10-28)

**Read this first in a fresh session, then the local `referee-handoff/referee-handoff/PROGRESS.md`.**
Those are the only two status files. This one is **overwritten at every checkpoint**; both are
updated before each checkpoint commit. It covers branches/PRs, every decision and why, the exact
next task, open questions, working rules, and facts not written anywhere else in the repo.

---

## 1. Branches and pull requests

| PR | Branch | State | Contents |
| --- | --- | --- | --- |
| #1 | `seq-v0` | **merged** | four-instruction sequencer `src/seq.v`, assembler `tools/seqasm.py`, lockstep + formal |
| #2 | `edge-ts` | **merged** | `src/edge_ts.v` edge-timestamp unit; sub-cycle formal proof of its error bound |
| #3 | `verdict` | **merged** | `src/verdict.v`; verdict soundness proved at tick resolution (`formal/verdict.sby`) |
| #4 | `edge-flags` | **merged** (`62313a5`) | `rise_stale` / `fall_stale` / `missed` flags; verdict `invalid` input; sound-or-flagged proof; `glitch` negative control |
| #6 | `missed-test` | **merged** (`47c3082`) | simulation tests for `missed` and `stale` (8/8 edge_ts tests) |
| #5 | `isa-spec` | **open, APPROVED, not merged** | ISA v0.3 spec: `isa.yaml`, `docs/isa-timing.md`. No more spec rounds. `engine-v0` contains it |
| #7 | `engine-v0` | **DRAFT, in progress** | engine v0; CP1 + CP2 done. The user runs ultrareview when it is complete |

- `main` = `47c3082`. The chip on `main`: UART TX/RX + binary host port v1 + `seq` in the top module,
  at 40 MHz. Last gds green: 4,974 std cells, 9.1% of 6x4, setup slack +10.7 ns at 50 MHz, 0 DRC.
- Teammate branch `uart` (pUrGe12) is **not ours**. It conflicts with `main` in 11 files. Agreed
  process: the teammate opens a PR, Claude reviews and recommends, Shreyansh decides.

### `engine-v0` commits
| Commit | What |
| --- | --- |
| `45c8bfb` | branch = `isa-spec` + `main` |
| `ca5c96f` | spec diff: ORD window r-1..r+1; §5.4 budget ≤ 11 cycles; opcodes 0x18..0x1B reserved; ledger A18 |
| `99e71e5` | **CP1** generator, assembler, cycle model, 14 tests, firmware, CI |
| `8e5e58c` | **CP2 part 1** `src/engine.v`, `src/engine_sys.v`, lockstep RTL == model |
| `0331cf9` | first HANDOFF-NOW; CI fixes (py3.11 f-string, oss-cad-suite token) |
| (this commit) | **CP2 complete**: formal list, snapshot lemma (A16 removed), trigger-edge ORD fix, `src/snapshot.v`, §0b diffs, CI formal job, status files consolidated |

## 2. Decisions, and why

| Decision | Why |
| --- | --- |
| **ISA v0.3 approved; no more spec rounds** | Anything found during RTL goes into PR #7 as an explicit diff in `docs/isa-timing.md` §0b, called out in the PR description |
| **ORD window = cycles r-1, r, r+1** | Report latencies differ by < 2 cycles in either direction. An edge latched at r-1 may truly come after the trigger; only edges latched at ≤ r-2 are certainly before. Snapshot one cycle after r so no truly-earlier edge is missed (that would allow a false PASS) |
| **ORD excludes the trigger edge itself** (found 28 Oct in CP2) | The trigger edge is reported in r by definition, so the plain rule made `tsget Y, <trigger>` always ORD: §5.3 could never PASS. Its order relative to the trigger is known. Found by the snapshot lemma; the lockstep couldn't see it (the model had the same rule) |
| **Spec diff 1 accepted (28 Oct)**: capture at end of r+2 from the edge_ts registers = edge_ts state as of the end of r+1; S visible from r+3; §5.3 budget +1 | Content as specified; the formal property says it exactly (`src/snapshot.v`, `SNAPSHOT_PROPS`) |
| **Snapshot is its own module, `src/snapshot.v`** | The tick-level lemma then runs on exactly the RTL the engine uses |
| **Clock: 60 MHz STA target, 40 MHz fallback** | RP2 sysclk up to 2x the project clock: 60 = 120/2 at 50% duty; 40 = 120/3 at 33% (check minimum pulse width). +10.7 ns slack at 20 ns today. u = 2 cycles = 33 ns. `info.yaml` stays 40 MHz until CP6 (the chip on `main` runs at 40) |
| **Flip-flop program memory, 64 x 16 per engine** | 1-cycle taken branches need a combinational fetch; SRAM clock-to-output (~6.3 ns) would sit in that path |
| **One engine in v0, two planned** | Decided at the 8 Nov gate from the area ledger. The image format already supports two |
| **Two deadline registers** (DL schedule: MARK/WAITD; TO timeout: TMO/TMON) | A timeout can never move the schedule: drift-free UART TX with a CTS timeout |
| **PULL never stalls** (E flag + branch, sticky IN_UNDERRUN) | Every non-wait instruction keeps a fixed cost; timing never depends on host latency |
| **JUDGE push modes `all / nonpass / none` + stats** | 400 kHz I2C = 400k SCL rises/s; the host link manages ~25k-140k slots/s. Only exceptions streamed; stats count everything |
| **Image split**: global config + per-engine images, each CRC-16 | Two engines can never drive one pin; defined now so the layout never changes |
| **Epoch tags; lost triggers counted, not INDETERMINATE** | A lost trigger is coverage, not soundness |
| **Two-sided rounding for bounds** (n_lo + inexact) | One rounded value makes PASS or VIOLATION unsound. Now proved necessary: `comb_onesided` fails |
| **`p` widened to 4 bits** (uo_out[4] excluded: host UART TX) | Uses the idle uo_out pins |
| **TSGET `e = last`; `cmp imm[, hi]`; opcodes 0x18..0x1B reserved for a v1 bitstream unit** | One TSGET + JUDGE per SCL rise; I2C address match ≤ 11 cycles; no ALU → no parity/CRC until v1 (A18) |
| **Sound-or-flagged verdicts; a minimum hold time is unavoidable** | A pulse between two samples is invisible (`docs/results/glitch_counterexample.md`) |
| **Generated Verilog constants spliced between markers in `src/engine.v`** | Unsure whether the LibreLane flow resolves include paths |
| **Formal reachability tasks start running** (`ENGINE_FORMAL_START_RUNNING`, cover + tie mutant only) | Otherwise BMC spends ~106 cycles in the RUN check with an arbitrary 1 Kbit image (> 10 min, too slow for CI). `prove` uses the real check |

## 3. Exact next task: engine v0 CP3 (PR #7, target before the 8 Nov gate)

Checkpoints (commit at each, keep CI green, tick them in the PR body):
1. ~~Generator + assembler + model + docs table, with tests~~ **done (`99e71e5`)**
2. ~~Engine RTL in lockstep + the §9 formal list~~ **done (this commit)**: see §6.
3. **Next — UART TX/RX firmware on RTL and gate level.** Needs:
   - host port v2 (command table, `docs/isa-timing.md` §7), incl. the VERDICT drain and stats commands;
   - a new top: UART + host port v2 + `engine_sys`; `uo_out[4]` = host TX; other uo bits from `uo_eng`.
     seq and host port v1 leave the top;
   - `info.yaml` sources (add `snapshot.v`, `engine.v`, `engine_sys.v`, `verdict.v`, `edge_ts.v`) + pinout,
     and `docs/info.md`;
   - a rewritten chip-level `test/test.py` (also run in gl_test).
4. I2C controller and SPI modes 0-3 firmware.
5. The §5.3 referee and §5.4 I2C target fragment as firmware, tested (a lockstep run of §5.3 exists already).
6. Harden at 6x4, close STA at 60 MHz (`src/config.json` CLOCK_PERIOD ≈ 16.6; `info.yaml` clock_hz
   60000000), update the area ledger. Then the 8 Nov gate: confirm 60 MHz, decide 1 or 2 engines.

## 4. Spec diffs made during implementation
All written into `docs/isa-timing.md` §0b (and `isa.yaml` where it has text). Still to copy into the
PR #7 description at the next PR-body update: diff 1 (snapshot timing), the trigger-edge ORD fix,
lost-while-pending, and diffs 2-10.

## 5. Open questions and known gaps
- **Open question 3** (`docs/isa-timing.md` §10): count ORD (near-coincident) edges in the stats? Unanswered.
- **Ledger A2**: the verdict proof still assumes no judged pin changes in the first 6 cycles after reset.
- The tick-level lemma covers pin 0 **rises** (the judged interval is rise → rise). Falls and
  `e = last` go through the same snapshot code; their cycle-level facts are in `snap`. A fall-based
  tick-level judgment is not proved separately.
- `engine_sys` is not in the top module yet, so gds numbers don't include the engine.
- Host port v2, the fast host baud rate and the VERDICT drain are not implemented.
- RC is one 8-bit loop counter, no nesting (A15). 16-bit timestamps limit judged intervals to 273 µs at
  60 MHz (A14). No bitstream unit in v0 (A18).
- **The protocol-emulator side is behind** (the user's assessment): after engine v0, prioritize
  breadth (1-Wire, WS2812, PS/2, NEC IR, JTAG, SWD), then the v1 bitstream unit for UART 8E1 / CAN / USB-LS.
- Waiting on humans: organizer email (8x4? sealed-list hash?); teammate sign-off; teammate's `uart` PR;
  hardware order; merging PR #5.

## 6. Verification status at this checkpoint
| Suite | Status |
| --- | --- |
| `cd test && pytest tools` | 14 pass |
| `cd test && pytest engine` | 3 pass: 25 random programs, UART TX firmware, **§5.3 I2C referee** (5 PASS / 3 VIOLATION / 2 INDETERMINATE: needs all three) |
| `cd formal && sby -f engine.sby` (~16 s) | prove, cover, snap, snap_cover PASS; snap_early and tie_timeout FAIL as required |
| `cd formal && sby -f verdict.sby` | bmc4, prove4, prove8, cover4 (17/17 covers), sharp4, sharp8, comb, comb_cover PASS; u1, glitch, comb_onesided, comb_u1 FAIL as required (~28 min with -j 4; prove8 ~14 min) |
| `verilator --lint-only -Wall` (engine_sys + engine + snapshot + edge_ts + verdict) | clean |
| `cd test && pytest seq uart edge_ts`, `make` (chip level), `formal/{seq,uart,edge_ts}.sby` | unchanged since `0331cf9` (no sources of theirs changed) |

## 7. Working rules (from the user)
- **Two status files only**: this file (overwritten at each checkpoint) and the local `PROGRESS.md`.
  **Update both before every checkpoint commit.**
- **Branch + PR for everything**; draft PRs early (the user runs ultrareview when complete). **Never push
  to `main`** unless explicitly allowed for that specific task. No force-push: merge `origin/main` in.
- Don't track or report plan usage unless the user raises it.
- After every milestone, a Haiku subagent updates the local `CHANGES.md` (history log).
- Nothing from the local `referee-handoff/` folder goes to GitHub.
- The teammate's branches are not ours; Claude reviews and recommends, Shreyansh decides.
- Comment RTL and tests generously (the humans are learning). PR descriptions: what, why, how verified,
  what to review.
- Cleanups: never `rm` with globs near sources; list first, delete named build directories only. Guard
  empty shell variables.
- `gh pr edit` fails on a deprecated Projects (classic) field. Use
  `gh api -X PATCH repos/JSpectre20/referee/pulls/N -f title=... -F body=@file`.
- RTL rules: synchronous reset on every register (no `initial`), `` `default_nettype none ``, one clock,
  2-flop synchronizers on every async input, no latches / clock gating / internal tristates,
  `verilator --lint-only -Wall` clean, top `tt_um_shreyansh_referee`, new sources in `info.yaml`.
  Formal-only code sits under `` `ifdef FORMAL `` / `SNAPSHOT_PROPS` and is never synthesized.

## 8. Things that exist only outside the repo (pointers, not copies)
In the git-excluded local folder `referee-handoff/referee-handoff/` (on Shreyansh's machine):
- `PROGRESS.md`: the single status board (with this file, the only two status files);
  `CLAUDE.md`: working rules; `CHANGES.md`: dated history log.
- `docs/PLAN.md`: full strategy; §0 = plan review and decisions; §4.15 area ledger.
- `docs/research/*`: competitor survey, organizer FAQ email, external reviews, SRAM guide, platform notes.

## 9. Testbench / model / formal lessons (so they aren't re-learned)
- The model must read **start-of-cycle registered state** for anything an instruction issued in the same
  cycle could change (trigger config, run state).
- Host-op timing in `test/engine/tb_engine.py`: writes / run / halt / IN push / clears go to the model
  **after** `step()`; pops and the stats latch **before** it; op guards are evaluated **inside `tick()`
  after the compare**.
- After reset release, the RTL's first cycle runs on the next rising edge: the model takes `step(0, 0)` in
  `reset()`. Never await an extra clock edge outside `tick()`.
- CI uses Python 3.11: no nested same-type quotes inside f-strings.
- A lockstep test can't find a spec bug the model shares. Directed firmware tests with an expected
  verdict mix (like the §5.3 test) and the tick-level lemma can.
- Yosys: `$past` only inside clocked blocks (use a ghost shift register for wires). In harness arithmetic,
  widen differences by a bit: a wrapped `mt - t` made the first `comb` attempt fail spuriously.
- Ghosts with no reset (e.g. `since_r0` before the first rise) must not seed other ghosts.

## 10. Commands
```bash
source ~/venvs/tt/bin/activate
python3 tools/isagen.py --check                      # generated files up to date
cd test && pytest tools engine seq uart edge_ts -v   # unit + lockstep
cd test && make                                      # chip level (current top)
cd formal && sby -f engine.sby                       # engine + snapshot properties (~16 s)
cd formal && sby -j 4 -f verdict.sby                 # verdict suite incl. snapshot lemma + comb theorem
verilator --lint-only -Wall src/engine_sys.v src/engine.v src/snapshot.v src/edge_ts.v src/verdict.v --top-module engine_sys
python3 tools/refasm.py firmware/engine/uart_tx.s --image
gh pr checks 7
```
