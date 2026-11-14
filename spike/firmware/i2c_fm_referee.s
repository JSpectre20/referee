; Referee: I2C Fast-mode timing, seven of the nine rules, on a bus the chip is not part of.
; Channels: 0 = SDA (ui_in[4]), 1 = SCL (ui_in[5]); input-only pins, so non-intrusive.
;
; v1 candidates used:
;   K   trigq                  the events are I2C's own: every SCL edge, and SDA edges only while
;                              SCL is high (START and STOP). A data edge is never an event, so it
;                              can never crowd out the SCL rise that ends tSU;DAT.
;   E2  tsts                   which edge this event is
;   B   second loop counter    two bits of state: RC0 = "a START is waiting for its first SCL
;                              fall", RC1 = "the bus is idle (after a STOP)"
;   P   poison, resync, desync if the chip loses an event, that state may be wrong: the hardware
;                              answers CAN'T TELL to every JUDGE until the program says `resync`,
;                              which it does while it holds a START (RC0 = 1, RC1 = 0) or a STOP
;                              (RC0 = 0, RC1 = 1), where its state is known whatever came before.
;                              `desync` when two edges next to an SCL RISE are too close to
;                              order. (RUN starts poisoned: the bus may be mid-transfer.)
;   receiver rule              an SDA edge within a cycle of SCL's FALL is read as data, as the
;                              I2C specification tells receivers to (see `near:` below): a legal
;                              hold time of 0 ns must not stop the referee.
;   D   guard band             per bound (none declared here: g = 0)
;
; Every interval is "X = earlier edge, Y = the edge that just happened"; JUDGE computes Y - X.
;   SCL rise:    tLOW (3) = SCL fall -> SCL rise;  tSU;DAT (0) = SDA's last edge -> SCL rise
;   SCL fall:    after a START: tHD;STA (5) = START -> SCL fall;  otherwise tHIGH (4)
;   START:       bus was idle: tBUF (8) = STOP -> START;  else tSU;STA (6) = SCL rise -> START
;   STOP:        tSU;STO (7) = SCL rise -> STOP
; Not judged by this version:
;   tHD;DAT (1): SCL fall -> SDA change. With no event at the data edge it has to be judged at
;                the next SCL rise, and only if SDA changed during this low period, which needs
;                "did this edge come after that one" (candidate J).
;   tVD;DAT (2): applies to data and ACK bits only; needs to know that no STOP or repeated START
;                follows (see spike/README.md).
; Coverage limit: an event that arrives while the previous one is being handled (the snapshot is
; released 11 to 13 cycles, about 200 ns, after the edge is reported) is lost; then every JUDGE is
; CAN'T TELL ("poisoned") until the next START or STOP. That takes an interval of under about
; 250 ns between two events: a third of tHIGH's minimum.

.bound 0, min, 100ns         ; tSU;DAT
.bound 3, min, 1300ns        ; tLOW
.bound 4, min, 600ns         ; tHIGH
.bound 5, min, 600ns         ; tHD;STA
.bound 6, min, 600ns         ; tSU;STA
.bound 7, min, 600ns         ; tSU;STO
.bound 8, min, 1300ns        ; tBUF

        trig  CH1, any       ; every SCL edge
        trigq CH0, any, 1, 1 ; SDA edges while SCL is high: START and STOP
next:   rel                  ; (RUN starts poisoned: no verdict until the first START or STOP)
wait:   tmon 1023, x16       ; 273 us without an event: wait again
        waits
        jt   wait
        tsts 1, tfall        ; SCL fell?
        ja   near            ; another SDA or SCL edge within a cycle of this event
        jc   scl_fall
        tsts 1, trise        ; SCL rose?
        jc   scl_rise
        tsts 0, trise        ; an SDA edge while SCL is high: a STOP if SDA rose
        jc   stop

; START
        tsget Y, 0, fall
        ldrc 1               ; RC0 <- 1: the next SCL fall ends tHD;STA
        loop t_buf, rc1      ; RC1 = 1: the bus was idle (RC1 <- 0 either way)
        tsget X, 1, rise
        judge 6              ; tSU;STA (a repeated START)
        jmp  sync
t_buf:  tsget X, 0, rise
        judge 8              ; tBUF
sync:   resync               ; state known from this event alone (while its snapshot is held)
        jmp  next

stop:   tsget X, 1, rise
        tsget Y, 0, rise
        judge 7              ; tSU;STO
        ldrc 1, rc1          ; RC1 <- 1: idle
        ldrc 0               ; RC0 <- 0: no START is waiting
        jmp  sync

scl_rise:
        tsget Y, 1, rise
        tsget X, 1, fall
        judge 3              ; tLOW
        tsget X, 0, last
        rel                  ; everything is read: let the next event in before judging
        judge 0              ; tSU;DAT
        jmp  wait

scl_fall:
        tsget Y, 1, fall
        loop hd_sta          ; RC0 = 1: the first SCL fall after a START
        tsget X, 1, rise
        rel
        judge 4              ; tHIGH
        jmp  wait
hd_sta: tsget X, 0, fall
        rel
        judge 5              ; tHD;STA
        jmp  wait

; Two edges too close to order. If SCL is low after them, SCL fell and SDA changed next to it.
; I2C lets a data hold time be 0 ns, and tells every receiver how to read this: UM10204 requires
; a device to hold SDA internally for at least 300 ns "to bridge the undefined region of the
; falling edge of SCL" (note to tHD;DAT in the bus-line characteristics table). So an SDA edge
; next to SCL's fall is a data change, never a START or STOP: the event is an SCL fall, and the
; protocol state moves on as for any SCL fall. The verdict that uses the ambiguous SCL edge is
; CAN'T TELL by the ORD flag; nothing is poisoned.
near:   tsts 1, level
        jnc  scl_fall
; SCL is high after them: an SDA edge next to SCL's RISE (a START or STOP with no setup time, or
; data changing as SCL rises: both illegal, and not tellable apart), or a glitch.
lost:   desync               ; lost track: CAN'T TELL until the next START or STOP
        jmp  next
