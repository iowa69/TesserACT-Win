#!/usr/bin/env bash
# Every flag the help text advertises must parse and run. A documented flag that
# is silently ignored fails nothing, which is worse than one that is missing.
set -uo pipefail
BIN=${1:-./scepter}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok()  { printf "  ok    %s\n" "$1"; pass=$((pass+1)); }
bad() { printf "  FAIL  %s -- %s\n" "$1" "$2"; fail=$((fail+1)); }

python3 - "$TMP" <<'PY'
import gzip, os, random, sys
random.seed(5)
d = sys.argv[1]
ad = "AGATCGGAAGAGC"
def rc(s): return s[::-1].translate(str.maketrans("ACGT", "TGCA"))
r1, r2 = [], []
for i in range(4000):
    frag = "".join(random.choice("ACGT") for _ in range(random.randint(120, 300)))
    a = (frag + ad + "A" * 200)[:150]
    b = (rc(frag) + ad + "A" * 200)[:150]
    q1 = "".join("I" if j < 120 else "#" for j in range(150))
    r1.append((a, q1)); r2.append((b, q1))
for name, rs in (("R1", r1), ("R2", r2)):
    with gzip.open(os.path.join(d, f"{name}.fq.gz"), "wt") as fh:
        for i, (s, q) in enumerate(rs):
            fh.write(f"@read{i} 1:N:0\n{s}\n+\n{q}\n")
PY
R1="$TMP/R1.fq.gz"; R2="$TMP/R2.fq.gz"

run () { local o="$1"; shift; "$BIN" --in1 "$R1" --in2 "$R2" --out1 "$o/o1.fq.gz" --out2 "$o/o2.fq.gz" "$@" > "$o/log" 2>&1; }

for spec in \
  "--preset assembly" "--preset illumina-pe" "--preset wgs-bacteria" "--preset strict" "--preset lenient" "--preset novaseq" \
  "--thread 4" "--threads 4" "--dedup" "--correction" "--detect-adapter-for-pe" \
  "--length-required 50" "--qualified-quality-phred 20" "--unqualified-percent-limit 40" \
  "--average-qual 15" "--cut-tail" "--cut-right" "--trim-front1 5" "--trim-tail1 5" \
  "--compression 1" "--quiet" "--overrepresentation-analysis" "--no-deep" \
  "--check-contamination" "--qc-only" "--dry-run" "--explain"
do
  d="$TMP/f$(echo "$spec" | tr -cd 'a-z0-9')"; mkdir -p "$d"
  if run "$d" $spec; then ok "$spec"; else bad "$spec" "run failed"; fi
done

for spec in "--nosuchflag" "--preset nonsense" "--compression 99"; do
  d="$TMP/r$(echo "$spec" | tr -cd 'a-z0-9')"; mkdir -p "$d"
  if run "$d" $spec; then bad "reject $spec" "accepted"; else ok "reject $spec"; fi
done

# effects
mkdir -p "$TMP/base" "$TMP/strict"
run "$TMP/base"
run "$TMP/strict" --length-required 145
a=$(zcat "$TMP/base/o1.fq.gz" 2>/dev/null | wc -l); a=$((a/4))
b=$(zcat "$TMP/strict/o1.fq.gz" 2>/dev/null | wc -l); b=$((b/4))
[ "$b" -lt "$a" ] && ok "--length-required filters ($a -> $b)" || bad "--length-required" "no effect ($a -> $b)"

mkdir -p "$TMP/rep"
run "$TMP/rep" --html "$TMP/rep/r.html" --json "$TMP/rep/r.json"
[ -s "$TMP/rep/r.html" ] && ok "--html written" || bad "--html" "missing"
[ -s "$TMP/rep/r.json" ] && ok "--json written" || bad "--json" "missing"
python3 -c "import json,sys; json.load(open('$TMP/rep/r.json'))" 2>/dev/null \
  && ok "JSON report is valid JSON" || bad "JSON report" "not parseable"

mkdir -p "$TMP/d1" "$TMP/d2"
run "$TMP/d1"; run "$TMP/d2"
cmp -s "$TMP/d1/o1.fq.gz" "$TMP/d2/o1.fq.gz" && ok "deterministic" || bad "determinism" "differs"

# adapters must actually be removed
kept=$(zcat "$TMP/base/o1.fq.gz" 2>/dev/null | awk 'NR%4==2' | grep -c "AGATCGGAAGAGC" || true)
[ "${kept:-0}" -eq 0 ] && ok "adapters removed" || bad "adapter trimming" "$kept reads still carry the adapter"

printf "\n%d passed, %d failed\n" "$pass" "$fail"
[ "$fail" -eq 0 ]
