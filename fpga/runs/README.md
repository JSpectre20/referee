# Recorded board runs

One folder per run of the FPGA build on a real bus, with everything needed to re-check the
chip's verdicts against the ILA's independent capture later, without the board:

```bash
python3 tools/ila_check.py fpga/runs/<run>          # one run: per-verdict table, ends PASSED / FAILED
python3 tools/ila_check.py --all fpga/runs          # every run here, each against its "expect"
```

`--all` is what a release check runs (pure Python, no Vivado); it exits non-zero if any run's
result differs from its `expect`, and says "NO RUNS: nothing checked" (not PASSED) for an empty
folder. `test/tools/test_ila_check.py::test_recorded_board_runs` runs it in CI (`pytest tools`)
once a run is recorded.

## Layout

```
fpga/runs/
  2026-12-01_zedboard_400k-btnl/      <date>_<board>_<what>, one folder per capture
    run.json         the parameters below (required)
    ila.csv          the ILA capture: fpga/ila_capture.tcl (write_hw_ila_data -csv_file)
    ila.ila          the same in Vivado's format (open it in the hardware manager; optional)
    capture.txt      ila_capture.tcl's settings, the ILA's status, the time
    ila_info.txt     copied from fpga/build/<board>/: the ILA clock, depth, probes and the
                     pad-to-sampling-flop delays of this bitstream
    verdicts.txt     the chip's side: tools/referee_host.py output (drain, stats, status), or
    verdicts.jsonl   the same as JSON lines (format below)
    check.txt        python3 tools/ila_check.py <run> > check.txt  (the result when recorded)
    scope/           optional: scope screenshots / CSVs of the rise times
    (ila.vcd         the same capture as a VCD: git-ignored, it is large)
```

## run.json

`ila_clock_hz` is required. `build_args` and `bitstream_sha256` are compared with the run's
`ila_info.txt` (copy both values from it), as are `ila_clock_hz`, `chip_clock_hz` and `skew_ns`
(which must be at least the build's). `image_crc16` (the "image crc16" line `referee_host.py load`
prints) is compared with the CRC of `firmware` assembled again. The rest says what bus the
verdicts were given on. Unknown keys are kept and ignored.

```json
{
  "date": "2026-12-01",
  "board": "zedboard",
  "commit": "<git rev-parse HEAD of the build>",
  "bitstream_sha256": "<sha256sum fpga/build/zedboard/referee_zedboard.bit>",
  "build_args": "zedboard",
  "firmware": "firmware/engine/i2c_setup_referee_raw.s",
  "image_crc16": "d3e9",
  "push": "nonpass",
  "chip_clock_hz": 60000000,
  "ila_clock_hz": 240000000,
  "skew_ns": 2.0,
  "aperture_ns": 0.25,
  "ppm": 100,
  "max_unresolved": 0,
  "rule": {"name": "tSU;DAT", "trigger": "scl", "trigger_edge": "rise",
           "ref": "sda", "ref_edge": "last", "kind": "min", "bound_ns": 100, "slot": 0},
  "bus": {
    "speed_hz": 400000,
    "pullups": "2.2 kohm to 3.3 V on SCL and SDA (Pmod JA pins 3, 4 to pin 6)",
    "wiring": "on-board only, nothing on the Pmod but the pull-ups",
    "rise_time_ns": {"scl": 120, "sda": 125},
    "rise_time_how": "scope on JA3 / JA4, 30 % - 70 % of 3.3 V, 10 rises averaged",
    "envelope": "declared bus envelope: monotonic edges, no runts, rise time <= 300 ns"
  },
  "traffic": "SW0 off, load ... --run, ILA armed (-trigger mark), BTNL once: 120 SCL rises",
  "expect": "PASSED",
  "notes": ""
}
```

| Key | Meaning | Default |
| --- | --- | --- |
| `ila_clock_hz` | the ILA's sample clock as built (`ila_info.txt`) | required |
| `chip_clock_hz` | the chip's clock as built | `clock_hz` in `info.yaml` |
| `skew_ns` | bound on how differently the two judged pads reach their ILA sampling flops: at least `skew_ns` of `ila_info.txt` (Vivado's routed delays, each path constrained to 3 ns) | 2.0 |
| `aperture_ns` | per edge: the sampling flop's setup / hold window, the MMCM's jitter and the two flops' clock-arrival difference | 0.25 |
| `max_unresolved` | verdicts the ILA may leave unresolved (inside its own band) before the run is INCONCLUSIVE (FAILED); `--strict` makes it 0 | 0 |
| `image_crc16`, `firmware` | the loaded image's CRC and its source, re-assembled and compared | - |
| `build_args`, `bitstream_sha256` | compared with `ila_info.txt` | - |
| `ppm` | the two clocks' combined frequency tolerance (board oscillator and PS crystal) | 100 |
| `push` | the firmware's JUDGE push mode: `nonpass` (the shipped `i2c_setup_referee.s`) or `all` | `nonpass` |
| `rule` | what the firmware judges: the interval from `ref`'s `ref_edge` (`rise`, `fall`, `last`) to `trigger`'s `trigger_edge`, a `min` or `max` bound in ns, the bound slot `b` | tSU;DAT, 100 ns |
| `columns` | CSV probe names, if they differ: `{"scl": "ila_scl", "sda": "ila_sda", "ts": "ila_ts", ...}` | as built |
| `files` | `{"csv": "ila.csv", "verdicts": "verdicts.txt"}` | `ila.csv`; `verdicts.jsonl`, `verdicts.txt` or `host.log` |
| `expect` | `PASSED`, or `FAILED` for a deliberate negative control | `PASSED` |

## The verdict log

Whatever `tools/referee_host.py` prints can go straight into `verdicts.txt`, in the order it
happened:

```bash
P=/dev/ttyACM0; R=fpga/runs/2026-12-01_zedboard_400k-btnl
python3 tools/referee_host.py --port $P drain    >> $R/verdicts.txt   # repeat until it prints nothing
python3 tools/referee_host.py --port $P stats 0  >> $R/verdicts.txt
python3 tools/referee_host.py --port $P status   >> $R/verdicts.txt
```

`ila_check.py` reads those lines (Python dicts) and also JSON lines, one record per line:

| Record | Meaning |
| --- | --- |
| `{"type": "drain", "slots": [h, h, m, ...]}` | the raw 16-bit slots of one drain, exactly as the chip sent them (a raw verdict's second slot is its m); decoded with `referee_host.split_verdicts` |
| `{"type": "verdict", "verdict": "PASS", "b": 0, "m": 7, "m3": 3, "FX": 0, "FY": 0, "TAG": 0, "L": 0, "NI": 1, "ORD": 0}` | one decoded verdict (`referee_host.decode_verdict` plus `m`, `null` if not raw) |
| `{"type": "stats", "slot": 0, "pass": 120, "viol": 0, "indet": 0, "min": 36, "max": 200}` | `stats <slot>`: the counts since the last `stats-clear` (or reset; RUN does not clear them), min / max as the 16-bit words read; `referee_host.py` prints the slot too |
| `{"type": "status", "data": 129}` | `status`: the 'Q' byte (or the decoded dict); `verdict_ovf` marks an incomplete stream |

The simulation (`fpga/sim/run.sh`) writes `fpga/sim/out/ila_run/verdicts.jsonl` in this format.

## What the check can and cannot say

- **Every judged edge is checked.** The chip judges every SCL rise (the shipped firmware), so
  the ILA trace's rises are the judgments: their number must equal the chip's
  `pass + viol + indet` since the stats were cleared, so the ILA window must contain the whole
  run (the pre-arm sequence of `fpga/README.md` step 5; SW0 off; one BTNL press, or any burst that
  fits the buffer). A mismatch with runts in the trace is reported as outside the envelope.
- **The ILA's own numbers are checked too**: `ila_clock_hz` is required and must match the
  build's `ila_info.txt`; `skew_ns` must not undercut the build's; and the trace calibrates
  itself (the chip-side probes change only on clk60 edges, so their gaps must be whole numbers
  of chip cycles at the stated clock ratio). Not caught by the self-check: a stated ratio that
  is an exact sub-multiple of the true one (e.g. 120 MHz stated for 240), which `ila_info.txt`
  catches.
- **No verdict proven right is not a pass**: if no PASS or VIOLATION is confirmed (for example
  a chip answering CAN'T TELL everywhere), or more verdicts are unresolved than
  `max_unresolved` (default 0), the run is INCONCLUSIVE and FAILS. An unflagged CAN'T TELL where
  the ILA shows the interval outside bound +- (2u + 1/16) cycles, where the chip's rule must
  have decided, is reported as a completeness note (fails under `--strict`).
- **`push all`**: verdict k belongs to rise k. Exact, but the 8-slot verdict FIFO and the
  115200-baud bridge cannot keep up with a PS-driven bus, so this needs a slow (host-paced)
  bus, e.g. the chip's own Player via `i2c-write`.
- **`push nonpass`** (the shipped firmware): only VIOLATION / CAN'T TELL verdicts are in the
  stream, in order; every other rise was a PASS. The checker finds an order-preserving
  placement of the stream on the rises that the ILA agrees with, and says whether it is unique;
  a rise that gets different verdicts under different placements is counted as unresolved.
  A planted wrong PASS (a broken rise with no VIOLATION left to cover it) or a wrong VIOLATION
  (no rise it fits) fails. **Its limit:** with header-only verdicts (m saturated at 3 cycles), a
  chip that mislabelled one clean rise as VIOLATION *and* the neighbouring broken rise as PASS
  looks the same as a correct chip; a raw verdict's exact m (`i2c_setup_referee_raw.s`, the
  recommended firmware for evidence runs) or `push all` closes that gap
  (`test_swap_needs_raw_m`).
- **Unresolved** verdicts are inside the ILA's own band (about +-6.7 ns at 240 MHz with 2 ns
  skew): the ILA can neither confirm nor refute them.
- The ILA samples the same pads through the same LVCMOS33 input buffers as the chip: its clock
  and resolution are independent of the chip's, its input threshold is not.
