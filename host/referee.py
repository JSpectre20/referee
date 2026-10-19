#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Command-line tool for the Referee UART console.

    python host/referee.py --port /dev/ttyACM0 run test/transcripts/*.txt
    python host/referee.py --port /dev/ttyACM0 r 00
    python host/referee.py --port /dev/ttyACM0 w 02 a5

`--port` can also be the pseudo-terminal printed by `make pty` in test/,
which connects this tool to the simulated chip.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from referee_console import ConsoleError, SerialConsole, parse_transcript  # noqa: E402


def run_transcripts(con, paths):
    failures = 0
    for path in paths:
        steps = parse_transcript(path)
        bad = 0
        for step in steps:
            try:
                got = con.cmd(step.line)
            except ConsoleError as e:
                print(f"  {step.where}: {step.line!r}: {e}")
                bad += 1
                con.sync()
                continue
            if got != step.expect:
                print(f"  {step.where}: {step.line!r}: expected {step.expect!r}, got {got!r}")
                bad += 1
        print(f"{'FAIL' if bad else 'PASS'}  {path}  ({len(steps) - bad}/{len(steps)} steps)")
        failures += bad
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="serial device, e.g. /dev/ttyACM0 or /dev/pts/5")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=2.0, help="seconds to wait for each reply")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("run", help="replay transcript files")
    p.add_argument("files", nargs="+")
    p = sub.add_parser("r", help="read a register")
    p.add_argument("addr")
    p = sub.add_parser("w", help="write a register")
    p.add_argument("addr")
    p.add_argument("data")
    args = ap.parse_args()

    con = SerialConsole(args.port, args.baud, args.timeout)
    try:
        con.sync()
        if args.action == "run":
            sys.exit(1 if run_transcripts(con, args.files) else 0)
        elif args.action == "r":
            print(f"{con.read(int(args.addr, 16)):02x}")
        else:
            con.write(int(args.addr, 16), int(args.data, 16))
            print("ok")
    except ConsoleError as e:
        sys.exit(f"error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
