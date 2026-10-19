// Formal check: uart_tx looped straight into uart_rx.
//
// For every divisor in [4, 6] and every possible stream of writes (any
// data, any timing the holding register allows), within the BMC depth:
//   - every byte arrives, in order, unchanged
//   - the receiver never reports a framing error
//   - at most 3 bytes are ever in flight (hold + shifter + receiver)
//   - the line is high whenever the transmitter says it is idle
`default_nettype none

module uart_loopback (
    input wire clk
);
  reg rst_n = 1'b0;
  always @(posedge clk) rst_n <= 1'b1;

  (* anyconst *) reg [15:0] div;
  (* anyseq *) reg want_write;
  (* anyseq *) reg [7:0] wdata;

  always @(*) assume (div >= 16'd4 && div <= 16'd6);

  wire hold_empty, idle, quiet, line;
  wire wr_en = rst_n && want_write && hold_empty;

  uart_tx u_tx (
      .clk(clk), .rst_n(rst_n), .div(div), .wr_data(wdata), .wr_en(wr_en),
      .hold_empty(hold_empty), .idle(idle), .quiet(quiet), .tx(line)
  );

  wire [7:0] rdata;
  wire rvalid, ferr;

  uart_rx u_rx (
      .clk(clk), .rst_n(rst_n), .div(div), .rx(line),
      .data(rdata), .valid(rvalid), .frame_err(ferr)
  );

  // Scoreboard: bytes written but not yet received, oldest first.
  reg [7:0] buf0, buf1, buf2, buf3;
  reg [1:0] wp, rp;
  reg [2:0] n;
  reg [3:0] received;

  always @(posedge clk) begin
    if (!rst_n) begin
      wp <= 0;
      rp <= 0;
      n <= 0;
      received <= 0;
    end else begin
      if (wr_en) begin
        case (wp)
          2'd0: buf0 <= wdata;
          2'd1: buf1 <= wdata;
          2'd2: buf2 <= wdata;
          default: buf3 <= wdata;
        endcase
        wp <= wp + 2'd1;
      end
      if (rvalid) begin
        rp <= rp + 2'd1;
        if (received != 4'hf) received <= received + 4'd1;
      end
      n <= n + {2'd0, wr_en} - {2'd0, rvalid};
    end
  end

  wire [7:0] head = (rp == 2'd0) ? buf0 : (rp == 2'd1) ? buf1 : (rp == 2'd2) ? buf2 : buf3;

  always @(posedge clk) begin
    if (rst_n) begin
      if (rvalid) begin
        assert (n != 0);
        assert (rdata == head);
      end
      assert (!ferr);
      assert (n <= 3);
      if (idle) assert (line);
      if (idle) assert (quiet);
    end
  end

  // Show that the interesting case is reachable: three bytes back to back.
  always @(posedge clk) begin
    if (rst_n) cover (received == 4'd3 && div == 16'd4);
  end

endmodule
