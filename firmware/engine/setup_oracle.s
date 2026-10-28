; Referee, used by the chip-level oracle test (test/test.py): on every rising
; edge of channel 1 (ui_in[5], "clock"), judge the time from channel 0's
; (ui_in[4], "data") latest edge to it, against a minimum and a maximum bound,
; and stream every verdict with its exact m (push all + raw), so the test can
; check each one against the true interval it computed from its own stimulus.
.clock_hz 60e6
.bound 0, min, 400ns             ; 24 cycles, exact
.bound 1, max, 700ns             ; 42 cycles, exact

        trig 1, rise
loop:   tmon 1023, x16
        waits
        jt   loop                ; no clock edge for 273 us: wait again
        tsget X, 0, last         ; data's latest edge before this clock rise
        tsget Y, 1, rise         ; this clock rise (the trigger edge: never ORD)
        judge 0, raw
        judge 1, raw
        rel
        jmp  loop
