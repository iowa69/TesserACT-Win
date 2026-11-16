# scepter - a from-scratch FASTQ preprocessor with deep quality analysis

BIN       := scepter
SRCDIR    := src
BUILDDIR  := build
PREFIX    ?= /usr/local

SOURCES   := $(wildcard $(SRCDIR)/*.cpp)
OBJECTS   := $(patsubst $(SRCDIR)/%.cpp,$(BUILDDIR)/%.o,$(SOURCES))
DEPS      := $(OBJECTS:.o=.d)

CXX       ?= g++
CXXSTD    := -std=c++17
WARN      := -Wall -Wextra -Wno-unused-parameter
OPT       ?= -O3
CXXFLAGS  += $(CXXSTD) $(WARN) $(OPT) -pthread -MMD -MP
LDFLAGS   += -pthread
LDLIBS    += -lz

# Portable by default. `make native` adds -march=native for local builds.
.PHONY: all native debug asan clean install uninstall test

all: $(BIN)

# These rebuild from scratch with different flags. The clean and the build have to
# be separate sub-makes, otherwise `-j` races them and clean deletes a fresh binary.
native:
	@$(MAKE) --no-print-directory clean
	@$(MAKE) --no-print-directory OPT="-O3 -march=native -mtune=native" $(BIN)

debug:
	@$(MAKE) --no-print-directory clean
	@$(MAKE) --no-print-directory OPT="-O0 -g3 -fno-omit-frame-pointer" $(BIN)

asan:
	@$(MAKE) --no-print-directory clean
	@$(MAKE) --no-print-directory OPT="-O1 -g3 -fsanitize=address,undefined -fno-omit-frame-pointer" \
	         LDFLAGS="-pthread -fsanitize=address,undefined" $(BIN)

$(BIN): $(OBJECTS)
	$(CXX) $(OBJECTS) $(LDFLAGS) $(LDLIBS) -o $@

$(BUILDDIR)/%.o: $(SRCDIR)/%.cpp | $(BUILDDIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILDDIR):
	@mkdir -p $(BUILDDIR)

-include $(DEPS)

flagcheck: $(BIN)
	@bash tests/check_flags.sh ./$(BIN)

check: test flagcheck

test: $(BIN)
	@bash tests/run_tests.sh

install: $(BIN)
	@install -d $(DESTDIR)$(PREFIX)/bin
	@install -m 755 $(BIN) $(DESTDIR)$(PREFIX)/bin/$(BIN)
	@echo "installed $(DESTDIR)$(PREFIX)/bin/$(BIN)"

uninstall:
	@rm -f $(DESTDIR)$(PREFIX)/bin/$(BIN)

clean:
	@rm -rf $(BUILDDIR) $(BIN)
