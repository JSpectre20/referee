# SPDX-License-Identifier: Apache-2.0
"""SPI firmware (spike/firmware/spi_controller.s, spi_target.s) against independent SPI devices
(spike/bench.py), modes 0-3, random data with fixed seeds.

Negative controls: a controller with the wrong CPHA must corrupt the data; the target just above
its stated maximum SCK (half period 6 cycles instead of 7) must fail the MISO setup check.
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench import Bench, SpiController, SpiTarget  # noqa: E402

CTRL_PINS = {"uio4": "sck", "uio5": "mosi", "uio6": "cs", "uio7": "miso"}
CTRL_MODES = {"uio4": "pp", "uio5": "pp", "uio6": "pp"}
TGT_MODES = {"uio7": "pp"}
GAP = 20e-6 * 60e6  # spi_controller.s ends a frame after GAP without a byte


def nets(b):
    b.net("cs", pull=1)  # CS has a pull-up: no frame while nothing drives it
    b.net("miso", pull=1)


def random_frames(rng, n, longest=6):
    return [rng.randbytes(rng.randrange(1, longest + 1)) for _ in range(n)]


# ------------------------------------------------------------------ controller firmware

def run_controller(mode, frames, replies, equ=None, fw_mode=None):
    fw_mode = mode if fw_mode is None else fw_mode
    b = Bench()
    nets(b)
    e = b.engine("A", CTRL_PINS).load("spi_controller.s", CTRL_MODES,
                                      equ={"CPOL": fw_mode >> 1, "CPHA": fw_mode & 1, **(equ or {})})
    t = b.add(SpiTarget(mode=mode, replies=replies))
    b.run_until(b.started, 400)
    sent = 0
    for i, f in enumerate(frames):
        e.send(f)
        sent += len(f)
        b.run_until(lambda: len(t.frames) == i + 1, 2000 * len(f) + 2 * GAP)
        assert len(e.received) == sent
    return b, e, t


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_spi_controller_modes_random_frames(mode):
    rng = random.Random(40 + mode)
    frames = random_frames(rng, 4)
    replies = rng.randbytes(sum(map(len, frames)))
    b, e, t = run_controller(mode, frames, replies)
    assert t.errors == []
    assert t.frames == [list(f) for f in frames]  # what the target received, frame by frame
    assert e.out() == list(replies)  # what the host got back: one OUT byte per byte


@pytest.mark.parametrize("mode", [0, 3])
def test_spi_controller_fastest_sck(mode):
    """HALF = 4 cycles (SCK 7.5 MHz): the bit loop's shortest half period."""
    rng = random.Random(50 + mode)
    frames = random_frames(rng, 3)
    replies = rng.randbytes(sum(map(len, frames)))
    b, e, t = run_controller(mode, frames, replies, equ={"HALF": "4*1s/60000000"})
    assert t.errors == []
    assert t.frames == [list(f) for f in frames]
    assert e.out() == list(replies)


def test_negative_control_spi_controller_wrong_cpha_fails():
    rng = random.Random(60)
    frames = random_frames(rng, 3)
    replies = rng.randbytes(sum(map(len, frames)))
    b, e, t = run_controller(0, frames, replies, fw_mode=1)  # firmware mode 1, target mode 0
    assert t.frames != [list(f) for f in frames] or e.out() != list(replies)


# ------------------------------------------------------------------ target firmware

def run_target(mode, frames, half, tcss=12, cut=None, fw_mode=None, seed=70):
    """frames: MOSI bytes per frame; cut: {frame index: bits after which CS rises}.
    The host queues each frame's replies before the frame and the next only after it (the
    firmware's documented protocol). Returns bench, engine, controller device, replies."""
    fw_mode = mode if fw_mode is None else fw_mode
    rng = random.Random(seed)
    b = Bench()
    nets(b)
    e = b.engine("B", {"uio4": "sck", "uio5": "mosi", "uio6": "cs", "uio7": "miso"}).load(
        "spi_target.s", TGT_MODES, equ={"CPOL": fw_mode >> 1, "CPHA": fw_mode & 1})
    c = b.add(SpiController(mode=mode, half=half, tcss=tcss, tcsh=60))
    b.run_until(b.started, 400)
    b.run(10)  # the firmware reaches its wait for CS
    replies = []
    for i, f in enumerate(frames):
        r = rng.randbytes(len(f))
        replies.append(list(r))
        e.send(r)
        c.transfer(f, cut=(cut or {}).get(i))
        b.run_until(lambda: len(c.frames) == i + 1, (4 * half + 40) * 8 * len(f) + 1000)
        b.run(100)  # CS high time: the firmware sees the frame end
    return b, e, c, replies


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
@pytest.mark.parametrize("half", [7, 30, 100])
def test_spi_target_modes_random_frames(mode, half):
    """half = 7 cycles is the stated maximum (SCK 4.29 MHz); 30 is 1 MHz; 100 (300 kHz) is slower
    than the firmware's wait timeout, so every wait also goes through the CS check."""
    frames = random_frames(random.Random(80 + mode), 4)
    b, e, c, replies = run_target(mode, frames, half)
    assert c.errors == []
    assert c.frames == replies  # MISO bytes, frame by frame
    assert e.out() == [x for f in frames for x in f]  # MOSI bytes: one OUT byte per byte


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_negative_control_spi_target_above_max_sck_fails(mode):
    """half = 6 cycles (5 MHz): MISO comes 5 cycles after the shifting edge, 1 cycle before the
    sampling edge, under the controller's 2-cycle setup."""
    frames = random_frames(random.Random(90 + mode), 3)
    b, e, c, replies = run_target(mode, frames, 6)
    assert any("MISO not stable" in m for m in c.errors)


def test_spi_target_cs_setup_11_cycles():
    """CPHA 0 drives MISO's first bit 9 cycles after CS falls: 11 cycles to the first edge work."""
    frames = random_frames(random.Random(95), 3)
    b, e, c, replies = run_target(0, frames, 7, tcss=11)
    assert c.errors == [] and c.frames == replies


def test_negative_control_spi_target_wrong_cpha_fails():
    frames = random_frames(random.Random(96), 3)
    b, e, c, replies = run_target(0, frames, 30, fw_mode=1)
    assert c.frames != replies or e.out() != [x for f in frames for x in f]


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_spi_target_cs_rising_mid_byte(mode):
    """Frame 1 is cut after 12 bits: its whole byte is pushed, the 4 bits are dropped, and the
    next frames are aligned again. Each frame's replies were queued just before it."""
    frames = [bytes([0xA5, 0x3C]), bytes([0x81, 0x7E, 0x42]), bytes([0x99])]
    b, e, c, replies = run_target(mode, frames, 30, cut={0: 12})
    assert c.errors == []
    assert e.out() == [0xA5, 0x81, 0x7E, 0x42, 0x99]
    assert c.frames == [replies[0][:1], replies[1], replies[2]]


# ------------------------------------------------------------------ two engines

@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_two_engines_spi_controller_to_spi_target(mode):
    """Engine A runs spi_controller.s, engine B spi_target.s, at the target's maximum SCK
    (half period 7 cycles); a passive SPI target device checks the bus timing (its MISO pin is
    not connected)."""
    rng = random.Random(100 + mode)
    b = Bench()
    nets(b)
    equ = {"CPOL": mode >> 1, "CPHA": mode & 1}
    a = b.engine("A", CTRL_PINS).load("spi_controller.s", CTRL_MODES, equ={**equ, "HALF": "117ns"})
    t = b.engine("B", CTRL_PINS).load("spi_target.s", TGT_MODES, equ=equ)
    mon = b.add(SpiTarget(mode=mode, miso="nc", half=7))  # also: SCK high and low >= 7 cycles
    b.net("nc")
    b.run_until(b.started, 400)
    frames = random_frames(rng, 4)
    replies = []
    for i, f in enumerate(frames):
        r = rng.randbytes(len(f))
        replies.append(r)
        t.send(r)
        a.send(f)
        b.run_until(lambda: len(mon.frames) == i + 1, 400 * len(f) + 2 * GAP)
        b.run(100)
    assert mon.errors == []
    assert mon.frames == [list(f) for f in frames]
    assert t.out() == [x for f in frames for x in f]  # B received what A sent
    assert a.out() == [x for r in replies for x in r]  # A received what B sent
