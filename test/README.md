# Referee tests

The UART console is checked in layers. Each layer catches things the one
before cannot.

| Layer | What | Where | Run time |
|-------|------|-------|----------|
| Unit | UART receiver and transmitter alone: exact bit timing, baud tolerance sweep, glitches, framing errors, break, plus a second opinion from [cocotbext-uart](https://github.com/alexforencich/cocotbext-uart) | `unit/` | ~5 s |
| Chip | Whole design through its pins only: transcripts, GPIO, cancel keys, overrun, framing errors, baud change, host baud error, random traffic against a Python model | `test.py`, `transcripts/` | ~1 min |
| Gate level | The chip tests again on the netlist after place and route (CI does this) | `make GATES=yes` | slower |
| Formal | Transmitter looped into receiver: any data, any write timing, divisors 4-6, checked to 100 cycles; plus a cover trace of 3 back-to-back bytes | `../formal/` | ~2 min (cover ~10 min) |
| End to end | Real host software over a virtual serial port to the simulated chip | `make pty` + `../host/referee.py` | ~15 s |

The transcripts in `transcripts/` are shared: the cocotb tests replay them in
simulation, and `host/referee.py` replays them over a serial port against an
FPGA or the real chip.

## Setting up

You need Icarus Verilog and Python 3.10+. For formal checks you also need
yosys and SymbiYosys. [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build)
has all of them:

```sh
# OSS CAD Suite unpacked in ~/tools/oss-cad-suite (or set OSS_CAD_SUITE)
python3 -m venv .venv && .venv/bin/pip install -r test/requirements.txt
source test/env.sh      # puts .venv, the vvp shim and OSS CAD Suite on PATH
```

`test/shims/` works around OSS CAD Suite's `vvp` wrapper, which forces the
suite's own Python and breaks cocotb installed in `.venv`. With Icarus from
your OS package manager you don't need it.

## How to run

```sh
cd test
pytest unit -v                  # unit tests
make -B                         # chip tests (RTL)
make -B GATES=yes               # chip tests on the gate-level netlist (see below)
cd ../formal && sby -f uart_loopback.sby bmc   # or: cover
```

Gate level: harden the project, then copy the netlist from
`../runs/wokwi/final/pnl/tt_um_shreyansh_referee.pnl.v` (path depends on your
flow) to `gate_level_netlist.v`.

### Talking to the simulated chip

```sh
cd test && make pty             # leave it running; it prints the device
# in another terminal:
python host/referee.py --port test/sim.tty --timeout 20 run test/transcripts/*.txt
picocom test/sim.tty            # or type commands yourself
```

The simulation is slower than real time (about a second per command), and the
bridge always talks at the reset baud rate.

## Speed notes

- The 40 MHz clock is generated in Verilog (`tb.v`, `unit/hdl/*_tb.v`). A
  cocotb-driven clock is about 50x slower with Icarus.
- Tests wait on timers and signal events rather than single clock edges.
- Long tests switch the chip to 2.5 Mbaud first (`set_divisor(FAST_DIV)`).

## Waveforms

```sh
gtkwave tb.fst tb.gtkw
surfer tb.fst
```

`make NO_DUMP=1` skips the waveform dump.
