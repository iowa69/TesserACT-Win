#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
#
# Runs the engines' own test suites against the staged, transformed source and the
# programs built from it.
#
#     bash engines/ci/run_upstream_suites.sh linux
#     bash engines/ci/run_upstream_suites.sh windows      (inside an MSYS2 UCRT64 shell)
#
# Run it after "python engines/build.py --target <platform>". Suites:
#
#   tesseract-e2e     tests/run_tests.sh of TesserACT: the end-to-end suite, with the
#                     checksums of four output files that upstream recorded on Linux
#   tesseract-flags   tests/check_flags.sh of TesserACT: every documented option works
#   tesseract-memory  tests/test_ec_memory_budget.py: the memory limit stops a run in time
#   tesseract-cpp     the C++ unit and component tests (on Windows without the programs
#                     named in engines/win/tesseract/tests-skip.txt)
#   scepter-e2e       tests/run_tests.sh of scepter
#   scepter-flags     tests/check_flags.sh of scepter
#
# Nothing under tests/ is edited: the scripts are run as upstream wrote them. On Windows
# a check that cannot pass there for a known reason is listed, with the reason, in
# engines/ci/expected-fail-windows.txt; any other failed check fails this script.
#
# Exit status 0 only when every suite passed. A summary is printed at the end and every
# suite's full output is kept under build/logs/upstream-suites-<platform>/.
#
# Settings (environment):
#   ENGINES_DIR   folder with the built programs    (default build/engines/<platform>)
#   SUITES_JOBS   compile jobs for the C++ tests    (default 4)
#   SUITES_ONLY   run only these suites, comma separated (for example "scepter-e2e,scepter-flags")
#   SUITES_KEEP   1 keeps the working folder and the built C++ test programs
#   SUITES_WORK   working folder, deleted first  (default build/upstream-suites/<platform>)
#   SUITES_LOG_DIR  where the output is kept, deleted first  (default build/logs/upstream-suites-<platform>)
#   SUITES_NO_INSTALL  1: on Windows, do not install the MSYS2 python package when it is missing

set -u

PLATFORM="${1:-}"
case "$PLATFORM" in
    linux|windows) ;;
    *) echo "usage: bash engines/ci/run_upstream_suites.sh linux|windows" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENGINES_DIR="${ENGINES_DIR:-$REPO/build/engines/$PLATFORM}"
[ -d "$ENGINES_DIR" ] && ENGINES_DIR="$(cd "$ENGINES_DIR" && pwd)"
STAGE="$REPO/build/stage/$PLATFORM"
WORK="${SUITES_WORK:-$REPO/build/upstream-suites/$PLATFORM}"
LOGS="${SUITES_LOG_DIR:-$REPO/build/logs/upstream-suites-$PLATFORM}"
JOBS="${SUITES_JOBS:-4}"
ONLY="${SUITES_ONLY:-}"
ONLY="${ONLY//[[:space:]]/}"          # "a, b" means "a,b"
EXPECTED_FAIL="$HERE/expected-fail-windows.txt"
EXE=""
[ "$PLATFORM" = windows ] && EXE=".exe"

die() { echo "run_upstream_suites: $*" >&2; exit 2; }

# ----------------------------------------------------------------------------- what must be there
T_STAGE="$STAGE/tesseract"
S_STAGE="$STAGE/scepter"
[ -f "$T_STAGE/tests/run_tests.sh" ] || die "no staged TesserACT source in $T_STAGE. Run: python engines/build.py --target $PLATFORM"
[ -f "$S_STAGE/tests/run_tests.sh" ] || die "no staged scepter source in $S_STAGE. Run: python engines/build.py --target $PLATFORM"
for program in "$ENGINES_DIR/tesseract-asm$EXE" "$ENGINES_DIR/scepter$EXE" \
               "$T_STAGE/tesseract-asm$EXE" "$T_STAGE/tesseract-model$EXE"; do
    [ -f "$program" ] || die "$program is missing. Run: python engines/build.py --target $PLATFORM"
done
# The end-to-end suite of TesserACT runs the program that the build left inside the staged
# tree. It must be the one that is shipped, or the suite would prove another program.
cmp -s "$T_STAGE/tesseract-asm$EXE" "$ENGINES_DIR/tesseract-asm$EXE" \
    || die "$ENGINES_DIR/tesseract-asm$EXE is not the program of the last build in $T_STAGE. Build again, or set ENGINES_DIR to the folder that build wrote to."

# ----------------------------------------------------------------------------- which Python
# PY_FIXTURES makes the read files of the bash suites (called as "python3" by them).
# PY_BUILD runs engines/win/tesseract/recipe.py, which starts the native compiler.
PY_BUILD=""
if [ "$PLATFORM" = linux ]; then
    command -v python3 >/dev/null 2>&1 || die "python3 is needed to make the test reads"
    PY_BUILD="$(command -v python3)"
else
    command -v cygpath >/dev/null 2>&1 || die "on Windows this script must run inside an MSYS2 shell (msys2 {0} in the workflow)"
    # MSYS2 turns POSIX paths in arguments and variables into Windows paths when it starts a
    # native program. The suites rely on it: make sure nothing has switched it off.
    unset MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL MSYS2_ENV_CONV_EXCL
    # A checkout that converted line ends would give bash scripts it cannot run and read
    # files with CRLF. .gitattributes ("* -text") prevents it; say so if it happened anyway.
    if grep -q $'\r$' "$T_STAGE/tests/run_tests.sh" "$S_STAGE/tests/run_tests.sh"; then
        die "the test scripts have Windows line ends: git converted them at checkout. Keep '* -text' in .gitattributes (or 'git config core.autocrlf false') and check out again."
    fi
    # The read files must be made by the MSYS2 Python: a native Windows Python writes CRLF
    # (seven scepter checks fail on that) and cannot open the /tmp/... paths that the suites
    # put inside Python source.
    if [ ! -x /usr/bin/python3 ] && [ "${SUITES_NO_INSTALL:-0}" != 1 ]; then
        echo "installing the MSYS2 python package (needed to make the test reads) ..."
        pacman -S --noconfirm --needed python >/dev/null 2>&1 || true
    fi
    [ -x /usr/bin/python3 ] || die "the MSYS2 Python (/usr/bin/python3) is missing. Install it with: pacman -S --needed python"
    # The native Python: the first python on PATH that says it runs on Windows itself.
    while IFS= read -r candidate; do
        if "$candidate" -c 'import os, sys; sys.exit(0 if os.name == "nt" else 1)' >/dev/null 2>&1; then
            PY_BUILD="$candidate"; break
        fi
    done < <(type -ap python python3 2>/dev/null)
    [ -n "$PY_BUILD" ] || die "no native Windows Python on PATH (needed to build the C++ tests with engines/win/tesseract/recipe.py). The workflow needs actions/setup-python and 'path-type: inherit' for MSYS2."
fi
for tool in timeout mktemp md5sum sha256sum cmp diff awk sed grep sort head wc tr xargs gzip zcat; do
    command -v "$tool" >/dev/null 2>&1 || die "the suites need '$tool', which is not on PATH (MSYS2: coreutils, diffutils, gawk, sed, grep, findutils, gzip)"
done

# ----------------------------------------------------------------------------- working folders
# Both folders are deleted before the run (and the working folder after it): refuse a
# setting that would take the project, the programs, the staged source or the logs with it.
canonical() { realpath -m -- "$1" 2>/dev/null || printf '%s\n' "$1"; }
WORK_REAL="$(canonical "$WORK")"
LOGS_REAL="$(canonical "$LOGS")"
for doomed in "$WORK_REAL" "$LOGS_REAL"; do
    [ "$doomed" != / ] || die "refusing to delete / (SUITES_WORK or SUITES_LOG_DIR)"
    for kept in "$REPO" "$REPO/build" "$REPO/build/logs" "$REPO/build/engines" "$STAGE" "$ENGINES_DIR" "${HOME:-$REPO}"; do
        case "$(canonical "$kept")/" in
            "$doomed"/*) die "refusing to use $doomed as a working or log folder: it is deleted first, and it is or holds $kept. Set SUITES_WORK and SUITES_LOG_DIR to folders of their own." ;;
        esac
    done
done
case "$LOGS_REAL/" in "$WORK_REAL"/*) die "SUITES_LOG_DIR ($LOGS) lies inside SUITES_WORK ($WORK), which is deleted at the end: the logs would be lost." ;; esac
case "$WORK_REAL/" in "$LOGS_REAL"/*) die "SUITES_WORK ($WORK) lies inside SUITES_LOG_DIR ($LOGS): give each a folder of its own." ;; esac
rm -rf "$WORK" "$LOGS"
mkdir -p "$WORK/tmp" "$WORK/bin" "$LOGS"
WORK="$(cd "$WORK" && pwd)"
LOGS="$(cd "$LOGS" && pwd)"
export TMPDIR="$WORK/tmp"
if [ "$PLATFORM" = windows ]; then
    # "python3" for every suite is the MSYS2 one, whatever comes first on PATH.
    printf '#!/usr/bin/bash\nexec /usr/bin/python3 "$@"\n' > "$WORK/bin/python3"
    chmod +x "$WORK/bin/python3"
    export PATH="$WORK/bin:$PATH"
    # Seventeen C++ test programs create their folders as "/tmp/...": for a native program
    # that is \tmp on the drive it runs from, which must exist.
    drive="$(cygpath -m "$T_STAGE")"; drive="${drive%%:*}"
    DRIVE_TMP="/$(printf '%s' "$drive" | tr 'A-Z' 'a-z')/tmp"
    DRIVE_TMP_CREATED=0
    [ -d "$DRIVE_TMP" ] || { mkdir -p "$DRIVE_TMP" && DRIVE_TMP_CREATED=1; }
    ls -A "$DRIVE_TMP" > "$WORK/drive-tmp-before.txt" 2>/dev/null || true
fi

# Release defaults for every suite: none of our variables may leak into the programs.
while IFS= read -r v; do unset "$v"; done < <(compgen -e | grep -E '^(TESSERACT_|SCEPTER$|SCEPTER_PORT_|TS_COMPAT_)' || true)
unset RUN_TESTS_STRICT_XFAIL PYTEST_BASELINE

{
    echo "upstream suites, $PLATFORM, $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "programs  $ENGINES_DIR"
    echo "staged    $STAGE"
    echo "bash      $BASH_VERSION"
    echo "python3   $(python3 -c 'import sys, platform; print(sys.version.split()[0], sys.platform, platform.machine())' 2>&1)"
    echo "build py  $PY_BUILD"
    "$ENGINES_DIR/tesseract-asm$EXE" --version 2>&1 | grep -m1 TesserACT
    "$ENGINES_DIR/scepter$EXE" --version 2>&1 | grep -m1 scepter
} | tee "$LOGS/summary.txt"
echo

# ----------------------------------------------------------------------------- running and judging
SUMMARY=()
FAILED=0
RAN=0

wanted() { [ -z "$ONLY" ] || case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

# unexpected_fails SUITE LOG -- the failed checks of a bash suite that are not excused.
# A failed check is a line "FAIL  name ..." (leading blanks allowed). On Windows a check is
# excused when expected-fail-windows.txt has "SUITE|start of the name". Prints one per line.
unexpected_fails() {
    local suite="$1" log="$2" list=/dev/null
    [ "$PLATFORM" = windows ] && [ -f "$EXPECTED_FAIL" ] && list="$EXPECTED_FAIL"
    tr -d '\r' < "$log" | awk -v suite="$suite" -v list="$list" '
        BEGIN {
            while ((getline line < list) > 0) {
                sub(/\r$/, "", line)
                if (line ~ /^[ \t]*(#|$)/) continue
                bar = index(line, "|")
                if (bar == 0) continue
                if (substr(line, 1, bar - 1) != suite) continue
                name = substr(line, bar + 1); sub(/[ \t]+#.*$/, "", name); sub(/[ \t]+$/, "", name)
                if (name != "") excused[++n] = name
            }
        }
        /^[ \t]*FAIL  / {
            text = $0; sub(/^[ \t]*FAIL  /, "", text)
            ok = 0
            for (i = 1; i <= n; i++) if (index(text, excused[i]) == 1) { ok = 1; break }
            if (!ok) print text
        }'
}

# judge SUITE LOG STATUS SUMMARY_REGEX -- decide pass or fail for a bash suite and note it.
judge() {
    local suite="$1" log="$2" status="$3" regex="$4"
    local line total bad excused verdict
    line="$(tr -d '\r' < "$log" | grep -E "$regex" | tail -1)"
    total="$(tr -d '\r' < "$log" | grep -cE '^[ \t]*FAIL  ')"
    bad="$(unexpected_fails "$suite" "$log")"
    excused=$total
    [ -n "$bad" ] && excused=$((total - $(printf '%s\n' "$bad" | wc -l)))
    if [ -z "$line" ]; then
        verdict="FAILED  it stopped before its summary (exit $status)"
    elif [ -n "$bad" ]; then
        verdict="FAILED  $(printf '%s\n' "$bad" | wc -l | tr -d ' ') check(s) failed"
    elif [ "$status" -ne 0 ] && [ "$total" -eq 0 ]; then
        verdict="FAILED  exit $status without a failed check"
    else
        verdict="passed"
    fi
    [ "$excused" -gt 0 ] && verdict="$verdict; $excused failed check(s) excused by expected-fail-windows.txt"
    note "$suite" "$verdict" "${line:-no summary line}" "$log"
    if [ -n "$bad" ]; then
        printf '%s\n' "$bad" | sed 's/^/      FAIL  /'
        if [ "$PLATFORM" = windows ]; then
            echo "      If a check cannot pass on Windows for a reason that is understood, excuse it in"
            echo "      engines/ci/expected-fail-windows.txt with a line \"$suite|<start of its name>  # reason\"."
        fi
    fi
    case "$verdict" in FAILED*) [ -z "$line" ] && tail -25 "$log" | sed 's/^/      | /' ;; esac
}

note() {      # SUITE VERDICT DETAIL LOG
    local shown="${4#"$REPO"/}"
    SUMMARY+=("$(printf '%-17s %s' "$1" "$2")" "$(printf '%-17s   %s  [%s]' '' "$3" "$shown")")
    RAN=$((RAN + 1))
    case "$2" in FAILED*) FAILED=$((FAILED + 1)) ;; esac
    printf '%-17s %s\n                    %s\n' "$1" "$2" "$3"
}

run_logged() {      # LOG DIR COMMAND...   -> exit status of the command
    local log="$1" dir="$2"
    shift 2
    ( cd "$dir" && "$@" ) > "$log" 2>&1
}

# ----------------------------------------------------------------------------- TesserACT
if wanted tesseract-e2e; then
    echo "running tesseract-e2e ..."
    run_logged "$LOGS/tesseract-e2e.log" "$T_STAGE" bash tests/run_tests.sh
    judge tesseract-e2e "$LOGS/tesseract-e2e.log" $? '^[0-9]+ passed, [0-9]+ failed, '
fi

if wanted tesseract-flags; then
    echo "running tesseract-flags ..."
    run_logged "$LOGS/tesseract-flags.log" "$T_STAGE" bash tests/check_flags.sh \
        "$T_STAGE/tesseract-asm$EXE" "$T_STAGE/tesseract-model$EXE"
    judge tesseract-flags "$LOGS/tesseract-flags.log" $? '^[0-9]+ passed, [0-9]+ failed$'
fi

if wanted tesseract-memory; then
    echo "running tesseract-memory ..."
    rm -rf "$WORK/memory"
    run_logged "$LOGS/tesseract-memory.log" "$T_STAGE" python3 tests/test_ec_memory_budget.py \
        "$T_STAGE/tesseract-asm$EXE" "$T_STAGE/tesseract-asm$EXE" "$WORK/memory"
    status=$?
    if [ $status -eq 0 ] && grep -q '"budget_low"' "$LOGS/tesseract-memory.log"; then
        note tesseract-memory "passed" "a run over its memory limit stops; the limit does not change the contigs" "$LOGS/tesseract-memory.log"
    else
        note tesseract-memory "FAILED  exit $status" "tests/test_ec_memory_budget.py" "$LOGS/tesseract-memory.log"
        tail -15 "$LOGS/tesseract-memory.log" | sed 's/^/      | /'
    fi
fi

if wanted tesseract-cpp; then
    echo "building and running tesseract-cpp (the C++ unit and component tests) ..."
    recipe="$REPO/engines/win/tesseract/recipe.py"
    [ "$PLATFORM" = windows ] && recipe="$(cygpath -m "$recipe")"
    run_logged "$LOGS/tesseract-cpp.log" "$REPO" "$PY_BUILD" "$recipe" unit-tests \
        --target "$PLATFORM" --jobs "$JOBS" --run
    status=$?
    for extra in tests-run-$PLATFORM.log tests-$PLATFORM.log; do      # the programs' own output
        [ -f "$REPO/build/logs/tesseract/$extra" ] && cp "$REPO/build/logs/tesseract/$extra" "$LOGS/tesseract-cpp-$extra"
    done
    built="$(tr -d '\r' < "$LOGS/tesseract-cpp.log" | grep -E '^built [0-9]+, failed [0-9]+, skipped [0-9]+' | tail -1)"
    ran="$(tr -d '\r' < "$LOGS/tesseract-cpp.log" | grep -E '^ran [0-9]+, passed [0-9]+' | tail -1)"
    case "$ran" in "ran 0,"*) ran="" ;; esac       # no program was run: nothing was proven
    if [ $status -eq 0 ] && [ -n "$ran" ]; then
        note tesseract-cpp "passed" "${built%% (*}; $ran" "$LOGS/tesseract-cpp.log"
    else
        note tesseract-cpp "FAILED  exit $status" "${built:-nothing was built}; ${ran:-nothing was run}" "$LOGS/tesseract-cpp.log"
        tr -d '\r' < "$LOGS/tesseract-cpp.log" | grep -E '^(FAILED|failed:)' | head -40 | sed 's/^/      /'
        [ -n "$built" ] || tail -15 "$LOGS/tesseract-cpp.log" | sed 's/^/      | /'
    fi
    if [ "$PLATFORM" = windows ]; then
        skipped="$(tr -d '\r' < "$LOGS/tesseract-cpp.log" | grep -oE 'skipped [0-9]+ \(.*\)' | tail -1)"
        [ -n "$skipped" ] && echo "                    not built for Windows (tests-skip.txt): ${skipped#skipped }"
    fi
    [ "${SUITES_KEEP:-0}" = 1 ] || rm -f "$T_STAGE"/build/test_*
fi

# ----------------------------------------------------------------------------- scepter
# Some checks find the data file from the program's own folder, so the program is put
# beside a copy of data/ and tests/, as upstream's own build leaves it.
S_ROOT="$WORK/scepter"
if wanted scepter-e2e || wanted scepter-flags; then
    mkdir -p "$S_ROOT"
    cp "$ENGINES_DIR/scepter$EXE" "$S_ROOT/"
    cp -r "$S_STAGE/data" "$S_STAGE/tests" "$S_ROOT/"
fi

if wanted scepter-e2e; then
    echo "running scepter-e2e ..."
    run_logged "$LOGS/scepter-e2e.log" "$S_ROOT" bash tests/run_tests.sh
    judge scepter-e2e "$LOGS/scepter-e2e.log" $? '^total [0-9]+ +passed [0-9]+ +failed [0-9]+'
fi

if wanted scepter-flags; then
    echo "running scepter-flags ..."
    mkdir -p "$WORK/scepter-flags"          # the script writes scepter.json and scepter.html here
    run_logged "$LOGS/scepter-flags.log" "$WORK/scepter-flags" bash "$S_ROOT/tests/check_flags.sh" "$S_ROOT/scepter$EXE"
    judge scepter-flags "$LOGS/scepter-flags.log" $? '^[0-9]+ passed, [0-9]+ failed$'
fi

# ----------------------------------------------------------------------------- tidy up
if [ "$PLATFORM" = windows ]; then
    # The C++ tests clean up with "rm -rf", which cmd.exe does not have: remove what they left.
    if [ "$DRIVE_TMP_CREATED" = 1 ]; then
        rm -rf "$DRIVE_TMP"
    else
        ls -A "$DRIVE_TMP" 2>/dev/null | grep -vxFf "$WORK/drive-tmp-before.txt" | while IFS= read -r left; do
            rm -rf "${DRIVE_TMP:?}/$left"
        done
    fi
fi
if [ "${SUITES_KEEP:-0}" != 1 ]; then
    rm -rf "$WORK"
    rmdir "$(dirname "$WORK")" 2>/dev/null || true
fi

# ----------------------------------------------------------------------------- summary
{
    echo
    echo "======================================================================="
    echo "Upstream suites on $PLATFORM"
    echo "======================================================================="
    printf '%s\n' "${SUMMARY[@]}"
    echo "-----------------------------------------------------------------------"
    if [ "$RAN" -eq 0 ]; then
        echo "RESULT: FAILED. No suite was run (SUITES_ONLY=$ONLY names none of them)."
    elif [ "$FAILED" -eq 0 ]; then
        echo "RESULT: all $RAN suites passed. Full output: ${LOGS#"$REPO"/}/"
    else
        echo "RESULT: FAILED. $FAILED of $RAN suites did not pass. Full output: ${LOGS#"$REPO"/}/"
    fi
} | tee -a "$LOGS/summary.txt"
[ "$RAN" -gt 0 ] && [ "$FAILED" -eq 0 ]
