# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = project.v uart_tx.v uart_rx.v host_port.v engine_sys.v engine.v snapshot.v edge_ts.v verdict.v

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
# Cell models for the PDK in $(PDK) (ihp-sg13cmos5l or ihp-sg13g2), e.g. sg13cmos5l_stdcell.v
PDK ?= ihp-sg13cmos5l
PDK_CELL_PREFIX = $(patsubst ihp-%,%,$(PDK))
PDK_LIBS_REF = $(PDK_ROOT)/$(PDK)/libs.ref
PDK_STDCELL = $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_stdcell.v
VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_io/verilog/$(PDK_CELL_PREFIX)_io.v
# Newer PDK versions ship the UDP (user-defined primitive) models in a separate file
VERILOG_SOURCES += $(wildcard $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_udp.v)

# Flip-flop power-up value (test/README.md, "Gate-level runs"):
#   GL_POWERUP=x     (default) the PDK's own models: every flop starts at X. The
#                    required run (the Tiny Tapeout gl_test action runs this one).
#   GL_POWERUP=0 / 1 every flop powers up at 0 / 1
#   GL_POWERUP=rand  every flop powers up at its own random bit; GL_SEED=<n>
#                    (default 1) seeds it and is printed in the log
# For 0 / 1 / rand the only flip-flop cell the netlist uses, sg13cmos5l_dfrbpq_1,
# is replaced by the behavioural model in gl_powerup_dfrbpq.v: it is removed from a
# copy of the PDK's cell file (every other cell stays the PDK's).
GL_POWERUP ?= x
ifeq ($(GL_POWERUP),x)
SIM_BUILD				= sim_build/gl
VERILOG_SOURCES += $(PDK_STDCELL)
else
ifeq ($(filter $(GL_POWERUP),0 1 rand),)
$(error GL_POWERUP must be x, 0, 1 or rand, not '$(GL_POWERUP)')
endif
# The override replaces only sg13cmos5l_dfrbpq_1; a netlist with any other flip-flop or latch
# cell would leave those at X and the run would not be what it claims: refuse it.
GL_FLOP_CELLS := $(shell grep -ohE 'sg13cmos5l_(df|dl|sdf)[a-z0-9_]*' $(PWD)/gate_level_netlist.v 2>/dev/null | sort -u)
GL_OTHER_FLOPS := $(filter-out sg13cmos5l_dfrbpq_1 sg13cmos5l_dlygate%,$(GL_FLOP_CELLS))
ifneq ($(GL_OTHER_FLOPS),)
$(error GL_POWERUP=$(GL_POWERUP): the netlist uses flip-flop / latch cells the override does not model: $(GL_OTHER_FLOPS))
endif
# one build directory per power-up mode (the defines are compiled in)
SIM_BUILD				= sim_build/gl_pu$(GL_POWERUP)
GL_STDCELL_NO_DFF = $(SIM_BUILD)/stdcell_without_dfrbpq_1.v
VERILOG_SOURCES += $(GL_STDCELL_NO_DFF) $(PWD)/gl_powerup_dfrbpq.v
ifeq ($(GL_POWERUP),rand)
COMPILE_ARGS    += -DGL_POWERUP_RAND -s gl_powerup_seed
GL_SEED ?= 1
PLUSARGS        += +GL_SEED=$(GL_SEED)
else
COMPILE_ARGS    += -DGL_POWERUP_$(GL_POWERUP)
endif
endif

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

# lets test.py skip tests that only check timing (same logic as RTL, much slower here)
export REFEREE_GL = 1

endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

# Include the testbench sources:
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim

ifdef GL_STDCELL_NO_DFF
# The PDK's cell models without sg13cmos5l_dfrbpq_1 (gl_powerup_dfrbpq.v replaces
# it). Fails unless exactly one module was removed, so a renamed or missing cell
# can't leave the PDK model in place silently. (After the include: the first rule
# in this file would otherwise become the default goal.)
$(GL_STDCELL_NO_DFF): $(PDK_STDCELL)
	@mkdir -p $(@D)
	awk 'BEGIN { n = 0; skip = 0 } \
	     /^[ \t]*module[ \t]+sg13cmos5l_dfrbpq_1[ \t(]/ { skip = 1; n++ } \
	     !skip { print } \
	     skip && /^[ \t]*endmodule/ { skip = 0 } \
	     END { if (n != 1) { print "sg13cmos5l_dfrbpq_1: " n " definitions found, expected 1" > "/dev/stderr"; exit 1 } }' \
	    $< > $@.tmp
	mv $@.tmp $@
endif
