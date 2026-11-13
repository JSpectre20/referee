# SPDX-License-Identifier: Apache-2.0
"""Bus target models for the chip-level tests (test/test.py).

They are written from the I2C and SPI specifications, not from the firmware, and they
watch the bus wires in test/tb.v, so they check the firmware's behaviour and its timing
from the outside:

- I2CTarget: a 24Cxx-style EEPROM (one address byte pointer, sequential read / write),
  optionally stretching SCL after every byte, and checking every Fast-mode timing limit
  (NXP UM10204 table 10: tLOW, tHIGH, tSU;STA, tHD;STA, tSU;STO, tBUF, tSU;DAT) on the
  bus as it actually moves.
- SPITarget: a full-duplex target in any of the four modes, replying with a given byte
  sequence and checking the clock's idle level, its half-periods, CS setup / hold and
  MOSI setup / hold around every sampling edge.

Both collect problems in `violations` (a list of strings) instead of raising, so a test
can report all of them at once. Both read the bus only once a time step has settled
(ReadOnly): a wire in tb.v built from two chip outputs that change in the same cycle can
show a zero-width glitch between them, which a real target would never see. Their own
outputs change a little later (a target's output delay), never in the ReadOnly phase.
"""

import cocotb
from cocotb.triggers import Edge, First, ReadOnly, Timer
from cocotb.utils import get_sim_time

# Fast-mode limits, ns (UM10204 rev. 7, table 10)
I2C_FM = {"tLOW": 1300, "tHIGH": 600, "tSU;STA": 600, "tHD;STA": 600, "tSU;STO": 600, "tBUF": 1300, "tSU;DAT": 100}


def _ns():
    return get_sim_time("ns")


def _later(sig, value, delay_ns):
    """Set a testbench input `delay_ns` from now (outside the ReadOnly phase)."""
    async def go():
        await Timer(delay_ns, unit="ns")
        sig.value = value
    cocotb.start_soon(go())


class I2CTarget:
    def __init__(self, dut, addr=0x50, stretch_ns=0, hold_ns=150):
        self.dut, self.addr, self.stretch_ns, self.hold_ns = dut, addr, stretch_ns, hold_ns
        self.mem = bytearray(256)
        self.ptr = 0
        self.violations = []
        self.events = []  # ("start" | "stop" | "addr" | "write" | "read", value)
        self.stretches = 0
        self.state = "idle"
        dut.i2c_sda_low.value = 0
        dut.i2c_scl_low.value = 0
        self.task = cocotb.start_soon(self._run())

    def stop_model(self):
        self.task.cancel()
        self.dut.i2c_sda_low.value = 0
        self.dut.i2c_scl_low.value = 0

    def _check(self, name, got):
        if got < I2C_FM[name]:
            self.violations.append(f"{name} = {got:.1f} ns < {I2C_FM[name]} ns at {_ns():.0f} ns")

    def _lines(self):
        return int(self.dut.scl_bus.value), int(self.dut.sda_bus.value)

    def _drive(self, low):
        """Change our SDA output tHD;DAT after the SCL fall (SCL is low then)."""
        _later(self.dut.i2c_sda_low, int(low), self.hold_ns)

    def _stretch(self):
        """Hold SCL low (the chip has just pulled it low) for stretch_ns."""
        self.stretches += 1
        _later(self.dut.i2c_scl_low, 1, 0.001)
        _later(self.dut.i2c_scl_low, 0, self.stretch_ns)

    async def _run(self):
        scl, sda = self._lines()
        t_rise = t_fall = t_sda = t_start = t_stop = None
        after_start = False
        while True:
            await First(Edge(self.dut.scl_bus), Edge(self.dut.sda_bus))
            await ReadOnly()
            now = _ns()
            nscl, nsda = self._lines()
            if (nscl, nsda) == (scl, sda):
                continue  # a zero-width glitch
            if nscl != scl:
                if nscl:  # SCL rises: the bit on SDA is sampled
                    if t_fall is not None:
                        self._check("tLOW", now - t_fall)
                    if t_sda is not None and t_fall is not None and t_sda > t_fall:
                        self._check("tSU;DAT", now - t_sda)
                    t_rise = now
                    self._rise(nsda)
                else:  # SCL falls
                    if t_rise is not None:
                        self._check("tHIGH", now - t_rise)
                    if after_start:
                        self._check("tHD;STA", now - t_start)
                        after_start = False
                    t_fall = now
                    self._fall()
            elif nsda != sda:
                if nscl:  # SDA moves while SCL is high: START or STOP
                    if t_rise is not None:
                        self._check("tSU;STA" if nsda == 0 else "tSU;STO", now - t_rise)
                    if nsda == 0:
                        if t_stop is not None:
                            self._check("tBUF", now - t_stop)
                        t_start, after_start = now, True
                        self._start()
                    else:
                        t_stop = now
                        self._stop()
                else:
                    t_sda = now
            scl, sda = nscl, nsda

    # -- protocol ----------------------------------------------------------------

    def _start(self):
        self.events.append(("start", None))
        self.state, self.bitn, self.byte = "addr", 0, 0

    def _stop(self):
        self.events.append(("stop", None))
        self.state = "idle"

    def _rise(self, sda):
        if self.state in ("idle", "ignore"):
            return
        if self.bitn < 8:
            if self.state in ("addr", "write"):
                self.byte = (self.byte << 1) | sda
            self.bitn += 1
        else:  # the ACK clock
            if self.state == "read":
                self.ctrl_ack = sda == 0
            self.bitn = 9

    def _fall(self):
        if self.state in ("idle", "ignore"):
            return
        if self.bitn == 8:  # a byte is complete: the ACK clock comes next
            if self.state == "addr":
                ours = (self.byte >> 1) == self.addr
                self.events.append(("addr", self.byte))
                self.next = ("read" if self.byte & 1 else "write") if ours else "ignore"
                self.first = True
                self._drive(ours)
            elif self.state == "write":
                if self.first:
                    self.ptr, self.first = self.byte, False
                else:
                    self.mem[self.ptr] = self.byte
                    self.events.append(("write", self.byte))
                    self.ptr = (self.ptr + 1) & 0xFF
                self._drive(True)
            else:  # read: release SDA, the controller ACKs or NACKs
                self._drive(False)
        elif self.bitn == 9:  # the ACK clock is over
            if self.state == "addr":
                self.state = self.next
            elif self.state == "read" and not self.ctrl_ack:
                self.state = "ignore"  # NACK: the controller sends STOP or a repeated START
            self.bitn, self.byte = 0, 0
            if self.state == "read":
                self.tx = self.mem[self.ptr]
                self.events.append(("read", self.tx))
                self.ptr = (self.ptr + 1) & 0xFF
                self._drive(not (self.tx >> 7 & 1))
            else:
                self._drive(False)
            if self.stretch_ns and self.state != "ignore":
                self._stretch()
        elif self.state == "read":  # bit bitn was sampled: the next one
            self._drive(not (self.tx >> (7 - self.bitn) & 1))


class SPITarget:
    def __init__(self, dut, mode, replies, half_ns, cycle_ns):
        """replies: one list of bytes per CS-low frame (like a device answering a command)."""
        self.dut = dut
        self.cpol, self.cpha = mode >> 1, mode & 1
        self.replies = [list(r) for r in replies]
        self.reply = []
        self.half, self.cyc = half_ns, cycle_ns
        self.frames = []  # bytes received, one list per CS-low frame
        self.violations = []
        dut.spi_miso.value = 0
        self.task = cocotb.start_soon(self._run())

    def stop_model(self):
        self.task.cancel()

    def _bad(self, what):
        self.violations.append(f"{what} at {_ns():.0f} ns")

    def _miso(self, bit):
        _later(self.dut.spi_miso, bit, 5)  # the target's output delay

    def _next_tx(self):
        return self.reply.pop(0) if self.reply else 0x00

    async def _run(self):
        d = self.dut
        cs, sck, mosi = int(d.spi_cs.value), int(d.spi_sck.value), int(d.spi_mosi.value)
        t_edge = t_cs = t_mosi = t_sample = None
        cur, bitn, shift, tx = None, 0, 0, 0
        nedge, t_half = 0, None  # SCK edges since CS fell; the last half period within this byte
        # a sampling edge's MOSI must be stable for this long before and after it: the
        # firmware gives half a period; allow 4 cycles for instruction slots
        margin = self.half - 4 * self.cyc
        while True:
            await First(Edge(d.spi_cs), Edge(d.spi_sck), Edge(d.spi_mosi))
            await ReadOnly()
            now = _ns()
            ncs, nsck, nmosi = int(d.spi_cs.value), int(d.spi_sck.value), int(d.spi_mosi.value)
            if (ncs, nsck, nmosi) == (cs, sck, mosi):
                continue  # a zero-width glitch
            if nmosi != mosi and ncs == 0:
                if t_sample is not None and now - t_sample < margin:
                    self._bad(f"MOSI hold {now - t_sample:.0f} ns")
                t_mosi = now
            if ncs != cs:
                if nsck != self.cpol:
                    self._bad(f"SCK = {nsck} (not CPOL {self.cpol}) when CS {'falls' if ncs == 0 else 'rises'}")
                if ncs == 0:
                    cur, bitn, shift, t_cs, t_edge, nedge = [], 0, 0, now, None, 0
                    self.reply = self.replies.pop(0) if self.replies else []
                    if self.cpha == 0:  # the first bit goes out when CS falls
                        tx = self._next_tx()
                        self._miso(tx >> 7 & 1)
                else:
                    if t_edge is not None and now - t_edge < self.half - self.cyc:
                        self._bad(f"CS hold {now - t_edge:.0f} ns")
                    if bitn:
                        self._bad(f"CS rose after {bitn} bits")
                    self.frames.append(cur)
            elif nsck != sck and ncs == 0:
                leading = sck == self.cpol  # leaving the idle level
                ref = t_edge if t_edge is not None else t_cs
                if now - ref < self.half - self.cyc:
                    self._bad(f"SCK half-period / CS setup {now - ref:.0f} ns")
                # Within a byte (16 edges, the first a leading edge) SCK must be symmetric:
                # each half period equals the one before, to within half a clock cycle (the
                # pads change on clock edges, so a real difference is a whole cycle).
                if nedge % 16 == 0:
                    t_half = None  # the first edge of a byte: the gap before it may differ
                else:
                    h = now - t_edge
                    if t_half is not None and abs(h - t_half) > self.cyc / 2:
                        self._bad(f"SCK half periods {t_half:.0f} / {h:.0f} ns within a byte")
                    t_half = h
                nedge += 1
                t_edge = now
                if leading == (self.cpha == 0):  # sampling edge
                    if t_mosi is not None and now - t_mosi < margin:
                        self._bad(f"MOSI setup {now - t_mosi:.0f} ns")
                    shift, bitn, t_sample = (shift << 1) | nmosi, bitn + 1, now
                    if bitn == 8:
                        cur.append(shift)
                        bitn, shift = 0, 0
                        if self.cpha == 0:
                            tx = self._next_tx()  # its MSB goes out on this bit's trailing edge
                else:  # shifting edge: the next bit out
                    if self.cpha == 1 and bitn == 0:
                        tx = self._next_tx()
                    self._miso(tx >> (7 - bitn) & 1)
            cs, sck, mosi = ncs, nsck, nmosi
