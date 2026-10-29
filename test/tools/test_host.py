# SPDX-License-Identifier: Apache-2.0
"""Host-side helpers of tools/referee_host.py for the bit-serial firmware (CP4).

- spread / gather against a bit-level model of what the firmware does with SR
  (sho msb then shi msb, once per bit), for every byte pair;
- set_equ (the SPI mode's CPOL override) and its error for an unknown name;
- the real-hardware sequences (Referee.i2c_write, i2c_read, spi_frame) against a fake
  chip that speaks the firmware's IN / OUT protocol, with a byte-level EEPROM and an
  SPI echo target behind it. The firmware itself is tested on the RTL (test/test.py);
  this checks the host code that only real hardware would otherwise exercise.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import referee_host as host  # noqa: E402


def firmware_bits(tx_hi, tx_lo, rx_bits):
    """What i2c_controller.s / spi_cpha*.s do with SR for one byte: per bit, `sho msb`
    (the bit out is SR[15]; SR shifts left, 1 in) then `shi msb` (SR shifts left, the
    sample in). Returns (bits sent, OUT bytes)."""
    sr, sent = tx_hi << 8 | tx_lo, []
    for s in rx_bits:
        sent.append(sr >> 15 & 1)
        sr = (sr << 1 | 1) & 0xFFFF
        sr = (sr << 1 | s) & 0xFFFF
    return sent, (sr >> 8, sr & 0xFF)


def test_spread_gather_every_byte():
    for t in range(256):
        hi, lo = host.spread(t)
        assert hi != host.SPI_END and not hi >> 6 & 1, "a spread byte never looks like the end marker"
        for r in (0x00, 0xFF, 0xA5, t ^ 0x5A):
            sent, (oh, ol) = firmware_bits(hi, lo, [r >> (7 - k) & 1 for k in range(8)])
            assert sum(b << (7 - k) for k, b in enumerate(sent)) == t, "MSB first, every data bit"
            assert host.gather(oh, ol) == r


def test_set_equ():
    src = ".equ CPOL 0   ; idle level\n.equ HALF 500ns\n        set 4, CPOL\n"
    out = host.set_equ(src, {"CPOL": 1})
    assert ".equ CPOL 1   ; idle level" in out and ".equ HALF 500ns" in out
    with pytest.raises(host.HostError):
        host.set_equ(src, {"CPHA": 1})
    for f in ("spi_cpha0.s", "spi_cpha1.s"):
        text = (Path(__file__).resolve().parents[2] / "firmware" / "engine" / f).read_text()
        for cpol in (0, 1):
            glob, img, _ = host.build(text, host.pin_config("4:pp,5:pp,6:pp"), equ={"CPOL": cpol})
            assert img[-1] == (1 << 4 | 1 << 5 | 1 << 6), "claims SCK, MOSI, CS"


class FakeChip(host.Referee):
    """The firmware's IN / OUT protocol with a byte-level bus behind it (no serial port)."""

    def __init__(self, i2c_addr=0x50):
        self.inq, self.outq = [], []
        self.mem, self.ptr, self.addr = bytearray(range(256)), 0, i2c_addr
        self.phase = "idle"  # I2C: idle | addr | ack_due | write | read
        self.spi_frames, self.spi_cur = [], None

    def transact(self, pkt, extra=0):
        if pkt[0] == 0x50:  # push
            if len(self.inq) >= 4:
                return False, 0, b""
            self.inq.append(pkt[2])
            self._run()
            return True, 0, b""
        if pkt[0] == 0x70:  # pop
            return (True, self.outq.pop(0), b"") if self.outq else (False, 0, b"")
        raise AssertionError(f"unexpected packet {pkt.hex()}")

    def _out(self, sr):
        self.outq += [sr >> 8 & 0xFF, sr & 0xFF]

    @staticmethod
    def _unspread(hi, lo):  # the data byte inside two spread IN bytes
        return sum((((hi << 8 | lo) >> (2 * k + 1)) & 1) << k for k in range(8))

    def _run(self):
        while self.inq:
            c = self.inq[0]
            if c in (host.I2C_START, host.I2C_STOP):
                self.inq.pop(0)
                self.phase = "addr" if c == host.I2C_START else "idle"
            elif c == host.I2C_XFER:
                if len(self.inq) < 3:
                    return
                _, hi, lo = (self.inq.pop(0) for _ in range(3))
                t = self._unspread(hi, lo)
                if self.phase == "read":  # open drain: SDA is the AND of both ends
                    s = t & self.mem[self.ptr]
                    self.ptr = (self.ptr + 1) & 0xFF
                else:
                    s = t
                    if self.phase == "addr":
                        self.rw, self.ours = t & 1, (t >> 1) == self.addr
                        self.first = True
                    elif self.phase == "write":
                        if self.first:
                            self.ptr, self.first = t, False
                        else:
                            self.mem[self.ptr] = t
                            self.ptr = (self.ptr + 1) & 0xFF
                    self.ack_from = self.phase
                _, (oh, ol) = firmware_bits(*host.spread(t), [s >> (7 - k) & 1 for k in range(8)])
                self.outq += [oh, ol]
            elif c in (host.I2C_ACK, host.I2C_NACK):
                self.inq.pop(0)
                if self.phase == "addr":
                    sample = 0 if self.ours else 1
                    self.phase = ("read" if self.rw else "write") if self.ours else "idle"
                elif self.phase == "write":
                    sample = 0
                else:  # read: the controller's own ACK / NACK
                    sample = 0 if c == host.I2C_ACK else 1
                self._out(0xFFFE | sample)
            else:  # SPI frames (spread bytes / end marker): an echo target, one byte late
                if c == host.SPI_END:
                    self.inq.pop(0)
                    self.spi_frames.append(self.spi_cur or [])
                    self.spi_cur = None
                    continue
                if len(self.inq) < 2:
                    return
                hi, lo = self.inq.pop(0), self.inq.pop(0)
                t = self._unspread(hi, lo)
                if self.spi_cur is None:
                    self.spi_cur = []
                r = self.spi_cur[-1] if self.spi_cur else 0xEE
                self.spi_cur.append(t)
                _, (oh, ol) = firmware_bits(hi, lo, [r >> (7 - k) & 1 for k in range(8)])
                self.outq += [oh, ol]


def test_i2c_write_read_and_nack():
    chip = FakeChip(0x50)
    assert chip.i2c_write(0x50, 0x10, [0xA5, 0x3C]) == [0, 0, 0, 0]
    assert chip.mem[0x10:0x12] == bytes([0xA5, 0x3C])
    assert chip.i2c_read(0x50, 0x10, 2) == [0xA5, 0x3C]
    assert chip.i2c_write(0x51, 0x00, [0x55]) == [1], "a wrong address is NACKed and the write stops"
    with pytest.raises(host.HostError):
        chip.i2c_read(0x51, 0x00, 1)


def test_spi_frame():
    chip = FakeChip()
    assert chip.spi_frame([0x9F, 0x01, 0x02]) == [0xEE, 0x9F, 0x01], "full duplex: the echo comes one byte late"
    assert chip.spi_frames == [[0x9F, 0x01, 0x02]]


def test_cli_parses():
    with pytest.raises(SystemExit):  # --port is required; the subcommands exist
        host.main(["i2c-write", "50", "10", "a5"])
