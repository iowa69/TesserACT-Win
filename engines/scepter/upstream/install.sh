#!/usr/bin/env bash
# Install scepter.
#
#   ./install.sh                 build and install into the active conda env,
#                                or into ~/.local/bin if conda is not active
#   ./install.sh --prefix DIR    install into DIR/bin
#
# scepter needs only a C++17 compiler, zlib and pthreads.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
prefix=""

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)  prefix="${2:?--prefix needs a directory}"; shift 2 ;;
        -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ -z "$prefix" ]; then
    if [ -n "${CONDA_PREFIX:-}" ]; then
        prefix="$CONDA_PREFIX"
        say "installing into the active conda environment: $prefix"
    else
        prefix="$HOME/.local"
        say "no conda environment active; installing into $prefix"
    fi
fi

cxx="${CXX:-g++}"
command -v "$cxx" >/dev/null 2>&1 || die "no C++ compiler found (set CXX, or install g++)"
if ! echo 'int main(){return 0;}' | "$cxx" -std=c++17 -x c++ - -o /dev/null 2>/dev/null; then
    die "$cxx does not accept -std=c++17"
fi

say "building with $cxx"
make -C "$here" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" CXX="$cxx"

mkdir -p "$prefix/bin"
install -m 0755 "$here/scepter" "$prefix/bin/scepter"
say "installed scepter into $prefix/bin"

"$prefix/bin/scepter" --version >/dev/null 2>&1 || die "the installed binary does not run"
say "$("$prefix/bin/scepter" --version)"

case ":$PATH:" in
    *":$prefix/bin:"*) ;;
    *) printf '\n\033[33mnote:\033[0m %s is not on your PATH. Add it with:\n      export PATH="%s/bin:$PATH"\n' "$prefix/bin" "$prefix" ;;
esac

cat <<EOF

Next:
  scepter -i reads_1.fq.gz -I reads_2.fq.gz -o out_1.fq.gz -O out_2.fq.gz

Feeding an assembler? Use the preset tuned for it:
  scepter --preset assembly -i R1.fq.gz -I R2.fq.gz --out-dir trimmed/
EOF
