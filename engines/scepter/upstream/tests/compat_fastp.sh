#!/usr/bin/env bash
#
# Cross-checks scepter against a real fastp install on the same input and
# asserts that the fastp-compatible parts of the JSON report agree.
#
#   bash tests/compat_fastp.sh
#   FASTP=/path/to/fastp COMPAT_R1=a.fq.gz COMPAT_R2=b.fq.gz bash tests/compat_fastp.sh
#
# Skips (exit 0) when no fastp binary is available.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCEPTER="${SCEPTER:-$ROOT/scepter}"
FASTP="${FASTP:-fastp}"
PAIRS="${COMPAT_PAIRS:-200000}"

if ! command -v "$FASTP" >/dev/null 2>&1 && [ ! -x "$FASTP" ]; then
    echo "SKIP  fastp is not on PATH; set FASTP=/path/to/fastp to run this comparison"
    exit 0
fi
if [ ! -x "$SCEPTER" ]; then
    echo "error: scepter binary not found at $SCEPTER (run 'make' first)" >&2
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required by this script" >&2
    exit 2
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/scepter-compat.XXXXXXXX")"
trap 'rm -rf "$TMP"' EXIT

PASSED=0
FAILED=0
pass() { printf 'PASS  %s\n' "$1"; PASSED=$((PASSED + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; printf '        %s\n' "$2"; FAILED=$((FAILED + 1)); }

# ---------------------------------------------------------------------------
# Input: a subset of the bundled benchmark reads when present, else synthetic.
# ---------------------------------------------------------------------------
SRC1="${COMPAT_R1:-$ROOT/../../bench/reads/ecoli_1.fq.gz}"
SRC2="${COMPAT_R2:-$ROOT/../../bench/reads/ecoli_2.fq.gz}"

if [ -f "$SRC1" ] && [ -f "$SRC2" ]; then
    echo "input: first $PAIRS pairs of $(basename "$SRC1") / $(basename "$SRC2")"
    lines=$((PAIRS * 4))
    gzip -dc "$SRC1" 2>/dev/null | head -n "$lines" | gzip -1 > "$TMP/r1.fq.gz"
    gzip -dc "$SRC2" 2>/dev/null | head -n "$lines" | gzip -1 > "$TMP/r2.fq.gz"
else
    echo "input: $PAIRS synthetic pairs (benchmark reads not found at $SRC1)"
    python3 - "$TMP" "$PAIRS" <<'PYEOF'
import gzip, os, random, sys
out, pairs = sys.argv[1], int(sys.argv[2])
random.seed(4242)
COMP = {'A': 'T', 'C': 'G', 'G': 'C', 'T': 'A'}
genome = ''.join(random.choice('ACGT') for _ in range(200000))
def qual(n):
    # A realistic 3'-decaying quality profile so q30_rate is not degenerate.
    return ''.join(chr(33 + max(2, min(41, int(40 - 18.0 * (i / float(n)) ** 3))))
                   for i in range(n))
Q = qual(150)
with gzip.open(os.path.join(out, "r1.fq.gz"), 'wt') as f1, \
     gzip.open(os.path.join(out, "r2.fq.gz"), 'wt') as f2:
    for i in range(pairs):
        p = random.randrange(0, len(genome) - 350)
        frag = genome[p:p + 300]
        a = frag[:150]
        b = ''.join(COMP[c] for c in reversed(frag[150:300]))
        f1.write("@sim_%d 1:N:0:1\n%s\n+\n%s\n" % (i, a, Q))
        f2.write("@sim_%d 2:N:0:1\n%s\n+\n%s\n" % (i, b, Q))
PYEOF
fi

cd "$TMP" || exit 2

echo "fastp:    $("$FASTP" --version 2>&1 | head -1)"
echo "scepter: $("$SCEPTER" --version 2>&1 | head -1)"
echo

"$FASTP" -i r1.fq.gz -I r2.fq.gz -o fp_1.fq.gz -O fp_2.fq.gz \
    -j fastp.json -h fastp.html -w 8 >fastp.log 2>&1
rc_fastp=$?
"$SCEPTER" -i r1.fq.gz -I r2.fq.gz -o fx_1.fq.gz -O fx_2.fq.gz \
    -j scepter.json -h scepter.html -w 8 --quiet --no-summary >scepter.log 2>&1
rc_scepter=$?

if [ "$rc_fastp" -ne 0 ]; then
    echo "error: fastp exited $rc_fastp" >&2
    tail -5 fastp.log >&2
    exit 2
fi
if [ "$rc_scepter" -ne 0 ]; then
    echo "error: scepter exited $rc_scepter" >&2
    tail -5 scepter.log >&2
    exit 2
fi

report="$(python3 <<'PYEOF'
import json

a = json.load(open("fastp.json"))
b = json.load(open("scepter.json"))
rows = []
for phase in ("before_filtering", "after_filtering"):
    sa, sb = a["summary"][phase], b["summary"][phase]
    rows.append(("total_reads", phase, sa["total_reads"], sb["total_reads"], 0.0))
    for key in ("q30_rate", "gc_content"):
        rows.append((key, phase, sa[key], sb[key], 0.005))
for name, phase, va, vb, tol in rows:
    if tol == 0.0:
        status = "OK" if va == vb else "MISMATCH"
        print("%s\t%s.%s\t%s\t%s\t%s" % (status, phase, name, va, vb, "exact"))
    else:
        d = abs(float(va) - float(vb))
        status = "OK" if d <= tol else "MISMATCH"
        print("%s\t%s.%s\t%.6f\t%.6f\t%.2e" % (status, phase, name, va, vb, d))
PYEOF
)"

if [ -z "$report" ]; then
    echo "error: could not compare the two JSON reports" >&2
    exit 2
fi

printf '%-9s %-34s %-14s %-14s %s\n' "STATUS" "FIELD" "FASTP" "SCEPTER" "DELTA"
while IFS=$'\t' read -r status field va vb delta; do
    printf '%-9s %-34s %-14s %-14s %s\n' "$status" "$field" "$va" "$vb" "$delta"
    if [ "$status" = "OK" ]; then
        pass "$field agrees"
    else
        fail "$field agrees" "fastp=$va scepter=$vb delta=$delta"
    fi
done <<< "$report"

echo
echo "-----------------------------------------"
printf 'total %d   passed %d   failed %d\n' "$((PASSED + FAILED))" "$PASSED" "$FAILED"
if [ "$FAILED" -ne 0 ]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
exit 0
