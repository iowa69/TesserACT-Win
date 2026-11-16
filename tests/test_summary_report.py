# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""run_summary.tsv and run_summary.html.

Run this file as a script to write the sample page that is looked at by eye:
``python3 tests/test_summary_report.py build/shots`` writes ``run_summary_sample.html`` there.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from html.parser import HTMLParser
from urllib.parse import unquote

import pytest

if __name__ == "__main__":      # run as a script: make the package importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tesseract_win import branding, fsutil, results, summary_report, versions
from tesseract_win.model import EndReason, RunStatus, SampleOutcome

SETTINGS_LINE = "Settings: standard trimming, standard assembly, 8 threads."


# --------------------------------------------------------------------------- building a folder

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def good(name, status=RunStatus.DONE, **extra):
    values = dict(name=name, status=status, trimmed=True, contigs=71, total_length=5482113,
                  largest=611204, n50=214880, depth=63, seconds=1391.6,
                  finished_at="2026-11-17T03:42:10")
    values.update(extra)
    return SampleOutcome(**values)


def put_on_disk(rdir, outcome, *, settings_text="standard trimming, standard assembly",
                fingerprint="standard", stored=None):
    """A finished sample in the folder, made the way the runner makes it."""
    name = outcome.name
    results.begin_attempt(rdir, name)
    asm = results.in_progress_assembly_dir(rdir, name)
    _write(os.path.join(asm, "contigs.fasta"), ">contig_1\nACGT\n")
    _write(os.path.join(asm, "report.html"), "<html>report</html>")
    _write(os.path.join(asm, "assembly.log"), "log\n")
    if outcome.trimmed:
        trim = results.in_progress_trimming_dir(rdir, name)
        _write(os.path.join(trim, "trimming_report.html"), "<html>trimming</html>")
        _write(os.path.join(trim, results.trimmed_read_names(name)[0]), "reads")
    results.commit_attempt(rdir, name, keep_trimmed_reads=True)
    results.collect(rdir, name)
    results.write_status(rdir, outcome, reads=[("/reads/%s_R1.fastq.gz" % name, 100),
                                               ("/reads/%s_R2.fastq.gz" % name, 101)],
                         settings_fingerprint=fingerprint, settings_text=settings_text,
                         trimmed=outcome.trimmed)
    if stored:
        path = results.status_path(rdir, name)
        data = fsutil.read_json(path)
        data.update(stored)
        fsutil.atomic_write_json(path, data)
    return outcome


def fail_on_disk(rdir, outcome):
    """A failed sample: its logs kept, its status file saying why."""
    name = outcome.name
    results.begin_attempt(rdir, name)
    asm = results.in_progress_assembly_dir(rdir, name)
    _write(os.path.join(asm, "assembly.log"), "error: truncated\n")
    _write(os.path.join(asm, "preflight.txt"), "FAIL gzip\n")
    outcome.log = os.path.join(asm, "assembly.log")
    results.abandon_attempt(rdir, name, keep_logs=True)
    results.write_status(rdir, outcome, reads=[], settings_fingerprint="standard",
                         settings_text="standard trimming, standard assembly", trimmed=outcome.trimmed)
    outcome.log = os.path.join(results.last_failed_dir(rdir, name), "assembly", "assembly.log")
    return outcome


def build_sample(rdir):
    """Twelve samples of mixed fortunes in one run (eleven in the list, one left out), and two
    results from an earlier run."""
    this_run = []
    # Two results from last week, not part of this run.
    put_on_disk(rdir, good("PA_01", contigs=102, total_length=6710455, largest=402113, n50=148220,
                           depth=48, trimmed=False, finished_at="2026-11-03T16:20:00"),
                settings_text="not trimmed; mode Careful", fingerprint="careful",
                stored={"tesseract_version": "1.6.1"})
    put_on_disk(rdir, good("PA_02", status=RunStatus.DONE_NOTES, contigs=133, total_length=6802004,
                           largest=311870, n50=96450, depth=31, finished_at="2026-11-03T17:05:00",
                           note="Low depth (about 31x): expect a fragmented assembly.",
                           notes=["Low depth (about 31x): expect a fragmented assembly."]))
    # Already there before this run: one is skipped, one is redone and the redo fails.
    put_on_disk(rdir, good("KP_005", contigs=64, total_length=5391020, largest=702331, n50=301450,
                           depth=88, finished_at="2026-11-03T15:02:00"))
    put_on_disk(rdir, good("KP_007", contigs=80, total_length=5512876, largest=488010, n50=190322,
                           depth=57, finished_at="2026-11-03T15:40:00"))

    this_run.append(put_on_disk(rdir, good("KP_001")))
    this_run.append(put_on_disk(rdir, good("KP_002", contigs=58, total_length=5401877, largest=803412,
                                           n50=341209, depth=91, seconds=1210,
                                           finished_at="2026-11-16T19:14:00")))
    this_run.append(put_on_disk(rdir, good(
        "KP_003", status=RunStatus.DONE_NOTES, contigs=412, total_length=5120934, largest=88120,
        n50=21877, depth=22, seconds=1675, finished_at="2026-11-16T19:42:00",
        note="Low depth (about 22x): expect a fragmented assembly.",
        notes=["Low depth (about 22x): expect a fragmented assembly.",
               "The assembly is smaller than expected for a 5.5 Mb genome (5.1 Mb).",
               "Many reads still carried adapter sequence before trimming."])))
    this_run.append(fail_on_disk(rdir, SampleOutcome(
        name="KP_004", status=RunStatus.FAILED, trimmed=True, seconds=95,
        finished_at="2026-11-16T19:44:00",
        note="A read file is incomplete or damaged (KP_004_R2.fastq.gz). It may have been cut short "
             "while copying or downloading.",
        next_step="Copy it again from the original, then try again.")))
    this_run.append(SampleOutcome(name="KP_005", status=RunStatus.SKIPPED,
                                  note="Skipped (already assembled on 3 Nov 2026)."))
    this_run.append(put_on_disk(rdir, good("KP_006", contigs=1904, total_length=4801223, largest=21044,
                                           n50=3120, depth=9, seconds=2890, thin_retry=True,
                                           status=RunStatus.DONE_NOTES,
                                           finished_at="2026-11-16T20:33:00",
                                           note="Very low depth (about 9x): the assembly is in many small pieces.",
                                           notes=["Very low depth (about 9x): the assembly is in many small pieces."])))
    this_run.append(SampleOutcome(
        name="KP_007", status=RunStatus.FAILED, trimmed=True, seconds=640, kept_earlier_result=True,
        finished_at="2026-11-16T20:44:00",
        note="This sample needs more memory than this PC could give. The earlier assembly (3 Nov 2026) was kept.",
        next_step="Close other programs and try again, or assemble it on a PC with more memory."))
    this_run.append(put_on_disk(rdir, good("SA_17", contigs=38, total_length=2801344, largest=612009,
                                           n50=255871, depth=140, trimmed=False, seconds=702,
                                           finished_at="2026-11-16T20:56:00"),
                                settings_text="not trimmed; standard assembly"))
    this_run.append(SampleOutcome(name="SA_18", status=RunStatus.STOPPED, trimmed=True, seconds=310,
                                  finished_at="2026-11-16T21:01:00", note="Stopped."))
    left_out = [("EC_09", "No Read 2 file found. Each sample needs two files in the same folder, "
                          "for example X_R1.fastq.gz and X_R2.fastq.gz.")]
    run_info = {
        "started": "2026-11-16T18:28:00", "finished": "2026-11-16T21:01:00", "seconds": 9180,
        "end_reason": EndReason.STOPPED,
        "message": "Stopped. 6 finished, 2 failed, 1 stopped, 2 not started. Nothing that was finished "
                   "has been lost.",
        "settings": SETTINGS_LINE,
        "events": ["The PC was asleep from 19:02 to 19:11. The run carried on when it woke up.",
                   "The run was stopped by the user at 21:01, while SA_18 was being trimmed."],
        "samples": ["KP_001", "KP_002", "KP_003", "KP_004", "KP_005", "KP_006", "KP_007", "SA_17",
                    "SA_18", "SA_19", "SA_20"],
    }
    _write(results.run_log_path(rdir), "run log\n")
    return this_run, left_out, run_info


def write_sample(rdir):
    this_run, left_out, run_info = build_sample(rdir)
    assert summary_report.write_run_summary(rdir, this_run=this_run, left_out=left_out,
                                            run_info=run_info) == []
    return _read(results.summary_html_path(rdir)), _read(results.summary_tsv_path(rdir))


@pytest.fixture
def rdir(tmp_path):
    return str(tmp_path / "Results 2026")


@pytest.fixture
def sample(rdir):
    page, table = write_sample(rdir)
    return rdir, page, table


# --------------------------------------------------------------------------- reading the page

VOID = {"meta", "br", "hr", "img", "input", "link", "area", "base", "col", "embed", "source", "wbr"}


class Page(HTMLParser):
    """Parses the page strictly enough to catch unbalanced tags and collects what it refers to."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []
        self.tags, self.links, self.attributes, self.ids = [], [], [], []
        self.text = []
        self.rows = []                      # table rows as lists of cell texts
        self._cell = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for key, value in attrs:
            self.attributes.append((tag, key, value or ""))
            if key in ("href", "src"):
                self.links.append(value or "")
            if key == "id":
                self.ids.append(value)
        if tag == "tr":
            self.rows.append([])
        if tag in ("td", "th") and self.rows:
            self._cell = []
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self.rows[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        if not self.stack or self.stack[-1] != tag:
            self.errors.append("</%s> closes %s" % (tag, self.stack[-1:] or "nothing"))
        else:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] not in ("style", "script"):
            self.text.append(data)
            if self._cell is not None:
                self._cell.append(data)

    @property
    def words(self):
        return " ".join(" ".join(self.text).split())

    def row(self, name):
        found = [r for r in self.rows if r and r[0] == name]
        assert found, "no table row for %r" % name
        return found[0]


def parse(page):
    parser = Page()
    parser.feed(page)
    parser.close()
    assert parser.errors == [] and parser.stack == [], (parser.errors, parser.stack)
    for link in parser.links:               # added in review: no link inside the page leads nowhere
        assert not link.startswith("#") or link[1:] in parser.ids, "the link %s leads nowhere" % link
    return parser


def tsv_rows(table):
    lines = table.split("\n")
    assert lines[-1] == ""
    header = lines[0].split("\t")
    return header, [dict(zip(header, line.split("\t"))) for line in lines[1:-1]]


# --------------------------------------------------------------------------- the TSV

def test_tsv_has_the_agreed_columns_and_plain_format(sample):
    _rdir, _page, table = sample
    header, rows = tsv_rows(table)
    assert header == ["sample", "status", "contigs", "total_length", "largest", "N50", "depth", "trimmed",
                      "settings", "program_version", "TesserACT_version", "scepter_version", "finished",
                      "seconds", "note"]
    assert tuple(header) == summary_report.TSV_COLUMNS
    assert "\r" not in table
    assert all(line.count("\t") == len(header) - 1 for line in table.split("\n")[:-1])
    with open(results.summary_tsv_path(_rdir), "rb") as handle:
        raw = handle.read()
    assert raw.decode("utf-8") == table and not raw.startswith(b"\xef\xbb\xbf")


def test_tsv_has_a_row_for_every_sample_in_the_folder_and_for_this_run_s_others(sample):
    _rdir, _page, table = sample
    _header, rows = tsv_rows(table)
    assert [(r["sample"], r["status"]) for r in rows] == [
        ("EC_09", "left_out"),
        ("KP_001", "done"), ("KP_002", "done"), ("KP_003", "done_notes"), ("KP_004", "failed"),
        ("KP_005", "done"), ("KP_006", "done_notes"), ("KP_007", "done"),
        ("PA_01", "done"), ("PA_02", "done_notes"),
        ("SA_17", "done"), ("SA_18", "stopped"), ("SA_19", "not_started"), ("SA_20", "not_started"),
    ]
    by_name = {r["sample"]: r for r in rows}
    kp1 = by_name["KP_001"]
    assert (kp1["contigs"], kp1["total_length"], kp1["largest"], kp1["N50"], kp1["depth"]) == \
        ("71", "5482113", "611204", "214880", "63")
    assert kp1["trimmed"] == "yes" and kp1["seconds"] == "1392"
    assert kp1["finished"] == "2026-11-17T03:42:10"
    assert kp1["settings"] == "standard trimming, standard assembly"
    assert (kp1["program_version"], kp1["TesserACT_version"], kp1["scepter_version"]) == (
        "v" + versions.APP_VERSION, "v" + versions.TESSERACT_VERSION, "v" + versions.SCEPTER_VERSION)
    # A mixed folder describes itself: settings and versions per sample.
    assert by_name["PA_01"]["settings"] == "not trimmed; mode Careful"
    assert by_name["PA_01"]["TesserACT_version"] == "v1.6.1" and by_name["PA_01"]["trimmed"] == "no"
    # This run's failure with no result: the reason and the next step, no numbers.
    kp4 = by_name["KP_004"]
    assert kp4["contigs"] == kp4["total_length"] == kp4["N50"] == kp4["depth"] == ""
    assert "incomplete or damaged" in kp4["note"] and "Copy it again" in kp4["note"]
    # A failed redo: the row is the result that is still there; the note says what happened.
    kp7 = by_name["KP_007"]
    assert kp7["contigs"] == "80" and kp7["finished"] == "2026-11-03T15:40:00"
    assert kp7["note"].startswith("The latest attempt failed: This sample needs more memory")
    assert kp7["note"].endswith("The earlier assembly was kept.")
    assert by_name["EC_09"]["note"].startswith("No Read 2 file found.")
    assert by_name["KP_003"]["note"].count("Low depth") == 1          # the first note is not doubled
    assert "smaller than expected" in by_name["KP_003"]["note"]


def test_tsv_numbers_are_whole_and_have_no_separators(sample):
    _rdir, _page, table = sample
    _header, rows = tsv_rows(table)
    for row in rows:
        for column in ("contigs", "total_length", "largest", "N50", "depth", "seconds"):
            assert re.fullmatch(r"\d*", row[column]), (row["sample"], column, row[column])
    assert "5,482,113" not in table and "5.48" not in table


def test_tsv_cells_never_break_the_table(rdir):
    nasty = SampleOutcome(name="KP_1", status=RunStatus.FAILED,
                          note="first line\nsecond\tline\r\nthird", next_step="do\tthis")
    assert summary_report.write_run_summary(rdir, this_run=[nasty],
                                            left_out=[("EC_1", "no\tpartner\nfound")], run_info={}) == []
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    assert [r["note"] for r in rows] == ["no partner found", "first line second line third. do this."]


# --------------------------------------------------------------------------- the page

def test_html_parses_and_is_one_self_contained_file(sample):
    _rdir, page, _table = sample
    parsed = parse(page)
    assert page.startswith("<!DOCTYPE html>")
    assert "\r" not in page
    for forbidden in ("script", "link", "img", "iframe", "object", "embed", "video", "audio", "base"):
        assert forbidden not in parsed.tags, forbidden
    assert parsed.tags.count("style") == 1
    assert "@import" not in page and "url(" not in page
    for _tag, key, _value in parsed.attributes:
        assert not key.startswith("on") and key != "style", key        # no handlers, no inline styles
    assert len(parsed.ids) == len(set(parsed.ids))


def test_html_refers_to_nothing_on_the_network_except_the_homepage(sample):
    _rdir, page, _table = sample
    parsed = parse(page)
    remote = [link for link in parsed.links if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:|^//", link)]
    assert remote == [branding.HOMEPAGE]
    without_homepage = page.replace(branding.HOMEPAGE, "")
    assert "http://" not in without_homepage and "https://" not in without_homepage
    assert "//" not in "".join(v for _t, key, v in parsed.attributes if key in ("href", "src")
                               and v != branding.HOMEPAGE)


def test_html_links_are_relative_and_lead_to_what_is_there(sample):
    rdir, page, _table = sample
    parsed = parse(page)
    local = [link for link in parsed.links if link != branding.HOMEPAGE and not link.startswith("#")]
    assert "KP_001/assembly/report.html" in local
    assert "KP_001/trimming/trimming_report.html" in local
    assert "KP_001/" in local and "assemblies/" in local
    assert "run_summary.tsv" in local and "run_log.txt" in local
    assert "KP_004/last_failed_attempt/assembly/assembly.log" in local
    assert "SA_17/trimming/trimming_report.html" not in local          # not trimmed: no such report
    assert "SA_19/" not in local                                       # never started: no folder
    for link in local:
        assert not os.path.isabs(link) and ".." not in link and "\\" not in link
        assert os.path.exists(os.path.join(rdir, *unquote(link).split("/"))), link
    for anchor in [link[1:] for link in parsed.links if link.startswith("#")]:
        assert anchor in parsed.ids, anchor
    assert rdir not in page and os.path.dirname(rdir) not in page      # nothing absolute: it can be moved


def test_html_says_the_outcome_in_one_line_with_time_and_duration(sample):
    _rdir, page, _table = sample
    words = parse(page).words
    assert ("Stopped. 6 finished, 2 failed, 1 stopped, 2 not started. Nothing that was finished has "
            "been lost.") in words
    assert "Finished 16 Nov 2026, 21:01. It took 2 h 33 min. Started 16 Nov 2026, 18:28." in words
    assert "<h1>Stopped. 6 finished" in page
    assert 'http-equiv="refresh"' not in page


def test_html_accounts_for_every_sample(sample):
    _rdir, page, _table = sample
    parsed = parse(page)
    words = parsed.words
    for piece in ("3 Done", "2 Done (check notes)", "2 Failed", "1 Skipped (already assembled)",
                  "1 Stopped", "2 Not started", "1 Left out", "12 samples in this run"):
        assert piece in words, piece
    # Eleven in the list plus the one left out; one circle per sample, each leading to its row.
    wells = [a for a in parsed.attributes if a[1] == "class" and a[2].startswith("well ")]
    assert len(wells) == 12
    assert "Already in this folder from earlier runs: 2 samples" in words
    assert "This run: 12 samples" in words


def test_html_table_has_the_agreed_columns_with_readable_numbers(sample):
    _rdir, page, _table = sample
    parsed = parse(page)
    assert parsed.rows[0] == ["Sample", "Status", "Contigs", "Total length", "N50", "Depth", "Note", "Open"]
    assert parsed.row("KP_001")[:7] == ["KP_001", "Done", "71", "5,482,113", "214,880", "63×", ""]
    assert parsed.row("KP_001")[7] == "ReportTrimmingFolder"
    kp3 = parsed.row("KP_003")
    assert kp3[1] == "Done (check notes)" and kp3[2] == "412"
    assert kp3[6] == "Low depth (about 22x): expect a fragmented assembly. Plus 2 more notes."
    assert parsed.row("KP_005")[1:6] == ["Skipped (already assembled)", "64", "5,391,020", "301,450", "88×"]
    assert parsed.row("KP_005")[6] == "Skipped (already assembled on 3 Nov 2026)."
    assert parsed.row("KP_004")[1:6] == ["Failed", "", "", "", ""]
    assert parsed.row("KP_004")[7] == "LogFolder"
    assert parsed.row("KP_007")[1:6] == ["Failed", "", "", "", ""]
    assert parsed.row("SA_19")[1] == "Not started" and parsed.row("SA_18")[1] == "Stopped"
    assert parsed.row("EC_09")[1] == "Left out" and parsed.row("EC_09")[6].startswith("No Read 2 file")
    pa1 = parsed.row("PA_01")
    assert pa1[1:6] == ["Done", "102", "6,710,455", "148,220", "48×"]
    assert pa1[6] == "Made on 3 Nov 2026 (not trimmed; mode Careful; TesserACT 1.6.1)."
    # Run order for this run, then the earlier ones.
    names = [r[0] for r in parsed.rows if len(r) == 8][1:]
    assert names == ["KP_001", "KP_002", "KP_003", "KP_004", "KP_005", "KP_006", "KP_007", "SA_17",
                     "SA_18", "SA_19", "SA_20", "EC_09", "PA_01", "PA_02"]


def test_html_gives_reason_and_next_step_for_every_failure_and_the_notes_in_full(sample):
    _rdir, page, _table = sample
    words = parse(page).words
    assert "KP_004 failed" in words
    assert ("A read file is incomplete or damaged (KP_004_R2.fastq.gz). It may have been cut short while "
            "copying or downloading. What to do: Copy it again from the original, then try again.") in words
    assert "KP_007 failed" in words
    assert "What to do: Close other programs and try again, or assemble it on a PC with more memory." in words
    assert "The earlier assembly (3 Nov 2026) was kept." in words
    assert "The earlier assembly was kept." not in words                # the note said it already
    for note in ("The assembly is smaller than expected for a 5.5 Mb genome (5.1 Mb).",
                 "Many reads still carried adapter sequence before trimming."):
        assert note in words
    assert "1 sample was stopped and 2 samples were not started." in words
    assert "choose this results folder and press Continue" in words
    assert "1 sample was left out of this run" in words


def test_html_says_where_things_are_what_happened_and_what_it_was_made_with(sample):
    _rdir, page, _table = sample
    words = parse(page).words
    assert "Your assemblies are in the folder assemblies : 9 files, one per sample" in words
    assert "The PC was asleep from 19:02 to 19:11. The run carried on when it woke up." in words
    assert "KP_006 had very little data: its assembly was made on a second attempt" in words
    methods = ("Reads were assembled with TesserACT %s, run through %s %s (%s); for 4 of the 5 samples they "
               "were first trimmed with scepter %s. %s Date of the run: 16 Nov 2026."
               % (versions.TESSERACT_VERSION, branding.APP_NAME, versions.APP_VERSION, branding.HOMEPAGE,
                  versions.SCEPTER_VERSION, SETTINGS_LINE))
    assert methods in words
    assert branding.ATTRIBUTION in words and branding.APP_NAME in words
    assert 'name="generator" content="%s %s"' % (branding.APP_NAME, versions.APP_VERSION) in page
    assert results.is_results_folder(_rdir)


def test_html_uses_the_house_colours_light_dark_and_print(sample):
    _rdir, page, _table = sample
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    for colour in ("#ecf0f6", "#ffffff", "#121820", "#2563eb", "#0f7032", "#8a5300", "#b42318"):
        assert colour in style, colour
    assert "prefers-color-scheme:dark" in style
    assert "@media print" in style
    assert style.count("{") == style.count("}")
    assert 'name="viewport"' in page and '<html lang="en">' in page and 'charset="utf-8"' in page


def test_a_name_cannot_turn_a_link_into_something_else():
    assert summary_report._url("KP 1", "assembly", "report.html") == "KP%201/assembly/report.html"
    assert summary_report._url("javascript:alert(1)") == "javascript%3Aalert%281%29"
    assert summary_report._url("..", "..", "secret") == "%2E%2E/%2E%2E/secret"
    assert summary_report._url("a/b#c?d%e") == "a%2Fb%23c%3Fd%25e"
    assert summary_report._url("v1.2", "KP.1") == "v1.2/KP.1"


def test_dates_do_not_depend_on_the_language_of_the_pc():
    assert summary_report._when("2026-11-17T03:42:10") == "17 Nov 2026, 03:42"
    assert summary_report._when("2026-03-01T15:05:00") == "1 Mar 2026, 15:05"
    assert summary_report._day("2026-11-03T16:20:00") == "3 Nov 2026"
    assert summary_report._when("some day") == "some day" and summary_report._when(None) == ""
    assert summary_report._took(33240) == "9 h 14 min"
    assert summary_report._took(401.2) == "6 min 41 s" and summary_report._took(42) == "42 s"
    assert summary_report._took("soon") == ""


NASTY = 'KP<1>&"x" <script>alert(1)'       # no "/": that cannot be in a folder name anywhere


def test_html_escapes_every_value(rdir):
    # Windows does not allow < > " in a folder name: there the same values reach the page from
    # the run alone, with no folder behind them.
    make = put_on_disk if os.name != "nt" else (lambda _rdir, outcome: outcome)
    on_disk = make(rdir, good(NASTY, note='note <b>&amp; "quoted"'))
    failed = SampleOutcome(name='F<2>&"y"', status=RunStatus.FAILED, note='reason <i>&"',
                           next_step='step </p><script>&"')
    noted = make(rdir, good('N&3"<', status=RunStatus.DONE_NOTES, note='<n1>', notes=['<n1>', '"n2"&']))
    run_info = {"started": "2026-11-16T18:28:00", "finished": "2026-11-17T03:42:00", "seconds": 33240,
                "end_reason": "completed", "message": 'Finished <at> 03:42 & "all" done',
                "settings": 'Settings: <own> adapters "ACGT" & more.',
                "events": ['The PC <slept> & "woke"'], "samples": [NASTY, 'F<2>&"y"', 'N&3"<', "W<4>"]}
    assert summary_report.write_run_summary(
        rdir, this_run=[on_disk, failed, noted], left_out=[('L<5>&"', 'why <not> & "so"')],
        run_info=run_info) == []
    page = _read(results.summary_html_path(rdir))
    parsed = parse(page)

    assert "<script" not in page.lower()
    assert "<i>" not in page and "<n1>" not in page and "<own>" not in page and "<slept>" not in page
    assert "note &lt;b&gt;&amp;amp; &quot;quoted&quot;" in page
    assert "KP&lt;1&gt;&amp;&quot;x&quot; &lt;script&gt;alert(1)" in page
    # ...and every value comes out of the parser exactly as it went in.
    words = parsed.words
    for value in (NASTY, 'F<2>&"y"', 'N&3"<', "W<4>", 'L<5>&"', 'why <not> & "so"', 'reason <i>&"',
                  'step </p><script>&"', '"n2"&', 'Finished <at> 03:42 & "all" done',
                  'Settings: <own> adapters "ACGT" & more.', 'The PC <slept> & "woke"',
                  'note <b>&amp; "quoted"'):
        assert value in words, value
    assert parsed.row(NASTY)[1] == "Done"
    titles = [value for _tag, key, value in parsed.attributes if key == "title"]
    assert "%s: Done" % NASTY in titles and 'L<5>&": Left out' in titles
    # Links to a folder with such a name are escaped twice over: for a URL and for HTML.
    if os.name != "nt":
        report = "KP%3C1%3E%26%22x%22%20%3Cscript%3Ealert%281%29/assembly/report.html"
        assert report in parsed.links
        assert os.path.isfile(os.path.join(rdir, *unquote(report).split("/")))
    for link in parsed.links:
        assert not re.search(r'[<>" ]', link), link

    table = _read(results.summary_tsv_path(rdir))
    assert NASTY in table and 'why <not> & "so"' in table            # the TSV keeps values as they are


@pytest.mark.parametrize("folder, name", [
    ("Results of José & Zoë (2026)", "KP 001 [rerun] #2"),
    ("結果 フォルダ", "サンプル_01"),
    ("Результаты 100%", "образец №7"),
])
def test_names_with_spaces_and_non_latin_letters(tmp_path, folder, name):
    rdir = str(tmp_path / folder)
    outcome = put_on_disk(rdir, good(name))
    failed = fail_on_disk(rdir, SampleOutcome(name=name + " b", status=RunStatus.FAILED, note="Не удалось — 失敗"))
    assert summary_report.write_run_summary(rdir, this_run=[outcome, failed], left_out=[],
                                            run_info={"finished": "2026-11-17T03:42:00"}) == []
    page = _read(results.summary_html_path(rdir))
    parsed = parse(page)
    assert parsed.row(name)[0] == name and "Не удалось — 失敗" in parsed.words
    assert folder in parsed.words                                       # the page says which folder it is
    local = [link for link in parsed.links if link != branding.HOMEPAGE and not link.startswith("#")]
    assert len(local) >= 7
    for link in local:
        assert re.fullmatch(r"[A-Za-z0-9._~%/-]+", link), link         # plain ASCII, safe in any browser
        assert os.path.exists(os.path.join(rdir, *unquote(link).split("/"))), link
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    assert [r["sample"] for r in rows] == [name, name + " b"]
    with open(results.summary_html_path(rdir), "rb") as handle:
        assert name.encode("utf-8") in handle.read()


def test_while_the_run_is_going_the_page_says_so_and_shows_who_is_waiting(rdir):
    done = put_on_disk(rdir, good("KP_1", finished_at="2026-11-16T18:50:00"))
    info = {"started": "2026-11-16T18:28:00", "finished": None, "seconds": 1500, "end_reason": None,
            "message": "", "settings": SETTINGS_LINE, "events": [],
            "samples": ["KP_1", "KP_2", "KP_3", "KP_4"], "current": "KP_2"}
    assert summary_report.write_run_summary(rdir, this_run=[done], left_out=[], run_info=info) == []
    page = _read(results.summary_html_path(rdir))
    parsed = parse(page)
    assert "The run is in progress: 1 of 4 samples finished so far." in parsed.words
    assert "Started 16 Nov 2026, 18:28. Running for 25 min 00 s so far." in parsed.words
    assert "rewritten after every sample" in parsed.words
    assert '<meta http-equiv="refresh" content="120">' in page
    assert parsed.row("KP_2")[1] == "Running now"
    assert parsed.row("KP_3")[1] == "Waiting" and parsed.row("KP_4")[1] == "Waiting"
    assert "1 Done" in parsed.words and "1 Running now" in parsed.words and "2 Waiting" in parsed.words
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    assert [(r["sample"], r["status"]) for r in rows] == [
        ("KP_1", "done"), ("KP_2", "running"), ("KP_3", "waiting"), ("KP_4", "waiting")]

    # The same list once the run has ended: nobody is "waiting" any more.
    info.update(finished="2026-11-16T19:00:00", end_reason=EndReason.ENVIRONMENT, current=None,
                message="Stopped: drive D: is full. Free some space, then press Continue. "
                        "The 1 finished sample is safe.")
    summary_report.write_run_summary(rdir, this_run=[done], left_out=[], run_info=info)
    page = _read(results.summary_html_path(rdir))
    parsed = parse(page)
    assert parsed.row("KP_2")[1] == "Not started" and "3 Not started" in parsed.words
    assert "Stopped: drive D: is full." in parsed.words and "tone-bad" in page
    assert "http-equiv" not in page


@pytest.mark.parametrize("reason, outcomes, expected", [
    ("completed", [RunStatus.DONE, RunStatus.DONE], ("tone-ok", "The run has finished: 2 done.")),
    ("completed", [RunStatus.DONE, RunStatus.FAILED], ("tone-warn", "The run has finished: 1 done, 1 failed.")),
    ("completed", [RunStatus.FAILED], ("tone-bad", "The run has finished: 1 failed.")),
    ("stopped", [RunStatus.DONE, RunStatus.STOPPED], ("tone-warn", "The run was stopped: 1 done, 1 stopped.")),
    ("environment", [RunStatus.DONE, RunStatus.NOT_STARTED],
     ("tone-bad", "The run was stopped by a problem with this PC or the results folder: 1 done, 1 not started.")),
])
def test_headline_when_the_caller_gives_no_message(rdir, reason, outcomes, expected):
    this_run = []
    for index, status in enumerate(outcomes):
        name = "KP_%d" % index
        if status is RunStatus.DONE:
            this_run.append(put_on_disk(rdir, good(name)))
        else:
            this_run.append(SampleOutcome(name=name, status=status, note="why", next_step="what"))
    summary_report.write_run_summary(rdir, this_run=this_run, left_out=[], run_info={
        "started": "2026-11-16T18:28:00", "finished": "2026-11-16T19:28:00", "seconds": 3600,
        "end_reason": reason})
    page = _read(results.summary_html_path(rdir))
    assert expected[0] in page and expected[1] in parse(page).words
    if all(status is RunStatus.DONE for status in outcomes):
        assert "Nothing. Every sample in this run has an assembly." in parse(page).words


def test_a_folder_with_no_run_in_hand_still_gets_its_summary(rdir):
    put_on_disk(rdir, good("KP_2"))
    put_on_disk(rdir, good("KP_10"))
    assert summary_report.write_run_summary(rdir, this_run=[], left_out=[], run_info=None) == []
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert "The results in this folder" in parsed.words
    assert [r[0] for r in parsed.rows if len(r) == 8][1:] == ["KP_2", "KP_10"]      # natural order
    os.makedirs(str(rdir) + " empty")
    assert summary_report.write_run_summary(str(rdir) + " empty", this_run=[], left_out=[], run_info={}) == []
    assert "No sample has been assembled in this folder yet." in \
        parse(_read(results.summary_html_path(str(rdir) + " empty"))).words


def test_the_last_word_on_a_sample_counts(rdir):
    """Failed, then "Try the failed samples again" worked: one row, done."""
    failed = SampleOutcome(name="KP_1", status=RunStatus.FAILED, note="no memory")
    done = put_on_disk(rdir, good("KP_1"))
    summary_report.write_run_summary(rdir, this_run=[failed, done], left_out=[],
                                     run_info={"finished": "2026-11-17T03:42:00"})
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert [r[:2] for r in parsed.rows if len(r) == 8][1:] == [["KP_1", "Done"]]
    assert "1 sample in this run" in parsed.words and "Failed" not in parsed.words


def test_a_new_result_that_could_not_be_put_in_place_keeps_its_numbers(rdir):
    """Audit row 5.6: the old result stays in place; the new one is in <sample>\\new_result."""
    put_on_disk(rdir, good("KP_1", contigs=80, finished_at="2026-11-03T15:40:00"))
    write_to = os.path.join(results.new_result_dir(rdir, "KP_1"), "assembly", "report.html")
    _write(write_to, "<html>new</html>")
    redo = good("KP_1", contigs=12, report_html=write_to, finished_at="2026-11-17T03:42:10",
                status=RunStatus.DONE_NOTES,
                note="Done, but the old result could not be replaced because one of its files is open in "
                     "another program. The new result is in KP_1\\new_result.")
    other = good("KP_2", contigs=33)             # no status file at all: its folder held foreign folders
    summary_report.write_run_summary(rdir, this_run=[redo, other], left_out=[],
                                     run_info={"finished": "2026-11-17T03:42:10"})
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert parsed.row("KP_1")[2] == "12" and "new_result" in parsed.row("KP_1")[6]
    assert "KP_1/new_result/assembly/report.html" in parsed.links
    assert parsed.row("KP_2")[1:3] == ["Done", "33"]
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    by_name = {r["sample"]: r for r in rows}
    assert by_name["KP_1"]["contigs"] == "80"                     # the row is what is in place...
    assert "new_result" in by_name["KP_1"]["note"]                # ...and the note says where the new one is
    assert by_name["KP_2"]["status"] == "done" and by_name["KP_2"]["contigs"] == "33"
    assert by_name["KP_2"]["N50"] == "214880" and by_name["KP_2"]["TesserACT_version"] == "v" + versions.TESSERACT_VERSION


def test_waiting_outcomes_handed_in_after_the_end_are_not_started(rdir):
    waiting = SampleOutcome(name="KP_1", status=RunStatus.WAITING)
    summary_report.write_run_summary(rdir, this_run=[waiting], left_out=[],
                                     run_info={"started": "2026-11-17T01:00:00", "finished": None})
    assert parse(_read(results.summary_html_path(rdir))).row("KP_1")[1] == "Waiting"
    summary_report.write_run_summary(rdir, this_run=[waiting], left_out=[],
                                     run_info={"started": "2026-11-17T01:00:00", "finished": "2026-11-17T03:42:10",
                                               "end_reason": "stopped"})
    assert parse(_read(results.summary_html_path(rdir))).row("KP_1")[1] == "Not started"
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    assert [(r["sample"], r["status"]) for r in rows] == [("KP_1", "not_started")]


def test_an_earlier_result_that_survived_a_failed_redo_says_so(rdir):
    put_on_disk(rdir, good("KP_1", finished_at="2026-11-03T15:40:00"))
    results.write_status(rdir, SampleOutcome(name="KP_1", status=RunStatus.FAILED, note="no memory",
                                             finished_at="2026-11-05T09:00:00"),
                         reads=[], settings_fingerprint="x", settings_text="x", trimmed=True)
    summary_report.write_run_summary(rdir, this_run=[], left_out=[], run_info=None)
    row = parse(_read(results.summary_html_path(rdir))).row("KP_1")
    assert row[1] == "Done"
    assert row[6] == ("Made on 3 Nov 2026 (standard trimming, standard assembly). "
                      "A later attempt (5 Nov 2026) failed; this result was kept.")


def test_two_left_out_samples_with_one_name_are_both_listed(rdir):
    reason = "Two samples are called KP_001. Rename one of them (double-click the name)."
    summary_report.write_run_summary(rdir, this_run=[], left_out=[("KP_001", reason), ("KP_001", reason)],
                                     run_info={"finished": "2026-11-17T03:42:00"})
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert len([r for r in parsed.rows if r and r[0] == "KP_001"]) == 2
    assert "2 Left out" in parsed.words and "2 samples were left out of this run" in parsed.words
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    assert [(r["sample"], r["status"]) for r in rows] == [("KP_001", "left_out")] * 2


# --------------------------------------------------------------------------- never in the way of a run

def test_summaries_are_replaced_whole_or_not_at_all(sample, monkeypatch):
    rdir, page, table = sample
    real_replace = os.replace

    class Crash(BaseException):
        pass

    def dying_replace(src, dst):
        raise Crash()

    monkeypatch.setattr(os, "replace", dying_replace)
    with pytest.raises(Crash):
        summary_report.write_run_summary(rdir, this_run=[], left_out=[], run_info={})
    monkeypatch.setattr(os, "replace", real_replace)
    assert _read(results.summary_html_path(rdir)) == page
    assert _read(results.summary_tsv_path(rdir)) == table


def test_a_summary_open_in_another_program_does_not_stop_the_other_one(sample, monkeypatch, caplog):
    """Audit row 9.13: run_summary.tsv is open in Excel."""
    rdir, page, table = sample
    monkeypatch.setattr(fsutil.time, "sleep", lambda seconds: None)
    real_replace = os.replace

    def locked(src, dst):
        if dst.endswith("run_summary.tsv"):
            raise PermissionError(13, "The process cannot access the file", dst)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", locked)
    extra = SampleOutcome(name="ZZ_99", status=RunStatus.FAILED, note="new failure")
    with caplog.at_level("ERROR"):
        not_written = summary_report.write_run_summary(rdir, this_run=[extra], left_out=[], run_info={})
    assert not_written == ["run_summary.tsv"]
    assert "run_summary.tsv could not be written" in caplog.text
    assert _read(results.summary_tsv_path(rdir)) == table               # the old one, untouched
    assert "ZZ_99" in _read(results.summary_html_path(rdir))            # the page was still renewed
    assert [n for n in os.listdir(rdir) if ".tmp" in n] == []


@pytest.mark.parametrize("this_run, left_out, run_info", [
    (None, None, None),
    ([], [], "not a dict"),
    (["not an outcome"], [], {}),
    ([], [("only one value",)], {}),
    ([], [], {"started": 5, "finished": object(), "seconds": "soon", "events": 7, "samples": 3}),
])
def test_bad_input_is_logged_not_raised(rdir, this_run, left_out, run_info, caplog):
    os.makedirs(rdir)
    with caplog.at_level("ERROR"):
        outcome = summary_report.write_run_summary(rdir, this_run=this_run, left_out=left_out,
                                                   run_info=run_info)
    assert isinstance(outcome, list)
    assert outcome == [] or caplog.text


def test_a_folder_that_is_gone_or_from_a_newer_program_is_reported_not_raised(rdir, tmp_path, caplog):
    blocker = tmp_path / "a file, not a folder"
    blocker.write_text("x")
    with caplog.at_level("ERROR"):
        assert summary_report.write_run_summary(str(blocker / "results"), this_run=[], left_out=[],
                                                run_info={}) == ["run_summary.tsv", "run_summary.html"]
    put_on_disk(rdir, good("KP_1"))
    data = fsutil.read_json(results.status_path(rdir, "KP_1"))
    data["format"] = versions.RESULTS_FORMAT + 1
    fsutil.atomic_write_json(results.status_path(rdir, "KP_1"), data)
    with caplog.at_level("ERROR"):
        assert summary_report.write_run_summary(rdir, this_run=[], left_out=[], run_info={}) == \
            ["run_summary.tsv", "run_summary.html"]
    assert not os.path.exists(results.summary_html_path(rdir))          # a newer folder is not touched
    assert "NewerFormat" in caplog.text


def test_500_samples_are_summarised_in_under_a_second(tmp_path):
    rdir = str(tmp_path / "big")
    this_run = []
    statuses = [RunStatus.DONE] * 7 + [RunStatus.DONE_NOTES, RunStatus.FAILED, RunStatus.SKIPPED]
    for index in range(500):
        name = "KP_%03d" % index
        status = statuses[index % 10]
        folder = results.sample_dir(rdir, name)
        if status is RunStatus.FAILED:
            this_run.append(SampleOutcome(name=name, status=status, note="A read file is incomplete or damaged.",
                                          next_step="Copy it again from the original, then try again.",
                                          finished_at="2026-11-17T03:42:10", seconds=60))
            continue
        outcome = good(name, status=RunStatus.DONE if status is RunStatus.SKIPPED else status,
                       contigs=50 + index, note="Low depth." if status is RunStatus.DONE_NOTES else "",
                       notes=["Low depth.", "Small assembly."] if status is RunStatus.DONE_NOTES else [])
        _write(os.path.join(folder, "assembly", "contigs.fasta"), ">c\nACGT\n")
        _write(os.path.join(folder, "assembly", "report.html"), "<html></html>")
        _write(os.path.join(folder, "trimming", "trimming_report.html"), "<html></html>")
        _write(os.path.join(rdir, "assemblies", name + ".fasta"), ">c\nACGT\n")
        results.write_status(rdir, outcome, reads=[("/r/%s_R1.fastq.gz" % name, 1), ("/r/%s_R2.fastq.gz" % name, 2)],
                             settings_fingerprint="standard", settings_text="standard trimming, standard assembly",
                             trimmed=True)
        this_run.append(SampleOutcome(name=name, status=RunStatus.SKIPPED) if status is RunStatus.SKIPPED
                        else outcome)
    info = {"started": "2026-11-16T18:28:00", "finished": "2026-11-17T03:42:00", "seconds": 33240,
            "end_reason": "completed", "message": "Finished.", "settings": SETTINGS_LINE, "events": [],
            "samples": ["KP_%03d" % i for i in range(500)]}
    summary_report.write_run_summary(rdir, this_run=this_run, left_out=[], run_info=info)    # warm
    best = 9.0
    for _ in range(3):
        started = time.perf_counter()
        assert summary_report.write_run_summary(rdir, this_run=this_run, left_out=[], run_info=info) == []
        best = min(best, time.perf_counter() - started)
    assert best < 1.0, "%.2f s for 500 samples" % best

    parsed = parse(_read(results.summary_html_path(rdir)))
    assert len([r for r in parsed.rows if len(r) == 8]) == 501
    assert "500 samples in this run" in parsed.words and "350 Done" in parsed.words
    assert "50 Failed" in parsed.words and "50 Skipped (already assembled)" in parsed.words
    assert "450 files, one per sample" in parsed.words
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    assert len(rows) == 500
    assert os.path.getsize(results.summary_html_path(rdir)) < 1024 * 1024


def test_the_sample_page_for_the_eye_has_twelve_mixed_rows_in_its_run(sample):
    _rdir, page, _table = sample
    parsed = parse(page)
    in_run = [r for r in parsed.rows if len(r) == 8][1:13]
    assert len({r[1] for r in in_run}) >= 6            # six different statuses among them


# --------------------------------------------------------------------------- added in review

def _rows(parsed):
    return [r for r in parsed.rows if len(r) == 8][1:]


def test_earlier_samples_show_all_their_notes_and_what_to_do(rdir):
    """Audit B.5 item 5 for samples that are in the folder from an earlier run: nothing else on the
    page lists their notes, so the table must, and no link may point at a section that is not there."""
    notes = ["Low depth (about 22x).", "The assembly is smaller than expected.", "Many reads carried adapters."]
    put_on_disk(rdir, good("KP_1", status=RunStatus.DONE_NOTES, note=notes[0], notes=list(notes),
                           finished_at="2026-11-03T15:02:00"))
    fail_on_disk(rdir, SampleOutcome(name="KP_2", status=RunStatus.FAILED, finished_at="2026-11-03T15:30:00",
                                     note="A read file is incomplete or damaged.",
                                     next_step="Copy it again from the original, then try again."))
    in_hand = [([], None), ([put_on_disk(rdir, good("KP_3"))], {"finished": "2026-11-17T03:42:00"})]
    for this_run, run_info in in_hand:
        assert summary_report.write_run_summary(rdir, this_run=this_run, left_out=[], run_info=run_info) == []
        parsed = parse(_read(results.summary_html_path(rdir)))        # parse() follows every "#" link
        cell = parsed.row("KP_1")[6]
        for note in notes:
            assert note in cell, note
        assert "Plus" not in cell
        failed = parsed.row("KP_2")
        assert failed[1] == "Failed" and "A read file is incomplete or damaged." in failed[6]
        assert "What to do: Copy it again from the original, then try again." in failed[6]
        assert "Tried on 3 Nov 2026." in failed[6]
        assert "KP_2/last_failed_attempt/assembly/assembly.log" in parsed.links


def test_one_odd_outcome_does_not_cost_the_whole_summary(rdir, caplog):
    """The page is rewritten after every sample: one value of the wrong kind must not blank it."""
    fine = put_on_disk(rdir, good("KP_1"))
    odd = [good("KP_2", contigs="71", total_length=5482113.0, n50=None, depth=float("nan")),
           good("KP_3", seconds=float("inf"), contigs=float("inf")),
           good("KP_4", notes=None, note=None, next_step=None, finished_at=None, status=RunStatus.DONE_NOTES),
           good("KP_5", seconds="soon", notes=["fine", 7, None], status=RunStatus.DONE_NOTES),
           SampleOutcome(name="KP_6", status="failed", note="given as text"),
           "not an outcome", None, 5]
    info = {"started": "2026-11-16T18:28:00", "finished": "2026-11-17T03:42:00", "seconds": float("inf"),
            "end_reason": "completed", "events": ["fine", None, 7], "samples": ["KP_1", 7, None]}
    with caplog.at_level("ERROR"):
        assert summary_report.write_run_summary(rdir, this_run=[fine] + odd, left_out=[], run_info=info) == []
    assert "not an outcome" in caplog.text                          # what was dropped is in the log
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert [r[0] for r in _rows(parsed)] == ["KP_1", "KP_2", "KP_3", "KP_4", "KP_5", "KP_6"]
    assert parsed.row("KP_1")[1:6] == ["Done", "71", "5,482,113", "214,880", "63×"]
    assert parsed.row("KP_2")[1:6] == ["Done", "71", "5,482,113", "", ""]
    assert parsed.row("KP_3")[1:3] == ["Done", ""]
    assert parsed.row("KP_5")[6] == "fine"
    assert parsed.row("KP_6")[1] == "Failed" and "given as text" in parsed.words
    assert "Finished 17 Nov 2026, 03:42." in parsed.words
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    by_name = {r["sample"]: r for r in rows}
    assert sorted(by_name) == ["KP_1", "KP_2", "KP_3", "KP_4", "KP_5", "KP_6"]
    assert (by_name["KP_2"]["contigs"], by_name["KP_2"]["total_length"], by_name["KP_2"]["depth"]) == \
        ("71", "5482113", "")
    assert by_name["KP_3"]["seconds"] == "" and by_name["KP_3"]["contigs"] == ""
    for row in rows:
        for column in ("contigs", "total_length", "largest", "N50", "depth", "seconds"):
            assert re.fullmatch(r"\d*", row[column]), (row["sample"], column, row[column])


@pytest.mark.parametrize("left_out", [[("only one value",)], ["just a name"], [("EC_1", "why", "more")], [7, None]])
def test_odd_left_out_entries_do_not_cost_the_summary_either(rdir, left_out):
    fine = put_on_disk(rdir, good("KP_1"))
    assert summary_report.write_run_summary(rdir, this_run=[fine], left_out=left_out,
                                            run_info="not a dict") == []
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert parsed.row("KP_1")[1] == "Done"
    assert len(_rows(parsed)) == 1 + len([item for item in left_out if isinstance(item, (str, tuple))])


def test_a_damaged_status_file_does_not_cost_the_whole_summary(rdir):
    """One sample's file holds a number no number can hold; the other samples keep their summary."""
    import json

    put_on_disk(rdir, good("KP_1"))
    put_on_disk(rdir, good("KP_2"))
    path = results.status_path(rdir, "KP_2")
    data = fsutil.read_json(path)
    data["outcome"].update(seconds=float("inf"), contigs=float("inf"))
    data["reads"] = 5
    _write(path, json.dumps(data))                      # "Infinity", as a damaged or foreign writer leaves it
    assert summary_report.write_run_summary(rdir, this_run=[], left_out=[], run_info=None) == []
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert parsed.row("KP_1")[2] == "71" and parsed.row("KP_2")[1:3] == ["Done", ""]
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    assert [(r["sample"], r["contigs"], r["seconds"]) for r in rows] == [("KP_1", "71", "1392"), ("KP_2", "", "")]


def test_a_name_that_is_left_out_is_not_also_counted_as_waiting(rdir):
    """A caller may list every row of the Samples page under "samples"; each is counted once."""
    left_out = [("EC_1", "No Read 2 file found.")]
    also_as_outcome = SampleOutcome(name="EC_1", status=RunStatus.LEFT_OUT, note="No Read 2 file.")
    for this_run in ([], [also_as_outcome]):
        for finished, words in ((None, "Waiting"), ("2026-11-17T03:42:00", "Not started")):
            info = {"started": "2026-11-16T18:28:00", "finished": finished, "samples": ["KP_1", "EC_1"]}
            summary_report.write_run_summary(rdir, this_run=this_run, left_out=left_out, run_info=info)
            parsed = parse(_read(results.summary_html_path(rdir)))
            assert [r[:2] for r in _rows(parsed)] == [["KP_1", words], ["EC_1", "Left out"]]
            assert "2 samples in this run" in parsed.words and "1 Left out" in parsed.words
            _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
            assert [r["sample"] for r in rows] == ["EC_1", "KP_1"]
            assert rows[0]["status"] == "left_out" and rows[0]["note"] == "No Read 2 file found."


def test_a_failed_redo_links_to_the_log_that_was_kept(rdir):
    """The outcome names the log inside _in_progress, which is gone; its copy is what to open."""
    put_on_disk(rdir, good("KP_1", finished_at="2026-11-03T15:40:00"))
    results.begin_attempt(rdir, "KP_1")
    log = os.path.join(results.in_progress_assembly_dir(rdir, "KP_1"), "assembly.log")
    _write(log, "error: out of memory\n")
    failed = SampleOutcome(name="KP_1", status=RunStatus.FAILED, note="This sample needs more memory.",
                           next_step="Close other programs.", log=log, kept_earlier_result=True)
    results.abandon_attempt(rdir, "KP_1", keep_logs=True)
    results.write_status(rdir, failed, reads=[], settings_fingerprint="x", settings_text="x", trimmed=True)
    assert not os.path.exists(log)
    summary_report.write_run_summary(rdir, this_run=[failed], left_out=[], run_info={"finished": "2026-11-17T03:42:00"})
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert "KP_1/last_failed_attempt/assembly/assembly.log" in parsed.links
    assert parsed.row("KP_1")[7] == "ReportLogTrimmingFolder"
    assert "Open the log" in parsed.words


def test_two_threads_writing_the_summary_at_once_both_succeed(sample):
    """The batch thread after a sample, the window on "Write the summary again"."""
    import threading

    rdir, _page, table = sample
    this_run, left_out, run_info = [], [("EC_09", "No Read 2 file found.")], {"finished": "2026-11-16T21:01:00"}
    outcomes, problems = [], []

    def write_many():
        for _ in range(15):
            try:
                outcomes.append(summary_report.write_run_summary(rdir, this_run=this_run, left_out=left_out,
                                                                 run_info=run_info))
                parse(_read(results.summary_html_path(rdir)))
                assert tsv_rows(_read(results.summary_tsv_path(rdir)))[0] == list(summary_report.TSV_COLUMNS)
            except BaseException as exc:            # noqa: BLE001
                problems.append(repr(exc)[:200])

    threads = [threading.Thread(target=write_many) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert problems == []
    assert outcomes == [[]] * 75
    assert [n for n in os.listdir(rdir) if ".tmp" in n] == []


def test_a_folder_spelled_in_other_capitals_is_the_same_sample_on_windows(rdir, monkeypatch):
    """Windows does not tell KP_1 from kp_1: the sample must not be listed twice."""
    put_on_disk(rdir, good("kp_1", contigs=64, finished_at="2026-11-03T15:02:00"))
    skipped = SampleOutcome(name="KP_1", status=RunStatus.SKIPPED)
    info = {"finished": "2026-11-17T03:42:00", "samples": ["KP_1"]}

    # Where capitals count (Linux), they are two samples.
    if os.path.normcase("A") == "A":
        summary_report.write_run_summary(rdir, this_run=[skipped], left_out=[], run_info=info)
        assert [r[0] for r in _rows(parse(_read(results.summary_html_path(rdir))))] == ["KP_1", "kp_1"]

    monkeypatch.setattr(os.path, "normcase", lambda path: path.lower())      # what it does on Windows
    summary_report.write_run_summary(rdir, this_run=[skipped], left_out=[], run_info=info)
    monkeypatch.undo()
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert [r[:3] for r in _rows(parsed)] == [["KP_1", "Skipped (already assembled)", "64"]]
    assert "kp_1/assembly/report.html" in parsed.links                       # the folder as it is spelled on disk
    assert "Already in this folder from earlier runs" not in parsed.words
    _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
    assert [(r["sample"], r["status"], r["contigs"]) for r in rows] == [("kp_1", "done", "64")]


def test_a_time_that_cannot_be_shown_is_left_out_not_printed_raw(rdir):
    fine = put_on_disk(rdir, good("KP_1"))
    info = {"started": 5, "finished": object(), "seconds": "soon", "events": 7, "samples": 3}
    assert summary_report.write_run_summary(rdir, this_run=[fine], left_out=[], run_info=info) == []
    page = _read(results.summary_html_path(rdir))
    parsed = parse(page)
    assert "object at 0x" not in page and "Started 5" not in parsed.words
    assert "The run has finished: 1 done." in parsed.words and "http-equiv" not in page


def test_any_iterable_is_taken_and_a_name_given_twice_is_one_row(rdir):
    fine = put_on_disk(rdir, good("KP_1"))
    info = {"finished": "2026-11-17T03:42:00", "samples": ("KP_1", "KP_2", "KP_1", "KP_2"),
            "events": (e for e in ["The PC was asleep from 22:14 to 07:31."])}
    assert summary_report.write_run_summary(rdir, this_run=iter([fine]), left_out={"EC_1": "No Read 2 file found."},
                                            run_info=info) == []
    parsed = parse(_read(results.summary_html_path(rdir)))
    assert [r[:2] for r in _rows(parsed)] == [["KP_1", "Done"], ["KP_2", "Not started"], ["EC_1", "Left out"]]
    assert "3 samples in this run" in parsed.words and "The PC was asleep from 22:14 to 07:31." in parsed.words
    assert parsed.row("EC_1")[6] == "No Read 2 file found."


def test_a_sample_not_reached_keeps_the_links_to_its_earlier_assembly(rdir):
    """Overwrite was chosen and the run was stopped before this sample: its old result still counts."""
    put_on_disk(rdir, good("KP_2", contigs=64, finished_at="2026-11-03T15:02:00"))
    first = put_on_disk(rdir, good("KP_1"))
    info = {"started": "2026-11-16T18:28:00", "finished": None, "samples": ["KP_1", "KP_2", "KP_3"]}
    for finished, words in ((None, "Waiting"), ("2026-11-16T21:01:00", "Not started")):
        info["finished"] = finished
        summary_report.write_run_summary(rdir, this_run=[first], left_out=[], run_info=info)
        parsed = parse(_read(results.summary_html_path(rdir)))
        row = parsed.row("KP_2")
        assert row[1:3] == [words, ""]                       # nothing of this run to show yet
        assert row[6] == "The earlier assembly (3 Nov 2026) is still in place."
        assert row[7] == "ReportTrimmingFolder"
        assert parsed.row("KP_3")[6:] == ["", ""]
        assert "Already in this folder from earlier runs" not in parsed.words
        _header, rows = tsv_rows(_read(results.summary_tsv_path(rdir)))
        assert [(r["sample"], r["status"], r["contigs"]) for r in rows] == [
            ("KP_1", "done", "71"), ("KP_2", "done", "64"), ("KP_3", words.lower().replace(" ", "_"), "")]


def test_no_results_folder_given_writes_nothing_anywhere(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    for nowhere in ("", None):
        with caplog.at_level("ERROR"):
            assert summary_report.write_run_summary(nowhere, this_run=[], left_out=[], run_info={}) == \
                ["run_summary.tsv", "run_summary.html"]
    assert os.listdir(str(tmp_path)) == [] and "no results folder" in caplog.text
    # A folder given as a path object is as good as one given as text.
    assert summary_report.write_run_summary(tmp_path / "as a Path", this_run=[], left_out=[], run_info={}) == []
    assert sorted(os.listdir(str(tmp_path / "as a Path"))) == ["run_summary.html", "run_summary.tsv"]


# --------------------------------------------------------------------------- the page for the eye

def main(argv):
    target = argv[1] if len(argv) > 1 else os.path.join("build", "shots")
    work = tempfile.mkdtemp(prefix="tesseract-win-sample-")
    try:
        rdir = os.path.join(work, "TesserACT_results")
        page, _table = write_sample(rdir)
        os.makedirs(target, exist_ok=True)
        out = os.path.join(target, "run_summary_sample.html")
        with open(out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(page)
        print(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
