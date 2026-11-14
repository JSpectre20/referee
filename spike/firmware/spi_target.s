; SPI target (peripheral), modes 0-3 (CPOL and CPHA by .equ), MSB first, full duplex.
; Pins, the controller's seen from the other side: SCK = input 4 (uio_in[4]), MOSI = input 5,
; CS = input 6 (active low), all through the 2-flop synchronizers; MISO = output pin 7 (uio[7]),
; push-pull (it is driven also while CS is high: one target per bus).
;
; Host protocol: OUT gets one byte per byte received (a byte cut short by CS rising is dropped).
; IN gives one byte per byte sent: the target takes the first one when CS falls and each next one
; during the last bit of the byte before (CPHA 0 must drive the next byte's first bit right after
; that byte's last trailing edge, before it can know whether another byte follows). If IN is
; empty then, the byte sent is 0xFF. A byte taken for a slot that CS rising cancels is dropped:
; queue a frame's bytes before or during it, and the next frame's only once this frame's last
; received byte has come out of OUT (by then the extra take has already happened).
;
; One program for all four modes. S1 = the SCK level after the sampling edge, S2 = after the
; shifting edge. Per byte: wait for S2 and send bit 7 (CPHA 0: SCK is already at S2 when CS
; falls, and after a byte's last sample its trailing edge is the shifting edge); then 7 times
; "wait S1, sample MOSI; wait S2, send the next bit"; then the 8th sample.
;
; Every wait has a short timeout TE (re-armed after each edge). A timeout looks at CS: still low
; (a slow SCK or a paused controller) -> wait again; high -> the frame is over, back to idle (a
; partial byte is dropped). CS must stay high >= TE + 4 cycles to be seen (about 350 ns).
;
; Speed limit at 60 MHz, from the cycle counts. An SCK edge at the pad in cycle E is seen by
; `waitp` in E+2 (synchronizer), `jt` issues in E+3 and the action in E+4: `shi` reads MOSI as it
; was in E+2 (the controller must hold MOSI 3 cycles after its sampling edge), and `sho` puts
; MISO on the pad in E+5. The waits between two actions are re-entered at most 6 cycles after
; the previous edge, never late. So MISO is valid H - 5 cycles before the next (sampling) edge,
; H = SCK half period in cycles. A controller needing 2 cycles (33 ns) of MISO setup needs
; H >= 7: SCK <= 60 MHz / 14 = 4.29 MHz. The CS-to-first-edge time must be >= 11 cycles (CPHA 0:
; MISO's first bit is on the pad 9 cycles after CS falls). The test bench confirms H = 7 works
; and H = 6 fails the setup check.
;
; v1 candidate used: A. One shift register cannot shift MISO out and MOSI in at the same time
; without the spread format of v0.3, and a target cannot ask its controller for spread data.
; Clock-independent: time literals are converted to cycles at the image's clock.
.equ CPOL 0
.equ CPHA 0
.equ S1 (1 + CPOL + CPHA) & 1    ; SCK level after the sampling edge
.equ S2 (CPOL + CPHA) & 1        ; SCK level after the shifting edge
.equ TE 250ns                    ; wait timeout: how often CS is looked at

        ldsr 0xFF                ; SRO[7:0] = 0xFF: an empty IN slot sends 0xFF (see below)
fin:    ldsr 0xFF, hi            ; SRO[15:8] = 0xFF in case IN is empty when CS falls
idle:   tmon 1023, x16
        waitp 6, 0               ; CS falls
        jt   idle
        pull hi                  ; the first byte to send (MSB in SRO[15])
byte:   tmon TE
bw:     waitp 4, S2              ; shifting edge (CPHA 0: SCK is there already)
        jt   tb
        sho  7, msb              ; bit 7
        ldrc 6
tm1:    tmon TE
w1:     waitp 4, S1              ; sampling edge
        jt   t1
        shi  5, msb              ; SRI <- {SRI[14:0], MOSI}
tm2:    tmon TE
w2:     waitp 4, S2              ; shifting edge
        jt   t2
        sho  7, msb              ; the next bit; SRO refills with 1s from the right, so after
        loop tm1                 ; 8 shifts SRO[15:8] = 0xFF
        pull hi                  ; the next slot's byte (IN empty: SRO[15:8] stays 0xFF)
tml:    tmon TE
wl:     waitp 4, S1              ; the 8th sampling edge
        jt   tl
        shi  5, msb
        push                     ; the byte received: SRI[7:0]
        jmp  byte
tb:     tstp 6                   ; timeouts: CS still low? then wait again
        jnc  byte
        jmp  fin                 ; CS high: the frame is over
t1:     tstp 6
        jnc  tm1
        jmp  fin
t2:     tstp 6
        jnc  tm2
        jmp  fin
tl:     tstp 6
        jnc  tml
        jmp  fin
