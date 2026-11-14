; Referee: I2C data setup time (tSU;DAT) on every SCL rise (docs/isa-timing.md section 5.3).
; Channels: 0 = SDA (ui_in[4]), 1 = SCL (ui_in[5]); non-intrusive.
; Clock-independent: assembled for the chip's clock (referee_host.py takes it from info.yaml;
; refasm.py --clock-hz). Time literals and BIT are converted to cycles at that clock.
.bound 0, min, 100ns         ; Fast-mode tSU;DAT

        trig 1, rise         ; snapshot on every SCL rise
loop:   tmon 1023, x16       ; 273 us without an SCL rise = bus idle
        waits
        jt   bus_idle
        tsget X, 0, last     ; SDA's latest edge before this SCL rise
        tsget Y, 1, rise     ; this SCL rise
        judge 0, nonpass     ; only VIOLATION / INDETERMINATE reach the host
        rel
        jmp  loop
bus_idle:
        jmp  loop
