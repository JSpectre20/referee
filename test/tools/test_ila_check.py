# SPDX-License-Identifier: Apache-2.0
"""tools/ila_check.py: synthetic ILA captures (Vivado CSV layout) and chip verdict records.

Each test builds an I2C-like trace (SCL rises; SDA changes `setup` ns before each rise) as the
ILA would store it with capture control "ila_chg == 1" (one row per change, the sample number
in ila_ts), and a verdict log as tools/referee_host.py or the testbench writes it, then checks
the outcome. The planted wrong verdicts (a PASS on a broken bound, a VIOLATION on a met one,
a wrong m) must FAIL.
"""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import ila_check as ic  # noqa: E402

T = 1e9 / 240e6  # 4.1667 ns
CLEAN, BAD, EDGE = 600.0, 40.0, 100.0


def trace(setups, period=2500.0, high=1200.0):
    """Events (t_ns, signal, level): START, then one SCL rise per entry of `setups`, SDA
    toggling `setup` ns before each rise."""
    ev = [(300.0, "sda", 0), (500.0, "scl", 0)]
    sda = 0
    for i, su in enumerate(setups):
        tr = 1000.0 + i * period
        sda ^= 1
        ev.append((tr - su, "sda", sda))
        ev.append((tr, "scl", 1))
        ev.append((tr + high, "scl", 0))
    return sorted(ev)


TC = 1e9 / 60e6  # 16.667 ns


def write_csv(path, events, ts0=5000, radix=True, prefix="", hex_ts=True, chip=True, t_ila=T):
    """Rows as the ILA stores them: the first sample, then every sample where a probe changed.
    An edge at t is stored in the first sample at or after it (sample period t_ila). With chip=True
    the chip's own view of SCL / SDA is there too, as on the board: the pad sampled on a clk60 edge
    (IOB register), so those columns change only on clk60 edges, then sampled by the ILA."""
    ev = [(ts0 + math.ceil(t / t_ila), sig, v) for t, sig, v in events]
    if chip:
        for t, sig, v in events:
            tc = math.ceil((t - 3.1) / TC) * TC + 3.1 + TC  # the clk60 edge that registers it
            ev.append((ts0 + math.ceil((tc + 0.7) / t_ila), sig + "_chip", v))
    ev.sort(key=lambda e: e[0])
    names = ["scl", "sda"] + (["scl_chip", "sda_chip"] if chip else [])
    lv = {n: 1 for n in names}
    rows = [(ts0, dict(lv))]
    for s, sig, v in ev:
        lv[sig] = v
        if rows[-1][0] == s:
            rows[-1] = (s, dict(lv))
        else:
            rows.append((s, dict(lv)))
    lines = ["Sample in Buffer,Sample in Window,TRIGGER," + ",".join(prefix + "ila_" + n for n in names) +
             f",{prefix}ila_ts[39:0]"]
    if radix:
        lines.append("Radix - UNSIGNED,UNSIGNED,UNSIGNED," + "BINARY," * len(names) + ("HEX" if hex_ts else "UNSIGNED"))
    for n, (s, l) in enumerate(rows):
        s &= (1 << 40) - 1
        vals = ",".join(str(l[x]) for x in names)
        lines.append(f"{n},{n},0,{vals},{s:010x}" if hex_ts else f"{n},{n},0,{vals},{s}")
    Path(path).write_text("\n".join(lines) + "\n")


def hdr(code, m3=3, raw=0, ord_=0, b=0, ni=1):
    """A VERDICT header slot (src/engine.v vhdr): code 0 CAN'T TELL, 1 PASS, 2 VIOLATION."""
    return code << 14 | ni << 9 | raw << 8 | b << 4 | ord_ << 3 | (m3 & 7)


def run(tmp_path, setups, records, cfg=None, strict=False, info=None, **kw):
    write_csv(tmp_path / "ila.csv", trace(setups), **kw)
    (tmp_path / "v.jsonl").write_text("".join(
        (r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records))
    c = ic.merge_cfg(ic.DEFAULTS, {"ila_clock_hz": 240e6, "chip_clock_hz": 60e6, **(cfg or {})})
    if info is not None:
        c["_ila_info"] = info
    out = []
    ok = ic.check(c, tmp_path / "ila.csv", tmp_path / "v.jsonl", strict=strict, emit=out.append)
    text = "\n".join(out)
    assert out[-1] == ("PASSED" if ok else "FAILED")
    return ok, text


def stats(p, v=0, i=0, mn=36, mx=36):
    return {"type": "stats", "slot": 0, "pass": p, "viol": v, "indet": i, "min": mn, "max": mx}


# ---------------------------------------------------------------- push nonpass (the shipped firmware)


def test_clean_bus_all_pass(tmp_path):
    ok, text = run(tmp_path, [CLEAN] * 20, [stats(20)])
    assert ok, text
    assert "20 confirmed" in text and "alignment: unique" in text


def test_violations_aligned_by_order(tmp_path):
    setups = [CLEAN] * 5 + [BAD] * 3 + [CLEAN] * 4
    drain = {"type": "drain", "slots": [hdr(2, m3=2)] * 3}
    ok, text = run(tmp_path, setups, [stats(9, 3, mn=2), drain])
    assert ok, text
    assert "alignment: unique" in text


def test_planted_wrong_pass_fails(tmp_path):
    """Three broken setups, but the chip reported only two VIOLATIONs: one PASS is wrong."""
    setups = [CLEAN] * 5 + [BAD] * 3 + [CLEAN] * 4
    drain = {"type": "drain", "slots": [hdr(2, m3=2)] * 2}
    ok, text = run(tmp_path, setups, [stats(10, 2, mn=2), drain])
    assert not ok
    assert "no order-preserving alignment" in text and "a PASS there is wrong" in text


def test_planted_wrong_violation_fails(tmp_path):
    """A clean bus, but the chip reported a VIOLATION."""
    drain = {"type": "drain", "slots": [hdr(2, m3=3)]}
    ok, text = run(tmp_path, [CLEAN] * 12, [stats(11, 1), drain])
    assert not ok
    assert "fits no judged edge" in text


def test_cant_tell_is_always_consistent(tmp_path):
    """CAN'T TELL never contradicts: flagged ones (ORD) on any rise, next to confirmed PASSes."""
    drain = {"type": "drain", "slots": [hdr(1), hdr(0, ord_=1), hdr(0, ord_=1), hdr(1)]}
    ok, text = run(tmp_path, [CLEAN, BAD, EDGE, CLEAN], [stats(2, 0, 2), drain], {"push": "all"})
    assert ok, text
    assert "2 CAN'T TELL" in text and "2 confirmed" in text


def test_all_cant_tell_is_inconclusive(tmp_path):
    """Review finding M3: a chip answering CAN'T TELL everywhere must not PASS; unflagged ones on
    a clean bus are also reported as incomplete (and fail under --strict)."""
    recs = [stats(0, 0, 10), {"type": "drain", "slots": [hdr(0, m3=3)] * 10}]
    for push in ("all", "nonpass"):
        ok, text = run(tmp_path, [CLEAN] * 10, recs, {"push": push})
        assert not ok and "INCONCLUSIVE" in text and "should have decided" in text
    # one unflagged CAN'T TELL on a 10 ns setup: far outside the chip's band (100 - 68 ns), so
    # its rule must have said VIOLATION: noted (completeness), failed only with --strict
    recs = [stats(9, 0, 1), {"type": "drain", "slots": [hdr(0, m3=0)]}]
    ok, text = run(tmp_path, [CLEAN] * 9 + [10.0], recs)
    assert ok and "should have decided" in text and "must have said VIOLATION" in text, text
    ok, text = run(tmp_path, [CLEAN] * 9 + [10.0], recs, strict=True)
    assert not ok


def test_incomplete_stream_fails(tmp_path):
    setups = [BAD] * 10
    drain = {"type": "drain", "slots": [hdr(2, m3=2)] * 8}
    ok, text = run(tmp_path, setups, [stats(0, 10, mn=2, mx=2), drain, {"type": "status", "data": 0x04}])
    assert not ok
    assert "VERDICT_OVF" in text and "stream incomplete" in text


def test_count_mismatch_fails(tmp_path):
    ok, text = run(tmp_path, [CLEAN] * 10, [stats(9)])
    assert not ok and "stats count 9 judgments, the ILA shows 10" in text


def test_nonpass_needs_stats(tmp_path):
    ok, text = run(tmp_path, [CLEAN] * 3, [])
    assert not ok and "needs the stats record" in text


# ---------------------------------------------------------------- push all (positional)


def test_all_mode_positional(tmp_path):
    setups = [CLEAN, BAD, CLEAN, BAD]
    good = [hdr(1), hdr(2, m3=2), hdr(1), hdr(2, m3=2)]
    ok, text = run(tmp_path, setups, [stats(2, 2, mn=2), {"type": "drain", "slots": good}], {"push": "all"})
    assert ok, text
    wrong = [hdr(1), hdr(1), hdr(1), hdr(2, m3=2)]  # edge 1 (40 ns) labelled PASS
    ok, text = run(tmp_path, setups, [stats(3, 1, mn=2), {"type": "drain", "slots": wrong}], {"push": "all"})
    assert not ok
    row1 = [ln for ln in text.splitlines() if ln.strip().startswith("1 ")][0]
    assert "PASS" in row1 and "CONTRADICTED" in row1


def test_raw_m_checked(tmp_path):
    """m is the chip's measurement: |m - true| < 2 cycles. 600 ns = 36 cycles at 60 MHz."""
    good = {"type": "drain", "slots": [hdr(1, raw=1), 36]}
    ok, text = run(tmp_path, [CLEAN], [stats(1), good], {"push": "all"})
    assert ok, text
    off = {"type": "drain", "slots": [hdr(1, raw=1), 30]}  # 500 ns: the ILA says 600
    ok, text = run(tmp_path, [CLEAN], [stats(1, mn=30, mx=30), off], {"push": "all"})
    assert not ok and "PASS*" in text


def test_unresolved_band(tmp_path):
    """Exactly at the bound the ILA cannot decide: INCONCLUSIVE by default (max_unresolved 0);
    a run may allow a few in run.json, never with --strict."""
    recs = [stats(2, mn=6, mx=36), {"type": "drain", "slots": [hdr(1), hdr(1)]}]
    ok, text = run(tmp_path, [EDGE, CLEAN], recs, {"push": "all"})
    assert not ok and "1 unresolved" in text and "INCONCLUSIVE" in text, text
    ok, text = run(tmp_path, [EDGE, CLEAN], recs, {"push": "all", "max_unresolved": 1})
    assert ok, text
    ok, text = run(tmp_path, [EDGE, CLEAN], recs, {"push": "all", "max_unresolved": 1}, strict=True)
    assert not ok


def test_huge_skew_cannot_hide_a_wrong_pass(tmp_path):
    """Review finding M1/M3: skew_ns 1000 turned a wrong PASS into 'unresolved' -> PASSED."""
    ok, text = run(tmp_path, [BAD] * 5, [stats(5)], {"skew_ns": 1000})
    assert not ok and "INCONCLUSIVE" in text


def test_wrong_ila_clock_caught_by_self_calibration(tmp_path):
    """Review finding M1: run.json says 200 MHz, the trace is 240 MHz; true setup 90 / 95 ns, the
    chip wrongly PASSes. The chip-side probes change only on clk60 edges: the gaps do not fit."""
    for su, mn in ((90.0, 36), (95.0, 8)):
        ok, text = run(tmp_path, [su] * 3, [stats(3, mn=mn, mx=mn)], {"ila_clock_hz": 200e6})
        assert not ok and "disagrees with the stated clocks" in text, text
        ok, text = run(tmp_path, [su] * 3, [stats(3, mn=mn, mx=mn)])
        assert not ok  # and with the right clock the wrong PASS is caught directly
    ok, text = run(tmp_path, [CLEAN] * 10, [stats(10)])
    assert ok and "self-calibration" in text


def test_ila_clock_required(tmp_path):
    write_csv(tmp_path / "ila.csv", trace([CLEAN]))
    (tmp_path / "v.jsonl").write_text(json.dumps(stats(1)) + "\n")
    assert ic.main(["--csv", str(tmp_path / "ila.csv"), "--verdicts", str(tmp_path / "v.jsonl")]) == 2
    assert ic.main(["--csv", str(tmp_path / "ila.csv"), "--verdicts", str(tmp_path / "v.jsonl"),
                    "--ila-clock-hz", "240e6", "--chip-clock-hz", "60e6"]) == 0


def test_ila_info_cross_checks(tmp_path):
    info = {"ila_clock_hz": "240000000", "chip_clock_hz": "60000000", "skew_ns": "1.200",
            "bitstream_sha256": "ab" * 32}
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], {}, info=info)
    assert ok, text
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], {}, info={**info, "ila_clock_hz": "200000000"})
    assert not ok and "differs from the build's ila_info.txt" in text
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], {"skew_ns": 1.0}, info=info)
    assert not ok and "below the build's measured" in text
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], {}, info={**info, "skew_ns": "?"})
    assert not ok and "unbounded" in text
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], {"bitstream_sha256": "cd" * 32}, info=info)
    assert not ok and "bitstream_sha256" in text
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], {}, strict=True)
    assert not ok and "no ila_info.txt" in text


def test_ambiguous_alignment_is_unresolved(tmp_path):
    """Review minor 1: two rises inside the band, one VIOLATION: which rise is not known, so
    neither row can be confirmed."""
    ok, text = run(tmp_path, [EDGE, EDGE, CLEAN], [stats(2, 1, mn=6, mx=36), {"type": "drain", "slots": [hdr(2, m3=3)]}],
                   {"max_unresolved": 2})
    assert ok and "2 possible" in text and "PASS|VIOLATION" in text and "2 unresolved" in text, text


def test_shifted_all_mode_stream_fails(tmp_path):
    recs = [stats(2, 1, mn=2, mx=36), {"type": "drain", "slots": [hdr(1), hdr(1), hdr(2, m3=2)]}]
    ok, _ = run(tmp_path, [CLEAN, BAD, CLEAN], recs, {"push": "all"})
    assert not ok


def test_image_crc_checked(tmp_path):
    import referee_host as host
    fw = "firmware/engine/i2c_setup_referee.s"
    img, _ = host.assemble((Path(__file__).resolve().parents[2] / fw).read_text())
    crc = host.refasm.crc16(img)
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], {"firmware": fw, "image_crc16": f"{crc:04x}"})
    assert ok, text
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], {"firmware": fw, "image_crc16": f"{crc ^ 1:04x}"})
    assert not ok and "assembles to CRC" in text


def test_stats_without_slot_noted(tmp_path):
    st = stats(4)
    del st["slot"]
    ok, text = run(tmp_path, [CLEAN] * 4, [st])
    assert ok and "names no slot" in text


def test_max_bound(tmp_path):
    """A maximum rule: SCL high time (rise -> fall) at most 1000 ns; the trace has 1200 ns."""
    rule = {"rule": {"name": "tHIGH max", "trigger": "scl", "trigger_edge": "fall", "ref": "scl",
                     "ref_edge": "rise", "kind": "max", "bound_ns": 1000.0, "slot": 0}, "push": "all"}
    # 3 judged falls after rises (the START's fall has no rise before it in the trace)
    viols = [{"type": "verdict", "verdict": "VIOLATION", "b": 0, "m3": 3}] * 3
    recs = [{"type": "verdict", "verdict": "INDETERMINATE", "b": 0, "FX": 1}] + viols
    ok, text = run(tmp_path, [CLEAN] * 3, [stats(0, 3, 1, mn=72, mx=72)] + recs, rule)
    assert ok, text
    recs[2] = {"type": "verdict", "verdict": "PASS", "b": 0, "m3": 3}
    ok, text = run(tmp_path, [CLEAN] * 3, [stats(1, 2, 1, mn=72, mx=72)] + recs, rule)
    assert not ok


# ---------------------------------------------------------------- inputs


def test_referee_host_text_output(tmp_path):
    """The lines `referee_host.py drain`, `stats 0` and `status` print, appended to one file."""
    setups = [CLEAN] * 3 + [BAD]
    import referee_host as host
    h = host.decode_verdict(hdr(2, m3=2))
    lines = ["Referee", str(h) + " ", "{'slot': 0, 'pass': 3, 'viol': 1, 'indet': 0, 'min': 2, 'max': 36}",
             str(host.decode_status(0x80))]
    ok, text = run(tmp_path, setups, lines)
    assert ok, text


def test_counter_wrap_and_hierarchical_names(tmp_path):
    ok, text = run(tmp_path, [CLEAN] * 6, [stats(6)], ts0=(1 << 40) - 400, prefix="u_top/")
    assert ok, text


def test_no_radix_row_decimal_ts(tmp_path):
    ok, text = run(tmp_path, [CLEAN] * 4, [stats(4)], radix=False, hex_ts=False)
    assert ok, text


def test_uncertain_reference_order(tmp_path):
    """SDA changes in the same sample as SCL rises: the latest SDA edge before the rise may be
    the previous one, so the interval is the hull of both; a CAN'T TELL is consistent, a
    VIOLATION claim (true < 100 ns) cannot be confirmed."""
    write_csv(tmp_path / "ila.csv", trace([0.5]))
    p = ic.Params(ic.merge_cfg(ic.DEFAULTS, {"ila_clock_hz": 240e6}))
    ila = ic.read_ila_csv(tmp_path / "ila.csv", ic.DEFAULTS["columns"])
    te = [s for s, lv in ic.edges(ila["scl"], ila["ts"]) if lv]
    re_ = [s for s, _ in ic.edges(ila["sda"], ila["ts"])]
    iv = ic.judged_intervals(te, re_, ila["ts"][0], p)[0]
    assert iv["lo"] < 0 and iv["hi"] > 600  # the hull reaches back to the START's SDA fall
    assert ic.claim("INDETERMINATE", iv, p) == ic.OK
    assert ic.claim("VIOLATION", iv, p) == ic.UNRES


def test_align_counts():
    # two rises that may each pass or violate, one VIOLATION: two ways
    ways, asg = ic.align([True, True], [[True, True]])
    assert ways == 2 and len(asg) == 1
    # a rise that cannot pass must be covered
    ways, asg = ic.align([True, False, True], [[True, True, True]])
    assert ways == 1 and asg == {0: 1}
    ways, _ = ic.align([False, False], [[True, True]])
    assert ways == 0


def test_all_runs_cli(tmp_path, capsys):
    good, neg = tmp_path / "runs" / "a_good", tmp_path / "runs" / "b_negative"
    for d in (good, neg):
        d.mkdir(parents=True)
        write_csv(d / "ila.csv", trace([CLEAN] * 5 + [BAD]))
        (d / "verdicts.jsonl").write_text(json.dumps(stats(5, 1, mn=2)) + "\n" +
                                          json.dumps({"type": "drain", "slots": [hdr(2, m3=2)]}) + "\n")
    (good / "run.json").write_text(json.dumps({"ila_clock_hz": 240e6, "chip_clock_hz": 60e6}))
    # the negative control: the same capture against a 30 ns bound, where the VIOLATION is wrong
    (neg / "run.json").write_text(json.dumps({"ila_clock_hz": 240e6, "chip_clock_hz": 60e6,
                                              "rule": {"bound_ns": 30}, "expect": "FAILED"}))
    assert ic.main(["--all", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    assert "ok   a_good: PASSED" in out and "ok   b_negative: FAILED" in out
    assert ic.main([str(neg)]) == 1
    (tmp_path / "empty").mkdir()
    capsys.readouterr()
    assert ic.main(["--all", str(tmp_path / "empty")]) == 0
    out = capsys.readouterr().out
    assert "NO RUNS: nothing checked" in out and "PASSED" not in out


def test_recorded_board_runs():
    """Every recorded run in fpga/runs still gives its expected result (CI: pytest tools)."""
    runs = Path(__file__).resolve().parents[2] / "fpga" / "runs"
    if not any((d / "run.json").exists() for d in runs.iterdir() if d.is_dir()):
        pytest.skip("no recorded board runs yet")
    assert ic.main(["--all", str(runs)]) == 0


def test_bad_inputs_fail(tmp_path):
    (tmp_path / "ila.csv").write_text("Sample in Buffer,x\n0,1\n")
    (tmp_path / "v.jsonl").write_text("")
    assert ic.main(["--csv", str(tmp_path / "ila.csv"), "--verdicts", str(tmp_path / "v.jsonl")]) == 2
    with pytest.raises(ic.CheckError):
        ic.read_ila_csv(tmp_path / "ila.csv", ic.DEFAULTS["columns"])


def test_swap_needs_raw_m(tmp_path):
    """The documented limit of header-only nonpass streams (review case C): a VIOLATION on a clean
    rise plus a PASS on the broken one looks right without m. With the raw firmware variant
    (i2c_setup_referee_raw.s) the exact m pins the VIOLATION to the clean rise, and it FAILS."""
    recs = [stats(2, 1, mn=2, mx=36), {"type": "drain", "slots": [hdr(2, m3=3)]}]
    ok, _ = run(tmp_path, [CLEAN, BAD, CLEAN], recs)
    assert ok  # the limit, kept visible
    recs = [stats(2, 1, mn=2, mx=36), {"type": "drain", "slots": [hdr(2, m3=3, raw=1), 36]}]
    ok, text = run(tmp_path, [CLEAN, BAD, CLEAN], recs)
    assert not ok and "fits no judged edge" in text
