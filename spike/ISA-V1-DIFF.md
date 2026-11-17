# ISA v1: spec diff against v0.3 (DRAFT for the 23 Nov review)

Status: **draft, 17 Nov 2026.** Everything here was built and tested on the reference model only
(`spike/model_v1.py`, `spike/asm_v1.py`, `spike/isa_v1.yaml`); nothing is in the RTL yet.
`diff tools/refmodel.py spike/model_v1.py` is the exact behaviour of every item.

**The rule (owner, 12 Nov):** an ISA addition used by fewer than two real programs is dropped. A
"real program" is one of the designed-for programs (UART 8N1 / 8E1, SPI 0-3 controller + target,
I2C controller + target, CAN 2.0A TX / RX) or a referee program, passing its own bus-level test
against a device model written from the protocol. Items that are not instructions (soundness
mechanisms, host configuration) are not decided by the count; each says who decides.

Verdicts: **KEEP** (two programs use it today), **UNPROVEN** (fewer than two today; the named
programs decide it by 21 Nov, otherwise it is dropped), **DROP**, **OWNER** (not an instruction:
the owner decides on 23 Nov).

## 1. Summary

| # | Item | Programs using it today | Verdict | Cost per engine (est.) |
| --- | --- | --- | --- | --- |
| A | Separate shift registers SRO / SRI (+ `o` source flag on `cmp` / `push`, to build) | SPI controller, SPI target | **KEEP** (fix owed) | +16 flops, about 800 um2 |
| B | Second loop counter RC1 | I2C referee (the SPI referee needs only RC0) | UNPROVEN: I2C controller, CAN | +8 flops, about 400 um2 |
| C1 | 7-bit address, 4-bit branch condition (new flags Z, U, A) | UART RX 8E1 (Z), I2C referee (A) | **KEEP** (needed for any program over 64 words, i.e. the Player) | +1 pc bit, branch mux |
| C2 | CRC / parity register (`crcp`, `bsc`, `sho .., crc`, flag Z) | UART TX 8E1, UART RX 8E1 | **KEEP** (Player only) | about 45 flops, 2,200 um2 |
| C3 | Bit stuffing (run counter, flag U, `sho / shi .., stuff`) | none | UNPROVEN: CAN TX, CAN RX | about 6 flops |
| C4 | NRZI | none | **DROP** with low-speed USB (first on the cut list) | - |
| D | Guard band per bound (signed n_lo, 10-bit width) + per-bus `.bus` declaration | none | UNPROVEN but see 2.D: the guard band is on the never-cut list | 10 pipeline flops + a 20-bit add in JUDGE cycle 2 |
| E1 | `trig <mask>, rise / fall / any` | I2C referee, SPI timing referee | **KEEP** | about 6 flops |
| E2 | `tsts ch, level / trise / tfall` (flag A) | I2C referee, SPI timing referee | **KEEP** | about 12 flops |
| K | `trigq`: level-qualified second trigger term | I2C referee, SPI timing referee (SCK edges only while CS is low) | **KEEP** | about 10 flops |
| P | Poison: `resync`, `desync`, RUN starts poisoned, header bit 10, own stats counter | every referee that keeps state | **OWNER (13 Nov rule): in** | 2 flops + 4 x 16-bit counters (about 3,100 um2) |
| Q | Event-loss mechanism: **snapshot queue, depth 2** (rule slots measured and not chosen) | every referee program | **OWNER (14 Nov gate): in; smallest design that passes** | about 14,000 um2 |
| - | "Lost" = an armed edge that is in no snapshot | - | follows from P | none |
| F | Short-level taint (timestamp flagged after a level of 5 cycles or less) | - | OWNER | about 4 flops per channel |
| G | Non-PASS pulse on an output pin | - | OWNER | a few flops |
| H | Timestamp prescaler | - | OWNER, default cut | about 10 flops + proof work |
| I | `call` / `ret` | no program overflowed | **DROP** | - |
| J | `judge b, flag` (a verdict as a branch condition) | not modelled | UNPROVEN: I2C referee tHD;DAT; depends on section 3 | 2 flops |
| L | "trigger lost" as a branch condition | - | **DROP**: P does it in hardware | - |
| M | Stale earlier edge = PASS for a minimum bound | not modelled | **DROP for v1** unless a soundness argument is ready by 21 Nov | 1 tag bit per register |

## 2. The diffs

### A. Separate input and output shift registers
- **What:** `sho`, `pull`, `ldsr` use SRO; `shi`, `push`, `cmp` use SRI (16 bits each).
- **Why:** v0.3 programs that send and receive in one byte "spread" each byte over two FIFO bytes;
  the SPI controller for modes 0-3 drops from 29 + 28 words (two files) to 26 (one file), and an
  SPI target (39 words) becomes possible.
- **Defect found by the firmware, fix owed:** with SRI as the only source of `cmp` and `push`, a
  program can no longer branch on a host byte (`pull; cmp 0x53`, the I2C controller's command
  dispatch) or push a constant (`ldsr 0xFF; push`). The UART RX 8E1 spends about 7 of its 42 words
  working around it. **Fix: a source flag `o` on `cmp` and `push` (read SRO).** No program needs more
  than 8 bits of SRO, so SRO could be 8 bits: decided once the I2C controller and target are ported.

### B. Second loop counter
`ldrc imm[, rc1]`, `loop a[, rc1]` (operand bit 8). Used today only by the I2C referee, as two
one-bit state flags. To be decided by the I2C controller port (bits x bytes) and CAN (bits x fields).

### C1. Wider branch field
Address `a` = 7 bits (programs up to 128 words); condition `c` = 4 bits: T, C, E, S as before, plus
Z (CRC register is 0), U (a stuff bit is due), A (the last `tsts` was ambiguous), each with its
negation; codes 14 and 15 reserved. Re-encodes BR, JMP, LOOP. The Player engine (96 or 128 words)
needs the 7-bit address whatever else is decided.

### C2. CRC / parity register (Player engine only)
- 16-bit L and POLY. POLY is the polynomial without its top term, left-aligned (CRC-15 0x4599 ->
  0x8B32; parity -> 0x8000). `crcp imm[, hi]` loads POLY.
- `bsc tap[, init][, st][, inv]`: tap = off / the bits `sho` sends / the bits `shi` samples; init =
  keep / zeros / ones (ones = bit 15 down to POLY's lowest set bit, so narrow CRCs work);
  `inv` for CRCs sent inverted.
- `sho p, crc` sends L[15]. Flag Z = (L == 0).
- Opcodes 0x18 (`crcp`), 0x19 (`bsc`); 0x1A and 0x1B stay reserved.

### C3. Bit stuffing
A run counter on the tapped stream; `st` = none / can (5 equal bits) / usb (6 ones); flag U = a
stuff bit is due; `sho p, stuff` sends one; `shi q, stuff` samples one and sets C on a stuffing
error. **Used by nothing until CAN is written.**

### D. Guard band (per bound in hardware, per bus in the source)
- **Hardware:** bound word 2 = `{n_lo[3:0], 0, is_max, width[9:0]}`. n_lo is **signed** (20 bits,
  1/16 cycle): a bound such as tHD;DAT min 0 ns with a guard needs a negative n_lo, and clamping it
  would make VIOLATION unsound. PASS tests against n_lo + width, VIOLATION against n_lo (min bound;
  mirrored for max). v0.3's `inexact` bit is the special case width = 1.
- **Assembler:** n_lo = floor(N - g_minus), width = ceil(N + g_plus) - n_lo; a width over 1023
  (63.9 cycles) is refused, never clipped.
- **Not built yet: the per-bus declaration** (`.bus`: rise / fall time per line from 0.847 x Rp x Cb
  or a scope measurement, pin skew, clock ppm; per rule, g_minus and g_plus derived from which spec
  level each end of the interval uses). The formulas are a soundness argument and get their own
  review.
- **Conflict to settle on 23 Nov:** the two-program rule says D is UNPROVEN (no program declares a
  guard yet); the plan lists the guard band as never-cut. Proposal: D counts as used by every
  referee program that declares a `.bus` (the I2C referee and the UART or SPI timing referee), and
  the declaration layer is written before 21 Nov.

### E1. Trigger on a set of edges
`trig <mask>, rise | fall | any` (the assembler defines CH0..CH3 = 1, 2, 4, 8 and refuses a bare
number, so v0.3's `trig 1, rise` cannot silently change meaning). Clears the second term (K).

### E2. Snapshot tests
`tsts ch, level | trise | tfall` (opcode 0x1C). `level`: C <- ch's level after every edge the
snapshot holds; `trise / tfall`: C <- ch's rise / fall is a trigger edge of this snapshot. Flag A
<- the answer is ambiguous (an ORD edge of ch, ch missed, several trigger edges in one cycle, or
another armed edge with ORD). The snapshot keeps `level`, `trig` and `armed`.

### K. Level-qualified trigger
`trigq <mask>, e, ch, lv` (opcode 0x1F): a second trigger term, live only while channel ch's
synchronized level in the edge's report cycle equals lv. For I2C, `trig CH1, any` + `trigq CH0,
any, 1, 1` = every SCL edge plus START and STOP; data edges are never events, so they cannot crowd
out the SCL rise that ends tSU;DAT. Measured effect on legal traffic with SDA changing 100-200 ns
after SCL falls: 92.5% of events unjudged without K, 0% with it.

### P. Poison (soundness)
- **Requirement (owner, 13 Nov):** no PASS or VIOLATION may be about the wrong pair of edges.
- `poisoned` (1 bit) is set by RUN, by `desync` (0x1E), and when a snapshot with a lost event
  before it becomes the current one. While it is set every JUDGE answers INDETERMINATE, with
  **header bit 10** set (v0.3's L bit, which it replaces) and counted in its own stats counter.
- `resync` (0x1D) clears it only while a snapshot is held and it is the one the program last read
  (same epoch). A loss after that snapshot does not block it; it poisons again when the next
  snapshot arrives. (An unconditional resync was tried first: it forgave an event lost while the
  current one was being handled, and the wrong-pair verdict came back.)
- **Hazard to state in the spec (fourth review, 17 Nov):** the epoch is 2 bits. At queue depth 4 a
  program that pops four snapshots without reading them reaches a head with the same epoch as the
  one it last read, and `resync` would vouch for an unread snapshot. Neither referee program can do
  this (every `resync` follows a read of the head); the spec says "read the head before `resync`", or
  `rel` clears the last-read mark (one flop).
- **"Lost" is redefined:** an armed edge reported one cycle after an accepted trigger is inside
  that snapshot (flagged ORD, visible through flag A), so it is not lost. Only an event that finds
  no snapshot slot is lost. This changes v0.3's LOST formal property.
- **Measured (model):** zero verdicts about the wrong pair of edges for both I2C referee variants
  on five traffic mixes with intervals down to 2% of their minimum, and in a 300-waveform fuzz per
  program (PR #12). The fourth review (17 Nov) fuzzed both programs at depths 1, 2 and 4 on 3,600
  random and about 3,500 glitchy waveforms and found one hole, in the receiver rule (below), now
  fixed and covered by a regression test; with the fix, 0 failures.
- **Legal 0 ns data hold never poisons:** the I2C referee follows the receiver rule of the I2C
  specification: when the event is SCL's fall, an SDA edge at or after it is data, never START /
  STOP. Legal traffic with data hold 0, 0-33, 0-100 and 0-800 ns: no lost event, no poisoned
  verdict (apart from the first START after RUN, by design), no VIOLATION.
- **The hole the review found:** the rule was also applied when the event was an SDA edge seen
  while SCL was still high, with SCL falling one cycle later. That dropped a START or STOP from the
  program's state, and a later START was judged as the wrong rule (tBUF for tSU;STA, or the
  reverse), unpoisoned. Fix: one instruction, the receiver rule only when SCL's fall is itself the
  event; otherwise lost track (poison until the next START or STOP). **Cost on real hardware:** an
  SDA edge 0 ns after SCL's fall can be reported a cycle before it (the two report latencies differ
  by less than 2 cycles); the referee then poisons until the next START or STOP. That is lost coverage,
  never a wrong verdict. The model, whole-cycle, cannot show how often.
- **Open (soundness assumption to state, found by the second review):** the classification above
  assumes the two lines' report times differ only by the proved sub-2-cycle window. A slow SCL fall
  (I2C allows tf up to 300 ns) crosses our pad threshold at a different moment than the spec's
  level, and if that skew exceeds about a cycle, a data change right after SCL's fall can be seen
  as an SDA edge while SCL is still high, with no ORD flag: a START or STOP to the program. Both
  referee variants read it that way (it predates the fix). Options for 23 Nov: state it as an
  assumption beside A3 (skew) with the guard-band declaration bounding it, or widen the window in
  which an SDA edge next to an SCL fall is treated as ambiguous (poison, never misread).

## 3. Event loss: the mechanism

**Gate (owner, 14 Nov):** under 5% of violating intervals of at least 5 cycles get no verdict.
Intervals under 4 cycles are below the minimum level length the proofs assume (ledger A1): no
sampled design can judge them, and they are listed apart as a physical limit.

| Traffic mix (data hold 0-800 ns) | queue depth 1 (v0.3) | depth 2 | depth 4 | rule slots |
| --- | --- | --- | --- | --- |
| intervals down to 70% of their minimum | 0 of 742 | 0 | 0 | 0 |
| down to 50% | 0 of 1043 | 0 | 0 | 0 |
| down to 30% | 347 of 1302 (26.7%) | 0 | 0 | 0 |
| down to 10% | 1178 of 1541 (76.4%) | 0 | 0 | 0 |
| down to 2% (165 intervals under 4 cycles) | 1046 of 1281 (81.7%) | 67 (5.2%) | 0 | 0 |
| **all mixes** | **43.5%** | **1.1%** | **0%** | **0%** |
| area | - | +14,000 um2 (1.2% of the core) | +42,000 um2 (3.5%) | 8 slots: +83,000 to +218,000 um2 (6.9% to 18.0%) |

**Choice: snapshot queue, depth 2** (the smallest design that passes the gate as stated, over the
whole population of violating intervals). It is close on one mix: 5.2% when intervals are drawn down to
2% of their minimum (gross violations, many under the 4-cycle limit). **If the gate is read per mix,
depth 4 is the choice (+28,000 um2 more); the owner decides on 23 Nov.**

**Rule slots, as measured** (`spike/rule_slots.py`, `spike/tests/test_rule_slots.py`): 8 slots, each
an end edge with an optional level qualifier, a start edge and an optional three-valued order
condition, evaluated in hardware on every edge. Sound on every mix, 100 random seeds and 4 directed
corner cases; nothing is ever lost, and they judge tHD;DAT, which the firmware referee cannot without
candidate J. Not chosen: 6 to 16 times the area of the depth-2 queue (about 50 flops per slot, plus a
bound read port or copy, two subtractors and a verdict block each; the subtractor, mux and glue sizes
are assumed, not measured), and they cannot judge rules that need protocol state (tVD;DAT, anything
counted in bits or bytes) or a protocol nobody built a slot kind for.

Not modelled: a mix of the two (a few hardware slots for the fastest rules beside the firmware
referee). It would be the answer if fast-SPI judging (below) is wanted.

**A second finding that bears on the choice (SPI referee, 17 Nov):** a firmware referee is limited
by throughput as well as by bursts. Every SCK edge is an event, and the SPI referee follows SCK half
periods of 14 cycles or more without losing one (SCK up to about 2.1 MHz at 60 MHz), **the same at
queue depth 1 and 2**: a queue absorbs a burst, not a faster steady rate. (Second limit: at depth 1 the
frame's first SCK edge needs tCSS of at least 14 cycles, 233 ns; depth 2 has none down to 10 cycles.) Judging a 5-10 MHz SPI bus
needs the rules checked in hardware (rule slots) or a sampled subset of edges.

| SCK half period (cycles) | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| events lost, depth 1 (12 frames x 16 bits) | 192 | 192 | 192 | 132 | 132 | 72 | 0 | 0 | 0 |
| events lost, depth 2 | 156 | 120 | 96 | 72 | 48 | 24 | 0 | 0 | 0 |

## 4. Words per program (model; every row passes its bus-level test)

| Program | v0.3 words | v1 words | Uses |
| --- | --- | --- | --- |
| UART TX / RX, 8N1 | 18 / 16 | 18 / 16 | none |
| UART TX, 8E1 | - | 22 | C2 |
| UART RX, 8E1 | - | 42 (about 7 work around the A defect) | C2, C1 (Z) |
| SPI controller, modes 0-3 | 29 + 28 (two files) | 26 (one file) | A |
| SPI target, modes 0-3 (SCK up to 4.29 MHz) | - | 39 | A |
| I2C Fast-mode referee, P only, 8 rules | 10 (one rule) | 52 of 64 | B, E1, E2, P |
| I2C Fast-mode referee, P + K, 7 rules | 10 (one rule) | 51 of 64 | B, E1, E2, P, K |
| SPI timing referee, modes 0 and 3, 6 rules (tSU, tCH, tCL, tCSS, tCSH, tCSD) | - | 50 of 64 | E1, E2, K, P |

**Still to write, and what each one decides:** I2C controller and I2C target (A fix, B, SRO width);
CAN 2.0A TX and RX (B, C2, C3); UART timing referee (D);
tHD;DAT and tVD;DAT in the I2C referee (J).

## 5. Other changes in v1

- **Image format version 4.** Per-engine image = program (the engine's own size: Referee 64,
  Player 96 or 128) + 16 bounds x 2 words + the claimed-pin mask; the RUN check takes 8 + that many
  cycles.
- **Two engines** from one parameterized module: Referee (full, 64 words) and Player (no judge
  hardware, owns the CRC unit, 96 words; 128 only if the first two-engine run is at or below 50%
  routed). The Player's fetch path is built like the Referee's since 13 Nov: its own read path,
  readback through a separate port, pc copies per 16-word bank.
- **Stats: 4 slots, 9 I2C rules.** Options: three passes over the traffic with different rule
  groups, or count-only slots for bounds 4 and up (about 3 x 16 flops per bound). Decide 23 Nov.
- **tVD;DAT (max 900 ns) is not judged:** the spec applies it to data and ACK bits only, and at the
  SDA edge the program cannot know whether a STOP or repeated START follows.
- Open question from v0.3 (`docs/isa-timing.md` section 10, item 3): count ORD edges in the stats?

## 6. Area (estimate, flops at 49 um2 routed average)

| Addition | um2 |
| --- | --- |
| A, B, C1 (both engines) | about 2,500 |
| C2, C3 (Player) | about 2,500 |
| D, E1, E2, K, P (Referee) | about 5,000 |
| Section 3 mechanism | 14,000 (queue depth 2; 42,000 at depth 4) |
| **Total** | **about 24,000 (2.0% of the 8x4 core); about 52,000 (4.3%) with depth 4** |

The plan's budget for the ISA v1 additions was 28,000 um2.
