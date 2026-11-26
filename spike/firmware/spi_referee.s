; Referee: SPI timing on a bus the chip is not part of, modes 0 and 3 (data sampled on SCK's
; rising edge, shifted on its falling edge; for modes 1 and 2 swap `trise` and `tfall` below and
; the two handlers' edge names).
; Channels: 0 = CS (active low, ui_in[4]), 1 = SCK (ui_in[5]), 2 = MOSI (ui_in[6]); input-only
; pins, so non-intrusive.
;
; Six rules, every interval "X = earlier edge, Y = the edge that just happened" (JUDGE: Y - X):
;   SCK rise (sampling):  tCL (2) = SCK fall -> SCK rise;  tSU (0) = MOSI's last edge -> SCK rise
;   SCK fall:             tCH (1) = SCK rise -> SCK fall
;   first SCK edge of a frame: tCSS (3) = CS fall -> that edge (+ tSU if it is a rise). It is not
;                         a tCL / tCH: the edge before it belongs to the previous frame
;   CS rise:              tCSH (4) = SCK's last edge -> CS rise (only if the frame had an SCK edge)
;   CS fall:              tCSD (5) = CS rise -> CS fall (CS high time between frames)
; Not judged: MOSI hold after the sampling edge. MOSI edges are not events here, and at the next
; SCK edge "did MOSI change before or after this sampling edge" needs candidate J.
;
; The bounds are examples in the style of a 2 MHz SPI EEPROM's datasheet; the test draws every
; interval around whatever they are.
;
; Speed limits (measured by the two sweeps in spike/tests/test_ref_spi.py, 60 MHz):
;   - every SCK edge is an event, and the program follows SCK half periods of 14 cycles or more
;     without losing one: SCK up to about 2.1 MHz, the same at queue depth 1 and 2 (a queue
;     absorbs bursts, not a faster steady rate). Faster SPI buses need the rules checked in
;     hardware.
;   - the frame's first SCK edge arrives while the CS fall is being handled: at queue depth 1 it
;     needs tCSS of 14 cycles or more (233 ns; the bound here is 250 ns = 15 cycles), at depth 2
;     none down to 10 cycles. A lost first edge poisons the rest of the frame (coverage, never a
;     wrong verdict).
; Interpretation to confirm against the datasheet in use: tCSS here ends at the frame's first SCK
; edge and tCSH starts at its last, whichever direction they are; flash-style datasheets (tSLCH,
; tCHSH) measure both to SCK's RISING edges.
;
; v1 candidates used:
;   E1  trig CH0, any            every CS edge (v0.3 could trigger on one edge kind only)
;   K   trigq CH1, any, 0, 0     SCK edges only while CS is low: the clock of another target's
;                                frame, or a bus idling, is never an event
;   E2  tsts                     which edge this event is; flag A: two event edges too close to order
;   P   resync / desync          one bit of state (RC0 = "the next SCK edge is the frame's first"),
;                                fully set by every CS fall: `resync` there. RUN starts poisoned
;   D   guard band               per bound (none declared here: g = 0)

.bound 0, min, 50ns          ; tSU   MOSI setup before the sampling edge
.bound 1, min, 250ns         ; tCH   SCK high
.bound 2, min, 250ns         ; tCL   SCK low
.bound 3, min, 250ns         ; tCSS  CS fall -> first SCK edge
.bound 4, min, 250ns         ; tCSH  last SCK edge -> CS rise
.bound 5, min, 500ns         ; tCSD  CS high between frames

        trig  CH0, any       ; every CS edge (TRIG clears the second term: set it first)
        trigq CH1, any, 0, 0 ; SCK edges while CS is low
next:   rel
wait:   tmon 1023, x16       ; 273 us without an event: wait again
        waits
        jt   wait
        tsts 1, trise        ; SCK rose? (A: another armed edge too close to order with this one)
        ja   lost
        jc   rise
        tsts 1, tfall        ; SCK fell?
        jc   fall
        tsts 0, tfall        ; CS fell?
        jc   cs_fall

        loop next            ; CS rose. RC0 = 1: no SCK edge in this frame, nothing to judge
        tsget Y, 0, rise
        tsget X, 1, last
        judge 4              ; tCSH
        jmp  next

rise:   loop first           ; RC0 = 1: the frame's first SCK edge (RC0 <- 0)
        tsget Y, 1, rise
        tsget X, 1, fall
        judge 2              ; tCL
        tsget X, 2, last
        rel                  ; everything is read: let the next event in before judging
        judge 0              ; tSU
        jmp  wait

fall:   loop first
        tsget Y, 1, fall
        tsget X, 1, rise
        rel
        judge 1              ; tCH
        jmp  wait

first:  tsget Y, 1, last     ; this SCK edge (the trigger: whichever of rise / fall came last)
        tsget X, 0, fall
        judge 3              ; tCSS
        tsts 1, trise        ; mode 0: the first edge samples data, so tSU too
        jnc  next
        tsget X, 2, last
        rel
        judge 0              ; tSU
        jmp  wait

cs_fall:
        tsget Y, 0, fall
        tsget X, 0, rise
        resync               ; state known from this event alone; tCSD uses only CS's own edges
        ldrc 1               ; RC0 <- 1: the next SCK edge is the frame's first
        rel                  ; before the JUDGE: the first SCK edge may follow CS soon
        judge 5              ; tCSD
        jmp  wait

; Two armed edges too close to order: CS and SCK within a cycle (illegal on any SPI bus: tCSS or
; tCSH near 0; or another target's SCK next to our CS edge), or an SCK pulse under 2 cycles. The
; program cannot tell whether the SCK edge belongs to the frame, or may never see it as an event
; of its own (a CS fall with the first SCK edge one cycle later would make the second edge look
; like the first). Lost track until the next CS fall.
lost:   desync
        jmp  next
