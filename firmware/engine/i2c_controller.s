; I2C controller (Fast-mode, 400 kHz), driven by the host through the IN FIFO.
; SCL = output pin 2 (uio[2]), SDA = output pin 3 (uio[3]), both open-drain (global
; config mode 2); they read back as inputs 2 and 3, so clock stretching and the
; target's ACK / data are seen on the bus itself.
;
; Commands (one IN byte each; results go to OUT):
;   'S'           START (also a repeated START)
;   'P'           STOP
;   'X' hi lo     clock 8 bits, MSB first. The data byte comes spread over two IN
;                 bytes (referee_host.spread: t7 0 t6 0 ... t0 0) and 8 SDA samples
;                 come back as two OUT bytes (referee_host.gather). To read a byte
;                 from the target, send 0xFF: SDA stays released for all 8 bits.
;   'A'           one clock with SDA released, sampling it: the target's ACK after a
;                 write, or the controller's NACK after a read. Two OUT bytes; the
;                 sample is bit 0 of the second (0 = ACK).
;   'K'           one clock with SDA low: the controller's ACK after a read. Two OUT
;                 bytes, ignored.
; Why the spread data: SR is the only shift register, and every bit needs one shift
; out (sho) and one shift in (shi); with a 0 between data bits, two shifts per bit
; move the next data bit up while the samples collect at the bottom.
;
; Timing (Fast-mode: tLOW >= 1.3 us, tHIGH >= 0.6 us, tSU;STA, tHD;STA, tSU;STO >= 0.6 us,
; tBUF >= 1.3 us, tSU;DAT >= 100 ns): every high phase is timed from the moment SCL
; is SEEN high (waitp), so a stretching target only makes it longer. A target holding
; SCL low for more than ~400 us (the command's timeout) is not waited for.
; Clock-independent: time literals are converted at the chip's clock (info.yaml).
.equ TLOW 1300ns
.equ THIGH 700ns
.equ TSU 700ns

        set  2, 1                ; bus idle: SCL and SDA released
        set  3, 1
cmd:    pull                     ; next command
        je   idle
        tmon 1023, x16           ; stretching budget for this command
        cmp  0x53                ; 'S'
        jc   start
        cmp  0x50                ; 'P'
        jc   stop
        cmp  0x58                ; 'X'
        jc   xfer
        cmp  0x4B                ; 'K'
        jc   ack
        cmp  0x41                ; 'A'
        jc   nack
idle:   mark                     ; nothing to do (or an unknown command): look again
        waitd 2us
        jmp  cmd

start:  set  3, 1                ; SDA up first (repeated START: SCL is low here)
        mark
        waitd TLOW
        set  2, 1
        waitp 2, 1               ; SCL really high (a target may stretch)
        mark
        waitd TSU                ; tSU;STA
        set  3, 0                ; START: SDA falls while SCL is high
        waitd TSU                ; tHD;STA
        set  2, 0
        jmp  cmd

stop:   set  3, 0                ; SDA low while SCL is low
        mark
        waitd TLOW
        set  2, 1
        waitp 2, 1
        mark
        waitd TSU                ; tSU;STO
        set  3, 1                ; STOP: SDA rises while SCL is high
        waitd TLOW               ; tBUF before the next START
        jmp  cmd

xfer:   pull hi                  ; spread data, high half
        je   xfer                ; (the host sends it right after 'X')
xlo:    pull                     ; low half
        je   xlo
        ldrc 7                   ; 8 bits
        jmp  bits
ack:    ldsr 0x00, hi            ; SR[15] = 0: SDA driven low for one clock
        ldrc 0
        jmp  bits
nack:   ldsr 0xFF, hi            ; SR[15] = 1: SDA released for one clock
        ldrc 0
bits:   sho  3, msb              ; SDA <- bit (SCL is low): tHD;DAT = a few cycles
        mark
        waitd TLOW               ; ... then tLOW of setup before SCL rises
        set  2, 1
        waitp 2, 1               ; SCL seen high (stretching)
        mark
        waitd THIGH
        shi  3, msb              ; sample SDA near the end of the high phase
        set  2, 0
        loop bits
        push hi                  ; the samples, spread: 1 s7 1 s6 ... 1 s0
        push
        jmp  cmd
