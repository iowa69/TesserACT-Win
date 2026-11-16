# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The scepter port: staging, its guards, the generated Windows resources, and the built program.

Staging tests need only the pinned source. The tests of the built program run when it
exists (``build/engines/staged-linux/scepter`` on Linux, ``build/engines/windows/scepter.exe``
on Windows) and are skipped otherwise.
"""

from __future__ import annotations

import gzip
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.engines

REPO = Path(__file__).resolve().parent.parent
ENGINES = REPO / "engines"
PORT = ENGINES / "win" / "scepter"
UPSTREAM = ENGINES / "scepter" / "upstream"

if str(ENGINES) not in sys.path:
    sys.path.insert(0, str(ENGINES))
import buildlib  # noqa: E402

from tesseract_win import branding, versions  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


recipe = _load("recipe_scepter_under_test", PORT / "recipe.py")

HEADER = ("SPDX-License-Identifier: MIT", "Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin")
#: Lines of scepter 1.1.0 that a person read and found safe with a 32-bit long: the five
#: lround lines of src/stats.cpp and, since the guard also covers zlib's long, the one
#: gzoffset line of src/fastqio.cpp.
REVIEWED_LINES = 6


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def small_upstream(tmp_path_factory) -> Path:
    """A private copy of the pinned source without its bulky data, to be changed by tests."""
    copy = tmp_path_factory.mktemp("scepter-src") / "upstream"
    shutil.copytree(UPSTREAM, copy, ignore=shutil.ignore_patterns("data", "tests", "tools", "conda", ".git"))
    return copy


@pytest.fixture(scope="module")
def stage_dir(tmp_path_factory) -> Path:
    """One stage folder for all the guard tests (each staging replaces it): little disk."""
    return tmp_path_factory.mktemp("scepter-stage") / "stage"


class Changed:
    """Change one file of the private copy for the length of a ``with`` block."""

    def __init__(self, tree: Path, rel: str, *, append: str = "", replace=None):
        self.path = tree / rel
        self.append, self.replace = append, replace

    def __enter__(self):
        self.original = self.path.read_bytes()
        text = self.original.decode("utf-8")
        if self.replace is not None:
            old, new = self.replace
            assert text.count(old) == 1, "the test's own anchor text must be found once"
            text = text.replace(old, new)
        self.path.write_bytes((text + self.append).encode("utf-8"))
        return self

    def __exit__(self, *exc):
        self.path.write_bytes(self.original)
        return False


def _built_program() -> Path:
    if os.name == "nt":
        return REPO / "build" / "engines" / "windows" / "scepter.exe"
    return REPO / "build" / "engines" / "staged-linux" / "scepter"


@pytest.fixture(scope="module")
def program() -> Path:
    path = _built_program()
    if not path.is_file():
        pytest.skip("no built scepter at %s (run engines/build.py --engine scepter)" % path)
    return path


def _run(program: Path, args, *, env=None, cwd=None):
    full = dict(os.environ)
    full.pop("SCEPTER_PORT_SELFTEST", None)
    full.update(env or {})
    return subprocess.run([str(program), *args], env=full, cwd=cwd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)


# ---------------------------------------------------------------------------------------
# the recipe's interface
# ---------------------------------------------------------------------------------------

def test_recipe_interface():
    assert recipe.NAME == "scepter"
    assert recipe.BINARY == "scepter"
    assert list(inspect.signature(recipe.stage).parameters) == ["upstream", "stage", "target"]
    assert list(inspect.signature(recipe.build).parameters) == ["tc", "upstream", "stage", "out_dir", "jobs"]


def test_every_port_file_starts_with_the_two_header_lines():
    files = [p for p in sorted(PORT.iterdir()) if p.is_file()] + [Path(__file__)]
    assert {p.name for p in files} >= {"recipe.py", "transform.py", "scepter_port.h", "scepter_port.cpp",
                                       "scepter_selftest.cpp"}
    for path in files:
        lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        if lines[0].startswith("#!"):
            lines = lines[1:]
        mark = "// " if path.suffix in (".h", ".cpp") else "# "
        assert lines[0] == mark + HEADER[0], path.name
        assert lines[1] == mark + HEADER[1], path.name


# ---------------------------------------------------------------------------------------
# staging: upstream untouched, idempotent, one reviewed change
# ---------------------------------------------------------------------------------------

def test_staging_leaves_upstream_untouched_and_is_idempotent(tmp_path):
    before = buildlib.tree_sha256(UPSTREAM)
    stage = tmp_path / "stage"
    try:
        first = recipe.stage(UPSTREAM, stage, "linux")
        assert buildlib.tree_sha256(UPSTREAM) == before == first["upstream_sha256"]
        assert first["version"] == versions.SCEPTER_VERSION
        assert first["transformations"]["T1-fseek64"] == {
            "file": "src/fastqio.cpp", "expected": 1, "replaced": 1, "present": 1}
        staged = buildlib.tree_sha256(stage)
        assert staged == first["staged_sha256"] != before

        # the scripted part again, on the tree it already changed: nothing left to do
        again = recipe.transform(stage)
        assert again["replaced"] == 0 and again["written"] == []
        assert again["transformations"]["T1-fseek64"]["present"] == 1
        assert buildlib.tree_sha256(stage) == staged

        # a fresh staging gives the same tree, for either target
        assert recipe.stage(UPSTREAM, stage, "windows")["staged_sha256"] == staged
        assert buildlib.tree_sha256(UPSTREAM) == before

        # what differs from upstream: one line of one file, and the added folder
        changed, added = [], []
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            rel = path.relative_to(stage)
            if not (UPSTREAM / rel).exists():
                added.append(rel.as_posix())
            elif path.read_bytes() != (UPSTREAM / rel).read_bytes():
                changed.append(rel.as_posix())
        assert changed == ["src/fastqio.cpp"]
        assert all(name.startswith(recipe.PORT_DIR + "/") for name in added)
        old = (UPSTREAM / "src/fastqio.cpp").read_text(encoding="utf-8").split("\n")
        new = (stage / "src/fastqio.cpp").read_text(encoding="utf-8").split("\n")
        assert len(old) == len(new)
        different = [(a, b) for a, b in zip(old, new) if a != b]
        assert len(different) == 1
        assert "std::fseek(fh_, static_cast<long>(offset), SEEK_SET)" in different[0][0]
        assert "scepter_fseek64(fh_, offset, SEEK_SET)" in different[0][1]
        for name in recipe.PORT_SOURCES:
            assert (stage / recipe.PORT_DIR / name).read_bytes() == (PORT / name).read_bytes()
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def test_staging_never_writes_into_the_source(stage_dir, small_upstream):
    before = buildlib.tree_sha256(small_upstream)
    for bad in (small_upstream, small_upstream / "staged", small_upstream.parent):
        with pytest.raises(buildlib.BuildError, match="outside the source"):
            recipe.stage(small_upstream, bad, "linux")
    with pytest.raises(buildlib.BuildError, match="unknown target"):
        recipe.stage(small_upstream, stage_dir, "macos")
    # the transformation script itself refuses a tree the recipe did not stage ...
    rules = recipe.transform_rules
    with pytest.raises(rules.TransformError, match="is not a staged copy"):
        rules.apply(small_upstream, port_header=PORT / "scepter_port.h", port_source=PORT / "scepter_port.cpp")
    assert rules.main([str(small_upstream)]) == 1
    # ... but will look at it and say what it would do
    assert rules.main(["--check", str(small_upstream)]) == 0
    counts = rules.apply(small_upstream, port_header=PORT / "scepter_port.h",
                         port_source=PORT / "scepter_port.cpp", check=True)
    assert counts["replaced"] == 1 and counts["written"] == []
    assert buildlib.tree_sha256(small_upstream) == before


def test_recipe_transform_refuses_a_tree_that_stage_did_not_make(small_upstream):
    """Found in review: recipe.transform() marked any tree as staged and then rewrote it,
    so one wrong call would have edited the pinned upstream source."""
    before = buildlib.tree_sha256(small_upstream)
    with pytest.raises(buildlib.BuildError, match="is not a staged copy"):
        recipe.transform(small_upstream)
    assert buildlib.tree_sha256(small_upstream) == before
    assert not (small_upstream / recipe.PORT_DIR).exists()


def test_staging_deletes_only_an_earlier_stage(tmp_path, small_upstream):
    """Found in review: staging replaces the stage folder, whatever folder it was given."""
    theirs = tmp_path / "documents"                       # somebody's folder, given by mistake
    (theirs / "src").mkdir(parents=True)
    (theirs / "thesis.txt").write_text("three years\n", encoding="utf-8")
    with pytest.raises(buildlib.BuildError, match=r"not an earlier stage.*thesis\.txt.*will not be deleted"):
        recipe.stage(small_upstream, theirs, "linux")
    assert (theirs / "thesis.txt").read_text(encoding="utf-8") == "three years\n"
    a_file = tmp_path / "stage.txt"
    a_file.write_text("kept", encoding="utf-8")
    with pytest.raises(buildlib.BuildError, match="is a file"):
        recipe.stage(small_upstream, a_file, "linux")
    assert a_file.read_text(encoding="utf-8") == "kept"

    # An empty folder is fine, and so is a name with a space, an accent, brackets and kanji.
    stage = tmp_path / "stage \u00e9 [1] \u65e5\u672c"
    stage.mkdir()
    first = recipe.stage(small_upstream, stage, "linux")
    # A staging that was cut short (no marker yet, part of the source) is replaced ...
    shutil.rmtree(stage / recipe.PORT_DIR)
    (stage / "src" / "cli.cpp").unlink()
    assert recipe.stage(small_upstream, stage, "windows")["staged_sha256"] == first["staged_sha256"]
    # ... and a complete earlier stage is replaced whatever was left in it.
    (stage / "left-over.o").write_text("", encoding="utf-8")
    assert recipe.stage(small_upstream, stage, "linux")["staged_sha256"] == first["staged_sha256"]
    assert not (stage / "left-over.o").exists()


def test_a_source_checked_out_with_windows_line_ends_still_stages(tmp_path, small_upstream):
    copy = tmp_path / "crlf"
    shutil.copytree(small_upstream, copy)
    for path in list((copy / "src").iterdir()) + [copy / "Makefile"]:
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    counts = recipe.stage(copy, tmp_path / "stage", "linux")
    assert counts["replaced"] == 1 and counts["reviewed_lines"] == REVIEWED_LINES
    staged = (tmp_path / "stage" / "src" / "fastqio.cpp").read_bytes()
    assert b"scepter_fseek64(fh_, offset, SEEK_SET)" in staged
    assert staged.count(b"\r\n") == staged.count(b"\n")          # line ends left as they were


# ---------------------------------------------------------------------------------------
# guards: a new upstream version that needs a person's eye stops the build
# ---------------------------------------------------------------------------------------

NEW_CODE = [
    ("a plain long", "long extraCounter = 0;", "a plain long"),
    ("unsigned long", "unsigned long extraMask = 0;", "a plain long"),
    ("ftell", "long long whereAmI(std::FILE* f) { return std::ftell(f); }", "a call that takes or returns long"),
    ("fseek", "void rewindTo(std::FILE* f) { std::fseek(f, 0, SEEK_SET); }", "a call that takes or returns long"),
    ("strtol", "int parseIt(const char* s) { return static_cast<int>(std::strtol(s, nullptr, 10)); }",
     "a call that takes or returns long"),
    ("strtoul", "auto parseU(const char* s) { return std::strtoul(s, nullptr, 10); }",
     "a call that takes or returns long"),
    ("lround", "int roundIt(double v) { return static_cast<int>(std::lround(v)); }",
     "a call that takes or returns long"),
    ("L literal", "auto fourGb = 4294967296L;", "a literal with an L or UL suffix"),
    ("UL literal", "auto mask = 0xFFFFFFFFUL;", "a literal with an L or UL suffix"),
    ("%ld", 'void show(long long n) { std::printf("%ld reads\\n", n); }', "a %ld-style format"),
    ("%lu", 'void showU(size_t n) { std::printf("%8lu reads\\n", n); }', "a %ld-style format"),
    ("off_t", "off_t whereTo = 0;", "off_t"),
    # found in review: the same functions and types by other routes
    ("strtol by name", "auto kParser = &std::strtol;", "a call that takes or returns long"),
    ("gzseek", "int rewindGz(gzFile g) { return gzseek(g, 0, SEEK_SET) < 0; }", "zlib's long"),
    ("gztell", "long long whereGz(gzFile g) { return gztell(g); }", "zlib's long"),
    ("z_off_t", "z_off_t kOffset = 0;", "zlib's long"),
    ("uLong", "uLong kChecksum = 0;", "zlib's long"),
    ("total_out", "unsigned long long written(z_stream& z) { return z.total_out; }", "zlib's long"),
    ("LONG_MAX", "long long kCap = LONG_MAX;", "a limit of long"),
    ("ULONG_MAX", "unsigned long long kMaskAll = ULONG_MAX;", "a limit of long"),
]


@pytest.mark.parametrize("label,code,reason", NEW_CODE, ids=[c[0] for c in NEW_CODE])
def test_new_code_that_depends_on_the_width_of_long_fails_staging(stage_dir, small_upstream, label, code, reason):
    with Changed(small_upstream, "src/util.cpp", append="\n" + code + "\n"):
        with pytest.raises(buildlib.BuildError) as caught:
            recipe.stage(small_upstream, stage_dir, "linux")
    message = str(caught.value)
    assert "G1: src/util.cpp:" in message        # where
    assert code in message                       # the line itself
    assert reason in message                     # why
    assert "1 problem(s)" in message


def test_lookalikes_do_not_fail_staging(stage_dir, small_upstream):
    harmless = "\n".join([
        "// a long comment about a long read, with 10L and %ld and std::ftell(f) in it",
        "/* long block comment: strtol(x), 5UL */",
        'const char* kLongWord = "long reads take long; fseek(";',
        'const char* kRaw = R"X(a long "quoted" line',
        "still inside the raw string: long x = 7L; )X\";",
        "long long kBig = 100000000LL; unsigned long long kMask = 0xFFull;",
        "int belong = 0, longest = 1'000'000, tell_l = 'L';",
        'void showAll(long long n) { std::printf("%lld %llu %zu %.6g\\n", n, 1ull, sizeof(n), 0.5); }',
        "long long widened(double v) { return std::llround(v); }",
        "long long kCap2 = LLONG_MAX; unsigned long long kCap3 = ULLONG_MAX;",
        "int total_output = 0, gztellx = 0, my_z_off_t_like = 0, uLongest = 0;  // gzseek(g) total_out",
        "",
    ])
    with Changed(small_upstream, "src/util.cpp", append="\n" + harmless):
        counts = recipe.stage(small_upstream, stage_dir, "linux")
    assert counts["reviewed_lines"] == REVIEWED_LINES


def test_a_changed_seek_site_fails_staging(stage_dir, small_upstream):
    site = "std::fseek(fh_, static_cast<long>(offset), SEEK_SET)"
    # upstream rewrote the line
    with Changed(small_upstream, "src/fastqio.cpp", replace=(site, "fseeko(fh_, offset, SEEK_SET)")):
        with pytest.raises(buildlib.BuildError, match="T1-fseek64.*exactly 1 time"):
            recipe.stage(small_upstream, stage_dir, "linux")
    # upstream added a second one
    again = "\nnamespace { bool again(std::FILE* fh_, long long offset) { return %s == 0; } }\n" % site
    with Changed(small_upstream, "src/fastqio.cpp", append=again):
        with pytest.raises(buildlib.BuildError, match="T1-fseek64.*found 2 and 0"):
            recipe.stage(small_upstream, stage_dir, "linux")


def test_a_reviewed_line_that_changed_fails_staging(stage_dir, small_upstream):
    with Changed(small_upstream, "src/stats.cpp",
                 replace=("long v = std::lround(p);", "long v = std::lround(p * scale);")):
        with pytest.raises(buildlib.BuildError) as caught:
            recipe.stage(small_upstream, stage_dir, "linux")
    message = str(caught.value)
    assert "long v = std::lround(p * scale);" in message          # the new line is shown
    assert "must be found 1 time(s), found 0" in message          # and the stale review is named


def test_a_new_system_header_fails_staging(stage_dir, small_upstream):
    with Changed(small_upstream, "src/util.h",
                 replace=("#include <vector>", "#include <vector>\n#include <filesystem>")):
        with pytest.raises(buildlib.BuildError, match=r"G2: src/util\.h includes <filesystem>"):
            recipe.stage(small_upstream, stage_dir, "linux")


def test_a_new_free_text_option_fails_staging(stage_dir, small_upstream):
    row = '{\'\\0\', "umi_prefix", Kind::Value, "STR", kCatUmi,'
    with Changed(small_upstream, "src/cli.cpp",
                 replace=(row, '{\'\\0\', "sample_label", Kind::Value, "STR", kCatUmi, "label", false, false},'
                               '\n    ' + row)):
        with pytest.raises(buildlib.BuildError, match="G3: .*sample_label"):
            recipe.stage(small_upstream, stage_dir, "linux")
    with Changed(small_upstream, "src/cli.cpp", replace=(row, row.replace('"STR"', '"TEXT"'))):
        with pytest.raises(buildlib.BuildError, match="G3: .*new kind of option value 'TEXT'"):
            recipe.stage(small_upstream, stage_dir, "linux")


def test_a_version_that_is_not_the_pinned_one_fails_staging(stage_dir, small_upstream):
    pinned = 'kScepterVersion = "%s"' % versions.SCEPTER_VERSION
    with Changed(small_upstream, "src/cli.h", replace=(pinned, 'kScepterVersion = "9.9.9"')):
        with pytest.raises(buildlib.BuildError, match=r"scepter 9\.9\.9 but .* pins SCEPTER_VERSION"):
            recipe.stage(small_upstream, stage_dir, "linux")


def test_a_port_folder_in_upstream_is_not_overwritten(stage_dir, small_upstream):
    theirs = small_upstream / recipe.PORT_DIR
    theirs.mkdir()
    try:
        (theirs / "notes.txt").write_text("upstream's own\n", encoding="utf-8")
        with pytest.raises(buildlib.BuildError, match="its own port/ folder"):
            recipe.stage(small_upstream, stage_dir, "linux")
    finally:
        shutil.rmtree(theirs)


def test_source_the_guards_would_not_read_fails_staging(stage_dir, small_upstream):
    """Found in review: only src/*.cpp and src/*.h are read, so a `long` in a sub-folder of
    src/, in a file of another kind or in a header outside src/ went through unseen."""
    hidden = "inline long hiddenValue() { return 1L; }\n"
    extra = small_upstream / "src" / "extra"
    extra.mkdir()
    try:
        (extra / "helper.h").write_text(hidden, encoding="utf-8")
        with pytest.raises(buildlib.BuildError, match=r"G5: src/extra/ is a folder inside src/"):
            recipe.stage(small_upstream, stage_dir, "linux")
    finally:
        shutil.rmtree(extra)
    for name in ("helper.hpp", "tables.inc", "extra.cc", "legacy.c"):
        other = small_upstream / "src" / name
        other.write_text(hidden, encoding="utf-8")
        try:
            with pytest.raises(buildlib.BuildError,
                               match=r"G5: src/%s is not one of the files the guards read" % re.escape(name)):
                recipe.stage(small_upstream, stage_dir, "linux")
        finally:
            other.unlink()
    outside = small_upstream / "third_party"
    outside.mkdir()
    try:
        (outside / "kx.h").write_text(hidden, encoding="utf-8")
        with Changed(small_upstream, "src/util.cpp", append='\n#include "../third_party/kx.h"\n'):
            with pytest.raises(buildlib.BuildError,
                               match=r'G5: src/util\.cpp:\d+ includes "\.\./third_party/kx\.h"'):
                recipe.stage(small_upstream, stage_dir, "linux")
        # an include that is only a comment, and one of a file the guards do read, are fine
        with Changed(small_upstream, "src/util.cpp",
                     append='\n// #include "../third_party/kx.h"\n#include "./common.h"\n'):
            recipe.stage(small_upstream, stage_dir, "linux")
    finally:
        shutil.rmtree(outside)


MAKEFILE_CHANGES = [
    ("language level", ("CXXSTD    := -std=c++17", "CXXSTD    := -std=c++20"), "",
     "must hold `CXXSTD := -std=c++17` exactly once, found 0"),
    ("more sources", ("SOURCES   := $(wildcard $(SRCDIR)/*.cpp)",
                      "SOURCES   := $(wildcard $(SRCDIR)/*.cpp $(SRCDIR)/io/*.cpp)"), "",
     "must hold `SOURCES := $(wildcard $(SRCDIR)/*.cpp)` exactly once, found 0"),
    ("a define", None, "\nCXXFLAGS += -DSCEPTER_NEW_THING\n", "now also says `CXXFLAGS += -DSCEPTER_NEW_THING`"),
    ("a library", None, "\nLDLIBS  +=  -lzstd\n", "now also says `LDLIBS += -lzstd`"),
]


@pytest.mark.parametrize("label,replace,append,expected", MAKEFILE_CHANGES, ids=[c[0] for c in MAKEFILE_CHANGES])
def test_a_change_in_how_upstream_builds_fails_staging(stage_dir, small_upstream, label, replace, append, expected):
    """The recipe compiles src/*.cpp with its own flags instead of running upstream's Makefile:
    a release that builds differently (a define, a library, more sources) must not go by unseen."""
    with Changed(small_upstream, "Makefile", append=append, replace=replace):
        with pytest.raises(buildlib.BuildError) as caught:
            recipe.stage(small_upstream, stage_dir, "linux")
    assert "G6: Makefile " + expected in str(caught.value)


def test_a_makefile_that_only_looks_different_still_stages(stage_dir, small_upstream):
    with Changed(small_upstream, "Makefile", replace=("CXXSTD    := -std=c++17", "CXXSTD := -std=c++17"),
                 append="\n# CXXFLAGS += -DNOT_REALLY\nextra:\n\t$(MAKE) CXXFLAGS=\"-O0\" $(BIN)\n"):
        recipe.stage(small_upstream, stage_dir, "linux")
    makefile = small_upstream / "Makefile"
    kept = makefile.read_bytes()
    makefile.unlink()
    try:
        with pytest.raises(buildlib.BuildError, match="G6: Makefile is missing"):
            recipe.stage(small_upstream, stage_dir, "linux")
    finally:
        makefile.write_bytes(kept)


def test_a_new_short_option_with_a_value_fails_staging(stage_dir, small_upstream):
    """Found in review: upstream bundles short options ("-AR title"), so the Windows main has to
    know which letters take a value to leave a title alone and to find an attached path."""
    row = '{\'\\0\', "tsv", Kind::Value, "FILE",'
    with Changed(small_upstream, "src/cli.cpp", replace=(row, row.replace("'\\0'", "'X'"))):
        with pytest.raises(buildlib.BuildError,
                           match=r"G3: .*short options that take a value.*update kValueShorts"):
            recipe.stage(small_upstream, stage_dir, "linux")


def test_the_unchanged_copy_still_stages(stage_dir, small_upstream):
    """The fixture is restored after every test above: none of them passed by leaving damage."""
    counts = recipe.stage(small_upstream, stage_dir, "linux")
    assert counts["replaced"] == 1 and counts["reviewed_lines"] == REVIEWED_LINES


# ---------------------------------------------------------------------------------------
# generated Windows resources
# ---------------------------------------------------------------------------------------

def test_manifest_declares_utf8_and_long_paths():
    text = recipe.manifest_text(versions.SCEPTER_VERSION)
    assert text.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
    assert text.isascii() and "\r" not in text and "<!--" not in text
    root = ET.fromstring(text.encode("utf-8"))
    assert root.tag == "{urn:schemas-microsoft-com:asm.v1}assembly"
    settings = {child.tag: child.text for child in root.iter() if "WindowsSettings}" in child.tag}
    assert settings == {
        "{http://schemas.microsoft.com/SMI/2019/WindowsSettings}activeCodePage": "UTF-8",
        "{http://schemas.microsoft.com/SMI/2016/WindowsSettings}longPathAware": "true",
    }
    identity = root.find("{urn:schemas-microsoft-com:asm.v1}assemblyIdentity")
    assert identity.get("version") == versions.SCEPTER_VERSION + ".0"
    assert identity.get("name") == branding.VENDOR + ".scepter"
    level = [e.get("level") for e in root.iter() if e.tag.endswith("requestedExecutionLevel")]
    assert level == ["asInvoker"]


def test_manifest_keeps_the_supported_systems_of_the_default_manifest():
    """Found in review: our manifest replaces the default one of MinGW toolchains, which
    declares Windows up to 10/11; without that block Windows runs the program as a Vista one."""
    root = ET.fromstring(recipe.manifest_text(versions.SCEPTER_VERSION).encode("utf-8"))
    ns = "{urn:schemas-microsoft-com:compatibility.v1}"
    blocks = root.findall(ns + "compatibility/" + ns + "application")
    assert len(blocks) == 1
    ids = [e.get("Id") for e in blocks[0]]
    assert all(e.tag == ns + "supportedOS" for e in blocks[0])
    assert "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}" in ids          # Windows 10 and 11
    assert len(ids) == len(set(ids)) == 5
    default = Path(buildlib.DEFAULT_CROSS_CXX).parent.parent / "x86_64-w64-mingw32/sysroot/usr/lib/default-manifest.o"
    if default.is_file():                                           # the one this toolchain links in
        theirs = re.findall(rb'supportedOS Id=""?(\{[0-9a-f-]{36}\})', default.read_bytes())
        assert sorted(i.decode("ascii") for i in theirs) == sorted(ids)


def test_version_resource_names_the_program():
    text = recipe.resource_text(versions.SCEPTER_VERSION)
    assert text.isascii()
    assert '1 24 "scepter.manifest"' in text
    assert "LANGUAGE 0x09, 0x01" in text
    assert "FILEVERSION %s,0" % versions.SCEPTER_VERSION.replace(".", ",") in text
    assert 'VALUE "FileDescription", "scepter read trimmer"' in text
    assert 'VALUE "CompanyName", "IOWA-BioTech"' in text
    assert 'VALUE "CompanyName", "%s"' % branding.VENDOR in text
    assert 'VALUE "FileVersion", "%s"' % versions.SCEPTER_VERSION in text
    assert 'VALUE "ProductVersion", "%s"' % versions.SCEPTER_VERSION in text
    assert 'VALUE "OriginalFilename", "scepter.exe"' in text


def test_windows_program_carries_manifest_and_version_when_built():
    exe = REPO / "build" / "engines" / "windows" / "scepter.exe"
    if not exe.is_file():
        pytest.skip("no Windows build at %s" % exe)
    found = recipe.windows_resources(exe)           # raises unless exactly ours is inside
    assert found["version_strings"]["FileDescription"] == "scepter read trimmer"
    assert found["version_strings"]["CompanyName"] == "IOWA-BioTech"
    assert found["version_strings"]["FileVersion"] == versions.SCEPTER_VERSION
    manifests = [blob for key, blob in recipe.pe_resources(exe).items() if key[0] == recipe.RT_MANIFEST]
    assert manifests == [recipe.manifest_text(versions.SCEPTER_VERSION).encode("utf-8")]


# ---------------------------------------------------------------------------------------
# building: nothing half-made is put in place
# ---------------------------------------------------------------------------------------

FAKE_COMPILER = r'''#!/bin/sh
# Stands in for g++: "compiles" by creating the object file, "links" by writing a few bytes,
# and warns or fails when the test asks for it.
out=""; src=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version) echo "fake g++ 1.0"; exit 0 ;;
        -o) out="$2"; shift ;;
        -c) src="$2"; shift ;;
    esac
    shift
done
if [ -n "$src" ]; then
    if [ -n "$FAKE_WARN_IN" ]; then
        case "$src" in *"$FAKE_WARN_IN") echo "$src:1:1: warning: something [-Wsomething]" ;; esac
    fi
    : > "$out"
    exit 0
fi
echo "a new program" > "$out"
if [ "$FAKE_LINK" = "fail" ]; then echo "ld: cannot finish"; exit 1; fi
exit 0
'''


def test_a_build_that_fails_leaves_the_previous_program(tmp_path, monkeypatch, small_upstream):
    """Found in review: the program was linked straight into the output folder and checked
    afterwards, so a build that failed its checks still replaced the previous program."""
    if os.name == "nt":
        pytest.skip("uses a shell script as the compiler")
    fake = tmp_path / "fake-g++"
    fake.write_text(FAKE_COMPILER, encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(recipe, "LOG_DIR", tmp_path / "logs")       # not the log of the real build
    out = tmp_path / "out"
    out.mkdir()
    program = out / "scepter"
    program.write_bytes(b"the previous program")
    tc = buildlib.Toolchain(target="linux", cxx=str(fake))

    def build():
        return recipe.build(tc, small_upstream, tmp_path / "stage", out, 2)

    monkeypatch.setenv("FAKE_LINK", "fail")
    monkeypatch.delenv("FAKE_WARN_IN", raising=False)
    with pytest.raises(buildlib.BuildError, match="cannot finish"):
        build()
    assert program.read_bytes() == b"the previous program"

    # a warning in one of our own files stops the build, before anything is put in place
    monkeypatch.setenv("FAKE_LINK", "ok")
    monkeypatch.setenv("FAKE_WARN_IN", "scepter_port.cpp")
    with pytest.raises(buildlib.BuildError, match="port files of scepter compile with 1 warning"):
        build()
    assert program.read_bytes() == b"the previous program"
    assert [p.name for p in out.iterdir()] == ["scepter"]

    # a warning in upstream's code is counted and reported, not fatal: the new program arrives
    monkeypatch.setenv("FAKE_WARN_IN", "stats.cpp")
    facts = build()
    assert facts["warnings"] == {"upstream": 1, "port": 0, "other": 0}
    assert facts["version"] == versions.SCEPTER_VERSION and facts["sources"] == 12
    assert program.read_bytes() == b"a new program\n"
    assert [p.name for p in out.iterdir()] == ["scepter"]


# ---------------------------------------------------------------------------------------
# the built program
# ---------------------------------------------------------------------------------------

def _assert_built_from_these_sources(binary: Path, scratch: Path) -> dict:
    """The facts build.py recorded for ``binary``, after checking that they describe this very
    file and that it was built from the port files and the pinned source as they are now."""
    info = binary.parent / "BUILD_INFO.json"
    if not info.is_file():
        pytest.skip("no BUILD_INFO.json beside %s" % binary)
    with open(info, encoding="utf-8") as handle:
        facts = json.load(handle).get("engines", {}).get("scepter")
    if not facts:
        pytest.skip("BUILD_INFO.json beside %s does not mention scepter" % binary)
    assert facts["sha256"] == buildlib.sha256(binary), "BUILD_INFO.json describes another file"
    fresh = recipe.stage(UPSTREAM, scratch / "fresh-stage", "linux")
    shutil.rmtree(scratch / "fresh-stage")
    assert facts["staged_sha256"] == fresh["staged_sha256"], (
        "%s was built from other sources than the ones here: run engines/build.py --engine scepter again" % binary)
    assert facts["warnings"]["port"] == 0
    return facts


def test_built_program_is_built_from_these_sources(program, tmp_path):
    """Without this the tests below pass against a program built before the last change."""
    facts = _assert_built_from_these_sources(program, tmp_path)
    assert facts["version"] == versions.SCEPTER_VERSION
    assert facts["transformations"]["T1-fseek64"]["present"] == 1


def test_windows_program_is_built_from_these_sources_when_built(tmp_path):
    exe = REPO / "build" / "engines" / "windows" / "scepter.exe"
    if not exe.is_file():
        pytest.skip("no Windows build at %s" % exe)
    facts = _assert_built_from_these_sources(exe, tmp_path)
    assert facts["dll_imports"] and all(buildlib.is_system_dll(name) for name in facts["dll_imports"])
    assert "-static" in facts["ldflags"]
    assert "-D__USE_MINGW_ANSI_STDIO=1" in facts["cxxflags"]


def test_built_program_reports_the_pinned_version(program):
    done = _run(program, ["--version"])
    assert done.returncode == 0
    assert b"\r" not in done.stdout                                 # binary standard output on Windows too
    assert done.stdout.decode("ascii").split("\n")[0] == "scepter " + versions.SCEPTER_VERSION
    # the self-test switch is exact: anything but "1" is a normal run
    for value in ("0", "", "11", "true"):
        again = _run(program, ["--version"], env={"SCEPTER_PORT_SELFTEST": value})
        assert again.returncode == 0 and again.stdout == done.stdout


def test_built_program_passes_its_self_test(program, tmp_path):
    work = tmp_path / "self test é"                                 # a space and an accent on the way in
    work.mkdir()
    star, exotic, title = "*", "é日 x", r"a\b"
    done = _run(program, [star, exotic, title, "*.fq"],
                env={"SCEPTER_PORT_SELFTEST": "1", "SCEPTER_PORT_SELFTEST_DIR": str(work)}, cwd=str(tmp_path))
    text = done.stdout.decode("utf-8")
    assert done.returncode == 0, text + done.stderr.decode("utf-8", "replace")
    assert done.stderr == b""
    assert b"\r" not in done.stdout
    lines = [line for line in text.split("\n") if line]
    assert all("=" in line for line in lines), lines
    probes = dict(line.split("=", 1) for line in lines)
    assert len(probes) == len(lines)                                # one line per probe, no key twice
    assert lines[-1] == "selftest=pass"

    windows = os.name == "nt"
    expected = {
        "target": "windows" if windows else "linux",
        "sizeof_long": "4" if windows else "8",
        "sizeof_long_long": "8",
        "sizeof_stat_size": "8",
        "exe_path_exists": "1",
        "same_file_two_files": "0",
        "same_file_itself": "1",
        "big_file_size": str((1 << 32) + (2 << 20) + 8),
        "big_file_seek": "1",
        "text_mode_write_bytes": "2",
        "text_mode_stream_bytes": "2",
        "text_mode_read_bytes": "6",
        "round_1f_0.25": "0.2",
        "round_2f_0.125": "0.12",
        "round_0f_2.5": "2",
        "round_4f_97.65625": "97.6562",
        "round_6g_1234565": "1.23456e+06",
        "round_6g_0.00001": "1e-05",
        "format_zu": "18446744073709551615",
        "argument_slashes": "1",
        "unicode_dir": "1",
        "unicode_dir_zlib": "1",
        "work_dir_removed": "1",
        "args": "4",
        "arg1": star,                                               # not expanded into file names
        "arg2_hex": exotic.encode("utf-8").hex(),                  # UTF-8 all the way
        "arg3": title,                                              # the self-test sees arguments untouched
        "arg4": "*.fq",
    }
    if windows:
        expected.update({"active_code_page": "65001", "unicode_dir_wide": "1", "default_file_mode": "binary",
                         "stdout_mode": "binary", "stderr_mode": "binary", "wildcard_expansion": "off"})
    assert {key: probes.get(key) for key in expected} == expected
    assert Path(probes["exe_path"]).resolve() == program.resolve()
    assert Path(probes["work_dir"]).parent == work
    assert list(work.iterdir()) == []                               # it cleaned up after itself


def test_built_program_trims_without_the_contaminant_database(program, tmp_path):
    """The app ships no database: a standard run must work and say nothing about it."""
    assert not (program.parent / "data" / "contam_db.bin.gz").exists()
    import random
    rng = random.Random(5)
    folder = tmp_path / "reads here"
    folder.mkdir()
    r1, r2 = folder / "s_R1.fastq.gz", folder / "s_R2.fastq.gz"
    swap = str.maketrans("ACGT", "TGCA")
    with gzip.open(r1, "wt", newline="\n") as one, gzip.open(r2, "wt", newline="\n") as two:
        for i in range(400):
            fragment = "".join(rng.choice("ACGT") for _ in range(260))
            quality = "F" * 120 + "#" * 30
            one.write("@M01:1:FC:1:1101:%d:%d 1:N:0:1\n%s\n+\n%s\n" % (1000 + i, 2000 + i, fragment[:150], quality))
            two.write("@M01:1:FC:1:1101:%d:%d 2:N:0:1\n%s\n+\n%s\n"
                      % (1000 + i, 2000 + i, fragment[::-1].translate(swap)[:150], quality))
    out = tmp_path / "out"
    out.mkdir()
    done = _run(program, ["-i", str(r1), "-I", str(r2), "-o", "s_trimmed_R1.fastq.gz", "-O", "s_trimmed_R2.fastq.gz",
                          "-j", "trimming_report.json", "-h", "trimming_report.html", "-R", "s", "-w", "2",
                          "--no-color", "--no-summary", "--verbose", "--cut_tail", "--cut_tail_mean_quality", "10",
                          "--qualified_quality_phred", "15", "--unqualified_percent_limit", "40",
                          "--n_base_limit", "5", "--average_qual", "15", "--length_required", "50"], cwd=str(out))
    messages = done.stderr.decode("utf-8")
    assert done.returncode == 0, messages
    assert done.stdout == b""
    for word in ("contam", "database", "warning", "error"):
        assert word not in messages.lower(), messages
    assert sorted(p.name for p in out.iterdir()) == ["s_trimmed_R1.fastq.gz", "s_trimmed_R2.fastq.gz",
                                                     "trimming_report.html", "trimming_report.json"]
    with gzip.open(out / "s_trimmed_R1.fastq.gz", "rt") as handle:
        records = handle.read().split("\n")
    assert len(records) // 4 == 400 and len(records[1]) < 150      # every pair kept, the bad tail cut
    # a second run over the outputs of the first: refused on Windows if the same-file test were wrong
    again = _run(program, ["-i", str(r1), "-I", str(r2), "-o", "s_trimmed_R1.fastq.gz", "-O", "s_trimmed_R2.fastq.gz",
                           "-j", "trimming_report.json", "-h", "trimming_report.html", "-w", "1", "--quiet"],
                 cwd=str(out))
    assert again.returncode == 0, again.stderr.decode("utf-8", "replace")
    # and the real thing is still refused: the input under another spelling, so that it is
    # the identity of the file that is compared and not the text of the path
    spelled = os.path.join(str(folder), ".", r1.name)
    assert spelled != str(r1)
    same = _run(program, ["-i", str(r1), "-I", str(r2), "-o", spelled, "-O", "o2.fastq.gz", "--quiet"],
                cwd=str(out))
    assert same.returncode == 1 and b"is the same file as the input" in same.stderr
    with gzip.open(r1, "rt") as handle:                             # and the input was not touched
        assert len(handle.read().split("\n")) // 4 == 400
