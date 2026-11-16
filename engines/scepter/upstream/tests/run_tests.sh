#!/usr/bin/env bash
#
# scepter functional test suite.
#
# Self-contained: every fixture is generated into a temporary directory that is
# removed on exit, and nothing is written to the source tree. Requires only
# bash, coreutils, awk, gzip and python3.
#
#   bash tests/run_tests.sh          # or: make test
#   SCEPTER=/path/to/scepter bash tests/run_tests.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCEPTER="${SCEPTER:-$ROOT/scepter}"

if [ ! -x "$SCEPTER" ]; then
    echo "error: scepter binary not found at $SCEPTER (run 'make' first)" >&2
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required by this test suite" >&2
    exit 2
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/scepter-tests.XXXXXXXX")"
trap 'rm -rf "$TMP"' EXIT

PASSED=0
FAILED=0
SKIPPED=0

pass() { printf 'PASS  %s\n' "$1"; PASSED=$((PASSED + 1)); }
skip() { printf 'SKIP  %s (%s)\n' "$1" "${2:-unavailable}"; SKIPPED=$((SKIPPED + 1)); }
fail() {
    printf 'FAIL  %s\n' "$1"
    if [ $# -gt 1 ]; then printf '        %s\n' "$2"; fi
    FAILED=$((FAILED + 1))
}
expect_eq() { # name expected actual
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "expected [$2], got [$3]"; fi
}
expect_true() { # name condition-description result("yes"/"no")
    if [ "$3" = "yes" ]; then pass "$1"; else fail "$1" "$2"; fi
}

# Records in a plain FASTQ file (0 when the file is missing).
nrec() { if [ -f "$1" ]; then awk 'END { print int(NR / 4) }' "$1"; else echo 0; fi; }
# Distinct read lengths, comma separated.
lens() { awk 'NR % 4 == 2 { print length($0) }' "$1" | sort -un | paste -sd, -; }
names() { awk 'NR % 4 == 1 { sub(/ .*/, "", $0); print }' "$1"; }

# Run scepter with reports parked in the temp dir unless the test says otherwise.
fp() { "$SCEPTER" "$@" --quiet --no-summary -j "$TMP/r.json" -h "$TMP/r.html" >"$TMP/out.log" 2>"$TMP/err.log"; }

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
python3 - "$TMP" <<'PYEOF'
import os, random, sys

out = sys.argv[1]
random.seed(20260731)
COMP = {'A': 'T', 'C': 'G', 'G': 'C', 'T': 'A'}
TRUSEQ = "AGATCGGAAGAGCACACGTCTGAACTCCAGTCA"
TRUSEQ2 = "AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT"

def rc(s):
    return ''.join(COMP[c] for c in reversed(s))

def rseq(n, alphabet='ACGT'):
    return ''.join(random.choice(alphabet) for _ in range(n))

def rec(name, seq, qual):
    return "@%s\n%s\n+\n%s\n" % (name, seq, qual)

def write(name, text):
    with open(os.path.join(out, name), 'w') as fh:
        fh.write(text)

HI = 'I'   # Q40
LO = '#'   # Q2
MID = '5'  # Q20

# --- 1/2: round-trip and lockstep ------------------------------------------
se, pe1, pe2, il = [], [], [], []
for i in range(500):
    a, b = rseq(100), rseq(100)
    se.append(rec("se_%d" % i, a, HI * 100))
    pe1.append(rec("pair_%d 1:N:0:ACGTAC" % i, a, HI * 100))
    pe2.append(rec("pair_%d 2:N:0:ACGTAC" % i, b, HI * 100))
    il.append(pe1[-1]); il.append(pe2[-1])
write("se.fq", ''.join(se))
write("pe_1.fq", ''.join(pe1))
write("pe_2.fq", ''.join(pe2))
write("il.fq", ''.join(il))

# Pairs fail on alternating mates, so a naive implementation would desynchronise.
lk1, lk2 = [], []
for i in range(40):
    q1 = LO * 100 if i % 3 == 0 else HI * 100
    q2 = LO * 100 if i % 3 == 1 else HI * 100
    lk1.append(rec("lock_%d" % i, rseq(100), q1))
    lk2.append(rec("lock_%d" % i, rseq(100), q2))
write("lock_1.fq", ''.join(lk1))
write("lock_2.fq", ''.join(lk2))

# --- 3: adapter read-through ------------------------------------------------
ad1, ad2, inserts1, inserts2 = [], [], [], []
for i in range(200):
    i1, i2 = rseq(60), rseq(70)
    inserts1.append(i1); inserts2.append(i2)
    ad1.append(rec("ad_%d" % i, i1 + TRUSEQ, HI * (60 + len(TRUSEQ))))
    ad2.append(rec("ad_%d" % i, i2 + TRUSEQ2, HI * (70 + len(TRUSEQ2))))
write("adapter_1.fq", ''.join(ad1))
write("adapter_2.fq", ''.join(ad2))
write("adapter_expect1.txt", ''.join(s + "\n" for s in inserts1))
write("adapter_expect2.txt", ''.join(s + "\n" for s in inserts2))

# --- 4: quality filtering ---------------------------------------------------
# 10 clean reads + 10 carrying 20 low-quality bases (20% unqualified at Q30).
qual = []
for i in range(10):
    qual.append(rec("q_good_%d" % i, rseq(100), HI * 100))
for i in range(10):
    qual.append(rec("q_bad_%d" % i, rseq(100), HI * 80 + LO * 20))
write("qual.fq", ''.join(qual))

nb = []
for i in range(10):
    nb.append(rec("n_good_%d" % i, rseq(100), HI * 100))
for i in range(10):
    s = rseq(100)
    s = s[:10] + 'N' + s[11:40] + 'N' + s[41:70] + 'N' + s[71:]
    nb.append(rec("n_bad_%d" % i, s, HI * 100))
write("nbase.fq", ''.join(nb))

mq = []
for i in range(10):
    mq.append(rec("e_good_%d" % i, rseq(100), HI * 100))   # mean Q40
for i in range(10):
    mq.append(rec("e_bad_%d" % i, rseq(100), MID * 100))   # mean Q20
write("meanq.fq", ''.join(mq))

# --- 5: length filtering ----------------------------------------------------
ln = []
for i in range(10):
    ln.append(rec("short_%d" % i, rseq(30), HI * 30))
for i in range(10):
    ln.append(rec("long_%d" % i, rseq(80), HI * 80))
write("len.fq", ''.join(ln))

# --- 6: sliding-window trimming --------------------------------------------
# 40-base reads; the window (4 bases, mean Q20) stops as soon as its mean
# recovers, which is what fixes the expected lengths below.
cf, ct, cr = [], [], []
for i in range(10):
    cf.append(rec("cf_%d" % i, rseq(40), LO * 8 + HI * 32))
    ct.append(rec("ct_%d" % i, rseq(40), HI * 32 + LO * 8))
    cr.append(rec("cr_%d" % i, rseq(40), HI * 24 + LO * 16))
write("cutfront.fq", ''.join(cf))
write("cuttail.fq", ''.join(ct))
write("cutright.fq", ''.join(cr))

# --- 7: poly-G / poly-X tails ----------------------------------------------
# The inserts avoid the tail base entirely so the trim point is unambiguous.
pg, px = [], []
for i in range(50):
    pg.append(rec("pg_%d" % i, rseq(60, 'ACT') + 'G' * 12, HI * 72))
    px.append(rec("px_%d" % i, rseq(60, 'CGT') + 'A' * 12, HI * 72))
write("polyg.fq", ''.join(pg))
write("polyx.fq", ''.join(px))

# --- 8/9: overlap merging and base correction -------------------------------
frag = rseq(200)
r1, r2 = frag[:150], rc(frag[50:200])
write("merge_expect.txt", frag + "\n")
write("corr_expect.txt", r2 + "\n")
mg1, mg2, co1, co2 = [], [], [], []
POS = 100                                     # inside the 100 bp overlap
wrong = {'A': 'C', 'C': 'A', 'G': 'T', 'T': 'G'}[r2[POS]]
bad = r2[:POS] + wrong + r2[POS + 1:]
badq = HI * POS + LO + HI * (149 - POS)
for i in range(20):
    mg1.append(rec("mrg_%d" % i, r1, HI * 150))
    mg2.append(rec("mrg_%d" % i, r2, HI * 150))
    co1.append(rec("cor_%d" % i, r1, HI * 150))
    co2.append(rec("cor_%d" % i, bad, badq))
write("merge_1.fq", ''.join(mg1))
write("merge_2.fq", ''.join(mg2))
write("corr_1.fq", ''.join(co1))
write("corr_2.fq", ''.join(co2))

# --- 10: duplicates ---------------------------------------------------------
d1, d2 = rseq(80), rseq(80)
u1, u2 = rseq(80), rseq(80)
write("dup_1.fq", rec("dup_a", d1, HI * 80) + rec("dup_b", d1, HI * 80) + rec("uniq", u1, HI * 80))
write("dup_2.fq", rec("dup_a", d2, HI * 80) + rec("dup_b", d2, HI * 80) + rec("uniq", u2, HI * 80))

# --- 11: UMI ----------------------------------------------------------------
um1, um2, umis = [], [], []
for i in range(10):
    s = rseq(60)
    umis.append(s[:8])
    um1.append(rec("umi_%d" % i, s, HI * 60))
    um2.append(rec("umi_%d" % i, rseq(60), HI * 60))
write("umi_1.fq", ''.join(um1))
write("umi_2.fq", ''.join(um2))
write("umi_expect.txt", ''.join(u + "\n" for u in umis))

# --- 12: split --------------------------------------------------------------
with open(os.path.join(out, "split.fq"), 'w') as fh:
    for i in range(30000):
        fh.write(rec("sp_%d" % i, rseq(40), HI * 40))

# --- 16: phred+64 -----------------------------------------------------------
# 'h' is 104: Q40 under phred+64, which must be written out as 'I' (Q40+33).
p64 = []
for i in range(20):
    p64.append(rec("p64_%d" % i, rseq(60), 'h' * 60))
write("p64.fq", ''.join(p64))
PYEOF

if [ $? -ne 0 ]; then
    echo "error: could not build test fixtures" >&2
    exit 2
fi

cd "$TMP" || exit 2

echo "scepter test suite  ($SCEPTER)"
echo "workspace: $TMP"
echo

# Flags that turn every transformation and filter off, so the tool is a pass-through.
PASSTHRU="-A -Q -L -G"

# ---------------------------------------------------------------------------
# 1. SE and PE round-trip
# ---------------------------------------------------------------------------
if fp -i se.fq -o rt_se.fq $PASSTHRU; then
    expect_eq "01a SE round-trip keeps every read" "500" "$(nrec rt_se.fq)"
    if cmp -s se.fq rt_se.fq; then
        pass "01b SE round-trip is byte-identical to the input"
    else
        fail "01b SE round-trip is byte-identical to the input" "output differs from input"
    fi
else
    fail "01a SE round-trip keeps every read" "scepter exited non-zero"
    fail "01b SE round-trip is byte-identical to the input" "scepter exited non-zero"
fi

if fp -i pe_1.fq -I pe_2.fq -o rt_1.fq -O rt_2.fq $PASSTHRU; then
    expect_eq "01c PE round-trip keeps every read 1" "500" "$(nrec rt_1.fq)"
    expect_eq "01d PE round-trip keeps every read 2" "500" "$(nrec rt_2.fq)"
    if cmp -s pe_1.fq rt_1.fq && cmp -s pe_2.fq rt_2.fq; then
        pass "01e PE round-trip is byte-identical to the input"
    else
        fail "01e PE round-trip is byte-identical to the input" "output differs from input"
    fi
else
    fail "01c PE round-trip keeps every read 1" "scepter exited non-zero"
    fail "01d PE round-trip keeps every read 2" "scepter exited non-zero"
    fail "01e PE round-trip is byte-identical to the input" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 2. Paired output stays in lockstep
# ---------------------------------------------------------------------------
if fp -i lock_1.fq -I lock_2.fq -o lk_1.fq -O lk_2.fq -A -G -L -q 30 -u 10; then
    n1="$(nrec lk_1.fq)"; n2="$(nrec lk_2.fq)"
    expect_eq "02a paired outputs hold the same number of records" "$n1" "$n2"
    expect_eq "02b filtering actually dropped some pairs" "yes" \
              "$([ "$n1" -gt 0 ] && [ "$n1" -lt 40 ] && echo yes || echo no)"
    if diff -q <(names lk_1.fq) <(names lk_2.fq) >/dev/null; then
        pass "02c paired outputs carry matching names in order"
    else
        fail "02c paired outputs carry matching names in order" "name order diverged"
    fi
else
    fail "02a paired outputs hold the same number of records" "scepter exited non-zero"
    fail "02b filtering actually dropped some pairs" "scepter exited non-zero"
    fail "02c paired outputs carry matching names in order" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 3. Adapter trimming
# ---------------------------------------------------------------------------
if fp -i adapter_1.fq -o ad_se.fq -a "AGATCGGAAGAGCACACGTCTGAACTCCAGTCA" -Q -L -G; then
    expect_eq "03a SE adapter trimming leaves the exact insert length" "60" "$(lens ad_se.fq)"
    if diff -q <(awk 'NR % 4 == 2' ad_se.fq) adapter_expect1.txt >/dev/null; then
        pass "03b SE adapter trimming reproduces the inserts exactly"
    else
        fail "03b SE adapter trimming reproduces the inserts exactly" "sequences differ"
    fi
else
    fail "03a SE adapter trimming leaves the exact insert length" "scepter exited non-zero"
    fail "03b SE adapter trimming reproduces the inserts exactly" "scepter exited non-zero"
fi

if fp -i adapter_1.fq -I adapter_2.fq -o ad_1.fq -O ad_2.fq \
      -a "AGATCGGAAGAGCACACGTCTGAACTCCAGTCA" \
      --adapter_sequence_r2 "AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT" -Q -L -G; then
    expect_eq "03c PE adapter trimming trims read 1 to the insert" "60" "$(lens ad_1.fq)"
    expect_eq "03d PE adapter trimming trims read 2 to the insert" "70" "$(lens ad_2.fq)"
else
    fail "03c PE adapter trimming trims read 1 to the insert" "scepter exited non-zero"
    fail "03d PE adapter trimming trims read 2 to the insert" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 4. Quality filtering: -q / -u / -n / -e
# ---------------------------------------------------------------------------
fp -i qual.fq -o f_q.fq -A -G -L -q 30 -u 10
expect_eq "04a -q/-u drop reads with too many unqualified bases" "10" "$(nrec f_q.fq)"
expect_eq "04b -q/-u keep only the clean reads" "0" "$(grep -c '^@q_bad' f_q.fq || true)"

fp -i qual.fq -o f_q0.fq -A -G -L
expect_eq "04c default -q/-u keep every read here" "20" "$(nrec f_q0.fq)"

fp -i nbase.fq -o f_n.fq -A -G -L -n 0
expect_eq "04d -n 0 drops reads containing N" "10" "$(nrec f_n.fq)"
expect_eq "04e -n 0 keeps the N-free reads" "0" "$(grep -c '^@n_bad' f_n.fq || true)"

fp -i nbase.fq -o f_n3.fq -A -G -L -n 3
expect_eq "04f -n 3 tolerates exactly three N bases" "20" "$(nrec f_n3.fq)"

fp -i meanq.fq -o f_e.fq -A -G -L -e 30
expect_eq "04g -e drops reads below the mean-quality floor" "10" "$(nrec f_e.fq)"
expect_eq "04h -e keeps the high-quality reads" "0" "$(grep -c '^@e_bad' f_e.fq || true)"

# ---------------------------------------------------------------------------
# 5. Length filtering: -l / --length_limit
# ---------------------------------------------------------------------------
fp -i len.fq -o f_l.fq -A -G -Q -l 50
expect_eq "05a -l drops reads shorter than the minimum" "10" "$(nrec f_l.fq)"
expect_eq "05b -l keeps only the long reads" "80" "$(lens f_l.fq)"

fp -i len.fq -o f_ll.fq -A -G -Q --length_limit 50
expect_eq "05c --length_limit drops reads longer than the maximum" "10" "$(nrec f_ll.fq)"
expect_eq "05d --length_limit keeps only the short reads" "30" "$(lens f_ll.fq)"

# ---------------------------------------------------------------------------
# 6. Sliding-window trimming: -5 / -3 / -r
# ---------------------------------------------------------------------------
fp -i cutfront.fq -o w_5.fq -A -G -Q -L -5 -W 4 -M 20
# Expected lengths below are what fastp 1.0.1 produces on the same input,
# checked directly against the binary. They look one window short of the
# obvious answer because fastp keeps only the last base of the first passing
# window (-5), only the first base of the last passing window (-3), and walks
# through the good bases at the head of the first failing window (-r).
expect_eq "06a -5 trims the low-quality 5' end" "31" "$(lens w_5.fq)"

fp -i cuttail.fq -o w_3.fq -A -G -Q -L -3 -W 4 -M 20
expect_eq "06b -3 trims the low-quality 3' end" "31" "$(lens w_3.fq)"

fp -i cutright.fq -o w_r.fq -A -G -Q -L -r -W 4 -M 20
expect_eq "06c -r drops everything after the first bad window" "24" "$(lens w_r.fq)"

fp -i cutfront.fq -o w_none.fq -A -G -Q -L
expect_eq "06d no cut flag leaves the read untouched" "40" "$(lens w_none.fq)"

# ---------------------------------------------------------------------------
# 7. poly-G and poly-X tails
# ---------------------------------------------------------------------------
fp -i polyg.fq -o t_g.fq -A -Q -L -g
expect_eq "07a -g removes the poly-G tail" "60" "$(lens t_g.fq)"

fp -i polyg.fq -o t_ng.fq -A -Q -L -G
expect_eq "07b -G keeps the poly-G tail" "72" "$(lens t_ng.fq)"

fp -i polyx.fq -o t_x.fq -A -Q -L -G -x
expect_eq "07c -x removes the poly-A tail" "60" "$(lens t_x.fq)"

# ---------------------------------------------------------------------------
# 8. Overlap merging
# ---------------------------------------------------------------------------
if fp -i merge_1.fq -I merge_2.fq --merge --merged_out mg.fq -A -G; then
    expect_eq "08a --merge produces one merged read per pair" "20" "$(nrec mg.fq)"
    expect_eq "08b merged reads have the full fragment length" "200" "$(lens mg.fq)"
    if diff -q <(awk 'NR == 2' mg.fq) merge_expect.txt >/dev/null; then
        pass "08c merged sequence equals the original fragment"
    else
        fail "08c merged sequence equals the original fragment" "merged sequence differs"
    fi
else
    fail "08a --merge produces one merged read per pair" "scepter exited non-zero"
    fail "08b merged reads have the full fragment length" "scepter exited non-zero"
    fail "08c merged sequence equals the original fragment" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 9. Overlap base correction
# ---------------------------------------------------------------------------
if fp -i corr_1.fq -I corr_2.fq -o co_1.fq -O co_2.fq -c -A -G; then
    if diff -q <(awk 'NR == 2' co_2.fq) corr_expect.txt >/dev/null; then
        pass "09a -c repairs the low-quality mismatch in read 2"
    else
        fail "09a -c repairs the low-quality mismatch in read 2" "read 2 was not corrected"
    fi
    corrected="$(python3 -c 'import json;print(json.load(open("'"$TMP"'/r.json"))["scepter"]["run"]["corrected_bases"])' 2>/dev/null || echo err)"
    expect_eq "09b -c reports one corrected base per pair" "20" "$corrected"

    fp -i corr_1.fq -I corr_2.fq -o nc_1.fq -O nc_2.fq -A -G
    if diff -q <(awk 'NR == 2' nc_2.fq) corr_expect.txt >/dev/null; then
        fail "09c without -c the mismatch is left alone" "read 2 changed without -c"
    else
        pass "09c without -c the mismatch is left alone"
    fi
else
    fail "09a -c repairs the low-quality mismatch in read 2" "scepter exited non-zero"
    fail "09b -c reports one corrected base per pair" "scepter exited non-zero"
    fail "09c without -c the mismatch is left alone" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 10. Deduplication
# ---------------------------------------------------------------------------
fp -i dup_1.fq -I dup_2.fq -o dd_1.fq -O dd_2.fq --dedup -A -G -Q -L
expect_eq "10a --dedup keeps one copy of a duplicated pair" "2" "$(nrec dd_1.fq)"
expect_eq "10b --dedup keeps read 2 in lockstep" "2" "$(nrec dd_2.fq)"
expect_eq "10c --dedup keeps the unique pair" "1" "$(grep -c '^@uniq' dd_1.fq || true)"

fp -i dup_1.fq -I dup_2.fq -o nd_1.fq -O nd_2.fq -A -G -Q -L
expect_eq "10d without --dedup every pair survives" "3" "$(nrec nd_1.fq)"

# ---------------------------------------------------------------------------
# 11. UMI extraction
# ---------------------------------------------------------------------------
if fp -i umi_1.fq -I umi_2.fq -o um_1.fq -O um_2.fq --umi --umi_loc read1 --umi_len 8 -A -G -Q -L; then
    expect_eq "11a --umi_len 8 shortens read 1 by the UMI" "52" "$(lens um_1.fq)"
    expect_eq "11b --umi leaves read 2 untouched" "60" "$(lens um_2.fq)"
    got="$(names um_1.fq | sed 's/.*://')"
    if diff -q <(printf '%s\n' "$got") umi_expect.txt >/dev/null; then
        pass "11c the UMI moved into the read name"
    else
        fail "11c the UMI moved into the read name" "names carry the wrong UMI"
    fi
    if diff -q <(names um_1.fq) <(names um_2.fq) >/dev/null; then
        pass "11d both mates carry the same UMI-tagged name"
    else
        fail "11d both mates carry the same UMI-tagged name" "names differ between mates"
    fi
else
    fail "11a --umi_len 8 shortens read 1 by the UMI" "scepter exited non-zero"
    fail "11b --umi leaves read 2 untouched" "scepter exited non-zero"
    fail "11c the UMI moved into the read name" "scepter exited non-zero"
    fail "11d both mates carry the same UMI-tagged name" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 12. Output splitting
# ---------------------------------------------------------------------------
mkdir -p splitdir
if fp -i split.fq -o splitdir/sp.fq --split 3 -d 4 -A -G -Q -L; then
    nfiles="$(ls splitdir | wc -l | tr -d ' ')"
    expect_eq "12a --split 3 writes three numbered files" "3" "$nfiles"
    total=0
    for f in splitdir/*; do total=$((total + $(nrec "$f"))); done
    expect_eq "12b split files together hold every input record" "30000" "$total"
    if ls splitdir/0001.sp.fq splitdir/0002.sp.fq splitdir/0003.sp.fq >/dev/null 2>&1; then
        pass "12c split files use zero-padded numeric prefixes"
    else
        fail "12c split files use zero-padded numeric prefixes" "got: $(ls splitdir | paste -sd' ' -)"
    fi
else
    fail "12a --split 3 writes three numbered files" "scepter exited non-zero"
    fail "12b split files together hold every input record" "scepter exited non-zero"
    fail "12c split files use zero-padded numeric prefixes" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 13. JSON report
# ---------------------------------------------------------------------------
if "$SCEPTER" -i pe_1.fq -I pe_2.fq -o j_1.fq -O j_2.fq -j report.json -h report.html \
       --quiet --no-summary >/dev/null 2>&1; then
    observed=$(( $(nrec j_1.fq) + $(nrec j_2.fq) ))
    reported="$(python3 -c 'import json,sys;print(json.load(open("report.json"))["summary"]["after_filtering"]["total_reads"])' 2>/dev/null || echo parse-error)"
    expect_eq "13a JSON parses and reports the observed output read count" "$observed" "$reported"
    keys="$(python3 -c '
import json
d = json.load(open("report.json"))
need = ["summary", "filtering_result", "duplication", "read1_before_filtering",
        "read2_before_filtering", "read1_after_filtering", "read2_after_filtering",
        "command", "scepter"]
print("yes" if all(k in d for k in need) else "missing: " + ",".join(k for k in need if k not in d))' 2>/dev/null || echo parse-error)"
    expect_eq "13b JSON carries the fastp-compatible key set plus the scepter namespace" "yes" "$keys"
    curves="$(python3 -c '
import json
c = json.load(open("report.json"))["read1_before_filtering"]["quality_curves"]
keys = ["A", "T", "C", "G", "mean"]
if not all(k in c for k in keys):
    print("missing curve keys")
elif not all(len(c[k]) == 100 for k in keys):
    print("wrong curve length")
elif not all(15.0 <= v <= 45.0 for k in keys for v in c[k]):
    print("implausible Phred means")
else:
    print("yes")' 2>/dev/null || echo parse-error)"
    expect_eq "13c per-base quality curves are present and plausible" "yes" "$curves"
else
    fail "13a JSON parses and reports the observed output read count" "scepter exited non-zero"
    fail "13b JSON carries the fastp-compatible key set plus the scepter namespace" "scepter exited non-zero"
    fail "13c per-base quality curves are present and plausible" "scepter exited non-zero"
fi

# Per-base curves must genuinely differ when the data says they should.
python3 - <<'PYEOF'
import random
random.seed(99)
# 'I' = Q40 on A, '5' = Q20 on T; C/G land in between.
with open("bybase.fq", "w") as fh:
    for i in range(200):
        seq, qual = [], []
        for c in range(40):
            b = "ATCG"[c % 4]
            seq.append(b)
            qual.append({"A": "I", "T": "5", "C": "?", "G": "7"}[b])
        fh.write("@bb_%d\n%s\n+\n%s\n" % (i, "".join(seq), "".join(qual)))
PYEOF
fp -i bybase.fq -o bb.out.fq -A -G -Q -L
bb="$(python3 -c '
import json, sys
c = json.load(open(sys.argv[1]))["read1_before_filtering"]["quality_curves"]
# Cycle 0 is an A, cycle 1 a T, cycle 2 a C, cycle 3 a G.
vals = (c["A"][0], c["T"][1], c["C"][2], c["G"][3])
ok = abs(vals[0] - 40) < 0.01 and abs(vals[1] - 20) < 0.01 and \
     abs(vals[2] - 30) < 0.01 and abs(vals[3] - 22) < 0.01
# A base absent from a cycle must report 0, not the overall mean.
zero = c["T"][0] == 0 and c["A"][1] == 0
print("yes" if ok and zero else "got %r zero=%r" % (vals, zero))' "$TMP/r.json" 2>/dev/null || echo parse-error)"
expect_eq "13d quality_curves are per-base, not the overall cycle mean" "yes" "$bb"

# ---------------------------------------------------------------------------
# 14. HTML report
# ---------------------------------------------------------------------------
if [ -s report.html ]; then
    pass "14a HTML report is non-empty"
else
    fail "14a HTML report is non-empty" "report.html missing or empty"
fi
if grep -q '<html' report.html 2>/dev/null; then
    pass "14b HTML report contains an <html> element"
else
    fail "14b HTML report contains an <html> element" "no <html> tag found"
fi
if grep -qE 'https?://' report.html 2>/dev/null; then
    fail "14c HTML report references no external URL" \
         "found: $(grep -oE 'https?://[^\"'\'' <)]*' report.html | sort -u | head -3 | paste -sd' ' -)"
else
    pass "14c HTML report references no external URL"
fi

# ---------------------------------------------------------------------------
# 15. Interleaved input
# ---------------------------------------------------------------------------
if fp --interleaved_in -i il.fq -o int_1.fq -O int_2.fq $PASSTHRU; then
    if cmp -s int_1.fq rt_1.fq && cmp -s int_2.fq rt_2.fq; then
        pass "15a --interleaved_in matches the equivalent two-file run"
    else
        fail "15a --interleaved_in matches the equivalent two-file run" "outputs differ"
    fi
    expect_eq "15b --interleaved_in reads every pair" "500" "$(nrec int_1.fq)"
else
    fail "15a --interleaved_in matches the equivalent two-file run" "scepter exited non-zero"
    fail "15b --interleaved_in reads every pair" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 16. phred+64 input
# ---------------------------------------------------------------------------
if fp -i p64.fq -o p33.fq -6 -A -G -Q -L; then
    expect_eq "16a -6 rewrites qualities in phred+33" "1" \
              "$(awk 'NR % 4 == 0' p33.fq | sort -u | grep -c '^I\{60\}$' || true)"
    q30="$(python3 -c 'import json;print(json.load(open("'"$TMP"'/r.json"))["summary"]["before_filtering"]["q30_rate"])' 2>/dev/null || echo err)"
    expect_eq "16b -6 scores phred+64 input as Q40" "1.0" "$(python3 -c "print(float('$q30'))" 2>/dev/null || echo err)"
    expect_eq "16c -6 leaves the sequences untouched" "60" "$(lens p33.fq)"
else
    fail "16a -6 rewrites qualities in phred+33" "scepter exited non-zero"
    fail "16b -6 scores phred+64 input as Q40" "scepter exited non-zero"
    fail "16c -6 leaves the sequences untouched" "scepter exited non-zero"
fi

# ---------------------------------------------------------------------------
# 17. Exit codes
# ---------------------------------------------------------------------------
"$SCEPTER" -i does_not_exist.fq -o x.fq --quiet --no-summary >/dev/null 2>missing.err
rc=$?
expect_eq "17a a missing input file exits non-zero" "yes" "$([ "$rc" -ne 0 ] && echo yes || echo no)"
expect_eq "17b a missing input file explains itself on stderr" "yes" \
          "$([ -s missing.err ] && echo yes || echo no)"

"$SCEPTER" -i se.fq -o x.fq -q 999 --quiet --no-summary >/dev/null 2>badopt.err
rc=$?
expect_eq "17c an out-of-range option value exits non-zero" "yes" "$([ "$rc" -ne 0 ] && echo yes || echo no)"
expect_eq "17d an out-of-range option value explains itself on stderr" "yes" \
          "$([ -s badopt.err ] && echo yes || echo no)"

"$SCEPTER" -i se.fq -o x.fq --no-such-flag --quiet --no-summary >/dev/null 2>badflag.err
rc=$?
expect_eq "17e an unknown option exits non-zero" "yes" "$([ "$rc" -ne 0 ] && echo yes || echo no)"

"$SCEPTER" --version >/dev/null 2>&1
expect_eq "17f --version exits zero" "0" "$?"

# ---------------------------------------------------------------------------
# Extra: --out-dir places auto-named outputs and reports together
# ---------------------------------------------------------------------------
mkdir -p od
fp -i pe_1.fq -I pe_2.fq --out-dir od -A -G -Q -L -j "$TMP/r.json" -h "$TMP/r.html"
expect_eq "18a --out-dir auto-names the FASTQ outputs" "yes" \
          "$([ -s od/pe_1.fq ] && [ -s od/pe_2.fq ] && echo yes || echo no)"
"$SCEPTER" -i pe_1.fq -I pe_2.fq --out-dir od2 --quiet --no-summary >/dev/null 2>&1
expect_eq "18b --out-dir also holds the default reports" "yes" \
          "$([ -s od2/scepter.json ] && [ -s od2/scepter.html ] && echo yes || echo no)"
"$SCEPTER" -i pe_1.fq -I pe_2.fq --out-dir od3 -j explicit.json -h explicit.html \
    --quiet --no-summary >/dev/null 2>&1
expect_eq "18c an explicit -j/-h path still wins over --out-dir" "yes" \
          "$([ -s explicit.json ] && [ ! -e od3/scepter.json ] && echo yes || echo no)"

# ---------------------------------------------------------------------------
# Extra: gzip round-trip and the depth estimate
# ---------------------------------------------------------------------------
gzip -c pe_1.fq > gz_1.fq.gz
gzip -c pe_2.fq > gz_2.fq.gz
fp -i gz_1.fq.gz -I gz_2.fq.gz -o gz_out_1.fq.gz -O gz_out_2.fq.gz $PASSTHRU
if [ -s gz_out_1.fq.gz ] && gzip -t gz_out_1.fq.gz 2>/dev/null; then
    expect_eq "19a gzip input/output round-trips" "yes" \
              "$(cmp -s <(gzip -dc gz_out_1.fq.gz) pe_1.fq && echo yes || echo no)"
else
    fail "19a gzip input/output round-trips" "output is missing or not valid gzip"
fi

# ---------------------------------------------------------------------------
# 20. Whole-file sampling for auto-detection
#
# The fixture's head and tail differ systematically: the first half is Q40 and
# GC-free, the second half is Q2 and all-GC. A sample drawn from the head only
# sees the first half; a sample drawn from the whole file must see both.
# ---------------------------------------------------------------------------
python3 - "$TMP" <<'PYEOF'
import gzip, os, random, sys
out = sys.argv[1]
random.seed(4242)

recs = []
for i in range(60000):                      # head: AT-only, Q40
    s = ''.join(random.choice('AT') for _ in range(100))
    recs.append("@HEAD:1:FC:1:1101:%d:%d 1:N:0:AC\n%s\n+\n%s\n" % (i, i, s, 'I' * 100))
for i in range(60000):                      # tail: GC-only, Q2
    s = ''.join(random.choice('GC') for _ in range(100))
    recs.append("@TAIL:1:FC:1:2222:%d:%d 1:N:0:AC\n%s\n+\n%s\n" % (i, i, s, '#' * 100))
text = ''.join(recs)
open(os.path.join(out, "headtail.fq"), 'w').write(text)
with gzip.open(os.path.join(out, "headtail.fq.gz"), 'wt') as fh:
    fh.write(text)

# Every quality line starts with '@' (Q31), which a naive record-boundary search
# would mistake for a header when it seeks into the middle of the file.
amb = []
for i in range(80000):
    s = ''.join(random.choice('ACGT') for _ in range(60))
    amb.append("@amb_%d\n%s\n+\n%s\n" % (i, s, '@' * 60))
open(os.path.join(out, "atqual.fq"), 'w').write(''.join(amb))
PYEOF

# Like fp(), but the reports land where the test asks instead of in r.json.
fpr() { "$SCEPTER" "$@" --quiet --no-summary -h "$TMP/r.html" >"$TMP/out.log" 2>"$TMP/err.log"; }

jq_num() {  # file jsonpath-ish: read scepter.detection.<key>
    python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["scepter"]["detection"][sys.argv[2]])' \
        "$1" "$2" 2>/dev/null || echo err
}

fpr -i headtail.fq --qc-only -j s_plain.json --sample-reads 6000
fpr -i headtail.fq.gz --qc-only -j s_gz.json --sample-reads 6000
fpr -i headtail.fq --qc-only -j s_head.json --sample-reads 6000 --sample-head

expect_eq "20a plain input is sampled by stratified seeking" "stratified" \
          "$(jq_num s_plain.json sample_strategy)"
expect_eq "20b gzip input is sampled by a one-pass reservoir" "reservoir" \
          "$(jq_num s_gz.json sample_strategy)"
expect_eq "20c --sample-head keeps the old head-only sampling" "head" \
          "$(jq_num s_head.json sample_strategy)"

# Head is Q40/0% GC, tail is Q2/100% GC, so a whole-file sample lands near the
# midpoint of both and a head sample does not.
for f in s_plain s_gz; do
    q="$(jq_num $f.json sample_mean_quality)"
    g="$(jq_num $f.json sample_gc_content)"
    expect_eq "20d ${f#s_} sample spans head and tail (mean Q near 21)" "yes" \
              "$(python3 -c "print('yes' if 18.0 <= float('$q') <= 24.0 else 'got %s' % '$q')" 2>/dev/null || echo err)"
    expect_eq "20e ${f#s_} sample spans head and tail (GC near 50%)" "yes" \
              "$(python3 -c "print('yes' if 0.40 <= float('$g') <= 0.60 else 'got %s' % '$g')" 2>/dev/null || echo err)"
done

qh="$(jq_num s_head.json sample_mean_quality)"
gh="$(jq_num s_head.json sample_gc_content)"
expect_eq "20f head-only sampling really does see only the head" "yes" \
          "$(python3 -c "print('yes' if float('$qh') > 39.0 and float('$gh') < 0.05 else 'got Q=%s GC=%s' % ('$qh', '$gh'))" 2>/dev/null || echo err)"

# The reservoir pass counts every record, so the report carries an exact total.
expect_eq "20g the reservoir pass reports the exact record count" "120000" \
          "$(jq_num s_gz.json records_scanned)"
expect_eq "20h the exact-count flag is set for the reservoir pass" "True" \
          "$(jq_num s_gz.json exact_record_count)"

# Resynchronisation must require the full 4-line shape: a '@' at the start of a
# quality line is not a record boundary.
fpr -i atqual.fq --qc-only -j s_amb.json --sample-reads 5000
qa="$(jq_num s_amb.json sample_mean_quality)"
expect_eq "20i seeking resynchronises past quality lines that start with '@'" "yes" \
          "$(python3 -c "print('yes' if abs(float('$qa') - 31.0) < 0.001 else 'got %s' % '$qa')" 2>/dev/null || echo err)"
expect_eq "20j the resynchronised sample is the requested size" "5000" \
          "$(jq_num s_amb.json sample_reads_read1)"

# ---------------------------------------------------------------------------
# 21. Contamination screening
# ---------------------------------------------------------------------------
CONTAM_DB="$ROOT/data/contam_db.bin.gz"
SLICES="$ROOT/tests/data/contam_test_slices.fa.gz"

cverdict() {
    python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["scepter"]["contamination"][sys.argv[2]])' \
        "$1" "$2" 2>/dev/null || echo err
}

# Off by default, whatever else is configured.
fpr -i se.fq --qc-only -j c_off.json
expect_eq "21a contamination screening is off unless asked for" "False" \
          "$(cverdict c_off.json enabled)"

# A missing database must warn and let the run finish, not abort it.
if "$SCEPTER" -i se.fq --qc-only --contam-db no_such_db.bin.gz -j c_missing.json \
       -h "$TMP/r.html" --no-summary >/dev/null 2>c_missing.err; then
    pass "21b a missing contamination database does not fail the run"
else
    fail "21b a missing contamination database does not fail the run" "scepter exited non-zero"
fi
expect_eq "21c a missing contamination database is reported as a warning" "yes" \
          "$(grep -qi 'contamination' c_missing.err && echo yes || echo no)"

if [ -s "$CONTAM_DB" ] && [ -s "$SLICES" ]; then
    python3 - "$TMP" "$SLICES" <<'PYEOF'
import gzip, random, sys
out, slices = sys.argv[1], sys.argv[2]
random.seed(31337)
COMP = str.maketrans("ACGT", "TGCA")

seqs, name = {}, None
for line in gzip.open(slices, "rt"):
    if line[0] == ">":
        name = line[1:].strip()
        seqs[name] = []
    else:
        seqs[name].append(line.strip())
seqs = {k: ''.join(v) for k, v in seqs.items()}

def sim(genome, n, tag, fh):
    for i in range(n):
        p = random.randrange(0, len(genome) - 150)
        r = list(genome[p:p + 150])
        if random.random() < 0.5:
            r = list(''.join(r)[::-1].translate(COMP))
        for j in range(150):                     # ~0.2% substitution error
            if random.random() < 0.002:
                r[j] = random.choice("ACGT")
        fh.write("@%s_%d\n%s\n+\n%s\n" % (tag, i, ''.join(r), 'I' * 150))

ec = seqs["Escherichia coli"]
kp = seqs["Klebsiella pneumoniae"]

with open(out + "/pure_ec.fq", "w") as fh:
    sim(ec, 20000, "ec", fh)
with open(out + "/mix_ec_kp.fq", "w") as fh:
    sim(ec, 10000, "ec", fh)
    sim(kp, 10000, "kp", fh)
# Negative control: the same base composition with every 31-mer destroyed, so it
# belongs to no species on the panel and to nothing else either.
shuffled = list(ec)
random.shuffle(shuffled)
with open(out + "/offpanel.fq", "w") as fh:
    sim(''.join(shuffled), 20000, "off", fh)
PYEOF

    fpr -i pure_ec.fq --qc-only --check-contamination -j c_pure.json
    expect_eq "21d a pure library is called pure" "pure" "$(cverdict c_pure.json verdict)"
    expect_eq "21e a pure library names the right species" "Escherichia coli" \
              "$(cverdict c_pure.json dominant_species)"

    fpr -i mix_ec_kp.fq --qc-only --check-contamination -j c_mix.json
    expect_eq "21f a 50:50 mixture is called mixed" "mixed" "$(cverdict c_mix.json verdict)"
    mixfrac="$(python3 -c '
import json
c = json.load(open("c_mix.json"))["scepter"]["contamination"]
by = {s["name"]: s for s in c["species"] if s["present"]}
need = ["Escherichia coli", "Klebsiella pneumoniae"]
if not all(n in by for n in need):
    print("missing: " + ",".join(n for n in need if n not in by))
elif not all(0.35 <= by[n]["relative_abundance"] <= 0.65 for n in need):
    print("skewed: " + ",".join("%s=%.3f" % (n, by[n]["relative_abundance"]) for n in need))
elif len(by) != 2:
    print("extra species called: " + ",".join(sorted(by)))
else:
    print("yes")' 2>/dev/null || echo parse-error)"
    expect_eq "21g both mixture members are called at roughly 50% each" "yes" "$mixfrac"

    fpr -i offpanel.fq --qc-only --check-contamination -j c_neg.json
    expect_eq "21h a species outside the panel is not confidently called" \
              "no panel species detected" "$(cverdict c_neg.json verdict)"
    expect_eq "21i a species outside the panel names nothing" "" \
              "$(cverdict c_neg.json dominant_species)"

    # The screen must reach every reporting surface.
    "$SCEPTER" -i pure_ec.fq --qc-only --check-contamination -j c_rep.json -h c_rep.html \
        --tsv c_rep.tsv --quiet --no-summary >/dev/null 2>&1
    expect_eq "21j the TSV carries the contamination verdict" "pure" \
              "$(awk -F'\t' '$1 == "contamination.verdict" { print $2 }' c_rep.tsv)"
    expect_eq "21k the HTML report has a contamination section" "yes" \
              "$(grep -q 'Contamination screen' c_rep.html && echo yes || echo no)"
    if grep -qE 'https?://' c_rep.html 2>/dev/null; then
        fail "21l the contamination HTML stays self-contained" "found an external URL"
    else
        pass "21l the contamination HTML stays self-contained"
    fi
    expect_eq "21m the terminal summary carries a contamination line" "yes" \
              "$("$SCEPTER" -i pure_ec.fq --qc-only --check-contamination -j c_sum.json \
                    -h "$TMP/r.html" 2>&1 >/dev/null | grep -qi 'contamination' && echo yes || echo no)"
else
    for t in "21d a pure library is called pure" \
             "21e a pure library names the right species" \
             "21f a 50:50 mixture is called mixed" \
             "21g both mixture members are called at roughly 50% each" \
             "21h a species outside the panel is not confidently called" \
             "21i a species outside the panel names nothing" \
             "21j the TSV carries the contamination verdict" \
             "21k the HTML report has a contamination section" \
             "21l the contamination HTML stays self-contained" \
             "21m the terminal summary carries a contamination line"; do
        skip "$t" "data/contam_db.bin.gz not built; run tools/build_contam_db.py"
    done
fi

echo
echo "-----------------------------------------"
if [ "$SKIPPED" -ne 0 ]; then
    printf 'skipped %d\n' "$SKIPPED"
fi
printf 'total %d   passed %d   failed %d\n' "$((PASSED + FAILED))" "$PASSED" "$FAILED"
if [ "$FAILED" -ne 0 ]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
exit 0
