#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
# Run the REAL tesseract 1.6.2 script over the fixtures and record what it does.
#
#   run_golden.sh [TOOLCHAIN] [all|noreal|only:<fixture>]
#
# Run from the repository: the script is engines/tesseract/upstream/tesseract, the engine is
# build/engines/linux/tesseract-asm, the read sets are fixtures/ (python3 make_fixtures.py) and
# the tool directories are toolbin/ (./make_toolbin.sh). Everything this script writes goes
# under out/, which git ignores:
#   out/golden-new/            what the native tool set printed  (compare: compare_goldens.py)
#   out/golden-alt/TOOLCHAIN/  what another tool set printed     (compare: compare_toolchains.sh)
#   out/<fixture>/...          the output directories of the runs
# golden/ is the evidence the port is tested against and is NEVER written by default. To
# replace it on purpose, after a new upstream release, say so: GOLD_OVERRIDE=golden.
#
# TOOLCHAIN names a directory under toolbin/ that holds symlinks to exactly the external
# programs the script needs (bash env awk gzip paste df head od tr sed grep sort cut tee cat
# wc mkdir mv rm tail basename dirname readlink nproc mktemp, plus cp for the stub engine).
# The script runs with PATH set to that directory ONLY, so
#   * bwa and minimap2 cannot be found (the Windows app has no mapper), and
#   * the awk / coreutils flavour is chosen explicitly (native = what this machine resolves:
#     gawk 5.4.0 + uutils coreutils 0.2.2; gnu-mawk = GNU coreutils 9.5 + mawk 1.3.4; ...).
# The environment is emptied (env -i) except HOME, LANG=en_US.UTF-8, PATH, TESSERACT_ASM and
# whatever a case adds. All paths given to the script are relative to this directory, so the
# captured text carries no machine-specific path.
#
# Each case writes golden/<fixture>/<invocation>/
#   cmd.txt  exit_status.txt  stdout.txt  stderr.txt
#   preflight.txt  SUMMARY.txt  assembly_stats.tsv     (copies from -o OUT, when written)
#   out_tree.txt                                       (every file and directory left in OUT)
#   engine_out/                                        (the engine files postflight read)
#   calls/                                             (stub engine only: argv + TESSERACT_* env per call)
set -u
W="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$W/../.." && pwd)"
SCRIPT="${TESSERACT_SCRIPT:-$REPO/engines/tesseract/upstream/tesseract}"
ASM_REAL="${TESSERACT_ASM_REAL:-$REPO/build/engines/linux/tesseract-asm}"
[ -f "$SCRIPT" ] || { echo "no script $SCRIPT" >&2; exit 2; }
[ -d "$W/fixtures" ] || { echo "no fixtures: run python3 make_fixtures.py first" >&2; exit 2; }
FAKE="$W/fake_asm/fake_asm.sh"
TC="${1:-native}"; MODE="${2:-all}"
BIN="$W/toolbin/$TC"
[ -d "$BIN" ] || { echo "no toolchain directory $BIN: run ./make_toolbin.sh first" >&2; exit 2; }
[ "$MODE" = noreal ] || [ -x "$ASM_REAL" ] || { echo "no engine $ASM_REAL (build it, or use the mode noreal)" >&2; exit 2; }
if [ "$TC" = native ]; then GOLD="$W/out/golden-new"; OUTROOT="out"; else GOLD="$W/out/golden-alt/$TC"; OUTROOT="out/alt/$TC"; fi
# A later release: TESSERACT_SCRIPT=/path/to/new/tesseract GOLD_OVERRIDE=out/golden-next
GOLD="${GOLD_OVERRIDE:-$GOLD}"; OUTROOT="${OUTROOT_OVERRIDE:-$OUTROOT}"
case "$GOLD" in /*) ;; *) GOLD="$W/$GOLD" ;; esac
for m in bwa minimap2; do
    [ -z "$(PATH="$BIN" command -v "$m")" ] || { echo "$m is visible in $BIN" >&2; exit 2; }
done
ASM_USE="$ASM_REAL"; TMO=900; KEEP_OUT=0
LANG_USE=en_US.UTF-8; HOME_USE="$HOME"; SCRIPT_USE="$SCRIPT"
ncase=0

want() { case "$MODE" in only:*) [ "${MODE#only:}" = "$1" ] ;; *) true ;; esac; }

# c FIXTURE INVOCATION [NAME=VALUE ...] -- <tesseract arguments>
c() {
    local fx="$1" inv="$2"; shift 2
    local xenv=()
    while [ "$1" != "--" ]; do xenv+=("$1"); shift; done; shift
    want "$fx" || return 0
    local g="$GOLD/$fx/$inv"; rm -rf "$g"; mkdir -p "$g"
    local out="" prev="" a
    for a in "$@"; do { [ "$prev" = "-o" ] || [ "$prev" = "--out" ]; } && out="$a"; prev="$a"; done
    [ -n "$out" ] && [ "$KEEP_OUT" = 0 ] && rm -rf "${W:?}/$out"
    if [ "$ASM_USE" = "$FAKE" ]; then mkdir -p "$g/calls"; xenv+=("FAKE_REC=$g/calls"); fi
    {   printf 'cd %q && env -i HOME=%q LANG=%q PATH=%q TESSERACT_ASM=%q' "$W" "$HOME_USE" "$LANG_USE" "$BIN" "$ASM_USE"
        [ ${#xenv[@]} -gt 0 ] && printf ' %q' "${xenv[@]}"
        printf ' %q' "$SCRIPT_USE"; [ $# -gt 0 ] && printf ' %q' "$@"; printf '\n'; } > "$g/cmd.txt"
    ( cd "$W" && exec timeout "$TMO" env -i HOME="$HOME_USE" LANG="$LANG_USE" PATH="$BIN" TESSERACT_ASM="$ASM_USE" \
          ${xenv[@]+"${xenv[@]}"} "$SCRIPT_USE" "$@" ) > "$g/stdout.txt" 2> "$g/stderr.txt" < /dev/null
    echo $? > "$g/exit_status.txt"
    if [ -n "$out" ] && [ -d "$W/$out" ] && [ "$KEEP_OUT" = 0 ]; then
        local f
        for f in preflight.txt SUMMARY.txt assembly_stats.tsv; do [ -e "$W/$out/$f" ] && cp "$W/$out/$f" "$g/$f"; done
        ( cd "$W/$out" && find . -mindepth 1 | LC_ALL=C sort | while IFS= read -r p; do
              if [ -d "$p" ]; then printf '%s/\n' "$p"; else printf '%s\n' "$p"; fi; done ) > "$g/out_tree.txt"
        mkdir -p "$g/engine_out"
        for f in contigs.fasta scaffolds.fasta report.json p2_self_qa.tsv assembly.log \
                 attempt1_default_k/contigs.fasta attempt1_default_k/assembly.log; do
            [ -e "$W/$out/$f" ] && { mkdir -p "$g/engine_out/$(dirname "$f")"; cp "$W/$out/$f" "$g/engine_out/$f"; }
        done
    fi
    ncase=$((ncase+1))
    printf '%-28s %-24s exit %s\n' "$fx" "$inv" "$(cat "$g/exit_status.txt")"
}
# chk FIXTURE INVOCATION R1 R2 [more arguments]: preflight only
chk() { local fx="$1" inv="$2" r1="$3" r2="$4"; shift 4; c "$fx" "$inv" -- check -1 "fixtures/$fx/$r1" -2 "fixtures/$fx/$r2" "$@"; }
# full FIXTURE INVOCATION R1 R2 [more arguments]: preflight + assembly + postflight, real engine
full() { local fx="$1" inv="$2" r1="$3" r2="$4"; shift 4
         [ "$MODE" = noreal ] && return 0
         c "$fx" "$inv" -- -1 "fixtures/$fx/$r1" -2 "fixtures/$fx/$r2" -o "$OUTROOT/$fx/$inv" -t 4 "$@"; }
# pf NAME CANNED [NAME=VALUE ...] -- [more arguments]: real script, stub engine, 300-pair reads
pf() { local name="$1" src="$2"; shift 2
       local xenv=("FAKE_SRC=$W/fake_asm/cases/$src")
       while [ "$1" != "--" ]; do xenv+=("$1"); shift; done; shift
       ASM_USE="$FAKE"
       c _postflight "$name" "${xenv[@]}" -- -1 fixtures/very_thin/verythin_1.fq.gz -2 fixtures/very_thin/verythin_2.fq.gz \
           -o "$OUTROOT/_postflight/$name" -t 4 "$@"
       ASM_USE="$ASM_REAL"; }

# ============================================================== required fixtures
chk  good check good_1.fq.gz good_2.fq.gz
chk  good check_gs0.3 good_1.fq.gz good_2.fq.gz --genome-size 0.3
full good run good_1.fq.gz good_2.fq.gz
KEEP_OUT=1
[ "$MODE" = noreal ] || c good run_again_same_outdir -- -1 fixtures/good/good_1.fq.gz -2 fixtures/good/good_2.fq.gz -o "$OUTROOT/good/run" -t 4
KEEP_OUT=0
full good run_gs0.3 good_1.fq.gz good_2.fq.gz --genome-size 0.3

chk  thin check thin_1.fq.gz thin_2.fq.gz
chk  thin check_gs0.3 thin_1.fq.gz thin_2.fq.gz --genome-size 0.3
full thin run thin_1.fq.gz thin_2.fq.gz
full thin run_gs0.3 thin_1.fq.gz thin_2.fq.gz --genome-size 0.3

chk  adapter check adapter_1.fq.gz adapter_2.fq.gz
chk  adapter check_gs0.3 adapter_1.fq.gz adapter_2.fq.gz --genome-size 0.3
full adapter run adapter_1.fq.gz adapter_2.fq.gz
full adapter run_gs0.3 adapter_1.fq.gz adapter_2.fq.gz --genome-size 0.3

chk  trunc_gz check trunc_1.fq.gz trunc_2.fq.gz
full trunc_gz run_noforce trunc_1.fq.gz trunc_2.fq.gz
full trunc_gz run_force trunc_1.fq.gz trunc_2.fq.gz --force
full trunc_gz run_force_gs0.3 trunc_1.fq.gz trunc_2.fq.gz --force --genome-size 0.3
full trunc_gz run_force_keep_going trunc_1.fq.gz trunc_2.fq.gz --force --keep-going

chk  phred64 check phred64_1.fq.gz phred64_2.fq.gz
full phred64 run_force phred64_1.fq.gz phred64_2.fq.gz --force

chk  r2_short check r2short_1.fq.gz r2short_2.fq.gz
full r2_short run_force r2short_1.fq.gz r2short_2.fq.gz --force
chk  r1_short check r1short_1.fq.gz r1short_2.fq.gz
chk  mate_order check order_1.fq.gz order_2.fq.gz
chk  polyg check polyg_1.fq.gz polyg_2.fq.gz
chk  nbases check nbases_1.fq.gz nbases_2.fq.gz
chk  plain check plain_1.fq plain_2.fq
chk  plain check_mates_swapped plain_2.fq plain_1.fq
chk  same_file check same_1.fq.gz same_1.fq.gz
c    same_file check_other_spelling -- check -1 fixtures/same_file/same_1.fq.gz -2 fixtures/same_file/../same_file/same_1.fq.gz
chk  same_file check_hardlink same_1.fq.gz hardlink_to_same_1.fq.gz
chk  same_file check_symlink same_1.fq.gz symlink_to_same_1.fq.gz
chk  same_file check_identical_copy same_1.fq.gz copy_of_same_1.fq.gz
chk  crlf check crlf_1.fq crlf_2.fq
chk  empty check empty_1.fq.gz empty_2.fq.gz
full empty run_noforce empty_1.fq.gz empty_2.fq.gz
chk  nonfastq check text_1.txt text_2.txt
chk  two_member check twomember_1.fq.gz twomember_2.fq.gz

# ================================================================= extra fixtures
chk  very_thin check verythin_1.fq.gz verythin_2.fq.gz
full very_thin run verythin_1.fq.gz verythin_2.fq.gz
full very_thin run_k_given verythin_1.fq.gz verythin_2.fq.gz -k 21,33,55,77
chk  sample_limit check limit_1.fq.gz limit_2.fq.gz
chk  sample_limit_late_errors check late_1.fq.gz late_2.fq.gz
chk  threshold_edge check edge_1.fq.gz edge_2.fq.gz
chk  threshold_over check over_1.fq.gz over_2.fq.gz
chk  space_header check space_1.fq.gz space_2.fq.gz
chk  tab_header check tab_1.fq.gz tab_2.fq.gz
chk  tab_header_r2_only check tabr2_1.fq.gz tabr2_2.fq.gz
chk  dot_names check dot_1.fq.gz dot_2.fq.gz
chk  blank_tail_both check blank_1.fq blank_2.fq
chk  blank_tail_r2 check blankr2_1.fq blankr2_2.fq
chk  blank_tail_r1 check blankr1_1.fq blankr1_2.fq
chk  blank_middle check blankmid_1.fq blankmid_2.fq
chk  no_final_newline check nonl_1.fq nonl_2.fq
chk  r2_midrecord check mid_1.fq mid_2.fq
chk  crlf_space_names check crlfsp_1.fq crlfsp_2.fq
chk  crlf_len147 check crlf147_1.fq crlf147_2.fq
chk  crlf_r2_only check crlfr2_1.fq crlfr2_2.fq
chk  gz_trailing_garbage check garbage_1.fq.gz garbage_2.fq.gz
chk  gz_trailing_zeros check zeros_1.fq.gz zeros_2.fq.gz
chk  gz_of_nothing check gzempty_1.fq.gz gzempty_2.fq.gz
chk  magic_not_extension check lies_1.fq.gz lies_2.fq
chk  gz_crc_error check crc_1.fq.gz crc_2.fq.gz
chk  both_empty check e_1.fq e_2.fq
chk  badlen check badlen_1.fq.gz badlen_2.fq.gz
chk  badlen_before_bad check bb_1.fq.gz bb_2.fq.gz
chk  fasta_input check fa_1.fa fa_2.fa
chk  qual_all_J check qJ_1.fq.gz qJ_2.fq.gz
chk  qual_all_K check qK_1.fq.gz qK_2.fq.gz
chk  qual_unsampled check qun_1.fq.gz qun_2.fq.gz
chk  qual_space check qsp_1.fq.gz qsp_2.fq.gz
chk  qual_binned check qbin_1.fq.gz qbin_2.fq.gz
chk  len_one_240 check len240_1.fq.gz len240_2.fq.gz
chk  len_max_239 check len239_1.fq.gz len239_2.fq.gz
chk  lowercase_iupac check lower_1.fq.gz lower_2.fq.gz
for gsz in 0.000002 0.000001 0.00000096 0.0000000725 0.00000007 5.0 1e-6 5,5 0.3x 0 abc 0x10 -1 ""; do
    chk tiny29 "check_gs_${gsz:-EMPTY}" tiny_1.fq tiny_2.fq --genome-size "$gsz"
done
chk  tiny29 check tiny_1.fq tiny_2.fq
chk  "dir with space" check "my reads_1.fq.gz" "my reads_2.fq.gz"
ASM_USE="$FAKE"
c "dir with space" run_stub_engine "FAKE_SRC=$W/fake_asm/cases/ok" -- -1 "fixtures/dir with space/my reads_1.fq.gz" -2 "fixtures/dir with space/my reads_2.fq.gz" -o "$OUTROOT/dir with space/run stub engine" -t 4
ASM_USE="$ASM_REAL"
chk  utf8_bom check bom_1.fq bom_2.fq
chk  utf16le check utf16_1.fq utf16_2.fq
chk  cr_only check cr_1.fq cr_2.fq

# ================================================================ argument handling
c _args no_arguments --
c _args help -- --help
c _args version -- --version
c _args version_v -- -v
c _args models_help -- models
c _args check_missing_r2 -- check -1 fixtures/tiny29/tiny_1.fq
c _args run_missing_out -- -1 fixtures/tiny29/tiny_1.fq -2 fixtures/tiny29/tiny_2.fq
c _args threads_0 -- check -1 fixtures/tiny29/tiny_1.fq -2 fixtures/tiny29/tiny_2.fq -t 0
c _args threads_abc -- check -1 fixtures/tiny29/tiny_1.fq -2 fixtures/tiny29/tiny_2.fq -t abc
c _args threads_4x -- check -1 fixtures/tiny29/tiny_1.fq -2 fixtures/tiny29/tiny_2.fq -t 4x
c _args threads_empty -- check -1 fixtures/tiny29/tiny_1.fq -2 fixtures/tiny29/tiny_2.fq -t ""
c _args unknown_organism -- check -1 fixtures/tiny29/tiny_1.fq -2 fixtures/tiny29/tiny_2.fq --organism martian
c _args unknown_first_word -- frobnicate
c _args check_not_first -- -1 fixtures/tiny29/tiny_1.fq check -2 fixtures/tiny29/tiny_2.fq
c _args check_ignores_unknown_options -- check -1 fixtures/tiny29/tiny_1.fq -2 fixtures/tiny29/tiny_2.fq --bogus -o "$OUTROOT/_args/never_created" -k 21
c _args long_options_check -- check --read1 fixtures/tiny29/tiny_1.fq --read2 fixtures/tiny29/tiny_2.fq --threads 2
c _args missing_files_check -- check -1 fixtures/nope_1.fq -2 fixtures/nope_2.fq
c _args missing_files_run -- -1 fixtures/nope_1.fq -2 fixtures/nope_2.fq -o "$OUTROOT/_args/missing_files_run" -t 4
c _args missing_r2_only_run -- -1 fixtures/tiny29/tiny_1.fq -2 fixtures/nope_2.fq -o "$OUTROOT/_args/missing_r2_only_run" -t 4
c _args directory_as_input -- check -1 fixtures/tiny29 -2 fixtures/tiny29/tiny_2.fq
ASM_USE=""
c _args check_without_engine -- check -1 fixtures/tiny29/tiny_1.fq -2 fixtures/tiny29/tiny_2.fq
ASM_USE="$ASM_REAL"
TMO=10
c _args option_without_value_HANGS -- check -1 fixtures/tiny29/tiny_1.fq -2
TMO=900

# ===================================================== postflight branches (stub engine)
pf ok ok --
pf ok_gs0.01 ok -- --genome-size 0.01
pf ok_gs0.0125 ok -- --genome-size 0.0125
pf ok_gs0.013 ok -- --genome-size 0.013
pf ok_gs0.0077 ok -- --genome-size 0.0077
pf ok_gs0.0076 ok -- --genome-size 0.0076
pf n50_ties n50_ties --
pf malformed malformed --
pf malformed_keep_going malformed -- --keep-going
pf odd_headers odd_headers --
pf crlf_outputs crlf_outputs --
pf alarm alarm --
pf spike_guard_circ spike_guard_circ --
pf detect_ecoli detect_ecoli --
pf detect_ecoli_gs0.01 detect_ecoli -- --genome-size 0.01
pf rc_fail rc_fail --
pf rc_fail_keep_going rc_fail -- --keep-going
pf missing_report_gfa missing_report_gfa --
pf no_gfa no_gfa --
pf no_gfa_flag no_gfa -- --no-gfa
pf empty_contigs empty_contigs --
pf scaffolds scaffolds --
pf minimal_report minimal_report --
pf no_version no_version --
pf retry retry --
pf retry_k_given retry -- -k 21,33
pf retry_kmers_given retry -- --kmers 21,33
pf retry_k_attached retry -- -k21,33
pf retry_gs_tiny retry -- --genome-size 0.000004
pf retry_rcfail retry_rcfail --
pf kdepth_1.9 kdepth_1.9 --
pf kdepth_1.9_gs0.002 kdepth_1.9 -- --genome-size 0.002
pf kdepth_2.0 kdepth_2.0 --
pf rcfail_signature rcfail_signature --
pf env_override ok TESSERACT_P2_CIRC=off TESSERACT_DEEP_NORM=50 TESSERACT_P2_SPIKEIN= TESSERACT_UNRELATED=1 --
pf env_user_sketch ok TESSERACT_P2_DETECT_SKETCH=/nonexistent/user.sketch TESSERACT_P2_DETECT_REPORT=0 --
pf extra_args ok -- --mode careful --min-contig 500 -- -x --force -1 zzz --keep-going
pf long_options_and_order ok -- --keep-going --genome-size 0.01
ASM_USE="$FAKE"
c _postflight threads_default_nproc "FAKE_SRC=$W/fake_asm/cases/ok" -- --read1 fixtures/very_thin/verythin_1.fq.gz --read2 fixtures/very_thin/verythin_2.fq.gz --out "$OUTROOT/_postflight/threads_default_nproc"
# preflight fails before the read statistics (truncated gzip) and the run is forced: the retry
# threshold then uses 5 Mb, NOT --genome-size (500 bp < 1,000,000 -> retry; with 0.002 Mb it would not).
c _postflight retry_ignores_genome_size_after_failed_preflight "FAKE_SRC=$W/fake_asm/cases/kdepth_1.9" -- -1 fixtures/trunc_gz/trunc_1.fq.gz -2 fixtures/trunc_gz/trunc_2.fq.gz -o "$OUTROOT/_postflight/retry_ignores_genome_size_after_failed_preflight" -t 4 --force --genome-size 0.002
c _postflight run_word_and_threads "FAKE_SRC=$W/fake_asm/cases/ok" -- run -t 08 -1 fixtures/very_thin/verythin_1.fq.gz -2 fixtures/very_thin/verythin_2.fq.gz -o "$OUTROOT/_postflight/run_word_and_threads"
ASM_USE="$ASM_REAL"

# A byte-identical copy of the script in a place where no om2detect.sketch can be found
# (no ../share/tesseract, no models/ beside it, an empty HOME): the two detection variables
# must then be absent from the engine's environment.
if want _postflight; then
    NS="$W/$OUTROOT/_nosketch"; rm -rf "$NS"; mkdir -p "$NS/bin" "$NS/home"; cp "$SCRIPT" "$NS/bin/tesseract"
    SCRIPT_USE="$NS/bin/tesseract"; HOME_USE="$NS/home"
    pf no_sketch_installed detect_ecoli --
    SCRIPT_USE="$SCRIPT"; HOME_USE="$HOME"
fi
# A locale whose decimal separator is a comma (en_DK). gawk prints '.', mawk prints ','.
LANG_USE=en_DK.UTF-8
chk adapter check_locale_decimal_comma adapter_1.fq.gz adapter_2.fq.gz --genome-size 0.3
pf locale_decimal_comma alarm -- --genome-size 0.013
LANG_USE=en_US.UTF-8

echo "cases run: $ncase   golden: $GOLD"
