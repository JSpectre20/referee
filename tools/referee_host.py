#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Host side of Referee's UART command port v2 (src/host_port.v, docs/isa-timing.md section 7).

Packets are 4 bytes, CMD A B C; every reply starts STATUS DATA (ACK 0x06 / NAK 0x15).
The packet builders here are shared by the cocotb chip tests (test/test.py) and
the command-line tool, so the two can't drift apart.

    python3 tools/referee_host.py --port /dev/ttyUSB0 id
    python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/uart_tx.s --pin 0:pp --run
    python3 tools/referee_host.py --port /dev/ttyUSB0 status
    python3 tools/referee_host.py --port /dev/ttyUSB0 drain
    python3 tools/referee_host.py --port /dev/ttyUSB0 stats 0
    python3 tools/referee_host.py --port /dev/ttyUSB0 halt

The chip's UART starts at 115200 8N1 (ui_in[3] RX, uo_out[4] TX) at the project clock in
info.yaml; `--fast` switches both ends to clock / 20 first (2 Mbaud at 40 MHz).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refasm  # noqa: E402

ACK, NAK, ID = 0x06, 0x15, 0x52


def _clock_hz():
    """The project clock: info.yaml is the single source (the demo board sets the clock from it)."""
    import re

    text = (Path(__file__).resolve().parent.parent / "info.yaml").read_text()
    return int(re.search(r"^\s*clock_hz:\s*(\d+)", text, re.M).group(1))


CLOCK_HZ = _clock_hz()
BAUD = 115200
POWER_UP_DIV = round(CLOCK_HZ / BAUD)  # the chip's power-up bit period (src/project.v CLKS_PER_BIT)
FAST_DIV = 20  # clock / 20: 2 Mbaud at 40 MHz, 3 Mbaud at 60 MHz
ENGINE_WORDS = refasm.IMAGE_WORDS  # 97
GLOBAL_WORDS = refasm.GLOBAL_WORDS  # 8
VERDICT = {0: "INDETERMINATE", 1: "PASS", 2: "VIOLATION", 3: "?"}


class HostError(Exception):
    pass


# ----------------------------------------------------------------------------
# Packets (one builder per command of src/host_port.v)
# ----------------------------------------------------------------------------


def _p(*b):
    return bytes(x & 0xFF for x in (list(b) + [0, 0, 0, 0])[:4])


def pkt_id():
    return _p(0x49)


def pkt_write_global(addr, word):
    return _p(0x47, addr, word >> 8, word)


def pkt_write_engine(addr, word, e=0):
    return _p(0x57, (e << 7) | addr, word >> 8, word)


def pkt_read(sel, addr, part):
    """sel 0 global / 1 engine 0; addr 0x80 = computed CRC; part 0 low / 1 high byte."""
    return _p(0x72, sel, addr, part)


def pkt_crc(sel, crc):
    return _p(0x43, sel, crc >> 8, crc)


def pkt_run(on, e=0):
    return _p(0x52, e, 1 if on else 0)


def pkt_push(byte, e=0):
    return _p(0x50, e, byte)


def pkt_pop(e=0):
    return _p(0x70, e)


def pkt_drain(maxn=8, e=0):
    return _p(0x44, e, maxn)


def pkt_stats_latch(slot, e=0):
    return _p(0x53, e, slot)


def pkt_stats_read(idx, part, e=0):
    return _p(0x73, e, idx, part)


def pkt_stats_clear(slot, e=0):
    return _p(0x5A, e, slot)


def pkt_lost(clear=False, e=0):
    return _p(0x4C, e, 1 if clear else 0)


def pkt_baud(div):
    return _p(0x42, div >> 8, div)


def pkt_status(port=False, e=0):
    return _p(0x51, e, 1 if port else 0)


def decode_status(data):
    """DATA of 'Q' e 0 -> dict (reading it cleared the sticky bits)."""
    names = ["running", "S", "crc_fail", "pin_fault", "in_underrun", "verdict_ovf", "out_ovf", "ni"]
    return {n: bool(data >> (7 - i) & 1) for i, n in enumerate(names)}


def decode_verdict(hdr):
    """A VERDICT header slot -> dict (src/engine.v `vhdr`)."""
    msat = hdr & 7
    return {
        "verdict": VERDICT[hdr >> 14], "FX": hdr >> 13 & 1, "FY": hdr >> 12 & 1, "TAG": hdr >> 11 & 1,
        "L": hdr >> 10 & 1, "NI": hdr >> 9 & 1, "raw": hdr >> 8 & 1, "b": hdr >> 4 & 0xF,
        "ORD": hdr >> 3 & 1, "m3": msat - 8 if msat & 4 else msat,
    }


def split_verdicts(slots):
    """Drained 16-bit slots -> list of (header dict, raw m or None), pairing raw slots."""
    out, i = [], 0
    while i < len(slots):
        h = decode_verdict(slots[i])
        m = None
        if h["raw"] and i + 1 < len(slots):
            m = slots[i + 1] - 0x10000 if slots[i + 1] & 0x8000 else slots[i + 1]
            i += 1
        out.append((h, m))
        i += 1
    return out


# ----------------------------------------------------------------------------
# Bit-serial firmware (i2c_controller.s, spi_cpha0.s, spi_cpha1.s)
# ----------------------------------------------------------------------------
# SR is the engine's only shift register and each bit needs a shift out (sho) and a
# shift in (shi). The host spreads each data byte over 16 bits with a 0 after every
# data bit, so two shifts per bit bring the next data bit to SR[15]; the 8 samples
# collect at the bottom as 1 s7 1 s6 ... 1 s0 and come back as two OUT bytes.

I2C_START, I2C_STOP, I2C_XFER, I2C_ACK, I2C_NACK = 0x53, 0x50, 0x58, 0x4B, 0x41  # 'S' 'P' 'X' 'K' 'A'
SPI_END = 0xFF  # end of frame: CS high (a spread byte never has bit 6 set)


def spread(byte):
    """Data byte -> the two IN bytes (high, low): t7 0 t6 0 t5 0 t4 0, t3 0 t2 0 t1 0 t0 0."""
    w = 0
    for k in range(8):
        w |= (byte >> k & 1) << (2 * k + 1)
    return w >> 8, w & 0xFF


def gather(hi, lo):
    """The two OUT bytes after 8 bits -> the byte sampled (bits 14, 12, ..., 0 of SR)."""
    w = hi << 8 | lo
    return sum((w >> (2 * k) & 1) << k for k in range(8))


def pin_config(spec):
    """'0:pp,1:od' -> {pin: (owner 1 = engine 0, mode 1 push-pull / 2 open-drain)}."""
    modes = {"pp": 1, "od": 2}
    pins = {}
    for item in filter(None, (spec or "").split(",")):
        p, m = item.split(":")
        pins[int(p)] = (1, modes[m])
    return pins


def load_packets(glob, img):
    """Halt, write the global config and the engine image, and both expected CRCs."""
    if len(glob) != GLOBAL_WORDS or len(img) != ENGINE_WORDS:
        raise HostError(f"image sizes {len(glob)}/{len(img)}, expected {GLOBAL_WORDS}/{ENGINE_WORDS}")
    pkts = [pkt_run(False)]
    pkts += [pkt_write_global(a, w) for a, w in enumerate(glob)]
    pkts += [pkt_write_engine(a, w) for a, w in enumerate(img)]
    pkts += [pkt_crc(0, refasm.crc16(glob)), pkt_crc(1, refasm.crc16(img))]
    return pkts


def set_equ(source_text, equ):
    """Override `.equ NAME value` lines, e.g. {"CPOL": 1} (each name must be defined)."""
    import re

    for name, value in (equ or {}).items():
        pat = re.compile(rf"^(\s*\.equ\s+{re.escape(name)}\s+)\S+", re.M)
        if not pat.search(source_text):
            raise HostError(f".equ {name} is not defined in the source")
        source_text = pat.sub(lambda m: m.group(1) + str(value), source_text, count=1)
    return source_text


def build(source_text, pins=None, mux=None, clock_hz=CLOCK_HZ, equ=None):
    """Assemble firmware -> (global image, engine image, notes)."""
    source_text = set_equ(source_text, equ)
    a = refasm.Assembler(clock_hz)
    words = a.assemble(source_text)
    return refasm.global_image(pins or {}, mux), a.image(words), a.notes


# ----------------------------------------------------------------------------
# Serial transport (real hardware)
# ----------------------------------------------------------------------------


class Referee:
    def __init__(self, port, baud=BAUD, timeout=0.5):
        import serial  # pyserial; only needed for real hardware

        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.ser.reset_input_buffer()

    def transact(self, pkt, extra=0):
        self.ser.write(pkt)
        reply = self.ser.read(2)
        if len(reply) != 2 or reply[0] not in (ACK, NAK):
            raise HostError(f"bad reply {reply.hex() or 'nothing'} to {pkt.hex()}")
        rest = b""
        if reply[0] == ACK and pkt[0] == 0x44:
            rest = self.ser.read(2 * reply[1])
        return reply[0] == ACK, reply[1], rest

    def must(self, pkt):
        ok, data, rest = self.transact(pkt)
        if not ok:
            raise HostError(f"NAK for {pkt.hex()}")
        return (data, rest) if pkt[0] == 0x44 else data

    def fast(self):
        self.must(pkt_baud(FAST_DIV))
        self.ser.baudrate = CLOCK_HZ // FAST_DIV


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", required=True)
    ap.add_argument("--fast", action="store_true", help="switch to clock / 20 baud first")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("id")
    sub.add_parser("status")
    sub.add_parser("halt")
    sub.add_parser("drain")
    st = sub.add_parser("stats")
    st.add_argument("slot", type=int)
    ld = sub.add_parser("load", help="assemble firmware, load it with a pin config, optionally run")
    ld.add_argument("source")
    ld.add_argument("--pin", default="", help="owned output pins, e.g. 0:pp,1:od")
    ld.add_argument("--run", action="store_true")
    ld.add_argument("--equ", action="append", default=[], help="override a .equ, e.g. CPOL=1 (SPI modes 2/3)")
    args = ap.parse_args(argv)

    ref = Referee(args.port)
    try:
        if args.fast:
            ref.fast()
        if args.cmd == "id":
            print("Referee" if ref.must(pkt_id()) == ID else "unexpected id")
        elif args.cmd == "status":
            print(decode_status(ref.must(pkt_status())))
        elif args.cmd == "halt":
            ref.must(pkt_run(False))
        elif args.cmd == "drain":
            n, rest = ref.must(pkt_drain())
            slots = [rest[2 * i] << 8 | rest[2 * i + 1] for i in range(n)]
            for h, m in split_verdicts(slots):
                print(h, "" if m is None else f"m = {m}")
        elif args.cmd == "stats":
            ref.must(pkt_stats_latch(args.slot))
            names = ["pass", "viol", "indet", "min", "max"]
            print({n: ref.must(pkt_stats_read(i, 1)) << 8 | ref.must(pkt_stats_read(i, 0)) for i, n in enumerate(names)})
        else:
            equ = dict(e.split("=", 1) for e in args.equ)
            glob, img, notes = build(Path(args.source).read_text(), pin_config(args.pin), equ=equ)
            for p in load_packets(glob, img):
                ref.must(p)
            if args.run:
                ok, _, _ = ref.transact(pkt_run(True))
                if not ok:
                    raise HostError(f"RUN refused: {decode_status(ref.must(pkt_status()))}")
            print("\n".join(notes) + ("\nloaded, running" if args.run else "\nloaded"))
    except (HostError, refasm.AsmError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
