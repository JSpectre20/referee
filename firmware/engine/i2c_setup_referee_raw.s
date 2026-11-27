; Referee: I2C data setup time (tSU;DAT), as i2c_setup_referee.s but every non-PASS verdict
; carries its exact m (the `raw` slot): the evidence variant for ILA-checked board runs
; (fpga/README.md step 5, tools/ila_check.py). With header-only verdicts m saturates at 3
; cycles, so the checker can place a VIOLATION on the wrong rise; the exact m pins it down.
; Each non-PASS verdict takes 2 of the FIFO's 8 slots: drain after at most 4.
; Channels: 0 = SDA (ui_in[4]), 1 = SCL (ui_in[5]); non-intrusive.
.bound 0, min, 100ns         ; Fast-mode tSU;DAT

        trig 1, rise         ; snapshot on every SCL rise
loop:   tmon 1023, x16       ; 273 us without an SCL rise = bus idle
        waits
        jt   bus_idle
        tsget X, 0, last     ; SDA's latest edge before this SCL rise
        tsget Y, 1, rise     ; this SCL rise
        judge 0, raw, nonpass ; only VIOLATION / INDETERMINATE, each with its exact m
        rel
        jmp  loop
bus_idle:
        jmp  loop
