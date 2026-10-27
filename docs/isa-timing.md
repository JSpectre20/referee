# Referee engine: ISA and timing model (v0.1 DRAFT, for review)

`isa.yaml` is the single source of truth: opcodes, fields, cycle costs. In step 2 a generator
produces the RTL opcode include, the assembler, the reference model, and the table in this
document from it. This file explains the decisions and states the timing contract as numbers.
The engine grows out of `seq` (four instructions) and adds deadlines, input waits with
timeouts, branches, FIFOs, and the referee instructions TSGET and JUDGE.

## 1. Decisions (proposed in the brief), with evidence

| # | Decision | Verdict | Evidence / change |
| --- | --- | --- | --- |
| 1 | STA target 60 MHz, 40 MHz fallback | **Agree** | The demo board's firmware picks an RP2 system clock that's a multiple of the project clock, allowed up to twice it (`clock_project_PWM`, `max_rp2040_freq = freqHz*2`). 60 MHz = 120 MHz / 2, exact, 50% duty. 40 MHz = 120/3, exact but 33% duty, so STA must check the minimum pulse width at 40 MHz. The current chip has +10.7 ns setup slack at a 20 ns constraint (gds of `1ecdc36`), so 16.7 ns is plausible. Risk: CMOS5L pads are uncharacterized; the TT pad limit quoted so far is ~66 MHz. |
| 2 | Every instruction exactly 1 cycle, incl. taken branches and LOOP | **Agree**, made precise | All non-waiting instructions cost 1 cycle; taken and untaken branches both cost 1. This works because the program memory is flip-flops read combinationally, so the next PC is known in the same cycle. Only WAITD, WAITP and PULL stall, by definition (section 3). |
| 3 | 64x16 flop program memory per engine, written only while halted | **Agree** | ~1,024 flops ≈ 50-53 µm² each (PLAN; Abagel measured 49 µm²) ≈ 55k µm² plus read mux. Today's whole chip is 82k µm² = 9.1% of 6x4, so one engine fits easily. SRAM would put the macro's ~6.3 ns clock-to-output (Abagel, slow corner) inside the 1-cycle branch path at 16.7 ns, so no SRAM for programs. |
| 4 | Loaded "through the UART console" | **Adjusted** | Through the binary host port on `main` (`src/host_port.v`), extended with commands for program words, the bounds table, pin modes and the FIFOs (section 6). The teammate's text console isn't on `main`; it can sit on top later. |
| 5 | Registers X, Y, SR, repeat counter, deadline | **Agree**, widths set | X, Y 16-bit (they hold timestamps); SR 16-bit (SPI 16, I2C 9 bits incl. ACK); RC 8-bit; DL 16-bit. Plus flags T, C and the timestamp flags FX, FY. |
| 6 | MARK / WAITD n, signed compare, half-range rule in the assembler | **Agree**, plus `DL n` | `DL n` advances the deadline without waiting. WAITP needs it to set a timeout without spending it. Immediates are 11 bits (≤ 2047 cycles; a 115200-baud bit is 521 at 60 MHz). Longer waits chain DLs; the assembler enforces the half-range rule (section 3.4). |
| 7 | WAITP pin, level with a deadline timeout; pin event wins a tie | **Agree** | Sets flag T. Branch on it with JT/JNT. |
| 8 | SET pins, fixed 1-cycle OUT-to-pad latency | **Agree** | Per-pin mode (input / push-pull / open-drain) set by the host while halted. In open-drain mode `set p, 1` releases the line (oe = 0). |
| 9 | Shift one bit out or in through a pin | **Agree** | SHO / SHI, with an LSB-first or MSB-first bit. SHI also sets C. |
| 10 | TX/RX FIFOs | **Agree** | IN (host→engine) and OUT (engine→host), 4 x 8 bits. PULL blocks while IN is empty; PUSH never blocks (drop + sticky overflow), so a full FIFO can't change program timing. |
| 11 | TSGET and JUDGE with a bounds table and a verdict FIFO | **Agree**, split made explicit | `tsget X, ch, e` and `tsget Y, ch, e` load two timestamps (and their stale/missed flags); `judge b` computes m = Y - X with the proved verdict unit against bounds[b] and pushes {verdict, flags, b, m} to the VERDICT FIFO (never blocks, sticky overflow). The split makes "JUDGE passes the right timestamps" a register-transfer property that formal can check. |
| 12 | Judging on ui_in pins only | **Agree**, pins fixed | Judge channels = ui_in[4..7] (4 edge_ts channels). ui_in[3] is the host UART RX. ui_in pads are input-only, so judging can't disturb the bus, by construction. |
| 13 | HALT | **Agree** | The engine stops, pins keep their level, the host sees "halted". |
| - | **Added:** conditional branches and pin test | needed | Protocols decide things (I2C ACK/NACK, clock-stretch timeouts, SPI target CS). So: JT/JNT (timeout flag), JC/JNC (sampled bit) and TSTP (sample a pin into C). LOOP covers counted loops. |
| - | **Added:** NOP, LDRC, LDSR | needed | Loading the counter and the shift register from immediates. |

## 2. Machine state

| State | Bits | Notes |
| --- | --- | --- |
| PC | 6 | 64 words |
| X, Y | 16 each | general; timestamps for JUDGE |
| SR | 16 | shift register |
| RC | 8 | LOOP counter |
| DL | 16 | deadline |
| T, C, FX, FY | 1 each | flags |
| NOW | 16 | free-running cycle counter, shared with edge_ts; read-only |
| Pin modes | 8 x 2 | host-written while halted |
| Bounds table | 16 x 21 | n_fx (cycles, 4 fractional bits) + is_max; host-written while halted |
| FIFOs | IN 4x8, OUT 4x8, VERDICT 4x16 | |

## 3. Timing contract (all numbers in cycles of the engine clock)

### 3.1 Issue
- An instruction **issues** in the cycle it executes. Straight-line code issues one per cycle.
- Every instruction costs **exactly 1 cycle** unless it waits (WAITD, WAITP, PULL).
- **Branches** (JMP, JT, JNT, JC, JNC, LOOP), taken or not: 1 cycle, no penalty.
- **Start:** the first cycle the engine is running issues the instruction at address 0.
  Halting (HALT or host) cancels any wait. Pins keep their level.
- **Program memory and configuration** (pin modes, bounds table) are written only while
  halted; writes while running are refused (NAK) and ignored.

### 3.2 Outputs
- A SET or SHO issued in cycle t changes its pad from **cycle t+1**: exactly one cycle, for
  every pin and mode. Pads change at no other time and never glitch (registered outputs).

### 3.3 Inputs
- Every input passes a 2-flop synchronizer. An external edge at real time t is visible to
  WAITP / TSTP / SHI from a cycle between t+1 and t+3. The exact window comes from the
  edge_ts proof: 1 - 1/K .. 2 + 1/K cycles after the edge to the start of the reporting cycle.
- Edge timestamps (TSGET) carry the proved uncertainty: an interval between two external edges
  is measured to within 2 cycles (`docs/verdict.md`), which is the u used by JUDGE.

### 3.4 Deadlines
- MARK: DL ← NOW, at the cycle MARK issues.
- `waitd n` issued at t: DL ← DL + n; the next instruction issues at **max(t + 1, DL)**. So a
  loop `mark; ...; waitd P; ...; jmp` paces itself at exactly P cycles per iteration, whatever
  the body costs (as long as it costs < P): no drift.
- `dl n`: DL ← DL + n, 1 cycle, no wait.
- `waitp q, v` issued at t: stalls until in[q] == v or NOW - DL ≥ 0, compared by **signed
  difference** (an already-passed deadline fires at once). It ends the cycle after the
  condition is seen; if the condition is true at t, it costs 1 cycle. If both happen in the same
  cycle, **the pin event wins** (T ← 0). DL is unchanged.
- **Half-range rule:** with a 16-bit DL, a deadline is valid only while DL - NOW < 2^15. The
  assembler tracks every path's DL advance between waits and rejects programs that could
  exceed 2^14 (a conservative static bound).

### 3.5 FIFOs and JUDGE
- PUSH and JUDGE never stall; a full FIFO drops the entry and sets a sticky overflow bit.
  Their timing is the same whether or not the FIFO is full.
- PULL stalls while IN is empty. Its timing is host-paced, so contracts restart at the next MARK.
- `tsget r, ch, e` returns the latched timestamp of that edge. If the edge is being reported in
  the same cycle, it returns NOW (the value edge_ts is latching). F(r) ← stale | missed.
- `judge b`: verdict(m = Y - X, bounds[b], invalid = FX | FY), using the proved rule (u = 2,
  sharp VIOLATION). It pushes {verdict[1:0], FX, FY, b[3:0], m[7:0] saturated}.

## 4. Instruction set

Generated from `isa.yaml` in step 2; hand-written here for review.

| Opcode | Syntax | Cycles | Effect |
| --- | --- | --- | --- |
| 0x00 | `nop` | 1 | nothing |
| 0x01 | `halt` | 1 | stop; pins keep their level |
| 0x02 | `set p, v` | 1 | output pin p ← v (pad at t+1); open-drain: 1 releases |
| 0x03 | `sho p[, msb]` | 1 | pin p ← SR[0] (or SR[15]); SR shifts, refills with 1 |
| 0x04 | `shi q[, msb]` | 1 | C ← in[q]; SR shifts it in |
| 0x05 | `tstp q` | 1 | C ← in[q] |
| 0x06 | `mark` | 1 | DL ← NOW |
| 0x07 | `dl n` | 1 | DL ← DL + n |
| 0x08 | `waitd n` | ≥ 1 | DL ← DL + n; next issues at max(t+1, DL) |
| 0x09 | `waitp q, v` | ≥ 1 | until in[q] == v (T ← 0) or deadline (T ← 1); event wins a tie |
| 0x0A | `jmp a` | 1 | pc ← a |
| 0x0B / 0x0C | `jt a` / `jnt a` | 1 | branch if T / !T |
| 0x0D / 0x0E | `jc a` / `jnc a` | 1 | branch if C / !C |
| 0x0F | `loop a` | 1 | if RC != 0: RC--, pc ← a |
| 0x10 | `ldrc imm` | 1 | RC ← imm |
| 0x11 | `ldsr imm[, hi]` | 1 | SR low (or high) byte ← imm |
| 0x12 | `pull[ hi]` | ≥ 1 | SR byte ← IN FIFO (stalls while empty) |
| 0x13 | `push[ hi]` | 1 | OUT FIFO ← SR byte (never stalls) |
| 0x14 | `tsget r, ch, e` | 1 | X or Y ← timestamp of judge channel ch, rising/falling |
| 0x15 | `judge b` | 1 | VERDICT FIFO ← verdict(Y - X, bounds[b]) |

Encoding: opcode in [15:11], operands in [10:0]; fields as in `isa.yaml`.

## 5. Two programs, to check that the ISA is enough

**UART TX, 8N1, any number of bytes, no drift** (60 MHz, 115200 baud: 521 cycles per bit):
```
start:  pull                 ; SR[7:0] <- next byte (waits for the host)
        mark                 ; timing restarts here
        set 0, 0             ; start bit, pad at +1
        waitd 521
        ldrc 7               ; 8 data bits
bit:    sho 0                ; LSB first
        waitd 521
        loop bit
        set 0, 1             ; stop bit
        waitd 521
        jmp start
```
Every bit edge lands exactly 521 cycles after the previous one, because WAITD is relative to
the deadline, not to "now". Static timing is complete: each path's cycle count is known.

**Referee: I2C setup time on ui_in[4] (SDA) and ui_in[5] (SCL)**, judged against bounds[0]
(minimum 100 ns = 6 cycles at 60 MHz):
```
wait:   waitp 13, 1          ; in[13] = ui_in[5] = SCL high (no deadline set: dl a large n first)
        tsget X, 0, 0        ; SDA's last rising edge  (channel 0 = ui_in[4])
        tsget Y, 1, 0        ; SCL's rising edge        (channel 1 = ui_in[5])
        judge 0              ; verdict FIFO <- PASS / VIOLATION / INDETERMINATE + flags
        waitp 13, 0
        jmp wait
```
(Falling SDA edges need a second TSGET/JUDGE pair with e = 1; that is the firmware's choice.)

## 6. Host port additions (binary, over the existing UART)

New 4-byte commands, in the format of `src/host_port.v`. Each is ACK/NAK'd; writes are refused
while running.

| Command | Bytes | Notes |
| --- | --- | --- |
| Write program word | `57 addr hi lo` | addr 0..63 (was 0..31) |
| Write bounds entry | `42 idx byte sel` | builds the 21-bit entry byte by byte (sel 0..2) |
| Pin mode | `4D pin mode 00` | mode 0 input, 1 push-pull, 2 open-drain (replaces the raw 'O' mask) |
| IN FIFO push | `50 byte 00 00` | NAK if full |
| OUT FIFO pop | `70 00 00 00` | data = byte; NAK if empty |
| VERDICT pop | `56 00 00 00` | 2 replies' worth: sent as `06 hi` then `06 lo`, or NAK if empty |
| Status | `51 00 00 00` | adds the sticky overflow bits |

## 7. What step 2 will prove and test

- **Formal:** every non-waiting instruction issues in exactly 1 cycle; WAITD next-issue =
  max(t+1, DL); deadline monotonicity (DL changes only by MARK / DL / WAITD); program and
  configuration writes only while halted; JUDGE feeds the verdict unit exactly Y - X, FX | FY
  and bounds[b]; pads change only one cycle after SET/SHO.
- **Lockstep:** the RTL against the model generated from `isa.yaml`, on random programs.
- **Firmware:** UART TX and RX, and JUDGE on an external UART at in-spec and off-spec baud, on
  RTL and gate level. Then harden at 6x4, STA at 60 MHz, and the area ledger.

## 8. Open questions for review

1. **Engine count:** v0 has one engine. Two engines (generator plus referee) need a second
   64x16 memory (~55k µm²). Decide at the 8 Nov gate from the v0 area.
2. **Judge channels:** 4 (ui_in[4..7]) at ~37 flops each. Enough for I2C (2) and SPI
   (CLK, MOSI, CS = 3). More channels would have to come from ui_in[0..2].
3. **10BASE-T:** at 60 MHz a Manchester half-bit is 3 cycles. A firmware loop of SHO + WAITD +
   LOOP is exactly 3 cycles, with zero slack and two transitions per bit, so it isn't practical.
   It needs the hardware serializer from the PLAN (stretch).
4. **WAITP on judge channels:** allowed (they are inputs 12..15), so the referee can sync on
   the bus it watches. Is that wanted?
