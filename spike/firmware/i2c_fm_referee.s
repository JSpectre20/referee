; Referee: I2C Fast-mode timing, eight of the nine rules, on a bus the chip is not part of.
; FIRST VERSION, KNOWN TO BE UNSAFE ON A BUS WITH VIOLATIONS: see "Limits" below and
; spike/README.md, "Findings". Kept as the record of what the spike found; to be redesigned.
; Channels: 0 = SDA (ui_in[4]), 1 = SCL (ui_in[5]); input-only pins, so non-intrusive.
;
; v1 candidates used:
;   E1  trig CH0|CH1, any      one snapshot per bus event (any edge of SDA or SCL)
;   E2  tsts                   which edge triggered; SCL's level at an SDA edge (START / STOP / data)
;   B   second loop counter    two one-shot state flags (no ALU, no memory): RC0 = "a START is
;                              waiting for its first SCL fall", RC1 = "the bus is idle (after STOP)"
;   D   guard band             the bounds below take a guard per rule (none here: g = 0)
;
; Every interval is "X = earlier edge, Y = the edge that just happened"; JUDGE computes Y - X.
;   SCL rise:               tLOW (3) = SCL fall -> SCL rise;  tSU;DAT (0) = SDA's last edge -> SCL rise
;   SCL fall:               after a START: tHD;STA (5) = START -> SCL fall;  otherwise tHIGH (4)
;   SDA edge, SCL low:      tHD;DAT (1) = SCL fall -> SDA edge
;                           tVD;DAT (2, max 900 ns) is NOT judged: the spec applies it to data and
;                           ACK bits only, and at this edge the program cannot know whether SDA is
;                           changing for a data bit or to prepare a STOP / repeated START
;   SDA fall, SCL high:     START. Bus was idle: tBUF (8) = STOP -> START; else tSU;STA (6) = SCL rise -> START
;   SDA rise, SCL high:     STOP.  tSU;STO (7) = SCL rise -> STOP
; Limits:
; - An SDA edge so close to an SCL edge that SCL's level at that moment can't be known (flag A)
;   is not classified: no verdict is given for it (one event fewer, not a wrong one).
; - A bus event that arrives while the previous one is still being handled (about 14 cycles) is
;   lost; the chip counts it (LOST). THIS CAN MAKE LATER VERDICTS WRONG: the two state flags
;   then disagree with the bus, and the next SCL fall can be judged as tHD;STA against an SDA
;   fall that was not a START. On traffic where no event is lost (LOST = 0) every verdict is
;   right; with LOST > 0 the verdicts of this version must not be trusted.

.bound 0, min, 100ns         ; tSU;DAT
.bound 1, min, 0ns           ; tHD;DAT
.bound 2, max, 900ns         ; tVD;DAT
.bound 3, min, 1300ns        ; tLOW
.bound 4, min, 600ns         ; tHIGH
.bound 5, min, 600ns         ; tHD;STA
.bound 6, min, 600ns         ; tSU;STA
.bound 7, min, 600ns         ; tSU;STO
.bound 8, min, 1300ns        ; tBUF

        trig CH0|CH1, any
        ldrc 1, rc1          ; at power-up treat the bus as idle
next:   rel
wait:   tmon 1023, x16       ; 273 us without an event: wait again
        waits
        jt   wait
        tsts 1, tfall        ; SCL fell? (tested first: the data change that follows comes soonest)
        jc   scl_fall
        tsts 1, trise        ; SCL rose?
        jc   scl_rise
        tsts 1, level        ; an SDA edge: was SCL high?
        ja   next            ; can't tell: no verdict
        jc   start_stop

; SDA changed while SCL was low: a data bit
        tsget X, 1, fall
        tsget Y, 0, last
        judge 1              ; tHD;DAT
        jmp  next

scl_fall:
        tsget Y, 1, fall
        loop hd_sta          ; RC0 = 1: this is the first SCL fall after a START
        tsget X, 1, rise
        judge 4              ; tHIGH
        jmp  next
hd_sta: tsget X, 0, fall
        judge 5              ; tHD;STA
        jmp  next

scl_rise:
        tsget Y, 1, rise
        tsget X, 1, fall
        judge 3              ; tLOW
        tsget X, 0, last
        judge 0              ; tSU;DAT
        jmp  next

start_stop:
        tsts 0, trise        ; SDA rose with SCL high: STOP
        jc   stop
; START
        tsget Y, 0, fall
        ldrc 1               ; RC0 <- 1: the next SCL fall ends tHD;STA
        loop t_buf, rc1      ; RC1 = 1: the bus was idle
        tsget X, 1, rise
        judge 6              ; tSU;STA (a repeated START)
        jmp  next
t_buf:  tsget X, 0, rise
        judge 8              ; tBUF
        jmp  next

stop:   tsget X, 1, rise
        tsget Y, 0, rise
        judge 7              ; tSU;STO
        ldrc 1, rc1          ; RC1 <- 1: idle
        jmp  next
