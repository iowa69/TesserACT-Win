#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Do the tests catch the mistakes a change to the port is likely to make?

    python3 tests/parity/port_mutations.py [word ...]      (Linux; about 7 minutes for all of them)

Each entry changes one line of ``tesseract_win/readcheck.py`` or ``postflight.py`` into a
plausible wrong variant (an unrounded limit, half-up rounding, CR stripped, a rule of the
block code dropped, ...) and runs ``tests/test_readcheck.py``, ``test_postflight.py`` and
``test_wrapper_parity.py`` on it. A mutation that no test catches means a rule is written
down but not pinned. With words, only the entries whose title holds one of them are run.

The working tree is never touched: the modules and the tests are copied to a temporary
folder (the goldens, fixtures and engines are linked from there) and changed in the copy.

An entry whose line is no longer in the source is reported as STALE: the code moved on and
the entry has to be rewritten. Exit status 1 when an entry is stale or not caught.

This is the companion of ``model/mutation_test.py``, which does the same to the auditor's
model of the bash script.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
R, P = "tesseract_win/readcheck.py", "tesseract_win/postflight.py"
TESTS = ["tests/test_readcheck.py", "tests/test_postflight.py", "tests/test_wrapper_parity.py"]

# (file, title, the line as it is, the line as a mistake would have it)
MUTATIONS = [
    (R, 'fraction judged unrounded',
     'adapter_fraction=float("%.4f" % (c.adapt / c.s if c.s else 0)),      # rounded BEFORE the limit',
     'adapter_fraction=(c.adapt / c.s if c.s else 0),'),
    (R, 'N fraction judged unrounded',
     'n_fraction=float("%.5f" % (c.nn / c.sumlen if c.sumlen else 0)),',
     'n_fraction=(c.nn / c.sumlen if c.sumlen else 0),'),
    (R, 'depth half-up',
     'depth = int("%.0f" % value)',
     'depth = int(value + 0.5)'),
    (R, 'no sample limit (block code)',
     'room = SAMPLE_ROWS - first',
     'room = 10 ** 9'),
    (R, 'no sample limit (row code)',
     'if c.n <= SAMPLE_ROWS:',
     'if True:'),
    (R, 'sample limit off by one (block)',
     'room = SAMPLE_ROWS - first',
     'room = SAMPLE_ROWS - first - 1'),
    (R, 'CRLF folded',
     'parts = block.split(b"\\n")',
     'parts = block.replace(b"\\r\\n", b"\\n").split(b"\\n")'),
    (R, 'every quality char',
     'QUALITY_STEP = 7 ',
     'QUALITY_STEP = 1 '),
    (R, 'lower-case n counted',
     'c.nn += b"".join(s1).count(b"N") + b"".join(s2).count(b"N")',
     'c.nn += b"".join(s1).upper().count(b"N") + b"".join(s2).upper().count(b"N")'),
    (R, 'phred64 >=',
     'elif low >= PHRED64_MIN and high > PHRED64_MAX:',
     'elif low >= PHRED64_MIN and high >= PHRED64_MAX:'),
    (R, 'same file by path',
     'return os.path.samefile(path1, path2)',
     'return os.path.abspath(path1) == os.path.abspath(path2)'),
    (R, 'trailing garbage accepted',
     '                if pending[:2] != _GZIP_MAGIC:\n                    return False',
     '                if pending[:2] != _GZIP_MAGIC:\n                    return True'),
    (R, 'zeros then garbage accepted',
     '                if pending.count(0) != len(pending):\n                    return False',
     '                if pending.count(0) != len(pending):\n                    return True'),
    (R, 'trailing zeros rejected',
     '                if pending[0] == 0:\n                    zeros = True\n                    continue',
     '                if pending[0] == 0:\n                    return False'),
    (R, 'incomplete last row dropped',
     'return (count + 3) >> 2 if self.ended else count >> 2',
     'return count >> 2'),
    (R, 'all_start checks min only',
     'return min(lines)[:1] == first and max(lines)[:1] == first',
     'return min(lines)[:1] == first'),
    (R, 'all_start checks max only',
     'return min(lines)[:1] == first and max(lines)[:1] == first',
     'return max(lines)[:1] == first'),
    (R, 'suffix kept on second name (block)',
     '                    if y[-2:] in _SUFFIXES:\n                        y = y[:-2]',
     '                    if False:\n                        y = y[:-2]'),
    (R, 'names not cut at space (row)',
     '    cut = name.find(b" ")',
     '    cut = -1'),
    (R, 'names not cut at space (block)',
     'cut1 = list(map(_item0, map(bytes.partition, h1, repeat(b" "))))',
     'cut1 = list(h1)'),
    (R, 'machine lines skipped after a failed check',
     'if one.state != "unreadable" and two.state != "unreadable":      # P23',
     'if not any(l.verdict == "FAIL" for l in lines):'),
    (R, 'machine lines kept when a file is unreadable',
     'if one.state != "unreadable" and two.state != "unreadable":      # P23',
     'if True:'),
    (R, 'badrec not set by length mismatch (block)',
     '                c.badlen += 1\n                if not c.badrec:\n                    c.badrec = first + i + 1',
     '                c.badlen += 1\n                if False:\n                    c.badrec = first + i + 1'),
    (R, 'badrec off by one (block)',
     '                if not c.badrec:\n                    c.badrec = first + i + 1',
     '                if not c.badrec:\n                    c.badrec = first + i'),
    (R, 'disk need computed in another order',
     'need_kb = (one.size + two.size) * 3 // 1024 + DISK_BASE_KB',
     'need_kb = (one.size + two.size) // 1024 * 3 + DISK_BASE_KB'),
    (R, 'memory limit exclusive',
     'if mem_kb >= MEMORY_OK_KB:',
     'if mem_kb > MEMORY_OK_KB:'),
    (R, 'disk limit exclusive',
     'if avail_kb >= need_kb:',
     'if avail_kb > need_kb:'),
    (R, 'depth low limit inclusive',
     'elif depth < DEPTH_LOW:',
     'elif depth <= DEPTH_LOW:'),
    (R, 'depth thin limit inclusive',
     'if depth < DEPTH_THIN:',
     'if depth <= DEPTH_THIN:'),
    (R, 'depth deep limit inclusive',
     'elif depth > DEPTH_DEEP:',
     'elif depth >= DEPTH_DEEP:'),
    (R, 'adapter limit inclusive',
     'if st.adapter_fraction > ADAPTER_LIMIT:',
     'if st.adapter_fraction >= ADAPTER_LIMIT:'),
    (R, 'polyg limit inclusive',
     'if st.polyg_fraction > POLYG_LIMIT:',
     'if st.polyg_fraction >= POLYG_LIMIT:'),
    (R, 'N limit inclusive',
     'if st.n_fraction > N_LIMIT:',
     'if st.n_fraction >= N_LIMIT:'),
    (R, 'short2 rule looks at one field',
     'if nf == 4 or (nf > 4 and f[4] == b"" and f[5] == b""):',
     'if nf == 4 or (nf > 4 and f[4] == b""):'),
    (R, 'short1 rule ignores field count',
     'if f[0] == b"" and nf <= 5:',
     'if f[0] == b"":'),
    (R, 'first mismatch keeps the @ (block)',
     'c.firstm = x[1:]',
     'c.firstm = x'),
    (R, 'unfinished row taken early',
     'whole = len(self.lines) & ~3',
     'whole = len(self.lines)'),
    (R, 'tab flag not set',
     '        if has_tab or self.tail_tab:\n            self.tabs = True',
     '        if False:\n            self.tabs = True'),
    (R, 'tab in the unfinished line forgotten',
     '        self.tail_tab = has_tab and b"\\t" in last',
     '        self.tail_tab = False'),
    (R, 'tab in the last line forgotten',
     '            self.tail = []\n            if self.tail_tab:\n                self.tabs = True',
     '            self.tail = []'),
    (R, 'tab flag dropped too early',
     '                self.tabs = any(b"\\t" in line for line in self.lines)',
     '                self.tabs = False'),
    (R, 'long line pieces lose tab',
     '            self.tail.append(block)\n            if has_tab:\n                self.tail_tab = True',
     '            self.tail.append(block)'),
    (R, 'polyG tail 9',
     'POLYG_TAIL = b"G" * 10 ',
     'POLYG_TAIL = b"G" * 9  '),
    (R, 'quality outside range kept',
     '_QUALITY_CODE = bytes(c if 33 <= c < 127 else 0 for c in range(256))',
     '_QUALITY_CODE = bytes(range(256))'),
    (R, 'library limit 250',
     'LONG_READ_LENGTH = 240 ',
     'LONG_READ_LENGTH = 241 '),
    (R, 'base name not used',
     'pf("OK", "gzip", "\'%s\' is complete" % os.path.basename(f.shown))',
     'pf("OK", "gzip", "\'%s\' is complete" % f.shown)'),
    (R, 'empty before unreadable',
     '    if info.st_size == 0:\n        return _File(path, shown, "empty")',
     '    if info.st_size < 0:\n        return _File(path, shown, "empty")'),
    (R, 'blank row not noticed',
     '        if a == _BLANK_ROW:\n            c.blank1 += 1',
     '        if False:\n            c.blank1 += 1'),
    (R, 'cancel ignored',
     '        if self.cancel is not None and self.cancel.is_set():\n            raise Cancelled()',
     '        if False:\n            raise Cancelled()'),
    (R, 'progress 1.0 early',
     'self.progress(min(fraction, 0.999))',
     'self.progress(fraction)'),
    (P, 'N50 with >',
     'if not n50 and running >= total * 0.5:',
     'if not n50 and running > total * 0.5:'),
    (P, 'N90 with >',
     'if not n90 and running >= total * 0.9:',
     'if not n90 and running > total * 0.9:'),
    (P, 'size limits inclusive',
     'if ratio < SIZE_LOW or ratio > SIZE_HIGH:',
     'if ratio <= SIZE_LOW or ratio >= SIZE_HIGH:'),
    (P, 'contig name whole header',
     'name = re.match(rb"[^ \\t]*", line).group(0)[1:]      # awk\'s $1 without the ">"',
     'name = line[1:]'),
    (P, 'retry size from the user even without statistics',
     'size = (genome or DEFAULT_GENOME_MB) if stats_ran else DEFAULT_GENOME_MB',
     'size = (genome or DEFAULT_GENOME_MB)'),
    (P, 'retry at k-mer depth 2',
     '        return float(text) < limit',
     '        return float(text) <= limit'),
    (P, 'first k-mer depth line',
     'if not found or not found[-1]:\n        return None\n    return found[-1].decode("ascii")',
     'if not found or not found[0]:\n        return None\n    return found[0].decode("ascii")'),
    (P, 'kb half-up',
     '% (_text(completeness), _text(qv), _awk_number(lost) / 1000))',
     '% (_text(completeness), _text(qv), int(_awk_number(lost) / 100 + 0.5) / 10))'),
    (P, 'alarm tolerant',
     'if alarm == b"1":',
     'if alarm.strip() == b"1":'),
    (P, 'hidden files moved',
     'if name in ("preflight.txt", FIRST_ATTEMPT_DIR) or name.startswith("."):',
     'if name in ("preflight.txt", FIRST_ATTEMPT_DIR):'),
    (P, 'organism after a failed engine',
     'detected = detected_organism(folder) if exit_code == 0 else ""',
     'detected = detected_organism(folder)'),
    (P, 'no-gfa as substring',
     'if name == "assembly_graph.gfa" and "--no-gfa" in engine_args:',
     'if name == "assembly_graph.gfa" and any("--no-gfa" in a for a in engine_args):'),
    (P, 'spike >= 0',
     'if spike and int(spike) > 0:',
     'if spike and int(spike) >= 0:'),
    (P, 'scaffolds by header start',
     'records = sum(1 for line in data.split(b"\\n") if b">" in line)',
     'records = sum(1 for line in data.split(b"\\n") if line.startswith(b">"))'),
    (P, 'retry want rounded',
     'wanted = int(float(size) * 1e6 * RETRY_FRACTION)',
     'wanted = round(float(size) * 1e6 * RETRY_FRACTION)'),
    (P, 'retry got <= want',
     'and got < wanted and',
     'and got <= wanted and'),
    (P, 'retry bases without CR',
     'return sum(len(line) for line in data.split(b"\\n") if not line.startswith(b">"))',
     'return sum(len(line.rstrip(b"\\r")) for line in data.split(b"\\n") if not line.startswith(b">"))'),
    (P, '-k attached counts',
     'not any(arg in ("-k", "--kmers") for arg in engine_args)',
     'not any(arg.startswith(("-k", "--kmers")) for arg in engine_args)'),
    (P, 'retry after failed engine',
     'needed = (exit_code == 0 and not any',
     'needed = (True and not any'),
    (P, 'stats written after empty contigs',
     '    if not _non_empty(contigs):\n        return None, False',
     '    if not os.path.exists(contigs):\n        return None, False'),
    (P, 'library guard: first verdict only',
     'guard = b"\\n".join(re.findall(rb\'"r2": *\\{[^}]*"verdict": *"([a-z]*)"\', report))',
     'guard = _first(rb\'"verdict": *"([a-z]*)"\', report)'),
    (P, 'report version parsed loosely',
     'if b\'"version"\' not in report:',
     "if b'version' not in report:"),
    (P, 'organism: any call',
     '    start = flat.find(b\'"organism_detect":{\')\n    if start < 0:\n        return ""',
     '    start = max(0, flat.find(b\'"organism_detect":{\'))'),
    (P, 'size uses default genome',
     'expected = genome or ORGANISM_GENOME_MB.get(detected, "")',
     'expected = genome or ORGANISM_GENOME_MB.get(detected, "5")'),
    (P, 'preflight tail keeps blank',
     '    if data.endswith(b"\\n"):\n        data = data[:-1]\n    return _text(data.rsplit(b"\\n", 1)[-1])',
     '    return _text(data.rstrip(b"\\n").rsplit(b"\\n", 1)[-1])'),
    (P, 'windows wording keeps CR',
     'st = contig_statistics(contigs, strip_cr=windows)',
     'st = contig_statistics(contigs, strip_cr=False)'),
    (P, 'upstream wording strips CR',
     'st = contig_statistics(contigs, strip_cr=windows)',
     'st = contig_statistics(contigs, strip_cr=True)'),
    (R, 'screen: blank rows counted as reads',
     'difference = (rows[0] - c.blank1) - (rows[1] - c.blank2)',
     'difference = rows[0] - rows[1]'),
    (R, 'screen: shorter file named wrongly',
     'fewer, more = (name2, name1) if difference > 0 else (name1, name2)',
     'fewer, more = (name1, name2) if difference > 0 else (name2, name1)'),
    (R, "screen: first malformed from the script's number",
     '_plural(c.bad, "read pair is", "read pairs are"), c.first_bad), both))',
     '_plural(c.bad, "read pair is", "read pairs are"), c.badrec), both))'),
    (R, 'progress from what was read',
     'done = sum(s.consumed if s.discard else s.taken for s in self.sources)',
     'done = sum(s.consumed for s in self.sources)'),
    (R, 'preflight written when cancelled late',
     '    if cancel is not None and cancel.is_set():\n        raise Cancelled()                # nothing is written for a check that was stopped\n',
     ''),
    (R, 'gzip second member not read',
     '                inflater = zlib.decompressobj(31)\n                between = False',
     '                return True'),
    (R, 'truncated gzip accepted',
     '                    return between               # in the middle of a member: truncated',
     '                    return True'),
    (R, 'zlib error swallowed',
     '            except zlib.error:\n                return False',
     '            except zlib.error:\n                return True'),
    (R, 'stray byte accepted',
     '                    if not extra:\n                        return False             # one stray byte: gzip reports an unexpected end',
     '                    if not extra:\n                        return True'),
    (R, 'same file verified twice differs',
     '            if same and len(usable) == 2:\n                two.complete = one.complete',
     '            pass'),
    (R, 'screen: CRLF not named',
     'if c.mism and st.first_mismatch.endswith("\\r"):',
     'if False:'),
    (R, 'screen: blank lines blamed on the wrong file',
     'for name, path, count in ((name1, path1, c.blank1), (name2, path2, c.blank2)):',
     'for name, path, count in ((name1, path1, c.blank2), (name2, path2, c.blank1)):'),
    (P, 'windows map-back always claims the self-check',
     '_MAP_BACK["windows"] + (_MAP_BACK_COVERED if self_check else "")',
     '_MAP_BACK["windows"] + _MAP_BACK_COVERED'),
    (P, 'windows hint shows the code',
     'text.append("  hint     the reads look like %s" % ORGANISM_NAMES[detected])',
     'text.append("  hint     the reads look like %s" % detected)'),
    (P, 'F4 not applied',
     '(exit_code, shown + "/assembly.log" if windows else shown + ".log")',
     '(exit_code, shown + ".log")'),
    (P, 'thin data warning before the folder checks',
     '    if retried:\n        pf("WARN", "thin data", _THIN_DATA)\n',
     ''),
    (P, 'stats file not written',
     '    if wrote_stats and stats is not None:',
     '    if False:'),
    (P, 'keep_first_attempt moves preflight',
     'if name in ("preflight.txt", FIRST_ATTEMPT_DIR) or name.startswith("."):',
     'if name in (FIRST_ATTEMPT_DIR,) or name.startswith("."):'),
    (P, 'empty name not a duplicate',
     '                if name in seen:\n                    duplicates += 1',
     '                if name and name in seen:\n                    duplicates += 1'),
    (P, 'empty-name records counted',
     '                if name != b"":\n                    if length == 0:\n                        empty += 1\n                    lengths.append(length)\n                    total += length\n                    count += 1\n                name = re.match',
     '                if True:\n                    if length == 0:\n                        empty += 1\n                    lengths.append(length)\n                    total += length\n                    count += 1\n                name = re.match'),
]


def make_copy() -> str:
    """A folder with the modules and tests copied and everything large linked."""
    copy = tempfile.mkdtemp(prefix="port-mutations-")
    os.makedirs(os.path.join(copy, "tesseract_win"))
    for name in os.listdir(os.path.join(ROOT, "tesseract_win")):
        if name.endswith(".py"):
            shutil.copy(os.path.join(ROOT, "tesseract_win", name), os.path.join(copy, "tesseract_win", name))
    shutil.copy(os.path.join(ROOT, "pyproject.toml"), copy)
    os.makedirs(os.path.join(copy, "tests", "parity"))
    os.makedirs(os.path.join(copy, "tests", "tools"))
    for name in ["tests/__init__.py", "tests/tools/gen.py", "tests/parity/__init__.py", "tests/parity/harness.py",
                 "tests/parity/script_fingerprint.py", "tests/parity/make_fixtures.py",
                 "tests/parity/FIXTURES.sha256"] + TESTS:
        shutil.copy(os.path.join(ROOT, *name.split("/")), os.path.join(copy, *name.split("/")))
    for name in ("tests/parity/golden", "tests/parity/fixtures", "tests/parity/fake_asm", "tests/parity/model",
                 "engines", "build"):
        target = os.path.join(ROOT, *name.split("/"))
        if os.path.exists(target):
            os.symlink(target, os.path.join(copy, *name.split("/")))
    return copy


def run_tests(copy: str):
    return subprocess.run([sys.executable, "-m", "pytest"] + TESTS + ["-q", "-x", "-p", "no:cacheprovider", "-m", "not slow"],
                          cwd=copy, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)


def main() -> int:
    words = sys.argv[1:]
    if not os.path.isdir(os.path.join(HERE, "fixtures")):
        print("no fixtures: run the parity tests once first (they make them)", file=sys.stderr)
        return 2
    copy = make_copy()
    missed = stale = done = 0
    try:
        clean = run_tests(copy)
        if clean.returncode != 0:
            print(clean.stdout[-2000:])
            print("the tests do not pass on the unchanged code; nothing can be concluded", file=sys.stderr)
            return 2
        for path, title, old, new in MUTATIONS:
            if words and not any(word in title for word in words):
                continue
            done += 1
            full = os.path.join(copy, *path.split("/"))
            with open(full, "r", encoding="utf-8") as handle:
                source = handle.read()
            if source.count(old) != 1:
                stale += 1
                print("STALE  %-52s (the line occurs %d times)" % (title, source.count(old)))
                continue
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(source.replace(old, new))
            started = time.time()
            try:
                result = run_tests(copy)
            finally:
                with open(full, "w", encoding="utf-8") as handle:
                    handle.write(source)
            failed = [text for text in result.stdout.split("\n") if text.startswith(("FAILED", "ERROR"))]
            if result.returncode == 0:
                missed += 1
                print("MISSED %-52s %3.0fs" % (title, time.time() - started))
            else:
                where = failed[0].split("::", 1)[-1].split(" - ")[0] if failed else result.stdout.strip().split("\n")[-1]
                print("caught %-52s %3.0fs  %s" % (title, time.time() - started, where[:90]))
            sys.stdout.flush()
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    print("%d mutations, %d not caught, %d stale" % (done, missed, stale))
    return 1 if missed or stale else 0


if __name__ == "__main__":
    sys.exit(main())
