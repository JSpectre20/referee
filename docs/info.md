<!---
This file is used to generate your project datasheet.
-->

## How it works

Referee is a programmable protocol emulator and bus referee, built for the Jane Street
protocol emulator ASIC competition. Protocols are programs, not fixed logic, and the chip
can also *judge* timing on a bus it is not part of, with verdicts that are proved sound.

This build contains two engines of ISA v0.3 (`docs/isa-timing.md`): engine 0, the **Referee**,
which emulates and judges, and engine 1, the **Player**, which only emulates. The Player has a
96-word program, no snapshot, verdict block, bounds or stats; its referee instructions are
inert (`trig`, `rel`, `judge` do nothing, `tsget` gives 0, `waits` only times out). Words
64..95 of the Player run by falling through from word 63 (jump targets are 0..63 until ISA v1).
The two share the pins (one owner per pin) and the judge channels, so the Referee can judge a
bus the Player drives.

- **A 24-instruction timing engine** (each engine) with a 64-word (Referee) or 96-word (Player)
  program memory. Every instruction takes
  exactly one cycle, except the three waits. A `set` or `sho` changes its pin exactly one
  cycle later. Two deadline registers keep schedules drift-free: DL for the schedule
  (`mark`, `waitd`) and TO for timeouts (`tmo`, `tmon`). A UART transmitter or receiver is
  16 to 18 words of firmware (`firmware/engine/`).
- **An edge-timestamp unit** on four judge channels (default `ui_in[4..7]`, input-only pads, so
  judging them cannot disturb the bus). A **snapshot** freezes every channel's timestamps
  at one instant. The trigger edge sets that instant, and edges too close to it to order are
  flagged (ORD).
- **A three-valued verdict block**: `judge` compares a measured interval with a bound written
  in ns and answers PASS, VIOLATION or INDETERMINATE. The rule is proved sound by formal
  proofs (`formal/verdict.sby`): a PASS means the true interval met the bound, and a
  VIOLATION means it broke it. **Stats** count verdicts per bound, and a FIFO streams them
  (all, or only the non-PASS ones) to the host.
- **Pin ownership**: a global config gives each output pin an owner and a mode (push-pull or
  open-drain). Each image carries a CRC, and RUN refuses an image that fails its CRC or
  claims a pin it doesn't own.
- **A UART host port** on the standard Tiny Tapeout UART pins: 115200 8N1 at power-up, and up
  to clock / 20 on command (3 Mbaud at 60 MHz). Commands are 4 bytes. Each reply is ACK (0x06) or NAK (0x15), then a
  data byte; the verdict drain sends more.

`e` is the engine: 0 = Referee, 1 = Player.

| Command | Bytes | Effect |
| --- | --- | --- |
| Identify | `49 00 00 00` | reply `06 52` |
| Write global word | `47 addr hi lo` | addr 0..7; only while both engines are halted |
| Write engine word | `57 e:addr hi lo` | e in bit 7; addr 0..96 (Referee: program 0..63, bounds 64..95; Player: program 0..95; both: claimed pins 96); only while that engine is halted |
| Read word | `72 sel addr part` | sel 0 global / 1 + e engine e; addr `80` = that engine's computed CRC (global: `80` + e = the global CRC engine e computed); part 0 low / 1 high byte |
| Expected CRC | `43 sel hi lo` | sel 0 global / 1 + e engine e |
| Run / halt | `52 e 01 00` / `52 e 00 00` | RUN answers after the 105-cycle image check: ACK only if it passed |
| IN push / OUT pop | `50 e byte 00` / `70 e 00 00` | byte FIFOs between host and firmware |
| Verdict drain | `44 00 max 00` | Referee only; reply `06 n`, then n 16-bit slots, high byte first |
| Stats latch / read / clear | `53 00 slot 00` / `73 00 idx part` / `5A 00 slot 00` | Referee only; per bound slot 0..3: PASS, VIOLATION, INDETERMINATE counts, min and max m |
| Lost triggers | `4C 00 op 00` | Referee only; op 1 also clears |
| Baud | `42 hi lo 00` | UART bit period in clock cycles (20 = 3 Mbaud at 60 MHz), after the reply |
| Status | `51 e sel 00` | sel 0: engine e's {running, S, CRC_FAIL, PIN_FAULT, IN_UNDERRUN, VERDICT_OVF, OUT_OVF, NI}, read-and-clear |

Pins: `uio[0..7]` are engine pins 0..7 (input, push-pull or open-drain). `uo_out[0..3]` and
`uo_out[5..7]` are engine pins 8..14 (push-pull). `uo_out[4]` is the host UART TX, which no
engine can ever drive. Each pin has one owner (none, Referee or Player) in the global config,
and RUN refuses an image that claims a pin its engine doesn't own, so two engines never drive
one pin. A pin is driven only once its owner's RUN check has passed.

## How to test

Set the project clock to 60 MHz (the clock in `info.yaml`; the power-up 115200 baud and the
firmware timing depend on it). Connect a 3.3 V serial port at 115200 baud to `ui_in[3]`
(chip RX) and `uo_out[4]` (chip TX); the demo board's microcontroller can act as the host.
Then, from a PC:

```bash
python3 tools/referee_host.py --port /dev/ttyUSB0 id
python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/uart_tx.s --pin 0:pp --run
```

The second command assembles the UART transmitter, gives the engine `uio[0]` (push-pull),
loads both images with their CRCs and runs it. Bytes pushed into IN (`50 00 byte 00`) come
out of `uio[0]` at 115200 baud while `ui_in[0]` (CTS) is low.

To referee a bus, load `firmware/engine/setup_oracle.s` (or `i2c_setup_referee.s`) and feed
the data and clock lines to `ui_in[4]` and `ui_in[5]`. Then read the verdicts:

```bash
python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/i2c_setup_referee.s --run
python3 tools/referee_host.py --port /dev/ttyUSB0 drain
python3 tools/referee_host.py --port /dev/ttyUSB0 stats 0
```

To drive a bus from the chip, load a bit-serial controller and talk through it. The I2C
controller (400 kHz, open-drain SCL on `uio[2]`, SDA on `uio[3]`, clock stretching honoured)
and the SPI controller (1 MHz, modes 0-3: `spi_cpha0.s` for modes 0/2, `spi_cpha1.s` for 1/3,
CPOL set with `--equ`; SCK `uio[4]`, MOSI `uio[5]`, CS `uio[6]`, MISO `uio[7]`):

```bash
python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/i2c_controller.s --pin 2:od,3:od --run
python3 tools/referee_host.py --port /dev/ttyUSB0 i2c-write 50 10 a5 3c
python3 tools/referee_host.py --port /dev/ttyUSB0 i2c-read 50 10 2
python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/engine/spi_cpha0.s --pin 4:pp,5:pp,6:pp --equ CPOL=1 --run
python3 tools/referee_host.py --port /dev/ttyUSB0 spi 9f 00 00 00
```

The same controllers run on the Player (`--engine 1`), and the Referee can then judge the bus
it drives: route judge channels to `uio_in` with `--mux` (here channel 0 = SDA = `uio_in[3]`,
channel 1 = SCL = `uio_in[2]`):

```bash
python3 tools/referee_host.py --port /dev/ttyUSB0 load2 firmware/engine/i2c_setup_referee.s firmware/engine/i2c_controller.s --pin1 2:od,3:od --mux 0:uio3,1:uio2 --run
python3 tools/referee_host.py --port /dev/ttyUSB0 --engine 1 i2c-write 50 10 a5 3c
python3 tools/referee_host.py --port /dev/ttyUSB0 stats 0
```

The engine has one 16-bit shift register, so the host sends each data byte spread over two IN
bytes and gets the 8 samples back as two OUT bytes (`referee_host.spread` / `gather`).

## External hardware

A 3.3 V USB-serial adapter (or the demo board's RP2350 as host) for the command port.
To referee a bus, wire its lines to `ui_in[4..7]`, for example an I2C bus's SDA and SCL.
To drive one, use `uio[2..3]` (I2C, with external pull-ups) or `uio[4..7]` (SPI).
