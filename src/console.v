/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * Text console over UART.
 *
 * Commands (hex is case-insensitive, 1-2 digits per number):
 *   r AA       read register AA        -> "dd"
 *   w AA DD    write DD to register AA -> "ok"
 *   anything else                      -> "?"
 *
 * Line handling:
 *   - Printable characters are echoed as they arrive.
 *   - CR (Enter) runs the line. LF is ignored, so CRLF terminals work.
 *   - Backspace, DEL, Ctrl-C or ESC throw the line away.
 *   - Every reply has the form "\r\n<body>\r\n> ". An empty line or a
 *     discarded line just gets a fresh prompt "\r\n> ".
 *   - The prompt is also sent once after reset.
 *   - Wait for the prompt before sending the next line. Bytes that arrive
 *     while a reply is being sent are dropped and set `overrun`.
 *
 * The parser reads one character at a time, so it needs no line buffer.
 */

`default_nettype none

module console (
    input  wire       clk,
    input  wire       rst_n,
    // From the UART receiver
    input  wire [7:0] rx_data,
    input  wire       rx_valid,
    // To the UART transmitter
    output wire [7:0] tx_data,
    output wire       tx_wr,
    input  wire       tx_hold_empty,
    // Register bus
    output wire [7:0] bus_addr,
    output wire [7:0] bus_wdata,
    output wire       bus_we,
    input  wire [7:0] bus_rdata,
    // Status
    output reg        overrun,
    output wire       idle
);

  localparam S_INPUT = 1'b0, S_RESP = 1'b1;
  localparam [1:0] K_NONE = 2'd0, K_HEX = 2'd1, K_OK = 2'd2, K_ERR = 2'd3;

  reg state;

  // ---------------------------------------------------------------------------
  // Character classes
  // ---------------------------------------------------------------------------
  wire [7:0] c = rx_data;
  wire [7:0] lc = c | 8'h20;  // lower case for letters
  wire is_cr = (c == 8'h0D);
  wire is_lf = (c == 8'h0A);
  wire is_cancel = (c == 8'h03) || (c == 8'h08) || (c == 8'h1B) || (c == 8'h7F);
  wire is_space = (c == 8'h20);
  wire is_print = (c >= 8'h20) && (c <= 8'h7E);
  wire is_digit = (c >= 8'h30) && (c <= 8'h39);
  wire is_hexl = (lc >= 8'h61) && (lc <= 8'h66);  // a-f
  wire is_hex = is_digit || is_hexl;
  wire is_letter = (lc >= 8'h61) && (lc <= 8'h7A);  // a-z
  wire [3:0] nib = is_digit ? c[3:0] : (lc[3:0] + 4'd9);

  // ---------------------------------------------------------------------------
  // Line state
  // ---------------------------------------------------------------------------
  reg       have_cmd;  // something other than spaces has been typed
  reg [7:0] cmd;  // lower-case command letter, 0 if the line started badly
  reg       err;  // syntax error somewhere in the line
  reg [7:0] cur;  // number being typed
  reg [1:0] ndig;  // digits in `cur`
  reg [7:0] arg0, arg1;  // last two finished numbers (arg1 is the newest)
  reg [1:0] nargs;  // finished numbers, 3 means "too many"

  wire       pend = (ndig != 2'd0);
  wire [1:0] nargs_inc = (nargs == 2'd3) ? 2'd3 : nargs + 2'd1;
  wire [1:0] fin_nargs = pend ? nargs_inc : nargs;
  wire [7:0] a_first = pend ? arg1 : arg0;  // second-to-last number
  wire [7:0] a_last = pend ? cur : arg1;  // last number

  wire       cmd_r = have_cmd && !err && (cmd == 8'h72) && (fin_nargs == 2'd1);  // r AA
  wire       cmd_w = have_cmd && !err && (cmd == 8'h77) && (fin_nargs == 2'd2);  // w AA DD

  wire       in_byte = (state == S_INPUT) && rx_valid;
  wire       eol = in_byte && is_cr;

  assign bus_addr  = (cmd == 8'h77) ? a_first : a_last;
  assign bus_wdata = a_last;
  assign bus_we    = eol && cmd_w;
  assign idle      = (state == S_INPUT);

  // ---------------------------------------------------------------------------
  // Reply: "\r\n" [body "\r\n"] "> "
  // ---------------------------------------------------------------------------
  reg [2:0] step;
  reg [1:0] kind;
  reg [7:0] rval;

  function [7:0] hexch(input [3:0] n);
    hexch = (n < 4'd10) ? (8'h30 + {4'd0, n}) : (8'h57 + {4'd0, n});  // '0'.., 'a'..
  endfunction

  reg [7:0] rbyte;
  reg       rvalid;
  always @(*) begin
    rbyte  = 8'h00;
    rvalid = 1'b1;
    case (step)
      3'd0: rbyte = 8'h0D;
      3'd1: rbyte = 8'h0A;
      3'd2: begin
        rbyte  = (kind == K_HEX) ? hexch(rval[7:4]) : (kind == K_OK) ? 8'h6F : 8'h3F;  // o / ?
        rvalid = (kind != K_NONE);
      end
      3'd3: begin
        rbyte  = (kind == K_HEX) ? hexch(rval[3:0]) : 8'h6B;  // k
        rvalid = (kind == K_HEX) || (kind == K_OK);
      end
      3'd4: begin
        rbyte  = 8'h0D;
        rvalid = (kind != K_NONE);
      end
      3'd5: begin
        rbyte  = 8'h0A;
        rvalid = (kind != K_NONE);
      end
      3'd6: rbyte = 8'h3E;  // >
      default: rbyte = 8'h20;
    endcase
  end

  wire resp_go = (state == S_RESP) && (!rvalid || tx_hold_empty);
  wire echo = in_byte && is_print;

  assign tx_wr   = (state == S_RESP) ? (rvalid && tx_hold_empty) : (echo && tx_hold_empty);
  assign tx_data = (state == S_RESP) ? rbyte : c;

  // ---------------------------------------------------------------------------
  // Main state machine
  // ---------------------------------------------------------------------------
  always @(posedge clk) begin
    overrun <= 1'b0;
    if (!rst_n) begin
      state    <= S_RESP;  // send the prompt after reset
      step     <= 3'd0;
      kind     <= K_NONE;
      rval     <= 8'd0;
      have_cmd <= 1'b0;
      cmd      <= 8'd0;
      err      <= 1'b0;
      cur      <= 8'd0;
      ndig     <= 2'd0;
      arg0     <= 8'd0;
      arg1     <= 8'd0;
      nargs    <= 2'd0;
    end else if (state == S_RESP) begin
      if (rx_valid && !is_lf) overrun <= 1'b1;
      if (resp_go) begin
        if (step == 3'd7) state <= S_INPUT;
        step <= step + 3'd1;
      end
    end else if (rx_valid) begin
      if (echo && !tx_hold_empty) overrun <= 1'b1;

      if (is_cr || is_cancel) begin
        if (!is_cr || !have_cmd) kind <= K_NONE;
        else if (cmd_r) kind <= K_HEX;
        else if (cmd_w) kind <= K_OK;
        else kind <= K_ERR;
        rval     <= bus_rdata;
        state    <= S_RESP;
        step     <= 3'd0;
        have_cmd <= 1'b0;
        cmd      <= 8'd0;
        err      <= 1'b0;
        cur      <= 8'd0;
        ndig     <= 2'd0;
        nargs    <= 2'd0;
      end else if (!is_print) begin
        // LF and other control characters are ignored
      end else if (!have_cmd) begin
        if (!is_space) begin
          have_cmd <= 1'b1;
          if (is_letter) cmd <= lc;
          else err <= 1'b1;
        end
      end else if (is_space) begin
        if (pend) begin
          arg0  <= arg1;
          arg1  <= cur;
          nargs <= nargs_inc;
          cur   <= 8'd0;
          ndig  <= 2'd0;
        end
      end else if (is_hex) begin
        if (ndig == 2'd2) begin
          err <= 1'b1;
        end else begin
          cur  <= {cur[3:0], nib};
          ndig <= ndig + 2'd1;
        end
      end else begin
        err <= 1'b1;
      end
    end
  end

endmodule
