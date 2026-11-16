#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
# Build toolbin/<name>/: one directory of symlinks per tool set. run_golden.sh runs the script
# with PATH set to one of these directories only, which (a) hides bwa and minimap2 and
# (b) fixes which awk and which coreutils the script sees.
#   native       what this machine resolves by default: the first gawk on PATH + /usr/bin coreutils
#   uu-gawk532   /usr/bin/gawk + /usr/bin coreutils (uutils 0.2.2 on Ubuntu 25.10)
#   uu-mawk      /usr/bin/mawk + /usr/bin coreutils
#   gnu-gawk     /usr/bin/gawk + GNU coreutils (the gnu* commands of the gnu-coreutils package)
#   gnu-mawk     /usr/bin/mawk + GNU coreutils          (a stock Ubuntu up to 24.04)
set -eu
W="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mk() {   # name, awk binary, coreutils flavour (uu | gnu)
    local d="$W/toolbin/$1" t
    rm -rf "$d"; mkdir -p "$d"
    ln -s "$2" "$d/awk"
    for t in bash sed gzip grep df mv rm cp; do ln -s "/usr/bin/$t" "$d/$t"; done
    for t in env dirname readlink nproc cat tee head od tr basename paste wc mkdir tail sort cut mktemp; do
        if [ "$3" = gnu ]; then ln -s "/usr/bin/gnu$t" "$d/$t"; else ln -s "/usr/bin/$t" "$d/$t"; fi
    done
    for t in bwa minimap2; do [ ! -e "$d/$t" ] || { echo "$t must not be in $d" >&2; exit 1; }; done
}
mk native     "$(command -v gawk)" uu
mk uu-gawk532 /usr/bin/gawk uu
mk uu-mawk    /usr/bin/mawk uu
mk gnu-gawk   /usr/bin/gawk gnu
mk gnu-mawk   /usr/bin/mawk gnu
for d in "$W"/toolbin/*; do
    printf '%-12s awk: %-40s paste: %s\n' "$(basename "$d")" "$("$d/awk" -W version 2>&1 | head -1)" "$("$d/paste" --version | head -1)"
done
