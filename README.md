# Referee

**Referee speaks serial protocols written as firmware (UART, SPI and I2C so far). When it
watches a bus, its own or anyone else's, it answers PASS, VIOLATION or CAN'T TELL, and a PASS
or a VIOLATION is meant to be proved right within a declared error budget.**

That proof exists today for two kinds of interval, under assumptions that are written down.
The table under "The error budget" says exactly which, and what is still open.

Referee is an entry for the Jane Street protocol-emulator ASIC competition: a chip for
[Tiny Tapeout](https://tinytapeout.com) on IHP SG13CMOS5L, with a 60 MHz clock and 24 digital
pins.

## What the chip is

- **A protocol emulator.** A small timing engine runs firmware: 24 instructions, and every one
  takes exactly one clock cycle except the three waits. UART, SPI and I2C are programs, not
  fixed logic: the UART transmitter is 18 words and the I2C controller 63, in a 64-word memory
  (`firmware/engine/`). A host loads programs over a UART port.
- **A bus referee.** Four judge channels, by default on input-only pins, timestamp every edge
  in hardware. Firmware picks two edges, and the chip compares the interval with a bound
  written in nanoseconds (for example the data setup time of Fast-mode I2C, at least 100 ns).
  The answer has three values:

  | Verdict | Meaning |
  | --- | --- |
  | PASS | the real interval met the bound |
  | VIOLATION | the real interval broke the bound |
  | CAN'T TELL (`INDETERMINATE` in the code) | the interval is too close to the bound for this chip to decide, or a timestamp was flagged |

  A chip that samples pins with its own clock cannot know an edge time better than about a
  clock cycle, and a sample taken right at an edge can go either way. Referee carries that
  uncertainty into the verdict instead of hiding it. [`docs/verdict.md`](docs/verdict.md)
  explains the rule and its proof. The proof (SymbiYosys) puts the external edges on a grid
  of 4 and of 8 steps per clock cycle, lets a sample taken next to an edge resolve either
  way, and shows that for the interval kinds listed below, under the assumptions listed
  below, a PASS or a VIOLATION is never wrong. With an uncertainty of one cycle instead of
  two, the proof fails, and the counterexample is kept in [`docs/results/`](docs/results/).

Timing rules are firmware too: a new rule is a few lines
([`firmware/engine/i2c_setup_referee.s`](firmware/engine/i2c_setup_referee.s) is 10 instructions).

## The error budget, as it stands today

| Item | Today |
| --- | --- |
| Measurement uncertainty | 2 clock cycles (33 ns at 60 MHz); fixed in hardware |
| Longest interval that can be judged | 273 µs (16-bit timestamps); longer ones are flagged and answered CAN'T TELL |
| Assumed, not checked by the chip | each level on a judged pin lasts at least 4 clock cycles; the judged pins have no skew or pad-delay difference between them; the chip runs at the clock the firmware was assembled for; a sample that goes metastable settles within one cycle |
| Proved at sub-cycle resolution | (1) a rising edge on one pin to a rising edge on another, read through the snapshot the way firmware reads it; (2) a high pulse (rise to fall) on one pin, measured straight from the timestamp unit, which firmware cannot do today |
| Not yet proved at sub-cycle resolution | every other interval firmware can judge: any interval with a falling edge, "the latest edge, whichever way", or two edges of one pin. The I2C setup-time referee in this repo uses "the latest edge", so its verdicts are not yet covered by the proof |
| Evidence for the unproved cases | falling-edge timestamps have the same proved latency as rising ones (`formal/edge_ts.sby`); the snapshot's capture of falling edges and of which edge came last is proved at whole-cycle resolution (`formal/engine.sby`, task `snap`); `tsget`'s choice between them is checked in lockstep simulation against the reference model; a chip-level test checks every verdict of a "latest edge" referee against the interval the test generated |
| Input threshold against the levels a spec measures at (I2C: 30% and 70% of the supply) | not accounted for yet. On a slow edge the chip's pin switches at a different moment than the spec's reference level |
| Timestamp width in the proofs | the proofs run with 6-bit timestamps; that the result carries over to the chip's 16 bits is not itself proved |

The full list of assumptions is in [`docs/isa-timing.md`](docs/isa-timing.md), section 11.
Closing the open rows is the main work planned below. For the threshold row, a guard band
that you declare per bus (from the pull-up and bus capacitance, or from a scope measurement)
will widen the CAN'T TELL band by exactly what the bus's edges can hide.

**Scope of the referee for now:** I2C Standard-mode and Fast-mode timing, and UART bit timing
(4800 baud and faster). Most of those rules involve a falling edge (an I2C START, a UART
start bit), so until the proof for falling edges is done they rest on the evidence row above,
not on the proof. Rules with intervals over 273 µs (a 1-Wire reset pulse, infrared remote
codes) wait for long-interval timestamps.

## Status (12 November 2026)

| Part | State |
| --- | --- |
| Engines | **One engine today.** Two are planned: a "Referee" (this engine) and a leaner "Player" that only emulates, so the chip can speak a protocol and judge a bus at the same time |
| Firmware that runs today (in simulation) | UART transmit and receive; I2C controller (400 kHz, clock stretching); SPI controller, modes 0 to 3; an I2C setup-time referee |
| Not there yet | I2C and SPI targets, parity and CRC (so no UART parity, CAN or USB yet), the full set of I2C timing rules, the per-bus guard band |
| Tests | 14 chip-level tests through the pins (cocotb), including an independent oracle that checks every verdict against the interval it generated; 24 unit tests, including the engine RTL run in lockstep with a reference model (the model's tables and the RTL's are generated from the same ISA file). One of the 24 covers `seq`, an earlier sequencer that is no longer in the chip |
| Proofs | verdict soundness, edge timestamps, engine properties, UART; each has negative controls that must fail |
| Hardening | One engine met 60 MHz at the slow corner on 6x4 tiles (+1.4 ns, 44% utilization) in October, on a probe branch (`sta-rtl`, commit `801e728` plus the flow settings now in `src/config.json`); the RTL has changed since. The competition's final size is 8x4: the switch and its first hardening run are in progress, and until they land `info.yaml` here says 6x4 |
| Real hardware | none yet: no chip, no FPGA run, no bench measurement |

## Try it

Simulation needs Python 3.11 or newer, `make` and
[Icarus Verilog](https://github.com/steveicarus/iverilog); the proofs need
[OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) (SymbiYosys).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r test/requirements.txt

cd test && make                                     # 14 chip-level tests, a few minutes
pytest tools engine seq uart edge_ts -q             # unit and lockstep tests
cd ../formal && sby -f engine.sby                   # engine properties, under a minute
sby -j 4 -f verdict.sby                             # verdict soundness, about 30 minutes
cd ..
```

`sby` reports the tasks named `u1`, `glitch`, `comb_u1`, `comb_onesided`, `snap_early` and
`tie_timeout` as FAIL. They are the negative controls: each removes one thing the proof needs,
and must fail.

Assemble a program and look at its image (from the repo root):

```bash
python3 tools/refasm.py --clock-hz 60000000 --image firmware/engine/i2c_setup_referee.s
```

Once a chip exists (there is none yet), the host tool talks to its UART port from a PC. It
needs `pip install pyserial`, the demo board's project clock set to 60 MHz, and for this
example an I2C bus's SDA on `ui_in[4]` and SCL on `ui_in[5]`.
[`docs/info.md`](docs/info.md) has the pins and every command.

```bash
python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/i2c_setup_referee.s --run
python3 tools/referee_host.py --port /dev/ttyUSB0 stats 0
```

## Where things are

| Path | What |
| --- | --- |
| [`docs/verdict.md`](docs/verdict.md) | the verdict rule, what is proved, why the uncertainty is 2 cycles |
| [`docs/isa-timing.md`](docs/isa-timing.md) | the instruction set, the timing contract, the assumption list |
| [`docs/info.md`](docs/info.md) | datasheet: pins, host commands, how to use the chip |
| [`docs/edge_ts.md`](docs/edge_ts.md), [`docs/uart.md`](docs/uart.md) | the edge-timestamp unit and the UART, with their proofs |
| `isa.yaml` | the one description of the ISA; the RTL constants and the tables used by the assembler and the reference model are generated from it |
| `src/` | RTL (Verilog) |
| `firmware/engine/` | protocol and referee programs |
| `tools/` | assembler, reference model, host tool |
| `test/`, `formal/` | cocotb tests and SymbiYosys proofs |

## Plan

1. **Now:** harden the current design at 8x4.
2. **November:** size the next ISA by writing the firmware first, on the reference model:
   I2C and SPI targets, UART with parity, CAN, and the full I2C Fast-mode rule set. An
   instruction-set addition that fewer than two real programs use is dropped.
3. **By early December:** two engines, parity and CRC, the per-bus guard band, the proof for
   falling edges, then an RTL freeze.
4. **After the freeze:** protocols and timing rules written against the frozen chip, to show
   what it can do that it was not designed for; a timing report on a real third-party I2C bus.
5. **January 2027:** final hardening and submission.

## License

Apache-2.0, see [LICENSE](LICENSE).
