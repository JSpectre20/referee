; Referee: I2C Fast-mode timing, eight of the nine rules, on a bus the chip is not part of.
; VARIANT "P only": every edge of SDA and SCL is a trigger, and the poison rule (P) makes it
; sound: a verdict is never about the wrong pair of edges. Kept for comparison with
; i2c_fm_referee.s (P + K), which loses far fewer events: see spike/README.md, "Findings".
; Channels: 0 = SDA (ui_in[4]), 1 = SCL (ui_in[5]); input-only pins, so non-intrusive.
;
; v1 candidates used:
;   E1  trig CH0|CH1, any      one snapshot per bus event (any edge of SDA or SCL)
;   E2  tsts                   which edge triggered; SCL's level at an SDA edge (START / STOP / data)
;   B   second loop counter    two one-shot state flags (no ALU, no memory): RC0 = "a START is
;                              waiting for its first SCL fall", RC1 = "the bus is idle (after STOP)"
;   D   guard band             the bounds below take a guard per rule (none here: g = 0)
;   P   poison, resync, desync the program keeps two bits of state across bus events. If the chip
;                              loses an event (it arrived while the previous one was being
;                              handled), that state may be wrong. The hardware then answers
;                              CAN'T TELL to every JUDGE until the program says `resync`. The
;                              program says it where its state is known whatever came before:
;                              after it has handled a START (RC0 = 1, RC1 = 0) and after a STOP
;                              (RC0 = 0, RC1 = 1). It says `desync` at power-up (it may be starting
;                              in the middle of a transfer) and when it cannot classify an event.
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
; Limits (coverage, not soundness):
; - A bus event that arrives while the previous one is still being handled (about 14 cycles,
;   230 ns) is lost; the chip counts it (LOST). From then until the next START or STOP every
;   JUDGE is CAN'T TELL (reported as "poisoned", counted apart from ordinary CAN'T TELL).
; - Edges too close together to classify (flag A) do the same through `desync`.
; - Short intervals are the ones whose end event gets lost, so the violations a bus is most
;   likely to have are the ones most likely to go unjudged. The report shows, per rule, how many
;   events were judged; lowering the loss is a performance goal for the next version.

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
        desync               ; the bus may be mid-transfer: no verdict until the first START or STOP
next:   rel
wait:   tmon 1023, x16       ; 273 us without an event: wait again
        waits
        jt   wait
        tsts 1, tfall        ; SCL fell? (tested first: the data change that follows comes soonest)
        ja   lost            ; two edges too close to say which one this event is
        jc   scl_fall
        tsts 1, trise        ; SCL rose?
        jc   scl_rise
        tsts 1, level        ; an SDA edge: was SCL high?
        ja   lost            ; can't tell whether this is data or a START / STOP
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
        jmp  sync
t_buf:  tsget X, 0, rise
        judge 8              ; tBUF
sync:   resync               ; after a START: RC0 = 1, RC1 = 0, whatever happened before
        jmp  next

stop:   tsget X, 1, rise
        tsget Y, 0, rise
        judge 7              ; tSU;STO
        ldrc 1, rc1          ; RC1 <- 1: idle
        ldrc 0               ; RC0 <- 0: no START is waiting
        jmp  sync            ; after a STOP the state is known too

lost:   desync               ; the program has lost track: CAN'T TELL until the next START or STOP
        jmp  next
