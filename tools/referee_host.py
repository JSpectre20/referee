#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Host side of Referee's UART command port v2 (src/host_port.v, docs/isa-timing.md section 7).

Packets are 4 bytes, CMD A B C; every reply starts STATUS DATA (ACK 0x06 / NAK 0x15).
A NAK's DATA is always the command byte (docs/isa-timing.md 0b, diff 23).
The packet builders here are shared by the cocotb chip tests (test/test.py) and
the command-line tool, so the two can't drift apart.

    python3 tools/referee_host.py --port /dev/ttyUSB0 id
    python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/uart_tx.s --pin 0:pp --run
    python3 tools/referee_host.py --port /dev/ttyUSB0 status
    python3 tools/referee_host.py --port /dev/ttyUSB0 drain
    python3 tools/referee_host.py --port /dev/ttyUSB0 stats 0
    python3 tools/referee_host.py --port /dev/ttyUSB0 halt
    python3 tools/referee_host.py --port /dev/ttyUSB0 --engine 1 status       # the Player

Two engines at once (engine 0 = the Referee, engine 1 = the Player): the Player speaks a
bus, and the Referee judges it through the judge-channel mux (channel ch on uio_in[k]):

    python3 tools/referee_host.py --port /dev/ttyUSB0 load2 firmware/engine/i2c_setup_referee.s \
        firmware/engine/i2c_controller.s --pin1 2:od,3:od --mux 0:uio3,1:uio2 --run
    python3 tools/referee_host.py --port /dev/ttyUSB0 --engine 1 i2c-write 50 10 a5
    python3 tools/referee_host.py --port /dev/ttyUSB0 stats 0

Bit-serial controllers (load the firmware first, then talk to the bus through it):

    python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/i2c_controller.s --pin 2:od,3:od --run
    python3 tools/referee_host.py --port /dev/ttyUSB0 i2c-write 50 10 a5 3c      # addr, pointer, data (hex)
    python3 tools/referee_host.py --port /dev/ttyUSB0 i2c-read 50 10 2           # addr, pointer, count
    python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/spi_cpha0.s --pin 4:pp,5:pp,6:pp --run
    python3 tools/referee_host.py --port /dev/ttyUSB0 spi 9f 00 00 00            # one CS-low frame

The chip's UART starts at 115200 8N1 (ui_in[3] RX, uo_out[4] TX) at the project clock in
info.yaml; `--fast` switches both ends to clock / 20 first (3 Mbaud at 60 MHz).
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
FAST_DIV = 20  # clock / 20: 3 Mbaud at 60 MHz
ENGINE_WORDS = refasm.IMAGE_WORDS  # 97, for both engines
REFEREE, PLAYER = refasm.REFEREE, refasm.PLAYER  # engine numbers e
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
    """sel 0 global / 1 + e engine e; addr 0x80 = the CRC that engine's last RUN check
    computed (global: 0x80 + e, as computed by engine e); part 0 low / 1 high byte."""
    return _p(0x72, sel, addr, part)


def pkt_crc(sel, crc):
    """sel 0 global / 1 + e engine e."""
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


def pin_config(spec, e=REFEREE):
    """'0:pp,1:od' -> {pin: (owner e + 1, mode 1 push-pull / 2 open-drain)}."""
    modes = {"pp": 1, "od": 2}
    pins = {}
    for item in filter(None, (spec or "").split(",")):
        p, m = item.split(":")
        pins[int(p)] = (e + 1, modes[m])
    return pins


def mux_config(spec):
    """'0:uio3,1:uio2' -> the 4 judge-channel sources (0 = ui_in[4 + ch], 1 + k = uio_in[k])."""
    mux = [0, 0, 0, 0]
    for item in filter(None, (spec or "").split(",")):
        ch, src = item.split(":")
        mux[int(ch)] = 0 if src == "ui" else 1 + int(src.removeprefix("uio"))
    return mux


def _check_sizes(glob, imgs):
    if glob is not None and len(glob) != GLOBAL_WORDS:
        raise HostError(f"global image is {len(glob)} words, expected {GLOBAL_WORDS}")
    for e, img in imgs.items():
        if len(img) != ENGINE_WORDS:
            raise HostError(f"engine {e} image is {len(img)} words, expected {ENGINE_WORDS}")


def load_system_packets(glob, imgs):
    """Halt BOTH engines (the global config is written only while both are halted), write
    the global config and each image in imgs ({e: image}), and the expected CRCs."""
    _check_sizes(glob, imgs)
    pkts = [pkt_run(False, e) for e in (REFEREE, PLAYER)]
    pkts += [pkt_write_global(a, w) for a, w in enumerate(glob)]
    for e, img in imgs.items():
        pkts += [pkt_write_engine(a, w, e) for a, w in enumerate(img)]
    pkts.append(pkt_crc(0, refasm.crc16(glob)))
    pkts += [pkt_crc(1 + e, refasm.crc16(img)) for e, img in imgs.items()]
    return pkts


def load_packets(glob, img, e=REFEREE):
    """Halt both engines, write the global config and engine e's image, and both CRCs."""
    return load_system_packets(glob, {e: img})


def load_engine_packets(img, e):
    """Halt engine e only and write its image and expected CRC; the global config (and the
    other engine, which may keep running) are untouched."""
    _check_sizes(None, {e: img})
    pkts = [pkt_run(False, e)]
    pkts += [pkt_write_engine(a, w, e) for a, w in enumerate(img)]
    pkts.append(pkt_crc(1 + e, refasm.crc16(img)))
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


def build(source_text, pins=None, mux=None, clock_hz=CLOCK_HZ, equ=None, engine=REFEREE):
    """Assemble firmware for engine `engine` -> (global image, engine image, notes).
    pins: {pin: (owner, mode)} with owner = engine + 1 (see pin_config)."""
    img, notes = assemble(source_text, clock_hz, equ, engine)
    return refasm.global_image(pins or {}, mux), img, notes


def assemble(source_text, clock_hz=CLOCK_HZ, equ=None, engine=REFEREE):
    """Firmware -> (engine image, notes) for engine `engine`."""
    a = refasm.Assembler(clock_hz, engine)
    words = a.assemble(set_equ(source_text, equ))
    return a.image(words), a.notes


# ----------------------------------------------------------------------------
# Serial transport (real hardware)
# ----------------------------------------------------------------------------


class Referee:
    e = REFEREE  # the engine push_in / pop_out (and the bus helpers) talk to

    def __init__(self, port, baud=BAUD, timeout=0.5, engine=REFEREE):
        import serial  # pyserial; only needed for real hardware

        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.ser.reset_input_buffer()
        self.e = engine

    def transact(self, pkt, extra=0):
        self.ser.write(pkt)
        reply = self.ser.read(2)
        if len(reply) != 2 or reply[0] not in (ACK, NAK) or (reply[0] == NAK and reply[1] != pkt[0]):
            # (a NAK echoes the command byte: anything else is a reply to another packet)
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

    # -- bit-serial firmware (i2c_controller.s, spi_cpha0.s, spi_cpha1.s) --------
    # The same sequences as the chip tests (test/test.py: push_in, pop_out, serial_byte,
    # i2c_write, i2c_read), for a real chip.

    def push_in(self, byte, timeout=1.0):
        """Push one byte into IN, retrying while it is full."""
        import time

        t0 = time.monotonic()
        while not self.transact(pkt_push(byte, self.e))[0]:
            if time.monotonic() - t0 > timeout:
                raise HostError("IN stayed full: is the firmware running?")
            time.sleep(0.001)

    def pop_out(self, timeout=1.0):
        """Pop one byte from OUT, waiting until the firmware has pushed it."""
        import time

        t0 = time.monotonic()
        while True:
            ok, byte, _ = self.transact(pkt_pop(self.e))
            if ok:
                return byte
            if time.monotonic() - t0 > timeout:
                raise HostError("no byte in OUT: is the firmware running?")
            time.sleep(0.001)

    def serial_byte(self, byte, cmd=None):
        """One byte through the firmware: spread in, gather the 8 samples out."""
        if cmd is not None:
            self.push_in(cmd)
        for b in spread(byte):
            self.push_in(b)
        return gather(self.pop_out(), self.pop_out())

    def i2c_bit(self, cmd):
        """I2C_NACK (SDA released, sampled) or I2C_ACK (SDA low): one clock; the SDA sample."""
        self.push_in(cmd)
        self.pop_out()
        return self.pop_out() & 1

    def i2c_write(self, addr, ptr, data):
        """START, address + W, pointer, data, STOP -> the target's ACK bits (0 = ACK)."""
        acks = []
        self.push_in(I2C_START)
        for b in [addr << 1, ptr] + list(data):
            if self.serial_byte(b, I2C_XFER) != b:
                raise HostError(f"SDA did not read back {b:#04x}: another controller, or a stuck bus")
            acks.append(self.i2c_bit(I2C_NACK))
            if acks[-1]:
                break
        self.push_in(I2C_STOP)
        return acks

    def i2c_read(self, addr, ptr, n):
        """START, address + W, pointer, repeated START, address + R, n bytes, STOP."""
        self.push_in(I2C_START)
        for b in (addr << 1, ptr):
            self.serial_byte(b, I2C_XFER)
            if self.i2c_bit(I2C_NACK):
                self.push_in(I2C_STOP)
                raise HostError(f"no ACK for {b:#04x}")
        self.push_in(I2C_START)
        self.serial_byte(addr << 1 | 1, I2C_XFER)
        if self.i2c_bit(I2C_NACK):
            self.push_in(I2C_STOP)
            raise HostError("no ACK for the read address")
        got = []
        for k in range(n):
            got.append(self.serial_byte(0xFF, I2C_XFER))
            self.i2c_bit(I2C_ACK if k < n - 1 else I2C_NACK)
        self.push_in(I2C_STOP)
        return got

    def spi_frame(self, data):
        """One CS-low frame, full duplex -> the bytes received."""
        got = [self.serial_byte(b) for b in data]
        self.push_in(SPI_END)
        return got


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", required=True)
    ap.add_argument("--fast", action="store_true", help="switch to clock / 20 baud first")
    ap.add_argument("--engine", type=int, choices=(REFEREE, PLAYER), default=REFEREE,
                    help="0 = the Referee (default), 1 = the Player: for status, halt, load and the bus commands")
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
    ld.add_argument("--mux", default="", help="judge channels, e.g. 0:uio3,1:uio2 (default: all on ui_in)")
    ld.add_argument("--keep-global", action="store_true",
                    help="write only this engine's image (the other engine keeps running and the config stays)")
    l2 = sub.add_parser("load2", help="load both engines: the Referee and the Player, one global config")
    l2.add_argument("referee_source")
    l2.add_argument("player_source")
    l2.add_argument("--pin0", default="", help="the Referee's output pins, e.g. 0:pp")
    l2.add_argument("--pin1", default="", help="the Player's output pins, e.g. 2:od,3:od")
    l2.add_argument("--mux", default="", help="judge channels, e.g. 0:uio3,1:uio2")
    l2.add_argument("--run", action="store_true", help="run the Player, then the Referee")
    hexb = lambda x: int(x, 16)  # noqa: E731
    iw = sub.add_parser("i2c-write", help="through i2c_controller.s: write data at a pointer")
    iw.add_argument("addr", type=hexb)
    iw.add_argument("ptr", type=hexb)
    iw.add_argument("data", type=hexb, nargs="+")
    ir = sub.add_parser("i2c-read", help="through i2c_controller.s: read n bytes from a pointer")
    ir.add_argument("addr", type=hexb)
    ir.add_argument("ptr", type=hexb)
    ir.add_argument("n", type=int)
    sp = sub.add_parser("spi", help="through spi_cpha0.s / spi_cpha1.s: one CS-low frame")
    sp.add_argument("data", type=hexb, nargs="+")
    args = ap.parse_args(argv)

    ref = Referee(args.port, engine=args.engine)
    e = args.engine
    try:
        if args.fast:
            ref.fast()
        if args.cmd == "id":
            print("Referee" if ref.must(pkt_id()) == ID else "unexpected id")
        elif args.cmd == "status":
            print(decode_status(ref.must(pkt_status(e=e))))
        elif args.cmd == "halt":
            ref.must(pkt_run(False, e))
        elif args.cmd == "drain":
            n, rest = ref.must(pkt_drain())
            slots = [rest[2 * i] << 8 | rest[2 * i + 1] for i in range(n)]
            for h, m in split_verdicts(slots):
                print(h, "" if m is None else f"m = {m}")
        elif args.cmd == "i2c-write":
            acks = ref.i2c_write(args.addr, args.ptr, args.data)
            print("ACK" if not any(acks) else f"NACK after {len(acks) - 1} bytes")
        elif args.cmd == "i2c-read":
            print(" ".join(f"{b:02x}" for b in ref.i2c_read(args.addr, args.ptr, args.n)))
        elif args.cmd == "spi":
            print(" ".join(f"{b:02x}" for b in ref.spi_frame(args.data)))
        elif args.cmd == "stats":
            ref.must(pkt_stats_latch(args.slot))
            names = ["pass", "viol", "indet", "min", "max"]
            print({n: ref.must(pkt_stats_read(i, 1)) << 8 | ref.must(pkt_stats_read(i, 0)) for i, n in enumerate(names)})
        elif args.cmd == "load2":
            img0, n0 = assemble(Path(args.referee_source).read_text(), engine=REFEREE)
            img1, n1 = assemble(Path(args.player_source).read_text(), engine=PLAYER)
            pins = {**pin_config(args.pin0, REFEREE), **pin_config(args.pin1, PLAYER)}
            glob = refasm.global_image(pins, mux_config(args.mux))
            for p in load_system_packets(glob, {REFEREE: img0, PLAYER: img1}):
                ref.must(p)
            if args.run:
                for en in (PLAYER, REFEREE):
                    if not ref.transact(pkt_run(True, en))[0]:
                        raise HostError(f"RUN of engine {en} refused: {decode_status(ref.must(pkt_status(e=en)))}")
            print("\n".join(n0 + n1) + ("\nloaded, running" if args.run else "\nloaded"))
        else:
            equ = dict(x.split("=", 1) for x in args.equ)
            img, notes = assemble(Path(args.source).read_text(), equ=equ, engine=e)
            if args.keep_global:
                pkts = load_engine_packets(img, e)
            else:
                pkts = load_packets(refasm.global_image(pin_config(args.pin, e), mux_config(args.mux)), img, e)
            for p in pkts:
                ref.must(p)
            if args.run:
                ok, _, _ = ref.transact(pkt_run(True, e))
                if not ok:
                    raise HostError(f"RUN refused: {decode_status(ref.must(pkt_status(e=e)))}")
            print("\n".join(notes) + ("\nloaded, running" if args.run else "\nloaded"))
    except (HostError, refasm.AsmError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
