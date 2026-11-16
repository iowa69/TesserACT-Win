#!/usr/bin/env bash
set -euo pipefail
make -j"${CPU_COUNT:-4}" CXX="${CXX:-g++}"
mkdir -p "${PREFIX}/bin"
install -m 0755 scepter "${PREFIX}/bin/scepter"
