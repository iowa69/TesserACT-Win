# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The engine tools: the proof of equal output, the self-tests, the upstream suites and
the command that brings in a new upstream release.

  * ``engines/equivalence.py``: the read sets are the same on every run; a record of the
    engines compared with a second record (other folder, other thread count) is
    identical; a changed checksum is caught and named;
  * ``engines/selftest.py``: both self-tests run and compare; a changed answer is caught,
    an answer that describes the platform is not;
  * ``engines/update.py --dry-run``: the current trees give "nothing changed"; an edited
    line of the ``tesseract`` script is traced to its section; a changed line under a
    rule fails; nothing in the project is touched;
  * ``engines/ci/run_upstream_suites.sh``: passes on the built engines and fails on a
    broken one (two quick suites here; CI runs all of them).

Added by the review: a run that is stopped or cannot start leaves no record of an earlier
run behind; read files cut short are named; "identical" is never said of nothing; a
download cut short is tried again; every folder may have spaces, accents, brackets and
non-Latin letters; what a real Windows answered in CI is covered by the list of platform
answers; ``update.py`` without ``--dry-run`` (in a copy of the project) replaces, can be
repeated without losing its list, finishes what a stopped run began, can go back to the
kept tree, and changes nothing when it cannot finish; the suite script refuses to delete
a folder that is not its own.

The tests that start an engine need the engines built by ``engines/build.py`` (they carry
the self-test) and are skipped without them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.engines

REPO = Path(__file__).resolve().parent.parent
ENGINES = REPO / "engines"
EXE = ".exe" if os.name == "nt" else ""
THREADS = 4

from tesseract_win import model, postflight, settings_map, versions  # noqa: E402


def load(name: str):
    spec = importlib.util.spec_from_file_location("tools_" + name, ENGINES / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


equivalence = load("equivalence")
selftest = load("selftest")
update = load("update")


def tool(script: str, *args, env=None):
    """Run one of the tools as CI does; returns (exit status, everything it printed)."""
    done = subprocess.run([sys.executable, str(ENGINES / script), *[str(a) for a in args]], cwd=str(REPO),
                          env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, encoding="utf-8", errors="replace")
    return done.returncode, done.stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def engines_dir() -> Path:
    """The folder of engines built from the staged source (they answer the self-test)."""
    folders = [os.environ.get("TESSERACT_WIN_ENGINES", "")]
    folders += [str(REPO / "build" / "engines" / name)
                for name in (("windows",) if os.name == "nt" else ("staged-linux", "linux"))]
    for folder in filter(None, folders):
        programs = [Path(folder) / ("tesseract-asm" + EXE), Path(folder) / ("scepter" + EXE)]
        if not all(p.is_file() for p in programs):
            continue
        answers = selftest.ask(str(programs[1]), "SCEPTER_PORT_SELFTEST")
        if not selftest.failure(answers):
            return Path(folder)
    pytest.skip("no engines built by engines/build.py (python engines/build.py --target linux)")


# --------------------------------------------------------------------------------------
# equivalence.py: the read sets
# --------------------------------------------------------------------------------------

def test_the_corpus_has_the_sets_the_audits_ask_for():
    assert list(equivalence.SYNTHETIC_SETS) == [
        "plain200k", "circular_phix", "repeat_dropout", "replicons_250", "low_depth", "very_thin",
        "novaseq", "example"]


@pytest.mark.slow
def test_make_corpus_is_deterministic_and_small(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    for folder in (first, second):
        status, out = tool("equivalence.py", "make-corpus", folder)
        assert status == 0, out
    a = json.loads((first / "corpus.json").read_text(encoding="utf-8"))
    b = json.loads((second / "corpus.json").read_text(encoding="utf-8"))
    assert a == b, "two runs of make-corpus describe different reads"
    assert (first / "corpus.json").read_bytes() == (second / "corpus.json").read_bytes()
    assert set(a["sets"]) == set(equivalence.SYNTHETIC_SETS)
    total = 0
    for name, entry in a["sets"].items():
        assert entry["kind"] == "synthetic" and entry["pairs"] > 0
        for mate in ("R1", "R2"):
            one, two = first / entry[mate], second / entry[mate]
            assert one.read_bytes() == two.read_bytes(), "%s %s differs between two runs" % (name, mate)
            assert equivalence.reads_digest(str(one))[0] == entry["sha256"][mate]
            assert one.read_bytes()[4:8] == b"\0\0\0\0", "the gzip header carries a time"
            total += one.stat().st_size
    assert total < 60e6, "the corpus is %.1f MB, over the 60 MB it may be" % (total / 1e6)
    assert not [p.name for p in first.iterdir() if p.name.startswith(".making")]
    # the example is the one the program ships
    example = tmp_path / "example"
    subprocess.run([sys.executable, str(REPO / "tools" / "make_example.py"), str(example)], check=True,
                   stdout=subprocess.DEVNULL)
    assert (example / "Example_isolate_R1.fastq.gz").read_bytes() == (first / a["sets"]["example"]["R1"]).read_bytes()


def test_make_corpus_refuses_an_unknown_set(tmp_path):
    status, out = tool("equivalence.py", "make-corpus", tmp_path / "c", "--sets", "plain200k,nonsense")
    assert status == 1 and "nonsense" in out and "plain200k" in out


def test_the_tool_passes_what_the_program_passes():
    """equivalence.py keeps its own copy of the standard settings; it must not drift."""
    names = ("r1.fq.gz", "r2.fq.gz", "o1.fq.gz", "o2.fq.gz", "j.json", "h.html")
    program = settings_map.scepter_command("scepter", model.TrimSettings(), read1=names[0], read2=names[1],
                                           out1=names[2], out2=names[3], json_report=names[4],
                                           html_report=names[5], title="S", threads=1)
    ours = ["scepter", "-i", names[0], "-I", names[1], "-o", names[2], "-O", names[3], "-j", names[4],
            "-h", names[5], "-R", "S", "-w", "1", *equivalence.TRIM_FLAGS]
    assert ours == program
    assert equivalence.PROFILE == dict(settings_map.PROFILE_ENVIRONMENT)
    assembly = settings_map.tesseract_command("asm", model.AssemblySettings(threads=THREADS), read1="a", read2="b",
                                              out_dir_name="assembly")
    assert assembly == ["asm", "-1", "a", "-2", "b", "-o", "assembly", "-t", str(THREADS), "--no-polish"]
    assert equivalence.RETRY_KMERS == postflight.RETRY_KMERS == settings_map.THIN_DATA_KMERS
    assert equivalence.FIRST_ATTEMPT_DIR == postflight.FIRST_ATTEMPT_DIR
    env = settings_map.engine_environment({"TESSERACT_X": "1", "PATH": "p"}, "sketch")
    ours_env = equivalence.clean_env(dict(equivalence.PROFILE, TESSERACT_P2_DETECT_REPORT="1",
                                          TESSERACT_P2_DETECT_SKETCH="sketch"))
    assert {k: v for k, v in env.items() if k.startswith("TESSERACT_")} == \
           {k: v for k, v in ours_env.items() if k.startswith("TESSERACT_")}
    assert env["NO_COLOR"] == ours_env["NO_COLOR"] == "1"


# --------------------------------------------------------------------------------------
# equivalence.py: the real isolate (no network here)
# --------------------------------------------------------------------------------------

def test_the_real_isolate_is_written_down_completely():
    real = equivalence.REAL_ISOLATE
    assert real["accession"] in real["files"][0]["url"] and len(real["files"]) == 2
    for item in real["files"]:
        assert item["url"].startswith("https://ftp.sra.ebi.ac.uk/vol1/fastq/")
        assert len(item["md5"]) == 32 and int(item["md5"], 16) >= 0
        assert 1e6 < item["bytes"] < 100e6
    assert sum(item["bytes"] for item in real["files"]) < 150e6, "too large for a CI runner"


def test_fetch_real_checks_what_it_downloads(tmp_path, monkeypatch):
    def pair(n):
        text = b"".join(b"@r%d/%d\nACGTACGTAC\n+\nIIIIIIIIII\n" % (i, n) for i in range(5))
        raw = io.BytesIO()
        import gzip
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle:
            handle.write(text)
        return raw.getvalue()

    payload = {"https://example.invalid/x_1.fastq.gz": pair(1), "https://example.invalid/x_2.fastq.gz": pair(2)}
    asked = []

    def fake_urlopen(request, timeout=0):
        asked.append(request.full_url)
        return io.BytesIO(payload[request.full_url])

    monkeypatch.setattr(equivalence.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(equivalence.time, "sleep", lambda seconds: None)
    files = [{"mate": "R%d" % n, "url": url, "md5": hashlib.md5(data).hexdigest(), "bytes": len(data)}
             for n, (url, data) in enumerate(sorted(payload.items()), 1)]
    monkeypatch.setattr(equivalence, "REAL_ISOLATE", {"accession": "X1", "organism": "Made up", "read_pairs": 5,
                                                       "bases": 100, "files": files})
    monkeypatch.setattr(equivalence, "REAL_SET", "real_X1")
    corpus = tmp_path / "corpus"
    assert equivalence.main(["fetch-real", str(corpus)]) == 0
    manifest = json.loads((corpus / "corpus.json").read_text(encoding="utf-8"))
    assert manifest["sets"]["real_X1"]["kind"] == "real" and manifest["sets"]["real_X1"]["pairs"] == 5
    assert len(asked) == 2
    assert equivalence.main(["fetch-real", str(corpus)]) == 0 and len(asked) == 2, "a good file is not fetched again"

    # a download that is not what the archive published is refused and leaves nothing behind
    files[0]["md5"] = "0" * 32
    other = tmp_path / "other"
    assert equivalence.main(["fetch-real", str(other)]) == 1
    assert not (other / "corpus.json").exists()
    assert not [p for p in (other / "real_X1").iterdir()]
    with pytest.raises(equivalence.Problem, match="HTTPS"):
        equivalence._download("http://example.invalid/x", str(tmp_path / "x"), 1, "0" * 32)


# --------------------------------------------------------------------------------------
# equivalence.py: run and compare
# --------------------------------------------------------------------------------------

RUN_SETS = "very_thin,low_depth,novaseq"


@pytest.fixture(scope="module")
def two_records(engines_dir, tmp_path_factory):
    """The same three read sets recorded twice: other folders, other thread count."""
    base = tmp_path_factory.mktemp("equivalence")
    corpus = base / "corpus"
    status, out = tool("equivalence.py", "make-corpus", corpus, "--sets", RUN_SETS)
    assert status == 0, out
    elsewhere = base / "moved here" / "corpus copy"
    shutil.copytree(corpus, elsewhere)
    records = []
    for name, folder, threads in (("a", corpus, THREADS), ("b", elsewhere, 2)):
        record = base / (name + ".json")
        status, out = tool("equivalence.py", "run", folder, "--engines", engines_dir, "--out", record,
                           "--threads", threads, "--work", base / ("work " + name))
        assert status == 0, out
        assert not (base / ("work " + name) / "novaseq").exists(), "the working folders are removed"
        records.append(record)
    return records[0], records[1]


def test_two_runs_of_the_same_engines_are_identical(two_records):
    a, b = two_records
    status, out = tool("equivalence.py", "compare", a, b)
    assert status == 0, out
    assert out.startswith("identical: ") and "3 read sets" in out
    compared = int(out.split()[1])
    record = json.loads(a.read_text(encoding="utf-8"))
    files = sum(len(run["files"]) for entry in record["sets"].values() for run in entry["assembly"].values())
    assert compared == files + 3 * 3, "every assembly file, two trimmed files and one report per set"
    assert json.loads(b.read_text(encoding="utf-8"))["threads"] == 2 and record["threads"] == THREADS


def test_the_record_holds_what_the_audits_ask_for(two_records):
    record = json.loads(two_records[0].read_text(encoding="utf-8"))
    assert record["ok"] is True and record["platform"] == sys.platform
    assert versions.TESSERACT_VERSION in record["versions"]["tesseract-asm"]
    assert versions.SCEPTER_VERSION in record["versions"]["scepter"]
    for name, entry in record["sets"].items():
        assert entry["problems"] == [] and set(entry["assembly"]) == {"trimmed", "raw"}
        assert entry["trim"]["exit"] == 0 and entry["trim"]["report"]["missing"] == []
        assert len(entry["trim"]["reads"]["R1"]) == 64 and entry["trim"]["html_written"] is True
        for run in entry["assembly"].values():
            assert run["exit"] == 0
            assert {"contigs.fasta", "assembly_graph.gfa", "replicons.tsv", "ends.tsv", "p2_edits.tsv",
                    "p2_self_qa.tsv"} <= set(run["files"])
            assert not any(name.startswith("report.") or name.endswith(".log") for name in run["files"])
            assert "report.json" in run["written"] and "assembly.log" in run["written"]
            assert all(facts["crlf"] == 0 for facts in run["files"].values())
    # the thin set made the second attempt, and the first one is recorded too
    thin = record["sets"]["very_thin"]["assembly"]["raw"]
    assert thin["retried"] is True and thin["first_exit"] == 0
    assert "attempt1_default_k/contigs.fasta" in thin["files"]
    assert record["sets"]["low_depth"]["assembly"]["raw"]["retried"] is False
    # a scaffold gap was written: the two files the engine audit could not cover with its own sets
    assert {"scaffolds.fasta", "scaffolds.agp"} <= set(record["sets"]["low_depth"]["assembly"]["raw"]["files"])
    # NovaSeq read names switch poly-G trimming on; the trimmer had work to do
    nova = record["sets"]["novaseq"]["trim"]
    assert nova["polyg_trimmed_reads"] > 0 and nova["adapter_trimmed_reads"] > 0
    assert 0 < nova["after_reads"] < nova["before_reads"]
    assert record["sets"]["low_depth"]["trim"]["polyg_trimmed_reads"] == 0
    # trimmed and raw reads are different inputs for the assembler
    assert record["sets"]["novaseq"]["trim"]["reads"]["R1"] != record["sets"]["novaseq"]["input"]["R1"]


@pytest.mark.parametrize("what", ["assembly file", "trimmed reads", "trimming report", "line ends", "missing file",
                                  "missing set", "failed run", "retry", "other reads"])
def test_compare_catches_a_difference(two_records, tmp_path, what):
    a, b = two_records
    record = json.loads(b.read_text(encoding="utf-8"))
    low = record["sets"]["low_depth"]
    if what == "assembly file":
        low["assembly"]["trimmed"]["files"]["contigs.fasta"]["sha256"] = "0" * 64
        expect = ["low_depth, trimmed reads: contigs.fasta differs"]
    elif what == "trimmed reads":
        low["trim"]["reads"]["R2"] = "1" * 64
        expect = ["low_depth: trimmed R2 reads (decompressed) differs"]
    elif what == "trimming report":
        low["trim"]["report"]["sha256"] = "2" * 64
        low["trim"]["report"]["sections"]["filtering_result"] = "3" * 64
        expect = ["low_depth: the trimming report differs in: filtering_result"]
    elif what == "line ends":
        facts = low["assembly"]["raw"]["files"]["scaffolds.agp"]
        facts.update(sha256="4" * 64, crlf=7, bytes=facts["bytes"] + 7)
        expect = ["low_depth, raw reads: scaffolds.agp differs (one of them has Windows line ends: 0 against 7)"]
    elif what == "missing file":
        del low["assembly"]["raw"]["files"]["ends.tsv"]
        expect = ["low_depth, raw reads: ends.tsv was written only by"]
    elif what == "missing set":
        del record["sets"]["novaseq"]
        expect = ["read set novaseq is only in"]
    elif what == "failed run":
        record["ok"] = False
        expect = ["records a run in which an engine failed"]
    elif what == "retry":
        record["sets"]["very_thin"]["assembly"]["raw"]["retried"] = False
        expect = ["very_thin, raw reads: whether the second attempt for thin data was made differs"]
    else:
        low["input"]["R1"] = "5" * 64
        expect = ["low_depth: the R1 reads that went in (the two runs did not use the same read files) differs"]
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(record), encoding="utf-8")
    status, out = tool("equivalence.py", "compare", a, changed)
    assert status == 1, out
    for text in expect:
        assert text in out, out
    assert "1 difference(s)" in out or what == "trimming report"


def test_compare_explains_a_file_it_cannot_read(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    status, out = tool("equivalence.py", "compare", bad, bad)
    assert status == 1 and "is not a readable record" in out
    status, out = tool("equivalence.py", "run", tmp_path, "--engines", tmp_path, "--out", tmp_path / "o.json")
    assert status == 1 and "is missing. Build the engines first" in out


def _record(**changes) -> dict:
    """The smallest record that compares as identical with itself."""
    record = {"format": equivalence.FORMAT, "ok": True, "platform": "linux", "threads": 1,
              "versions": {"tesseract-asm": "TesserACT 1", "scepter": "scepter 1"}, "detect_sketch": None,
              "sets": {"s": {"input": {"R1": "a" * 64, "R2": "b" * 64}, "problems": [],
                             "trim": {"exit": 0, "reads": {"R1": "c" * 64, "R2": "d" * 64}, "html_written": True,
                                      "report": {"sha256": "e" * 64, "sections": {}, "missing": []}},
                             "assembly": {"raw": {"exit": 0, "retried": False, "written": ["contigs.fasta"],
                                                  "first_headers": [">c1"],
                                                  "files": {"contigs.fasta": {"sha256": "f" * 64, "bytes": 9,
                                                                              "crlf": 0}}}}}}}
    record.update(changes)
    return record


def test_compare_does_not_call_identical_what_it_did_not_compare(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_record()), encoding="utf-8")
    status, out = tool("equivalence.py", "compare", good, good)
    assert status == 0 and out.startswith("identical: 4 files compared in 1 read sets"), out

    # records with sets but without a single checksum: nothing was proven
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps(_record(sets={"s": {"input": {}, "trim": {"exit": 0}, "assembly": {}}})),
                     encoding="utf-8")
    status, out = tool("equivalence.py", "compare", empty, empty)
    assert status == 1 and "nothing was compared" in out, out

    # the HTML report of the trimming is not checksummed (it carries times), but it must be there on both
    record = _record()
    record["sets"]["s"]["trim"]["html_written"] = False
    other = tmp_path / "other.json"
    other.write_text(json.dumps(record), encoding="utf-8")
    status, out = tool("equivalence.py", "compare", good, other)
    assert status == 1 and "s: whether the trimming report in HTML was written differs" in out, out

    # JSON that is not a record is explained, not a traceback
    for text in ("[1, 2]", '"a string"', '{"sets": []}'):
        other.write_text(text, encoding="utf-8")
        status, out = tool("equivalence.py", "compare", good, other)
        assert status == 1 and "is not a readable record" in out and "Traceback" not in out, out


def _tiny_corpus(folder: Path) -> Path:
    """A corpus of one hand-made set, without the read generator."""
    (folder / "tiny").mkdir(parents=True)
    for mate in ("1", "2"):
        reads = b"".join(b"@r%d/%s\nACGTACGTACGTACGTACGT\n+\nIIIIIIIIIIIIIIIIIIII\n" % (i, mate.encode())
                         for i in range(50))
        equivalence.write_gz(str(folder / "tiny" / ("tiny_R%s.fastq.gz" % mate)), reads)
    entry = equivalence._describe(str(folder), "tiny", "tiny/tiny_R1.fastq.gz", "tiny/tiny_R2.fastq.gz",
                                  "synthetic", "fifty made-up read pairs")
    equivalence.write_json(str(folder / "corpus.json"), {"format": equivalence.FORMAT, "sets": {"tiny": entry}})
    return folder


@pytest.mark.skipif(os.name == "nt", reason="the stand-in engines are shell scripts")
def test_run_never_leaves_the_record_of_another_run(tmp_path, monkeypatch):
    """Review focus 1: nothing that looks finished but is not."""
    corpus = _tiny_corpus(tmp_path / "corpus")
    engines = tmp_path / "engines"
    engines.mkdir()
    for name, line in (("tesseract-asm", "TesserACT 0.0.0"), ("scepter", "scepter 0.0.0")):
        (engines / name).write_text("#!/bin/sh\necho %s\nexit 3\n" % line, encoding="utf-8")
        (engines / name).chmod(0o755)
    out = tmp_path / "record.json"
    stale = json.dumps(_record())

    # the engines are not there: the old record must not survive as if it were the answer
    out.write_text(stale, encoding="utf-8")
    assert equivalence.main(["run", str(corpus), "--engines", str(tmp_path / "nowhere"), "--out", str(out)]) == 1
    assert not out.exists()

    # stopped in the middle of a set (Ctrl-C, a crash): no record at all
    out.write_text(stale, encoding="utf-8")

    def stopped(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(equivalence, "trim", stopped)
    with pytest.raises(KeyboardInterrupt):
        equivalence.main(["run", str(corpus), "--engines", str(engines), "--out", str(out),
                          "--work", str(tmp_path / "work")])
    assert not out.exists() and not Path(str(out) + ".part").exists()

    # an engine that fails: a record is written, it says so, and compare refuses it
    monkeypatch.undo()
    assert equivalence.main(["run", str(corpus), "--engines", str(engines), "--out", str(out),
                             "--work", str(tmp_path / "work"), "--threads", "1"]) == 1
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["ok"] is False and record["sets"]["tiny"]["trim"]["exit"] == 3
    assert record["sets"]["tiny"]["problems"] == ["trimming ended with status 3",
                                                  "the assembly of the raw reads ended with status 3"]
    problems, _ = equivalence.compare_records(record, record)
    assert any("records a run in which an engine failed" in p for p in problems)

    # without --work every run has a working folder of its own (two runs at once never share files),
    # and removes it
    monkeypatch.setattr(equivalence, "REPO", str(tmp_path / "repo"))
    assert equivalence.main(["run", str(corpus), "--engines", str(engines), "--out", str(out), "--keep"]) == 1
    own = tmp_path / "repo" / "build" / "equivalence-work" / ("run-%d" % os.getpid())
    assert (own / "tiny" / "trimming" / "trimming.log").is_file()
    assert equivalence.main(["run", str(corpus), "--engines", str(engines), "--out", str(out)]) == 1
    assert not (tmp_path / "repo" / "build" / "equivalence-work").exists()

    # read files cut short (a stopped make-corpus, a broken download) are named, and no engine is started
    whole = (corpus / "tiny" / "tiny_R1.fastq.gz").read_bytes()
    monkeypatch.setattr(equivalence, "trim", lambda *a, **k: pytest.fail("an engine was started on broken reads"))
    for broken in (whole[:len(whole) // 2], b"", b"not gzip at all"):
        (corpus / "tiny" / "tiny_R1.fastq.gz").write_bytes(broken)
        assert equivalence.main(["run", str(corpus), "--engines", str(engines), "--out", str(out)]) == 1
        record = json.loads(out.read_text(encoding="utf-8"))
        assert record["ok"] is False and len(record["sets"]["tiny"]["problems"]) == 1
        assert "tiny/tiny_R1.fastq.gz" in record["sets"]["tiny"]["problems"][0]
    (corpus / "tiny" / "tiny_R2.fastq.gz").unlink()
    assert equivalence.main(["run", str(corpus), "--engines", str(engines), "--out", str(out)]) == 1
    assert "tiny/tiny_R2.fastq.gz is missing" in json.loads(out.read_text(encoding="utf-8"))["sets"]["tiny"]["problems"]


def test_a_download_cut_short_is_tried_again(tmp_path, monkeypatch):
    import http.client
    data = b"x" * 1000
    calls = []

    def flaky(request, timeout=0):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise http.client.IncompleteRead(data[:10], 990)
        if len(calls) == 2:
            return io.BytesIO(data[:500])                       # the right start, then the line dropped
        return io.BytesIO(data)

    monkeypatch.setattr(equivalence.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(equivalence.time, "sleep", lambda seconds: None)
    target = tmp_path / "reads.fastq.gz"
    equivalence._download("https://example.invalid/r", str(target), len(data), hashlib.md5(data).hexdigest())
    assert target.read_bytes() == data and len(calls) == 3 and not Path(str(target) + ".part").exists()

    def never(request, timeout=0):
        raise http.client.BadStatusLine("")

    monkeypatch.setattr(equivalence.urllib.request, "urlopen", never)
    target.unlink()
    with pytest.raises(equivalence.Problem, match="Nothing was added"):
        equivalence._download("https://example.invalid/r", str(target), len(data), hashlib.md5(data).hexdigest(),
                              attempts=2)
    assert not target.exists() and not Path(str(target) + ".part").exists()


def test_run_where_every_folder_has_an_awkward_name(engines_dir, tmp_path):
    """Review focus 2: spaces, accents, brackets and non-Latin letters in every path."""
    base = tmp_path / "Prova à [1] 日本"            # short: Windows paths end at 260 characters
    corpus = base / "read sets"
    status, out = tool("equivalence.py", "make-corpus", corpus, "--sets", "very_thin")
    assert status == 0, out
    record = base / "récord [x].json"
    status, out = tool("equivalence.py", "run", corpus, "--engines", engines_dir, "--out", record,
                       "--threads", THREADS, "--work", base / "w [t]")
    assert status == 0, out
    found = json.loads(record.read_text(encoding="utf-8"))
    thin = found["sets"]["very_thin"]
    assert found["ok"] is True and thin["trim"]["report"]["missing"] == []
    assert thin["assembly"]["raw"]["retried"] is True and thin["assembly"]["trimmed"]["exit"] == 0
    status, out = tool("equivalence.py", "compare", record, record)
    assert status == 0 and "1 read sets" in out, out


def test_report_digest_removes_only_the_run_specific_fields():
    report = {"summary": {"n": 1}, "command": "scepter -i /here/a",
              "scepter": {"command": "x", "started_at": "now", "duration_seconds": 1.5, "version": "1.1.0",
                          "detection": {"sample_seconds": 0.1, "instrument": "MiSeq"},
                          "config": {"in1": "/here/a", "in2": "/here/b", "threads": 1, "out1": "o"}}}
    there = json.loads(json.dumps(report).replace("/here/", "C:\\\\there\\\\").replace("now", "later"))
    there["scepter"]["duration_seconds"] = 9.0
    there["scepter"]["detection"]["sample_seconds"] = 3.0
    one, two = equivalence.report_digest(report), equivalence.report_digest(there)
    assert one == two and one["missing"] == []
    there["scepter"]["config"]["threads"] = 8
    assert equivalence.report_digest(there)["sections"]["scepter.config"] != one["sections"]["scepter.config"]
    assert equivalence.report_digest({"summary": {}})["missing"] == sorted(equivalence.REPORT_RUN_SPECIFIC)


# --------------------------------------------------------------------------------------
# selftest.py
# --------------------------------------------------------------------------------------

def test_selftest_run_and_compare(engines_dir, tmp_path):
    answers = tmp_path / "out" / "selftest.txt"
    status, out = tool("selftest.py", "run", "--engines", engines_dir, "--out", answers)
    assert status == 0, out
    assert "both self-tests passed" in out
    found = selftest.parse(str(answers))
    assert set(found) == {"tesseract-asm", "scepter"}
    for engine in found.values():
        assert engine["exit"] == "0" and engine["selftest"] == "pass" and len(engine) > 20
    assert found["tesseract-asm"]["fprintf_fasta_header"] == ">NODE_1_length_1234_cov_24.8645"
    assert found["scepter"]["sizeof_long_long"] == "8"
    assert b"\r" not in answers.read_bytes()

    status, out = tool("selftest.py", "compare", answers, answers)
    assert status == 0 and "the self-tests agree" in out, out

    # what describes the platform may differ; Windows answers more questions than Linux
    text = answers.read_text(encoding="utf-8")
    first, second = text.split("[scepter]")
    windows = (first.replace("platform=linux", "platform=windows").replace("sizeof_long=8", "sizeof_long=4")
               + "[scepter]"
               + second.replace("target=linux", "target=windows").replace("sizeof_long=8", "sizeof_long=4")
               .replace("sizeof_z_off_t=8", "sizeof_z_off_t=4").replace("info_long_paths=1", "info_long_paths=0")
               .replace("selftest=pass\n", "active_code_page=65001\nstdout_mode=binary\nselftest=pass\n"))
    assert windows.count("=") == text.count("=") + 2 and "platform=windows" in windows
    other = tmp_path / "windows.txt"
    other.write_text(windows, encoding="utf-8", newline="\r\n")
    status, out = tool("selftest.py", "compare", answers, other)
    assert status == 0, out

    # a rounding answer that differs is a real difference
    other.write_text(windows.replace("printf_4f_0.03125=0.0312", "printf_4f_0.03125=0.0313")
                     .replace("text_mode_write_bytes=2", "text_mode_write_bytes=3"), encoding="utf-8")
    status, out = tool("selftest.py", "compare", answers, other)
    assert status == 1, out
    assert "tesseract-asm: printf_4f_0.03125 differs" in out and "scepter: text_mode_write_bytes differs" in out
    assert "2 difference(s)" in out

    # an answer only one side gives, and a self-test that failed
    other.write_text(windows.replace("unicode_dir=1\n", "").replace("[scepter]\nexit=0", "[scepter]\nexit=1"),
                     encoding="utf-8")
    status, out = tool("selftest.py", "compare", answers, other)
    assert status == 1 and "unicode_dir is answered only in" in out and "did not pass (exit 1" in out, out


#: Every answer that differed between the Linux and the Windows engines in CI (the engine smoke run of
#: 2026-11-16: windows-2022, MSYS2 UCRT64), with what Windows said. Answers Linux does not give at all
#: are included. The tools could not be run on Windows here; this is what a real Windows said.
WINDOWS_SAID = {
    "tesseract-asm": {"platform": "windows", "sizeof_long": "4"},
    "scepter": {"target": "windows", "sizeof_long": "4", "sizeof_z_off_t": "4", "zlib_version": "1.3.2",
                "exe_path": "D:/a/w/w/build/engines/windows/scepter.exe",
                "work_dir": "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp/scepter-selftest-5020",
                "active_code_page": "65001", "unicode_dir_wide": "1", "error_mode": "0x8003",
                "default_file_mode": "binary", "stdin_mode": "binary", "stdout_mode": "binary",
                "stderr_mode": "binary", "wildcard_expansion": "off"},
}


def test_what_a_real_windows_answered_is_covered():
    for engine, answers in WINDOWS_SAID.items():
        for key in answers:
            assert selftest.platform_reason(engine, key), "%s %s differs on a real Windows and has no reason" % (engine, key)
    linux = {"tesseract-asm": {"exit": "0", "selftest": "pass", "platform": "linux", "sizeof_long": "8",
                               "printf_4f_0.03125": "0.0312", "same_file_two_files": "0"},
             "scepter": {"exit": "0", "selftest": "pass", "target": "linux", "sizeof_long": "8", "sizeof_z_off_t": "8",
                         "zlib_version": "1.3", "exe_path": "/x/scepter", "work_dir": "./scepter-selftest-1",
                         "round_2f_0.125": "0.12", "text_mode_write_bytes": "2"}}
    windows = {engine: dict(answers, **WINDOWS_SAID[engine]) for engine, answers in linux.items()}
    problems, compared, left_out = selftest.compare_answers(linux, windows, "linux", "windows")
    assert problems == [] and compared == 8 and left_out == 16
    # the same Windows with one rounding answer of its own is caught
    windows["scepter"]["round_2f_0.125"] = "0.13"
    problems, _, _ = selftest.compare_answers(linux, windows, "linux", "windows")
    assert problems == ["scepter: round_2f_0.125 differs\n    linux: 0.12\n    windows: 0.13"]


@pytest.mark.skipif(os.name == "nt", reason="the stand-in engines are shell scripts")
def test_selftest_run_replaces_old_answers_and_keeps_what_explains_a_failure(tmp_path):
    folder = tmp_path / "engines"
    folder.mkdir()
    for name in ("tesseract-asm", "scepter"):
        path = folder / name
        path.write_text("#!/bin/sh\necho 'Assertion failed: something broke'\necho 'on stderr' >&2\nexit 3\n",
                        encoding="utf-8")
        path.chmod(0o755)
    answers = tmp_path / "answers.txt"
    answers.write_text("[tesseract-asm]\nexit=0\nselftest=pass\n[scepter]\nexit=0\nselftest=pass\n", encoding="utf-8")
    status, out = tool("selftest.py", "compare", answers, answers)
    assert status == 0, out
    status, out = tool("selftest.py", "run", "--engines", folder, "--out", answers)
    assert status == 1 and "ended with status 3" in out
    assert "# (printed) Assertion failed: something broke" in out and "# on stderr" in out
    text = answers.read_text(encoding="utf-8")
    assert "exit=3" in text and "selftest=pass" not in text and "# (printed) Assertion failed" in text
    status, out = tool("selftest.py", "compare", answers, answers)
    assert status == 1 and "did not pass (exit 3, selftest=none)" in out

    # a file that "run" did not write is explained, not a traceback
    answers.write_bytes("[scepter]\nexit=0\n".encode("utf-16"))
    status, out = tool("selftest.py", "compare", answers, answers)
    assert status == 1 and "is not a file written by" in out and "Traceback" not in out, out
    status, out = tool("selftest.py", "compare", tmp_path / "absent.txt", answers)
    assert status == 1 and "cannot read" in out and "Traceback" not in out, out


def test_every_platform_answer_has_a_reason():
    for engine, pattern, reason in selftest.PLATFORM_ANSWERS:
        assert engine in selftest.ENGINES and pattern and len(reason) > 15
    assert selftest.platform_reason("scepter", "info_long_paths")
    assert selftest.platform_reason("scepter", "round_2f_0.125") is None
    assert selftest.platform_reason("tesseract-asm", "fprintf_fasta_header") is None
    assert selftest.platform_reason("tesseract-asm", "target") is None, "a rule of one engine is not the other's"


@pytest.mark.skipif(os.name == "nt", reason="the stand-in engines are shell scripts")
def test_selftest_run_fails_when_an_engine_fails(tmp_path):
    folder = tmp_path / "engines"
    folder.mkdir()
    for name, body in (("tesseract-asm", "echo platform=linux; echo same_file_two_files=1; echo selftest=fail; exit 1"),
                       ("scepter", "echo usage: scepter; exit 0")):
        path = folder / name
        path.write_text("#!/bin/sh\n%s\n" % body, encoding="utf-8")
        path.chmod(0o755)
    status, out = tool("selftest.py", "run", "--engines", folder, "--out", tmp_path / "a.txt")
    assert status == 1
    assert "tesseract-asm  3 answers, ended with status 1" in out
    assert "did not end with selftest=pass" in out and "FAILED" in out
    assert "same_file_two_files=1" in out, "the answers of a failed self-test are shown"
    status, out = tool("selftest.py", "compare", tmp_path / "a.txt", tmp_path / "a.txt")
    assert status == 1 and "did not pass" in out
    status, out = tool("selftest.py", "run", "--engines", tmp_path / "nowhere", "--out", tmp_path / "b.txt")
    assert status == 1 and "is missing" in (tmp_path / "b.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# update.py --dry-run
# --------------------------------------------------------------------------------------

PINS = {"tesseract": (versions.TESSERACT_VERSION, versions.TESSERACT_COMMIT),
        "scepter": (versions.SCEPTER_VERSION, versions.SCEPTER_COMMIT)}


@pytest.fixture()
def untouched():
    """Fails the test if a dry run changed the project."""
    before = {name: tree_digest(ENGINES / name / "upstream") for name in PINS}
    pins = sha256(REPO / "tesseract_win" / "versions.py")
    yield
    assert {name: tree_digest(ENGINES / name / "upstream") for name in PINS} == before
    assert sha256(REPO / "tesseract_win" / "versions.py") == pins
    assert not (ENGINES / "tesseract" / "upstream.incoming").exists()


def dry_run(engine, source, version=None, commit=None, *extra):
    return tool("update.py", engine, "--from", source, "--version", version or PINS[engine][0],
                "--commit", commit or PINS[engine][1], "--dry-run", "--skip-build", *extra)


@pytest.mark.parametrize("engine", ["tesseract", "scepter"])
def test_update_with_the_current_tree_changes_nothing(engine, untouched):
    status, out = dry_run(engine, ENGINES / engine / "upstream")
    assert status == 0, out
    assert "nothing changed: this is the release the project already uses" in out
    assert "it was left as it is" in out and "FAILED" not in out and "LOOK" not in out
    assert "ok      staging and the scripted Windows changes" in out
    assert "Nothing. This is the release the project already uses and every guard passed." in out
    assert "DRY RUN finished" in out and "exactly as they were" in out
    if engine == "tesseract":
        assert "the tesseract script is the one the Python port matches" in out


def test_update_names_the_section_of_the_script_that_changed(tmp_path, untouched):
    source = tmp_path / "TesserACT-edited"
    shutil.copytree(ENGINES / "tesseract" / "upstream", source)
    script = source / "tesseract"
    text = script.read_text(encoding="utf-8")
    start = text.index("# ---------------------------------------------------------------- postflight")
    old = 'pf FAIL "exit status"'
    assert text.count(old) == 1 and text.index(old) > start, "the line this test edits moved"
    script.write_text(text.replace(old, 'pf WARN "exit status"'), encoding="utf-8", newline="\n")
    status, out = dry_run("tesseract", source)
    assert status == 0, out
    assert "LOOK    the tesseract script is not the one the Python port matches" in out
    assert "changed in postflight (logic)" in out
    assert "the tesseract script changed in its postflight section" in out
    assert "update tesseract_win/postflight.py" in out and "re-capture the goldens" in out
    assert '-' in out and '+' in out and 'pf WARN "exit status"' in out, "the changed line is shown"
    for other in ("head", "arguments", "preflight", "assembly"):
        assert "in its %s section" % other not in out
    assert "TESSERACT_WRAPPER_SHA256: %s -> %s" % (versions.TESSERACT_WRAPPER_SHA256, sha256(script)) in out
    assert "engines/tesseract/upstream replaced: 0 files added, 0 removed, 1 changed" in out
    assert "nothing changed" not in out


def test_update_tells_a_comment_from_logic_and_reads_an_archive(tmp_path, untouched):
    source = tmp_path / "pack" / "TesserACT-1.6.2"
    shutil.copytree(ENGINES / "tesseract" / "upstream", source)
    script = source / "tesseract"
    text = script.read_text(encoding="utf-8")
    marker = "# ---------------------------------------------------------------- preflight\n"
    assert marker in text
    script.write_text(text.replace(marker, marker + "# a remark that changes nothing\n"), encoding="utf-8", newline="\n")
    archive = shutil.make_archive(str(tmp_path / "release"), "gztar", root_dir=str(tmp_path / "pack"))
    status, out = dry_run("tesseract", archive)
    assert status == 0, out
    assert "changed in preflight (comments only)" in out
    assert "only in comments or blank lines: nothing there for the Python port to follow" in out
    assert "the port needs no change, but the parity tests fail until" in out


def test_update_stops_at_a_changed_line_the_port_depends_on(tmp_path, untouched):
    source = tmp_path / "tesseract-new"
    shutil.copytree(ENGINES / "tesseract" / "upstream", source)
    target = source / "src" / "p2_provenance.cpp"
    text = target.read_text(encoding="utf-8")
    assert text.count('"/proc/self/exe"') == 1
    target.write_text(text.replace('"/proc/self/exe"', '"/proc/self/exe" /* moved */ + std::string()'),
                      encoding="utf-8", newline="\n")
    status, out = dry_run("tesseract", source)
    assert status == 1, out
    assert "FAILED  staging and the scripted Windows changes stopped" in out and "rule R2" in out
    assert "a line the Windows port depends on is no longer what it was" in out
    assert "RESULT: a guard failed" in out and "DRY RUN finished" in out


def test_update_reports_what_is_new_in_a_release(tmp_path, untouched):
    """A made-up scepter 1.2.0: a new option, a new header, a new 'long' that the guard must stop."""
    source = tmp_path / "scepter-1.2.0"
    shutil.copytree(ENGINES / "scepter" / "upstream", source)
    header = source / "src" / "cli.h"
    header.write_text(header.read_text(encoding="utf-8").replace('"%s"' % versions.SCEPTER_VERSION, '"1.2.0"'),
                      encoding="utf-8", newline="\n")
    status, out = dry_run("scepter", source, "1.2.0", "0123456789abcdef0123456789abcdef01234567")
    assert status == 0, out
    assert "scepter %s -> 1.2.0" % versions.SCEPTER_VERSION in out
    assert "SCEPTER_VERSION: %s -> 1.2.0" % versions.SCEPTER_VERSION in out
    assert "SCEPTER_COMMIT: %s -> 0123456789abcdef0123456789abcdef01234567" % versions.SCEPTER_COMMIT in out
    assert "T1-fseek64 replaced 1 of 1" in out and "every guard passed" in out

    util = source / "src" / "util.cpp"
    util.write_text("#include <sys/mman.h>\n" + util.read_text(encoding="utf-8")
                    + "\nstatic long added_in_this_release = 0;\n", encoding="utf-8", newline="\n")
    status, out = dry_run("scepter", source, "1.2.0", "0123456789abcdef0123456789abcdef01234567")
    assert status == 1, out
    assert "FAILED  staging and the scripted Windows changes stopped" in out and "src/util.cpp" in out
    assert "system headers it did not include before (<sys/mman.h>)" in out


def test_update_refuses_what_cannot_be_right(tmp_path, untouched):
    upstream = ENGINES / "tesseract" / "upstream"
    status, out = dry_run("tesseract", upstream, "9.9.9")
    assert status == 2 and "says it is TesserACT %s, but --version is 9.9.9" % versions.TESSERACT_VERSION in out
    status, out = dry_run("tesseract", upstream, None, "abc123")
    assert status == 2 and "--commit must be the full commit" in out
    status, out = dry_run("tesseract", tmp_path / "absent")
    assert status == 2 and "does not exist" in out
    status, out = dry_run("tesseract", ENGINES / "scepter" / "upstream")
    assert status == 2 and "does not hold the source of this engine" in out
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"this is not an archive")
    status, out = dry_run("tesseract", junk)
    assert status == 2 and "neither a folder nor an archive" in out


def test_update_rewrites_only_the_pins(tmp_path):
    """The rewriting itself, on a copy of versions.py."""
    (tmp_path / "tesseract_win").mkdir()
    copy = tmp_path / "tesseract_win" / "versions.py"
    shutil.copy2(REPO / "tesseract_win" / "versions.py", copy)
    before = copy.read_text(encoding="utf-8")
    assert update.read_pins(tmp_path)["TESSERACT_WRAPPER_SHA256"] == versions.TESSERACT_WRAPPER_SHA256
    changes = update.rewrite_pins(tmp_path, {"TESSERACT_VERSION": "1.7.0", "TESSERACT_TAG": "v1.7.0",
                                             "TESSERACT_COMMIT": versions.TESSERACT_COMMIT})
    assert changes == ["TESSERACT_VERSION: %s -> 1.7.0" % versions.TESSERACT_VERSION,
                       "TESSERACT_TAG: %s -> v1.7.0" % versions.TESSERACT_TAG]
    after = copy.read_text(encoding="utf-8")
    assert after == before.replace('"%s"' % versions.TESSERACT_VERSION, '"1.7.0"', 1).replace(
        '"%s"' % versions.TESSERACT_TAG, '"v1.7.0"', 1)
    assert update.read_pins(tmp_path)["SCEPTER_VERSION"] == versions.SCEPTER_VERSION
    with pytest.raises(update.Stop, match="exactly once"):
        update.rewrite_pins(tmp_path, {"NO_SUCH_PIN": "1"})


# --------------------------------------------------------------------------------------
# update.py for real, in a copy of the project (the command itself is never run on this tree)
# --------------------------------------------------------------------------------------

NEW_COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture()
def project(tmp_path) -> Path:
    """A copy of what update.py reads and writes, to run it in without --dry-run."""
    root = tmp_path / "project copy"
    shutil.copytree(ENGINES, root / "engines", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "upstream.incoming"))
    shutil.copytree(REPO / "tesseract_win", root / "tesseract_win",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "assets"))
    for rel in (update.FINGERPRINT_TOOL, update.FINGERPRINT_RECORD):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, root / rel)
    return root


def real_update(root: Path, engine: str, source, version: str, commit: str):
    done = subprocess.run([sys.executable, str(root / "engines" / "update.py"), engine, "--from", str(source),
                           "--version", version, "--commit", commit, "--skip-build"], cwd=str(root),
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                          encoding="utf-8", errors="replace")
    return done.returncode, done.stdout


def made_up_scepter(folder: Path) -> Path:
    """A scepter 1.2.0 that differs from the pinned one by its version and by its licence text."""
    shutil.copytree(ENGINES / "scepter" / "upstream", folder)
    header = folder / "src" / "cli.h"
    header.write_text(header.read_text(encoding="utf-8").replace('"%s"' % versions.SCEPTER_VERSION, '"1.2.0"'),
                      encoding="utf-8", newline="\n")
    licence = folder / "LICENSE"
    licence.write_text(licence.read_text(encoding="utf-8") + "\nOne more condition.\n", encoding="utf-8", newline="\n")
    return folder


LICENCE_NOTE = "the LICENSE file of scepter changed: update the NOTICE file of this project"


def pins_of(root: Path) -> dict:
    return update.read_pins(root)


def test_update_for_real_then_again_then_back(project, tmp_path):
    """The command without --dry-run: it replaces, it can be repeated without losing its list, and the
    tree it kept can be brought back with the same command."""
    upstream = project / "engines" / "scepter" / "upstream"
    kept = project / "build" / "update" / "scepter" / "upstream-before"
    pinned = tree_digest(upstream)
    source = made_up_scepter(tmp_path / "scepter new [1.2.0] à")
    header_note = LICENCE_NOTE

    status, out = real_update(project, "scepter", source, "1.2.0", NEW_COMMIT)
    assert status == 0, out
    assert "scepter %s -> 1.2.0" % versions.SCEPTER_VERSION in out
    assert "engines/scepter/upstream replaced: 0 files added, 0 removed, 2 changed" in out and header_note in out
    assert tree_digest(upstream) == tree_digest(source) and tree_digest(kept) == pinned
    assert pins_of(project)["SCEPTER_VERSION"] == "1.2.0" and pins_of(project)["SCEPTER_COMMIT"] == NEW_COMMIT
    assert pins_of(project)["TESSERACT_VERSION"] == versions.TESSERACT_VERSION, "the other engine's pins are left alone"
    assert not (project / "engines" / "scepter" / "upstream.incoming").exists()

    # the same command again (UPDATING.md: "run the same command again"): the list is still complete
    status, again = real_update(project, "scepter", source, "1.2.0", NEW_COMMIT)
    assert status == 0, again
    assert "an earlier run of this command put it there" in again and "0 files added, 0 removed, 2 changed" in again
    assert "scepter %s -> 1.2.0" % versions.SCEPTER_VERSION in again and header_note in again
    assert "nothing changed" not in again and "Nothing. This is the release" not in again
    assert tree_digest(upstream) == tree_digest(source) and tree_digest(kept) == pinned, "a repeat replaces nothing"

    # back to the tree that was kept, given as --from although the command deletes that folder itself
    status, back = real_update(project, "scepter", kept, versions.SCEPTER_VERSION, versions.SCEPTER_COMMIT)
    assert status == 0, back
    assert "Traceback" not in back and "scepter 1.2.0 -> %s" % versions.SCEPTER_VERSION in back
    assert tree_digest(upstream) == pinned and tree_digest(kept) == tree_digest(source)
    assert pins_of(project)["SCEPTER_VERSION"] == versions.SCEPTER_VERSION
    assert (project / "tesseract_win" / "versions.py").read_bytes() == (REPO / "tesseract_win" / "versions.py").read_bytes()


def test_update_after_a_failed_guard_the_same_command_still_gives_the_whole_list(project, tmp_path):
    source = tmp_path / "scepter-1.2.0"
    made_up_scepter(source)
    util = source / "src" / "util.cpp"
    util.write_text("#include <sys/mman.h>\n" + util.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    outputs = []
    for attempt in range(2):
        status, out = real_update(project, "scepter", source, "1.2.0", NEW_COMMIT)
        assert status == 1, out
        assert "FAILED  staging and the scripted Windows changes stopped" in out and "RESULT: a guard failed" in out
        assert "system headers it did not include before (<sys/mman.h>)" in out, "attempt %d lost the list" % attempt
        assert LICENCE_NOTE in out
        outputs.append(out.split("What a person must look at now")[1])
    assert outputs[0] == outputs[1], "the second run must ask for the same things as the first"


def test_update_finishes_what_a_stopped_run_began(project, tmp_path):
    """Review focus 1. Stopped after the old tree was moved aside and before the new one was in place."""
    upstream = project / "engines" / "scepter" / "upstream"
    kept = project / "build" / "update" / "scepter" / "upstream-before"
    pinned = tree_digest(upstream)
    source = made_up_scepter(tmp_path / "scepter-1.2.0")
    kept.parent.mkdir(parents=True)
    shutil.move(str(upstream), str(kept))
    half = upstream.with_name("upstream.incoming")
    (half / "src").mkdir(parents=True)
    (half / "src" / "main.cpp").write_text("// half a copy\n", encoding="utf-8")

    status, out = real_update(project, "scepter", source, "1.2.0", NEW_COMMIT)
    assert status == 0, out
    assert "was stopped while it replaced it" in out and "0 files added, 0 removed, 2 changed" in out
    assert LICENCE_NOTE in out
    assert tree_digest(upstream) == tree_digest(source) and not half.exists()
    assert tree_digest(kept) == pinned, "the tree that was moved aside is still the one that is kept"


def test_update_changes_nothing_when_it_cannot_finish(project, tmp_path):
    upstream = project / "engines" / "scepter" / "upstream"
    pinned = tree_digest(upstream)
    source = made_up_scepter(tmp_path / "scepter-1.2.0")
    pins = project / "tesseract_win" / "versions.py"
    text = pins.read_text(encoding="utf-8")
    pins.write_text(text.replace("SCEPTER_COMMIT = ", "SCEPTER_REVISION = "), encoding="utf-8", newline="\n")
    status, out = real_update(project, "scepter", source, "1.2.0", NEW_COMMIT)
    assert status == 2, out
    assert "must set SCEPTER_COMMIT exactly once" in out and "Nothing was changed" in out
    assert tree_digest(upstream) == pinned and "1.2.0" not in pins.read_text(encoding="utf-8")
    assert not (project / "build").exists() and not upstream.with_name("upstream.incoming").exists()

    pins.write_text(text, encoding="utf-8", newline="\n")
    incoming = upstream.with_name("upstream.incoming")
    shutil.copytree(source, incoming)
    status, out = real_update(project, "scepter", incoming, "1.2.0", NEW_COMMIT)
    assert status == 2 and "this command's own working copy" in out and tree_digest(upstream) == pinned, out


def test_update_copies_the_release_and_not_what_a_build_left_in_it(project, tmp_path):
    source = tmp_path / "TesserACT-built"
    shutil.copytree(ENGINES / "tesseract" / "upstream", source)
    (source / "build").mkdir()
    (source / "build" / "main.o").write_bytes(b"object")
    (source / "tesseract-asm").write_bytes(b"a program")
    (source / "src" / "left.o").write_bytes(b"object")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    status, out = real_update(project, "tesseract", source, *PINS["tesseract"])
    assert status == 0, out
    assert "it was left as it is" in out and "nothing changed" in out, "build products are not a change"

    # a folder of upstream's own that happens to be called build, below the top, is part of the release
    (source / "tests" / "data" / "build").mkdir(parents=True)
    (source / "tests" / "data" / "build" / "expected.txt").write_text("kept\n", encoding="utf-8")
    status, out = real_update(project, "tesseract", source, *PINS["tesseract"])
    assert status == 0, out
    assert "replaced: 1 files added, 0 removed, 0 changed" in out and "tests/data/build/expected.txt" in out
    upstream = project / "engines" / "tesseract" / "upstream"
    assert (upstream / "tests" / "data" / "build" / "expected.txt").is_file()
    for absent in ("build", "tesseract-asm", "src/left.o", ".git"):
        assert not (upstream / absent).exists(), absent
    assert (upstream / "tesseract").is_file(), "the bash script is the release, not a build product"


# --------------------------------------------------------------------------------------
# run_upstream_suites.sh (Linux; two quick suites: CI runs all six)
# --------------------------------------------------------------------------------------

def _suites(tmp_path, engines, only):
    env = dict(os.environ, ENGINES_DIR=str(engines), SUITES_ONLY=only, SUITES_WORK=str(tmp_path / "work"),
               SUITES_LOG_DIR=str(tmp_path / "logs"))
    done = subprocess.run(["bash", str(ENGINES / "ci" / "run_upstream_suites.sh"), "linux"], cwd=str(REPO), env=env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True)
    return done.returncode, done.stdout


@pytest.fixture()
def suite_engines(engines_dir):
    staged = REPO / "build" / "stage" / "linux"
    if os.name == "nt" or shutil.which("bash") is None:
        pytest.skip("the suites run under bash (on Windows: in the MSYS2 step of CI)")
    program = staged / "tesseract" / "tesseract-asm"
    if not program.is_file() or program.read_bytes() != (engines_dir / "tesseract-asm").read_bytes():
        pytest.skip("build/stage/linux does not hold the build of %s" % engines_dir)
    return engines_dir


def test_upstream_suites_pass_on_the_built_engines(suite_engines, tmp_path):
    status, out = _suites(tmp_path, suite_engines, "scepter-flags,tesseract-memory")
    assert status == 0, out
    assert "scepter-flags     passed" in out and "tesseract-memory  passed" in out
    assert "RESULT: all 2 suites passed" in out
    assert (tmp_path / "logs" / "scepter-flags.log").is_file() and (tmp_path / "logs" / "summary.txt").is_file()
    assert not (tmp_path / "work").exists()
    status, out = _suites(tmp_path, suite_engines, "no-such-suite")
    assert status == 1 and "No suite was run" in out


def test_upstream_suites_fail_on_a_broken_engine(suite_engines, tmp_path):
    broken = tmp_path / "engines"
    broken.mkdir()
    shutil.copy2(suite_engines / "tesseract-asm", broken / "tesseract-asm")
    fake = broken / "scepter"
    fake.write_text("#!/bin/sh\necho scepter 0.0.0\nexit 0\n", encoding="utf-8")     # trims nothing, says yes
    fake.chmod(0o755)
    status, out = _suites(tmp_path, broken, "scepter-flags")
    assert status == 1, out
    assert "scepter-flags     FAILED" in out and "check(s) failed" in out
    assert "RESULT: FAILED. 1 of 1 suites did not pass" in out
    assert "      FAIL  " in out, "the failed checks are listed"
    status, out = _suites(tmp_path, tmp_path / "nowhere", "scepter-flags")
    assert status == 2 and "is missing" in out
    status = subprocess.run(["bash", str(ENGINES / "ci" / "run_upstream_suites.sh"), "macos"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    assert status == 2


def test_upstream_suites_delete_only_folders_of_their_own(suite_engines, tmp_path):
    """SUITES_WORK and SUITES_LOG_DIR are deleted first: never the programs, never one inside the other."""
    script = ["bash", str(ENGINES / "ci" / "run_upstream_suites.sh"), "linux"]
    programs = tmp_path / "programs"
    shutil.copytree(suite_engines, programs)

    def run(**settings):
        env = dict(os.environ, ENGINES_DIR=str(programs), SUITES_ONLY="scepter-flags")
        env.update(settings)
        done = subprocess.run(script, cwd=str(REPO), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True)
        return done.returncode, done.stdout

    for settings in ({"SUITES_WORK": str(programs), "SUITES_LOG_DIR": str(tmp_path / "logs")},
                     {"SUITES_WORK": str(tmp_path / "work"), "SUITES_LOG_DIR": str(tmp_path)},
                     {"SUITES_WORK": str(tmp_path / "work"), "SUITES_LOG_DIR": str(programs / ".." / "programs")}):
        status, out = run(**settings)
        assert status == 2 and "refusing to use" in out, out
        assert (programs / "tesseract-asm").is_file() and (programs / "scepter").is_file()
    status, out = run(SUITES_WORK=str(tmp_path / "work"), SUITES_LOG_DIR=str(tmp_path / "work" / "logs"))
    assert status == 2 and "the logs would be lost" in out, out
    status, out = run(SUITES_WORK=str(tmp_path / "logs" / "work"), SUITES_LOG_DIR=str(tmp_path / "logs"))
    assert status == 2 and "a folder of its own" in out, out
    # and with folders of their own it runs; blanks in the list of suites do not hide a suite
    status, out = run(SUITES_WORK=str(tmp_path / "work dir"), SUITES_LOG_DIR=str(tmp_path / "log dir"),
                      SUITES_ONLY="no-such-suite, scepter-flags")
    assert status == 0 and "RESULT: all 1 suites passed" in out, out
    assert (tmp_path / "log dir" / "scepter-flags.log").is_file() and (programs / "scepter").is_file()
