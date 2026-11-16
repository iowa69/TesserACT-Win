# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The port against the bash ``tesseract`` 1.6.2 script.

Three kinds of evidence:

* every golden case under ``tests/parity/golden`` (captured from the real script) is given
  to ``readcheck`` and ``postflight`` with ``wording="upstream"`` and must come out byte for
  byte, after masking only what the audit lists as machine- or run-specific;
* the script the port matches is pinned by its sha256 and by a fingerprint of its sections
  and literals, so a new upstream script fails here and says where it changed;
* where bash, awk and gzip exist, the real script and the port are run side by side on
  three read sets, which proves that the goldens are what the script prints today.

The cases are counted in ``test_how_many_cases``: 161 goldens, 17 of them belong to a
command line and are skipped, 144 apply, 137 are identical and 7 differ on purpose
(departures F2 and F7 of the audit), each of which is asserted in its own test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from tesseract_win import postflight, readcheck, versions
from tests.parity import harness as H
from tests.parity import script_fingerprint

pytestmark = pytest.mark.parity

CASES = H.load_cases()
BY_NAME = {case.name: case for case in CASES}

#: Cases the script answers in a way the port departs from on purpose (audit section 9).
INVALID_GENOME_SIZE = ("tiny29/check_gs_-1", "tiny29/check_gs_0", "tiny29/check_gs_0.3x", "tiny29/check_gs_0x10",
                       "tiny29/check_gs_5,5", "tiny29/check_gs_abc")                       # F2
FOLDER_AS_READ_FILE = ("_args/directory_as_input",)                                         # F7
DEPARTURES = INVALID_GENOME_SIZE + FOLDER_AS_READ_FILE

COMMAND_LINE = [case for case in CASES if case.kind == "command-line"]
APPLICABLE = [case for case in CASES if case.kind != "command-line"]
IDENTICAL = [case for case in APPLICABLE if case.name not in DEPARTURES]


@pytest.fixture(scope="session")
def fixtures() -> str:
    """The read sets the goldens were captured from, made once and then reused."""
    return H.ensure_fixtures()


def _needs_links(case: H.Case) -> bool:
    return "hardlink" in case.name or "symlink" in case.name


def _explain(case: H.Case, outcome: H.Outcome, differences) -> str:
    import difflib
    real = case.real_engine
    want = (H.mask(case.golden("stdout.txt") or b"", real) or b"").decode("utf-8", "replace").split("\n")
    got = (H.mask(outcome.stdout, real) or b"").decode("utf-8", "replace").split("\n")
    diff = "\n".join(difflib.unified_diff(want, got, "golden stdout", "port stdout", lineterm="", n=1))
    return "%s differs from the golden in: %s\n%s" % (case.name, ", ".join(differences), diff)


# --------------------------------------------------------------------------- the goldens

def test_how_many_cases():
    """The numbers quoted in the report. A new golden must be sorted into one of the groups."""
    assert len(CASES) == 161
    assert len(COMMAND_LINE) == 17
    assert len(APPLICABLE) == 144
    assert len(IDENTICAL) == 137
    assert sorted(DEPARTURES) == sorted(case.name for case in APPLICABLE if case.name in DEPARTURES)
    assert sum(1 for case in IDENTICAL if case.kind == "check") == 76
    assert sum(1 for case in IDENTICAL if case.kind == "run") == 61
    assert sum(1 for case in IDENTICAL if case.kind == "run" and case.stub) == 44
    # Every check and every postflight case the audit lists is among the ones compared.
    assert {case.name for case in COMMAND_LINE} == {
        "_args/check_missing_r2", "_args/check_not_first", "_args/check_without_engine", "_args/help",
        "_args/models_help", "_args/no_arguments", "_args/option_without_value_HANGS", "_args/run_missing_out",
        "_args/threads_0", "_args/threads_4x", "_args/threads_abc", "_args/threads_empty",
        "_args/unknown_first_word", "_args/unknown_organism", "_args/version", "_args/version_v",
        "good/run_again_same_outdir"}


@pytest.mark.parametrize("case", IDENTICAL, ids=lambda case: case.name)
def test_golden_case_is_identical(case, fixtures, tmp_path):
    """stdout, exit status, preflight.txt, SUMMARY.txt, assembly_stats.tsv, files left, engine runs."""
    if _needs_links(case) and not os.path.exists(os.path.join(H.HERE, case.read2)):
        pytest.skip("this file system has no such link")
    outcome = H.run_port(case, str(tmp_path), wording="upstream")
    differences = H.differences(case, outcome)
    assert not differences, _explain(case, outcome, differences)


@pytest.mark.parametrize("case", COMMAND_LINE, ids=lambda case: case.name)
def test_command_line_case_is_not_for_the_port(case):
    pytest.skip("belongs to a command line, which this program does not have: %s" % case.reason)


@pytest.mark.parametrize("name", INVALID_GENOME_SIZE)
def test_departure_f2_invalid_genome_size_is_refused(name, fixtures):
    """The script prints ``about x`` or ``about +infx`` depending on the awk; the port refuses."""
    case = BY_NAME[name]
    with pytest.raises(ValueError):
        H.run_port(case, "unused")
    with pytest.raises(ValueError):
        postflight.thin_retry_needed("unused", genome_size_mb=case.genome)
    with pytest.raises(ValueError):
        postflight.summarise("unused", read1="a", read2="b", exit_code=0, genome_size_mb=case.genome)


def test_departure_f7_folder_given_as_read_file(fixtures):
    """The script calls a folder readable and blames the other file; the port says it cannot read it."""
    case = BY_NAME["_args/directory_as_input"]
    golden = case.golden("stdout.txt").decode("utf-8").split("\n")
    assert "the -1 file has 1 fewer reads" in golden[2]          # what the script printed
    outcome = H.run_port(case, "unused")
    assert outcome.exit_status == case.exit_status == 1
    assert outcome.stdout.decode("utf-8").split("\n") == [
        golden[0],
        "  FAIL   file                   cannot read 'fixtures/tiny29'",
        "preflight: 1 failed, 0 warnings", ""]


# --------------------------------------------------------------------------- Windows wording on the goldens

_REWORDED = {
    "adapters": "trim them first (Remove adapters on the Trimming page)",
    "poly-G": "consider Trim poly-G tails on the Trimming page",
    "quality": "trim this sample first (Trimming page): trimming rewrites the qualities as Phred+33",
    "map-back": "not included in the Windows edition",
    "exit status": "/assembly.log)",
}
_LINUX_WORDS = ("fastp", "cutadapt", "seqtk", "bwa", "minimap2", "tesseract models", "--organism")


@pytest.mark.parametrize("case", [c for c in IDENTICAL if c.name != "_postflight/crlf_outputs"],
                         ids=lambda case: case.name)
def test_windows_wording_changes_only_the_advice(case, fixtures, tmp_path):
    """Same lines, verdicts, numbers and files; only the listed sentences are different."""
    if _needs_links(case) and not os.path.exists(os.path.join(H.HERE, case.read2)):
        pytest.skip("this file system has no such link")
    upstream = H.run_port(case, str(tmp_path / "u"), wording="upstream")
    windows = H.run_port(case, str(tmp_path / "w"), wording="windows")
    assert windows.exit_status == upstream.exit_status
    assert windows.tree == upstream.tree
    assert windows.files.get("assembly_stats.tsv") == upstream.files.get("assembly_stats.tsv")
    theirs, ours = upstream.stdout.split(b"\n"), windows.stdout.split(b"\n")
    assert len(theirs) == len(ours)
    for old, new in zip(theirs, ours):
        if old == new:
            continue
        old_text, new_text = old.decode("utf-8", "replace"), new.decode("utf-8", "replace")
        if old_text.startswith("  hint "):
            name = "hint"
            assert re.fullmatch(r"  hint     the reads look like [A-Z][a-z]+ [a-z]+( complex)?", new_text), new_text
        else:
            assert old_text[:32] == new_text[:32], (old_text, new_text)      # same verdict, same check
            name = old_text[9:32].strip()
            assert name in _REWORDED, "the %s line may not differ: %r" % (name, new_text)
            assert _REWORDED[name] in new_text, new_text
        assert any(word in old_text for word in _LINUX_WORDS) or name == "exit status", old_text
    whole = windows.stdout.decode("utf-8", "replace")
    for word in _LINUX_WORDS:
        assert word not in whole, "%r is still named in the Windows wording" % word
    for a, b in ((upstream.preflight, windows.preflight), (upstream.postflight, windows.postflight)):
        if a is not None:
            assert (a.fails, a.warns) == (b.fails, b.warns)
            assert [(line.verdict, line.name) for line in a.lines] == [(line.verdict, line.name) for line in b.lines]


def test_windows_wording_reads_crlf_engine_output_as_text(fixtures, tmp_path):
    """Departure F5: the golden shows the script calling every contig line malformed."""
    case = BY_NAME["_postflight/crlf_outputs"]
    assert b"168 lines with non-ACGTN characters" in case.golden("SUMMARY.txt")
    windows = H.run_port(case, str(tmp_path), wording="windows").postflight
    lines = {line.name: line for line in windows.lines}
    assert lines["contigs.fasta"].verdict == "OK"
    assert lines["contigs.fasta"].message == "3 contigs, 10000 bp; largest 5000; N50 5000 (L50 1); N90 2000 (L90 3)"
    assert lines["self-check"].verdict == "WARN" and "about 2.2 kb" in lines["self-check"].message
    assert "\r" not in windows.text
    assert windows.fails == 0 and windows.warns == 3


# --------------------------------------------------------------------------- the engine calls the goldens recorded

def _expected_environment(case: H.Case) -> dict:
    """What the script hands the engine: its profile, unless the user set a value that is not empty."""
    user = {key: value for key, value in case.env.items() if key.startswith("TESSERACT_")}
    sketch = user.get(postflight.DETECT_SKETCH_ENV) or ("" if "/_nosketch/" in case.script else "<sketch>")
    env = dict(user)
    for key, value in postflight.PROFILE_ENV + postflight.detect_env(sketch):
        if not user.get(key):
            env[key] = value
    return env


@pytest.mark.parametrize("case", [c for c in IDENTICAL if c.kind == "run" and c.stub], ids=lambda case: case.name)
def test_recorded_engine_calls_agree_with_the_constants(case):
    """``PROFILE_ENV`` and ``RETRY_ARGS`` are what the real script gave the engine."""
    calls = case.calls()
    assert calls, "the stand-in engine was not called in %s" % case.name
    first = ["-1", case.read1, "-2", case.read2, "-o", case.out, "-t", case.threads or calls[0][0][7]]
    assert calls[0][0] == first + case.extra
    if len(calls) == 2:
        assert calls[1][0] == first + list(postflight.RETRY_ARGS) + case.extra
    assert len(calls) <= 2
    expected = _expected_environment(case)
    for _argv, env in calls:
        seen = dict(env)
        if seen.get(postflight.DETECT_SKETCH_ENV, "").endswith("/models/om2detect.sketch"):
            seen[postflight.DETECT_SKETCH_ENV] = "<sketch>"
        assert seen == expected


def test_profile_is_nine_variables():
    names = [name for name, _value in postflight.PROFILE_ENV]
    assert len(names) == len(set(names)) == 9
    assert all(name.startswith("TESSERACT_") for name in names)
    assert postflight.detect_env("") == ()
    assert postflight.detect_env("C:\\x\\om2detect.sketch") == (
        ("TESSERACT_P2_DETECT_REPORT", "1"), ("TESSERACT_P2_DETECT_SKETCH", "C:\\x\\om2detect.sketch"))


def test_the_settings_module_holds_the_same_values():
    """``postflight`` records the profile and the retry ladder; a second copy elsewhere must not drift."""
    settings_map = pytest.importorskip("tesseract_win.settings_map")
    if hasattr(settings_map, "PROFILE_ENVIRONMENT"):
        assert tuple(settings_map.PROFILE_ENVIRONMENT) == postflight.PROFILE_ENV
    if hasattr(settings_map, "THIN_DATA_KMERS"):
        assert settings_map.THIN_DATA_KMERS == postflight.RETRY_KMERS
    if hasattr(settings_map, "engine_environment"):
        env = settings_map.engine_environment({"PATH": "x", "TESSERACT_OTHER": "1"}, "the.sketch")
        mine = dict(postflight.PROFILE_ENV + postflight.detect_env("the.sketch"))
        assert {key: value for key, value in env.items() if key.startswith("TESSERACT_")} == mine


def test_canned_engine_folders_are_what_their_generator_writes(tmp_path):
    """``fake_asm/cases`` is stored; it must stay the output of ``make_fake_cases.py``."""
    subprocess.run([sys.executable, os.path.join(H.HERE, "fake_asm", "make_fake_cases.py"), str(tmp_path)],
                   check=True, stdout=subprocess.DEVNULL)
    assert H.tree_of(str(tmp_path)) == H.tree_of(H.FAKE_CASES)
    for name in H.tree_of(H.FAKE_CASES):
        if not name.endswith("/"):
            with open(os.path.join(H.FAKE_CASES, name), "rb") as a, open(os.path.join(str(tmp_path), name), "rb") as b:
                assert a.read() == b.read(), name


@pytest.mark.skipif(os.name != "posix", reason="tests/tools/gen.py writes its files in text mode")
def test_fixtures_are_what_the_project_generator_writes(fixtures, tmp_path):
    """``make_fixtures.py`` repeats ``tests/tools/gen.py`` call for call; the very thin set proves it."""
    import gzip
    gen = os.path.join(H.ROOT, "tests", "tools", "gen.py")
    genome, prefix = str(tmp_path / "g.fa"), str(tmp_path / "vt")
    subprocess.run([sys.executable, gen, "genome", genome, "7", "300000"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, gen, "reads", genome, prefix, "0.3", "150", "350", "30", "0.005", "4245",
                    "paired", "fastq", "gz"], check=True, stdout=subprocess.DEVNULL)
    for mate in ("1", "2"):
        with gzip.open("%s_%s.fq.gz" % (prefix, mate), "rb") as made, \
                gzip.open(os.path.join(fixtures, "very_thin", "verythin_%s.fq.gz" % mate), "rb") as kept:
            assert made.read() == kept.read()
    with open(genome, "rb") as made, open(os.path.join(fixtures, "genome.fa"), "rb") as kept:
        assert made.read() == kept.read()


def test_fixtures_are_the_ones_the_goldens_were_captured_from(fixtures):
    with open(H.FIXTURES_DIGEST, "r", encoding="ascii") as handle:
        recorded = [text.strip() for text in handle if text.strip() and not text.startswith("#")]
    assert recorded == [H.manifest_digest(fixtures)]


# --------------------------------------------------------------------------- which script the port matches

def _what_changed() -> str:
    done = subprocess.run([sys.executable, script_fingerprint.__file__, H.UPSTREAM_SCRIPT, H.FINGERPRINT],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    return done.stdout


def test_upstream_script_is_the_one_the_port_matches():
    """A new ``tesseract`` script fails here until ``readcheck`` and ``postflight`` follow it."""
    with open(H.UPSTREAM_SCRIPT, "rb") as handle:
        found = hashlib.sha256(handle.read()).hexdigest()
    assert found == versions.TESSERACT_WRAPPER_SHA256, (
        "engines/tesseract/upstream/tesseract is not the script the port was written from.\n"
        "What changed:\n%s\nBring tesseract_win/readcheck.py and postflight.py in line, capture new goldens "
        "(tests/parity/run_golden.sh), then record the new sha256 and fingerprint." % _what_changed())


def test_script_fingerprint_is_the_recorded_one():
    now = json.loads(json.dumps(script_fingerprint.fingerprint(H.UPSTREAM_SCRIPT)))
    with open(H.FINGERPRINT, "r", encoding="utf-8") as handle:
        recorded = json.load(handle)
    assert now == recorded, "the upstream script changed:\n%s" % _what_changed()
    assert recorded["sha256"] == versions.TESSERACT_WRAPPER_SHA256
    assert recorded["version"] == versions.TESSERACT_VERSION


def test_fingerprint_tool_says_where_a_script_changed(tmp_path):
    """The tool that explains a failure of the two tests above must itself work."""
    with open(H.UPSTREAM_SCRIPT, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert "TESSERACT_DEEP_NORM=30" in text and "a > 0.05" in text
    changed = tmp_path / "tesseract"
    changed.write_text(text.replace("TESSERACT_DEEP_NORM=30", "TESSERACT_DEEP_NORM=40").replace("a > 0.05", "a > 0.10"),
                       encoding="utf-8")
    done = subprocess.run([sys.executable, script_fingerprint.__file__, str(changed), H.FINGERPRINT],
                          stdout=subprocess.PIPE, universal_newlines=True)
    assert done.returncode == 1
    assert "section preflight  LOGIC changed" in done.stdout and "section assembly   LOGIC changed" in done.stdout
    assert "literal profile" in done.stdout and "literal thresholds_awk" in done.stdout
    same = subprocess.run([sys.executable, script_fingerprint.__file__, H.UPSTREAM_SCRIPT, H.FINGERPRINT],
                          stdout=subprocess.PIPE, universal_newlines=True)
    assert same.returncode == 0 and same.stdout.startswith("unchanged")


def test_constants_are_the_literals_of_the_script():
    """The numbers kept in the two modules, read back out of the script text itself."""
    with open(H.FINGERPRINT, "r", encoding="utf-8") as handle:
        literals = json.load(handle)["literals"]
    assert literals["profile"] == ["%s=%s" % pair for pair in postflight.PROFILE_ENV]
    assert literals["profile_added_with_sketch"] == [
        '%s=1 "%s=$SKETCH"' % (postflight.DETECT_REPORT_ENV, postflight.DETECT_SKETCH_ENV)]
    assert dict(literals["organism_mb"]) == postflight.ORGANISM_GENOME_MB
    assert set(postflight.ORGANISM_NAMES) == set(postflight.ORGANISM_GENOME_MB)
    assert literals["sample_rows"] == [str(readcheck.SAMPLE_ROWS)]
    assert literals["adapter"] == [readcheck.ADAPTER.decode("ascii")]
    assert literals["polyg"] == [readcheck.POLYG_TAIL.decode("ascii")]
    assert literals["quality_step"] == [str(readcheck.QUALITY_STEP)]
    assert literals["retry_ladder"] == [postflight.RETRY_KMERS]
    assert literals["retry_want"] == [str(postflight.RETRY_FRACTION)]
    assert set(literals["thresholds_awk"]) == {
        "a > %s" % readcheck.ADAPTER_LIMIT, "a > %s" % readcheck.POLYG_LIMIT, "d < %d" % postflight.RETRY_KMER_DEPTH,
        "p < 95", "r < %s || r > %s" % (postflight.SIZE_LOW, postflight.SIZE_HIGH)}
    assert readcheck.N_LIMIT == readcheck.ADAPTER_LIMIT          # the script uses "a > 0.05" for both
    assert literals["disk_need"] == ['$(( ( $(wc -c < "$R1") + $(wc -c < "$R2") ) * 3 / 1024 + %d ))'
                                     % readcheck.DISK_BASE_KB]
    names = set(literals["check_names"])
    assert names == {"file", "gzip", "pairing", "format", "quality", "read length", "depth", "adapters", "poly-G",
                     "N bases", "disk", "memory", "exit status", "scaffolds.fasta", "contigs.fasta", "size",
                     "report.json", "self-check", "circles", "spike-in", "library", "map-back", "thin data",
                     "organism"}


# --------------------------------------------------------------------------- the real script, today

_TOOLS = ("bash env awk gzip paste df head od tr sed grep sort cut tee cat wc mkdir mv rm tail basename dirname "
          "readlink nproc mktemp cp").split()
_ENGINE = os.path.join(H.ROOT, "build", "engines", "linux", "tesseract-asm")


def _toolbin(folder: str) -> str:
    """A PATH that holds exactly what the script needs, so that no read mapper can be found."""
    os.makedirs(folder)
    for tool in _TOOLS:
        found = shutil.which(tool)
        if found is None:
            pytest.skip("the real script cannot run here: no %s" % tool)
        os.symlink(found, os.path.join(folder, tool))
    return folder


def _memory_bytes() -> int:
    with open("/proc/meminfo", "r", encoding="ascii") as handle:
        for line in handle:
            if "MemAvailable" in line:
                return int(line.split()[1]) * 1024
    return 0


def _run_script(args, engine: str, toolbin: str, home: str, extra_env=()):
    env = ["env", "-i", "HOME=" + home, "LANG=en_US.UTF-8", "PATH=" + toolbin, "TESSERACT_ASM=" + engine]
    return subprocess.run(env + list(extra_env) + [H.UPSTREAM_SCRIPT] + list(args), cwd=H.HERE,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)


@pytest.mark.skipif(os.name != "posix" or not all(shutil.which(t) for t in ("bash", "awk", "gzip")),
                    reason="needs bash, awk and gzip")
@pytest.mark.parametrize("fixture, read1, read2, genome", [
    ("good", "good_1.fq.gz", "good_2.fq.gz", None),
    ("mate_order", "order_1.fq.gz", "order_2.fq.gz", "0.3"),
    ("crlf_len147", "crlf147_1.fq", "crlf147_2.fq", None),
])
def test_real_script_and_port_agree_on_a_check(fixture, read1, read2, genome, fixtures, tmp_path):
    """``tesseract check`` run now, against the port, with nothing masked."""
    one, two = "fixtures/%s/%s" % (fixture, read1), "fixtures/%s/%s" % (fixture, read2)
    args = ["check", "-1", one, "-2", two] + (["--genome-size", genome] if genome else [])
    done = _run_script(args, H.FAKE_ENGINE, _toolbin(str(tmp_path / "bin")), str(tmp_path))
    check = readcheck.check_reads(os.path.join(H.HERE, one), os.path.join(H.HERE, two), genome_size_mb=genome,
                                  wording="upstream", shown_read1=one, shown_read2=two)
    assert done.stdout == check.data
    assert done.returncode == (0 if check.ok else 1)
    if genome is None:
        assert done.stdout == BY_NAME[fixture + "/check"].golden("stdout.txt"), (
            "the golden is not what the script prints today")


@pytest.mark.engines
@pytest.mark.skipif(os.name != "posix" or not all(shutil.which(t) for t in ("bash", "awk", "gzip")),
                    reason="needs bash, awk and gzip")
def test_real_script_and_port_agree_on_reads_the_trimmer_wrote(fixtures, tmp_path):
    """The read check as the program uses it: on the output of scepter (reads of many lengths)."""
    scepter = os.path.join(os.path.dirname(_ENGINE), "scepter")
    if not os.access(scepter, os.X_OK):
        pytest.skip("no trimmer at %s" % scepter)
    one, two = str(tmp_path / "t_R1.fastq.gz"), str(tmp_path / "t_R2.fastq.gz")
    subprocess.run([scepter, "-i", os.path.join(fixtures, "adapter", "adapter_1.fq.gz"),
                    "-I", os.path.join(fixtures, "adapter", "adapter_2.fq.gz"), "-o", one, "-O", two,
                    "--preset", "assembly", "-w", "4", "--no-color", "--no-summary",
                    "-j", str(tmp_path / "t.json"), "-h", str(tmp_path / "t.html")],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    done = _run_script(["check", "-1", one, "-2", two, "--genome-size", "0.3"], H.FAKE_ENGINE,
                       _toolbin(str(tmp_path / "bin")), str(tmp_path))
    check = readcheck.check_reads(one, two, genome_size_mb="0.3", wording="upstream")
    assert done.stdout == check.data
    assert done.returncode == 0 and check.ok
    assert check.stats.pairs > 50000 and check.stats.mean_length < check.stats.max_length == 150
    assert check.stats.adapter_fraction == 0.0, "the trimmer was given 7.9 % of pairs with adapter"
    assert readcheck.check_input_pair(one, two).ok


@pytest.mark.skipif(os.name != "posix" or not all(shutil.which(t) for t in ("bash", "awk", "gzip")),
                    reason="needs bash, awk and gzip")
@pytest.mark.parametrize("engine_kind", [
    pytest.param("real", marks=pytest.mark.engines),
    "stand-in",
])
def test_real_script_and_port_agree_on_a_run_with_a_retry(engine_kind, fixtures, tmp_path):
    """The very thin set, start to end: read check, engine, retry, second engine run, summary.

    Once with the real engine (both sides run it themselves) and once with the stand-in
    engine, which needs no build.
    """
    one, two = "fixtures/very_thin/verythin_1.fq.gz", "fixtures/very_thin/verythin_2.fq.gz"
    toolbin = _toolbin(str(tmp_path / "bin"))
    theirs, ours = str(tmp_path / "script out"), str(tmp_path / "port out")
    canned = os.path.join(H.FAKE_CASES, "retry")
    if engine_kind == "real":
        if not os.access(_ENGINE, os.X_OK):
            pytest.skip("no engine at %s" % _ENGINE)
        engine, extra_env = _ENGINE, []
    else:
        os.makedirs(str(tmp_path / "rec"))
        engine, extra_env = H.FAKE_ENGINE, ["FAKE_SRC=" + canned, "FAKE_REC=" + str(tmp_path / "rec")]
    done = _run_script(["-1", one, "-2", two, "-o", theirs, "-t", "4", "--genome-size", "0.3"], engine, toolbin,
                       str(tmp_path), extra_env)

    # ---- the port, doing what the script does
    stdout = b""
    check = readcheck.check_reads(os.path.join(H.HERE, one), os.path.join(H.HERE, two), out_dir=ours,
                                  genome_size_mb="0.3", wording="upstream", shown_read1=one, shown_read2=two,
                                  available_memory_bytes=_memory_bytes())
    stdout += check.data + readcheck.encode_text("assembling into %s (vanilla) ...\n" % ours)
    environment = {"PATH": toolbin, "HOME": str(tmp_path), "LANG": "en_US.UTF-8"}
    environment.update(postflight.PROFILE_ENV + postflight.detect_env(H.UPSTREAM_SKETCH))
    calls = []

    def run_engine(more=()) -> int:
        calls.append(more)
        if engine_kind != "real":
            return H.stub_engine(canned, ours, len(calls))
        with open(os.path.join(ours, "assembly.log"), "wb") as log:
            return subprocess.run([engine, "-1", one, "-2", two, "-o", ours, "-t", "4"] + list(more), cwd=H.HERE,
                                  env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=600).returncode

    status = run_engine()
    retry = postflight.thin_retry_needed(ours, exit_code=status, genome_size_mb="0.3", stats_ran=check.has_statistics)
    assert retry, "the very thin set must trigger the retry (assembled %d bp, k-mer depth %s)" % (
        retry.assembled_bp, retry.kmer_depth)
    stdout += readcheck.encode_text(retry.message + "\n")
    postflight.keep_first_attempt(ours)
    status = run_engine(postflight.RETRY_ARGS)
    version = subprocess.run(["bash", engine, "--version"] if engine_kind != "real" else [engine, "--version"],
                             stdout=subprocess.PIPE, env=environment).stdout
    summary = postflight.summarise(ours, read1=one, read2=two, exit_code=status, genome_size_mb="0.3",
                                   engine_version=postflight.engine_version_line(version), retried=True,
                                   wording="upstream")
    stdout += summary.data

    # ---- compare; only the two folder names and the free disk and memory figures can differ
    def same(data: bytes) -> bytes:
        return H.mask(data.replace(theirs.encode(), b"OUT").replace(ours.encode(), b"OUT"))

    assert same(done.stdout) == same(stdout)
    assert done.returncode == (0 if summary.ok else 1) == 0
    assert H.tree_of(theirs) == H.tree_of(ours)
    for name in H.WRAPPER_FILES + ("contigs.fasta", "attempt1_default_k/contigs.fasta"):
        with open(os.path.join(theirs, *name.split("/")), "rb") as a, open(os.path.join(ours, *name.split("/")), "rb") as b:
            assert same(a.read()) == same(b.read()), name
    assert [line.name for line in summary.lines if line.verdict == "WARN"][-1] == "thin data"
    if engine_kind == "real":
        golden = BY_NAME["very_thin/run"]
        assert summary.contigs is not None
        assert ("%d contigs, %d bp" % (summary.contigs.contigs, summary.contigs.total_length)).encode() in \
            golden.golden("SUMMARY.txt"), "the engine no longer assembles what the golden recorded"
