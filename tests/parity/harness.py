# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""What the parity tests share: the fixtures, the golden cases, and the port driven like the script.

The goldens under ``golden/`` were captured from the real bash ``tesseract`` 1.6.2 and are
never edited. Each case directory holds ``cmd.txt`` (the command that was run, with paths
relative to this folder), what the script printed, its exit status and, for runs, the
files it left behind.

``run_port`` gives the same input to ``tesseract_win.readcheck`` and
``tesseract_win.postflight`` and returns what the script would have printed and written.
The engine is not run: its place is taken by the stored engine folders, either the canned
ones of the stand-in engine (``fake_asm/cases``) or, for the cases captured with the real
engine, the files kept in the golden itself.

Machine- and run-specific parts are masked exactly as the audit lists them
(``wrapper-port.md``, section 12): the ``disk`` and ``memory`` messages, and the engine's
own version line in ``SUMMARY.txt``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from tesseract_win import postflight, readcheck

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
GOLDEN = os.path.join(HERE, "golden")
FIXTURES = os.path.join(HERE, "fixtures")
FAKE_CASES = os.path.join(HERE, "fake_asm", "cases")
FAKE_ENGINE = os.path.join(HERE, "fake_asm", "fake_asm.sh")
MAKE_FIXTURES = os.path.join(HERE, "make_fixtures.py")
FIXTURES_DIGEST = os.path.join(HERE, "FIXTURES.sha256")
FINGERPRINT = os.path.join(GOLDEN, "SCRIPT_FINGERPRINT.json")
UPSTREAM_SCRIPT = os.path.join(ROOT, "engines", "tesseract", "upstream", "tesseract")
UPSTREAM_SKETCH = os.path.join(ROOT, "engines", "tesseract", "upstream", "models", "om2detect.sketch")

#: Numbers given to the port in place of this machine's free disk space and memory.
PINNED_FREE_BYTES = 8 << 30
PINNED_MEMORY_BYTES = 8 << 30
#: What the stand-in engine prints for ``--version``.
STUB_VERSION_LINE = "TesserACT 1.6.2-fake"
#: Files the wrapper writes itself; everything else in an output folder is the engine's.
WRAPPER_FILES = ("preflight.txt", "SUMMARY.txt", "assembly_stats.tsv")

_MACHINE = re.compile(rb"^(  (?:OK|FAIL|WARN) +(?:disk|memory) +).*$", re.M)
_ENGINE_VERSION = re.compile(rb"^(  version  tesseract [0-9.]+, engine ).*$", re.M)


def mask(data: Optional[bytes], engine_version: bool = False) -> Optional[bytes]:
    """Blank out what depends on the machine: the disk and memory messages, and on request
    the engine's version line."""
    if data is None:
        return None
    data = _MACHINE.sub(rb"\1<masked>", data)
    if engine_version:
        data = _ENGINE_VERSION.sub(rb"\1<masked>", data)
    return data


# --------------------------------------------------------------------------- fixtures

def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def manifest_digest(folder: str) -> str:
    """A digest of what the fixtures hold once decompressed (it does not depend on zlib)."""
    rows = []
    with open(os.path.join(folder, "MANIFEST.tsv"), "r", encoding="utf-8") as handle:
        next(handle)
        for line in handle:
            cells = line.rstrip("\n").split("\t")
            rows.append("\t".join((cells[1], cells[2], cells[5], cells[6])))
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def ensure_fixtures() -> str:
    """Make ``fixtures/`` if it is missing or was made by another ``make_fixtures.py``.

    About 25 seconds the first time; afterwards the folder is reused. The fixtures are
    made in a folder of their own and moved into place, so a second test run that starts
    meanwhile never sees half of them.
    """
    want = _sha256(MAKE_FIXTURES)
    stamp = os.path.join(FIXTURES, ".made_by")

    def current() -> bool:
        try:
            with open(stamp, "r", encoding="ascii") as handle:
                return handle.read().strip() == want
        except OSError:
            return False

    if current():
        return FIXTURES
    fresh = "%s.tmp%d" % (FIXTURES, os.getpid())
    shutil.rmtree(fresh, ignore_errors=True)
    subprocess.run([sys.executable, MAKE_FIXTURES, fresh], check=True, stdout=subprocess.DEVNULL)
    if os.path.exists(FIXTURES_DIGEST):
        with open(FIXTURES_DIGEST, "r", encoding="ascii") as handle:
            recorded = [line.strip() for line in handle if line.strip() and not line.startswith("#")][0]
        if manifest_digest(fresh) != recorded:
            shutil.rmtree(fresh, ignore_errors=True)
            raise AssertionError(
                "make_fixtures.py no longer writes the read sets the goldens were captured from "
                "(the digest of fixtures/MANIFEST.tsv is not the one in FIXTURES.sha256)")
    with open(os.path.join(fresh, ".made_by"), "w", encoding="ascii") as handle:
        handle.write(want + "\n")
    if current():                        # another run was quicker
        shutil.rmtree(fresh, ignore_errors=True)
        return FIXTURES
    stale = "%s.old.tmp%d" % (FIXTURES, os.getpid())
    try:
        if os.path.isdir(FIXTURES):
            os.rename(FIXTURES, stale)
        os.rename(fresh, FIXTURES)
    except OSError:
        if not current():
            raise
        shutil.rmtree(fresh, ignore_errors=True)
    shutil.rmtree(stale, ignore_errors=True)
    return FIXTURES


# --------------------------------------------------------------------------- golden cases

@dataclass
class Case:
    """One golden case: what was typed, and what the script did."""
    name: str                            # e.g. "good/check"
    folder: str
    kind: str                            # "check", "run" or "command-line"
    reason: str = ""                     # for "command-line": why it is not a case for the port
    script: str = ""                     # the script path of cmd.txt (no longer on disk)
    env: Dict[str, str] = field(default_factory=dict)
    argv: List[str] = field(default_factory=list)
    read1: str = ""
    read2: str = ""
    out: str = ""
    threads: Optional[str] = None
    genome: Optional[str] = None
    force: bool = False
    keep_going: bool = False
    extra: List[str] = field(default_factory=list)
    exit_status: int = 0

    @property
    def stub(self) -> str:
        """Name of the canned folder the stand-in engine copied; "" for the real engine."""
        return os.path.basename(self.env.get("FAKE_SRC", ""))

    @property
    def real_engine(self) -> bool:
        return self.kind == "run" and not self.stub

    def golden(self, name: str) -> Optional[bytes]:
        try:
            with open(os.path.join(self.folder, name), "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def tree(self) -> Optional[List[str]]:
        """Everything the script left in the output folder (folders end in "/")."""
        data = self.golden("out_tree.txt")
        if data is None:
            return None
        return sorted(entry[2:] if entry.startswith("./") else entry
                      for entry in data.decode("utf-8").split("\n") if entry)

    def calls(self) -> List[Tuple[List[str], Dict[str, str]]]:
        """Arguments and ``TESSERACT_*`` environment of every call of the stand-in engine."""
        out = []
        number = 1
        while True:
            argv = self.golden("calls/call%d.argv" % number)
            env = self.golden("calls/call%d.env" % number)
            if argv is None or env is None:
                return out
            pairs = dict(line.split("=", 1) for line in env.decode("utf-8").split("\n") if line)
            out.append((argv.decode("utf-8").split("\n")[:-1], pairs))
            number += 1


_VALUE_OPTIONS = {"-1": "read1", "--read1": "read1", "-2": "read2", "--read2": "read2", "-o": "out",
                  "--out": "out", "-t": "threads", "--threads": "threads", "--organism": "organism",
                  "-organism": "organism", "--genome-size": "genome"}


def _parse(case: Case) -> None:
    """Read the arguments the way the script does (audit section 2)."""
    words = list(case.argv)
    if not words or words[0] in ("-h", "--help", "help", "version", "--version", "-v", "models", "--help-all"):
        case.kind, case.reason = "command-line", "prints the usage text or a version and reads no file"
        return
    case.kind = "run"
    if words[0] == "check":
        case.kind = "check"
        words.pop(0)
    elif words[0] == "run":
        words.pop(0)
    values: Dict[str, str] = {}
    index = 0
    while index < len(words):
        word = words[index]
        if word in _VALUE_OPTIONS:
            if index + 1 >= len(words):
                case.kind, case.reason = "command-line", "an option without its value (the script hangs)"
                return
            values[_VALUE_OPTIONS[word]] = words[index + 1]
            index += 2
        elif word == "--force":
            case.force = True
            index += 1
        elif word == "--keep-going":
            case.keep_going = True
            index += 1
        elif word == "--":
            case.extra += words[index + 1:]
            break
        else:
            case.extra.append(word)
            index += 1
    case.read1, case.read2, case.out = values.get("read1", ""), values.get("read2", ""), values.get("out", "")
    case.threads = values.get("threads")
    case.genome = values.get("genome") or None
    if case.exit_status == 2:
        case.kind, case.reason = "command-line", "a usage error of the command line (exit status 2)"


def load_cases() -> List[Case]:
    cases = []
    for folder, _dirs, files in os.walk(GOLDEN):
        if "cmd.txt" not in files:
            continue
        name = os.path.relpath(folder, GOLDEN).replace(os.sep, "/")
        with open(os.path.join(folder, "cmd.txt"), "r", encoding="utf-8") as handle:
            words = shlex.split(handle.read().split("\n")[0])
        at = next(i for i, word in enumerate(words) if word.endswith("/tesseract") and "=" not in word)
        with open(os.path.join(folder, "exit_status.txt"), "r", encoding="ascii") as handle:
            status = int(handle.read().strip())
        case = Case(name=name, folder=folder, kind="", script=words[at],
                    env=dict(word.split("=", 1) for word in words[words.index("-i") + 1:at]),
                    argv=words[at + 1:], exit_status=status)
        _parse(case)
        cases.append(case)
    return sorted(cases, key=lambda c: c.name)


# --------------------------------------------------------------------------- the port, driven like the script

@dataclass
class Outcome:
    """What the port produced for one case, in the shape of a golden."""
    exit_status: int
    stdout: bytes
    files: Dict[str, Optional[bytes]]    # preflight.txt, SUMMARY.txt, assembly_stats.tsv (None: not written)
    tree: Optional[List[str]]            # what is in the output folder afterwards (None: a check)
    engine_calls: int = 0
    preflight: Optional[readcheck.ReadCheck] = None
    postflight: Optional[postflight.Postflight] = None
    retry: Optional[postflight.RetryDecision] = None


def tree_of(folder: str) -> List[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        for name in dirs:
            out.append(os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/") + "/")
        for name in files:
            out.append(os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/"))
    return sorted(out)


def _put(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def stub_engine(canned: str, out_dir: str, call: int) -> int:
    """What ``fake_asm.sh`` does: copy a canned folder into the output folder.

    From the second call on ``<canned>.retry`` is used when it exists. ``_stderr.txt`` is
    what the engine "printed" (it becomes ``assembly.log``) and ``_rc`` its exit status.
    """
    source = canned + ".retry" if call >= 2 and os.path.isdir(canned + ".retry") else canned
    os.makedirs(out_dir, exist_ok=True)
    log, status = b"", 0
    for name in sorted(os.listdir(source)):
        path = os.path.join(source, name)
        if name == "_stderr.txt":
            with open(path, "rb") as handle:
                log = handle.read()
        elif name == "_rc":
            with open(path, "r", encoding="ascii") as handle:
                status = int(handle.read().strip())
        else:
            shutil.copyfile(path, os.path.join(out_dir, name))
    _put(os.path.join(out_dir, "assembly.log"), log)
    return status


def stored_engine(case: Case, out_dir: str, first_attempt: bool) -> int:
    """Lay down what the real engine left behind, from what the golden kept of it.

    The golden lists every file of the output folder (``out_tree.txt``) and keeps the ones
    the wrapper reads (``engine_out/``). Files it lists but did not keep get a stand-in
    that is not empty, which is all the wrapper asks of them. With ``first_attempt`` the
    files that ended up in ``attempt1_default_k/`` are put where the first engine run
    wrote them. The exit status is the one the golden summary reports.
    """
    prefix = postflight.FIRST_ATTEMPT_DIR + "/"
    for entry in case.tree() or []:
        if entry.endswith("/"):
            continue
        inside = entry.startswith(prefix)
        if inside != first_attempt or entry in WRAPPER_FILES:
            continue
        kept = case.golden("engine_out/" + entry)
        target = entry[len(prefix):] if inside else entry
        _put(os.path.join(out_dir, *target.split("/")), kept if kept is not None else b"not kept in the golden\n")
    if first_attempt:
        return 0                         # the script retries only after an exit status of 0
    summary = case.golden("SUMMARY.txt") or b""
    found = re.search(rb"^  (?:OK|FAIL) +exit status +(\d+)", summary, re.M)
    assert found, "no exit status line in the golden summary of %s" % case.name
    return int(found.group(1))


def run_port(case: Case, workdir: str, wording: str = "upstream") -> Outcome:
    """Give the port the input of a golden case and collect what it prints and writes."""
    read1, read2 = os.path.join(HERE, case.read1), os.path.join(HERE, case.read2)
    if case.kind == "check":
        check = readcheck.check_reads(read1, read2, genome_size_mb=case.genome, wording=wording,
                                      shown_read1=case.read1, shown_read2=case.read2)
        return Outcome(0 if check.ok else 1, check.data, {}, None, preflight=check)
    assert case.kind == "run", case.kind
    out_dir = os.path.join(workdir, *case.out.split("/"))

    def collect(status: int, stdout: bytes, **more) -> Outcome:
        files: Dict[str, Optional[bytes]] = {}
        for name in WRAPPER_FILES:
            try:
                with open(os.path.join(out_dir, name), "rb") as handle:
                    files[name] = handle.read()
            except OSError:
                files[name] = None
        return Outcome(status, stdout, files, tree_of(out_dir), **more)

    check = readcheck.check_reads(read1, read2, out_dir=out_dir, genome_size_mb=case.genome, wording=wording,
                                  shown_read1=case.read1, shown_read2=case.read2, shown_out=case.out,
                                  free_bytes=PINNED_FREE_BYTES, available_memory_bytes=PINNED_MEMORY_BYTES)
    stdout = check.data
    if check.fails and not case.force:
        return collect(1, stdout, preflight=check)
    stdout += readcheck.encode_text("assembling into %s (vanilla) ...\n" % case.out)

    has_first = any(entry.startswith(postflight.FIRST_ATTEMPT_DIR + "/") for entry in case.tree() or [])

    def engine(call: int) -> int:
        if case.stub:
            return stub_engine(os.path.join(FAKE_CASES, case.stub), out_dir, call)
        return stored_engine(case, out_dir, first_attempt=has_first and call == 1)

    calls = 1
    status = engine(1)
    retry = postflight.thin_retry_needed(out_dir, exit_code=status, engine_args=case.extra,
                                         genome_size_mb=case.genome, stats_ran=check.has_statistics)
    if retry:
        stdout += readcheck.encode_text(retry.message + "\n")
        postflight.keep_first_attempt(out_dir)
        calls = 2
        status = engine(2)
    summary = postflight.summarise(
        out_dir, read1=case.read1, read2=case.read2, exit_code=status,
        engine_version=STUB_VERSION_LINE if case.stub else "TesserACT (masked)",
        genome_size_mb=case.genome, engine_args=case.extra, retried=bool(retry), wording=wording,
        shown_out=case.out)
    stdout += summary.data
    return collect(0 if summary.ok or case.keep_going else 1, stdout, engine_calls=calls, preflight=check,
                   postflight=summary, retry=retry)


def expected_engine_calls(case: Case) -> int:
    """How many times the script ran the engine in a golden run case."""
    if case.stub:
        return len(case.calls())
    tree = case.tree() or []
    if not any(entry == "assembly.log" for entry in tree):
        return 0
    return 2 if any(entry.startswith(postflight.FIRST_ATTEMPT_DIR + "/") for entry in tree) else 1


def differences(case: Case, outcome: Outcome) -> List[str]:
    """Where the port's output differs from the golden, after masking. Empty: identical."""
    out = []
    real = case.real_engine
    if outcome.exit_status != case.exit_status:
        out.append("exit status %d, golden %d" % (outcome.exit_status, case.exit_status))
    if mask(outcome.stdout, real) != mask(case.golden("stdout.txt") or b"", real):
        out.append("stdout")
    if case.kind == "run":
        for name in WRAPPER_FILES:
            if mask(outcome.files.get(name), real) != mask(case.golden(name), real):
                out.append(name)
        if outcome.tree != case.tree():
            out.append("files left in the output folder")
        if outcome.engine_calls != expected_engine_calls(case):
            out.append("engine runs: %d, golden %d" % (outcome.engine_calls, expected_engine_calls(case)))
    return out
