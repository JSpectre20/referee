# Referee engine: ISA and timing model (v0.2 DRAFT, for review)

`isa.yaml` is the single source of truth: opcodes, fields, cycle costs, state. In step 2 a
generator produces the RTL opcode include, the assembler, the reference model, and the
instruction table below from it. This document explains the decisions, states the timing
contract as numbers, and keeps the **assumption ledger** (section 10). The engine grows out of
`seq` and adds deadlines, waits with timeouts, branches, FIFOs, atomic timestamp snapshots,
and the referee instructions TSGET and JUDGE.

## 0. Changes since v0.1 (review of 27 Oct)

| Review item | Resolution | Where |
| --- | --- | --- |
| 1. Atomic timestamps | edge_ts takes a **snapshot** of every channel on a trigger edge set by `trig`. TSGET reads the snapshot. A trigger while a snapshot is pending keeps the old one and sets sticky SNAP_OVR, which forces INDETERMINATE. `waits` / `rel` added. Formal: snapshot = edge_ts state at the trigger. | §3.6, §5.3 |
| 2. Deadline rules | **Two registers.** DL (schedule) changes only by MARK and WAITD. TO (timeout) changes only by TMO / TMON. Waits change neither. `dl n` removed. Drift-free UART TX with a timeout shown. | §3.4, §5.1 |
| 3. PULL on empty | **Non-blocking**: E flag + branch, sticky IN_UNDERRUN for the host. Justified in §3.5; ledger A7. | §3.5, §10 |
| 4. Bounds table | The assembler fills it from **ns** with the image's clock frequency. Rounding is sound on both sides (floor + inexact bit). | §6 |
| 5. Loader | CRC-16 over the 98-word image is checked by the engine before it starts; refused on mismatch. Full readback. Writes only while halted, enforced **inside the engine**. | §7 |
| 6. §8 answers | Two engines planned (decided at the gate). Four channels on ui_in[4..7], each muxable to a uio pin; NI flag in every verdict. 10BASE-T out of v0. WAITP on channels required. | §1, §9 |
| 7. Raw m | `judge b, raw` also pushes the exact m (second FIFO slot). | §3.7 |
| Also | Four conditional branches merged into one `br c, a` with 8 conditions (T, C, E, S and their negations). | §4 |

## 1. Decisions, with evidence

| Decision | Status | Evidence / note |
| --- | --- | --- |
| STA at 60 MHz, 40 MHz fallback | Agreed | Demo-board firmware (`clock_project_PWM`) uses an RP2 system clock that's a multiple of the project clock, up to twice it: 60 MHz = 120 MHz / 2 at 50% duty. 40 MHz = 120 / 3, 33% duty, so STA must check the minimum pulse width there. The current chip has +10.7 ns setup slack at 20 ns. At 60 MHz, u = 2 cycles = 33 ns. |
| Every non-waiting instruction takes 1 cycle, incl. taken branches and LOOP | Agreed | Flip-flop program memory is read combinationally, so the next PC is known in the same cycle. Only WAITD, WAITP and WAITS stall. PULL no longer does (§3.5). |
| 64x16 flop program memory per engine | Agreed | ~1,024 flops at ~50 µm² ≈ 55k µm² plus mux. The chip today is 82k µm² = 9.1% of 6x4. An SRAM macro's ~6.3 ns clock-to-output would sit inside the 1-cycle branch path. |
| Loader | Adjusted | The binary host port on `main`, extended (§7). The teammate's text console isn't on `main`. |
| Engines | Planned 2, v0 has 1 | Second engine (another 64-word memory, ~55k µm²) decided at the 8 Nov gate from the area ledger. |
| Judge channels | Agreed | 4 channels, default ui_in[4..7] (ui_in[3] is the host UART RX). Each has a source mux: ui_in[4+ch] or uio_in[0..7], for future active modes. **Non-intrusive only when all four select ui_in**; every verdict carries the NI bit so the claim is checkable per verdict. |
| WAITP on judge pins | Required | Inputs 12..15 *are* the channels after the mux: the same synchronized signal edge_ts sees, so the engine and the timestamps agree on when an edge happened. |
| 10BASE-T | Out of v0 | A Manchester half-bit is 3 cycles at 60 MHz; firmware can't keep up (needs the PLAN's hardware serializer). |

## 2. Machine state

| State | Bits | Changed by |
| --- | --- | --- |
| PC | 6 | issue, branches, RUN (→ 0) |
| X, Y | 16 each | TSGET (and future ALU ops) |
| SR | 16 | SHO, SHI, LDSR, PULL |
| RC | 8 | LDRC, LOOP |
| DL (schedule deadline) | 16 | **MARK, WAITD only** |
| TO (timeout deadline) | 16 | **TMO, TMON only** |
| Flags T, C, E, S | 1 each | WAITP/WAITS (T); SHI/TSTP (C); PULL (E); snapshot logic, REL (S) |
| FX, FY | 1 each | TSGET |
| Sticky status: OUT_OVF, VERDICT_OVF, IN_UNDERRUN, SNAP_OVR, CRC_FAIL | 1 each | set by hardware; cleared by the host status read or a new RUN |
| NOW | 16 | +1 every cycle (shared with edge_ts) |
| Image: program 64 words, bounds 16 entries, pin modes, channel mux | 98 words | host, **only while halted** |

## 3. Timing contract (cycles of the engine clock)

### 3.1 Issue
- An instruction **issues** in the cycle it executes. Straight-line code issues one per cycle.
- Every instruction costs **exactly 1 cycle** except WAITD, WAITP and WAITS, which stall by
  definition. Branches (JMP, BR, LOOP), taken or not, cost 1 cycle.
- RUN starts with the image check (§7). If it passes, address 0 issues exactly
  **C_START = 99 cycles** after RUN is accepted (98 check cycles + 1). HALT, or a host halt,
  cancels any wait; pins keep their level.

### 3.2 Outputs
- SET / SHO issued in cycle t change the pad from **cycle t+1**, for every pin and mode. Pads
  change at no other time and never glitch.

### 3.3 Inputs
- Every input passes a 2-flop synchronizer. An external edge at time t becomes visible to
  WAITP / TSTP / SHI between 1 - 1/K and 2 + 1/K cycles later, the same window edge_ts is
  proved to have (`docs/verdict.md`).

### 3.4 Deadlines: two registers, nothing implicit
- **DL, the schedule.** `mark`: DL ← NOW. `waitd n` issued at t: DL ← DL + n; the next
  instruction issues at **max(t + 1, DL)**. Nothing else writes DL.
- **TO, the timeout.** `tmo t`: TO ← DL + t (relative to the schedule, so it's as deterministic
  as the schedule). `tmon t`: TO ← NOW + t (relative to now, for use after an input-paced
  wait). Nothing else writes TO.
  - t is 0..1023 cycles, or 0..1023 × 16 cycles with the `x16` bit: up to 16,368 cycles
    (273 µs at 60 MHz).
  - The assembler takes a time and rounds timeouts **up** to the next step, so they never fire
    early. Longer timeouts are loops (`ldrc k; again: tmon …; waitp …; jnt got; loop again`).
- **Waits:** `waitp q, v` / `waits` stall until their event (T ← 0) or **NOW - TO ≥ 0** by
  signed difference (T ← 1); if both happen in the same cycle the **event wins**. They end the
  cycle after the condition is seen (1 cycle if it already holds) and change neither DL nor TO.
- **Re-anchoring is explicit:** after an input-paced wait, firmware that wants to time from the
  event issues `mark`. Until then, DL still describes the old schedule.
- **Half-range rule:** DL and TO are valid while DL - NOW and TO - NOW are below 2^15. The
  assembler tracks every path and rejects programs that could exceed 2^14 (a conservative
  static bound).

### 3.5 FIFOs
- **PULL never stalls.** If IN has a byte: SR byte ← it, E ← 0. If empty: E ← 1, SR unchanged,
  sticky IN_UNDERRUN ← 1. Firmware branches on E.
- **Why non-blocking, not blocking at frame boundaries:**
  1. Every instruction except the three waits keeps a fixed cost, so the "1-cycle issue"
     property holds for every instruction and static timing stays complete.
  2. A blocking PULL makes program timing depend on the host (MicroPython latency is in
     milliseconds). Inside a frame that corrupts the frame; at a boundary it needs a hardware
     notion of "frame" the ISA doesn't have.
  3. The firmware states the boundary policy itself, deterministically: at a frame start,
     `pull; je idle`, where `idle` is a WAITD-paced poll loop.
  4. Underrun stays observable: E for the firmware, IN_UNDERRUN for the host.
- **PUSH never stalls:** full → byte dropped, OUT_OVF.
- **JUDGE never stalls:** full → entry dropped (both slots if raw), VERDICT_OVF.

### 3.6 Snapshots (atomic timestamps)
- `trig ch, e` arms the trigger on channel ch, edge e (1 cycle).
- When that edge is reported (the cycle edge_ts latches it):
  - If S = 0: snapshot ← every channel's {rise_ts, fall_ts, rise_stale, fall_stale, missed}
    **as latched at the end of that cycle**, and S ← 1. Channels reporting an edge in the same
    cycle contribute NOW.
  - If S = 1: the snapshot is **kept** (the firmware is still reading it), and sticky
    SNAP_OVR ← 1.
- `tsget r, ch, e` reads the snapshot, never the live latches, so all timestamps used by one
  verdict come from one instant. F(r) ← stale | missed | !S | SNAP_OVR.
- `rel` releases the snapshot (S ← 0). `waits` waits for the next one, with timeout TO.
- While SNAP_OVR is set, every JUDGE is INDETERMINATE: a trigger was lost, so the firmware may be
  pairing edges from different instants.

### 3.7 JUDGE
- `judge b[, raw]`: m ← Y - X (signed); verdict(m, bounds[b], invalid = FX | FY | SNAP_OVR)
  with the proved rule (u = 2 cycles, sharp VIOLATION, two-sided rounding of §6).
- Pushes a header slot {verdict, FX, FY, SNAP_OVR, NI, R, b, m saturated to 4 bits}. With
  `raw`, R = 1 and a second slot carries the exact m[15:0], for the vernier work. Both slots go
  in or neither does.

## 4. Instruction set

Generated from `isa.yaml` in step 2; hand-written here for review. Opcode in [15:11].

| Opcode | Syntax | Cycles | Effect |
| --- | --- | --- | --- |
| 0x00 | `nop` | 1 | - |
| 0x01 | `halt` | 1 | stop |
| 0x02 | `set p, v` | 1 | output pin p ← v (pad at t+1); open-drain: 1 releases |
| 0x03 | `sho p[, msb]` | 1 | pin p ← SR[0] or SR[15]; SR shifts, refills with 1 |
| 0x04 | `shi q[, msb]` | 1 | C ← in[q]; SR shifts it in |
| 0x05 | `tstp q` | 1 | C ← in[q] |
| 0x06 | `mark` | 1 | DL ← NOW |
| 0x07 | `tmo t[, x16]` | 1 | TO ← DL + t (× 16 with x16) |
| 0x08 | `waitd n` | ≥ 1 | DL ← DL + n; next issues at max(t+1, DL) |
| 0x09 | `waitp q, v` | ≥ 1 | until in[q] == v (T ← 0) or TO (T ← 1); event wins a tie |
| 0x0A | `waits` | ≥ 1 | until a snapshot is pending (T ← 0) or TO (T ← 1) |
| 0x0B | `jmp a` | 1 | pc ← a |
| 0x0C | `br c, a` | 1 | if c ∈ {T, !T, C, !C, E, !E, S, !S}: pc ← a (aliases `jt` … `jns`) |
| 0x0D | `loop a` | 1 | if RC != 0: RC--, pc ← a |
| 0x0E | `ldrc imm` | 1 | RC ← imm |
| 0x0F | `ldsr imm[, hi]` | 1 | SR byte ← imm |
| 0x10 | `pull[ hi]` | 1 | SR byte ← IN, E ← 0; or E ← 1 if empty |
| 0x11 | `push[ hi]` | 1 | OUT ← SR byte (dropped if full) |
| 0x12 | `trig ch, e` | 1 | snapshot trigger ← channel ch, edge e |
| 0x13 | `rel` | 1 | S ← 0 |
| 0x14 | `tsget r, ch, e` | 1 | X or Y ← snapshot timestamp; F(r) ← its flags |
| 0x15 | `judge b[, raw]` | 1 | VERDICT ← verdict(Y - X, bounds[b]) (+ raw m) |
| 0x16 | `tmon t[, x16]` | 1 | TO ← NOW + t (× 16 with x16) |

The assembler accepts time literals for `n` (`waitd 8.68us`). It converts them with the image's
clock, rounding to the nearest cycle, and reports the realized value and its error.

## 5. Programs that check the ISA

### 5.1 UART TX with flow control and a timeout, still drift-free
115200 baud at 60 MHz: `.equ BIT 521` (8.68 µs; 0.03% slow).
```
start:  pull                 ; non-blocking
        je   idle            ; nothing to send
        tmon 250us           ; CTS timeout (assembler: 15008 cycles = 938 x 16)
        waitp 8, 0           ; wait for CTS (ui_in[0]) low ...
        jt   cts_timeout     ; ... or give up
        mark                 ; <- the frame's schedule starts here: DL = t0
        set  0, 0            ; start bit: pad at t0 + 2
        waitd BIT
        ldrc 7
bit:    sho  0
        waitd BIT
        loop bit
        set  0, 1            ; stop bit
        waitd BIT
        jmp  start
idle:   mark                 ; nothing to send: poll every 10 us
        waitd 600            ; (re-anchoring is fine here: no frame is in flight)
        jmp  start
```
**Why it stays drift-free.** DL is written only by `mark` and `waitd`, and `tmon`/`waitp`
touch only TO and T. So inside a frame, DL = t0 + k·BIT exactly, and the pad changes at
t0 + 2 + k·BIT for k = 0..9, whatever the timeout code did before `mark` and however long
CTS took. With one shared register, `tmon` would have moved the schedule's anchor; that is why
there are two.

### 5.2 Schedule-relative timeout (half-duplex reply window)
```
        ...                  ; last stop bit sent: DL = end of the stop bit
        tmo  10*BIT          ; reply within 10 bit times of the stop bit (assembler: 326 x 16 = 5216)
        waitp 9, 0           ; peer's start bit on ui_in[1]
        jt   no_reply
        mark                 ; re-anchor on the peer's start bit (explicit)
```
`tmo` is relative to DL, so the window is fixed relative to the transmitted frame, not to
whenever `tmo` happened to issue.

### 5.3 Referee: I2C setup time with an atomic snapshot
Channels: 0 = SDA (ui_in[4]), 1 = SCL (ui_in[5]). Bound 0 = minimum 100 ns (Fast-mode tSU;DAT).
```
.clock_hz 60e6
.bound 0, min, 100ns         ; assembler: 6.0 cycles, exact
        trig 1, 0            ; snapshot on every SCL rising edge
loop:   tmon 250us
        waits                ; next SCL rise (or 250 us without one)
        jt   bus_idle
        tsget X, 0, 0        ; SDA's last rise   } both from the
        tsget Y, 1, 0        ; this SCL rise     } same snapshot
        judge 0, raw
        tsget X, 0, 1        ; SDA's last fall
        judge 0, raw
        rel
        jmp  loop
bus_idle: ...
```
(Firmware picks the latest SDA edge of either polarity; a stale or missed edge on either pin
forces INDETERMINATE automatically.)

## 6. Bounds from ns: sound at any clock
- The image is assembled for a clock: `.clock_hz 60e6` or `--clock-hz`. A bound is written in
  time: `.bound b, min|max, 600ns`.
- The assembler computes N = t · f in cycles and stores **n_lo = N rounded down** to 1/16 cycle,
  plus `inexact` = 1 if that rounding lost anything (then n_hi = n_lo + 1/16).
- The verdict tests each side against the bound that keeps it sound:

| Bound | PASS if | VIOLATION if |
| --- | --- | --- |
| minimum | m - u ≥ n_hi | m + u ≤ n_lo |
| maximum | m + u ≤ n_lo | m - u ≥ n_hi |

  PASS ⇒ the true interval meets the *true* bound, and VIOLATION ⇒ it breaks it, whichever
  way the conversion rounded. Rounding a single value either way would make one side unsound.
  This changes `verdict.v` slightly; the proof in step 2 covers it.
- Contracts therefore stay in ns and don't depend on the clock. The **assumption** is that the
  chip runs at the clock the image was assembled for (ledger A9).

## 7. Loader and host port

- **Image:** 98 words = program[0..63], then bounds[0..15] (2 words each: n_lo[19:4];
  {n_lo[3:0], inexact, is_max, 10 zero bits}), then pin modes (8 × 2 bits), then channel mux
  (4 × 4 bits).
- **Checksum:** CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over the 98 words in order, MSB
  first. The host writes the expected CRC.
- **RUN:** the engine reads the 98 words through its own read port (98 cycles), computes the
  CRC, and starts only on a match. Otherwise it stays halted and sets CRC_FAIL, and the RUN
  command is answered NAK. Any image write clears the "checked" state, so every RUN re-checks.
- **Writes only while halted, in hardware:** the engine gates its own write enables with
  !running. The host port's NAK is only a courtesy.
- **Readback:** every image word and the computed CRC can be read back while halted.

| Command | Bytes | Notes |
| --- | --- | --- |
| Identify | `49 00 00 00` | data 0x52 |
| Write image word | `57 addr hi lo` | addr 0..97; NAK while running |
| Read image word | `72 addr sel 00` | sel 0: high byte, 1: low byte; NAK while running |
| Expected CRC | `43 hi lo 00` | |
| Read computed CRC | `63 sel 00 00` | after a RUN attempt |
| Run / halt | `52 01 00 00` / `52 00 00 00` | RUN: ACK only if the CRC matched |
| IN push | `50 byte 00 00` | NAK if full |
| OUT pop | `70 00 00 00` | NAK if empty |
| VERDICT pop | `56 sel 00 00` | pops one 16-bit slot: sel 0 high byte (read first), 1 low byte (completes the pop) |
| Status | `51 00 00 00` | running, S, sticky bits, NI; read-and-clear of the sticky bits |

## 8. Proofs and tests in step 2

- **Formal (engine):**
  - every non-waiting instruction issues in exactly 1 cycle;
  - WAITD: next issue = max(t+1, DL);
  - DL is written only by MARK/WAITD and TO only by TMO/TMON;
  - WAITP/WAITS: the event wins a tie;
  - pads change only one cycle after SET/SHO;
  - image writes only while halted;
  - no issue unless the CRC matched;
  - JUDGE feeds the verdict unit exactly Y - X, FX | FY | SNAP_OVR, and bounds[b].
- **Formal (edge_ts snapshot):**
  - after an accepted trigger, snapshot == the edge_ts latched state;
  - the snapshot is constant while S;
  - SNAP_OVR is set iff a trigger arrives while S.
- **Formal (verdict):** the two-sided rounding rule, sound for every n_lo / inexact.
- **Lockstep:** RTL against the model generated from `isa.yaml`, on random programs.
- **Firmware:**
  - UART TX and RX;
  - JUDGE on an external UART at in-spec and off-spec baud;

  all on RTL and gate level.
- **Physical:** harden at 6x4, close STA at 60 MHz, update the area ledger.

## 9. Open questions (remaining)

1. **Two engines:** shared edge_ts with one snapshot each, or one snapshot shared by
   both? (Proposal: one edge_ts, one snapshot per engine.)
2. **SNAP_OVR clearing:** only by the host (as specified), or also by firmware after it has
   resynchronized? (Proposal: host only, so a lost trigger can't be hidden.)

## 10. Assumption ledger

Every assumption behind a timing or verdict claim, where it is used, and how it is handled.
"Flagged" means the hardware detects the violation and the verdict becomes INDETERMINATE.

| # | Assumption | Used by | Handling |
| --- | --- | --- | --- |
| A1 | Each level on a judged pin lasts ≥ 4 cycles | edge_ts / verdict proofs | **Assumed.** Some minimum is unavoidable: a pulse between two samples is invisible (`docs/results/glitch_counterexample.md`). `missed` warns at the limit. |
| A2 | No judged pin changes in the first 6 cycles after reset | verdict proof | **Assumed**; harmless in practice: timestamps are stale (→ INDETERMINATE) until the first edge |
| A3 | Zero skew between the channels' synchronizers | verdict proof | **Assumed**; real skew (tens of ps) adds to u |
| A4 | No glitch filter on judge channels | u = 2 | Design fact; a filter's fixed delay must be added to u |
| A5 | A metastable first flop resolves within one cycle | edge_ts proof | **Assumed** (standard 2-flop synchronizer argument) |
| A6 | Intervals are shorter than a quarter of the timestamp range | verdict | **Flagged** (stale); no longer assumed |
| A7 | For streaming at line rate, the host keeps IN non-empty at frame boundaries | firmware throughput | **Observable:** E (firmware) and IN_UNDERRUN (host); timing never depends on it (PULL never stalls) |
| A8 | DL and TO stay within half the 16-bit range of NOW | waits | **Enforced** by the assembler (static bound 2^14); single timeouts are at most 16,368 cycles by encoding |
| A9 | The chip runs at the clock the image was assembled for | bounds from ns, time literals | **Assumed**; the host library sets the demo-board clock from the image header |
| A10 | Non-intrusive only when all channels select ui_in | the non-intrusive claim | **Reported** per verdict (NI bit) |
| A11 | All timestamps of one verdict come from one instant | JUDGE | **Enforced** by the snapshot; a lost trigger is **flagged** (SNAP_OVR) |
| A12 | Timing contracts restart at `mark` after an input- or host-paced wait | static timing | **By construction** (DL changes only at MARK/WAITD); stated in §3.4 |
| A13 | The image in memory is the image the host meant | everything | **Enforced:** CRC check before RUN; readback |
