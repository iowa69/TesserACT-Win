# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The TesserACT engine port: its recipe, its scripted transformations and the staged Linux build.

What is checked here without any compiler:
  * staging copies upstream, never writes to it, and the transformations are idempotent;
  * a changed upstream line under a rule or a guard stops staging with an error that names it;
  * the LLP64 rewrite does what its header says, token by token;
  * the manifest and the version resource the recipe writes;
  * staging with odd folder names, a read-only upstream, a folder it must not delete, an interruption;
  * ``build`` driven with a stand-in compiler: a build that fails at any step leaves the program of
    the build before it in place and installs nothing that was not checked.

What is checked when the staged Linux engine is present (``build/engines/staged-linux``, made by
``engines/build.py --target linux --engine tesseract --out build/engines/staged-linux``):
  * ``--version`` answers with the pinned version;
  * the hidden self-test passes (``TS_COMPAT_SELFTEST=1``);
  * on three synthetic read sets it writes the same bytes as the reference binary.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "engines") not in sys.path:
    sys.path.insert(0, str(REPO / "engines"))

import buildlib  # noqa: E402
from tesseract_win import branding, versions  # noqa: E402

pytestmark = pytest.mark.engines

UPSTREAM = REPO / "engines" / "tesseract" / "upstream"
RECIPE_DIR = REPO / "engines" / "win" / "tesseract"
STAGED_DIR = REPO / "build" / "engines" / "staged-linux"
STAGED_ASM = STAGED_DIR / "tesseract-asm"
REFERENCE_DIR = REPO / "build" / "engines" / "reference-linux"
WINDOWS_ASM = REPO / "build" / "engines" / "windows" / "tesseract-asm.exe"
GEN = REPO / "tests" / "tools" / "gen.py"

#: The twelve programs the port audit found to need fork().
FORK_TESTS = {
    "test_correction_unique_best", "test_envflags", "test_envflags_retrofit", "test_polish_original_quality",
    "test_resolver_coverage_modes", "test_resolver_read_threads", "test_v14_defaults", "test_v3_io_cli",
    "test_v3_rel_coupling", "test_v3_graph_counter_lines", "test_v3_graph_counter_staging",
    "test_v3_graph_joindeadends",
}

#: The nine variables the bash ``tesseract`` wrapper sets (its "1.6 profile"); the two detection
#: variables are added by ``profile_env``.
PROFILE = {
    "TESSERACT_P2_LIBGUARD": "1", "TESSERACT_P2_CIRC": "verify", "TESSERACT_P2_SPIKEIN": "1",
    "TESSERACT_P2_ENDS": "1", "TESSERACT_P2_TIPS_LOWERCASE": "1", "TESSERACT_P2_SELF_QA": "1",
    "TESSERACT_P2_PROVENANCE": "1", "TESSERACT_DEEP_NORM": "30", "TESSERACT_DEEP_NORM_MATE": "both",
}

staged_engine = pytest.mark.skipif(
    os.name == "nt" or not STAGED_ASM.is_file(),
    reason="no staged Linux engine (engines/build.py --target linux --engine tesseract "
           "--out build/engines/staged-linux)")


@pytest.fixture(scope="module")
def recipe():
    path = RECIPE_DIR / "recipe.py"
    spec = importlib.util.spec_from_file_location("recipe_tesseract_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def changed_files(a: Path, b: Path):
    """Relative paths whose bytes differ between two trees, and paths present in only one."""
    files_a = {p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file()}
    differing = sorted(f for f in files_a & files_b if (a / f).read_bytes() != (b / f).read_bytes())
    return differing, sorted(files_a ^ files_b)


# ------------------------------------------------------------------------------------------- the recipe

def test_recipe_interface(recipe):
    assert recipe.NAME == "tesseract"
    assert recipe.BINARY == "tesseract-asm"
    assert list(inspect.signature(recipe.stage).parameters) == ["upstream", "stage", "target"]
    assert list(inspect.signature(recipe.build).parameters) == ["tc", "upstream", "stage", "out_dir", "jobs"]
    assert not (RECIPE_DIR / "Makefile.win").exists(), "the recipe replaces the prototype makefile"


def test_every_file_of_the_port_carries_the_licence_header():
    for path in sorted(RECIPE_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].startswith("#!"):
            lines = lines[1:]
        assert lines[0].lstrip("/# ") == "SPDX-License-Identifier: MIT", path.name
        assert lines[1].lstrip("/# ") == "Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin", path.name


def test_staging_leaves_upstream_untouched_and_is_idempotent(recipe, tmp_path):
    before = buildlib.tree_sha256(UPSTREAM)
    staged = tmp_path / "stage"
    counts = recipe.stage(UPSTREAM, staged, "linux")
    assert buildlib.tree_sha256(UPSTREAM) == before, "staging wrote to the pristine upstream tree"

    assert counts["version"] == versions.TESSERACT_VERSION
    assert counts["rules"] == {"R1": "applied", "R2": "applied"}
    assert counts["llp64"]["files"] > 0 and counts["llp64"]["type"] > 0
    assert counts["llp64_pinned"] and counts["llp64"] == recipe.LLP64_EXPECTED[versions.TESSERACT_VERSION]
    first = buildlib.tree_sha256(staged)
    assert first != before, "the transformations changed nothing"

    # A second application changes nothing and does not fail.
    again = recipe.transform(staged)
    assert buildlib.tree_sha256(staged) == first
    assert again["rules"] == {"R1": "already applied", "R2": "already applied"}
    assert again["llp64"] == {"files": 0, "type": 0, "func": 0, "literal": 0, "format": 0}
    assert again["guards"] == counts["guards"]
    assert buildlib.tree_sha256(UPSTREAM) == before


def test_the_staged_tree_is_the_same_for_both_targets(recipe, tmp_path):
    recipe.stage(UPSTREAM, tmp_path / "linux", "linux")
    recipe.stage(UPSTREAM, tmp_path / "windows", "windows")
    assert buildlib.tree_sha256(tmp_path / "linux") == buildlib.tree_sha256(tmp_path / "windows")
    with pytest.raises(buildlib.BuildError):
        recipe.stage(UPSTREAM, tmp_path / "other", "macos")


def test_staging_changes_only_engine_and_test_sources(recipe, tmp_path):
    staged = tmp_path / "stage"
    counts = recipe.stage(UPSTREAM, staged, "linux")
    differing, only_one = changed_files(UPSTREAM, staged)
    assert only_one == [], "staging added or removed files"
    assert all(f.startswith(("src/", "tests/")) and f.endswith((".cpp", ".h")) for f in differing), differing
    assert "src/p2_provenance.cpp" in differing
    assert len(differing) == counts["llp64"]["files"] + 1       # p2_provenance.cpp has no `long`
    provenance = (staged / "src" / "p2_provenance.cpp").read_text(encoding="utf-8")
    assert "TS_COMPAT_CMDLINE_STREAM(f);" in provenance
    assert 'digestFile("binary", TS_COMPAT_SELF_EXE, false)' in provenance
    assert "/proc/" not in provenance
    # dev_fork_batch.cpp stays in the tree: the upstream test source scan reads it.
    assert (staged / "src" / "dev_fork_batch.cpp").is_file()


def _mutations():
    cmdline = 'std::ifstream f("/proc/self/cmdline", std::ios::binary);'
    exe = 'digestFile("binary", "/proc/self/exe", false)'
    return [
        ("R1", "src/p2_provenance.cpp", lambda t: t.replace(cmdline, 'std::ifstream f("/proc/self/cmdline");')),
        ("R1", "src/p2_provenance.cpp", lambda t: t.replace(cmdline, cmdline + "\n    " + cmdline.replace(" f(", " g("))
         .replace(cmdline.replace(" f(", " g("), cmdline)),
        ("R2", "src/p2_provenance.cpp", lambda t: t.replace(exe, 'digestFile("binary", selfPath(), false)')),
        ("G1", "src/util.cpp", lambda t: t.replace("long long currentMemoryBytes() {", "long long residentBytes() {")),
        ("G3", "src/util.cpp", lambda t: t.replace("long long peakMemoryBytes() {", "long long peakMemoryBytes()\n{")),
        ("G4", "src/seqio.cpp", lambda t: t + '\nstatic const char* kMaps = "/proc/self/maps";\n'),
        ("G6", "src/gfa.cpp", lambda t: t + '\nstatic void sortWith() { std::FILE* p = popen("sort", "r"); (void)p; }\n'),
        ("G8", "src/gfa.cpp", lambda t: t + '\nstatic void quiet() { std::FILE* f = freopen("x", "w", stderr); (void)f; }\n'),
        # a call from inside util.cpp would reach the renamed POSIX copy, which answers 0 on Windows
        ("G9", "src/util.cpp", lambda t: t.replace("std::string dirname(", "long long memoryNow() { return currentMemoryBytes(); }\n\nstd::string dirname(")),
        ("G10", "src/gfa.cpp", lambda t: t + '\nstatic void swap() { std::rename("a.tmp", "a.gfa"); }\n'),
        ("LLP64", "src/gfa.cpp", lambda t: t + "\nstatic long gExtraCounter = 0;\n"),
        ("raw string", "src/gfa.cpp", lambda t: t + '\nstatic const char* kText = R"(long)";\n'),
    ]


@pytest.mark.parametrize("index", range(len(_mutations())), ids=[m[0] for m in _mutations()])
def test_a_changed_upstream_line_makes_staging_fail_loudly(recipe, tmp_path, index):
    """Each mutation stands for an upstream release that touched a line the port depends on."""
    name, relative, mutate = _mutations()[index]
    upstream = tmp_path / "upstream"
    shutil.copytree(UPSTREAM, upstream)
    target = upstream / relative
    text = target.read_text(encoding="utf-8")
    mutated = mutate(text)
    assert mutated != text, "the mutation did not apply: the test is out of date"
    target.write_text(mutated, encoding="utf-8")
    with pytest.raises(buildlib.BuildError) as caught:
        recipe.stage(upstream, tmp_path / "stage", "linux")
    assert name in str(caught.value), str(caught.value)


def test_staging_refuses_a_folder_that_is_not_the_engine(recipe, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(buildlib.BuildError):
        recipe.stage(tmp_path / "empty", tmp_path / "stage", "linux")


# ------------------------------------------------------------------------------- staging, harder cases

def copy_of_upstream(where: Path) -> Path:
    shutil.copytree(UPSTREAM, where)
    return where


def test_staging_with_spaces_accents_brackets_and_non_latin_names(recipe, tmp_path):
    """A "[" in the path of the project must not be read as the start of a character set."""
    plain = tmp_path / "plain"
    recipe.stage(UPSTREAM, plain, "linux")
    upstream = copy_of_upstream(tmp_path / "üp [1] stream (v2)")
    odd = tmp_path / "st age é [x] 漢字" / "stage [2]"
    counts = recipe.stage(upstream, odd, "windows")
    assert counts["rules"] == {"R1": "applied", "R2": "applied"} and counts["guards"]["G1"] == 1
    assert buildlib.tree_sha256(odd) == buildlib.tree_sha256(plain)
    again = recipe.win_rules.apply(odd)
    assert again["rules"] == {"R1": "already applied", "R2": "already applied"}
    assert again["guards"] == counts["guards"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_staging_a_read_only_upstream_twice(recipe, tmp_path):
    """A pristine tree may be kept read-only; its copy must still be transformed and, later, replaced."""
    upstream = copy_of_upstream(tmp_path / "upstream")
    try:
        for root, _dirs, files in os.walk(upstream):
            for name in files:
                os.chmod(os.path.join(root, name), 0o444)
        for root, _dirs, _files in os.walk(upstream, topdown=False):
            os.chmod(root, 0o555)
        before = buildlib.tree_sha256(upstream)
        staged = tmp_path / "stage"
        first = recipe.stage(upstream, staged, "linux")
        second = recipe.stage(upstream, staged, "linux")
        assert first == second and first["rules"] == {"R1": "applied", "R2": "applied"}
        assert buildlib.tree_sha256(upstream) == before
        assert stat.S_IMODE((upstream / "src" / "util.cpp").stat().st_mode) == 0o444, "upstream keeps its permissions"
        assert (staged / "src" / "util.cpp").stat().st_mode & stat.S_IWUSR
    finally:
        for root, _dirs, files in os.walk(upstream):
            os.chmod(root, 0o755)
            for name in files:
                os.chmod(os.path.join(root, name), 0o644)


@pytest.mark.parametrize("where", ["same", "inside", "parent", "other spelling", "link"])
def test_staging_never_deletes_or_writes_into_upstream(recipe, tmp_path, where):
    """Staging starts by deleting the folder it is given."""
    upstream = copy_of_upstream(tmp_path / "outer" / "upstream")
    before = buildlib.tree_sha256(upstream)
    if where == "link":
        if os.name == "nt":
            pytest.skip("needs symbolic links")
        (tmp_path / "link").symlink_to(upstream, target_is_directory=True)
    stage = {"same": upstream, "inside": upstream / "build" / "stage", "parent": tmp_path / "outer",
             "other spelling": upstream / "src" / ".." / ".." / "upstream", "link": tmp_path / "link"}[where]
    with pytest.raises(buildlib.BuildError) as caught:
        recipe.stage(upstream, stage, "linux")
    assert "refusing to stage" in str(caught.value)
    assert upstream.is_dir() and buildlib.tree_sha256(upstream) == before


def test_staging_never_deletes_the_recipe_folder(recipe, tmp_path, monkeypatch):
    here = tmp_path / "project" / "recipe"
    here.mkdir(parents=True)
    (here / "keep.txt").write_text("kept", encoding="utf-8")
    monkeypatch.setattr(recipe, "HERE", here)
    for stage in (here, here.parent, here / "stage"):
        with pytest.raises(buildlib.BuildError):
            recipe.stage(UPSTREAM, stage, "linux")
    assert (here / "keep.txt").read_text(encoding="utf-8") == "kept"


def test_staging_onto_a_file_fails_cleanly(recipe, tmp_path):
    (tmp_path / "stage").write_text("not a folder", encoding="utf-8")
    with pytest.raises(buildlib.BuildError):
        recipe.stage(UPSTREAM, tmp_path / "stage", "linux")
    assert (tmp_path / "stage").read_text(encoding="utf-8") == "not a folder"


def test_an_interrupted_staging_leaves_upstream_alone_and_the_next_one_is_complete(recipe, tmp_path, monkeypatch):
    before = buildlib.tree_sha256(UPSTREAM)
    staged = tmp_path / "stage"
    complete = recipe.stage(UPSTREAM, staged, "linux")
    good = buildlib.tree_sha256(staged)

    real = recipe.llp64_transform.transform
    calls = {"n": 0}

    def stopped_midway(text):
        calls["n"] += 1
        if calls["n"] == 25:
            raise KeyboardInterrupt()
        return real(text)

    monkeypatch.setattr(recipe.llp64_transform, "transform", stopped_midway)
    with pytest.raises(KeyboardInterrupt):
        recipe.stage(UPSTREAM, staged, "linux")
    assert buildlib.tree_sha256(UPSTREAM) == before
    assert buildlib.tree_sha256(staged) != good, "the interruption did not land in the middle: the test is out of date"
    monkeypatch.undo()
    assert recipe.stage(UPSTREAM, staged, "linux") == complete
    assert buildlib.tree_sha256(staged) == good


def test_a_source_that_is_not_utf8_stops_staging_and_is_named(recipe, tmp_path):
    upstream = copy_of_upstream(tmp_path / "upstream")
    target = upstream / "src" / "gfa.cpp"
    target.write_bytes(target.read_bytes() + b"\n// caf\xe9\n")
    with pytest.raises(buildlib.BuildError) as caught:
        recipe.stage(upstream, tmp_path / "stage", "linux")
    assert "src/gfa.cpp" in str(caught.value)


def test_staging_copes_with_empty_and_unrelated_files(recipe, tmp_path):
    upstream = copy_of_upstream(tmp_path / "upstream")
    (upstream / "src" / "empty.h").write_bytes(b"")
    (upstream / "src" / "notes [draft] é.txt").write_text("long story, 5L, %ld\n", encoding="utf-8")
    counts = recipe.stage(upstream, tmp_path / "stage", "linux")
    assert counts["llp64"] == recipe.LLP64_EXPECTED[versions.TESSERACT_VERSION]
    assert (tmp_path / "stage" / "src" / "empty.h").read_bytes() == b""
    assert (tmp_path / "stage" / "src" / "notes [draft] é.txt").read_text(encoding="utf-8") == "long story, 5L, %ld\n"


# ------------------------------------------------------------- build(), driven with a stand-in compiler

FAKE_TOOL = r"""
import os, sys
args = sys.argv[1:]
env = os.environ

def write(path, text):
    with open(path, "w") as handle:
        handle.write(text)

if "--version" in args:
    print("stand-in g++ 0.0")
    sys.exit(0)
if args[:1] == ["-p"]:                                  # objdump -p
    print("SizeOfStackReserve\t%s" % env.get("FAKE_STACK", "0000000001000000"))
    print("\tDLL Name: KERNEL32.dll")
    sys.exit(0)
if args[:1] == ["-h"]:                                  # objdump -h
    print("  0 .text         00001000")
    if env.get("FAKE_NO_RSRC") != "1":
        print("  5 .rsrc         000007e0")
    sys.exit(0)
out = args[args.index("-o") + 1]
if "coff" in args:                                      # windres
    write(out, "resource")
    sys.exit(0)
if "-c" in args:                                        # compile
    source = args[args.index("-c") + 1]
    name = os.path.basename(source)
    if name == env.get("FAKE_FAIL_COMPILE"):
        print("%s:1:1: error: stand-in failure" % source)
        sys.exit(1)
    if name == env.get("FAKE_WARN_COMPILE"):
        print("%s:1:1: warning: stand-in warning [-Wfake]" % source)
    write(out, "object of " + name)
    sys.exit(0)
name = os.path.basename(out)                            # link
if env.get("FAKE_FAIL_LINK") and name.startswith(env["FAKE_FAIL_LINK"]):
    print("ld: stand-in failure for " + name)
    sys.exit(1)
write(out, "program %s from %d objects" % (name, sum(a.endswith(".o") for a in args)))
"""

FAKE_SWITCHES = ("FAKE_STACK", "FAKE_NO_RSRC", "FAKE_FAIL_COMPILE", "FAKE_WARN_COMPILE", "FAKE_FAIL_LINK")


@pytest.fixture
def fake_tools(recipe, tmp_path, monkeypatch):
    """A toolchain whose compiler, resource compiler and objdump are one script that writes small
    text files, so the whole of ``build`` runs in a second or two. Logs go to the test's folder."""
    if os.name == "nt":
        pytest.skip("the stand-in compiler is a shell script")
    folder = tmp_path / "stand-in-tools"                # no space: the tests split the logged command lines
    folder.mkdir()
    (folder / "tool.py").write_text(FAKE_TOOL, encoding="utf-8")
    tool = folder / "g++"
    tool.write_text('#!/bin/sh\nexec %s %s "$@"\n' % (shlex.quote(sys.executable), shlex.quote(str(folder / "tool.py"))),
                    encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setattr(recipe, "LOG_DIR", tmp_path / "logs")
    for name in FAKE_SWITCHES:
        monkeypatch.delenv(name, raising=False)

    def toolchain(target):
        return buildlib.Toolchain(target=target, cxx=str(tool), windres=str(tool), strip=str(tool),
                                  objdump=str(tool), cross=target == "windows")
    return toolchain


def commands(log: Path):
    return [line[2:].split() for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("$ ")]


def test_build_for_linux_with_a_stand_in_compiler(recipe, tmp_path, fake_tools):
    before = buildlib.tree_sha256(UPSTREAM)
    stage, out = tmp_path / "stage[1]é" / "tesseract", tmp_path / "out dir é"
    facts = recipe.build(fake_tools("linux"), UPSTREAM, stage, out, 4)
    assert buildlib.tree_sha256(UPSTREAM) == before
    assert sorted(p.name for p in out.iterdir()) == ["tesseract-asm"], "nothing but the program, no partial file"
    assert (out / "tesseract-asm").read_text().startswith("program tesseract-asm")
    # where tests/run_tests.sh looks for them
    assert (stage / "tesseract-asm").read_bytes() == (out / "tesseract-asm").read_bytes()
    assert (stage / "tesseract-model").read_text().startswith("program tesseract-model")
    assert sorted(p.name for p in stage.iterdir() if p.name.endswith(".new")) == []
    assert facts["version"] == versions.TESSERACT_VERSION
    assert facts["git_commit"] == versions.TESSERACT_COMMIT[:12]
    assert facts["upstream_tree_sha256"] == before
    assert facts["port_sources_sha256"] == recipe.port_sources_sha256()
    assert facts["warnings"] == {"own": 0, "upstream": 0}
    assert facts["transformations"]["llp64"] == recipe.LLP64_EXPECTED[versions.TESSERACT_VERSION]
    assert facts["new_libc_imports"] is None, "nm cannot read the stand-in program"

    cmds = commands(tmp_path / "logs" / "build-linux.log")
    compiles = [c for c in cmds if "-c" in c]
    compiled = {Path(c[c.index("-c") + 1]).name: c for c in compiles}
    sources = {p.name for p in (UPSTREAM / "src").glob("*.cpp")}
    assert set(compiled) == sources | {"ts_compat.cpp", "ts_selftest.cpp", "ts_compat_tests.cpp"}
    header = str(RECIPE_DIR / "ts_compat.h")
    for name, cmd in compiled.items():
        assert cmd[cmd.index("-include") + 1] == header, name
        assert set(buildlib.common_cxxflags(fake_tools("linux"))) <= set(cmd), name
        assert not any(flag.startswith("-DTS_COMPAT_UTIL_TU") or "MINGW" in flag for flag in cmd), name
    assert '-DTS_GIT_COMMIT="%s"' % versions.TESSERACT_COMMIT[:12] in compiled["p2_provenance.cpp"]
    links = [c for c in cmds if "-c" not in c]
    assert len(links) == 2
    for link in links:
        objects = [Path(a).name for a in link if a.endswith(".o")]
        assert "ts_compat.o" in objects and "ts_selftest.o" in objects and "dev_fork_batch.o" in objects
        assert "ts_compat_tests.o" not in objects, "the test shims are not part of a shipped program"
        assert ("main.o" in objects) != ("model_main.o" in objects)
        assert "-lz" in link and "-static" not in link


def test_build_for_windows_with_a_stand_in_compiler(recipe, tmp_path, fake_tools):
    stage, out = tmp_path / "stage" / "tesseract", tmp_path / "out"
    tc = fake_tools("windows")
    facts = recipe.build(tc, UPSTREAM, stage, out, 4)
    assert sorted(p.name for p in out.iterdir()) == ["tesseract-asm.exe"]
    assert (stage / "tesseract-asm.exe").is_file() and (stage / "tesseract-model.exe").is_file()
    assert facts["stack_reserve"] == 16 * 1024 * 1024 and facts["has_resources"] is True

    cmds = commands(tmp_path / "logs" / "build-windows.log")
    compiled = {Path(c[c.index("-c") + 1]).name: c for c in cmds if "-c" in c}
    sources = {p.name for p in (UPSTREAM / "src").glob("*.cpp")}
    # the fork batch of upstream is replaced by the stand-in of the port, and only for Windows
    assert set(compiled) == (sources - {"dev_fork_batch.cpp"}) | {
        "ts_compat.cpp", "ts_selftest.cpp", "ts_compat_tests.cpp", "dev_fork_batch_win.cpp"}
    for name, cmd in compiled.items():
        for flag in ("-D__USE_MINGW_ANSI_STDIO=1", "-D_FILE_OFFSET_BITS=64", "-I" + str(RECIPE_DIR / "include")):
            assert flag in cmd, (name, flag)
        assert cmd[cmd.index("-include") + 1] == str(RECIPE_DIR / "ts_compat.h"), name
        assert set(buildlib.common_cxxflags(tc)) <= set(cmd), name
        # only util.cpp has its three memory functions renamed out of the way
        assert ("-DTS_COMPAT_UTIL_TU" in cmd) == (name == "util.cpp"), name
    links = [c for c in cmds if "-c" not in c and "coff" not in c and c[1] not in ("-p", "-h")]
    assert len(links) == 2
    for link in links:
        objects = [Path(a).name for a in link if a.endswith(".o")]
        assert "dev_fork_batch_win.o" in objects and "dev_fork_batch.o" not in objects
        assert "ts_compat.o" in objects and "ts_compat_tests.o" not in objects
        assert sum(name.endswith("_res.o") for name in objects) == 1, "manifest and version resource"
        assert set(buildlib.common_ldflags(tc)) <= set(link)
        assert "-static" in link and "-Wl,--stack,16777216" in link
    script = (recipe.object_dir(stage) / "res" / "tesseract-asm.rc").read_text(encoding="ascii")
    assert 'VALUE "FileDescription", "TesserACT assembler\\0"' in script
    assert 'VALUE "CompanyName", "IOWA-BioTech\\0"' in script
    assert 'VALUE "FileVersion", "%s\\0"' % versions.TESSERACT_VERSION in script


BUILD_FAILURES = [
    ("linux", {"FAKE_FAIL_COMPILE": "gfa.cpp"}, "gfa.cpp"),
    ("linux", {"FAKE_FAIL_COMPILE": "ts_selftest.cpp"}, "ts_selftest.cpp"),
    ("linux", {"FAKE_FAIL_LINK": "tesseract-asm"}, "tesseract-asm"),
    ("linux", {"FAKE_FAIL_LINK": "tesseract-model"}, "tesseract-model"),
    ("linux", {"FAKE_WARN_COMPILE": "ts_compat.cpp"}, "compiles with warnings"),
    ("windows", {"FAKE_FAIL_COMPILE": "dev_fork_batch_win.cpp"}, "dev_fork_batch_win.cpp"),
    ("windows", {"FAKE_STACK": "0000000000200000"}, "16 MiB"),
    ("windows", {"FAKE_NO_RSRC": "1"}, "manifest is missing"),
    ("windows", {"FAKE_FAIL_LINK": "tesseract-model"}, "tesseract-model"),
    ("windows", {"FAKE_WARN_COMPILE": "ts_compat.cpp"}, "compiles with warnings"),
]


@pytest.mark.parametrize("target, switches, message", BUILD_FAILURES,
                         ids=["%s %s" % (t, " ".join(sorted(s.values()))) for t, s, _ in BUILD_FAILURES])
def test_a_build_that_fails_leaves_the_earlier_program_in_place(recipe, tmp_path, fake_tools, monkeypatch,
                                                                target, switches, message):
    """Whatever step fails, out_dir holds the program of the build before it and nothing else:
    never a new program that was not checked, never a partial file."""
    tc = fake_tools(target)
    stage, out = tmp_path / "stage" / "tesseract", tmp_path / "out"
    out.mkdir()
    earlier = out / ("tesseract-asm" + tc.exe)
    earlier.write_text("the program of the build before", encoding="utf-8")
    for name, value in switches.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(buildlib.BuildError) as caught:
        recipe.build(tc, UPSTREAM, stage, out, 4)
    assert message in str(caught.value), str(caught.value)
    assert sorted(p.name for p in out.iterdir()) == [earlier.name]
    assert earlier.read_text(encoding="utf-8") == "the program of the build before"
    assert not (stage / earlier.name).exists(), "tests/run_tests.sh must not find the program of a failed build"
    assert not (stage / ("tesseract-model" + tc.exe)).exists()


def test_a_warning_in_upstream_code_is_counted_not_fatal(recipe, tmp_path, fake_tools, monkeypatch):
    monkeypatch.setenv("FAKE_WARN_COMPILE", "gfa.cpp")
    facts = recipe.build(fake_tools("linux"), UPSTREAM, tmp_path / "stage" / "tesseract", tmp_path / "out", 4)
    assert facts["warnings"] == {"own": 0, "upstream": 1}


def test_warnings_of_the_port_are_told_from_those_of_upstream(recipe, tmp_path):
    here = recipe.HERE.as_posix()
    log = tmp_path / "build.log"
    log.write_text("\n".join([
        "$ g++ -Wall -c %s/ts_compat.cpp -o x.o   # a command line, not a warning: here" % here,
        "%s/ts_compat.cpp:10:5: warning: unused variable 'x' [-Wunused-variable]" % here,
        "%s:20:1: warning: as a Windows compiler spells the path [-Wsomething]" % (here + "/ts_compat.h").replace("/", "\\"),
        "/stage/src/gfa.cpp:5:1: warning: upstream's own [-Wx]",
        "/stage/src/gfa.cpp:5:1: note: not a warning",
        "",
    ]), encoding="utf-8")
    found = recipe._warnings(log)
    assert len(found["own"]) == 2 and len(found["upstream"]) == 1
    assert recipe._warnings(tmp_path / "absent.log") == {"own": [], "upstream": []}


def test_objects_of_an_earlier_build_do_not_outlive_a_failed_staging(recipe, tmp_path, fake_tools):
    """build_tests() links whatever objects it finds beside the staged tree."""
    tc = fake_tools("linux")
    stage, out = tmp_path / "stage" / "tesseract", tmp_path / "out"
    recipe.build(tc, UPSTREAM, stage, out, 4)
    assert list((recipe.object_dir(stage) / "src").glob("*.o"))
    good = (out / "tesseract-asm").read_bytes()

    changed = copy_of_upstream(tmp_path / "changed")
    target = changed / "src" / "p2_provenance.cpp"
    target.write_text(target.read_text(encoding="utf-8").replace('"/proc/self/exe"', "selfPath()"), encoding="utf-8")
    with pytest.raises(buildlib.BuildError) as caught:
        recipe.build(tc, changed, stage, out, 4)
    assert "R2" in str(caught.value)
    assert not recipe.object_dir(stage).exists()
    with pytest.raises(buildlib.BuildError) as caught:
        recipe.build_tests(tc, stage, 2)
    assert "build the engine first" in str(caught.value)
    assert (out / "tesseract-asm").read_bytes() == good


def test_build_refuses_a_source_that_is_not_the_pinned_release(recipe, tmp_path, fake_tools, monkeypatch):
    monkeypatch.setattr(recipe.versions, "TESSERACT_VERSION", "9.9.9")
    out = tmp_path / "out"
    with pytest.raises(buildlib.BuildError) as caught:
        recipe.build(fake_tools("linux"), UPSTREAM, tmp_path / "stage" / "tesseract", out, 4)
    assert "9.9.9" in str(caught.value) and "1.6.2" in str(caught.value)
    assert not out.exists() or list(out.iterdir()) == []


def test_the_upstream_test_programs_are_built_except_exactly_the_skip_list(recipe, tmp_path, fake_tools):
    tc = fake_tools("windows")
    stage = tmp_path / "stage" / "tesseract"
    recipe.build(tc, UPSTREAM, stage, tmp_path / "out", 4)
    result = recipe.build_tests(tc, stage, 4)
    programs = {p.stem for p in (UPSTREAM / "tests").glob("test_*.cpp")}
    assert result["failed"] == {}
    assert set(result["skipped"]) == set(recipe.read_skip_list())
    assert set(result["built"]) == programs - set(result["skipped"])
    assert len(result["built"]) + len(result["skipped"]) == len(programs)
    for name in result["built"]:
        assert (stage / "build" / (name + ".exe")).is_file()
    for name in result["skipped"]:
        assert not (stage / "build" / (name + ".exe")).exists()
    links = [c for c in commands(tmp_path / "logs" / "tests-windows.log") if "coff" not in c]
    assert len(links) == len(result["built"])
    for link in links:
        objects = [Path(a).name for a in link if a.endswith(".o")]
        # setenv, unsetenv and mkdtemp for the tests; never a second main()
        assert "ts_compat_tests.o" in objects and "dev_fork_batch_win.o" in objects
        assert "main.o" not in objects and "model_main.o" not in objects
        assert "-Wl,--stack,16777216" in link and "-static" in link
    # keep=False is a link check that leaves nothing behind; names= selects
    some = recipe.build_tests(tc, stage, 2, names=["test_units", "test_envflags"], keep=False)
    assert some["built"] == ["test_units"], "test_envflags needs fork(): it is on the skip list"


@pytest.mark.skipif(os.name == "nt", reason="the stand-in test programs are shell scripts")
def test_run_tests_reports_every_way_a_test_program_can_end(recipe, tmp_path, monkeypatch):
    monkeypatch.setattr(recipe, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("TESSERACT_K", "from the user's shell")
    stage = tmp_path / "stage"
    (stage / "build").mkdir(parents=True)
    scripts = {
        "test_passes": 'echo "K=[${TESSERACT_K-unset}] in $(basename "$PWD")"',
        "test_fails": "echo 'non-UTF-8: \\351'; exit 3",
        "test_hangs": "sleep 30",
    }
    for name, body in scripts.items():
        path = stage / "build" / name
        path.write_text("#!/bin/sh\n%s\n" % body, encoding="utf-8")
        path.chmod(0o755)
    (stage / "build" / "test_not_a_program").write_text("text", encoding="utf-8")
    tc = buildlib.Toolchain(target="linux", cxx="g++")
    statuses = recipe.run_tests(tc, stage, list(scripts) + ["test_not_a_program", "test_absent"], timeout=2)
    assert statuses == {"test_passes": 0, "test_fails": 3, "test_hangs": 124, "test_not_a_program": 127,
                        "test_absent": 127}
    log = (tmp_path / "logs" / "tests-run-linux.log").read_text(encoding="utf-8")
    assert "K=[unset] in stage" in log, "TESSERACT_* variables of the caller must not reach a test"
    assert "== test_hangs (exit 124)" in log and "[timed out]" in log


# --------------------------------------------------------------------------------------- the LLP64 rewrite

LLP64_CASES = [
    ("long x = 5L;", "long long x = 5LL;"),
    ("unsigned long y = 7UL;", "unsigned long long y = 7ULL;"),
    ("const long n = static_cast<long>(v.size());", "const long long n = static_cast<long long>(v.size());"),
    ("std::max<long>(a, 0L)", "std::max<long long>(a, 0LL)"),
    ("std::pair<long,long> p; std::map<long, int> m;", "std::pair<long long,long long> p; std::map<long long, int> m;"),
    ("long long z; unsigned long long w; long double d = 1.0L;", None),
    ("long /* how wide */ long v;", None),
    ("int belong = 0; int longest = 1; int mylong;", None),
    ("// long live the comment, 5L\n/* long 5L %ld */", None),
    ('const char* s = "a long story, 5L";', None),
    ('std::printf("%ld %5lu %-3lx %lld %Lf\\n", a, b, c, d, e);', 'std::printf("%lld %5llu %-3llx %lld %Lf\\n", a, b, c, d, e);'),
    ('std::fscanf(fh, "%255s %lu", acc, &cid)', 'std::fscanf(fh, "%255s %llu", acc, &cid)'),
    ("x = std::labs(a) + atol(s) + std::strtol(p, &e, 10) + strtoul(p, 0, 10) + std::lround(f);",
     "x = std::llabs(a) + atoll(s) + std::strtoll(p, &e, 10) + strtoull(p, 0, 10) + std::llround(f);"),
    ("int labs = 3; int lround;", None),
    ("mask = 0x1fL | 0xFFul | 10lu;", "mask = 0x1fLL | 0xFFuLL | 10LLu;"),
    ("double h = 0.5L; float f = 1.f; int k = 10;", None),
    ("char c = 'L'; char q = '\\''; long t;", "char c = 'L'; char q = '\\''; long long t;"),
    # long double literals with an exponent, a hexadecimal fraction, a bare point: never integers
    ("long double a = 1e-3L, b = 2.5e+10L, c = 1E5L, d = 0x1p-3L, e = .5L, f = 1.L;", None),
    # digit separators are not the quotes of a character literal
    ("int n = 1'000; long x = 'a';", "int n = 1'000; long long x = 'a';"),
    ("long big = 1'000'000L + 0b1'0l;", "long long big = 1'000'000LL + 0b1'0LL;"),
    ("mask = 0b11l | 08L | 0xFFLu;", "mask = 0b11LL | 08LL | 0xFFLLu;"),
    # "%%ld" prints a percent sign and the letters ld
    ('std::printf("100%%ld of %ld, %%%ld\\n", n, m);', 'std::printf("100%%ld of %lld, %%%lld\\n", n, m);'),
    # an "R" at the end of a string literal does not start a raw string
    ('s = (end ? "R" : "L") + std::to_string(n); t = "R" + std::to_string(n); u = FOO_R"x("; long q;',
     's = (end ? "R" : "L") + std::to_string(n); t = "R" + std::to_string(n); u = FOO_R"x("; long long q;'),
    # the limits of long and the other C functions that take or return one
    ("long a = LONG_MAX; unsigned long b = ULONG_MAX; if (a < LONG_MIN) {}",
     "long long a = LLONG_MAX; unsigned long long b = ULLONG_MAX; if (a < LLONG_MIN) {}"),
    ("ldiv_t r = std::ldiv(a, b); x = lroundl(v) + lrintl(w);", "lldiv_t r = std::lldiv(a, b); x = llroundl(v) + llrintl(w);"),
    ("x = LLONG_MAX; y = ULLONG_MAX; lldiv_t r = lldiv(a, b); int ldiv = 2;", None),
    # line ends are kept as they are
    ("long a;\r\nlong\r\nlong b;\r\nconst char* s = \"%ld\";\r\n", "long long a;\r\nlong\r\nlong b;\r\nconst char* s = \"%lld\";\r\n"),
]


@pytest.mark.parametrize("source, expected", LLP64_CASES)
def test_llp64_rewrite(recipe, source, expected):
    expected = source if expected is None else expected
    new, counts = recipe.llp64_transform.transform(source)
    assert new == expected
    assert (sum(counts) == 0) == (new == source)
    again, counts_again = recipe.llp64_transform.transform(new)
    assert again == new and sum(counts_again) == 0, "not idempotent"


def test_llp64_rewrite_refuses_raw_strings(recipe):
    with pytest.raises(ValueError):
        recipe.llp64_transform.transform('const char* t = R"(%ld long)";')


@pytest.mark.parametrize("source", [
    'auto t = R"x(long)x";', 'auto t = u8R"(a "quoted" long)";', 'auto t = LR"(long)";',
    'int a;\nauto t = R"(first line\nlong second line)";\nlong b;',
])
def test_llp64_rewrite_refuses_every_spelling_of_a_raw_string(recipe, source):
    with pytest.raises(ValueError) as caught:
        recipe.llp64_transform.transform(source)
    assert "raw string" in str(caught.value)


def test_llp64_rewrite_of_empty_and_very_large_sources(recipe):
    assert recipe.llp64_transform.transform("") == ("", [0, 0, 0, 0])
    line = 'static long v%d = %dL; // long %%ld\nstatic const char* s%d = "%%ld long";\n'
    big = "".join(line % (i, i, i) for i in range(60000))               # about 5 MB
    new, counts = recipe.llp64_transform.transform(big)
    assert counts == [60000, 0, 60000, 60000]
    assert new.count("static long long v") == 60000 and new.count('"%lld long"') == 60000
    assert new.count("// long %ld") == 60000, "comments are left alone"
    assert recipe.llp64_transform.transform(new) == (new, [0, 0, 0, 0])


def test_llp64_rewrite_of_upstream_is_exactly_what_was_audited(recipe):
    """The counts pinned for the release, file by file through the public function."""
    totals = [0, 0, 0, 0]
    changed = 0
    for pattern in recipe.TRANSFORM_GLOBS:
        for path in sorted(UPSTREAM.glob(pattern)):
            with open(path, encoding="utf-8", newline="") as handle:
                text = handle.read()
            new, counts = recipe.llp64_transform.transform(text)
            changed += new != text
            totals = [a + b for a, b in zip(totals, counts)]
    pinned = recipe.LLP64_EXPECTED[versions.TESSERACT_VERSION]
    assert [changed] + totals == [pinned[k] for k in ("files", "type", "func", "literal", "format")]


def test_llp64_rewrite_leaves_no_plain_long_in_the_staged_tree(recipe, tmp_path):
    import re
    staged = tmp_path / "stage"
    recipe.stage(UPSTREAM, staged, "linux")
    plain = re.compile(r"(?<!\w)long\b(?!\s+(?:long|double)\b)")
    for pattern in recipe.TRANSFORM_GLOBS:
        for path in sorted(staged.glob(pattern)):
            masked, _ = recipe.llp64_transform._mask(path.read_text(encoding="utf-8"))
            for match in plain.finditer(masked):
                before = masked[max(0, match.start() - 40):match.start()]
                assert re.search(r"(?<!\w)long\s+$", before), "%s: a 32-bit long is left near %r" % (
                    path.name, masked[max(0, match.start() - 30):match.end() + 10])


# ------------------------------------------------------------------------- the compatibility layer, statically

def test_the_compatibility_header_is_inert_off_windows():
    """Outside Windows the header may define the two macros of rules R1 and R2 and nothing else."""
    text = (RECIPE_DIR / "ts_compat.h").read_text(encoding="utf-8")
    start = text.index("#ifndef _WIN32")
    end = text.index("#else  // _WIN32")
    directives = [line.strip() for line in text[start:end].splitlines()[1:] if line.strip().startswith("#")]
    assert directives == [
        '#define TS_COMPAT_SELF_EXE "/proc/self/exe"',
        '#define TS_COMPAT_CMDLINE_STREAM(var) std::ifstream var("/proc/self/cmdline", std::ios::binary)',
    ]
    code = [line for line in text[start:end].splitlines()[1:] if line.strip() and not line.strip().startswith(("#", "//"))]
    assert code == []
    before = [line for line in text[:start].splitlines() if line.strip() and not line.strip().startswith("//")]
    assert before == ["#pragma once"]


@pytest.mark.skipif(os.name == "nt" or shutil.which("g++") is None, reason="needs g++ on a POSIX system")
def test_the_macros_expand_to_the_upstream_text_on_linux():
    source = 'TS_COMPAT_CMDLINE_STREAM(f);\nbinary_ = digestFile("binary", TS_COMPAT_SELF_EXE, false);\n'
    out = subprocess.run(["g++", "-E", "-P", "-x", "c++", "-include", str(RECIPE_DIR / "ts_compat.h"), "-"],
                         input=source, capture_output=True, text=True, check=True).stdout
    assert out.split() == ('std::ifstream f("/proc/self/cmdline", std::ios::binary);\n'
                           'binary_ = digestFile("binary", "/proc/self/exe", false);').split()


def _syntax_check(recipe, target):
    """Compile the port's own sources for ``target`` without generating code, warnings fatal."""
    import concurrent.futures
    try:
        tc = buildlib.detect_toolchain(target)
    except buildlib.BuildError as exc:
        pytest.skip(str(exc))
    if shutil.which(tc.cxx) is None and not os.path.exists(tc.cxx):
        pytest.skip("no compiler for %s" % target)
    names = list(recipe.COMPAT_SOURCES + recipe.TEST_COMPAT_SOURCES)
    if target == "windows":
        names.append(recipe.FORK_BATCH_WINDOWS)
    # upstream's own headers do: the sources of the port use none of the rewritten declarations
    flags = recipe.cxxflags(tc) + ["-I" + str(UPSTREAM / "src"), "-Werror", "-fsyntax-only"]

    def one(name):
        return name, subprocess.run([tc.cxx, *flags, str(RECIPE_DIR / name)], capture_output=True, text=True, timeout=600)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for name, done in pool.map(one, names):
            assert done.returncode == 0 and not done.stderr.strip(), "%s for %s:\n%s" % (name, target, done.stderr[-3000:])


@pytest.mark.slow
@pytest.mark.skipif(os.name == "nt", reason="the Windows build itself proves it there")
def test_the_compatibility_layer_compiles_for_windows_without_a_warning(recipe):
    """The Windows half cannot be run here, but it must at least stay valid for the cross-compiler."""
    _syntax_check(recipe, "windows")


@pytest.mark.slow
@pytest.mark.skipif(os.name == "nt" or shutil.which("g++") is None, reason="needs g++ on a POSIX system")
def test_the_compatibility_layer_compiles_for_linux_without_a_warning(recipe):
    _syntax_check(recipe, "linux")


def test_the_windows_half_of_the_header_never_includes_windows_h():
    """<windows.h> defines `near`, `far`, `min`, `max` as macros and the engine has variables called `near`."""
    text = (RECIPE_DIR / "ts_compat.h").read_text(encoding="utf-8")
    includes = [line.split()[1] for line in text.splitlines() if line.strip().startswith("#include")]
    assert includes and not [name for name in includes if "windows.h" in name.lower() or "winsock" in name.lower()]
    for stub in ("sys/resource.h", "sys/wait.h"):
        stub_text = (RECIPE_DIR / "include" / stub).read_text(encoding="utf-8")
        assert not [line for line in stub_text.splitlines()
                    if line.strip().startswith("#include") and ("windows.h" in line.lower() or "winsock" in line.lower())]


def test_manifest_and_version_resource(recipe, tmp_path):
    rc = recipe.write_resource(tmp_path, "tesseract-asm", "TesserACT assembler", versions.TESSERACT_VERSION)
    script = rc.read_text(encoding="ascii")
    numbers = ",".join((versions.TESSERACT_VERSION.split(".") + ["0"] * 4)[:4])
    assert '1 24 "tesseract-asm.manifest"' in script
    assert "FILEVERSION %s\n" % numbers in script and "PRODUCTVERSION %s\n" % numbers in script
    assert 'VALUE "FileDescription", "TesserACT assembler\\0"' in script
    assert 'VALUE "CompanyName", "%s\\0"' % branding.VENDOR in script
    assert branding.VENDOR == "IOWA-BioTech"
    assert 'VALUE "FileVersion", "%s\\0"' % versions.TESSERACT_VERSION in script
    assert 'VALUE "ProductVersion", "%s\\0"' % versions.TESSERACT_VERSION in script

    manifest = (tmp_path / "tesseract-asm.manifest").read_bytes()
    assert manifest.startswith(b"<?xml"), "nothing may come before the XML declaration"
    root = ElementTree.fromstring(manifest)
    settings = {el.tag.split("}")[1]: (el.tag.split("}")[0].lstrip("{"), el.text) for el in root.iter()
                if el.tag.endswith(("activeCodePage", "longPathAware"))}
    assert settings["activeCodePage"] == ("http://schemas.microsoft.com/SMI/2019/WindowsSettings", "UTF-8")
    assert settings["longPathAware"] == ("http://schemas.microsoft.com/SMI/2016/WindowsSettings", "true")
    identity = next(el for el in root.iter() if el.tag.endswith("assemblyIdentity"))
    assert identity.get("version") == numbers.replace(",", ".")


def test_the_skip_list_of_the_upstream_cpp_tests(recipe):
    skip = recipe.read_skip_list()
    programs = {p.stem for p in (UPSTREAM / "tests").glob("test_*.cpp")}
    assert set(skip) <= programs, "tests-skip.txt names programs upstream does not have"
    assert FORK_TESTS <= set(skip)
    assert all(reason for reason in skip.values()), "every skipped program states its reason"
    for name in FORK_TESTS:
        text = (UPSTREAM / "tests" / (name + ".cpp")).read_text(encoding="utf-8")
        assert "fork()" in text or "env_reject.h" in text, name
    windows = recipe.test_programs(UPSTREAM, "windows")
    assert set(windows["skip"]) == set(skip)
    assert set(windows["build"]) == programs - set(skip) and "test_units" in windows["build"]
    assert recipe.test_programs(UPSTREAM, "linux")["skip"] == ["test_dev_fork_batch"]
    # Exactly these: a program added to the list needs its reason here as well.
    assert set(skip) == FORK_TESTS | {"test_v3_io_seqio", "test_dev_fork_batch"}
    assert "create_symlink" in (UPSTREAM / "tests" / "test_v3_io_seqio.cpp").read_text(encoding="utf-8")
    assert "dev_fork_batch.cpp" in (UPSTREAM / "tests" / "test_dev_fork_batch.cpp").read_text(encoding="utf-8")


def test_the_skip_list_file_is_read_strictly(recipe, tmp_path):
    lines = [line for line in recipe.TESTS_SKIP.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    names = [line.split()[0] for line in lines]
    assert len(names) == len(set(names)) == len(recipe.read_skip_list()), "a program is listed twice"
    other = tmp_path / "skip.txt"
    other.write_bytes(b"# a comment\r\n\r\ntest_a  needs fork \r\ntest_b\tneeds a shell\r\ntest_c\r\n")
    assert recipe.read_skip_list(other) == {"test_a": "needs fork", "test_b": "needs a shell", "test_c": ""}


@pytest.mark.skipif(not WINDOWS_ASM.is_file(), reason="no cross-built Windows engine in build/engines/windows")
def test_the_windows_program_carries_manifest_and_version(recipe):
    """Static facts of the .exe. It cannot be run here; what it does is proven by CI on Windows."""
    data = WINDOWS_ASM.read_bytes()
    assert data[:2] == b"MZ"
    assert b"<activeCodePage xmlns=\"http://schemas.microsoft.com/SMI/2019/WindowsSettings\">UTF-8</activeCodePage>" in data
    assert b"longPathAware" in data
    for text in ("TesserACT assembler", branding.VENDOR, versions.TESSERACT_VERSION):
        assert text.encode("utf-16-le") in data, text
    assert b"--map-polish is not available in the Windows build" in data
    info = json.loads((WINDOWS_ASM.parent / "BUILD_INFO.json").read_text(encoding="utf-8"))["engines"]["tesseract"]
    assert info["version"] == versions.TESSERACT_VERSION
    assert info["stack_reserve"] >= 16 * 1024 * 1024
    assert all(buildlib.is_system_dll(name) for name in info["dll_imports"]), info["dll_imports"]
    assert info["warnings"]["own"] == 0


# ------------------------------------------------------------------------------- the staged Linux engine

def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TESSERACT_", "TS_COMPAT_"))}
    env.update(extra)
    return env


def _built_from_current_sources(recipe, folder: Path, binary: Path, command: str):
    info_path = folder / "BUILD_INFO.json"
    assert info_path.is_file(), "%s has no BUILD_INFO.json: build it with %s" % (folder, command)
    info = json.loads(info_path.read_text(encoding="utf-8"))["engines"].get("tesseract")
    assert info, "BUILD_INFO.json does not describe tesseract: build it with %s" % command
    stale = "%s was not built from the sources as they are now; rebuild it with: %s" % (binary.name, command)
    assert info["sha256"] == buildlib.sha256(binary), stale
    assert info["upstream_tree_sha256"] == buildlib.tree_sha256(UPSTREAM), stale
    assert info.get("port_sources_sha256") == recipe.port_sources_sha256(), stale
    assert info["warnings"] == {"own": 0, "upstream": 0}
    return info


@staged_engine
def test_the_staged_engine_was_built_from_the_sources_as_they_are_now(recipe):
    """A program left over from before a change to the layer would prove nothing below."""
    info = _built_from_current_sources(
        recipe, STAGED_DIR, STAGED_ASM,
        "python3 engines/build.py --target linux --engine tesseract --jobs 4 --out build/engines/staged-linux")
    assert info["new_libc_imports"] == []


@pytest.mark.skipif(not WINDOWS_ASM.is_file(), reason="no cross-built Windows engine in build/engines/windows")
def test_the_windows_engine_was_built_from_the_sources_as_they_are_now(recipe):
    _built_from_current_sources(recipe, WINDOWS_ASM.parent, WINDOWS_ASM,
                                "python3 engines/build.py --target windows --engine tesseract --jobs 4")


@staged_engine
def test_staged_engine_answers_version():
    out = subprocess.run([str(STAGED_ASM), "--version"], env=clean_env(), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert out.stdout.splitlines()[0] == "TesserACT %s" % versions.TESSERACT_VERSION
    # Anything but exactly "1" leaves the self-test off.
    off = subprocess.run([str(STAGED_ASM), "--version"], env=clean_env(TS_COMPAT_SELFTEST="0"),
                         capture_output=True, text=True, timeout=60)
    assert off.returncode == 0 and off.stdout == out.stdout


@staged_engine
def test_staged_engine_passes_its_self_test(tmp_path):
    work = tmp_path / "self test"
    work.mkdir()
    out = subprocess.run([str(STAGED_ASM), "*.fastq", "two words", "--version"],
                         env=clean_env(TS_COMPAT_SELFTEST="1", TS_COMPAT_SELFTEST_DIR=str(work)),
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    assert all("=" in line for line in lines), "every line is key=value"
    probes = dict(line.split("=", 1) for line in lines)
    assert len(probes) == len(lines), "a probe is printed twice"
    assert "TesserACT" not in out.stdout, "the self-test must run instead of the program"
    expected = {
        "platform": "linux", "sizeof_long": "8", "sizeof_long_long": "8",
        "same_file_two_files": "0", "same_file_itself": "1", "same_file_other_spelling": "1",
        "stat_size_over_4g": str((1 << 32) + 4097),
        "fopen_w_newline_bytes": "2", "ofstream_newline_bytes": "2",
        "printf_4f_24.86445": "24.8645", "printf_4f_0.00005": "0.0001",
        "printf_0f_0.5": "0", "printf_0f_1.5": "2", "printf_0f_2.5": "2",
        "memory_current_gt0": "1", "memory_peak_gt0": "1", "memory_total_gt0": "1",
        "self_exe_exists": "1",
        "unicode_dir_create": "1", "unicode_fopen_roundtrip": "1",
        "argv_tail": "*.fastq|two words|--version",
        # 4,096 levels of recursion on a worker thread, as the gap filler does
        "thread_stack_4m": "1",
        "selftest": "pass",
    }
    for key, value in expected.items():
        assert probes.get(key) == value, "%s=%r, expected %r" % (key, probes.get(key), value)
    assert lines[-1] == "selftest=pass"
    assert list(work.iterdir()) == [], "the self-test left files behind"


def run_self_test(work: Path, *args, **more):
    return subprocess.run([str(STAGED_ASM), *args],
                          env=clean_env(TS_COMPAT_SELFTEST="1", TS_COMPAT_SELFTEST_DIR=str(work)),
                          timeout=300, **more)


@staged_engine
def test_the_self_test_exits_1_when_a_probe_fails(tmp_path):
    """A work folder that cannot be made: one probe fails, the rest still report, the status is 1."""
    blocker = tmp_path / "a file, not a folder"
    blocker.write_text("x", encoding="utf-8")
    out = run_self_test(blocker, "--version", capture_output=True, text=True)
    assert out.returncode == 1, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    assert all("=" in line for line in lines)
    assert "work_dir=0" in lines and lines[-1] == "selftest=fail"
    assert "printf_0f_2.5=2" in lines, "the probes before the failure are still reported"
    assert "FAILED work_dir=0" in out.stderr
    assert "TesserACT" not in out.stdout, "the program itself must not run"
    assert blocker.read_text(encoding="utf-8") == "x"


@staged_engine
@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="needs /dev/full")
def test_the_self_test_does_not_pass_when_its_report_cannot_be_written(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with open("/dev/full", "wb") as full:
        out = run_self_test(work, stdout=full, stderr=subprocess.PIPE)
    assert out.returncode == 1
    assert list(work.iterdir()) == []


@staged_engine
def test_self_tests_running_at_the_same_time_in_one_folder(tmp_path):
    work = tmp_path / "shared é"
    work.mkdir()
    env = clean_env(TS_COMPAT_SELFTEST="1", TS_COMPAT_SELFTEST_DIR=str(work))
    running = [subprocess.Popen([str(STAGED_ASM)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for _ in range(3)]
    for proc in running:
        stdout, stderr = proc.communicate(timeout=300)
        assert proc.returncode == 0, stdout + stderr
        assert stdout.splitlines()[-1] == "selftest=pass"
    assert list(work.iterdir()) == [], "each self-test cleans up its own folder"


def files_deleted_while_open(program: Path, work: Path, trace: Path):
    """Run the self-test of ``program`` under strace. Returns (deleted, deleted_while_open): the
    files under ``work`` it deleted, and those of them it still had open at that moment."""
    import re
    done = subprocess.run(["strace", "-f", "-o", str(trace), "-e", "trace=open,openat,creat,close,unlink,unlinkat",
                           str(program)], env=clean_env(TS_COMPAT_SELFTEST="1", TS_COMPAT_SELFTEST_DIR=str(work)),
                          capture_output=True, text=True, timeout=600)
    if not trace.is_file() or "ptrace" in done.stderr or "Operation not permitted" in done.stderr:
        pytest.skip("strace cannot follow a program here: %s" % done.stderr.strip()[-200:])
    assert done.returncode == 0, done.stdout + done.stderr
    opened = re.compile(r'^\d+\s+(?:openat\(AT_FDCWD, |open\(|creat\()"((?:[^"\\]|\\.)*)".*\)\s+=\s+(\d+)\s*$')
    closed = re.compile(r'^\d+\s+close\((\d+)\)\s+=\s+0')
    removed = re.compile(r'^\d+\s+(?:unlinkat\(AT_FDCWD, |unlink\()"((?:[^"\\]|\\.)*)".*\)\s+=\s+0')
    marker = "ts-compat-selftest-"
    open_now = {}                    # descriptor -> path (all threads of a process share the table)
    deleted, deleted_while_open = [], []
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        if marker in line:
            assert "unfinished" not in line and "resumed" not in line, "cannot follow this trace: " + line
        match = opened.match(line)
        if match:
            open_now[match.group(2)] = match.group(1)
            continue
        match = closed.match(line)
        if match:
            open_now.pop(match.group(1), None)
            continue
        match = removed.match(line)
        if match and marker in match.group(1):
            deleted.append(match.group(1))
            if match.group(1) in open_now.values():
                deleted_while_open.append(match.group(1))
    return deleted, deleted_while_open


@staged_engine
@pytest.mark.skipif(os.name == "nt" or shutil.which("strace") is None, reason="needs strace")
def test_the_self_test_never_deletes_a_file_it_still_has_open(tmp_path):
    """Linux deletes an open file and Windows refuses. The first run on Windows failed its
    `cleanup` probe for exactly that (a std::ifstream still open on mode_read.txt), which no run
    on Linux can show; so the rule is checked here, system call by system call."""
    work = tmp_path / "work"
    work.mkdir()
    deleted, deleted_while_open = files_deleted_while_open(STAGED_ASM, work, tmp_path / "trace.txt")
    names = {path.rsplit("/", 1)[-1] for path in deleted}
    assert {"a.txt", "b.txt", "sparse.bin", "mode_w.txt", "mode_ofstream.txt", "mode_read.txt", "cov.txt"} <= names, names
    assert len(deleted) >= 9, "the trace does not show the self-test's files: the test is out of date"
    assert deleted_while_open == []
    assert list(work.iterdir()) == []


@staged_engine
@pytest.mark.skipif(os.name == "nt", reason="POSIX resource limits")
@pytest.mark.parametrize("limit", [2 << 20, "unlimited"], ids=["ulimit -s 2048", "ulimit -s unlimited"])
def test_the_self_test_gives_the_same_answers_whatever_the_stack_limit_of_the_shell(tmp_path, limit):
    """Its output is compared with the Windows one line by line, so it must not depend on `ulimit -s`."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
    if limit == "unlimited":
        if hard != resource.RLIM_INFINITY:
            pytest.skip("the stack limit cannot be raised here")
        wanted = resource.RLIM_INFINITY
    else:
        wanted = limit
    work = tmp_path / "work"
    work.mkdir()
    out = run_self_test(work, capture_output=True, text=True,
                        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_STACK, (wanted, hard)))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "thread_stack_4m=1" in out.stdout.splitlines() and out.stdout.splitlines()[-1] == "selftest=pass"


@staged_engine
def test_the_model_builder_and_the_engine_carry_the_same_self_test(tmp_path):
    """Every program of the port links the layer, so CI can ask any of them."""
    model = REPO / "build" / "stage" / "linux" / "tesseract" / "tesseract-model"
    if not model.is_file():
        pytest.skip("no staged tree with tesseract-model")
    work = tmp_path / "work"
    work.mkdir()
    out = subprocess.run([str(model)], env=clean_env(TS_COMPAT_SELFTEST="1", TS_COMPAT_SELFTEST_DIR=str(work)),
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0 and out.stdout.splitlines()[-1] == "selftest=pass", out.stdout + out.stderr


@staged_engine
@pytest.mark.skipif(shutil.which("nm") is None, reason="needs nm")
def test_new_c_library_imports_can_be_listed(recipe):
    """Informative on purpose: the imported names depend on the C library the engine was built with."""
    new = recipe.new_libc_imports(STAGED_ASM)
    assert isinstance(new, list)
    known = {line.strip() for line in recipe.LIBC_IMPORTS.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")}
    assert len(known) > 100 and not set(new) & known
    assert recipe.new_libc_imports(STAGED_ASM.parent / "absent") is None


def gen(*args):
    subprocess.run([sys.executable, str(GEN), *[str(a) for a in args]], check=True, capture_output=True, timeout=600)


def profile_env(sketch: Path):
    """The environment the wrapper gives the engine: nine profile variables and the two of detection."""
    return clean_env(TESSERACT_P2_DETECT_REPORT="1", TESSERACT_P2_DETECT_SKETCH=str(sketch), **PROFILE)


def assemble(binary: Path, reads, out: Path, sketch: Path):
    out.mkdir(parents=True)
    with open(out / "assembly.log", "wb") as log:
        done = subprocess.run([str(binary), "-1", str(reads[0]), "-2", str(reads[1]), "-o", str(out), "-t", "4"],
                              env=profile_env(sketch), stdout=log, stderr=subprocess.STDOUT, timeout=900)
    assert done.returncode == 0, (out / "assembly.log").read_text(errors="replace")[-2000:]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: Files whose bytes do not depend on the run (no paths, times or memory figures).
DETERMINISTIC = ("contigs.fasta", "assembly_graph.gfa", "scaffolds.agp", "scaffolds.fasta", "ends.tsv",
                 "p2_edits.tsv", "replicons.tsv", "p2_self_qa.tsv", "p2_self_qa.contigs.tsv")


@staged_engine
@pytest.mark.slow
@pytest.mark.skipif(not (REFERENCE_DIR / "tesseract-asm").is_file(), reason="no reference engine to compare with")
def test_staged_engine_writes_the_same_bytes_as_the_reference(tmp_path):
    """Three small read sets, wrapper profile, 4 threads, one run at a time."""
    data = tmp_path / "data"
    data.mkdir()
    sketch = REFERENCE_DIR / "om2detect.sketch"
    # 1: a circular chromosome, a 5 kb plasmid at 10 copies, a 3 kb plasmid at 25 copies, phiX
    gen("circgenome", data / "p.fa", 21, 600, UPSTREAM / "src" / "p2_phix.h", "90000:1", "5000:10", "3000:25")
    gen("reads", data / "p.fa", data / "p", 45, 150, 400, 40, 0.003, 22, "paired", "fastq", "gz")
    # 2: three replicons, 2x250, 25x, 1 % errors, plain FASTQ
    gen("genome", data / "w.fa", 41, 90000, 30000, 12000)
    gen("reads", data / "w.fa", data / "w", 25, 250, 550, 60, 0.01, 42, "paired", "fastq", "plain")
    # 3: upstream's own scaffold-gap fixture (no read covers 200 bp; 600 bp fragments span it)
    gen("genome", data / "g.fa", 7, 40000)
    gen("holereads", data / "g.fa", data / "g", 60, 150, 600, 40, 1007, 20000, 20200)
    sets = {
        "plasmids": (data / "p_1.fq.gz", data / "p_2.fq.gz"),
        "2x250": (data / "w_1.fq", data / "w_2.fq"),
        "scaffold_gap": (data / "g_1.fq.gz", data / "g_2.fq.gz"),
    }
    agp_seen = False
    for name, reads in sets.items():
        ours, theirs = tmp_path / (name + ".staged"), tmp_path / (name + ".reference")
        assemble(STAGED_ASM, reads, ours, sketch)
        assemble(REFERENCE_DIR / "tesseract-asm", reads, theirs, sketch)
        assert sorted(p.name for p in ours.iterdir()) == sorted(p.name for p in theirs.iterdir()), name
        assert (ours / "contigs.fasta").stat().st_size > 30000, name
        for filename in DETERMINISTIC:
            if not (theirs / filename).exists():
                assert filename not in ("contigs.fasta", "assembly_graph.gfa"), "%s: %s is missing" % (name, filename)
                continue
            assert digest(ours / filename) == digest(theirs / filename), "%s: %s differs" % (name, filename)
            agp_seen = agp_seen or filename == "scaffolds.agp"
    assert agp_seen, "no read set made the engine write scaffolds.agp"
