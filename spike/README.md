# ISA v1 spike: candidates sized by firmware, on the model only

Status: **work in progress, not a spec.** Nothing here is in the chip. The RTL, `isa.yaml`,
`tools/` and every existing test are untouched (ISA v0.3); this folder holds copies:

| File | What |
| --- | --- |
| `isa_v1.yaml` | copy of `isa.yaml` with the candidates (fields, enums, instructions) |
| `gen.py`, `isa_tables_v1.py` | the encoding tables, made with the project's generator |
| `asm_v1.py` | copy of `tools/refasm.py`; `diff tools/refasm.py spike/asm_v1.py` = the assembler changes |
| `model_v1.py` | copy of `tools/refmodel.py`; `diff tools/refmodel.py spike/model_v1.py` = the behaviour of every candidate |
| `bench.py` | pure-Python test bench: models, wires, bus devices, one clock cycle at a time |
| `firmware/` | the programs, written against the model |
| `tests/` | `python3 -m pytest spike/tests -q` |

The output of the spike is the ISA v1 spec diff (for `docs/isa-timing.md` section 0b), the
firmware, and the two tables at the end of this file.

## The rule

Each candidate below goes into the model. Then the firmware is written against the model. A
candidate stays only if **at least two real programs use it**, where a real program is one from
the lists below that passes its test on the model. A test written only to exercise a feature does
not count. At the end this file holds two tables: words per program, and "used by" per candidate.

## Programs

Emulation (the "Player" side):

| Program | Exists in v0.3 | Needs |
| --- | --- | --- |
| UART TX / RX, 8N1 | yes (18 / 16 words) | nothing new |
| UART TX / RX, 8E1 | no | parity |
| SPI controller, modes 0-3 | yes (29 words each for CPHA 0 / 1) | separate shift registers (no byte spreading) |
| SPI target, modes 0-3 | no | separate shift registers |
| I2C controller (100 / 400 kHz, repeated START, stretching) | yes (63 of 64 words) | separate shift registers, second loop counter |
| I2C target (24C02-style EEPROM) | no | separate shift registers |
| CAN 2.0A frame TX and RX with ACK | no | CRC-15, bit stuffing, second loop counter |

Referee side:

| Program | Exists in v0.3 | Needs |
| --- | --- | --- |
| I2C tSU;DAT | yes (10 words) | guard band |
| I2C Fast-mode, all nine rules (tSU;DAT, tHD;DAT min and max, tLOW, tHIGH, tHD;STA, tSU;STA, tSU;STO, tBUF) | no | trigger on any edge of a set of channels, which edge triggered, line level at the trigger, guard band |
| UART bit timing (start-bit width, bit-to-bit edges against the baud tolerance) | no | trigger on any edge, guard band |
| SPI timing (CS-to-SCK setup, data setup and hold against SCK) | no | which edge triggered, guard band |

## Candidates

Opcode space: 5 bits. 0x00-0x17 are used; 0x18-0x1B were reserved for the bitstream unit; 0x1C-0x1F
are free. Operand space: 11 bits.

| # | Candidate | Encoding | State added (per engine) |
| --- | --- | --- | --- |
| A | **Separate shift registers.** `sho` shifts SRO out; `shi` shifts into SRI. `pull` and `ldsr` load SRO; `push` and `cmp` read SRI. | no new opcode | +16 flops (SRO 16, SRI 16; the spike reports whether any program needs more than 8 bits of either) |
| B | **Second loop counter.** `ldrc imm[, rc1]`, `loop a[, rc1]` | one operand bit each | +8 flops |
| C1 | **Wider branch field.** Address `a` grows to 7 bits (programs up to 128 words) and the branch condition `c` to 4 bits (room for the new flags below). BR uses all 11 operand bits | re-encodes BR, JMP, LOOP | +1 pc bit |
| C2 | **CRC / parity register** (Player only). 16-bit LFSR `L`, left-aligned polynomial `POLY`, fed in wire order with each bit `sho` sends or `shi` samples. `crcp imm[, hi]` loads the polynomial. `bsc ...` sets the tap (off / sho / shi), clears or presets `L`, and sets the stuffing mode. `sho p, crc` sends `L`'s top bit and shifts `L`. New branch condition Z: `L == 0` | opcodes 0x18 (`crcp`), 0x19 (`bsc`); one mode bit in `sho` | about 40 flops |
| C3 | **Bit stuffing.** A run counter on the tapped bit stream; new branch condition U: "the run has reached N" (N = 5, either level, for CAN; N = 6, ones only, for USB). `sho p, stuff` sends the complement of the last bit without touching SRO or `L`; `shi q, stuff` samples a bit without touching SRI or `L` and sets C if it equals the last bit (a stuff error) | mode bits in `sho` / `shi`; part of `bsc` | about 6 flops |
| C4 | NRZI on `sho` / `shi` | one bit in `bsc` | 2 flops. Low-speed USB only, which is first on the cut list: not implemented unless USB is attempted |
| D | **Per-bound guard band.** Bound word 2 becomes `{n_lo[3:0], 0, is_max, width[9:0]}`; `n_lo` is signed. PASS and VIOLATION test against `n_lo` and `n_lo + width` (the v0.3 `inexact` bit is `width = 1`). The assembler computes both from the rule and one declaration per bus | bound table layout; assembler directives `.bus`, `.bound ... from/to` | 10 flops in the JUDGE pipeline (Referee only) |
| E1 | **Trigger on a set of edges.** `trig` takes a channel mask and an edge kind (rise / fall / any) | operand bits of `trig` | about 6 flops (Referee only) |
| E2 | **Snapshot tests.** `tsts ch, what`: C <- the level of channel `ch` after its last edge in the snapshot, or "the trigger was this channel's rise / fall"; A <- 1 if that answer is ambiguous (the edge is order-ambiguous with the trigger, or flagged). New branch condition A | opcode 0x1C | about 12 flops (Referee only): the levels and trigger edges kept in the snapshot, and A |
| J | **A verdict as a branch condition** (finding 3): `judge b, flag` | the unused fourth value of JUDGE's push field | 2 flops (Referee only). Not in the model yet |
| K | **Level-qualified trigger** (finding 3) | operand bits of `trig` | about 4 flops (Referee only). Not in the model yet |
| F | Short-level taint: a timestamp next to a level that, as sampled, lasted 5 cycles or less is flagged, which forces CAN'T TELL | none (part of the timestamp flags) | about 4 flops per channel. **Not an instruction, so the two-program rule does not decide it:** it is a soundness mechanism, and the owner decides |
| G | Non-PASS pulse on a chosen output pin | global config word 7 | a few flops. Host configuration, not an instruction: the owner decides |
| H | Timestamp prescaler | - | default cut (decided on 23 Nov) |
| I | `call` / `ret` | 2 opcodes | only if two programs overflow without it |

## Findings so far (13 Nov)

1. **State kept across bus events is unsafe when events can be lost.** The first I2C Fast-mode
   referee (`firmware/i2c_fm_referee.s`, 47 words) triggers on every edge of SDA and SCL and keeps
   two one-bit states in the loop counters ("a START is waiting for its SCL fall", "the bus is
   idle"). On legal traffic it judges every event of eight rules correctly
   (`tests/test_ref_i2c.py`, first test). But one event takes about 14 cycles (230 ns) to handle,
   and an event that arrives inside that window is lost (the chip counts it). After a lost START or
   SCL fall the state is out of step, and the next SCL fall is judged as tHD;STA against an SDA
   fall that was not a START: a verdict about the wrong pair of edges. The second test shows it and
   is marked as an expected failure until the program is redesigned.
2. **Short intervals are exactly the ones whose end event gets lost.** With SDA edges as triggers,
   an SCL rise that follows an SDA change by less than the handling time is lost, so a tSU;DAT
   violation is the case least likely to be judged. A device that changes SDA 100-200 ns after SCL
   falls loses every tHD;DAT event (third test: 0 of 257 judged, LOST saturated), though no
   verdict is wrong.
3. **Consequence for the candidates.** A referee program should be stateless (decide everything from
   the snapshot it holds) and should not use the start edge of an interval as a trigger. Two new
   candidates follow, to be tried next:
   - **J: a verdict as a branch condition.** `judge b, flag`: no push, no stats; C <- PASS,
     A <- CAN'T TELL (valid two instructions later, because JUDGE is two cycles). With a bound
     "min 0 ns" this is "did this edge certainly come after that one", which is how a program
     can tell a START from a data change, or whether SDA moved during this SCL low period,
     without remembering anything.
   - **K: a level-qualified trigger.** Trigger on an edge of one channel only while another
     channel is at a given level (SDA edges while SCL is high = START and STOP). Data edges then
     never trigger, so they cannot push out the SCL rise that ends tSU;DAT.
4. **Stats slots.** There are four, and the report needs nine rules: either three passes over the
   traffic with different rule groups, or count-only slots for bounds 4 and up. To decide in the
   spec diff.
5. **tVD;DAT (data valid time, maximum 900 ns) needs to know what comes next.** The spec applies
   it to data and ACK bits, not to the SDA change that prepares a STOP or a repeated START. At the
   SDA edge the program cannot know which it is, so the first version does not judge the rule at
   all (eight of nine). A stateless design can judge it one event later, once the SCL high period
   that follows has ended without a START or STOP.
6. **Bugs the review of this folder found in the first version of the model** (all fixed, each with a
   test): `bsc off` could not be assembled (YAML reads `off` as false); "preset to ones" was wrong
   for CRCs narrower than 16 bits; `tsts` could report "not ambiguous" for two trigger edges one
   cycle apart; `trig 1, rise` silently changed meaning from v0.3 (now refused); the guard-band
   test would not have caught a rounding error on the far end of the band.

## Results

(filled in as programs pass their tests on the model)

| Program | v0.3 words | v1 words | Candidates used |
| --- | --- | --- | --- |
| I2C Fast-mode referee, eight of nine rules, first version (to be redesigned: findings 1, 2, 5) | - (one rule: 10) | 47 of 64 | B, E1, E2 |

| Candidate | Used by | Verdict |
| --- | --- | --- |
