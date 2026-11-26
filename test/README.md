# Sample testbench for a Tiny Tapeout project

This is a sample testbench for a Tiny Tapeout project. It uses [cocotb](https://docs.cocotb.org/en/stable/) to drive the DUT and check the outputs.
See below to get started or for more information, check the [website](https://tinytapeout.com/hdl/testing/).

## Setting up

1. Edit [Makefile](Makefile) and modify `PROJECT_SOURCES` to point to your Verilog files.
2. Edit [tb.v](tb.v) and replace `tt_um_example` with your module name.

## How to run

To run the RTL simulation:

```sh
make -B
```

To run one test on its own (a regular expression on the test name; end it with `$$`,
which make turns into `$`, so `i2c_controller` does not also pick
`i2c_controller_clock_stretching`; `|` does not work because the Makefile passes the value
unquoted):

```sh
make COCOTB_TEST_FILTER='rejects_bad_commands$$'
```

**Every chip test must pass when run on its own**, from a fresh reset, as well as in the full
suite. A test that passes only after another one has run is a bug in the test or in the
chip: `rejects_bad_commands` passed in the suite for weeks while, run alone, a refused read
returned an image word nothing had written (an X byte on the host TX; docs/isa-timing.md 0b,
diff 23). Before a checkpoint, run all of them one at a time.

To run gatelevel simulation, first harden your project and copy `../runs/wokwi/results/final/verilog/gl/{your_module_name}.v` to `gate_level_netlist.v`
(in CI the gl_test job copies it from the gds job's `tt_submission` artifact; never commit it).

Then run:

```sh
make -B GATES=yes PDK_ROOT=<the PDK root>
```

### Gate-level runs: flip-flop power-up values

`GL_POWERUP` chooses what every flip-flop holds at time 0:

| `GL_POWERUP` | flip-flop model | in CI (gds workflow) |
| --- | --- | --- |
| `x` (default) | the PDK's own cell models: every flop starts at X | `gl_test`, **required** |
| `0` | every flop powers up at 0 | `gl_test_powerup`, must pass |
| `1` | every flop powers up at 1 | `gl_test_powerup`, must pass |
| `rand` | every flop its own random bit, from `GL_SEED=<n>` (default 1; printed in the log as `GL_SEED=...`) | `gl_test_powerup` with fixed seeds, must pass |

```sh
make -B GATES=yes PDK_ROOT=<pdk> GL_POWERUP=rand GL_SEED=1234 COCOTB_TEST_FILTER='reset_state$$'
```

For `0`, `1` and `rand`, the only flip-flop cell the netlist uses, `sg13cmos5l_dfrbpq_1`, is
replaced by the behavioural model in `gl_powerup_dfrbpq.v` (same function: Q takes D at the
rising clock, RESET_B low clears it): the Makefile removes that one module from a copy of the
PDK's cell file in the build directory and fails if it does not find exactly one. Every other
cell model is the PDK's. Each mode has its own build directory (`sim_build/gl`,
`sim_build/gl_pu0`, ...). The model is test-only and never in `info.yaml`.

Why both kinds of run. Silicon powers up with every flop at some 0 or 1, and our reset sets
every register that needs it. A simulation that starts at X is the strict check that the reset
really reaches every flop, but it is pessimistic: when synthesis folds a register's reset into
logic that also reads the register itself, X-simulation can't see that the result is the same
for q = 0 and q = 1. That happened in hardening run 44309280727: `u_sys.crc_got_e0[14]` had
D = o21ai(q, n1, o21ai(n2, n3, q)), which during reset is NOT(q AND NOT q), 1 in silicon and X
in simulation, and 12 of 17 tests failed at gate level although the chip would have worked
(the first failing test, `no_pin_driven_before_first_run`, passed on the same netlist with all
flops starting at 0 and at 1). The 0 / 1 / random runs check the chip from defined power-up
states; the X run stays the required gate.

### Waiver policy for gate-level X-pessimism

If a flop stays X through reset in the X run while the 0 / 1 / random runs pass, the fix is,
in this order: (1) rewrite the register in the RTL so that its reset or clear does not go
through logic that reads the register itself (as `crc_got_g` / `crc_got_e` in
`src/engine.v`), and harden again; (2) only if the X persists, an **explicit per-flop
waiver**: the flop's netlist name, its D cone showing the q AND NOT q structure, the proof
that its value after reset is the same for q = 0 and q = 1, and a review by someone other
than the author, recorded with the hardening run's number. In the X run a waiver deposits
that proved value into that one flop at the end of reset, nothing else; every other flop
still starts at X and every test runs unchanged. A waiver names one flop of one netlist and
lapses with the next hardening run (names change). No waiver exists yet, so the deposit
mechanism is not built; it comes with the first one. **The gate is never relaxed**: no test
is skipped, weakened or marked expected-to-fail at gate level to get past an X, and the X run
is never dropped or made optional.

If you wish to save the waveform in VCD format instead of FST format, edit tb.v to use `$dumpfile("tb.vcd");` and then run:

```sh
make -B FST=
```

This will generate `tb.vcd` instead of `tb.fst`.

## How to view the waveform file

Using GTKWave

```sh
gtkwave tb.fst tb.gtkw
```

Using Surfer

```sh
surfer tb.fst
```
