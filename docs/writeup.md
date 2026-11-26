# Referee: write-up

*A living document, updated at every checkpoint. Status as of 18 November 2026. Where something is
in progress or not yet committed, the text says so.*

## 1. The idea

Most protocol emulators answer one question: can this chip speak protocol X? Referee also answers
a second one: **did this bus, ours or anyone else's, keep its timing contract?**

Referee speaks serial protocols written as firmware, and it judges buses. Point four input pins at
a bus and load a short program, and for every interval the program names (for example, I2C's data
setup time before each SCL rise) it answers whether the bound was met. The answer has three values:

| Verdict | Meaning |
| --- | --- |
| PASS | the real interval met the bound |
| VIOLATION | the real interval broke the bound |
| CAN'T TELL | the chip cannot decide from what it saw |

The third value is the point. A chip that samples pins with its own clock cannot know when an edge
happened to better than about a clock cycle, and a sample taken right at an edge can go either
way. A two-valued checker hides that and is sometimes wrong. Referee carries the uncertainty into
the verdict: **a PASS or a VIOLATION is meant to be proved right** under assumptions that are
written down, and everything the chip cannot settle is CAN'T TELL. Today the proof covers two kinds
of interval (section 3); closing it for the others is planned work. A tool that refuses to guess is
what you want when the question is "does this part meet its datasheet?".

The second idea is that **timing rules are firmware too**. A rule nobody planned for, on a
protocol nobody planned for, is a new program, not a new chip (section 7).

## 2. What is on the chip

**The one-engine chip** (ISA v0.3; hardened green on 13 November at 8x4 tiles, 60 MHz, +2.7 ns of
slack at the slow corner, 34.5% of the core; simulation only, no silicon or FPGA run yet; on `main`
until 26 November, now the Referee half of the two-engine chip below):

```
 host PC / demo board ── UART ──> host port ──> engine ──> pins (uio, uo_out)
                                     │            ▲
 4 judge channels (ui_in[4..7]) ──> edge timestamps ──> snapshot ──> verdict block ──> FIFO + stats
```

- **A timing engine.** 24 instructions; every one takes exactly one clock cycle except the three
  waits, and a `set` changes its pin exactly one cycle later. Two deadline registers keep schedules
  free of drift: one for the schedule, one for timeouts. UART, SPI and I2C controllers are 16 to 63
  words of firmware in a 64-word memory.
- **An edge-timestamp unit** on four judge channels: every rising and falling edge gets a 16-bit
  timestamp in hardware.
- **A snapshot.** A trigger edge freezes all timestamps at one instant, one cycle after the
  trigger is reported. Edges too close to the trigger to put in order are flagged (ORD).
- **A three-valued verdict block.** `judge` compares the interval between two snapshot timestamps
  with a bound written in nanoseconds. The uncertainty is u = 2 cycles (33 ns at 60 MHz).
- **Stats and a verdict FIFO**, so the host can read counts, extremes and every non-PASS verdict
  without streaming each one.
- **Pin ownership and checked images.** Each image carries a CRC, and RUN refuses an image that
  fails it or claims a pin it does not own. No pin is driven before the first successful RUN.

**Two engines (17-19 November; merged into `integration` and `main` on 26 November).** One parameterized
engine module, built twice: the **Referee** (the engine above, 64 words, all judging hardware) and
a lean **Player** (96 words, no snapshot, verdict block, bounds or stats), so the chip can speak a
protocol and judge a bus at once. Locally: 17 of 17 chip tests, 39 unit and lockstep tests and the
formal tasks pass. Hardening (section 12): the first run missed 60 MHz by 1.77 ns, the fix moved
that to 0.19 ns on one endpoint, and the second fix (a registered readback address) met 60 MHz
(+1.80 ns) but its gate-level test failed, 12 of 17 (ledger rows 30 and 31). With both fixed, run
44425384876 (19 November) is green throughout: +1.34 ns at the slow corner at 60 MHz, 54.4% of the
core, a 1 h 43 min build, precheck, and the gate-level tests with every flip-flop starting at X and
with them starting at 0, at 1 and at random.

**An FPGA build** (merged into `integration` on 17 November, `fpga/`): the current one-engine chip,
unchanged, at 60 MHz in a Zynq-7020 (ZedBoard first, PYNQ-Z2 from the same script), judging a real
I2C bus that the Zynq's own I2C controllers (third-party IP) drive on Pmod pins. Lint and an
end-to-end Icarus simulation pass, and Yosys estimates about 2,300 LUTs. **It has not been through
Vivado or run on a board yet**; timing at 60 MHz on the board is unverified.

### Non-intrusive by construction

```
                 the bus being judged (SDA, SCL, CS, ...)
                                   │
                                   ▼
  ui_in[4..7]   input-only pads: there is no output driver behind them
                                   │
                                   ▼                 global config word 6, per channel:
        judge-channel mux  <──── uio_in[0..7]   <──  "watch a uio pin instead" (the only
        (default: ui_in)          (bidirectional       intrusive option; NI = 0 when any
                                   pads)               channel selects it)
                                   │
                                   ▼
        2-flop synchronizers ──> edge_ts ──> snapshot (queue) ──> verdict ──> FIFO / stats ──> host
                                                                    │
                                              every verdict header carries the NI bit
                                              (1 = all four channels on ui_in)

  Pin drivers exist only on uio[0..7] and uo_out. Only the engines drive them (plus the host UART's
  TX on uo_out[4]): an owned pin, after a checked RUN, one cycle after a `set` / `sho`. Nothing on
  the judging path has an output.
```

The judging path, from the pad to the verdict, has no connection to any pin driver. The
judge-channel mux can route a channel to a `uio` pin instead, so that the Referee can judge a bus
the Player is driving; that is the only intrusive configuration, and the verdict header and the
engine status both carry the NI bit, so a report says which it was. (A Referee program could still
own and drive `uio` pins of its own; its image's pin claims, checked at RUN, say whether it does.)

## 3. What a verdict means, and what is proved

The rule, with m the measured interval and N the bound (both in cycles), for a minimum bound:
PASS if m - u >= N, VIOLATION if m + u <= N, CAN'T TELL otherwise (mirrored for a maximum).
Bounds written in nanoseconds are rounded so that each side stays sound whichever way the
conversion rounds.

**Proved (SymbiYosys, unbounded):** external edges are placed on a grid of 4 or 8 steps per clock
cycle, and a sample taken next to an edge may resolve either way. Under that model, for a rising
edge on one pin to a rising edge on another read through the snapshot, and for a high pulse on one
pin, a PASS or VIOLATION is never wrong about the interval between the moments the pads switched.
With u = 1 instead of 2 the proof fails, and the
counterexample is kept in the repository (a true interval of 1.25 cycles measured as 0).

**Assumed, and written down:** each level on a judged pin lasts at least 4 cycles; the judged pins
have no skew between them; the chip runs at the clock the program was assembled for; a metastable
sample settles within a cycle; and the **bus envelope**: the proof is about the interval between
the moments the pads switched, which is the specification's interval only if every edge completes,
monotonically, within the declared rise / fall time and every level reaches VIL / VIH. A runt that
never reaches VIH, or a bounce, is outside the envelope and can be misjudged with no flag (ledger
row 24); no digital pad can detect a level that stops at 60% of the supply. ISA v1 declares the
edge times in `.bus`; the short-level flag's threshold is to be derived from them per channel, so
that runts and bounces up to that length are flagged (in progress, row 24).
**Not yet proved:** intervals involving a falling edge, "the latest edge, whichever way", or two
edges of one pin, at sub-cycle resolution.

**Slow edges and the pad threshold (ISA v1, in the model only).** A specification measures an
interval between fixed levels of an edge (I2C: 30% and 70% of the supply); the chip's pad switches
somewhere else, and on a slow edge that moves the pad's view of the interval. The v1 guard band
(section 5) widens each bound's CAN'T TELL band by exactly what a declared bus can hide. Its
formulas have a written soundness argument, a brute-force check against an independent waveform
model (section 4) and a reviewer's own check; they are not formally proved.

## 4. How it is verified

| Layer | What | Why it is there |
| --- | --- | --- |
| Formal, tick level | verdict soundness through the snapshot; edge-timestamp latency; negative controls (u = 1, a glitch, one-sided rounding, a snapshot one cycle early) that must FAIL | the only layer that sees sub-cycle timing and metastability |
| Formal, cycle level | engine properties (one cycle per instruction, deadlines, pins change only one cycle after `set` and only if owned, no image write while running, the snapshot equals the timestamp state of the right cycle); for two engines also the Player's configuration and per-engine "ran" | the timing contract firmware relies on |
| Lockstep | the RTL against a cycle-accurate reference model on random programs, every cycle | two independent descriptions: the model is written from the spec, not the RTL |
| Generated ISA | one YAML file generates the RTL constants, the assembler tables, the model's tables and the instruction table in the docs | nothing can drift apart |
| Chip tests | cocotb tests through the pins (14 on the one-engine chip, 13 of them also on its gate-level netlist; 17 on the two-engine chip, all of them also on its gate-level netlist in CI except one timing-only test, with flip-flops starting at X, 0, 1 and random), including an independent oracle that checks every verdict against the interval the test generated, with late-resolving samples | end to end |
| Firmware on the model | every protocol program against a device model written from the protocol, with negative controls (a bit time 3% off, the wrong clock phase) | a program counts only if an independent device accepts it |
| Referee programs | random traffic with intervals drawn around every bound; an oracle that reads the waveform alone; fuzzing with glitchy waveforms; a slow-edge model in which the pads see each edge where their threshold is crossed | a verdict about the wrong pair of edges is the worst failure a referee can have |
| Guard-band brute force | see below | the guard formulas must not share their assumptions with their test |
| Independent reviews | adversarial reviews by a second AI that did not write the code, with their own fuzzers (section 9) | a test written with the code shares its blind spots |

A lockstep test cannot find a bug that the model shares with the RTL. Several of the bugs below
were found by the layers that do not share the model: the proofs, the independent oracles, the
waveform brute force and reviews against the specification text.

**The independent-waveform brute force** (`spike/tests/test_v1_core.py`, ISA v1 work, not yet
committed). It checks the guard band without using the assembler's formulas. For each trial it
draws a random bus (clock 40-60 MHz, clock error, skew, a pad threshold range, rise and fall times,
RC or linear edges), a random bound and its two ends, and a true interval near the bound. It then
builds each edge's voltage as a function of time (a linear ramp, or an RC exponential, with a true
transition time anywhere up to the declared one) and finds, **by bisection on that waveform**, when
it crosses the specification's level and when it crosses the pad's threshold (anywhere in the
declared range, plus a delay up to the skew). That gives the interval the pads see; the chip's
count is taken at the extremes the 2-cycle bound allows, and the verdict the assembler's bound
gives is compared with the true specification interval. 12,000 trials: no wrong PASS or VIOLATION.
Negative controls on the same trials must fail: the guard set to zero (over 100 wrong), the skew
term dropped, the clock-error terms dropped, and the old "threshold between 30% and 70%" assumption
on pads that really switch at 24-61% (10 wrong in 12,000). It caught two of the AI's own bugs
(ledger rows 22 and 23): it found the first, that the threshold range must include zero, by
itself, and it now reproduces the second, the false 30-70% assumption that a reviewer found, as a
standing negative control.

## 5. ISA v1: sized by firmware first

Before changing the hardware, every candidate addition went into a copy of the reference model,
and the programs were written against it. **An addition used by fewer than two real programs is
dropped**, where a real program passes a bus-level test against an independent device model.
Soundness mechanisms are not instructions and are decided by the owner, not by the count. The diff,
with the evidence for each item, is `spike/ISA-V1-DIFF.md` (a draft; its final form is due with the
ISA v1.0 tag on 21 November). **Everything in this section is in the model, not yet in the RTL.**

- **Separate input and output shift registers** (an SPI controller for all four modes drops from
  29 + 28 words in two files to 26 in one; an SPI target becomes possible), a source flag `o` on
  `cmp` / `push` (a UART receiver with parity drops from 42 to 33 words), a **CRC / parity
  register** and **bit stuffing** (UART 8E1; CAN 2.0A TX in 65 words and RX in 120, which is more
  than the 96-word engines in the plan).
- **Triggers and snapshot tests:** a trigger on a set of edges (`trig <mask>, rise|fall|any`), a
  level-qualified second trigger term (`trigq`: for I2C, SDA edges only while SCL is high, so data
  edges never crowd out the SCL rise that ends tSU;DAT), and `tsts` (which edge triggered, a line's
  level, flag A when that is ambiguous).
- **J, a verdict as a branch condition** (`judge b, flag`): with a bound of "min 0 ns" it asks
  "did this edge certainly come after that one", which lets a program classify an event from the
  snapshot instead of remembering state.
- **A referee must never judge from a stale state.** If the chip loses an event, the referee is
  *poisoned*: every verdict is CAN'T TELL until the firmware states, with `resync`, that its
  verdicts depend only on the snapshot it holds; `resync` counts only for the snapshot the program
  last read, and `rel` clears that mark.
- **Event loss: a snapshot queue of depth 2, chosen by measurement.** One snapshot left 43.5% of
  violating intervals unjudged on the test mixes (the 17 November program). Measured per rule on 18 November (section 6),
  depth 2 and depth 4 gave identical results on both declared buses, so the queue went from the
  depth 4 chosen on 17 November to depth 2: about 14,000 um2 instead of 42,000. Checking every rule
  in dedicated hardware also left nothing unjudged, at 6 to 16 times the area of the depth-2 queue.
- **A guard band, declared per bus.** The user declares the bus once: clock tolerance, skew, the
  **pad's threshold range** (`vil`, `vih` as fractions of the supply: required, since the pad's
  real range, for example JEDEC LVCMOS33's 0.8 V / 2.0 V on 3.3 V, is 24-61%, not 30-70%), and each
  line's rise and fall time (from the pull-up and capacitance, or a scope). The assembler derives
  every bound's two-sided guard from that and from the specification level at each end of the
  interval. The bound word gets a signed lower end, because a "min 0 ns" bound with a guard needs a
  negative one.
- **Flag W, for slow edges.** An SDA and an SCL edge that reach the pads close together may truly
  have come in the other order at the levels the specification counts, so a data change can look
  like a START or STOP, or the reverse. `twin ch, n, rise|fall|any` sets per channel and direction
  how late an edge can arrive (the assembler computes it from the declaration), and the hardware
  marks W on a snapshot whose trigger has an edge of another channel within that window, before or
  after it. The program then loses track (poison) instead of misreading.
- **The receiver rule, as our definition (option A, owner, 18 November), motivated by UM10204.**
  UM10204 Rev. 6 (4 April 2014), Table 10, note [3]: "A device must internally provide a hold time
  of at least 300 ns for the SDA signal (with respect to the VIH(min) of the SCL signal) to bridge
  the undefined region of the falling edge of SCL." The current Rev. 7.0 (1 October 2021) replaces
  that note with a transmitter-side rule (Table 11, note [2]): "Ensure SCL drops below 0.3VDD on
  falling edge before SDA crosses into the indeterminate range of 0.3 VDD to 0.7 VDD." So the 300 ns
  classification is the project's definition, motivated by the Rev. 6 note and by what receivers
  implement, not a rule of the current specification. The referee classifies an SDA change that
  SCL's fall follows within 300 ns as a possible data change with a negative hold time. As first
  built it called every such change data; for an SDA **rise** that is wrong, because the rise may be
  a STOP and UM10204 sets no minimum time from a STOP to the next SCL fall (ledger row 25). Being
  changed to: an SDA **fall** in the window is a VIOLATION naming both rules (tHD;STA if a START,
  tHD;DAT if data; both are broken); an SDA **rise** is CAN'T TELL. The tHD;DAT verdict uses the
  guard band, so a truly
  negative hold that looks positive at the pads is CAN'T TELL, never PASS, and the assembler
  refuses a bus whose SDA windows reach 300 ns. Why: the alternative, poisoning on every such case,
  also poisoned legal traffic (with data holds of 0-33 ns, 51.6% of it went unjudged).
- **The short-level taint.** A timestamp next to a level that, as sampled, lasted 5 cycles or less
  is flagged, so its verdict is CAN'T TELL. The proofs assume levels of at least 4 cycles; this
  puts a hardware flag on that assumption instead of trusting it.
- **The Referee gets 112 or 128 words** (owner, 18 November; first set at 96, but the stateless
  I2C referee then used 96 of 96, leaving no room for a firmware fix after the freeze): 128 if a
  local synthesis estimate of the full v1 projects a build under about 4 hours with at least +1 ns
  of slack, otherwise 112. The Player stays at 96. The RTL change needs another hardening run.
- **The stateless I2C referee (in the model; under independent review, not yet committed).** The
  earlier referee (82 words) kept one bit of protocol state; the stateless one reads it from the
  snapshot with J, so an ambiguous event skips its own verdict instead of poisoning those after it.
  With the STOP fix, candidate M and the per-channel short-level threshold it is 102 words.
- **A stated limit:** a firmware referee needs about 14 cycles between events, so it follows SPI
  only up to about 2.1 MHz SCK, the same at queue depths 1 and 2. Fast SPI judging is out of scope.

**What goes into the RTL first** (the owner's cut line, 18 November): must have = the soundness set
(queue depth 2, poison / resync / desync, trigger mask + `trigq` + `tsts`, the signed guard band, J,
the short-level taint, made programmable per channel, and candidate M, the stale-edge tag) + 7-bit
addresses + image format 4 with 8-bit addressing + a 112- or 128-word Referee; should have = the
separate shift registers and the CRC unit; cut first = count-only stats for all 16 bounds and the
violation pulse pin.

## 6. Measurement definitions, and every change to them, with the reason

Coverage numbers are only meaningful if their definitions are fixed and public. These are every
change made this week, in order, with the reason, and with the old number still reported, so that
no reader has to take a new definition on trust.

**1. The single 5% gate became three measurements (owner, 18 November).** The original gate (14
November) was: under 5% of the violating intervals of at least 5 cycles get no verdict, over all
the traffic mixes. After the slow-edge and receiver-rule fixes the I2C referee scored 31.5% on the
fast declared bus and 52.6% on the slow one, against 3.4% before the redesign. That one number
mixed three different things, so it was replaced by three, and the old number is still computed
and split by cause (`spike/tests/test_ref_i2c_gate.py`, not yet committed):

| Measurement | Definition | Why |
| --- | --- | --- |
| 1. Per rule | violate one rule at a time; under 5% of its violating intervals of at least 5 cycles unjudged | an all-mix number lets one rule's loss hide behind another's coverage |
| 2. First violation reported | in each transfer with a gross START / STOP violation, is that first violation reported as VIOLATION? Only cases inside the truly ambiguous band around 300 ns may be CAN'T TELL. Target: at least 95% | what a user needs is that the violation that matters is reported, not that every later interval of a broken transfer is |
| 3. Lost after an ambiguity | the JUDGEs poisoned after a poisoning START / STOP event | a stated cost of soundness, reported, not gated |

The old gate's unjudged intervals on the fast bus, split by cause (first measurement, 18 November):
27.8% inside the 300 ns band, 47.9% certainly data but poisoned anyway, 24.3% other. The middle
group is what the firmware change below and the stateless referee are meant to recover.

**2. tHIGH and tLOW are counted from 6 cycles, not 5.** The first per-rule run showed about 6% of
violating tHIGH intervals unjudged. The cause was found before anything was committed: it is
entirely the short-level taint, which flags every level of 5 cycles or less **by design**, and a
tHIGH or tLOW interval *is* one level of one line. With the taint switched off, 0 of 948 were
unjudged on the fast bus (2 of 948 on the slow one). So for these two rules measurement 1 counts
from 6 cycles; the 5-cycle ones are CAN'T TELL by the definition of the taint.

**3. The 300 ns band.** Under the receiver rule, an SDA change while SCL is high is a START / STOP
if SCL's fall comes 300 ns or more later, and data if it comes sooner. Near 300 ns the chip cannot
tell which, within a band of 18 cycles (300 ns) plus or minus a margin: the 2-cycle measurement
uncertainty plus how far the declared bus lets an SCL fall and an SDA edge move at the pads. Inside
the band, CAN'T TELL is the correct answer, so measurement 2 counts it as allowed. That includes a
START or STOP whose **own** SCL fall lies in the band: whether it was a START / STOP at all cannot
be decided, so neither can its rule.

**4. Two declared buses, fast and slow.** Every measurement runs on both: a "fast" declaration
(10 ns edges, 2 ns skew), which is what the whole-cycle test traffic really is, and the "slow"
example bus of the I2C referee (2.2 kohm x 100 pF, so 186 ns rises, 20 ns falls, 5 ns skew). Both
use the LVCMOS33 threshold range (24-61%). The slow bus shows what slow edges cost; it is not a
second chance at a target.

**Results so far** (queue depth 4, which per rule gives the same numbers as depth 2; model only):

- Measurement 1, fast bus: every rule 0% unjudged except tHD;STA, 5 of 43 (11.6%), all in the
  300 ns band: **tHD;STA misses the per-rule 5% target.** Slow bus (first measurement, before the
  firmware change below): tSU;DAT 100%, tLOW 53.1%, tHD;STA 23.3%, tSU;STO 19.2%, tSU;STA and tBUF
  0% (section 11 says why).
- Measurement 2: first 38.9% (fast) and 23.1% (slow). The program then got the words it needed
  (96-word Referee): at an SDA event that SCL's fall follows closely, it waits for that fall and asks
  J whether the two are certainly less than 300 ns apart; if so it reports the negative hold as a
  VIOLATION instead of poisoning. Now **91.3% on the fast bus (84 of 92) and 43.6% on the slow bus
  (34 of 78): below the 95% target.** The remaining fast misses are cascades (poison from another
  ambiguity swallowing a later report), and 3 are holds of -2 or -3 cycles, within u of the 0 ns
  bound and so CAN'T TELL by the verdict rule. The stateless referee is meant to remove the
  cascades; that is expected, not measured.
- Measurement 3 (first measurement): 5,050 poisoned JUDGEs on the fast bus, 7,974 on the slow bus,
  over all mixes.
- Legal traffic: 0% unjudged on both buses, for every data hold tested from 0 ns (measured after
  option A), on the dense test mixes. After more than 273 µs of idle bus, or of constant SDA, it is
  not 0% (ledger row 26); sparse-traffic mixes are being added.

## 7. Generality

The thesis is that a new timing rule, on a protocol nobody planned for, is a program. What makes
that possible is a small set of primitives, none of them specific to one protocol:

| Primitive | What it gives a referee program |
| --- | --- |
| edge timestamps | every edge of four channels, timed in hardware, whatever the program is doing |
| snapshot + queue | all timestamps frozen at one instant per event; events wait instead of being lost |
| trigger mask, level-qualified trigger | the events are the protocol's own (an SCL edge; SDA while SCL is high) |
| `tsts` | which edge triggered, a line's level, and whether that answer is ambiguous |
| J | "did this edge certainly come after that one", as a branch, from the snapshot alone |
| the guarded verdict | three values, sound within the declared bus envelope (section 3), with the guard derived by the assembler |
| poison / resync | a program that keeps state cannot give a confident verdict from a stale state |
| flag W | two edges close enough at the pads that their true order is unknown are never misread |

The **two-program rule** keeps the primitives general: an instruction that fewer than two real
programs use is dropped. In the model today the I2C, SPI and UART timing referees share them (the
UART referee was 23 words on 17 November; the SPI referee is 50).

**Planned (not built): a rule-table front end.** Each stateless rule becomes one row of a table:
trigger edge, optional level qualifier, reference edge, minimum or maximum, the bound in
nanoseconds, and the specification level at each end. The assembler generates the stateless referee
and its guard bands from the table. The I2C and SPI referees are to be re-expressed as tables, and
the generated programs must pass the same tests; rules that need state stay hand-written and are
labelled as such. The time-boxed firmware proof is to target the generated template (sound for any
channels and bound), not one hand-written program; a bounded proof is the honest fallback.

**Planned (after the 14 December RTL freeze): 4-5 new referees written from datasheets as tables**,
on the frozen design: UART bit timing, 1-Wire, SMBus, WS2812 and CAN bit timing, with the hours and
lines each took logged. That is the test of the thesis: rules written after the hardware was fixed.

## 8. Bug ledger

Every real bug found so far, how it was found, and what now stops it from coming back. Rows 15-32
were found this week; the ISA v1 ones (17-29) are fixed or being fixed in the model and are not yet
committed. Mistakes in our own claims and plans are listed too (rows 24, 29, 32): showing what was
corrected, and how, is part of the method.

| # | Bug | Found by | Consequence if shipped | Fix and regression |
| --- | --- | --- | --- | --- |
| 1 | An uncertainty of u = 1 cycle is unsound (an interval can be misread by more than a cycle when a sample resolves late) | the tick-level proof; zero-delay simulation had hidden it | false PASS / VIOLATION | u = 2; proof task `u1` must fail (counterexample kept); the chip-test oracle now models late-resolving samples |
| 2 | Ordering gap: a snapshot taken in the trigger's own report cycle could miss an edge that truly came before the trigger. The first proof hid it behind a harness condition ("the earlier edge has been reported") | re-reading the proof's assumptions while revising the ISA | a false PASS | snapshot one cycle later, edges in the uncertain window flagged ORD; the proof now goes through the real snapshot with no harness condition; task `snap_early` must fail |
| 3 | The trigger edge itself was flagged ORD, so the referee could never say PASS | the snapshot lemma (formal); the lockstep was blind because the model shared the rule | a referee that only says CAN'T TELL | trigger edge exempt from ORD; a lockstep test runs the I2C referee and needs all three verdicts |
| 4 | The proof harness overflowed: a difference wrapped at 25 bits, so an interval about 230,000 cycles off satisfied "within 2 cycles" | a spurious failure of the first combinational proof | a proof that fails for the wrong reason: the wrapped assumption admitted impossible cases | differences one bit wider; negative controls that must fail around every theorem |
| 5 | The expected-CRC registers could be written during the RUN check, so the image checked might not be the image run | a review of the RUN check before a checkpoint | an unchecked program running | writes gated in hardware; formal (constant while not halted); a lockstep mutant hammers the write strobes in every check cycle |
| 6 | A chip test hung, instead of failing, when the power-up baud divisor did not match the clock | a test run that never finished | a hang instead of a clear failure | the test fails in 2 ms; the clock is named in one place and a test checks the divisor, the testbench clock and the hardening clock agree |
| 7 | Synthesis merged the four "kept" copies of the program counter that split the fetch fan-out | reading the netlist | the timing fix silently undone | one keep process per copy, checked in the netlist; formal asserts the copies equal pc |
| 8 | The first 8x4 hardening missed 60 MHz by 0.41 ns: fetch and readback shared one program read port | the hardening run (the flow fails the job on any setup violation) | no chip at 60 MHz | fetch got its own read path: +2.74 ns |
| 9 | The SPI controller's SCK was one cycle asymmetric (8 high, 6 low) in modes 1 and 3 | writing the ISA v1 SPI controller | out-of-spec clock on fast SPI | fixed in firmware; model and chip tests check every SCK phase |
| 10 | A lost event left an I2C referee's state out of step, and it judged a START's hold time against an SDA edge that was not a START | the referee test's independent oracle | a confident verdict about the wrong pair of edges | the poison rule; that test must now pass on every traffic mix |
| 11 | The first `resync` forgave an event lost while the current one was being handled; the wrong-pair verdict came back | the same test | as 10 | `resync` vouches only for the snapshot the program last read; a lost event poisons when the first snapshot after it is reached |
| 12 | The receiver rule was also applied when the event was an SDA edge seen with SCL still high and SCL fell one cycle later: a START or STOP dropped out of the referee's state | the fourth independent review's fuzz with glitchy waveforms (19 failures in about 3,500 runs) | a later START judged against the wrong rule, unpoisoned | the rule now applies only when SCL's fall is itself the event; 12 regression cases (6 failed before the fix) |
| 13 | A data-valid-time rule was applied where the I2C specification does not apply it, by the program and its oracle alike | a review against the specification text | VIOLATIONs where the specification sets no bound | the rule is not judged until it can be judged correctly; lesson: an oracle that shares the program's reading proves the mechanism, not the rule |
| 14 | Smaller model and assembler bugs (a counter named like a method shadowed it; YAML read `off` as false; CRC preset wrong for narrow CRCs; an ambiguity flag missing for two triggers a cycle apart; `trig 1` silently changing meaning) | reviews of the model | wrong firmware sizing | each fixed with a test |
| 15 | Two engines, first hardening: 60 MHz missed by 1.77 ns on 493 endpoints. The host port's command decode (which engine, is it halted, is its FIFO full) and the fan-out of every strobe to about 500 flip-flops happened in one cycle | the hardening run | no two-engine chip at 60 MHz | the host port's actions take effect one cycle after the packet (a pipeline register, about 37 flip-flops; spec diff 21); chip, lockstep and formal pass; the rerun moved the miss to row 16 |
| 16 | Two engines, second hardening: 0.19 ns missed on one endpoint: the host read address, through about 4.5 ns of fan-out buffers, into the readback / RUN-check mux of the Player's 96 words and the CRC update. Functionally a false path (the host reads only when no check runs), structurally real | the hardening rerun | no two-engine chip at 60 MHz | a registered readback address, one kept copy per 16-word bank (like the pc copies), the RUN check's schedule unchanged (spec diff 22); chip, lockstep and formal pass, with a new formal property that during the check the register is exactly the word read; hardening rerun in progress |
| 17 | H1 (slow edges): a slow SCL rise let a repeated START be read as data | the independent soundness review's strict fuzz on the slow-edge model (21 failures in 1,200 runs, with 0 on whole-cycle edges) | wrong-pair verdicts on a slow bus | flag W made symmetric: an edge of another channel close **before** the trigger counts too, with the trigger's own channel and direction window; core test `test_flag_w_before_side_with_the_triggers_own_window` |
| 18 | H2: W was marked only on the newest snapshot in the queue | the same review | a queued snapshot missing its W, misread | W marked on every open snapshot; `test_flag_w_marks_every_open_snapshot_not_only_the_newest` |
| 19 | H3: a flag JUDGE that was CAN'T TELL because of the taint fell through to the "certainly fine" branch | the same review | an unchecked SDA event treated as START / STOP | that check removed (the symmetric W makes it redundant) |
| 20 | H4: a slow SDA rise just before SCL's fall read as a STOP: so the docs' claim "lost coverage, never a wrong verdict" was false | the same review | a STOP that was not one; a false claim in the docs | resolved by option A (section 5): such an edge is defined as data with a negative hold; tHD;DAT judged with its guard band; the assembler refuses buses where the definition cannot hold. The reviewer's directed cases are still to be added to the slow-edge test file |
| 21 | H5: W counted only edges that were themselves trigger edges | the same review | close edges missed | W counts any edge of another channel; `test_flag_w_after_side_any_edge_of_another_channel`. Fixing H5 alone raised the old gate to 7.5% (443 of 5909), one reason for section 6 |
| 22 | The guard band assumed the pad's threshold lies between 30% and 70% of the supply; false for JEDEC LVCMOS33 (24-61%). The reviewer's counterexample: a legal tSU;DAT of 100 ns with SCL rising in 186 ns and a 24% threshold reads 4.95 cycles at the pads and could be judged VIOLATION | the independent guard-band review (3,729 wrong in 100,000 trials) | a false VIOLATION on a legal bus | the threshold range is declared in `.bus` (required) and the edge offsets are computed for it, linear or RC; `test_reviewer_counterexample_slow_scl_rise_low_threshold`; the waveform brute force keeps the old assumption as a negative control |
| 23 | With a declared threshold range strictly inside 30-70%, the computed pad offset range excluded zero; but a real edge may be faster than declared, and then its pad crossing comes as close to the specification crossing as you like | the independent-waveform brute force (section 4) | a guard too narrow: a possible false PASS or VIOLATION | the offset range always includes zero; the brute force (with thresholds of 45-55% among its cases) is the regression |
| 24 | An overclaim: the ISA v1 draft said the referee is sound "on any traffic". An interrupted or non-monotonic edge breaks that: an SCL runt that peaks at 60% of the supply (the pads see a high level of more than 5 cycles, the specification sees no edge), or a 27 ns bounce on a slow SDA fall, gives a PASS or VIOLATION about the wrong interval with no flag | the independent review of the stateless referee (review 5), with directed waveforms; its strict fuzz found nothing else in 4,200 waveforms | wrong verdicts on a ringing bus, and a false claim in the docs | the claim narrowed in the README, the datasheet, `docs/verdict.md` and this write-up to "within the declared bus envelope" (edges complete within the declared rise / fall time, levels reach VIL / VIH), section 3 (the ISA v1 spec draft and tables still to change); the short-level flag's threshold becomes programmable per channel, derived from the declared edge time (ISA v1 must-have, in progress) |
| 25 | A false VIOLATION on legal traffic: option A (row 20) also classified an SDA **rise** up to 300 ns before SCL's fall as data, so a STOP followed 200-250 ns later by an SCL fall was reported as a tHD;DAT VIOLATION. UM10204 sets no minimum time from a STOP to the next SCL fall | review 5 (`stopfall.py`) | a VIOLATION on legal traffic, against the first principle | an SDA **fall** in the window is a VIOLATION naming both rules (tHD;STA if a START, tHD;DAT if data: both are broken); an SDA **rise** is CAN'T TELL; option A is stated as the project's definition, motivated by the UM10204 note, not as the specification's own rule (in progress) |
| 26 | Blind after idle: the first START after RUN or after about 273 µs of idle bus, and tHIGH / tSU;DAT after SDA has been constant that long, were never judged (the reference edge is stale). Dense test traffic hid it; real buses are idle for milliseconds between transactions | the referee agent's own report, then review 5 (a legal 60-byte read of 0x00: 412 of 550 tSU;DAT unanswered) | the first START of almost every real transaction unjudged: exactly the bit-banged-driver bug the demo is about | candidate M: a tag bit per timestamp, "seen since RUN" or not; a seen but stale edge is certainly earlier than any fresh one, and its interval is at least about 273 µs minus u; sparse traffic (transactions 1-10 ms apart) in every referee test (ISA v1 must-have, in progress) |
| 27 | The SDA event's early release relied on two one-cycle relations no spec text stated (`waitp` sees an edge in the cycle the timestamp unit reports it; an edge reported in cycle k sets W before a branch in cycle k + 1) | review 5 (shifting either by one cycle gives 16-20 wrong-pair PASS verdicts) | an RTL faithful to the written spec could be unsound | both relations written into the spec and the ISA tables; a lockstep / formal property in RTL v1 (in progress) |
| 28 | Stale spec text that the RTL would be written from: the branch-on-W definition, the trigger text from v0.3, "W is final after BUS_W cycles" (false when the receiver window is longer), two different flop counts for W | review 5 | RTL built from an old definition | corrected before the v1.0 tag (in progress) |
| 29 | Measurement 2's headline (96.3%, target 95%) combined two widened definitions: a band extension (a START / STOP whose own SCL fall is in the 300 ns band) and a new "u" category (gross violations within the verdict rule's own uncertainty); with the strict 300 ns band alone it is 89.8%. The slow bus's misses were wrongly explained by edge spacing; most sit inside bound 12's own guard band | review 5's recomputation | a target met by redefining the measurement | all three numbers reported with their definitions: 89.8% (strict band), 94.4% (with the band extension), 96.3% (also excluding "u"); "u" kept only if it is exactly the edges within the proved ordering window, to be confirmed by measurement; the slow-bus cause corrected in section 6 with the next model commit (in progress) |
| 30 | A NAKed host command still returned a data byte read from image memory; a test passed only because earlier tests had written that memory | the first two-engine gate-level run (12 of 17 tests failed), then running each test alone | stale memory in host replies; a test suite that depended on its order | every NAK's data byte is the command byte (spec diff 23; a refused RUN replies 15 52); every NAK in every chip test is checked; every chip test also runs on its own (17 single runs pass); the new checks fail on the old RTL and on the old netlist; hardening run 44425384876 green |
| 31 | Gate-level X pessimism: one bit of the Referee's CRC register stayed X through reset (the synthesized reset path computes NOT(q AND NOT q): 1 in silicon, X in simulation), so the RUN check could not be shown at gate level | the same gate-level run, diagnosed in a local gate-level simulation (the first failing test, `no_pin_driven_before_first_run`, passes with flip-flops powering up 0 and with 1) | none in silicon, but a gate that could not show the RUN check | the CRC registers rewritten as one next-state expression each, the seed forced through its own term with the update nets kept as a synthesis cut point (cycle-identical: formal property 14, a mutant fails it); gate-level tests also run with flip-flops powering up at 0, at 1 and at random (two fixed seeds); the X-start run stays required; any future case needs an explicit, reviewed per-flop waiver, never a relaxed gate. Hardening run 44425384876: the X-start run and all four power-up runs pass |
| 32 | Planning mistakes in our own plan, caught by checking it against the code: the cost of a 128-word Referee was estimated at +25k µm² from 96 words; the RTL Referee has 64. Claude's first correction (about +50k) was also too low: a local synthesis (64 vs 80 words, every address still in range) gives about 1,290 µm² per word before hardening, about 1,580 after (each word has a write enable and two read ports), so +64 words is about +100k µm², 54.5% to about 63% of the core before the other v1 additions. Also, the rule-table front end was scheduled before RTL v1 | Claude's check of the plan against the code, then a synthesis measurement | an area overrun; hardware work squeezed against the then-planned 7 December freeze (now 14 December) | a local synthesis estimate of the full v1 decides 112 or 128 words; RTL v1 before the front end (software keeps improving after the freeze) |

## 9. Independent review

Every important piece of work gets an adversarial review by a second AI agent (Claude Opus) that
did not write it, reads it from the files, and is told to break the claim: to find a concrete input
for which a PASS or VIOLATION is wrong. The reviewers write their own fuzzers and checkers, and the
strictest of them maps every verdict's two timestamps back to the two waveform edges they came
from, so a verdict about the wrong pair of edges is caught even when its value looks right. Their
findings go into the ledger with a regression test.

| Review | Asked to break | Method | Found |
| --- | --- | --- | --- |
| Model review (spike, mid-November) | the first ISA v1 model and assembler | reading against the spec; directed tests | the smaller bugs of ledger row 14, each fixed with a test |
| Second review of the poison rule and the level-qualified trigger | "no wrong pair once poisoned" | 300 random and glitchy waveforms per program | no unpoisoned PASS or VIOLATION contradicting the oracle |
| Fourth review (17 November), receiver rule and snapshot queue | the gap rule at every queue depth, edges folded into a snapshot, the receiver rule, the test's own checker | fuzz at depths 1, 2 and 4: 3,600 random and about 3,500 glitchy waveforms | one hole (ledger row 12): 19 failures in about 3,500 runs; 0 after the fix |
| SPI timing referee (17 November) | the new referee and its oracle | 3,000 + 450 fuzz runs | sound; a tCSS point and comment fixes applied |
| Soundness review (18 November) | J ignoring the poison, the slow-edge windows (W), `rel` clearing the read mark, the short-level taint, the I2C referee read against UM10204 | its own strict fuzz, 1,200 runs at depths 1, 2 and 4, edges mapped back to the waveform, with a slow-edge skew model | **must fix**: 0 failures with whole-cycle edges but 21 on the slow-edge model; five holes H1-H5 (rows 17-21). J ignoring the poison, the `rel` clear and the taint: sound |
| Guard-band review (18 November) | the guard formulas from first principles (signs, clock-error term, rounding, the 30-70% assumption), the slow-edge test's fairness, the UART and CAN claims | its own 200,000-trial brute force; JEDEC thresholds | the maths sound (0 wrong), but **must fix**: the threshold assumption is false for LVCMOS33 (3,729 wrong in 100,000; row 22). Also: negative controls for the skew and clock-error terms were missing (added); the slow-edge test cannot catch guard errors under about a cycle (the unit brute force covers them); the CAN claims overreached on bit-timing tolerance and were to be reworded; several overclaims in the spec-diff draft |
| Fact checks (smaller model) | numbers and claims in documents, this write-up included | each number traced to its source | the first draft of this write-up: 12 findings, fixed |

**Owed:** an independent review of the current ISA v1 work (option A, the three measurements, the
96-word Referee, the stateless referee) before it is committed.

## 10. How AI was used

The RTL, firmware, tests, proofs and documents were written with Claude (Anthropic) through Claude
Code: a main session that designs, writes and reviews, and separate agent sessions for bounded tasks
(the FPGA build, the two-engine RTL, the spec closure in the model) and for the independent
reviews. The human author set the goals, made every decision recorded in the plan (each dated
decision above, such as option A, queue depth 2 and the three measurements, is the owner's), and
reviews the work in batches and at gates. A hardening run that fails is shown to the owner, with
the cause, before it is fixed.

What kept it honest:
- **Nothing is merged without a second reviewer** that did not write it, and the reviews are told
  to try to break the claim, with a concrete failing input for every hole (section 9).
- **Independent oracles and models**: the reference model is written from the spec, not from the
  RTL; device models are written from the protocol, not from the firmware; the referee oracle reads
  only the waveform; the guard brute force simulates the edges instead of reusing the formulas.
- **Negative controls everywhere**: every proof and every important test has a deliberately broken
  variant that must fail.

What the AI got wrong, and was caught: the code is AI-written, so every bug in the ledger is an AI
bug. Most were caught by the proofs and the independent oracles; rows 12, 13 and 17-22 were caught
by a second AI reviewer, row 23 by a brute force the AI wrote to check its own formulas. The AI
also overclaimed in prose: a first README said "any protocol" and implied proofs that did not
exist, and was toned down twice by review before publication; the ISA v1 notes said slow edges
cost "coverage, never a wrong verdict" until the soundness review showed H4; estimated numbers
were twice written down before they were measured, and replaced.

**Commit trailers.** 42 commits in the repository's history end with a "Co-Authored-By: Claude ..."
trailer (33 Claude Opus, 9 Claude Fable), and they stay there as a record of how the work was
produced. From 18 November, by the owner's choice, new commits carry no such trailer; this section
is where the AI's part is stated.

## 11. Limits (plainly)

- No silicon and, so far, no FPGA or real-bus run. The FPGA build exists but has not been through
  Vivado or onto a board.
- The two-engine chip is ISA v0.3: the ISA v1 work (section 5) is in the model, not yet in the RTL.
  The two-engine build itself is green (+1.34 ns at 60 MHz, 54.4%, gate level included; run
  44425384876).
- **Soundness holds within the declared bus envelope only**: every edge completes, monotonically,
  within the declared rise / fall time, and every level reaches VIL / VIH. A runt or a bounce outside
  it can be misjudged with no flag (row 24); no digital pad can see a level stopping at 60% of the
  supply.
- In the current referee model, the first START after RUN or after about 273 µs of idle bus is not
  judged, nor tHIGH / tSU;DAT after SDA has been constant that long (row 26): candidate M fixes it
  and is an ISA v1 must-have.
- **ISA v1 exists in the model only, not in the RTL**: queue depth 2, poison, J, W, the guard band,
  the taint, the larger Referee memory. The RTL work starts after ISA v1.0 is tagged (21 November).
- The proofs cover two kinds of interval at sub-cycle resolution; the rest rests on cycle-level
  proofs, simulation and fuzzing (section 3). The guard-band formulas are argued and brute-forced,
  not proved.
- The proofs assume every level on a judged pin lasts at least 4 clock cycles. In v1 the
  short-level taint flags levels of 5 cycles or less, so those give CAN'T TELL.
- **On a slow bus some rules cannot be judged, and that is physics, not a defect.** On the example
  bus (186 ns rises), the guard for tSU;DAT is larger than the bound itself: the assembler's
  CAN'T TELL band runs from about 4.3 to 26.1 cycles (about 72 to 435 ns) around a 100 ns bound, so
  every short setup is CAN'T TELL (436 of 436 violating intervals unjudged). On the same bus, 53.1%
  of violating tLOW and 23.3% of tHD;STA intervals went unjudged (first measurement). A future
  version of the assembler is to list such rules at the top of its report.
- The first gross START / STOP violation per transfer is reported 91.3% of the time on the fast bus
  and 43.6% on the slow bus: below the 95% target (section 6). With the stateless referee (not yet
  committed) the fast bus is at 89.8% with the strict 300 ns band, 94.4% with the band extension and
  96.3% with the "u" category (each defined in row 29); all three will be reported. tHD;STA misses
  the per-rule target on the fast bus (14.0% with the stateless referee, 11.6% before it; all in the
  300 ns band or the first START after RUN).
- Intervals longer than 273 microseconds are flagged and answered CAN'T TELL.
- A firmware referee follows SPI only up to about 2.1 MHz SCK.
- tVD;DAT (I2C data valid time) is not judged: at the SDA edge the program cannot know whether a
  STOP or repeated START follows.
- The level each I2C interval is measured between (our reading of UM10204's timing figure) is an
  input to the guard band, still to be confirmed rule by rule against the specification.

## 12. Status and plan

| Date | Done |
| --- | --- |
| 13 November | one engine hardened green at 8x4 and 60 MHz (+2.7 ns), with precheck and gate-level tests |
| 17 November | FPGA build for ZedBoard / PYNQ-Z2 merged (not yet run on a board); two engines in RTL (pull request open); ISA v1 spec closure in the model |
| 18 November | two-engine hardening: -1.77 ns, fixed to -0.19 ns (build 1 h 54 min, 55.2% of the core), then +1.80 ns (1 h 44 min, 54.5%) with the gate-level test red (rows 30, 31); the two reviews' holes fixed in the model; option A; queue depth 2; three measurements; the stateless I2C referee and its review (not yet committed); soundness claims narrowed to the declared bus envelope |
| 19 November | gate-level fixes; two-engine hardening green throughout (+1.34 ns, 54.4%, 1 h 43 min; gate level with X / 0 / 1 / random start); in the model: the STOP fix, candidate M (idle-safe judging), the per-channel short-level threshold, B dropped, 112- / 128-word Referee (not yet reviewed or committed) |
| 26 November | two engines merged into `integration` and `main`; RTL freeze moved once, to 14 December, with the scope frozen |

Planned, in order: the independent review of the ISA v1 model work, its fixes and the ISA v1.0 tag;
the RTL for ISA v1, must-haves first (the soundness set, candidate M, the per-channel short-level
threshold, the larger Referee with image format 4, 7-bit addresses), in lockstep with the v1 model,
with hardening runs around 5, 10 (feature complete) and 13 December (release candidate) and the
freeze on 14 December; a miss on 13 December ships the last green build's scope. After the freeze:
a held-out round in which every drawn protocol gets both a speaking program and a referee, the
rule-table front end, and the time-boxed firmware proof. Also planned:
one command that reproduces every claim in the README and this write-up and ends PASSED or FAILED,
and a mutation campaign on the verdict, snapshot, timestamp and poison logic.

| Target date | Milestone |
| --- | --- |
| 21 November (missed; the tag now follows the independent review and its fixes) | ISA v1.0 tagged; later changes are numbered diffs under a change gate |
| late November | two engines hardened green (done, 19 November); first FPGA run judging a real I2C bus (not yet) |
| 5, 10, 13 December | hardening runs: first v1 build; feature complete; release candidate (a miss ships the last green build's scope) |
| 14 December | RTL freeze (moved once from 7 December; hardware changes until then only with measured evidence, full proofs, gate-level tests and a green hardening build) |
| 20 December | proofs closed, or named as bounded |
| after the freeze | new referees written from datasheets as tables (section 7) |
| January 2027 | final hardening and submission |
