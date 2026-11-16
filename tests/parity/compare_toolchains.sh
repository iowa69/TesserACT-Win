#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
# compare_toolchains.sh TOOLCHAIN : diff out/golden-alt/TOOLCHAIN against golden/ (native) after
# masking the output-directory prefix and the free-disk / free-memory figures.
W="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; tc="$1"; n=0; d=0
norm() { sed -E "s#out/alt/$tc/#out/#g; s#(OK|FAIL) +disk +.*#\1 disk <masked>#; s#(OK|WARN) +memory +.*#\1 memory <masked>#" "$1"; }
while IFS= read -r f; do
    rel="${f#$W/out/golden-alt/$tc/}"
    case "$rel" in */cmd.txt|*/out_tree.txt|*/calls/*|*/engine_out/*) continue ;; esac
    n=$((n+1))
    if ! diff <(norm "$W/golden/$rel" 2>&1) <(norm "$f") > "$W/out/.cmp.$$" 2>&1; then
        d=$((d+1)); echo "=== DIFFERS: $rel"; head -${2:-12} "$W/out/.cmp.$$" | cut -c1-260
    fi
done < <(find "$W/out/golden-alt/$tc" -type f | LC_ALL=C sort)
rm -f "$W/out/.cmp.$$"
echo "toolchain $tc: $n files compared with native, $d differ"
