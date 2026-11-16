#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
# Stand-in for tesseract-asm, used ONLY to drive the postflight branches of the real
# `tesseract` script that a real assembly of synthetic reads does not reach.
#   FAKE_SRC   directory of canned engine output; copied into -o OUT
#              (FAKE_SRC.retry is used instead from the second call on, when that dir exists)
#   FAKE_REC   directory where each call's argv and TESSERACT_* environment are recorded
# Files _stderr.txt (written to stderr, i.e. into assembly.log) and _rc (exit status) in the
# canned directory steer the call and are not copied.
if [ "${1:-}" = "--version" ]; then echo "TesserACT 1.6.2-fake"; echo "stub engine"; exit 0; fi
args=("$@"); out=""; k=""
while [ $# -gt 0 ]; do
    case "$1" in -o) out="${2:-}"; shift 2 ;; -k) k="${2:-}"; shift 2 ;; *) shift ;; esac
done
n=1; while [ -e "$FAKE_REC/call$n.argv" ]; do n=$((n+1)); done
printf '%s\n' "${args[@]}" > "$FAKE_REC/call$n.argv"
env | grep '^TESSERACT_' | LC_ALL=C sort > "$FAKE_REC/call$n.env"
src="$FAKE_SRC"
[ "$n" -ge 2 ] && [ -d "$FAKE_SRC.retry" ] && src="$FAKE_SRC.retry"
mkdir -p "$out"
for f in "$src"/*; do
    case "$(basename "$f")" in _stderr.txt|_rc) ;; *) cp "$f" "$out/" ;; esac
done
[ -f "$src/_stderr.txt" ] && cat "$src/_stderr.txt" >&2
rc=0; [ -f "$src/_rc" ] && rc="$(cat "$src/_rc")"
exit "$rc"
