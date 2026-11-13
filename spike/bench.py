# SPDX-License-Identifier: Apache-2.0
"""Pure-Python, cycle-stepped test bench for the ISA v1 spike: engine models, nets, bus devices.

What it holds:
  - Engines: each one a `model_v1.Model` (each thinks it is engine 0), loaded from a firmware
    source, with a pin map onto named nets ("uio2" -> "sda") and a host that pushes IN bytes and
    pops OUT bytes with the FIFOs' real back-pressure (depth 4).
  - Nets: resolved every cycle from their drivers (engine pins, devices) and an optional pull.
  - Devices: Python models of external chips, stepped once per cycle (UART, SPI below; I2C and CAN
    devices can be added the same way).

Timing convention, one bench step = one clock cycle c:
  1. Every driver states its level for cycle c:
     - an engine drives what `pins()` reports after its step c-1, so a SET/SHO issued in cycle t
       is on the net from cycle t+1 (the 1-cycle output latency, docs/isa-timing.md 3.2);
     - a device drives what it decided by its sample() of cycle c-1: a device reacts one cycle
       after it sees a level (a registered output, 16.7 ns at 60 MHz).
  2. Each net resolves: 0 if anyone drives 0, 1 if anyone drives 1, else its pull, else None
     (floating). Drivers at different levels (push-pull 1 against push-pull 0 or an open-drain
     low) raise BenchError. Open-drain drivers only ever drive 0 or release.
  3. Devices sample the levels of cycle c (directly: no synchronizer).
  4. Each engine steps with the levels of cycle c on its input pads (a floating net reads 0). The
     model's 2-flop synchronizer makes a level of cycle c visible to waitp/tstp/shi in cycle c+2.
  5. Every net's waveform gets its level of cycle c: wave[c].
"""

import re
import sys
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import asm_v1  # noqa: E402
from model_v1 import UO_PINS, Model  # noqa: E402

FW = HERE / "firmware"
CLOCK_HZ = 60_000_000
MODES = {"pp": 1, "od": 2}  # global-config pin modes: push-pull, open-drain


class BenchError(AssertionError):
    pass


def set_equ(text, equ):
    """Override `.equ NAME value` lines of a source, e.g. {"CPOL": 1, "BIT": "1s/115200*103/100"}."""
    for name, value in (equ or {}).items():
        pat = re.compile(rf"^(\s*\.equ\s+{re.escape(name)}\s+)[^;#\n]*", re.M)
        if not pat.search(text):
            raise BenchError(f".equ {name} is not defined in the source")
        text = pat.sub(lambda m: f"{m.group(1)}{value} ", text, count=1)
    return text


def out_pin(pad):
    """Pad name -> SET/SHO pin number: uio0..7 -> 0..7, uo0..3,5..7 -> 8..14 (uo4 is the host TX)."""
    if pad.startswith("uio"):
        return int(pad[3:])
    if pad.startswith("uo") and int(pad[2:]) in UO_PINS:
        return 8 + UO_PINS.index(int(pad[2:]))
    raise BenchError(f"{pad} is not an engine output pad")


class Net:
    def __init__(self, name, pull=None):
        self.name, self.pull = name, pull
        self.level = pull
        self.wave = []  # level per cycle: 0, 1 or None (floating)


class Device:
    """An external chip. drive(c) -> {net: 0 | 1 | None (release)} for cycle c, decided from what
    sample() saw up to cycle c-1. sample(c, lv) sees every net's level in cycle c. Protocol and
    timing violations go to self.errors (tests assert it is empty, negative controls that it is not)."""

    def __init__(self):
        self.errors = []

    def drive(self, c):
        return {}

    def sample(self, c, lv):
        pass

    def err(self, c, msg):
        self.errors.append(f"{type(self).__name__} cycle {c}: {msg}")


class Engine:
    """One engine model, its pad-to-net map and its host.

    pins: {pad: net}, pads "uio0".."uio7" (in or out), "uo0".."uo7" (out), "ui0".."ui7" (in).
    load() assembles, writes the images, RUNs; `words` and `notes` keep the assembler's report."""

    def __init__(self, bench, name, pins, words=128):
        self.bench, self.name, self.pins = bench, name, dict(pins)
        self.model = Model(words)
        self.size = words
        self.modes = {}
        self.host_in = deque()  # bytes the host still has to push
        self.received = []  # (cycle, byte) popped from OUT
        self.host_every = 1  # the host acts once every N cycles
        self.forbid = ()

    def load(self, source, modes, equ=None, forbid=("PIN_FAULT", "OUT_OVF")):
        """source: firmware file name (in spike/firmware), a Path, or source text.
        modes: {pad: "pp" | "od"} for the pads this engine drives. forbid: sticky status bits that
        must never be set (IN_UNDERRUN is allowed by default: polling `pull; je` sets it)."""
        if isinstance(source, Path) or source.endswith(".s"):
            path = source if isinstance(source, Path) else FW / source
            source = path.read_text()
        text = set_equ(source, equ)
        asm = asm_v1.Assembler(self.bench.clock_hz)
        words = asm.assemble(text)
        if len(words) > self.size:
            raise BenchError(f"{self.name}: {len(words)} words, the engine holds {self.size}")
        img = asm.image(words)
        self.words, self.notes = len(words), asm.notes
        self.modes = {pad: MODES[m] for pad, m in modes.items()}
        glob = asm_v1.global_image({out_pin(pad): (1, m) for pad, m in self.modes.items()})
        md = self.model
        for i, w in enumerate(glob):
            md.write_global(i, w)
        for i, w in enumerate(img):
            md.write_engine(i, w)
        md.set_crc(0, asm_v1.crc16(glob))
        md.set_crc(1, asm_v1.crc16(img))
        assert md.run()
        self.forbid = tuple(forbid)
        return self

    def report(self):
        return f"{self.words} of {self.size} words"

    def send(self, *data):
        """Queue bytes for the host to push into IN as room frees up."""
        for b in data:
            self.host_in.extend(b if isinstance(b, (bytes, list, tuple)) else [b])

    def out(self):
        return [b for _, b in self.received]

    # -- per cycle ----------------------------------------------------------

    def drivers(self):
        """[(net, level)] this engine drives in the coming cycle."""
        uio_out, uio_oe, uo = self.model.pins()
        res = []
        for pad, net in self.pins.items():
            if pad.startswith("uio") and (uio_oe >> int(pad[3:])) & 1:
                res.append((net, (uio_out >> int(pad[3:])) & 1))
            elif pad.startswith("uo") and self.model.ran and pad in self.modes:
                res.append((net, (uo >> int(pad[2:])) & 1))
        return res

    def step(self, c, lv):
        ui = uio = 0
        for pad, net in self.pins.items():
            v = lv[net] or 0
            if pad.startswith("uio"):
                uio |= v << int(pad[3:])
            elif pad.startswith("ui"):
                ui |= v << int(pad[2:])
        md = self.model
        md.step(ui, uio)
        if c % self.host_every == 0:  # the host, between cycles
            if self.host_in and md.in_push(self.host_in[0]):
                self.host_in.popleft()
            b = md.out_pop()
            if b is not None:
                self.received.append((c, b))
        if md.crc_fail:
            raise BenchError(f"{self.name}: the RUN check failed (CRC or pin claims)")
        for k in self.forbid:
            if md.sticky[k]:
                raise BenchError(f"{self.name}: sticky {k} set by cycle {c} (pc {md.pc})")


class Bench:
    def __init__(self, clock_hz=CLOCK_HZ):
        self.clock_hz = clock_hz
        self.nets, self.engines, self.devices = {}, [], []
        self.cycle = 0

    def net(self, name, pull=None):
        if name not in self.nets:
            self.nets[name] = Net(name, pull)
        return self.nets[name]

    def engine(self, name, pins, words=128):
        for net in pins.values():
            self.net(net)
        e = Engine(self, name, pins, words)
        self.engines.append(e)
        return e

    def add(self, dev):
        self.devices.append(dev)
        return dev

    def cycles(self, t_s):
        """A time in seconds -> cycles (float)."""
        return t_s * self.clock_hz

    def step(self):
        c = self.cycle
        drv = {n: [] for n in self.nets}
        for e in self.engines:
            for net, v in e.drivers():
                drv[net].append((e.name, v))
        for d in self.devices:
            for net, v in d.drive(c).items():
                if v is not None:
                    drv[net].append((type(d).__name__, v))
        lv = {}
        for name, net in self.nets.items():
            levels = {v for _, v in drv[name]}
            if len(levels) > 1:
                raise BenchError(f"cycle {c}: net {name} driven to both levels by {drv[name]}")
            net.level = levels.pop() if levels else net.pull
            net.wave.append(net.level)
            lv[name] = net.level
        for d in self.devices:
            d.sample(c, lv)
        for e in self.engines:
            e.step(c, lv)
        self.cycle += 1

    def run(self, n):
        for _ in range(int(n)):
            self.step()

    def run_until(self, pred, limit):
        """Step until pred() is true; BenchError after `limit` cycles. Returns the cycles taken."""
        start = self.cycle
        while not pred():
            if self.cycle - start >= limit:
                raise BenchError(f"condition not reached within {limit} cycles")
            self.step()
        return self.cycle - start

    def started(self):
        """True once every loaded engine has passed its RUN check."""
        return all(e.model.state == "running" for e in self.engines)

    def wave(self, name):
        return self.nets[name].wave

    def edges(self, name, start=0):
        """[(cycle, new level)] of every change of a net from cycle `start` on."""
        w = self.nets[name].wave
        return [(c, w[c]) for c in range(max(start, 1), len(w)) if w[c] != w[c - 1]]

    def errors(self):
        return [m for d in self.devices for m in d.errors]


# ---------------------------------------------------------------------------- UART

def parity_bit(byte):
    """Even parity: the bit that makes the number of ones in data + parity even."""
    return bin(byte).count("1") & 1


class UartRx(Device):
    """UART receiver, 8N1 or 8E1 (parity="even"), LSB first, on `net` (idle high).

    bit: bit period in cycles (a float is fine). The frame grid is anchored at the start bit's
    falling edge s (the first cycle the line is low): bit k spans [s + k*bit, s + (k+1)*bit).
    Checks: every edge inside a frame lies within `tol` cycles of a grid boundary
    (default 2% of a bit); the start bit is low at its middle; the stop bit is high at its middle;
    parity; the next start bit does not come before the stop bit has lasted a full bit (- tol).
    Each bit is read at the first cycle >= its ideal middle. frames: [(byte, parity_ok, stop_ok)]."""

    def __init__(self, net, bit, parity=None, tol=None):
        super().__init__()
        self.net, self.bit, self.parity = net, bit, parity
        self.nbits = 11 if parity else 10  # start, 8 data, [parity], stop
        self.tol = 0.02 * bit if tol is None else tol
        self.frames = []
        self.prev, self.start, self.end = 1, None, None
        self.bits = []

    def sample(self, c, lv):
        v = lv[self.net]
        if v is None:
            self.err(c, "line floating")
            v = 1
        if self.start is None:
            if self.prev == 1 and v == 0:  # a start bit
                if self.end is not None and c < self.end - self.tol:
                    self.err(c, f"stop bit too short: next start {self.end - c:.1f} cycles early")
                self.start, self.bits = c, []
        else:
            if v != self.prev:  # an edge inside the frame: on the grid?
                j = round((c - self.start) / self.bit)
                dev = c - (self.start + j * self.bit)
                if not 1 <= j <= self.nbits - 1 or abs(dev) > self.tol:
                    self.err(c, f"edge {dev:+.1f} cycles off the bit grid (bit {j}, tol {self.tol:.1f})")
            k = len(self.bits)
            if c >= self.start + (k + 0.5) * self.bit:
                self.bits.append(v)
                if len(self.bits) == self.nbits:
                    self._frame(c)
        self.prev = v

    def _frame(self, c):
        b = self.bits
        if b[0] != 0:
            self.err(c, "start bit not low at its middle")
        byte = sum(bit << i for i, bit in enumerate(b[1:9]))
        par_ok = (not self.parity) or b[9] == parity_bit(byte)
        stop_ok = b[-1] == 1
        self.frames.append((byte, par_ok, stop_ok))
        self.end = self.start + self.nbits * self.bit  # the stop bit should last until here
        self.start = None


class UartTx(Device):
    """UART transmitter, 8N1 or 8E1, LSB first, on `net`. Frames come from a queue of
    (byte, fault) with fault None, "parity" (send the wrong parity bit) or "framing" (stop bit low
    for one bit period, then the line goes back high). Frames start `gap` cycles apart at least;
    bit k of a frame starting at t0 occupies [t0 + k*bit, t0 + (k+1)*bit) (rounded up to cycles)."""

    def __init__(self, net, bit, parity=None, gap=0, start=0):
        super().__init__()
        self.net, self.bit, self.parity, self.gap = net, bit, parity, gap
        self.queue = deque()
        self.t0, self.levels, self.next_ok = None, None, start

    def send(self, byte, fault=None):
        self.queue.append((byte, fault))

    def idle(self):
        return self.t0 is None and not self.queue

    def drive(self, c):
        if self.t0 is None and self.queue and c >= self.next_ok:
            byte, fault = self.queue.popleft()
            bits = [0] + [(byte >> i) & 1 for i in range(8)]
            if self.parity:
                bits.append(parity_bit(byte) ^ (fault == "parity"))
            bits.append(0 if fault == "framing" else 1)
            self.t0, self.levels = c, bits
        if self.t0 is None:
            return {self.net: 1}
        k = int((c - self.t0) // self.bit)
        if k >= len(self.levels):
            self.next_ok = c + self.gap
            self.t0 = None
            return {self.net: 1}
        return {self.net: self.levels[k]}


# ---------------------------------------------------------------------------- SPI

def spi_edges(cpol, cpha):
    """-> (level SCK has after the sampling edge, level after the shifting edge)."""
    lead = 1 - cpol
    return (lead, cpol) if cpha == 0 else (cpol, lead)




class SpiTarget(Device):
    """SPI target (peripheral), mode = 2*CPOL + CPHA, MSB first; nets cs (active low), sck, mosi
    in, miso out. `replies`: the bytes to send, one per byte slot, in order across frames (a slot
    counts once its first bit has been sampled; 0xFF when none is left). MISO is released while
    CS is high.

    Edge convention: an SCK edge "at cycle e" means SCK has its new level from cycle e. The target
    samples MOSI as it was in cycle e-1 and drives its next MISO bit from cycle e+1 (CPHA 0: the
    first bit from the cycle after the one in which CS is low).
    Checks: SCK is at its idle level (CPOL) when CS falls and when it rises; MOSI is stable from
    e-tsu to e+th-1 around every sampling edge e; every frame is a whole number of bytes; with
    `half`, every SCK high and low time inside a frame lasts >= half cycles.
    frames: [[bytes received]]."""

    def __init__(self, cs="cs", sck="sck", mosi="mosi", miso="miso", mode=0, replies=(), tsu=2, th=1,
                 half=None):
        super().__init__()
        self.cs, self.sck, self.mosi, self.miso = cs, sck, mosi, miso
        self.cpol, self.cpha = mode >> 1, mode & 1
        self.s_lvl, _ = spi_edges(self.cpol, self.cpha)
        self.replies, self.tsu, self.th, self.half = list(replies), tsu, th, half
        self.last_edge = None
        self.frames = []
        self.slot0 = 0  # byte slots used by the frames before this one
        self.active, self.prev, self.out = False, None, None
        self.hist = deque(maxlen=16)  # MOSI in the previous cycles (hist[-1] = cycle c-1)
        self.hold = []  # [(last cycle, level)] MOSI must keep after a sampling edge

    def _bit(self, k):
        """Bit k (0 = first) of this frame's MISO stream."""
        s = self.slot0 + k // 8
        byte = self.replies[s] if s < len(self.replies) else 0xFF
        return (byte >> (7 - k % 8)) & 1

    def drive(self, c):
        return {self.miso: self.out}

    def sample(self, c, lv):
        cs, sck, mosi = lv[self.cs], lv[self.sck], lv[self.mosi]
        for last, level in self.hold:
            if mosi != level:
                self.err(c, "MOSI changed within the hold time after a sampling edge")
        self.hold = [(last, v) for last, v in self.hold if last > c]
        if self.prev is not None:
            pcs, psck = self.prev
            if pcs == 1 and cs == 0:  # CS falls: a frame starts
                if sck != self.cpol:
                    self.err(c, "SCK not idle when CS fell")
                self.active, self.rx, self.nbits, self.last_edge = True, [], 0, None
                if self.cpha == 0:
                    self.out = self._bit(0)
            elif pcs == 0 and cs == 1 and self.active:  # CS rises: the frame ends
                if sck != self.cpol:
                    self.err(c, "SCK not idle when CS rose")
                if self.nbits % 8:
                    self.err(c, f"frame ended after {self.nbits} bits, not a whole number of bytes")
                self.frames.append(self.rx)
                self.slot0 += -(-self.nbits // 8)
                self.active, self.out, self.hold = False, None, []
            elif self.active and sck != psck:
                if self.half and self.last_edge is not None and c - self.last_edge < self.half:
                    self.err(c, f"SCK phase of {c - self.last_edge} cycles, under {self.half}")
                self.last_edge = c
                if sck == self.s_lvl:  # sampling edge
                    if len(set(list(self.hist)[-self.tsu:])) > 1:
                        self.err(c, f"MOSI not stable for {self.tsu} cycles before a sampling edge")
                    bit = self.hist[-1]
                    if mosi != bit:
                        self.err(c, "MOSI changed at the sampling edge")
                    if self.th > 1:
                        self.hold.append((c + self.th - 1, bit))
                    if self.nbits % 8 == 0:
                        self.rx.append(0)
                    self.rx[-1] = (self.rx[-1] << 1) | (bit or 0)
                    self.nbits += 1
                else:  # shifting edge: the next bit (CPHA 0: after the sampled one; CPHA 1: to be sampled)
                    self.out = self._bit(self.nbits)
        self.hist.append(mosi)
        self.prev = (cs, sck)


class SpiController(Device):
    """SPI controller, mode = 2*CPOL + CPHA, MSB first; drives cs, sck, mosi; samples miso.

    half: SCK half period in cycles. A frame: CS falls at cycle t; SCK edge i is at
    t + tcss + i*half; CS rises tcss after the last edge and then stays high >= tcsh cycles.
    MOSI changes at the shifting edges (CPHA 0: its first bit at CS fall). At a sampling edge at
    cycle e, MISO is read as it was in cycle e-1 and must be stable from e-tsu to e-1 (setup).
    transfer(data, cut=None) queues a frame; cut = number of bits after which CS rises.
    frames: [[bytes received]] (a cut frame keeps only its whole bytes)."""

    def __init__(self, cs="cs", sck="sck", mosi="mosi", miso="miso", mode=0, half=30, tcss=None,
                 tcsh=60, tsu=2, start=0):
        super().__init__()
        self.cs, self.sck, self.mosi, self.miso = cs, sck, mosi, miso
        self.cpol, self.cpha = mode >> 1, mode & 1
        self.s_lvl, _ = spi_edges(self.cpol, self.cpha)
        self.half, self.tcsh, self.tsu = half, tcsh, tsu
        self.tcss = half if tcss is None else tcss
        self.queue, self.frames = deque(), []
        self.ev, self.samples = {}, set()  # cycle -> {net: level}; sampling-edge cycles
        self.lv = {cs: 1, sck: self.cpol, mosi: 1}
        self.free_at = start
        self.miso_hist = deque(maxlen=16)

    def transfer(self, data, cut=None):
        self.queue.append((list(data), cut))

    def done(self):
        return not self.queue and not self.ev

    def _schedule(self, t, data, cut):
        bits = [(b >> (7 - i)) & 1 for b in data for i in range(8)]
        n = len(bits) if cut is None else cut
        ev = {t: {self.cs: 0}}
        if self.cpha == 0:
            ev[t][self.mosi] = bits[0]
        for i in range(2 * n):
            e = t + self.tcss + i * self.half
            lead, k = i % 2 == 0, i // 2
            sck = 1 - self.cpol if lead else self.cpol
            ev[e] = {self.sck: sck}
            if lead and self.cpha == 1:
                ev[e][self.mosi] = bits[k]
            elif not lead and self.cpha == 0 and k + 1 < n:
                ev[e][self.mosi] = bits[k + 1]
            if sck == self.s_lvl:
                self.samples.add(e)
        end = t + 2 * self.tcss + (2 * n - 1) * self.half
        ev.setdefault(end, {})[self.cs] = 1
        self.ev, self.rx, self.nbits = ev, [], 0
        self.free_at = end + self.tcsh

    def drive(self, c):
        if not self.ev and self.queue and c >= self.free_at:
            self._schedule(c, *self.queue.popleft())
        for net, v in self.ev.pop(c, {}).items():
            self.lv[net] = v
            if net == self.cs and v == 1:
                self.frames.append(self.rx[: self.nbits // 8])
        return dict(self.lv)

    def sample(self, c, lv):
        if c in self.samples:
            self.samples.discard(c)
            window = list(self.miso_hist)[-self.tsu:]
            if len(set(window)) > 1 or None in window:
                self.err(c, f"MISO not stable for {self.tsu} cycles before a sampling edge")
            if self.nbits % 8 == 0:
                self.rx.append(0)
            self.rx[-1] = (self.rx[-1] << 1) | (self.miso_hist[-1] or 0)
            self.nbits += 1
        self.miso_hist.append(lv[self.miso])
