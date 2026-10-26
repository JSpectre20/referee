#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Host side of Referee's UART command port (src/host_port.v).

Packets are 4 bytes, CMD A B C; every packet gets a 2-byte reply, STATUS DATA.
The packet builders here are shared by the cocotb tests (test/test.py) and
the command-line tool, so the two can't drift apart.

    python3 tools/referee_host.py --port /dev/ttyUSB0 id
    python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/seq/uart_tx.s --sr 0x41 --oe 0x01 --run
    python3 tools/referee_host.py --port /dev/ttyUSB0 status
    python3 tools/referee_host.py --port /dev/ttyUSB0 halt

The chip's UART is 115200 8N1 at the 40 MHz project clock (ui_in[3] RX,
uo_out[4] TX).
"""

import argparse
import sys
from pathlib import Path

ACK, NAK, ID = 0x06, 0x15, 0x52
BAUD = 115200
PROG_WORDS = 32


class HostError(Exception):
    pass


# ----------------------------------------------------------------------------
# Packets
# ----------------------------------------------------------------------------


def pkt_id():
    return bytes([0x49, 0, 0, 0])


def pkt_write(addr, word):
    return bytes([0x57, addr & 0xFF, (word >> 8) & 0xFF, word & 0xFF])


def pkt_sr(byte):
    return bytes([0x53, byte & 0xFF, 0, 0])


def pkt_run(on):
    return bytes([0x52, 1 if on else 0, 0, 0])


def pkt_oe(mask):
    return bytes([0x4F, mask & 0xFF, 0, 0])


def pkt_status():
    return bytes([0x51, 0, 0, 0])


def decode_status(data):
    """DATA byte of a 'Q' reply -> dict."""
    return {"running": bool(data & 0x80), "overrun": bool(data & 0x40), "pc": data & 0x1F}


def load_packets(words, sr=None, oe=None, run=False):
    """Packets that halt, load a program (padded with `jump 0`), and optionally start it."""
    if len(words) > PROG_WORDS:
        raise HostError(f"program has {len(words)} words; seq holds {PROG_WORDS}")
    jump0 = 0xC000
    padded = list(words) + [jump0] * (PROG_WORDS - len(words))
    pkts = [pkt_run(False)] + [pkt_write(a, w) for a, w in enumerate(padded)]
    if sr is not None:
        pkts.append(pkt_sr(sr))
    if oe is not None:
        pkts.append(pkt_oe(oe))
    if run:
        pkts.append(pkt_run(True))
    return pkts


# ----------------------------------------------------------------------------
# Serial transport (real hardware)
# ----------------------------------------------------------------------------


class Referee:
    def __init__(self, port, baud=BAUD, timeout=0.5):
        import serial  # pyserial; only needed for real hardware

        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.ser.reset_input_buffer()

    def transact(self, pkt):
        self.ser.write(pkt)
        reply = self.ser.read(2)
        if len(reply) != 2:
            raise HostError(f"no reply to {pkt.hex()} (got {reply.hex() or 'nothing'})")
        status, data = reply
        if status not in (ACK, NAK):
            raise HostError(f"bad reply {reply.hex()} to {pkt.hex()}")
        return status == ACK, data

    def must(self, pkt):
        ok, data = self.transact(pkt)
        if not ok:
            raise HostError(f"NAK for {pkt.hex()}")
        return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("id")
    sub.add_parser("status")
    sub.add_parser("halt")
    sub.add_parser("run")
    ld = sub.add_parser("load", help="assemble and load a seq program")
    ld.add_argument("source")
    ld.add_argument("--sr", type=lambda s: int(s, 0), help="shift-register byte")
    ld.add_argument("--oe", type=lambda s: int(s, 0), help="uio output-enable mask")
    ld.add_argument("--run", action="store_true")
    args = ap.parse_args(argv)

    ref = Referee(args.port)
    try:
        if args.cmd == "id":
            data = ref.must(pkt_id())
            print("Referee" if data == ID else f"unexpected id {data:#04x}")
        elif args.cmd == "status":
            print(decode_status(ref.must(pkt_status())))
        elif args.cmd == "halt":
            ref.must(pkt_run(False))
        elif args.cmd == "run":
            ref.must(pkt_run(True))
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import seqasm

            words = seqasm.assemble(Path(args.source).read_text())
            for p in load_packets(words, args.sr, args.oe, args.run):
                ref.must(p)
            print(f"loaded {len(words)} words" + (", running" if args.run else ""))
    except HostError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
