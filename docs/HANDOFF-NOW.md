# HANDOFF-NOW — session checkpoint (2026-10-28)

**Read this first in a fresh session.** It is written so work can continue with zero loss. It
covers branches/PRs, every decision of the session and why, the exact next task, open questions,
working rules, and facts not written anywhere else in the repo. (A few items live only in local,
git-excluded notes; §8 says which.)

---

## 1. Branches and pull requests

| PR | Branch | State | Contents |
| --- | --- | --- | --- |
| #1 | `seq-v0` | **merged** | four-instruction sequencer `src/seq.v`, assembler `tools/seqasm.py`, lockstep + formal |
| #2 | `edge-ts` | **merged** | `src/edge_ts.v` edge-timestamp unit; sub-cycle formal proof of its error bound |
| #3 | `verdict` | **merged** | `src/verdict.v`; verdict soundness proved at tick resolution (`formal/verdict.sby`) |
| #4 | `edge-flags` | **merged** (`62313a5`) | `rise_stale` / `fall_stale` / `missed` flags; verdict `invalid` input; sound-or-flagged proof without the interval-length assumption; `glitch` negative control |
| #6 | `missed-test` | **merged** (`47c3082`) | simulation tests for `missed` and `stale` (8/8 edge_ts tests) |
| #5 | `isa-spec` | **open, APPROVED, not merged** | ISA v0.3 spec: `isa.yaml`, `docs/isa-timing.md`. No more spec rounds. `engine-v0` contains it |
| #7 | `engine-v0` | **DRAFT, in progress** | engine v0; the user will run ultrareview on it when complete |

- `main` = `47c3082`. The chip on `main`: UART TX/RX + binary host port v1 + `seq` in the top module.
  Last gds green: 4,974 std cells, 9.1% of 6x4, setup slack +10.7 ns at 50 MHz, 0 DRC.
- Also pushed directly to `main` earlier, with explicit permission for that task only: Claude's UART +
  host port (`1ecdc36`).
- Teammate branch `uart` (pUrGe12) is **not ours**. It conflicts with `main` in 11 files (both have
  `uart_tx.v` / `uart_rx.v`). Agreed process: the teammate opens a PR, Claude reviews and recommends,
  Shreyansh decides.

### `engine-v0` commits so far
| Commit | What |
| --- | --- |
| `45c8bfb` | branch = `isa-spec` + `main` |
| (spec diff) | ORD window r-1..r+1; §5.4 budget ≤ 11 cycles; opcodes 0x18..0x1B reserved; ledger A18 |
| `99e71e5` | **CP1** `tools/isagen.py` (+ `--check`), generated `tools/isa_tables.py`, assembler `tools/refasm.py`, cycle model `tools/refmodel.py`, `test/tools/test_isa.py` (14 tests), `firmware/engine/{uart_tx,i2c_setup_referee}.s`, CI `.github/workflows/engine.yaml` |
| `8e5e58c` | **CP2 part 1** `src/engine.v`, `src/engine_sys.v`, `edge_ts` `last_fall`, `verdict` n_lo + inexact, lockstep `test/engine/*` (RTL == model every cycle on 25 random programs + UART TX firmware) |
| (this commit) | this file; CI fixes (Python 3.11 f-string in the lockstep test; GitHub token for `setup-oss-cad-suite`, which hit the anonymous API rate limit) |

## 2. Decisions made this session, and why

| Decision | Why |
| --- | --- |
| **ISA v0.3 approved; no more spec rounds** | Third revision. Anything found during RTL goes into PR #7 as an explicit diff to `isa.yaml` / `docs/isa-timing.md` (§0b), called out in the PR description |
| **ORD window = cycles r-1, r, r+1** | Edge report latencies differ by < 2 cycles in *either* direction (latency 1-1/K .. 2+1/K). An edge latched at r-1 may truly come after the trigger; only edges latched at ≤ r-2 are certainly before. Taking the snapshot one cycle after r guarantees no truly-earlier edge is missed (which would allow a false PASS) |
| **60 MHz STA target, 40 MHz fallback** | Demo-board firmware (`clock_project_PWM`) uses an RP2 sysclk that's a multiple of the project clock, up to 2x: 60 MHz = 120/2 at 50% duty. 40 MHz = 120/3 at 33% duty (check minimum pulse width). +10.7 ns slack at 20 ns today. u = 2 cycles = 33 ns at 60 MHz |
| **Flip-flop program memory, 64 x 16 per engine** | 1-cycle taken branches need a combinational fetch; the SRAM macro's ~6.3 ns clock-to-output would sit in that path. ~55k µm² per engine is affordable (chip at 9.1% of 6x4) |
| **One engine in v0, two planned** | Decided at the 8 Nov gate from the area ledger. The image format already supports two (global config + per-engine images); one shared `edge_ts`, one snapshot + trigger per engine |
| **Two deadline registers** | DL (schedule) changes only by MARK / WAITD; TO (timeout) only by TMO / TMON. A timeout can never move the schedule, so the UART TX stays drift-free with a CTS timeout |
| **PULL never stalls** (E flag + branch, sticky IN_UNDERRUN) | Keeps every non-wait instruction at a fixed cost; timing never depends on host latency |
| **JUDGE push modes `all / nonpass / none` + stats** (bound slots 0..3: PASS/VIOL/INDET counts, min/max m; Σm, Σm² reserved) | 400 kHz I2C = 400k SCL rises/s. A 3 Mbaud host link manages ~25k slots/s with single-command pops, ~140k with the drain read. Only exceptions are streamed; stats count everything |
| **Image split**: global config (pin owner + mode, channel mux) + per-engine images (program, bounds, claimed-pin mask), each CRC-16 | Two engines can never drive one pin (RUN refuses claims of unowned pins; PIN_FAULT at run time). Defined now so the CRC layout and host scripts never change later |
| **Epoch tags on snapshot timestamps; lost triggers counted, not INDETERMINATE** | A lost trigger is a coverage loss, not a soundness one (the held snapshot is still one instant). Tags catch X and Y from different snapshots, including firmware bugs |
| **Two-sided rounding for bounds** (n_lo + inexact) | A single rounded value makes either PASS or VIOLATION unsound. PASS uses the rounded-up bound, VIOLATION the rounded-down one |
| **`p` widened to 4 bits**: uio[0..7] + uo_out[0,1,2,3,5,6,7] | Uses the idle uo_out pins. uo_out[4] is excluded: it is the host UART TX |
| **TSGET `e = last`** | One TSGET + one JUDGE per SCL rise for setup-time checks |
| **`cmp imm[, hi]`** | I2C target address match before the 9th SCL rise (≤ 11 cycles = 183 ns at 60 MHz; ~1 µs margin in Fast-mode) |
| **Opcodes 0x18..0x1B reserved for a v1 bitstream unit** | No ALU means no parity/CRC: UART 8E1, CAN, USB-LS need an LFSR tapping SHO/SHI bits, bit stuffing, NRZI. Until then the host supplies the parity bit (ledger A18) |
| **Sound-or-flagged verdicts; a minimum hold time is unavoidable** | A pulse between two samples is invisible to any sampled instrument (`docs/results/glitch_counterexample.md`) |
| **Generated Verilog constants are spliced between markers in `src/engine.v`**, not `` `include ``d | Unsure whether the Tiny Tapeout LibreLane flow resolves include paths |

## 3. Exact next task: engine v0, PR #7 (target: before the 8 Nov gate)

Checkpoints (commit at each, keep CI green, tick them in the PR body):
1. ~~Generator + assembler + model + docs table, with tests~~ **done (`99e71e5`)**
2. Engine RTL in lockstep with the model (**done, `8e5e58c`**) **+ the formal list in `docs/isa-timing.md` §9 (TODO):**
   - ~~re-run `formal/verdict.sby` after the `verdict.v` port change~~ done, all as required (§6);
   - engine properties:
     - 1-cycle issue for non-wait instructions;
     - WAITD next issue = max(t+1, DL);
     - DL only by MARK/WAITD, TO only by TMO/TMON;
     - a wait's event wins a tie;
     - pads change only one cycle after SET/SHO, and only for owned pins;
     - image writes only while halted;
     - no issue unless the RUN check passed;
     - JUDGE feeds `Y - X` and `invalid = FX|FY|OX|OY|(EX≠EY)`;
     - LOST increments iff a trigger arrives while busy, saturating;
     - HALT releases open-drain pins;
   - snapshot properties:
     - the capture equals the `edge_ts` registers in cycle r+2;
     - constant while S;
     - ORD set exactly on edges latched in r-1..r+1;
   - combinational theorem: the two-sided verdict rule is sound for any n_lo / inexact when |m - T| < 2 cycles;
   - the snapshot lemma that removes the harness condition `(!pend_r0 || rise[0])` from `formal/verdict_formal.sv` (ledger A16);
   - CI job for these.
3. UART TX/RX firmware on RTL **and gate level**. Needs:
   - host port v2 (command table, `docs/isa-timing.md` §7);
   - a new top: UART + host port v2 + `engine_sys`; `uo_out[4]` = host TX; other uo bits from `uo_eng`. seq and host port v1 leave the top;
   - `info.yaml` sources + pinout and `docs/info.md`;
   - a rewritten chip-level `test/test.py` (also run in gl_test).
4. I2C controller and SPI modes 0-3 firmware.
5. The §5.3 referee and §5.4 I2C target fragment as firmware, tested.
6. Harden at 6x4, close STA at 60 MHz (`src/config.json` CLOCK_PERIOD ≈ 16.6; `info.yaml` clock_hz 60000000), update the area ledger.

## 4. Spec diffs made during implementation (to be written into `docs/isa-timing.md` §0b and the PR #7 body — not done yet)
1. Snapshot captured at the **end of cycle r+2** from the `edge_ts` registers. They then hold exactly the
   state latched at the end of r+1, so the content matches the spec, but **S becomes visible one cycle
   later** than §3.6 implies.
2. RUN start state:
   - pc = 0; X, Y, SR, RC = 0; flags 0; **DL = TO = NOW of the first issue cycle**;
   - snapshot, epoch, trigger and `valid` cleared (TSGET before any snapshot returns 0 with F = 1);
   - sticky bits and CRC_FAIL cleared;
   - LOST and stats **not** cleared (host only); FIFO contents kept.
3. RUN clears `edge_ts` `missed` (through its `clear` input, which also clears `toggled`).
4. Pin-claim check is stricter: a claimed pin must be owned by the engine **and in a driving mode**;
   uo_out pins must be push-pull.
5. A stats clear also zeroes min/max.
6. Host clears (status read-and-clear, LOST clear) win over a same-cycle hardware set.
7. RUN check: strobe in cycle c, check cycles c+1..c+105, first issue at c+106 (= C_START).
8. `isa.yaml` gained `enums`, `flags_kw`, `aliases` (structured, non-semantic).
9. Verilog constants spliced into `src/engine.v` (see §2).
10. Host fast-baud command: designed (§7), not implemented (uart_tx/rx have a fixed CLKS_PER_BIT).

## 5. Open questions and known gaps
- **Open question 3** (`docs/isa-timing.md` §10): count ORD (near-coincident) edges in the stats? Unanswered.
- **Ledger A2**: the verdict proof still assumes no judged pin changes in the first 6 cycles after reset.
- **Ledger A16**: the merged cross-interval verdict proof relies on the harness condition above; to be
  removed by the snapshot lemma (CP2).
- The engine formal list (CP2) is not written yet.
- `engine_sys` is not in the top module yet. The chip on `main` still runs `seq` + host port v1, so
  gds numbers don't include the engine.
- Host port v2, the fast host baud rate and the VERDICT drain are not implemented.
- RC is one 8-bit loop counter, no nesting (A15). 16-bit timestamps limit judged intervals to 273 µs at
  60 MHz (A14).
- No bitstream unit in v0: no parity/CRC/stuffing/NRZI in firmware (A18).
- **The protocol-emulator side is behind** (the user's assessment): leading entries have hardened
  engines and 13-47 firmware protocols. After engine v0, prioritize breadth: 1-Wire, WS2812, PS/2, NEC IR,
  JTAG, SWD (cheap with the current ISA), then the v1 bitstream unit for UART 8E1 / CAN / USB-LS.
- Waiting on humans:
  - organizer email (is 8x4 allowed now? accept a sealed-list hash by email?);
  - teammate sign-off on the strategy decisions;
  - teammate's `uart` PR;
  - hardware order (Tiny Tapeout FPGA dev kit, logic analyzer, I2C sensor/EEPROM Pmod);
  - merging PR #5.

## 6. Verification status at this checkpoint
| Suite | Status |
| --- | --- |
| `cd test && pytest tools` | 14 pass (local + CI) |
| `cd test && pytest engine` | passes locally. Failed in CI on a Python 3.11 f-string; fixed in this commit |
| `cd test && pytest seq uart edge_ts` | pass |
| `cd test && make` (chip level, current top) | 5 pass |
| `formal/verdict.sby` after the `verdict.v` port change | **re-run 28 Oct after the port change: bmc4, prove4, prove8, cover4, sharp4, sharp8 PASS; u1 and glitch FAIL as required.** Proofs unchanged |
| `formal/{seq,uart,edge_ts}.sby` | pass (edge_ts CI failure was the setup action's API rate limit; fixed in this commit) |

## 7. Working rules (from the user)
- **Branch + PR for everything**; open the PR as a **draft early** (the user runs ultrareview when it's
  complete). **Never push to `main`** unless the user explicitly allows it for that specific task; the
  permission does not carry over. Don't rewrite pushed history (no force-push): merge `origin/main` in instead.
- **Don't track or report plan usage** unless the user raises it. When they do: work until weekly usage
  crosses 85% (never above 90%), finish running jobs, then pause and update the handoff notes.
- **After every milestone, a Haiku subagent updates the local PROGRESS.md / CHANGES.md.**
- Nothing from the local `referee-handoff/` folder goes to GitHub. That includes competitor notes, the
  organizer email and external reviews.
- The teammate's branches are not ours. If their PR conflicts with Claude's work, Claude reviews and
  recommends; Shreyansh decides.
- Comment RTL and tests generously (the humans are learning). PR descriptions: what, why, how verified,
  what to review.
- Cleanups: never `rm` with globs near sources; list first, delete named build directories only. Guard
  empty shell variables.
- `gh pr edit` fails on a deprecated Projects (classic) field. Use
  `gh api -X PATCH repos/JSpectre20/referee/pulls/N -f title=... -F body=@file`.
- RTL rules: synchronous reset on every register (no `initial`), `` `default_nettype none ``, one clock,
  2-flop synchronizers on every async input, no latches / clock gating / internal tristates,
  `verilator --lint-only -Wall` clean, top `tt_um_shreyansh_referee`, new sources in `info.yaml`.

## 8. Things that exist only outside the repo (pointers, not copies)
In the git-excluded local folder `referee-handoff/referee-handoff/` (on Shreyansh's machine):
- `docs/PLAN.md`: full strategy; §0 = plan review and decisions; area ledger.
- `docs/research/*`: competitor survey, organizer FAQ email, external reviews, SRAM guide, platform notes.
- `RESUME.md`, `PROGRESS.md`, `CHANGES.md`, `CLAUDE.md`: working state and logs.

## 9. Testbench / model lessons (so they aren't re-learned)
- The model must read **start-of-cycle registered state** for anything an instruction issued in the same
  cycle could change (trigger config, run state). A model bug of exactly this kind was found by the lockstep.
- Host-op timing in `test/engine/tb_engine.py`:
  - writes / run / halt / IN push / clears go to the model **after** `step()`;
  - pops and the stats latch go **before** it, because they read pre-step state;
  - op guards are evaluated **inside `tick()` after the compare**.
- After reset release, the RTL's first cycle runs on the next rising edge: the model takes `step(0, 0)` in
  `reset()`. Never await an extra clock edge outside `tick()`.
- CI uses Python 3.11: no nested same-type quotes inside f-strings.

## 10. Commands
```bash
source ~/venvs/tt/bin/activate
python3 tools/isagen.py --check                      # generated files up to date
cd test && pytest tools engine seq uart edge_ts -v   # unit + lockstep
cd test && make                                      # chip level (current top)
cd formal && sby -f verdict.sby                      # also seq.sby, uart.sby, edge_ts.sby
verilator --lint-only -Wall src/engine_sys.v src/engine.v src/edge_ts.v src/verdict.v --top-module engine_sys
python3 tools/refasm.py firmware/engine/uart_tx.s --image
gh pr checks 7
```
