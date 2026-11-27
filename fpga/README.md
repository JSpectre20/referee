# Referee on a Zynq-7020 board (ZedBoard, PYNQ-Z2)

The current Referee chip (`src/`, unchanged, the RTL that hardens for Tiny Tapeout) runs at
**60 MHz** in the programmable logic of a Zynq-7020 and judges a **real I2C bus** driven by the
Zynq's own I2C controllers (Xilinx's Cadence I2C IP, a third-party implementation) on physical
Pmod pins, where a scope, a logic analyzer or an external EEPROM can be attached. The PC talks
to the chip with `tools/referee_host.py`, unchanged, through the board's USB-UART.

**ZedBoard is the primary target**; PYNQ-Z2 is built by the same script.

An **ILA** (Xilinx Integrated Logic Analyzer) in the same bitstream samples the judged pads at
**240 MHz** (4 x the chip clock) from the board's own oscillator, so every board run also records
an independent, finer reference of the bus; `tools/ila_check.py` checks each chip verdict
against it ("Capture with the ILA and check every verdict" below).

```
 PC ── USB-UART ── PS UART (MIO) ─┐  bare-metal app (fpga/sw/main.c):    ┌─ PS UART (EMIO) ── chip ui_in[3] / uo_out[4]
                                  └─ bridge, 115200 both ways, verbatim ─┘
 PS I2C0 (controller, EMIO) ─┐                                   ┌─ chip ui_in[5] = SCL (judge channel 1)
 PS I2C1 (target 0x50, EMIO) ┴─ open-drain nets ── Pmod JA pins ─┴─ chip ui_in[4] = SDA (judge channel 0)
 FCLK_CLK0 100 MHz ── MMCM ── 60.000 MHz ── chip clk          chip uio[0..7] ── Pmod JB
 board oscillator (100 / 125 MHz) ── its own MMCM ── 240 MHz ── ILA: pads, chip pins, sample counter
```

| File | What |
| --- | --- |
| `build.tcl` | Vivado batch script: project, block design (PS7 + Clocking Wizard), the ILA's two IPs, synthesis, implementation, bitstream, XSA, probes file |
| `zedboard.xdc`, `pynq-z2.xdc` | board pins, each copied from the board's official master XDC or board file (source URL in the header) |
| `ila.xdc` | the ILA clock is asynchronous to everything else (added only when the ILA is built) |
| `rtl/fpga_top.v` | the wrapper and Vivado top: open-drain I2C nets, IOBUFs, reset, LEDs, the chip, the ILA capture logic |
| `rtl/ila_none.v` | empty stand-ins for the ILA IPs: the build without ILA, the lint, the Yosys estimate |
| `ila_capture.tcl` | Vivado hardware manager, batch: arm the ILA, wait, export `ila.csv` / `.ila` / `.vcd` |
| `runs/` | recorded board runs, re-checkable without the board (`runs/README.md`) |
| `sw/main.c` | bare-metal app: UART bridge, I2C traffic generator, I2C target (24C02-like), ILA mark |
| `sw/build_app.tcl`, `sw/run_app.tcl` | command-line app build (classic Vitis) and JTAG run (xsct) |
| `sim/` | local checks without Vivado: lint, Icarus end-to-end simulation with an ILA model and the ILA check, Yosys estimate (`sim/run.sh`) |

## What goes where

| | ZedBoard (xc7z020clg484-1) | PYNQ-Z2 (xc7z020clg400-1) |
| --- | --- | --- |
| USB-UART to the PC | UART1, MIO 48/49, micro-USB **J14 "UART"** | UART0, MIO 14/15, the micro-USB **PROG/UART** port |
| UART to the chip (EMIO) | UART0 | UART1 |
| I2C **SCL** | Pmod **JA pin 3** (Y10) | Pmod **JA pin 3** (Y16) |
| I2C **SDA** | Pmod **JA pin 4** (AA9) | Pmod **JA pin 4** (Y17) |
| chip `uio[0..3]` | Pmod **JB pins 1-4** (W12 W11 V10 W8) | Pmod **JB pins 1-4** (W14 Y14 T11 T10) |
| chip `uio[4..7]` | Pmod **JB pins 7-10** (V12 W10 V9 V8) | Pmod **JB pins 7-10** (V16 W16 V12 W13) |
| GND / 3.3 V | Pmod pins 5, 11 / 6, 12 | Pmod pins 5, 11 / 6, 12 |
| traffic on / off | SW0 | SW0 |
| 100 kHz / 400 kHz | SW1 | SW1 |
| reset the chip | BTNC | BTN0 |
| clear the error LED | BTND | BTN1 |
| one write + read-back cycle | BTNL | BTN2 |

SCL and SDA sit where a Digilent type-6 (I2C) Pmod expects them (pins 3 and 4). On PYNQ-Z2,
Pmod JA shares its pins with the Raspberry Pi header: leave that header empty.

LEDs (ZedBoard LD0..LD7; PYNQ-Z2 has LD0..LD3 plus LD4 red = error and LD4 green = ok):

| LED | Meaning |
| --- | --- |
| LD0 | heartbeat, about 1 Hz, from the 60 MHz chip clock; off while the chip is in reset (MMCM not locked, PS not initialised, or the reset button held) |
| LD1 | traffic on |
| LD2 | I2C activity (SCL low in the last ~17 ms) |
| LD3 | the chip's UART TX was active in the last ~17 ms (the chip is answering) |
| LD4 | error: an I2C transfer was not ACKed, timed out, or read back wrong (sticky; BTND / BTN1 clears it) |
| LD5 | ok: the last write / read-back cycle matched |
| LD6 | 400 kHz selected (ZedBoard only) |
| LD7 | chip `uo_out[0]`, engine pin 8 (ZedBoard only) |

### Pull-ups

The FPGA's internal pull-ups are on (XDC `PULLUP`), so the bus idles high with nothing attached,
but they are weak (tens of kohm) and give slow rising edges. **Fit external 2.2-4.7 kohm pull-ups
from SCL and SDA to 3.3 V (Pmod pin 6 or 12)** for realistic edges, especially at 400 kHz. A
Digilent I2C Pmod with its own pull-ups also works. A scope or logic analyzer goes on JA pins 3
and 4 with ground on pin 5.

### The I2C bus inside the FPGA

SCL and SDA are each one open-drain net on one pin: either PS controller pulling a line low
pulls the pad low (an IOBUF that only ever drives 0). Both PS controllers and the chip's judge
channels read the **pad** (the IOBUF's input), so the referee sees the real pin with its real
edges, and an external device on the Pmod is part of the same bus. The judge channels are
registered once in the I/O block before the chip, so SDA and SCL are sampled with matched input
delays; that extra cycle delays both equally and changes no interval.

### The bus traffic

With SW0 on, the app repeats, every ~30 ms: write `[pointer, 4 bytes]` to 0x50; wait 10 ms (an
EEPROM's write cycle); random read (write the pointer, repeated START, read 4 bytes); compare.
The target is PS I2C1 behaving like a 24C02: 256 bytes, pointer first, auto-increment.
One cycle is 120 SCL rising edges (write 6 bytes x 9 + STOP, pointer 2 x 9 + repeated START,
read 5 x 9 + STOP), so with SW0 off one press of BTNL / BTN2 adds exactly 120 verdicts.
To use a real 24C02 (address 0x50) on the Pmod instead of the PS target, set `TARGET_ADDR` to
0x51 in `sw/main.c` (`TRAFFIC_ADDR` follows 0x50).

## Build and run

You need: **Vivado and Vitis 2020.2 to 2024.2** (the script uses `write_hw_platform`, the
`.gen` directory layout and `processing_system7` 5.5 / `clk_wiz` 6.x; it has not been run on
any version yet, see "What is not verified"). **No board files are needed**: every PS7 setting
is in `build.tcl` with its source. Python 3 with `pip install pyserial` on the PC.

### 1. Bitstream and XSA (Vivado, about 10-20 minutes)

From the repository root:

```bash
vivado -mode batch -source fpga/build.tcl -tclargs zedboard      # or pynq-z2
```

Output in `fpga/build/zedboard/`: `referee_zedboard.xsa` (bitstream included),
`referee_zedboard.bit`, `referee_zedboard.ltx` (the ILA's probes), `ila_info.txt` (the ILA's
clock, depth, probes and the routed pad-to-sampling-flop delays), `timing_summary.rpt`,
`utilization.rpt`, `clocks.rpt`. The log prints the slack of each clock domain and ends with
`INFO: timing met at 60 MHz (chip) and 240 MHz (ILA).` or with a `CRITICAL WARNING: TIMING NOT
MET`: if it names the ILA's domain, see "If the ILA does not close"; otherwise see "If 60 MHz
does not close" below before trusting any verdict.

Optional arguments after the board: jobs, chip MHz, ILA MHz (0 = no ILA), ILA depth, e.g.
`-tclargs zedboard 4 60 200` (ILA at 200 MHz) or `-tclargs zedboard 4 60 0` (no ILA: the
design as before, `rtl/ila_none.v` in place of the IPs).

The script stops with an error if a PS7 setting did not take, if either MMCM is not exactly
on frequency (60.000 MHz for the chip, 240.000 MHz for the ILA), if an ILA setting did not take,
or if the block design's port names differ from what `rtl/fpga_top.v` expects.

### 2. The app (Vitis)

**Vitis 2023.2 and later (Unified IDE):**

1. File > New Component > Platform. Name `referee_pf`; hardware design: the XSA from step 1;
   operating system `standalone`, processor `ps7_cortexa9_0`. Finish, then Build the platform.
2. File > New Component > Application. Name `referee_app`; platform `referee_pf`; domain
   `standalone_ps7_cortexa9_0`. Finish.
3. In `referee_app`, Sources > right-click `src` > Import > Files: `fpga/sw/main.c`. Build.

**Vitis 2020.2 to 2023.1 (classic):** either the same in the GUI (new Application Project from
the XSA, "Empty Application (C)", import `main.c`), or from a shell:

```bash
xsct fpga/sw/build_app.tcl zedboard
# -> fpga/build/zedboard/ws/referee_app/Debug/referee_app.elf
```

The app uses only the standalone drivers XUartPs, XIicPs and XGpioPs and works with both the
classic BSP and the system-device-tree BSP of 2023.2 and later.

### 3. Program the board and start the app

1. **Boot mode JTAG.** ZedBoard: boot-mode jumpers JP7..JP11 (MIO 6..2) all on the GND side.
   PYNQ-Z2: jumper JP1 on JTAG. ZedBoard: leave VADJ (J18) as it is; the buttons and switches
   are constrained for 2.5 V (see the comment in `zedboard.xdc`).
2. Connect JTAG (ZedBoard: micro-USB J17 "PROG"; PYNQ-Z2: the PROG/UART port) and the USB-UART
   (ZedBoard: J14), power on.
3. **Vitis Unified:** select `referee_app`, Run (the default launch programs the bitstream, runs
   `ps7_init` and `ps7_post_config`, downloads and starts the app).
   **Or from a shell (any Vitis with xsct):**
   ```bash
   xsct fpga/sw/run_app.tcl zedboard fpga/build/zedboard/ws/referee_app/Debug/referee_app.elf
   ```
   `ps7_post_config` matters: it enables the PS-PL level shifters; without it nothing on EMIO
   (I2C, UART, GPIO) and no FCLK reset reaches the PL.
4. LD0 starts blinking (the chip runs at 60 MHz and is out of reset). The USB-UART shows, once:
   `Referee bridge ready (115200 8N1). SW0 traffic, SW1 400 kHz.`
   Close any terminal on that port before using the host tool.

### 4. From the PC

Find the port: ZedBoard's J14 (Cypress USB-UART) is one COM port / `/dev/ttyACM*`; PYNQ-Z2's
FTDI chip shows two interfaces, the UART is the second (higher COM number, `/dev/ttyUSB1`).

```bash
python3 tools/referee_host.py --port COM5 id
# Referee
python3 tools/referee_host.py --port COM5 load firmware/engine/i2c_setup_referee.s --run
# bound 0: 100ns = 6.0000 cycles -> n_lo 96/16 (exact)
# loaded, running
```

Turn **SW0 on** (traffic; SW1 picks 400 kHz), wait a second, turn it off, then:

```bash
python3 tools/referee_host.py --port COM5 drain
# (nothing: the firmware sends only VIOLATION / CAN'T TELL verdicts, and a healthy bus has none)
python3 tools/referee_host.py --port COM5 stats 0
# {'pass': <about 4,000 per second of traffic>, 'viol': 0, 'indet': 0, 'min': ..., 'max': ...}
```

`pass` counts every SCL rising edge since RUN (one tSU;DAT verdict each); `min` and `max` are
the shortest and longest measured data setup time in chip cycles (16.7 ns). With SW0 off,
reload (`load ... --run`), press BTNL / BTN2 once, and `stats 0` must say `'pass': 120`
(plus `viol` / `indet` if any). The counters are 16-bit and **saturate at 65535**: continuous
traffic (about 4,000 SCL rises a second at either speed) gets there in about 15 s; reload
to clear them.

A clean Cadence controller is not expected to break tSU;DAT (it changes SDA microseconds before
SCL rises), so `viol` stays 0. To see the chip catch a slow edge, make SDA's rising edges slow
(a large pull-up, say 47 kohm, and a capacitor of a few nF from SDA to ground, 400 kHz) and
watch `viol` / `indet` and `drain`. This is a suggestion; it has not been tried.

Do not use `--fast`: it switches the chip to 3 Mbaud, which the 115200 bridge cannot follow
(press the reset button to get back to 115200).

### 5. Capture with the ILA and check every verdict

The ILA records the judged pads (and the chip's view of them, the host UART, the engine pins)
while the chip judges; `tools/ila_check.py` rebuilds every judged interval from the ILA's
samples and checks each chip verdict against it. A run is recorded in a folder under
`fpga/runs/` (layout and the `run.json` keys: `runs/README.md`).

```bash
P=COM5                                   # or /dev/ttyACM0
R=fpga/runs/2026-12-01_zedboard_400k-btnl
mkdir -p $R && cp fpga/build/zedboard/ila_info.txt $R/
# 1. SW0 off (no traffic), SW1 on (400 kHz). Load and run the referee. The evidence variant
#    pushes every non-PASS verdict with its exact m (see below); note the "image crc16" line.
#    Loading and RUN do NOT clear the stats, LOST or the verdict FIFO, and VERDICT_OVF stays set
#    until a status read (docs/isa-timing.md 0b, diff 2): empty and clear them before arming.
python3 tools/referee_host.py --port $P load firmware/engine/i2c_setup_referee_raw.s --run
python3 tools/referee_host.py --port $P drain          # repeat until it prints nothing
python3 tools/referee_host.py --port $P status         # reading it clears the sticky bits
python3 tools/referee_host.py --port $P stats-clear 0
#    (or: BTNC / BTN0 resets the chip, then load ... --run: all of it starts from zero)
# 2. Arm the ILA (second terminal; Vivado's hw_server talks to the same JTAG cable as xsct).
vivado -mode batch -source fpga/ila_capture.tcl -tclargs $R -trigger mark
#    ... wait for "ARMED", then press BTNL (BTN2 on PYNQ-Z2) ONCE: one write / read-back
#    cycle, 120 SCL rises. The ILA stores every edge; with the bus idle again its 1 ms
#    heartbeat fills the rest of the buffer, so the capture finishes about 9 s later and
#    writes $R/ila.csv, ila.ila, ila.vcd and capture.txt.
# 3. The chip's side, in this order (repeat drain until it prints nothing):
python3 tools/referee_host.py --port $P drain   >> $R/verdicts.txt
python3 tools/referee_host.py --port $P stats 0 >> $R/verdicts.txt
python3 tools/referee_host.py --port $P status  >> $R/verdicts.txt
# 4. Describe the run: copy the run.json example from fpga/runs/README.md into $R/run.json
#    and fill in the date, commit, firmware and its image crc16 (step 1), ila_clock_hz,
#    skew_ns, build_args and bitstream_sha256 from ila_info.txt (the checker compares them),
#    the pull-ups and the rise times you measured on JA3 / JA4.
# 5. Check, keep the result, commit the folder:
python3 tools/ila_check.py $R | tee $R/check.txt          # ends PASSED or FAILED
git add $R
```

`-trigger mark` starts at the PS's mark (EMIO GPIO 15, set by `sw/main.c` when a traffic cycle
starts), so the window opens just before the START. The default `-trigger scl_fall` works for
any traffic, including the chip's own Player. `-mode all` stores every sample (a contiguous
34 us at 8192 x 240 MHz, a few bytes of a ~300 us burst): for looking at edges, not for checking
a run, whose judged edges must all be in the trace. All options: the header of `ila_capture.tcl`.
If the capture times out (no trigger, buffer not full) the script exports nothing and says why
in `capture.txt`.
In the Vivado GUI the same capture is: Open Hardware Manager, Open Target, set the probes file
`referee_zedboard.ltx`, Refresh; trigger `ila_mark` = R (or `ila_scl` = F), capture mode BASIC
with `ila_chg` = 1, Run; File > Export > Export ILA Data (CSV).

Why the raw variant (`firmware/engine/i2c_setup_referee_raw.s`, `judge 0, raw, nonpass`): the
shipped `i2c_setup_referee.s` pushes header-only verdicts, whose m saturates at 3 cycles, so a
VIOLATION cannot be pinned to its rise and the check mostly tests PASS soundness on a clean
bus; with the exact m it can. Each raw verdict takes 2 FIFO slots: at most 4 non-PASS verdicts
per burst before the FIFO overflows (which fails the check as an incomplete stream).

What PASSED means: every judged edge was checked, at least one verdict was confirmed, none was
contradicted, none was left unresolved (unless `max_unresolved` in run.json allows a few), the
clocks agree with the build (`ila_info.txt`) and with the trace itself. What it cannot see:
`runs/README.md`, "What the check can and cannot say". A healthy bus gives 120 PASS verdicts,
each confirmed by the ILA to about +-6.7 ns.

### The ILA

| | |
| --- | --- |
| Clock | **240 MHz** (T = 4.17 ns, 4 x the chip clock) from the board's PL oscillator (ZedBoard 100 MHz on Y9, PYNQ-Z2 125 MHz on H16) through its **own MMCM** (`clk_ila_wiz`, VCO 1200 MHz): nothing shared with the chip's clock path (PS crystal, FCLK_CLK0, the chip's MMCM); runs from configuration on, so the debug hub always has a clock |
| Depth | **8192** samples (build argument, 1024 .. 65536; 131072 x 72 bits would need more BRAM than the 7020 has) |
| Storage | capture control on: with `ila_chg == 1` (the default of `ila_capture.tcl`) only samples where a probe changed are stored, plus a heartbeat every 2^18 samples (1.09 ms); 8192 samples hold about 11 BTNL cycles (about 700 stored samples each) |
| Probes | `ila_scl`, `ila_sda` (the raw pads, IOBUF outputs, before the chip's IOB register and synchronisers), `ila_scl_chip`, `ila_sda_chip` (the chip's judge-channel inputs), `ila_uart_rx` (host to chip), `ila_uo_out[7:0]` (bit 4 = chip UART TX, the others engine pins 8..14), `ila_uio_in[7:0]`, `ila_uio_oe[7:0]` (engine pins 0..7 and their enables), `ila_rst_n`, `ila_mark`, `ila_chg`, `ila_ts[39:0]` (sample counter, 76 minutes before it wraps; data only) |
| Sampling | every signal passes two ASYNC_REG flops in the ILA clock; the first is the sampling point. `ila.xdc` bounds the SCL and SDA pad-to-sampling-flop paths to 3 ns each (`set_max_delay -datapath_only`; the board XDCs no longer false-path them); `build.tcl` writes their routed min / max delays and the skew to `ila_info.txt` and stops if it cannot read them or the skew is over 3 ns. The checker requires run.json's `skew_ns` >= that; the two flops' clock-arrival difference and the MMCM jitter are in `aperture_ns` (0.25 ns per edge) |
| Trigger | `ila_capture.tcl -trigger`: `scl_fall` (default), `sda_fall`, `mark` (PS GPIO 15), `uart_rx` (a host byte), `uart_tx` (a chip reply), `now` |
| Uncertainty | per interval +-(T + 2 x aperture + skew), about +-6.7 ns at 240 MHz with 2 ns skew, against the chip's own +-33 ns (2 cycles) |
| Self-check | the chip-side probes change only on clk60 edges, so their gaps in the trace must be whole numbers of chip cycles at the stated ILA / chip clock ratio (+-1 sample): `ila_check.py` fails a run whose stated clocks do not fit its own trace |

**Expected resources** (estimates, not from a Vivado run): ILA data width 72 bits x 8192 =
**18 RAMB36** (13% of the 7020's 140; 36 at depth 16384), about **1,500-2,500 LUTs** and
**2,500-3,500 flip-flops** for the ILA core (2 comparators per trigger probe, capture control)
and the debug hub, plus the capture logic in `fpga_top`: 174 flip-flops (2 x 31 synchroniser
flops, 72 probe registers, the 40-bit counter) and some 30 LUTs plus 10 CARRY4 (counted by
hand; not synthesised). The chip itself is about 2,300
LUTs: the whole design stays under 10% of the LUTs.

### If the ILA does not close

The ILA domain is reported separately (`clk_ila` setup / hold slack in the log). A miss there
does not affect the chip's verdicts, only the trustworthiness of the capture. Rebuild with the
ILA at 200 MHz (5 ns samples, +-7.5 ns per interval; still 3.3 x finer than the chip):

```bash
vivado -mode batch -source fpga/build.tcl -tclargs zedboard 4 60 200
```

and set `"ila_clock_hz": 200000000` in each run's `run.json` (`ila_info.txt` says it). If it still
misses, halve the depth (`... 60 200 4096`); `-tclargs zedboard 4 60 0` builds without the ILA.

### Using the chip's own I2C and SPI controllers on real pins

`uio[0..7]` are on Pmod JB, each pin driven only when the chip enables it. To let the chip's own
I2C controller talk to the PS's target: SW0 off, wire **JB3 to JA3 (SCL)** and **JB4 to JA4
(SDA)**, then

```bash
python3 tools/referee_host.py --port COM5 load firmware/engine/i2c_controller.s --pin 2:od,3:od --run
python3 tools/referee_host.py --port COM5 i2c-write 50 10 a5 3c
# ACK
python3 tools/referee_host.py --port COM5 i2c-read 50 10 2
# a5 3c
```

(One engine: while it drives the bus it is not refereeing it.) SPI (`spi_cpha0.s`, SCK `uio[4]`
= JB7, MOSI JB8, CS JB9, MISO JB10) works the same way with a device on JB.

## Local checks (no Vivado)

```bash
fpga/sim/run.sh          # lint + simulation + ILA check, about 10 minutes;  fpga/sim/run.sh yosys  adds the
                         # estimate (synthesis of the whole chip: needs well over 4 GB of RAM)
```

- **Lint:** `verilator --lint-only -Wall` on `rtl/fpga_top.v` with the chip's sources, a behavioural
  IOBUF and a port-only stand-in for the block design, with both boards' generics: clean (the one
  waiver, DECLFILENAME, is for `src/project.v`, whose module is `tt_um_shreyansh_referee`).
- **Lint and Yosys** use `rtl/ila_none.v` for the two ILA IPs.
- **Simulation (Icarus):** `sim/tb_fpga.v` runs `fpga_top` with the chip at 60 MHz and
  `sim/zynq_ps_model.v` in place of the block design: a PS UART on EMIO, an I2C controller and an
  I2C target (0x50, clock stretching) on EMIO pins, each modelled at its pins (drive `_t` low to
  pull, read `_i`). It checks: identify `49 00 00 00` answers `06 52` through the EMIO UART at
  115200; all 109 load / RUN packets of `i2c_setup_referee.s` are ACKed; writes and repeated-start
  read-backs at 400 kHz and 100 kHz over the open-drain nets read back correctly, with the target
  stretching SCL; the chip's judge channels see every pad edge; `stats 0` has one verdict per SCL
  rise on the pad (268: 260 PASS, 8 VIOLATION, 0 CAN'T TELL); the 8 VIOLATIONs are the bits of a
  byte whose SDA changes 40 ns before SCL rises, and `drain` returns them.
  `sim/ila_model.v` stands in for the ILA (its own 240 MHz clock; every sample with
  `ila_chg = 1` from RUN on, written as a Vivado-style CSV) and the testbench writes the chip's
  stats, drained slots and status as JSON lines; `tools/ila_check.py` then checks all 268
  verdicts against the simulated capture (PASSED, alignment unique: the 8 VIOLATIONs land on
  the 8 rises with a 40 ns setup), and, as a negative control, the same capture against a
  30 ns bound, where those VIOLATIONs are wrong, must FAIL.
- **Yosys** `synth_xilinx -family xc7`: about 2,300 LUTs as logic plus 45 LUTRAM cells
  (~180 LUTs), ~1,630 flip-flops, 161 CARRY4, no block RAM, no DSP: under 5% of the 7020's
  53,200 LUTs. The longest path in the chip is about 18 LUT levels when Yosys maps adders to LUTs
  (Vivado uses carry chains and will have fewer).

## What is not verified

- **Nothing here has been through Vivado or Vitis**, and nothing has run on a board. In particular
  **timing at 60 MHz on the -1 speed grade**: the estimate above (18 LUT levels, roughly 10-13 ns
  against 16.7 ns) suggests it closes, but only `timing_summary.rpt` can say.
- The PS7 property names and values are copied from the cited sources and checked by the script
  as it runs; the PS7 IP's built-in "ZedBoard" preset is assumed present (Avnet's own board file
  relies on it). The block design's port names are checked by the script after generation.
- `sw/main.c` was syntax-checked against stand-in headers, not compiled with a real BSP. Its I2C
  target and repeated-start read follow the Linux `i2c-cadence` driver and Xilinx's XIicPs driver
  (sources read, not run); the DATA-interrupt handshake of target mode is the riskiest part.
  Symptom if it is wrong: LD4 (error) on, LD5 off, `stats 0` still counts verdicts.
- The simulation models the PS controllers at their pins; it does not model the Cadence IP.
- Button polarity (pressed = 1) is from Avnet's board file (ZedBoard) and the PYNQ button driver.
- **The ILA:** the IP property names (`C_EN_STRG_QUAL`, `C_PROBE11_TYPE`, `ALL_PROBE_SAME_MU_CNT`,
  ...), the hardware-manager properties and compare values in `ila_capture.tcl`
  (`CONTROL.CAPTURE_MODE`, `CAPTURE_COMPARE_VALUE eq1'b1`, `eq1'bF`, `eq8'bXXXFXXXX`), the CSV
  layout `write_hw_ila_data -csv_file` produces, and 240 MHz timing on the -1 part are from
  Xilinx's documentation and examples as remembered, not from a run. `build.tcl` reads its ILA
  settings back and stops if one did not take; `ila_check.py` finds columns by probe name and
  reads any radix. Both Tcl scripts were only run with every Vivado command stubbed (syntax and
  control flow), and `ila_check.py` only on synthetic and simulated captures.
- Whether the ILA really ignores a trigger while it is still filling its pre-trigger samples does
  not matter here: the heartbeat fills them within 17 ms of arming. Wait a second after `ARMED`.
- `set_max_delay -datapath_only` from an input port with no `set_input_delay` (`ila.xdc`) is
  expected to constrain the pad-to-flop path on its own; `build.tcl` stops if the delays cannot
  be read, so a constraint that did not take shows up at build time.
- `STATUS.CORE_STATUS` reading `FULL` after a complete capture (`ila_capture.tcl` refuses to
  export otherwise) is from the documentation, not tried.
- The board oscillator pins: ZedBoard Y9 (Avnet board file `sys_clk`, 100 MHz), PYNQ-Z2 H16 (TUL
  board file `sys_clk`, 125 MHz), both LVCMOS33; copied, not tried.

## If 60 MHz does not close

Build the chip for a slower clock, for example 50 MHz:

```bash
vivado -mode batch -source fpga/build.tcl -tclargs zedboard 4 50
```

This sets the MMCM to 50.000 MHz and the chip's power-up bit period `CLKS_PER_BIT` to
round(50 MHz / 115200) = 434. The firmware's time bounds are converted to cycles at assembly
time, and `tools/referee_host.py` takes the clock from `info.yaml`, so for that build set
`clock_hz: 50000000` in `info.yaml` in your local copy (do not commit it; the chip is 60 MHz), or
assemble with `python3 tools/refasm.py --clock-hz 50000000`. What changes: one cycle is 20 ns, so
the measurement uncertainty (2 cycles) grows from 33 ns to 40 ns and the CAN'T TELL band around
each bound widens by 7 ns each way; the longest judgeable interval grows from 273 us to 328 us;
it is no longer the chip's own operating point.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No banner on the USB-UART | right port (ZedBoard J14; PYNQ-Z2 the second FTDI interface), 115200 8N1; boot jumpers on JTAG; the app was started (Run, or `run_app.tcl`) |
| Banner, but `id` says `bad reply nothing` | LD0 not blinking: the chip is in reset. `ps7_post_config` not run (no FCLK reset release, no EMIO), reset button held, or the bitstream is not loaded |
| `bad reply` with odd bytes | another program has the port open; or `--fast` was used (press the reset button) |
| `RUN refused` | the image's CRC or pin claims failed: reload with the same command |
| LD4 (error) on with traffic | missing or too-weak pull-ups at 400 kHz (try 100 kHz with SW1 off); another device at 0x50 on the Pmod; Pmod pins swapped |
| LD2 never lights | traffic off (SW0), or the app is not running |
| `stats 0` stuck at 65535 | 16-bit counters saturate: `load ... --run` again to clear them |
| `drain` prints nothing | normal on a healthy bus: only non-PASS verdicts are queued |
| Vivado: `PS7 ... is '...', expected '...'` | a property did not take in your Vivado version; look at the PS7 settings in the block design and the Vivado log |
| Vivado: `CONFIG.preset` error on ZedBoard | in `build.tcl` change `set ps7_preset(zedboard) ZedBoard` to `set ps7_preset(zedboard) {}`; the explicit Digilent values still apply |
| Vivado: block design port ... not found | Vivado named an EMIO port differently: rename it in `rtl/fpga_top.v` to match the generated `zynq_ps.v` |
| Timing not met | see "If 60 MHz does not close", or "If the ILA does not close" if the log names the ILA's domain |
| `ila_capture.tcl`: no ILA found | the bitstream on the board is not this build (or not loaded: LD0 dark), the `.ltx` is from another build, or the JTAG clock is too fast for the debug hub: `-jtag_hz 1000000` |
| `ila_capture.tcl` never finishes | it waits for the trigger: `-trigger mark` needs the app from this commit (GPIO 15); try `-trigger scl_fall` or `now` |
| `ila_check.py`: stats count N judgments, the ILA shows M | the ILA window missed part of the run (arm before the traffic, load with `--run` just before), or a glitch on SCL (see its `note:` lines) |
| `ila_check.py`: stream incomplete / VERDICT_OVF | more than 8 non-PASS verdicts at once: the FIFO dropped some; capture a shorter burst |

## Scope: what this shows and what it does not

It shows the chip's unchanged RTL, at its own 60 MHz clock and with its host protocol
unchanged, refereeing a real, third-party I2C implementation on real pins, end to end from the
PC tool. It does not show the ASIC: FPGA input buffers have different thresholds (LVCMOS33, not
the IHP pads), propagation delays and noise behaviour than the chip's pads, the judge channels
pass through an extra input register, and the FPGA's timing has nothing to do with the
hardened chip's. Verdicts are only as good as the chip's assumptions (`README.md`, "The error
budget"), and the I2C setup-time rule it runs is not yet covered by the formal proof (it uses
"the latest edge"). The 3 Mbaud host mode is not available through the bridge.
