# Referee engine: ISA and timing model (v0.3, approved 28 Oct; spec diffs since are listed in §0b)

`isa.yaml` is the single source of truth: opcodes, fields, cycle costs, state, image formats.
In step 2 a generator produces from it the RTL opcode include, the assembler, the reference model,
and the instruction table below. This document explains the decisions, states the timing contract
as numbers, and keeps the **assumption ledger** (§11).

## 0. Changes since v0.2 (review of 28 Oct)

| Review item | Resolution | Where |
| --- | --- | --- |
| 1. SNAP_OVR the wrong way round | A lost trigger no longer forces INDETERMINATE: the held snapshot is still one instant. **Epoch tags** (2 bits) go with each timestamp, and JUDGE is INDETERMINATE if X and Y come from different snapshots. A saturating **LOST** counter (host-readable, host-clearable only) and a per-verdict **L** bit report the lost coverage. | §3.6, §3.7, A11 |
| 2. Target mode | `cmp imm[, hi]`: C ← (SR byte == imm). I2C target address-match-and-ACK fragment with its cycle budget. | §5.4 |
| 3. Verdict throughput | JUDGE push mode `all / nonpass / none`; hardware **stats** for bound slots 0..3 (PASS/VIOLATION/INDETERMINATE counts, min/max m), with room for Σm and Σm². Host UART up to 3 Mbaud; bulk **drain** read. Throughput numbers corrected and worked out. | §3.7, §8 |
| 4. Two-engine image format | **Global config** (output-pin owner + mode, channel mux) plus **per-engine images** (program, bounds, claimed-pin mask), each with its own CRC. RUN refuses if an engine claims a pin it doesn't own, so two engines can never drive one pin. | §7 |
| 5. Wider `p` | 4 bits: uio[0..7] and uo_out[0,1,2,3,5,6,7]. **uo_out[4] is excluded:** it is the host UART TX. | §2, §4 |
| 6. TSGET `last` | `e` is 2 bits: rise / fall / last. §5.3 rewritten: one TSGET + one JUDGE per SCL rise. | §3.6, §5.3 |
| 7. Smaller items | Long WAITDs split by the assembler; 16-bit timestamp range in the ledger; HALT releases open-drain pins; the UART TX keeps its byte on a CTS timeout; proof plan as lemma + combinational theorem; RC has no nesting; one snapshot + trigger per engine. | §3.4, §5.1, §9, §11 |
| **Found while revising** | **Ordering gap:** a snapshot taken in the trigger's own report cycle can miss an edge that truly came *before* the trigger. Per-edge report latency varies by up to 1 + 2/K cycles, so a near-simultaneous earlier edge can be latched one cycle later, and JUDGE could then say a false PASS. The merged verdict proof only judges cross intervals once the earlier edge "has been reported", which is a harness condition, not hardware. **Fix:** take the snapshot one cycle after the trigger's report, and flag edges latched in that two-cycle window as order-ambiguous (**ORD**, forcing INDETERMINATE). | §3.6, A16 |

## 0b. Spec diffs after approval (in the engine-v0 PR)

| Diff | Why |
| --- | --- |
| ORD window is cycles **r - 1, r, r + 1** (was r, r + 1) | Review of v0.3: report latencies differ by < 2 cycles in *either* direction, so an edge latched at r - 1 may truly come after the trigger. Edges latched at r - 2 or earlier are certainly before it; taking the snapshot at r + 1 already guarantees no earlier edge is missed. |
| §5.4 budget ≤ 11 cycles (183 ns) | 2 + 1/K (visibility) + 1 (`waitp` ends) + 6 instructions + 1 (pad) ≈ 10.25 cycles |
| Opcodes 0x18..0x1B reserved for the v1 bitstream unit | LFSR CRC/parity tapping SHO/SHI bits, bit stuffing, NRZI (ledger A18) |
| **1. Snapshot timing (accepted 28 Oct).** The snapshot is loaded at the **end of cycle r + 2** from the edge_ts registers. In cycle r + 2 those registers hold exactly the **edge_ts state as of the end of cycle r + 1**, so the content is what §3.6 specifies; an edge reported in r + 2 is not in it. **S is visible from r + 3**, one cycle later than v0.3 implied. §5.3's cycle budget is +1 | Loading from the registers (not from edge_ts's next-state logic) keeps the capture path short and the snapshot a plain register copy. Formal: `formal/engine.sby` (`snap`) |
| **ORD excludes the trigger edge itself** (found while writing the CP2 proofs) | The trigger edge is reported in cycle r by definition, so the r - 1..r + 1 rule flagged it ORD, and every judgment that uses it (e.g. `tsget Y, 1, rise` in §5.3) was INDETERMINATE: the referee could never say PASS. Its order relative to the trigger is known (it *is* the trigger), so its own report in r is excluded. All other edges keep the full window. Found by the snapshot lemma; the lockstep couldn't see it because the model followed the same rule. New lockstep test runs §5.3 and needs all three verdicts |
| **Lost triggers: while S *or a capture is pending*** (r .. r + 2) | Follows from diff 1: S is not yet 1 in r + 1 and r + 2. The same trigger edge can't repeat that fast (A1), so this only matters for a TRIG to another channel |
| **2. RUN start state:** pc = 0; X, Y, SR, RC = 0; flags 0; **DL = TO = NOW of the first issue cycle**; snapshot, epoch, trigger and `valid` cleared (TSGET before any snapshot returns 0 with F = 1); sticky bits and CRC_FAIL cleared. LOST and stats are **not** cleared (host only); FIFO contents are kept | A defined start state for the proofs and the model. DL = TO = NOW makes a leading `waitd`/`tmo` relative to the first instruction |
| **3. RUN clears edge_ts `missed`** (through its `clear` input, which also clears `toggled`) | A `missed` from before the run must not poison the first verdicts |
| **4. Stricter pin-claim check:** a claimed pin must be owned by the engine **and in a driving mode**; uo_out pins must be push-pull | An owned pin in input mode would accept SET/SHO and drive nothing |
| **5. A stats clear also zeroes min/max** (they read 0 until the next valid judgment) | One clear = a fresh slot |
| **6. Host clears win** over a same-cycle hardware set (status read-and-clear, LOST clear) | The alternative loses the clear silently; a set in the same cycle is seen again on the next event |
| **7. RUN check timing:** strobe in cycle c, check in cycles c + 1 .. c + 105, first issue at c + 106 (= C_START) | 8 + 97 words, one per cycle |
| **8. `isa.yaml` gained `enums`, `flags_kw`, `aliases`** | Structured, non-semantic: the generator needs them |
| **9. Generated Verilog constants are spliced between markers in `src/engine.v`**, not `` `include ``d | Unsure whether the Tiny Tapeout LibreLane flow resolves include paths |
| **10. Host fast-baud command:** designed (§7), not implemented yet | `uart_tx` / `uart_rx` have a fixed CLKS_PER_BIT; done with host port v2 (CP3) |

## 1. Decisions, with evidence

| Decision | Status | Evidence / note |
| --- | --- | --- |
| STA at 60 MHz, 40 MHz fallback | Agreed | The demo board's `clock_project_PWM` uses an RP2 system clock that's a multiple of the project clock, up to twice it: 60 MHz = 120 MHz / 2 at 50% duty. 40 MHz = 120 / 3 at 33% duty, so STA must check the minimum pulse width there. The current chip has +10.7 ns setup slack at 20 ns. At 60 MHz, u = 2 cycles = 33 ns. |
| Every non-waiting instruction takes 1 cycle, incl. taken branches and LOOP | Agreed | Flop program memory is read combinationally. Only WAITD, WAITP and WAITS stall. |
| 64x16 flop program memory per engine | Agreed | ~55k µm² per engine. The chip today is 82k µm² = 9.1% of 6x4. SRAM clock-to-output (~6.3 ns) would sit in the 1-cycle branch path. |
| Engines | 1 in v0, 2 planned | Decided at the 8 Nov gate from the area ledger. The image format already supports 2 (§7). One shared edge_ts, with one snapshot + trigger per engine. |
| Judge channels | 4 | Default ui_in[4..7]; each has a mux to uio_in[0..7] for active modes. Non-intrusive only when all four are on ui_in; every verdict carries NI. |
| WAITP on judge channels | Required | Inputs 12..15 are the channels after the mux, the same synchronized signal edge_ts sees. |
| 10BASE-T | Out of v0 | Needs a hardware serializer. |

## 2. Machine state (per engine unless noted)

| State | Bits | Changed by |
| --- | --- | --- |
| PC | 6 | issue, branches, RUN (→ 0) |
| X, Y | 16 each, + F, O, E tags (1+1+2 bits) | TSGET |
| SR | 16 | SHO, SHI, LDSR, PULL |
| RC | 8 | LDRC, LOOP. One counter, **no nesting** in v0 (inner loops must be unrolled or use the schedule) |
| DL (schedule) | 16 | **MARK, WAITD only** |
| TO (timeout) | 16 | **TMO, TMON only** |
| Flags T, C, E, S | 1 each | WAITP/WAITS; SHI/TSTP/CMP; PULL; snapshot logic, REL |
| LOST | 8, saturating | +1 per trigger lost while S; **cleared only by the host** |
| lost_since_verdict | 1 | set by a lost trigger; cleared by JUDGE (reported as L) |
| Stats, slots 0..3 | 5 x 16 each (+ reserved Σm 32, Σm² 48) | JUDGE b < 4; cleared by the host |
| Sticky status: OUT_OVF, VERDICT_OVF, IN_UNDERRUN, PIN_FAULT, CRC_FAIL | 1 each | hardware; cleared by the host status read or a new RUN |
| NOW (shared) | 16 | +1 every cycle |
| Output pins (shared) | 15: uio[0..7] (input / push-pull / open-drain), uo_out[0,1,2,3,5,6,7] (push-pull) | owner + mode in the global config |

## 3. Timing contract (cycles of the engine clock)

### 3.1 Issue
- An instruction **issues** in the cycle it executes; straight-line code issues one per cycle.
- Every instruction costs **exactly 1 cycle** except WAITD, WAITP and WAITS. Branches (JMP, BR,
  LOOP), taken or not, cost 1 cycle.
- RUN checks the images (§7, 105 cycles). If both pass, address 0 issues **C_START = 106**
  cycles after RUN is accepted.
- **HALT** (or a host halt) cancels any wait. Push-pull pins keep their level;
  **open-drain pins are released**, so a halted engine can't hold SDA low and hang the bus.

### 3.2 Outputs
- SET / SHO issued in cycle t change the pad from **cycle t+1**, for every pin and mode, if the
  engine owns the pin. Otherwise there is no effect and PIN_FAULT is set. Pads change at no
  other time and never glitch.

### 3.3 Inputs
- 2-flop synchronizers. An external edge at time t is visible to WAITP / TSTP / SHI between
  1 - 1/K and 2 + 1/K cycles later (the window proved for edge_ts).

### 3.4 Deadlines: two registers, nothing implicit
- **DL, the schedule.** `mark`: DL ← NOW. `waitd n` issued at t: DL ← DL + n; the next
  instruction issues at **max(t + 1, DL)**. Nothing else writes DL.
- **TO, the timeout.** `tmo t[, x16]`: TO ← DL + t·s; `tmon t[, x16]`: TO ← NOW + t·s, where
  s = 16 with `x16`, else 1. Maximum 16,368 cycles (273 µs at 60 MHz). Nothing else writes TO.
  The assembler takes times and rounds timeouts **up**, so they never fire early.
- **Waits** (`waitp q, v`, `waits`) stall until their event (T ← 0) or NOW - TO ≥ 0 by
  signed difference (T ← 1); the **event wins** a tie. They end the cycle after the condition
  is seen, and change neither DL nor TO.
- **Re-anchoring is explicit:** firmware issues `mark` after an input-paced wait if it wants
  to time from the event.
- **Long waits:** `waitd n` holds n ≤ 2047 (34 µs at 60 MHz). The assembler splits longer
  waits into consecutive WAITDs, which is exact because each one adds to DL. For example, a
  9600-baud bit, 6,250 cycles, becomes 2047 + 2047 + 2047 + 109. The assembler reports the extra
  words (here +3), because 64 words is tight.
- **Half-range rule:** DL - NOW and TO - NOW stay below 2^15. The assembler enforces a
  conservative static bound of 2^14.

### 3.5 FIFOs
- **PULL never stalls:** E ← 1 and sticky IN_UNDERRUN if IN is empty, SR unchanged.
  - Why non-blocking: every non-wait instruction keeps a fixed cost, and timing never depends
    on host latency.
  - The firmware states its frame-boundary policy (`pull; je idle`); underrun stays observable.
- **PUSH and JUDGE never stall.** A full FIFO drops the entry (both slots if raw) and sets
  OUT_OVF / VERDICT_OVF.

### 3.6 Snapshots (atomic, ordered timestamps)
- `trig ch, e` arms this engine's trigger on channel ch, rise or fall.
- When the trigger edge is reported (report cycle r) and **S = 0** (and no capture is pending):
  - the snapshot ← every channel's {rise_ts, fall_ts, last-edge, stale and missed flags}
    **as edge_ts had latched them at the end of cycle r + 1**. The copy is made at the end of
    r + 2 from edge_ts's registers, which hold exactly that state during r + 2 (§0b, diff 1);
  - per edge, **ORD** = "latched in cycle r - 1, r or r + 1", **except the trigger edge
    itself** (its report in r: its order relative to the trigger is known);
  - epoch ← epoch + 1, S ← 1, both visible from cycle **r + 3**.
- **Why one cycle later, and why ORD:**
  - Two edges' report latencies differ by less than 1 + 2/K < 2 cycles, **in either direction**.
    - An edge that truly came *before* the trigger is latched by the end of cycle r + 1, so the
      snapshot contains it.
    - An edge that truly came *after* the trigger can be latched as early as cycle r - 1.
    - Only edges latched at r - 2 or earlier are certainly before it.
  - So an edge latched in cycles r - 1, r or r + 1 may have come just before *or* just after the
    trigger edge. Its order is unknowable, so it is flagged ORD and any verdict using it is
    INDETERMINATE.
  - The header still reports ORD, so near-coincident edges can be counted. For I2C, an SDA
    edge within ~2 cycles of an SCL rise breaks a rule whichever order it came in.
- **Trigger while S = 1 or a capture is pending (r .. r + 2):** the snapshot is kept (still one consistent instant), LOST + 1
  (saturating), and lost_since_verdict ← 1. **Not** INDETERMINATE: the loss is coverage, not
  soundness.
- `tsget r, ch, e`: r ← that edge's timestamp from the snapshot. e is rise, fall or **last**
  (whichever of the two was latched later, as tracked by edge_ts). Tags: F(r) ← stale |
  missed | no valid snapshot yet; O(r) ← ORD; E(r) ← epoch.
- `rel`: S ← 0. `waits`: wait for the next snapshot, with timeout TO.

### 3.7 JUDGE and stats
- `judge b[, raw][, push]`: m ← Y - X; verdict(m, bounds[b], **invalid = FX | FY | OX | OY |
  (EX ≠ EY)**) with the proved rule (u = 2 cycles, sharp VIOLATION, two-sided rounding of §6).
  - **EX ≠ EY**: X and Y came from different snapshots. This catches mixed instants, including
    firmware bugs such as `tsget X; rel; waits; tsget Y`. The 2-bit tag catches mixes up to
    3 snapshots apart.
- **Push mode:** `all` (default) pushes every verdict; `nonpass` only VIOLATION and
  INDETERMINATE; `none` pushes nothing (stats only).
- **Header:** {verdict, FX, FY, TAG, L, NI, R, b, ORD, m saturated to 3 bits}. With `raw`, a
  second slot carries the exact m. Both slots go in or neither does.
- **Stats:** every `judge b` with b < 4 updates slot b, in every push mode:
  - n_pass, n_viol, n_indet (16-bit, saturating);
  - m_min, m_max over the judgments that weren't invalid.

  The host latches one slot into a shadow register (atomic) and reads it; it clears per slot.
  Address space for Σm (32 bits) and Σm² (48 bits) is reserved for the vernier work.

## 4. Instruction set

Generated from `isa.yaml` by `tools/isagen.py` (do not edit the table by hand). Opcode in [15:11]; 24 used,
4 reserved, 4 free.

Opcodes 0x18..0x1B are reserved for the v1 bitstream unit; 0x1C..0x1F are free.

<!-- BEGIN GENERATED instruction table (tools/isagen.py) -->
| Opcode | Syntax | Cycles | Effect |
| --- | --- | --- | --- |
| 0x00 | `nop` | 1 | nothing |
| 0x01 | `halt` | 1 | stop. Push-pull pins keep their level; open-drain pins are RELEASED (a held-low SDA would hang the bus). |
| 0x02 | `set p, v` | 1 | pin p <- v if this engine owns p (else no effect, PIN_FAULT). Push-pull: drive v. Open-drain: v=0 drives low, v=1 releases. |
| 0x03 | `sho p[, msb]` | 1 | pin p <- msb ? SR[15] : SR[0] (ownership as SET); SR shifts that bit out, refilling with 1 |
| 0x04 | `shi q[, msb]` | 1 | C <- in[q]; SR <- msb ? {SR[14:0], in[q]} : {in[q], SR[15:1]} |
| 0x05 | `tstp q` | 1 | C <- in[q] |
| 0x06 | `mark` | 1 | DL <- NOW |
| 0x07 | `tmo t[, x16]` | 1 | TO <- DL + t * (x16 ? 16 : 1) (timeout relative to the schedule) |
| 0x08 | `waitd n` | ≥ 1 (waits) | DL <- DL + n; the next instruction issues at max(t + 1, DL) |
| 0x09 | `waitp q, v` | ≥ 1 (waits) | until in[q] == v (T <- 0) or NOW - TO >= 0 (T <- 1); pin event wins a tie; DL and TO unchanged |
| 0x0a | `waits` | ≥ 1 (waits) | until S (T <- 0) or NOW - TO >= 0 (T <- 1); snapshot wins a tie; DL and TO unchanged |
| 0x0b | `jmp a` | 1 | pc <- a |
| 0x0c | `br c, a` | 1 | if condition c: pc <- a. Aliases: jt jnt jc jnc je jne js jns |
| 0x0d | `loop a` | 1 | if RC != 0: RC <- RC - 1, pc <- a; else fall through |
| 0x0e | `ldrc imm` | 1 | RC <- imm |
| 0x0f | `ldsr imm[, hi]` | 1 | SR[7:0] (or SR[15:8]) <- imm |
| 0x10 | `pull[ hi]` | 1 | if IN non-empty: SR byte <- IN, E <- 0; else E <- 1, SR unchanged, IN_UNDERRUN <- 1. Never stalls. |
| 0x11 | `push[ hi]` | 1 | OUT <- SR byte; never stalls; full -> drop, OUT_OVF <- 1 |
| 0x12 | `trig ch, e` | 1 | this engine's snapshot trigger <- channel ch, edge e (rise or fall) |
| 0x13 | `rel` | 1 | S <- 0 (release the snapshot; the next trigger may replace it) |
| 0x14 | `tsget r, ch, e` | 1 | r <- snapshot timestamp of channel ch, edge e (rise, fall or last); F(r) <- its stale \| missed \| !valid; O(r) <- its ORD bit; E(r) <- snapshot epoch |
| 0x15 | `judge b[, raw][, push]` | 1 | m <- Y - X; v <- verdict(m, bounds[b], invalid = FX \| FY \| OX \| OY \| (EX != EY)); update stats[b] if b < 4; push per mode (all / nonpass / none), header (+ m if raw), all or nothing; clear lost_since_verdict |
| 0x16 | `tmon t[, x16]` | 1 | TO <- NOW + t * (x16 ? 16 : 1) (timeout relative to now, e.g. after an input-paced wait) |
| 0x17 | `cmp imm[, hi]` | 1 | C <- (SR[7:0] (or SR[15:8]) == imm) |
| 0x18..0x1b | reserved | - | v1 bitstream unit (LFSR CRC/parity tapping SHO/SHI bits) |
<!-- END GENERATED instruction table -->

Output pins `p`: 0-7 = uio[0..7], 8-14 = uo_out[0,1,2,3,5,6,7]. Input pins `q`: 0-7 =
uio_in[0..7], 8-11 = ui_in[0..3], 12-15 = judge channels 0-3.

## 5. Programs that check the ISA

### 5.1 UART TX with flow control and a timeout, drift-free, byte kept
115200 baud at 60 MHz: `.equ BIT 521`.
```
start:  pull                 ; non-blocking
        je   idle
cts:    tmon 250us           ; assembler: 938 x 16 = 15008 cycles
        waitp 8, 0           ; CTS (ui_in[0]) low ...
        jt   cts             ; ... timed out: retry; the byte stays in SR
        mark                 ; the frame's schedule starts here: DL = t0
        set  0, 0            ; start bit: pad at t0 + 2
        waitd BIT
        ldrc 7
bit:    sho  0               ; data bits, LSB first
        waitd BIT
        loop bit
        set  0, 1            ; stop bit
        waitd BIT
        jmp  start
idle:   mark                 ; nothing to send: poll every 10 us
        waitd 600
        jmp  start
```
DL is written only by `mark` and `waitd`, and `tmon`/`waitp` touch only TO and T. So inside a
frame DL = t0 + k·BIT, and the pad changes at **t0 + 2 + k·BIT** for k = 0..9, however long CTS
took. (Traced cycle by cycle in the v0.2 review.)

### 5.2 Schedule-relative timeout (half-duplex reply window)
```
        ...                  ; last stop bit sent: DL = end of the stop bit
        tmo  10*BIT          ; reply within 10 bit times (assembler: 326 x 16 = 5216)
        waitp 9, 0           ; peer's start bit on ui_in[1]
        jt   no_reply
        mark                 ; re-anchor on the peer's start bit (explicit)
```

### 5.3 Referee: I2C setup time, one TSGET + one JUDGE per SCL rise
Channels: 0 = SDA (ui_in[4]), 1 = SCL (ui_in[5]); non-intrusive (NI = 1).
```
.clock_hz 60e6
.bound 0, min, 100ns         ; Fast-mode tSU;DAT: 6.0 cycles, exact
        trig 1, rise         ; snapshot on every SCL rise
loop:   tmon 1023, x16       ; 273 us without an SCL rise = bus idle
        waits
        jt   bus_idle
        tsget X, 0, last     ; SDA's latest edge before this SCL rise (rise or fall)
        tsget Y, 1, rise     ; this SCL rise
        judge 0, nonpass     ; only VIOLATION / INDETERMINATE go to the host; stats count all
        rel
        jmp  loop
```
- **Cycle budget:** S appears in r + 3 (§0b, diff 1) → waits ends → 5 instructions → back in
  `waits` ~8 cycles after the SCL rise is reported (v0.3 said ~7; +1 for the capture delay).
  A 400 kHz SCL period is 150 cycles at 60 MHz, so no trigger is lost in steady state.
- `tsget Y, 1, rise` reads the trigger edge itself, which is never ORD (§3.6).
- **What each outcome means:**
  - SDA's last edge ≥ 100 ns + u before SCL: PASS.
  - Clearly closer: VIOLATION.
  - Within ~2 cycles of SCL: ORD, INDETERMINATE. The edge's order can't be known; for I2C it
    breaks a rule either way.
  - Stale or missed: INDETERMINATE.

### 5.4 I2C target: address match and ACK before the 9th SCL rise
SCL = uio_in[0] (input), SDA = uio[1] (open-drain, owned; read back as input 1). Address 0x50.
Entered after a START.
```
addr:   tmon 1023, x16       ; bus timeout for this byte (273 us)
        ldrc 7               ; 8 bits: address + R/W, MSB first
abit:   waitp 0, 1           ; SCL rises
        jt   bus_timeout
        shi  1, msb          ; sample SDA
        waitp 0, 0           ; SCL falls
        jt   bus_timeout
        loop abit            ; after the 8th fall: falls through
        cmp  0xA0            ; 0x50<<1 | W
        jc   ack
        cmp  0xA1            ; 0x50<<1 | R
        jnc  not_me
ack:    set  1, 0            ; ACK: pull SDA low
        waitp 0, 1           ; 9th SCL rise: the controller samples the ACK
        waitp 0, 0           ; 9th SCL fall
        set  1, 1            ; release SDA
```
**Deadline check, worst case (read address):**
- The 8th SCL fall becomes visible ≤ 2 + 1/K cycles after it happens.
- `waitp` ends the next cycle; then loop, cmp, jc, cmp, jnc, set are 6 instructions, and the pad
  changes one cycle after `set`.
- Budget: 2 + 1/K + 1 + 6 + 1 ≈ 10.25, so SDA is low **≤ 11 cycles = 183 ns after SCL falls**
  (275 ns at 40 MHz).
- Fast-mode guarantees SCL low ≥ 1.3 µs and needs tSU;DAT = 100 ns before the 9th rise:
  1.3 µs - 0.183 µs - 0.1 µs = **1.02 µs margin**.
- Fast-mode Plus (0.5 µs low, 50 ns setup): **267 ns margin**.
- SDA changes only after the fall has been seen, so the hold time (≥ 0) is met. No host round
  trip is involved.

## 6. Bounds from ns: sound at any clock

- The image is assembled for a clock (`.clock_hz` or `--clock-hz`); bounds are written in time
  (`.bound b, min|max, 600ns`).
- Stored: **n_lo = N rounded down** to 1/16 cycle, plus `inexact`; then n_hi = n_lo + 1/16.

| Bound | PASS if | VIOLATION if |
| --- | --- | --- |
| minimum | m - u ≥ n_hi | m + u ≤ n_lo |
| maximum | m + u ≤ n_lo | m - u ≥ n_hi |

Each side tests against the bound that keeps it sound, whichever way the conversion rounded.
The chip must run at the assembled clock (A9).

## 7. Images and loader

**Global config (8 words, CRC-16):**
- magic `0x5246`;
- format version | engine count;
- 15 output pins × {owner (none / engine 0 / engine 1), mode};
- channel mux (4 × 4 bits);
- 1 reserved word.

It can be written only while **all** engines are halted.

**Per-engine image (97 words, its own CRC-16):**
- program (64 words);
- bounds (16 × 2 words);
- the **claimed-pin mask** (15 bits), computed by the assembler from the program's SET/SHO
  operands.

It can be written only while **that** engine is halted.

**RUN(e)** reads the global image and engine e's image through the read ports (8 + 97 = 105
cycles). It starts only if:
1. both CRCs match the host's expected values, and
2. every pin e claims is owned by e in the global config.

Otherwise e stays halted, CRC_FAIL is set, and the command is NAK'd. Because ownership is one
owner per pin and claims must be owned, **two engines can never drive one pin**. At run time, a
SET/SHO to an unowned pin has no effect and sets PIN_FAULT (a second line of defence).

**Other loader rules:**
- **Writes only while halted** are enforced in hardware (gated write enables), not only by
  the host port.
- **Readback** covers every image word and the computed CRCs, while halted.
- **CRC:** CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), MSB first, over the image words in
  order.

**Host port commands** (4-byte commands, 2-byte replies unless noted; `e` = engine):

| Command | Bytes | Notes |
| --- | --- | --- |
| Identify | `49 00 00 00` | data 0x52 |
| Write global word | `47 addr hi lo` | all engines halted |
| Write engine word | `57 e:addr hi lo` | e in addr[7]; that engine halted |
| Read word | `72 sel addr part` | global or engine e; high or low byte; while halted |
| Expected CRC | `43 sel hi lo` | sel: global / engine e |
| Run / halt | `52 e 01 00` / `52 e 00 00` | RUN is ACK'd only if the checks passed |
| IN push / OUT pop | `50 e byte 00` / `70 e 00 00` | |
| VERDICT drain | `44 e max 00` | reply: count, then 2 bytes per slot (up to 8) |
| Stats latch / read / clear | `53 e slot 00` / `73 e idx 00` / `5A e slot 00` | latch is atomic per slot |
| Lost counter | `4C e 00 00` | reply: LOST; `4C e 01 00` clears it (host only) |
| Baud | `42 div_hi div_lo 00` | applied after the reply; 60 / 30 / 20 = 1 / 2 / 3 Mbaud at 60 MHz |
| Status | `51 e 00 00` | running, S, sticky bits, NI; read-and-clear of the sticky bits |

## 8. Throughput (why push modes and stats are needed)

| Quantity | Value |
| --- | --- |
| 400 kHz I2C, one setup check per SCL rise | **400,000** judgments/s (9 SCL rises per byte = 44,000 bytes/s; the "40k" estimate is the per-byte rate) |
| One judgment per byte (e.g. per ACK) | ~44,000 judgments/s |
| Host link at 3 Mbaud | ~300 kB/s |
| Popping slots with single commands (6 bytes on the wire per byte) | ~25,000 slots/s |
| Popping slots with the drain read (~2 bytes per slot) | ~140,000 slots/s |
| VERDICT FIFO | 8 slots: absorbs a burst of 8 non-PASS verdicts (4 with raw) |

So all-verdict streaming of per-rise judging is impossible on any link the demo board has.
`nonpass` sends only the exceptions, stats count everything, and the FIFO absorbs bursts.
At 115200 baud (the power-up default) the drain gives ~5,000 slots/s, which is enough for
`nonpass` on mostly-compliant buses.

## 9. Proofs and tests in step 2

**Lemma (edge_ts + snapshot), tick-level harness as in `docs/verdict.md`:**
- For any accepted snapshot and any two of its edges without ORD and F, |(ts_Y - ts_X) -
  (t_Y - t_X)| < 2 cycles, where t is the true edge time.
- Every edge that truly came before the trigger edge is in the snapshot.
- ORD is set exactly on the edges latched in cycles r - 1 .. r + 1 (r = the trigger's report
  cycle) other than the trigger edge itself, and any edge latched at r - 2 or earlier truly
  came before the trigger.
- The snapshot equals the edge_ts state as of the end of cycle r + 1, and is constant while S.

This removes the harness condition used by the merged cross-interval proof (A16).

**Status (engine-v0 CP2): all of section 9's formal items are proved.**

| Item | Where | Task(s) |
| --- | --- | --- |
| Snapshot lemma at tick level: Y is the trigger edge and never ORD; X is never older than pin 0's last rise at or before the trigger (completeness); X without ORD *is* that rise (order); the cross verdict judged **through the snapshot** is sound, with the A16 harness condition removed | `formal/verdict_formal.sv` (+ `src/snapshot.v`) | `verdict.sby`: prove4, prove8, sharp4, sharp8 (unbounded), bmc4, cover4 |
| Snapshot at cycle level, every input free: capture == edge_ts state as of the end of r + 1, visible from r + 3; ORD exactly on reports in r - 1..r + 1 except the trigger edge; constant while S; lost triggers | `src/snapshot.v` (`SNAPSHOT_PROPS`) | `engine.sby`: snap, snap_cover; snap_early (capture at r + 1) must FAIL |
| Combinational theorem, engine parameters (M_W 16, FRAC_W 4, N_W 20, sharp) | `formal/verdict_comb.sv` | `verdict.sby`: comb, comb_cover; comb_onesided (rounded-down bound only) and comb_u1 must FAIL |
| Engine properties 1-10 above, every input free, arbitrary image | `src/engine.v`, `src/engine_sys.v` (`FORMAL`) | `engine.sby`: prove (unbounded), cover; tie_timeout (timeout wins a tie) must FAIL |

The cover and tie_timeout tasks start the engine running (define `ENGINE_FORMAL_START_RUNNING`)
so that they don't spend ~106 cycles in a RUN check with an arbitrary image; `prove` doesn't
use it and goes through the real check.

**Theorem (verdict block alone, combinational):** for every m, n_lo, inexact, is_max and every
true interval T with |m - T| < u, PASS ⇒ T meets the true bound and VIOLATION ⇒ T breaks it.
This holds for any K, because bounds are in 1/16 cycle and the lemma is in cycles.

**Engine formal:**
- every non-waiting instruction issues in 1 cycle;
- WAITD: next issue = max(t+1, DL);
- DL is written only by MARK/WAITD and TO only by TMO/TMON;
- the wait event wins a tie;
- pads change only one cycle after SET/SHO, and only for owned pins;
- image writes only while halted;
- no issue unless both CRCs and the pin-claim check passed;
- JUDGE feeds the verdict block exactly Y - X with invalid = FX|FY|OX|OY|(EX≠EY);
- LOST increments iff a trigger arrives while S, and saturates;
- HALT releases open-drain pins.

**Lockstep, firmware and physical:**
- the RTL in lockstep with the generated model, on random programs;
- firmware: UART TX/RX, the §5.3 referee, the §5.4 target, and JUDGE on an external UART at
  in-spec and off-spec baud, on RTL and gate level;
- harden at 6x4, close STA at 60 MHz, update the area ledger.

## 10. Open questions

1. **Resolved:** one edge_ts; one snapshot and one trigger per engine.
2. **Resolved:** lost triggers are counted (LOST, host-cleared) and reported per verdict (L);
   they don't force INDETERMINATE.
3. *New:* ORD makes edges within ~2 cycles (33 ns at 60 MHz) of the trigger INDETERMINATE.
   Should the referee also count these "near-coincident" edges in the stats? That's cheap: one
   more counter per slot.

## 11. Assumption ledger

| # | Assumption | Used by | Handling |
| --- | --- | --- | --- |
| A1 | Each level on a judged pin lasts ≥ 4 cycles | edge_ts / verdict proofs | **Assumed.** Some minimum is unavoidable (`docs/results/glitch_counterexample.md`); `missed` warns at the limit |
| A2 | No judged pin changes in the first 6 cycles after reset | verdict proof | **Assumed**; timestamps are stale (INDETERMINATE) until the first edge anyway |
| A3 | Zero skew between the channels' synchronizers | verdict proof | **Assumed**; real skew (tens of ps) adds to u |
| A4 | No glitch filter on judge channels | u = 2 | Design fact; a filter's delay must be added to u |
| A5 | A metastable first flop resolves within one cycle | edge_ts proof | **Assumed** (standard 2-flop argument) |
| A6 | Intervals shorter than a quarter of the timestamp range | verdict | **Flagged** (stale) |
| A7 | Line-rate streaming needs IN non-empty at frame boundaries | throughput | **Observable** (E, IN_UNDERRUN); timing never depends on it |
| A8 | DL and TO within half the 16-bit range of NOW | waits | **Enforced** by the assembler (2^14) and the timeout encoding (≤ 16,368) |
| A9 | The chip runs at the clock the image was assembled for | bounds in ns, time literals | **Assumed**; the host library sets the demo-board clock from the image header |
| A10 | Non-intrusive only when all channels select ui_in | the non-intrusive claim | **Reported** per verdict (NI) |
| A11 | X and Y of one verdict come from one instant | JUDGE | **Enforced**: snapshot + epoch tags (EX ≠ EY → INDETERMINATE; detects mixes up to 3 snapshots apart). Lost triggers are coverage, **counted** (LOST, L), not a soundness issue |
| A12 | Timing contracts restart at `mark` after an input- or host-paced wait | static timing | **By construction** (DL only at MARK/WAITD) |
| A13 | The image in memory is the image the host meant | everything | **Enforced**: per-image CRC before RUN; readback |
| A14 | A judged interval is at most 16,384 cycles (16-bit timestamps): **273 µs at 60 MHz** (410 µs at 40) | JUDGE | **Flagged** (stale) beyond it. Per-bit UART checks fit at 9600 baud (104 µs per bit); a whole 9600-baud frame (1.04 ms) doesn't. 16 bits kept for v0; widening later is an ISA change |
| A15 | One LOOP counter, no nesting | firmware | Design limit for v0; the assembler reports nested-loop attempts |
| A16 | The earlier edge of a judged pair is latched before the pair is judged | cross intervals | Was a **harness condition** in the merged verdict proof. Now **enforced** by the one-cycle snapshot delay, ambiguous order **flagged** (ORD), and **proved**: the cross verdict is judged through `src/snapshot.v` with no harness condition (`formal/verdict.sby`, snapshot lemma, §9) |
| A18 | No parity or CRC in v0 (no ALU) | UART 8E1/8O1, CAN, USB, any CRC | **Design limit**: until the v1 bitstream unit (opcodes 0x18..0x1B reserved: LFSR CRC/parity on SHO/SHI bits, bit stuffing, NRZI), the host supplies the parity bit (e.g. as the 9th bit of a 16-bit IN pair) |
| A17 | Two engines never drive one pin | outputs | **Enforced** by the image format (one owner per pin; claims ⊆ owned, checked at RUN) and at run time (PIN_FAULT) |
