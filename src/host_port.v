/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * host_port: binary command port over the UART, used to load and run `seq`.
 *
 * Every command is a 4-byte packet  CMD A B C  and gets a 2-byte reply
 * STATUS DATA, where STATUS is ACK (0x06) or NAK (0x15).
 *
 *   CMD        A      B     C      action                            DATA
 *   'I' 0x49   -      -     -      identify                          0x52 ('R')
 *   'W' 0x57   addr   hi    lo     program word addr <= {hi,lo}      addr
 *                                  (NAK while running or addr >= 32)
 *   'S' 0x53   byte   -     -      seq shift register <= byte        byte
 *                                  (NAK while running)
 *   'R' 0x52   run    -     -      run <= A[0] (0 halts, pc -> 0)    A[0]
 *   'O' 0x4F   oe     -     -      uio_oe <= A (1 = pin is output)   A
 *   'Q' 0x51   -      -     -      status; clears `overrun`          {run, overrun, 0, pc[4:0]}
 *   other                          NAK                               CMD
 *
 * Rules
 *   - Wait for the 2-byte reply before sending the next packet. A packet
 *     that completes while a reply is still going out is dropped and sets
 *     `overrun` (read and cleared by 'Q').
 *   - Resync: a packet that is not complete within TIMEOUT cycles of its
 *     previous byte is thrown away (so a host that lost count recovers by
 *     pausing ~3 ms). A UART framing error also throws the partial packet away.
 *   - Program words can only be written while halted; `seq` enforces this
 *     too, the NAK here just tells the host.
 */

`default_nettype none

module host_port #(
    parameter integer TIMEOUT_W = 17  // resync after 2**17 cycles = 3.3 ms at 40 MHz
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

    // seq
    output reg         run,
    output reg         prog_we,
    output reg  [ 4:0] prog_addr,
    output reg  [15:0] prog_data,
    output reg         sr_we,
    output reg  [ 7:0] sr_data,
    input  wire [ 4:0] seq_pc,

    // pins
    output reg  [ 7:0] uio_oe
);

  localparam [7:0] ACK = 8'h06, NAK = 8'h15, ID = 8'h52;
  localparam [7:0] C_ID = 8'h49, C_W = 8'h57, C_S = 8'h53, C_R = 8'h52, C_O = 8'h4F, C_Q = 8'h51;

  // ---------------------------------------------------------------------------
  // Packet assembly
  // ---------------------------------------------------------------------------
  reg [7:0] b0, b1, b2;  // first three bytes of the packet
  reg [1:0] nbytes;  // bytes received so far (0..3)
  reg [TIMEOUT_W-1:0] idle;  // cycles since the last byte of a partial packet
  wire timed_out = &idle;

  wire packet = rx_valid && (nbytes == 2'd3);  // 4th byte arrives this cycle

  // ---------------------------------------------------------------------------
  // Reply: two bytes, STATUS then DATA
  // ---------------------------------------------------------------------------
  localparam [1:0] R_IDLE = 2'd0, R_SEND0 = 2'd1, R_SEND1 = 2'd2;
  reg [1:0] rstate;
  reg [7:0] r_status, r_data;
  wire replying = (rstate != R_IDLE);

  // Start a byte whenever one is waiting and the transmitter is free. The
  // transmitter's busy rises the cycle after start, so after SEND0 starts its
  // byte, SEND1 simply waits for busy to fall again.
  assign tx_start = (rstate == R_SEND0 || rstate == R_SEND1) && !tx_busy;

  always @(*) tx_data = (rstate == R_SEND0) ? r_status : r_data;

  reg overrun;

  // ---------------------------------------------------------------------------
  // Command execution (one cycle, when the 4th byte arrives)
  // ---------------------------------------------------------------------------
  wire [7:0] cmd = b0, a = b1, bb = b2, c = rx_data;
  reg  [7:0] st, dt;  // reply for this packet
  always @(*) begin
    st = ACK;
    dt = 8'h00;
    case (cmd)
      C_ID: dt = ID;
      C_W: begin
        dt = a;
        if (run || a[7:5] != 3'd0) st = NAK;
      end
      C_S: begin
        dt = a;
        if (run) st = NAK;
      end
      C_R: dt = {7'd0, a[0]};
      C_O: dt = a;
      C_Q: dt = {run, overrun, 1'b0, seq_pc};
      default: begin
        st = NAK;
        dt = cmd;
      end
    endcase
  end

  wire exec = packet && !replying;  // a packet we will act on

  always @(posedge clk) begin
    prog_we <= 1'b0;  // pulses
    sr_we   <= 1'b0;
    if (!rst_n) begin
      b0 <= 8'd0;
      b1 <= 8'd0;
      b2 <= 8'd0;
      nbytes <= 2'd0;
      idle <= {TIMEOUT_W{1'b0}};
      rstate <= R_IDLE;
      r_status <= 8'd0;
      r_data <= 8'd0;
      overrun <= 1'b0;
      run <= 1'b0;
      prog_addr <= 5'd0;
      prog_data <= 16'd0;
      sr_data <= 8'd0;
      uio_oe <= 8'd0;  // all uio pins are inputs until the host says otherwise
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
        rstate   <= R_SEND0;
        if (cmd == C_Q) overrun <= 1'b0;
        if (st == ACK) begin
          case (cmd)
            C_W: begin
              prog_we   <= 1'b1;
              prog_addr <= a[4:0];
              prog_data <= {bb, c};
            end
            C_S: begin
              sr_we   <= 1'b1;
              sr_data <= a;
            end
            C_R: run <= a[0];
            C_O: uio_oe <= a;
            default: ;
          endcase
        end
      end

      // Reply sequencing.
      if (tx_start) rstate <= (rstate == R_SEND0) ? R_SEND1 : R_IDLE;
    end
  end

endmodule

`default_nettype wire
