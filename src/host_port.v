/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * host_port (v2): binary command port over the UART for engine_sys
 * (docs/isa-timing.md section 7).
 *
 * Every command is a 4-byte packet  CMD A B C  and gets a reply that starts
 * STATUS DATA, where STATUS is ACK (0x06) or NAK (0x15). Only the drain
 * reply is longer. `e` is the engine number: v0 has engine 0 only, any other
 * e is NAK'd.
 *
 *   CMD        A        B        C       action                                 DATA
 *   'I' 0x49   -        -        -       identify                               0x52 ('R')
 *   'G' 0x47   addr     hi       lo      global config word addr <= {hi,lo}     addr
 *                                        (addr 0..7; NAK unless halted)
 *   'W' 0x57   e:addr   hi       lo      engine image word <= {hi,lo}           A
 *                                        (e = A[7], addr = A[6:0] 0..96; NAK unless halted)
 *   'r' 0x72   sel      addr     part    read a word: sel 0 global, 1 engine 0;  byte
 *                                        addr 0x80 = the CRC the last RUN check
 *                                        computed; part 0 low byte, 1 high byte
 *                                        (NAK unless halted)
 *   'C' 0x43   sel      hi       lo      expected CRC: sel 0 global, 1 engine 0  sel
 *                                        (NAK unless halted)
 *   'R' 0x52   e        op       -       op 1: RUN. The reply waits for the      op
 *                                        105-cycle image check: ACK only if both
 *                                        CRCs and the pin claims passed (NAK and
 *                                        CRC_FAIL otherwise). op 0: halt.
 *   'P' 0x50   e        byte     -       push byte into IN (NAK if full)        byte
 *   'p' 0x70   e        -        -       pop OUT (NAK if empty)                 the byte
 *   'D' 0x44   e        max      -       VERDICT drain: reply ACK, n, then      n
 *                                        n x {hi, lo} slots, n = min(max, level)
 *   'S' 0x53   e        slot     -       latch stats slot 0..3 (atomic)         slot
 *   's' 0x73   e        idx      part    read latched stats word idx 0..4        byte
 *                                        (pass, viol, indet, min, max); part as 'r'
 *   'Z' 0x5A   e        slot     -       clear stats slot                       slot
 *   'L' 0x4C   e        op       -       LOST; op 1 also clears it              LOST
 *   'B' 0x42   div_hi   div_lo   -       UART bit period in cycles (>= 6),       div_lo
 *                                        applied after this reply has been sent
 *   'Q' 0x51   e        sel      -       sel 0: engine status, read-and-clear of the sticky bits:
 *                                          {running, S, CRC_FAIL, PIN_FAULT,
 *                                           IN_UNDERRUN, VERDICT_OVF, OUT_OVF, NI}
 *                                        sel 1: port status {overrun, 0000000},
 *                                          read-and-clear
 *   other                                NAK                                    CMD
 *
 * Rules
 *   - Wait for the whole reply before sending the next packet. A packet that
 *     completes while a reply is still going out is dropped and sets
 *     `overrun` ('Q' e 1 reads and clears it).
 *   - Resync: a packet that is not complete within TIMEOUT cycles of its
 *     previous byte is thrown away (a host that lost count pauses ~2.2 ms at
 *     60 MHz). A UART framing error also throws the partial packet away.
 *   - Image and config writes are also refused inside engine_sys / engine
 *     unless halted (hardware write gates); the NAK here just tells the host.
 *   - Every strobe to engine_sys is a one-cycle pulse in the cycle the 4th
 *     byte arrives, with its address/data taken from that packet. A read and
 *     its clear ('Q', 'L') happen in that same cycle, so nothing is lost
 *     between them; a hardware set in that very cycle loses to the clear
 *     (docs/isa-timing.md 0b, diff 6).
 */

`default_nettype none

module host_port #(
    parameter integer TIMEOUT_W = 17,   // resync after 2**17 cycles = 2.2 ms at 60 MHz
    parameter integer DIV_RESET = 521   // power-up bit period: 115200 baud at 60 MHz (set by the top)
) (
    input  wire        clk,
    input  wire        rst_n,

    // UART
    input  wire [ 7:0] rx_data,
    input  wire        rx_valid,
    input  wire        rx_ferr,
    output reg  [ 7:0] tx_data,
    output wire        tx_start,
    input  wire        tx_busy,
    output reg  [15:0] div,        // UART bit period in cycles

    // engine_sys: global image
    output wire        h_gwe,
    output wire [ 2:0] h_gaddr,
    output wire [15:0] h_gwdata,
    input  wire [15:0] h_grdata,
    output wire        h_gcrc_we,
    output wire [15:0] h_gcrc,

    // engine_sys: engine 0
    output wire        h_we,
    output wire [ 6:0] h_addr,
    output wire [15:0] h_wdata,
    output wire [ 6:0] h_raddr,
    input  wire [15:0] h_rdata,
    output wire        h_crc_we,
    output wire [15:0] h_crc,
    output wire        h_run,
    output wire        h_halt,
    output wire        h_in_push,
    output wire [ 7:0] h_in_data,
    input  wire        in_full,
    output wire        h_out_pop,
    input  wire [ 7:0] out_data,
    input  wire        out_empty,
    output wire        h_vf_pop,
    input  wire [15:0] vf_data,
    input  wire [ 3:0] vf_count,
    output wire        h_stats_latch,
    output wire [ 1:0] h_stats_slot,
    output wire [ 2:0] h_stats_idx,
    input  wire [15:0] stats_rdata,
    output wire        h_stats_clear,
    output wire        h_lost_clear,
    input  wire [ 7:0] lost,
    output wire        h_status_clear,
    input  wire        halted,
    input  wire        running,
    input  wire        checking,
    input  wire [15:0] crc_got_g,
    input  wire [15:0] crc_got_e,
    input  wire [ 7:0] status,     // {checking, running, S, CRC_FAIL, PIN_FAULT, IN_UNDERRUN, VERDICT_OVF, OUT_OVF}
    input  wire        ni
);

  localparam [7:0] ACK = 8'h06, NAK = 8'h15, ID = 8'h52;
  localparam [7:0] C_ID = 8'h49, C_G = 8'h47, C_W = 8'h57, C_RD = 8'h72, C_CRC = 8'h43, C_RUN = 8'h52;
  localparam [7:0] C_PUSH = 8'h50, C_POP = 8'h70, C_DRAIN = 8'h44, C_SLATCH = 8'h53, C_SREAD = 8'h73;
  localparam [7:0] C_SCLR = 8'h5A, C_LOST = 8'h4C, C_BAUD = 8'h42, C_Q = 8'h51;

  // ---------------------------------------------------------------------------
  // Packet assembly (as in v1)
  // ---------------------------------------------------------------------------
  reg [7:0] b0, b1, b2;  // first three bytes of the packet
  reg [1:0] nbytes;  // bytes received so far (0..3)
  reg [TIMEOUT_W-1:0] idle;  // cycles since the last byte of a partial packet
  wire timed_out = &idle;

  wire packet = rx_valid && (nbytes == 2'd3);  // 4th byte arrives this cycle

  // ---------------------------------------------------------------------------
  // Reply: STATUS, DATA, then (drain only) 2 bytes per verdict slot
  // ---------------------------------------------------------------------------
  localparam [1:0] R_IDLE = 2'd0, R_RUNWAIT = 2'd1, R_SEND = 2'd2;
  reg [1:0] rstate;
  reg [7:0] r_status, r_data;
  reg [4:0] ridx, rlen;  // byte being sent / reply length (2 .. 18)
  wire replying = (rstate != R_IDLE);
  reg overrun;
  reg baud_pend;  // a new bit period waits for the reply to finish
  reg [15:0] div_next;

  // Start a byte whenever one is waiting and the transmitter is free. The
  // transmitter's busy rises the cycle after start, so the next byte simply
  // waits for busy to fall again.
  assign tx_start = (rstate == R_SEND) && !tx_busy;
  always @(*) begin
    case (ridx)
      5'd0: tx_data = r_status;
      5'd1: tx_data = r_data;
      default: tx_data = ridx[0] ? vf_data[7:0] : vf_data[15:8];  // drain slots, high byte first
    endcase
  end
  // A drained slot leaves the FIFO as its low byte starts (uart_tx samples
  // the byte in that same cycle).
  assign h_vf_pop = tx_start && (ridx >= 5'd2) && ridx[0];

  // ---------------------------------------------------------------------------
  // Command decode (combinational, used in the cycle the 4th byte arrives)
  // ---------------------------------------------------------------------------
  wire [7:0] cmd = b0, a = b1, bb = b2, c = rx_data;
  wire e_ok = (a == 8'd0);  // engine 0 only
  wire [3:0] drain_n = (bb < {4'd0, vf_count}) ? bb[3:0] : vf_count;  // min(max, FIFO level <= 8)
  wire [15:0] rd_word = (a == 8'd0) ? ((bb == 8'h80) ? crc_got_g : h_grdata) : ((bb == 8'h80) ? crc_got_e : h_rdata);
  wire [15:0] new_div = {a, bb};

  reg [7:0] st, dt;  // reply for this packet
  always @(*) begin
    st = ACK;
    dt = 8'h00;
    case (cmd)
      C_ID: dt = ID;
      C_G: begin
        dt = a;
        if (!halted || a > 8'd7) st = NAK;
      end
      C_W: begin
        dt = a;
        if (!halted || a[7] || a[6:0] > 7'd96) st = NAK;
      end
      C_RD: begin
        dt = c[0] ? rd_word[15:8] : rd_word[7:0];
        if (!halted || a > 8'd1 || (bb != 8'h80 && (a == 8'd0 ? bb > 8'd7 : bb > 8'd96))) st = NAK;
      end
      C_CRC: begin
        dt = a;
        if (!halted || a > 8'd1) st = NAK;
      end
      C_RUN: begin
        dt = {7'd0, bb[0]};
        if (!e_ok || (bb[0] && !halted)) st = NAK;
      end
      C_PUSH: begin
        dt = bb;
        if (!e_ok || in_full) st = NAK;
      end
      C_POP: begin
        dt = out_empty ? 8'h00 : out_data;  // never the stale storage of an empty FIFO
        if (!e_ok || out_empty) st = NAK;
      end
      C_DRAIN: begin
        dt = {4'd0, drain_n};
        if (!e_ok) st = NAK;
      end
      C_SLATCH, C_SCLR: begin
        dt = bb;
        if (!e_ok || bb > 8'd3) st = NAK;
      end
      C_SREAD: begin
        dt = c[0] ? stats_rdata[15:8] : stats_rdata[7:0];
        if (!e_ok || bb > 8'd4) st = NAK;
      end
      C_LOST: begin
        dt = lost;
        if (!e_ok) st = NAK;
      end
      C_BAUD: begin
        dt = bb;
        if (new_div < 16'd6) st = NAK;
      end
      C_Q: begin
        dt = bb[0] ? {overrun, 7'd0} : {status[6:0], ni};
        if (!e_ok || bb > 8'd1) st = NAK;
      end
      default: begin
        st = NAK;
        dt = cmd;
      end
    endcase
  end

  wire exec = packet && !replying;  // a packet we will act on
  wire go = exec && (st == ACK);  // ... and carry out

  // Strobes and their operands, straight from the packet (one-cycle pulses).
  assign h_gwe = go && cmd == C_G;
  assign h_gaddr = (cmd == C_RD) ? bb[2:0] : a[2:0];
  assign h_gwdata = {bb, c};
  assign h_gcrc_we = go && cmd == C_CRC && a == 8'd0;
  assign h_gcrc = {bb, c};
  assign h_we = go && cmd == C_W;
  assign h_addr = a[6:0];
  assign h_wdata = {bb, c};
  assign h_raddr = bb[6:0];
  assign h_crc_we = go && cmd == C_CRC && a == 8'd1;
  assign h_crc = {bb, c};
  assign h_run = go && cmd == C_RUN && bb[0];
  assign h_halt = go && cmd == C_RUN && !bb[0];
  assign h_in_push = go && cmd == C_PUSH;
  assign h_in_data = bb;
  assign h_out_pop = go && cmd == C_POP;
  assign h_stats_latch = go && cmd == C_SLATCH;
  assign h_stats_clear = go && cmd == C_SCLR;
  assign h_stats_slot = bb[1:0];
  assign h_stats_idx = bb[2:0];
  assign h_lost_clear = go && cmd == C_LOST && bb[0];
  assign h_status_clear = go && cmd == C_Q && !bb[0];

  always @(posedge clk) begin
    if (!rst_n) begin
      b0 <= 8'd0;
      b1 <= 8'd0;
      b2 <= 8'd0;
      nbytes <= 2'd0;
      idle <= {TIMEOUT_W{1'b0}};
      rstate <= R_IDLE;
      r_status <= 8'd0;
      r_data <= 8'd0;
      ridx <= 5'd0;
      rlen <= 5'd0;
      overrun <= 1'b0;
      baud_pend <= 1'b0;
      div_next <= DIV_RESET[15:0];
      div <= DIV_RESET[15:0];
    end else begin
      // Byte collection, timeout and framing-error resync.
      if (rx_ferr || (nbytes != 2'd0 && timed_out)) begin
        nbytes <= 2'd0;
        idle   <= {TIMEOUT_W{1'b0}};
      end else if (rx_valid) begin
        case (nbytes)
          2'd0: b0 <= rx_data;
          2'd1: b1 <= rx_data;
          2'd2: b2 <= rx_data;
          default: ;
        endcase
        nbytes <= nbytes + 2'd1;  // wraps to 0 after the 4th byte
        idle   <= {TIMEOUT_W{1'b0}};
      end else if (nbytes != 2'd0) begin
        idle <= idle + 1'b1;
      end

      // Execute a complete packet.
      if (packet && replying) overrun <= 1'b1;
      if (exec) begin
        r_status <= st;
        r_data   <= dt;
        ridx     <= 5'd0;
        rlen     <= (go && cmd == C_DRAIN) ? 5'd2 + {drain_n, 1'b0} : 5'd2;
        // RUN: hold the reply until the image check is over (checking is
        // high from the next cycle until the engine runs or refuses).
        rstate   <= h_run ? R_RUNWAIT : R_SEND;
        if (go && cmd == C_Q && bb[0]) overrun <= 1'b0;
        if (go && cmd == C_BAUD) begin
          baud_pend <= 1'b1;
          div_next  <= new_div;
        end
      end

      if (rstate == R_RUNWAIT && !checking) begin
        r_status <= running ? ACK : NAK;
        rstate   <= R_SEND;
      end

      // Reply sequencing.
      if (tx_start) begin
        ridx <= ridx + 5'd1;
        if (ridx + 5'd1 == rlen) rstate <= R_IDLE;
      end

      // A new baud rate takes effect once the reply is completely sent.
      if (baud_pend && rstate == R_IDLE && !tx_busy) begin
        div <= div_next;
        baud_pend <= 1'b0;
      end
    end
  end

  wire _unused = &{1'b0, status[7], c[7:1]};

endmodule

`default_nettype wire
