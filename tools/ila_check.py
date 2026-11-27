#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check the chip's verdicts against an ILA capture of the same pins (fpga/README.md, "ILA").

The FPGA build samples the judged pads with a Xilinx ILA at 240 MHz (4 x the chip clock), from
the board's own oscillator. This tool reconstructs every judged interval from the ILA samples,
with the ILA's own uncertainty, and checks each chip verdict against it:

  PASS           CONTRADICTED if the ILA shows the bound certainly broken,
                 confirmed if the ILA shows it certainly met (interval minus the ILA's
                 uncertainty meets the bound), unresolved otherwise (the ILA's own band);
  VIOLATION      the same the other way round;
  CAN'T TELL     always consistent;
  raw m (or the header's saturated m)  CONTRADICTED if the ILA interval is certainly outside
                 the chip's measurement claim (m - 2, m + 2) cycles.

The summary ends PASSED or FAILED. FAILED: any CONTRADICTED verdict; a count that does not
match (ILA rises vs the chip's stats vs the drained stream); an incomplete verdict stream
(VERDICT_OVF); no order-preserving alignment of the stream to the rises; INCONCLUSIVE (no PASS
or VIOLATION confirmed, or more unresolved verdicts than run.json's max_unresolved, default 0);
clocks that disagree with the build's ila_info.txt or with the trace itself (self-calibration
on the chip-side probes); a firmware CRC or bitstream that differs from the run's record; or
nothing to check. --strict also fails a missing ila_info.txt, too little data to
self-calibrate, and an unflagged CAN'T TELL the chip's own rule should have decided.

    python3 tools/ila_check.py fpga/runs/2026-12-01_zedboard_400k          # a run folder
    python3 tools/ila_check.py --all fpga/runs                              # every run folder
    python3 tools/ila_check.py --csv ila.csv --verdicts host.log --bound-ns 100

A run folder holds run.json (parameters, see fpga/runs/README.md), the ILA CSV
(fpga/ila_capture.tcl) and the verdict log (tools/referee_host.py output, or JSON lines).

Timing model of the ILA (all in ns, T = one ILA sample period):
  an edge stored in sample s happened in [(s - 1) T - a, s T + a] at its sampling flop
  (a = aperture_ns, the flop's setup / hold window); the two channels' pad-to-flop delays
  differ by at most skew_ns; so an interval of D samples is in
  [(D - 1) T - 2a - skew, (D + 1) T + 2a + skew], widened by ppm (both clocks' tolerance).
When it is not certain which reference edge was the latest before the trigger edge (they are
within that uncertainty of each other), the hull of all candidates is used. Pure Python.
"""

import argparse
import ast
import bisect
import csv
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import referee_host as host  # noqa: E402

OK, UNRES, BAD = "confirmed", "unresolved", "CONTRADICTED"
U_CYCLES = 2  # the chip's proved measurement error bound: |m - true| < 2 cycles

DEFAULTS = {
    # ila_clock_hz has no default: it must be stated (run.json or --ila-clock-hz) and is
    # cross-checked against ila_info.txt and against the trace itself (self-calibration).
    "chip_clock_hz": host.CLOCK_HZ,
    "skew_ns": 2.0,
    "aperture_ns": 0.25,   # the sampling flop's window plus MMCM jitter and clock skew, per edge
    "ppm": 100.0,
    "max_unresolved": 0,   # verdicts the ILA can neither confirm nor refute; more fail the run
    "push": "nonpass",
    "ts_bits": 40,
    "rule": {
        "name": "tSU;DAT", "trigger": "scl", "trigger_edge": "rise", "ref": "sda",
        "ref_edge": "last", "kind": "min", "bound_ns": 100.0, "slot": 0,
    },
    "columns": {"scl": "ila_scl", "sda": "ila_sda", "scl_chip": "ila_scl_chip",
                "sda_chip": "ila_sda_chip", "uo_out": "ila_uo_out", "uio_oe": "ila_uio_oe",
                "rst_n": "ila_rst_n", "mark": "ila_mark", "ts": "ila_ts"},
    "files": {"csv": "ila.csv", "verdicts": None},
    "expect": "PASSED",
}


class CheckError(Exception):
    pass


# ----------------------------------------------------------------------------
# The ILA CSV (Vivado write_hw_ila_data -csv_file)
# ----------------------------------------------------------------------------


def _base(name):
    """'u_top/ila_ts[39:0]' -> 'ila_ts'."""
    return re.sub(r"\[.*\]$", "", name.strip().split("/")[-1]).strip()


def _width(name):
    m = re.search(r"\[(\d+):(\d+)\]\s*$", name.strip())
    return abs(int(m.group(1)) - int(m.group(2))) + 1 if m else 1


def _value(text, radix):
    t = text.strip().replace("_", "")
    r = (radix or "").upper()
    if r.startswith("HEX"):
        return int(t, 16)
    if r.startswith("BIN"):
        return int(t, 2)
    if r.startswith("OCT"):
        return int(t, 8)
    try:
        return int(t)
    except ValueError:
        return int(t, 16)


def find_column(header, want):
    bases = [_base(h) for h in header]
    if want in bases:
        return bases.index(want)
    hits = [i for i, b in enumerate(bases) if b.endswith(want)]
    if len(hits) == 1:
        return hits[0]
    return None


def read_ila_csv(path, columns, ts_bits=None):
    """-> dict: 'ts' (unwrapped sample numbers), one list per found column, 'trigger_row'."""
    with open(path, newline="") as fh:
        rows = [r for r in csv.reader(fh) if r and any(c.strip() for c in r)]
    if len(rows) < 2:
        raise CheckError(f"{path}: no samples")
    header = [h.strip() for h in rows[0]]
    radix = [None] * len(header)
    first = 1
    if rows[1][0].strip().lower().startswith("radix"):
        r = rows[1]
        radix = [r[0].split("-", 1)[1].strip() if "-" in r[0] else None] + [x.strip() for x in r[1:]]
        first = 2
    idx = {k: find_column(header, v) for k, v in columns.items()}
    if idx.get("ts") is None:
        raise CheckError(f"{path}: no timestamp column {columns.get('ts')!r} (have {header})")
    tcol = find_column(header, "TRIGGER")
    bits = ts_bits or _width(header[idx["ts"]])
    if bits <= 1:
        bits = 40
    out = {k: [] for k, i in idx.items() if i is not None}
    out["trigger_row"] = None
    wrap, last = 0, None
    for n, r in enumerate(rows[first:]):
        for k, i in idx.items():
            if i is None:
                continue
            v = _value(r[i], radix[i])
            if k == "ts":
                if last is not None and v < last:
                    wrap += 1 << bits
                last = v
                v += wrap
            out[k].append(v)
        if tcol is not None and out["trigger_row"] is None and r[tcol].strip() not in ("", "0"):
            out["trigger_row"] = n
    ts = out["ts"]
    if any(b <= a for a, b in zip(ts, ts[1:])):
        raise CheckError(f"{path}: sample numbers are not increasing")
    return out


def edges(levels, ts):
    """[(sample number, new level)] for every change of a 1-bit column."""
    return [(ts[i], levels[i] & 1) for i in range(1, len(levels)) if (levels[i] ^ levels[i - 1]) & 1]


# ----------------------------------------------------------------------------
# The chip's side: tools/referee_host.py output or JSON lines
# ----------------------------------------------------------------------------


def _signed16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _verdict_from_header(h, m):
    v = dict(h)
    v["m"] = m
    return v


def read_verdict_log(path):
    """-> {'verdicts': [dict in stream order], 'stats': [dict], 'status': [dict], 'ignored': n}.

    Accepted lines (in any mix, in the order they happened):
      JSON {"type": "verdict", "verdict": "PASS", "b": 0, "m": 7, ...}   one decoded verdict
      JSON {"type": "drain", "slots": [h, (m,) ...]}                     raw drained slots
      JSON {"type": "stats", "slot": 0, "pass": .., "viol": .., "indet": .., "min": .., "max": ..}
      JSON {"type": "status", "data": <byte>}  or the decoded dict
      the lines `referee_host.py drain`, `stats <slot>` and `status` print (Python dicts).
    """
    out = {"verdicts": [], "stats": [], "status": [], "ignored": 0}
    for line in Path(path).read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        rec = None
        if s.startswith("{"):
            try:
                rec = json.loads(s)
            except json.JSONDecodeError:
                m = re.match(r"^(\{.*\})\s*(?:m = (-?\d+))?\s*$", s)
                if m:
                    try:
                        d = ast.literal_eval(m.group(1))
                    except (ValueError, SyntaxError):
                        d = None
                    if isinstance(d, dict) and "verdict" in d:
                        rec = {"type": "verdict", **d, "m": int(m.group(2)) if m.group(2) else None}
                    elif isinstance(d, dict) and "pass" in d:
                        rec = {"type": "stats", **d}
                    elif isinstance(d, dict) and "verdict_ovf" in d:
                        rec = {"type": "status", **d}
        if rec is None:
            out["ignored"] += 1
            continue
        t = rec.get("type")
        if t is None:
            t = "verdict" if "verdict" in rec else "stats" if "pass" in rec else "status"
        if t == "drain":
            for h, m in host.split_verdicts([int(x) & 0xFFFF for x in rec["slots"]]):
                out["verdicts"].append(_verdict_from_header(h, m))
        elif t == "verdict":
            out["verdicts"].append(rec)
        elif t == "stats":
            out["stats"].append(rec)
        elif t == "status":
            if "data" in rec and "verdict_ovf" not in rec:
                rec = {**rec, **host.decode_status(int(rec["data"]))}
            out["status"].append(rec)
        else:
            out["ignored"] += 1
    return out


def m_range(v):
    """The chip's measured m as an inclusive range in cycles, or None (not usable)."""
    if any(int(v.get(f, 0)) for f in ("FX", "FY", "TAG", "ORD")):
        return None  # invalid inputs: m means nothing
    if v.get("m") is not None:
        return (int(v["m"]), int(v["m"]))
    if "m3" in v:
        m3 = int(v["m3"])
        if m3 <= -4:
            return (-math.inf, -4)
        if m3 >= 3:
            return (3, math.inf)
        return (m3, m3)
    return None


# ----------------------------------------------------------------------------
# Intervals and claims
# ----------------------------------------------------------------------------


class Params:
    def __init__(self, cfg):
        if not cfg.get("ila_clock_hz"):
            raise CheckError("ila_clock_hz is required (run.json or --ila-clock-hz; ila_info.txt has it)")
        self.T = 1e9 / float(cfg["ila_clock_hz"])
        self.Tc = 1e9 / float(cfg["chip_clock_hz"])
        self.margin = 2 * float(cfg["aperture_ns"]) + float(cfg["skew_ns"])
        self.ppm = float(cfg["ppm"]) * 1e-6
        r = cfg["rule"]
        self.kind = r["kind"]
        self.bound = float(r["bound_ns"])
        if self.kind not in ("min", "max"):
            raise CheckError(f"rule kind must be min or max, not {self.kind!r}")


def _widen(lo, hi, p):
    if math.isfinite(lo):
        lo -= abs(lo) * p.ppm
    if math.isfinite(hi):
        hi += abs(hi) * p.ppm
    return lo, hi


def judged_intervals(trig_edges, ref_edges, s0, p):
    """For each trigger edge: (sample, nominal ns or None, lo, hi, note)."""
    T, mg = p.T, p.margin
    out = []
    for st in trig_edges:
        # maybe before st: (se - st - 1) T < mg;  surely before st: (st - se - 1) T > mg
        n_possible = bisect.bisect_left(ref_edges, st + 1 + mg / T)
        n_certain = bisect.bisect_left(ref_edges, st - 1 - mg / T)
        certain = n_certain > 0
        note = ""
        if certain:
            cands = ref_edges[n_certain - 1:n_possible]
        else:
            cands = ref_edges[:n_possible]
            note = "no reference edge surely before it in the trace"
        rng = [((st - se - 1) * T - mg, (st - se + 1) * T + mg) for se in cands]
        if not certain:
            rng.append(((st - s0 - 1) * T - mg, math.inf))  # the last edge was before the trace
        lo, hi = _widen(min(r[0] for r in rng), max(r[1] for r in rng), p)
        nominal = (st - cands[-1]) * T if cands else None
        if len(cands) > 1:
            note = f"{len(cands)} candidate reference edges"
        out.append({"s": st, "nominal": nominal, "lo": lo, "hi": hi, "note": note})
    return out


def decidable(iv, p):
    """For an unflagged CAN'T TELL: 'PASS' / 'VIOLATION' if the ILA shows the true interval so far
    from the bound that the chip's rule must have decided (docs/isa-timing.md section 6: outside
    bound +- (2u + 1/16) cycles), else None."""
    band = (2 * U_CYCLES + 1 / 16) * p.Tc
    lo, hi, b = iv["lo"], iv["hi"], p.bound
    if p.kind == "min":
        return "PASS" if lo >= b + band else "VIOLATION" if hi <= b - band else None
    return "PASS" if hi <= b - band else "VIOLATION" if lo >= b + band else None


def flagged(v):
    return any(int(v.get(f, 0)) for f in ("FX", "FY", "TAG", "ORD"))


def calibrate(ila, p):
    """Self-calibration of the clock ratio: the chip-side probes (the chip's judge-channel
    registers, its outputs) change only on clk60 edges, so on one probe the gap between two
    changes is k * (f_ila / f_chip) samples, +-1 (sampling) for some integer k. Short gaps only
    (the two oscillators drift apart by up to ppm). -> (checked gaps, misfits, worst residual)."""
    r = p.Tc / p.T
    ts = ila["ts"]
    n = bad = 0
    worst_res = 0.0
    for c in ("scl_chip", "sda_chip", "uo_out", "uio_oe", "rst_n"):
        if c not in ila:
            continue
        ev = [ts[i] for i in range(1, len(ts)) if ila[c][i] != ila[c][i - 1]]
        for a, b in zip(ev, ev[1:]):
            g = b - a
            if g > 4000:
                continue
            res = abs(g - round(g / r) * r)
            n += 1
            worst_res = max(worst_res, res)
            if res > 1.25 + g * p.ppm:
                bad += 1
    return n, bad, worst_res


def read_ila_info(path):
    """fpga/build/<board>/ila_info.txt (key value lines) -> dict of strings."""
    out = {}
    for line in Path(path).read_text().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            k, v = parts
            if k.startswith("pad_to_flop"):
                k = k + "_" + v.split()[0]
                v = v.split()[-1]
            out[k] = v.strip()
    return out


def claim(label, iv, p):
    """Does the ILA interval support the chip's label? -> confirmed / unresolved / CONTRADICTED."""
    lo, hi, b = iv["lo"], iv["hi"], p.bound
    if label == "INDETERMINATE":
        return OK
    if label not in ("PASS", "VIOLATION"):
        return BAD
    meets = (lo >= b, hi < b) if p.kind == "min" else (hi <= b, lo > b)   # (surely met, surely broken)
    want_met = label == "PASS"
    if (meets[0] if want_met else meets[1]):
        return OK
    if (meets[1] if want_met else meets[0]):
        return BAD
    return UNRES


def m_claim(rng, iv, p):
    """The chip's measurement claim: true interval in ((m_lo - 2) Tc, (m_hi + 2) Tc)."""
    if rng is None:
        return None
    a, b = (rng[0] - U_CYCLES) * p.Tc, (rng[1] + U_CYCLES) * p.Tc
    if iv["hi"] <= a or iv["lo"] >= b:
        return BAD
    if iv["lo"] > a and iv["hi"] < b:
        return OK
    return UNRES


def row_outcome(v, iv, p, implicit):
    """One verdict against one interval -> (outcome, label shown, m text)."""
    lab = v.get("verdict", "?")
    c = claim(lab, iv, p)
    mr = None if implicit else m_range(v)
    mc = m_claim(mr, iv, p)
    out = worst(c, BAD if mc == BAD else None)
    mtxt = "" if mr is None else (str(mr[0]) if mr[0] == mr[1] else f"{mr[0]}..{mr[1]}")
    if mc == BAD:
        lab += "*"
    if out == OK and lab.startswith("INDETERMINATE"):
        out = "consistent"  # CAN'T TELL: never wrong
    return out, lab, mtxt


def worst(*outs):
    outs = [o for o in outs if o is not None]
    return BAD if BAD in outs else UNRES if UNRES in outs else OK


# ----------------------------------------------------------------------------
# Alignment of a non-PASS stream (push nonpass) to the rises
# ----------------------------------------------------------------------------

CAP = 1000


def align(can_pass, allowed):
    """Order-preserving matches of K stream verdicts to N rises, every unmatched rise a PASS.
    -> (number of ways, capped at CAP; one assignment {verdict j: rise i} or None)."""
    n, k = len(can_pass), len(allowed)
    f = [[0] * (k + 1) for _ in range(n + 1)]
    f[0][0] = 1
    for i in range(1, n + 1):
        for j in range(0, min(i, k) + 1):
            w = f[i - 1][j] if can_pass[i - 1] else 0
            if j and allowed[j - 1][i - 1]:
                w += f[i - 1][j - 1]
            f[i][j] = min(w, CAP)
    ways = f[n][k]
    if not ways:
        return 0, None
    asg, i, j = {}, n, k
    while i > 0:
        if j and allowed[j - 1][i - 1] and f[i - 1][j - 1]:
            asg[j - 1] = i - 1
            j -= 1
        i -= 1
    return ways, asg


def alignment_options(can_pass, allowed):
    """For each rise: the set of what it can be over ALL consistent alignments
    (None = an implicit PASS, j = stream verdict j)."""
    n, k = len(can_pass), len(allowed)
    fw = [[False] * (k + 1) for _ in range(n + 1)]   # first i rises can take the first j verdicts
    fw[0][0] = True
    for i in range(1, n + 1):
        for j in range(0, k + 1):
            fw[i][j] = (fw[i - 1][j] and can_pass[i - 1]) or (j > 0 and fw[i - 1][j - 1] and allowed[j - 1][i - 1])
    bw = [[False] * (k + 1) for _ in range(n + 2)]   # rises i.. can take verdicts j..
    bw[n][k] = True
    for i in range(n - 1, -1, -1):
        for j in range(k, -1, -1):
            bw[i][j] = (bw[i + 1][j] and can_pass[i]) or (j < k and bw[i + 1][j + 1] and allowed[j][i])
    opts = []
    for i in range(n):
        o = set()
        for j in range(k + 1):
            if fw[i][j] and can_pass[i] and bw[i + 1][j]:
                o.add(None)
            if j < k and fw[i][j] and allowed[j][i] and bw[i + 1][j + 1]:
                o.add(j)
        opts.append(sorted(o, key=lambda x: -1 if x is None else x))
    return opts


# ----------------------------------------------------------------------------
# The check
# ----------------------------------------------------------------------------


def merge_cfg(base, over):
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def check(cfg, csv_path, log_path, strict=False, brief=False, emit=print):
    """Run the check; print the report through emit; return True for PASSED."""
    p = Params(cfg)
    r = cfg["rule"]
    cols = dict(cfg["columns"])
    trig_name, ref_name = r["trigger"], r["ref"]
    cols.setdefault(trig_name, trig_name)
    cols.setdefault(ref_name, ref_name)
    ila = read_ila_csv(csv_path, cols, cfg.get("ts_bits"))
    log = read_verdict_log(log_path)
    for name in (trig_name, ref_name):
        if name not in ila:
            raise CheckError(f"{csv_path}: no column for {name!r} ({cols[name]!r})")
    ts = ila["ts"]
    s0 = ts[0]
    te = [s for s, lv in edges(ila[trig_name], ts) if lv == (1 if r["trigger_edge"] == "rise" else 0)]
    want_ref = {"rise": (1,), "fall": (0,), "last": (0, 1)}[r["ref_edge"]]
    re_ = [s for s, lv in edges(ila[ref_name], ts) if lv in want_ref]
    ivs = judged_intervals(te, re_, s0, p)
    slot = int(r.get("slot", 0))
    stream = [v for v in log["verdicts"] if int(v.get("b", slot)) == slot]
    stats = [s for s in log["stats"] if int(s.get("slot", slot)) == slot]
    st = stats[-1] if stats else None
    problems, warnings = [], []
    if st is not None and "slot" not in st:
        warnings.append(f"the stats record names no slot (older referee_host output): taken as slot {slot}")

    emit(f"ILA check: {csv_path}")
    emit(f"  ILA      {float(cfg['ila_clock_hz']) / 1e6:.3f} MHz (T = {p.T:.3f} ns), {len(ts)} stored samples "
         f"spanning {(ts[-1] - s0) * p.T / 1e3:.1f} us; per-interval uncertainty +-({p.T:.3f} + "
         f"{p.margin:.2f}) ns, {cfg['ppm']} ppm")
    emit(f"  rule     {r['name']}: {r['kind']} {p.bound:g} ns, {ref_name} {r['ref_edge']} edge -> "
         f"{trig_name} {r['trigger_edge']} (slot {slot})")
    emit(f"  chip     clock {float(cfg['chip_clock_hz']) / 1e6:.3f} MHz, push {cfg['push']}, "
         f"{len(stream)} verdicts in the stream" + (f", stats {_fmt_stats(st)}" if st else ", no stats"))
    emit(f"  trace    {len(te)} {trig_name} {r['trigger_edge']}s judged")

    # The clocks: stated, recorded at build time (ila_info.txt) and seen in the trace
    info = cfg.get("_ila_info")
    if info is None:
        (problems if strict else warnings).append(
            "no ila_info.txt in the run folder: the ILA clock and skew are not cross-checked against the build")
    else:
        for key in ("ila_clock_hz", "chip_clock_hz"):
            if key in info and abs(float(info[key]) - float(cfg[key])) > 1e-6 * float(info[key]):
                problems.append(f"{key} {cfg[key]} differs from the build's ila_info.txt ({info[key]})")
        sk = info.get("skew_ns", "?")
        if sk == "?":
            problems.append("ila_info.txt has no measured skew_ns: the pad-to-sampling-flop skew is unbounded")
        elif float(cfg["skew_ns"]) < float(sk):
            problems.append(f"skew_ns {cfg['skew_ns']} is below the build's measured {sk} ns")
        for key in ("bitstream_sha256", "build_args"):
            if key in info and key in cfg and info[key] not in ("?", "") and str(cfg[key]) != info[key]:
                problems.append(f"{key} in run.json ({cfg[key]}) differs from ila_info.txt ({info[key]})")
    n_gaps, misfit, worst_res = calibrate(ila, p)
    if n_gaps < 6:
        (problems if strict else warnings).append(
            f"only {n_gaps} chip-side gaps in the trace: the clock ratio is not self-calibrated")
    elif misfit > n_gaps // 100:
        problems.append(f"the trace disagrees with the stated clocks: {misfit} of {n_gaps} chip-side gaps are "
                        f"not a whole number of chip cycles at {float(cfg['ila_clock_hz']) / 1e6:g} / "
                        f"{float(cfg['chip_clock_hz']) / 1e6:g} MHz (worst {worst_res:.2f} samples)")
    else:
        emit(f"  clocks   self-calibration: {n_gaps} chip-side gaps fit f_ila / f_chip = {p.Tc / p.T:.4f} "
             f"(worst {worst_res:.2f} samples)")
    if cfg.get("firmware") and cfg.get("image_crc16") is not None:
        try:
            img, _ = host.assemble((Path(__file__).resolve().parent.parent / cfg["firmware"]).read_text(),
                                   clock_hz=float(cfg["chip_clock_hz"]))
            crc = host.refasm.crc16(img)
            want = int(str(cfg["image_crc16"]), 16)
            if crc != want:
                problems.append(f"{cfg['firmware']} assembles to CRC {crc:04x}, the run loaded {want:04x}")
        except (OSError, host.refasm.AsmError, ValueError) as e:
            warnings.append(f"could not re-assemble {cfg['firmware']}: {e}")

    # Envelope notes: pulses shorter than 2 chip cycles, the chip channel's own count
    runt_total = 0
    for name in (trig_name, ref_name):
        e = edges(ila[name], ts)
        runts = sum(1 for a, b in zip(e, e[1:]) if (b[0] - a[0]) * p.T < 2 * p.Tc)
        runt_total += runts
        if runts:
            warnings.append(f"{runts} {name} pulses shorter than 2 chip cycles: OUTSIDE the declared envelope")
    chip_col = f"{trig_name}_chip"
    if chip_col in ila:
        n_chip = sum(1 for _, lv in edges(ila[chip_col], ts) if lv == (1 if r["trigger_edge"] == "rise" else 0))
        if n_chip != len(te):
            warnings.append(f"the chip's own {trig_name} channel shows {n_chip} {r['trigger_edge']}s, the pad {len(te)}")

    # Stream completeness and counts
    if any(s.get("verdict_ovf") for s in log["status"]):
        problems.append("VERDICT_OVF was set: the verdict FIFO dropped verdicts, the stream is incomplete")
    if any(v.get("verdict") not in ("PASS", "VIOLATION", "INDETERMINATE") for v in stream):
        problems.append("a verdict with an invalid code (3) in the stream")
    if any(int(v.get("L", 0)) for v in stream):
        warnings.append("a verdict has L = 1 (a trigger was lost): see the counts")
    if st:
        sat = [k for k in ("pass", "viol", "indet") if int(st[k]) >= 0xFFFF]
        if sat:
            problems.append(f"stats {sat} saturated at 65535: counts cannot be checked (reload to clear)")
        total = int(st["pass"]) + int(st["viol"]) + int(st["indet"])
        if total != len(te):
            why = ("runts on the bus: OUTSIDE the declared envelope" if runt_total
                   else "the window missed part of the run, or lost triggers")
            problems.append(f"stats count {total} judgments, the ILA shows {len(te)} {trig_name} "
                            f"{r['trigger_edge']}s ({why})")
    cnt = {lab: sum(1 for v in stream if v.get("verdict") == lab) for lab in ("PASS", "VIOLATION", "INDETERMINATE")}

    rows, counts = [], {OK: 0, UNRES: 0, BAD: 0, "consistent": 0}
    incomplete, decided = [], {True: 0, False: 0}
    alignment = ""
    if cfg["push"] == "all":
        if len(stream) != len(te):
            problems.append(f"{len(stream)} verdicts in the stream, {len(te)} judged edges in the trace")
        if st and (cnt["PASS"], cnt["VIOLATION"], cnt["INDETERMINATE"]) != (
                int(st["pass"]), int(st["viol"]), int(st["indet"])):
            problems.append("the stream's PASS / VIOLATION / CAN'T TELL counts differ from stats")
        for i, iv in enumerate(ivs):
            v = stream[i] if i < len(stream) else None
            rows.append((i, iv, v, False))
        alignment = "positional (push all: verdict k is judged edge k)"
    elif cfg["push"] == "nonpass":
        if cnt["PASS"]:
            problems.append(f"{cnt['PASS']} PASS verdicts in a push-nonpass stream: wrong 'push' in run.json?")
        if not st:
            problems.append("push nonpass needs the stats record (stats <slot>) to know the PASS count")
        elif (cnt["VIOLATION"], cnt["INDETERMINATE"]) != (int(st["viol"]), int(st["indet"])):
            problems.append(f"the stream has {cnt['VIOLATION']} VIOLATION / {cnt['INDETERMINATE']} CAN'T TELL, "
                            f"stats {st['viol']} / {st['indet']}: stream incomplete (drain until empty)")
        nonpass = [v for v in stream if v.get("verdict") != "PASS"]
        can_pass = [claim("PASS", iv, p) != BAD and (not strict or claim("PASS", iv, p) == OK) for iv in ivs]
        allowed = []
        for v in nonpass:
            row = []
            for iv in ivs:
                c = claim(v.get("verdict"), iv, p)
                mc = m_claim(m_range(v), iv, p)
                row.append(c != BAD and mc != BAD and (not strict or c == OK))
            allowed.append(row)
        ways, asg = align(can_pass, allowed)
        options = alignment_options(can_pass, allowed) if ways > 1 else None
        if not ways:
            problems.append("no order-preserving alignment of the stream to the judged edges is consistent "
                            "with the ILA (a wrong verdict)")
            why = [f"  stream verdict {j} ({v.get('verdict')}) fits no judged edge in the trace"
                   for j, v in enumerate(nonpass) if not any(allowed[j])]
            why += [f"  edge {i}: the ILA says the bound is broken; a PASS there is wrong"
                    for i, ok in enumerate(can_pass) if not ok]
            problems += why[:20] + ([f"  ... and {len(why) - 20} more"] if len(why) > 20 else [])
            asg = None
            alignment = "none found (the table shows the ILA's view of each judged edge only)"
        else:
            alignment = ("unique" if ways == 1 else f"{ways if ways < CAP else f'>= {CAP}'} possible") + \
                " (push nonpass: order and m only; see fpga/runs/README.md on its limits)"
        by_rise = {i: nonpass[j] for j, i in (asg or {}).items()}
        for i, iv in enumerate(ivs):
            if asg is None:
                rows.append((i, iv, {"verdict": "?", "unaligned": True}, False))
            elif options is not None and len(options[i]) > 1:
                # this edge gets different verdicts under different alignments: show them all,
                # judge it by the worst
                alts = [({"verdict": "PASS"}, True) if j is None else (nonpass[j], False) for j in options[i]]
                rows.append((i, iv, {"verdict": "|".join(sorted({a[0]["verdict"] for a in alts})),
                                     "alts": alts}, False))
            else:
                rows.append((i, iv, by_rise.get(i, {"verdict": "PASS"}), i not in by_rise))
    else:
        raise CheckError(f"push must be 'all' or 'nonpass', not {cfg['push']!r}")

    # Per-verdict table
    emit("")
    emit(f"  {'#':>4} {'t (us)':>10} {'ILA (ns)':>9} {'ILA range (ns)':>21} {'chip':>14} {'m':>5}  check")
    ila_view = {OK: "ILA: met", BAD: "ILA: broken", UNRES: "ILA: in its band"}
    for i, iv, v, implicit in rows:
        if v is not None and v.get("unaligned"):
            met = claim("PASS", iv, p)
            if not (brief and met == OK):
                nominal = "-" if iv["nominal"] is None else f"{iv['nominal']:.1f}"
                hi = "inf" if not math.isfinite(iv["hi"]) else f"{iv['hi']:.1f}"
                emit(f"  {i:>4} {(iv['s'] - s0) * p.T / 1e3:>10.3f} {nominal:>9} [{iv['lo']:>8.1f}, {hi:>8}] "
                     f"{'(unaligned)':>14} {'':>5}  {ila_view[met]}")
            continue
        if v is None:
            out, lab, mtxt = BAD, "(missing)", ""
        elif "alts" in v:
            # which verdict the chip gave here is not known: confirmed only if every option is
            # (every option is non-contradicted by construction), else unresolved
            outs = {row_outcome(a, iv, p, imp)[0] for a, imp in v["alts"]}
            out = outs.pop() if len(outs) == 1 else UNRES
            lab, mtxt = v["verdict"], ""
        else:
            out, lab, mtxt = row_outcome(v, iv, p, implicit)
            if v.get("verdict") == "INDETERMINATE" and not flagged(v):
                should = decidable(iv, p)
                if should:
                    incomplete.append(f"edge {i}: unflagged CAN'T TELL where the ILA shows the chip's rule "
                                      f"must have said {should}")
        if lab.startswith("PASS") or lab.startswith("VIOLATION"):
            decided[out == OK] += 1
        counts[out] += 1
        nominal = "-" if iv["nominal"] is None else f"{iv['nominal']:.1f}"
        hi = "inf" if not math.isfinite(iv["hi"]) else f"{iv['hi']:.1f}"
        if brief and out == OK:
            continue
        shown = ("PASS (implicit)" if implicit else lab).replace("INDETERMINATE", "CAN'T TELL")
        emit(f"  {i:>4} {(iv['s'] - s0) * p.T / 1e3:>10.3f} {nominal:>9} [{iv['lo']:>8.1f}, {hi:>8}] "
             f"{shown:>14} {mtxt:>5}  {out}{'  ' + iv['note'] if iv['note'] else ''}")
    emit("  (* = the chip's m is outside its own +-2 cycle claim against the ILA)")

    # Stats extremes: some judged edge must fit m_min and m_max (signed 16-bit on the wire)
    if st and int(st["pass"]) + int(st["viol"]) > 0:
        for key in ("min", "max"):
            mv = _signed16(int(st[key]))
            if not any(m_claim((mv, mv), iv, p) != BAD for iv in ivs):
                problems.append(f"stats m_{key} = {mv} cycles fits no judged edge in the trace")

    if counts[BAD]:
        problems.append(f"{counts[BAD]} verdicts CONTRADICTED by the ILA")
    max_unres = 0 if strict else int(cfg.get("max_unresolved", 0))
    if counts[UNRES] > max_unres:
        problems.append(f"{counts[UNRES]} verdicts unresolved by the ILA (allowed: {max_unres}, run.json "
                        f"max_unresolved): INCONCLUSIVE")
    aligned = not (rows and rows[0][2] is not None and rows[0][2].get("unaligned"))
    if ivs and aligned and not decided[True]:
        problems.append("no PASS or VIOLATION confirmed by the ILA: INCONCLUSIVE (nothing was shown right)")
    if incomplete:
        (problems if strict else warnings).append(
            f"{len(incomplete)} CAN'T TELL verdicts where the chip should have decided (completeness, "
            f"not soundness): " + "; ".join(incomplete[:5]) + (" ..." if len(incomplete) > 5 else ""))
    if not ivs:
        problems.append("no judged edge in the trace: nothing was checked")

    emit("")
    for w in warnings:
        emit(f"  note: {w}")
    emit(f"  alignment: {alignment}")
    if rows and rows[0][2] is not None and rows[0][2].get("unaligned"):
        emit(f"SUMMARY: {len(rows)} judged edges, the verdict stream could not be aligned to them")
    else:
        emit(f"SUMMARY: {len(rows)} verdicts checked: {counts[OK]} confirmed, {counts['consistent']} CAN'T TELL "
             f"(always consistent), {counts[UNRES]} unresolved (inside the ILA's own band), {counts[BAD]} contradicted")
    for pr in problems:
        emit(f"  FAIL: {pr}")
    emit("FAILED" if problems else "PASSED")
    return not problems


def _fmt_stats(st):
    return (f"pass {st['pass']} viol {st['viol']} indet {st['indet']} "
            f"min {_signed16(int(st['min']))} max {_signed16(int(st['max']))}")


def load_run(run_dir):
    run_dir = Path(run_dir)
    meta = run_dir / "run.json"
    cfg = merge_cfg(DEFAULTS, json.loads(meta.read_text()) if meta.exists() else {})
    if (run_dir / "ila_info.txt").exists():
        cfg["_ila_info"] = read_ila_info(run_dir / "ila_info.txt")
    files = cfg.get("files") or {}
    csv_path = run_dir / (files.get("csv") or "ila.csv")
    log = files.get("verdicts")
    if log is None:
        for name in ("verdicts.jsonl", "verdicts.txt", "host.log"):
            if (run_dir / name).exists():
                log = name
                break
    if log is None:
        raise CheckError(f"{run_dir}: no verdict log (verdicts.jsonl / verdicts.txt / host.log)")
    return cfg, csv_path, run_dir / log


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run", nargs="?", help="a run folder with run.json, the ILA CSV and the verdict log")
    ap.add_argument("--all", metavar="DIR", help="check every run folder under DIR (each against its 'expect')")
    ap.add_argument("--csv", help="the ILA CSV (overrides the run folder's)")
    ap.add_argument("--verdicts", help="the verdict log (overrides the run folder's)")
    ap.add_argument("--bound-ns", type=float, help="override the rule's bound")
    ap.add_argument("--kind", choices=("min", "max"), help="override the rule's kind")
    ap.add_argument("--push", choices=("all", "nonpass"), help="the firmware's JUDGE push mode")
    ap.add_argument("--ila-clock-hz", type=float)
    ap.add_argument("--chip-clock-hz", type=float)
    ap.add_argument("--skew-ns", type=float)
    ap.add_argument("--aperture-ns", type=float)
    ap.add_argument("--ppm", type=float)
    ap.add_argument("--strict", action="store_true",
                    help="also fail on: any unresolved verdict, an incomplete CAN'T TELL, a missing ila_info.txt, "
                         "too few gaps to self-calibrate")
    ap.add_argument("--brief", action="store_true", help="table rows only for verdicts not confirmed")
    a = ap.parse_args(argv)
    try:
        if a.all:
            bad = 0
            dirs = sorted(d for d in Path(a.all).iterdir() if (d / "run.json").exists())
            if not dirs:
                print(f"no run folders (with run.json) under {a.all}")
                print("NO RUNS: nothing checked")
                return 0
            for d in dirs:
                cfg, c, v = load_run(d)
                lines = []
                got = "PASSED" if check(cfg, c, v, a.strict, True, lines.append) else "FAILED"
                ok = got == cfg.get("expect", "PASSED")
                bad += not ok
                print(f"{'ok  ' if ok else 'BAD '} {d.name}: {got} (expected {cfg.get('expect', 'PASSED')})")
                if not ok:
                    print("\n".join("     " + x for x in lines))
            print("PASSED" if not bad else "FAILED")
            return 0 if not bad else 1
        if a.run:
            cfg, csv_path, log_path = load_run(a.run)
        else:
            if not (a.csv and a.verdicts):
                ap.error("give a run folder, or --csv and --verdicts")
            cfg, csv_path, log_path = merge_cfg(DEFAULTS, {}), None, None
        csv_path = Path(a.csv) if a.csv else csv_path
        log_path = Path(a.verdicts) if a.verdicts else log_path
        over = {"ila_clock_hz": a.ila_clock_hz, "chip_clock_hz": a.chip_clock_hz, "skew_ns": a.skew_ns,
                "aperture_ns": a.aperture_ns, "ppm": a.ppm, "push": a.push}
        cfg.update({k: v for k, v in over.items() if v is not None})
        if a.bound_ns is not None:
            cfg["rule"]["bound_ns"] = a.bound_ns
        if a.kind:
            cfg["rule"]["kind"] = a.kind
        return 0 if check(cfg, csv_path, log_path, a.strict, a.brief) else 1
    except (CheckError, OSError, KeyError, ValueError, TypeError) as e:
        print(f"error: {e}")
        print("FAILED")
        return 2


if __name__ == "__main__":
    sys.exit(main())
