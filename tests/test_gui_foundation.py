# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Tests of the look and the building blocks of the window (gui/theme, icons, texts, widgets).

The first half needs no display: the words, the palettes, the number-field rules, the
shortening of names, the pictures. The second half carries the ``gui`` marker and opens
real Tk windows; it is skipped when no display is available.

Which display: ``TESSERACT_WIN_TEST_DISPLAY`` if set; otherwise, on Linux, the headless
display ``:99`` when it is running (so that a test run does not flash windows on the
desktop of whoever is working at the PC); otherwise ``DISPLAY``.

    python -m pytest tests/test_gui_foundation.py -q
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import os
import re
import struct
import subprocess
import sys
import time
import zlib

import pytest

from tesseract_win import branding, model
from tesseract_win.gui import icons, texts, theme
from tesseract_win.gui import widgets as w

GUI_DIR = os.path.dirname(os.path.abspath(theme.__file__))
REPO = os.path.dirname(os.path.dirname(GUI_DIR))


# =========================================================================== the words

ALL_HOVER = {}
for _name in ("TRIM_HOVER", "ASSEMBLY_HOVER", "MODE_HOVER", "HOVER"):
    for _key, _value in getattr(texts, _name).items():
        ALL_HOVER["%s[%s]" % (_name, _key)] = _value


def _strings(value):
    """Every string inside a module-level value of texts."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _strings(item)


def all_texts():
    found = []
    for name in dir(texts):
        if name.startswith("_") or name in ("annotations",):
            continue
        value = getattr(texts, name)
        if isinstance(value, (str, dict, tuple, list)):
            found.extend(_strings(value))
    return found


def sentences(text):
    """Number of sentences: ends at . ? ! followed by a space and a capital, or the end."""
    cleaned = text.replace("...", "\u2026")
    return len(re.findall(r"[.?!](?=\s+[A-Z0-9(]|\)?$)", cleaned))


def test_hover_text_for_every_trimming_field():
    names = [f.name for f in dataclasses.fields(model.TrimSettings)]
    assert sorted(texts.TRIM_HOVER) == sorted(names)
    assert sorted(texts.TRIM_LABEL) == sorted(names)
    for name in names:
        assert texts.TRIM_HOVER[name].strip()
        assert texts.TRIM_LABEL[name].strip()


def test_hover_text_for_every_assembly_field():
    names = [f.name for f in dataclasses.fields(model.AssemblySettings)]
    assert sorted(texts.ASSEMBLY_HOVER) == sorted(names)
    assert sorted(texts.ASSEMBLY_LABEL) == sorted(names)
    for name in names:
        assert texts.ASSEMBLY_HOVER[name].strip()
        assert texts.ASSEMBLY_LABEL[name].strip()


def test_label_and_hover_for_every_assembly_mode():
    assert tuple(texts.MODE_LABEL) == model.ASSEMBLY_MODES
    assert sorted(texts.MODE_HOVER) == sorted(model.ASSEMBLY_MODES)
    assert "recommended" in texts.MODE_LABEL["standard"]


@pytest.mark.parametrize("enum_class, labels", [
    (model.SampleState, texts.SAMPLE_STATE_LABEL),
    (model.RunStatus, texts.RUN_STATUS_LABEL),
    (model.Stage, texts.STAGE_LABEL),
    (model.StopMode, texts.STOP_MODE_LABEL),
    (model.Existing, texts.EXISTING_LABEL),
    (model.EndReason, texts.END_REASON_LABEL),
])
def test_label_for_every_enum_value(enum_class, labels):
    assert issubclass(enum_class, enum.Enum)
    for member in enum_class:
        assert member in labels, "no label for %s" % member
        assert labels[member].strip()
    assert len(labels) == len(list(enum_class))


def test_status_words_are_the_agreed_ones():
    assert texts.RUN_STATUS_LABEL[model.RunStatus.DONE_NOTES] == "Done (check notes)"
    assert texts.RUN_STATUS_LABEL[model.RunStatus.SKIPPED] == "Skipped (already assembled)"
    assert texts.RUN_STATUS_LABEL[model.RunStatus.SUMMARISING] == "Summarising"       # British spelling
    assert texts.STAGE_LABEL[model.Stage.SUMMARISE] == "Summarise"
    assert [texts.STAGE_LABEL[s] for s in model.Stage] == [
        "Join lanes", "Trim", "Check reads", "Assemble", "Summarise"]
    assert texts.SAMPLE_STATE_LABEL[model.SampleState.LEFT_OUT] == "Left out"


def test_every_status_has_a_colour_kind():
    kinds = {"ok", "warn", "bad", "accent", "muted"}
    for member in list(model.SampleState) + list(model.RunStatus):
        assert w.status_kind(member) in kinds
        assert w.status_kind(member.value) == w.status_kind(member)
    assert w.status_kind(model.RunStatus.FAILED) == "bad"
    assert w.status_kind(model.RunStatus.DONE) == "ok"
    assert w.status_kind(model.RunStatus.DONE_NOTES) == "warn"
    assert w.status_kind(model.RunStatus.ASSEMBLING) == "accent"
    for member in model.RunStatus:
        if member.is_running:
            assert w.status_kind(member) == "accent"


@pytest.mark.parametrize("name", sorted(ALL_HOVER))
def test_hover_text_is_one_or_two_plain_sentences(name):
    text = ALL_HOVER[name]
    assert text == text.strip() and text[-1] in ".?!", text
    assert 1 <= sentences(text) <= 2, "%d sentences: %s" % (sentences(text), text)
    assert len(text) <= 250, "too long for two lines of the help line (%d): %s" % (len(text), text)
    assert not re.search(r"(^|\s)--?[a-zA-Z]", text), "looks like a flag name: " + text
    assert "_" not in text, "looks like an option name: " + text


def test_no_jargon_and_no_flags_anywhere():
    banned = re.compile(r"\b(mates?|phred|k-?mers?|fastp|scepter|preflight|postflight|stderr|exit code)\b",
                        re.IGNORECASE)
    for text in all_texts():
        assert not banned.search(text), text
        assert not re.search(r"(^|\s)--[a-z]", text), text


def test_british_spelling():
    joined = " ".join(all_texts())
    for american in ("Summarize", "summarize", "summarizing", "organization", " color", "Canceled"):
        assert american not in joined
    assert "Summarise" in joined


def test_steps_and_pages():
    assert [key for key, _name in texts.STEPS] == ["samples", "trimming", "assembly", "run"]
    for key, name in texts.STEPS:
        assert texts.PAGE_TITLE[key] == name
        assert texts.PAGE_INTRO[key].strip()
    assert texts.STEP_STANDARD == "standard" and texts.STEP_CHANGED == "changed"
    assert texts.step_ready(40) == "40 ready"
    assert texts.next_label("trimming") == "Next: Trimming"


def test_table_columns():
    assert list(texts.SAMPLES_COLUMNS.values()) == ["Trim", "Sample", "Read 1", "Read 2", "Size", "Status"]
    assert list(texts.RUN_COLUMNS.values()) == ["Sample", "Status", "Contigs", "Total length", "N50", "Notes"]
    for key in list(texts.SAMPLES_COLUMNS) :
        assert ("%s_column" % key) in texts.HOVER
    for key in ("contigs", "total_length", "n50", "notes"):
        assert ("%s_column" % key) in texts.HOVER


def test_start_button_words():
    assert texts.start_label(0) == "Start"
    assert texts.start_label(1) == "Start (1 sample)"
    assert texts.start_label(38) == "Start (38 samples)"
    assert texts.start_label(1200) == "Start (1,200 samples)"
    assert texts.start_reason(0, 0, False) == "Add samples to begin."
    assert texts.start_reason(4, 0, True) == "No sample is ready: see the notes in the list."
    assert texts.start_reason(4, 3, False) == "Choose a results folder to begin."
    assert texts.start_reason(4, 3, True) == ""
    assert texts.ready_counter(38, 2) == ("38 ready.", "2 left out: why?")
    assert texts.ready_counter(38, 0) == ("38 ready.", "")


def test_sentences_with_numbers():
    assert texts.count(1, "sample") == "1 sample" and texts.count(3, "sample") == "3 samples"
    assert texts.number(5482113) == "5,482,113"
    assert texts.minutes(20) == "less than a minute"
    assert texts.minutes(60) == "1 minute"
    assert texts.minutes(14 * 60 + 59) == "14 minutes"
    assert texts.minutes(2 * 3600 + 5 * 60) == "2 hours 5 minutes"
    assert texts.sample_header(13, 40, "KP_013") == "Sample 13 of 40: KP_013"
    assert texts.clocks("06:41", "2 h 14 min") == "This sample 06:41. Whole run 2 h 14 min."
    assert texts.time_left(None) == texts.ESTIMATE_PENDING
    assert texts.time_left(4 * 3600) == "About 4 hours left (rough estimate)."
    assert texts.time_left(25 * 60) == "About 25 minutes left (rough estimate)."
    assert texts.working_line(7.6, "3.1 GB") == "Working: 8 processor threads busy, 3.1 GB of memory in use"
    assert texts.settings_line(True, True, 8) == "Settings: standard trimming, standard assembly, 8 threads."
    assert texts.settings_line(False, False, 1) == "Settings: changed trimming, changed assembly, 1 thread."
    assert texts.settings_line(None, True, 4).startswith("Settings: no trimming,")
    assert texts.space_line("61 GB", "D:", "212 GB") == "Needs about 61 GB. Free on D: 212 GB."
    assert texts.existing_line(12) == "12 of these samples already have results here."
    assert texts.existing_line(12, 2).endswith("2 failed last time and will be tried again.")
    assert texts.existing_line(0) == ""


def test_number_field_message():
    assert texts.number_reset(0, 40, 10) == "Use a whole number from 0 to 40. Set back to 10."
    assert texts.number_reset(1, 100000, 500) == "Use a whole number from 1 to 100,000. Set back to 500."
    assert texts.number_reset(0.1, 20, 5.0, decimal=True) == "Use a number from 0.1 to 20. Set back to 5.0."


def test_stop_question():
    q = texts.stop_question("KP_002", model.RunStatus.ASSEMBLING, 14 * 60, 12)
    assert q.title == "Stop the run?"
    assert [c.key for c in q.choices] == ["keep", model.StopMode.AFTER_CURRENT.value, model.StopMode.NOW.value]
    assert [c.label for c in q.choices] == ["Keep running", "Stop after KP_002", "Stop now"]
    assert q.default == "keep" and q.cancel == "keep"          # Esc and Enter both keep it running
    assert q.choice("now").kind == "danger"
    assert "KP_002 is being assembled (14 minutes so far)." in q.body
    assert "The 12 samples already finished are kept." in q.body
    one = texts.stop_question("A", model.RunStatus.TRIMMING, 5, 1)
    assert "is being trimmed (less than a minute so far)" in one.body
    assert "The 1 sample already finished is kept." in one.body
    none = texts.stop_question("A", model.RunStatus.CHECKING_READS, 5, 0)
    assert "finished" not in none.body.split("\n")[-1] or "No other sample" in none.body


def test_close_question():
    q = texts.close_question("KP_002", model.RunStatus.ASSEMBLING, 14 * 60, 12)
    assert branding.APP_NAME in q.title
    assert [c.key for c in q.choices] == ["keep", "stop_and_close"]
    assert q.default == "keep" and q.cancel == "keep"
    assert "that work would be lost" in q.body and "12 finished samples are kept" in q.body


def test_overwrite_question_some():
    names = ["KP_%03d" % i for i in range(12)]
    q = texts.overwrite_question(12, 40, 8, "not trimmed; mode Careful", names)
    assert q.title == "Some samples are already assembled"
    assert [c.key for c in q.choices] == ["skip", "overwrite", "cancel"]
    assert q.default == model.Existing.SKIP.value          # a wrong Skip costs a click, a wrong Overwrite hours
    assert q.cancel == "cancel"
    assert "12 of the 40 samples already have an assembly" in q.body
    assert "8 of them were made with different settings (not trimmed; mode Careful)." in q.body
    assert q.choice("skip").description == "Keep the existing results. Assemble only the other 28."
    assert "replaced when its new one is ready" in q.choice("overwrite").description
    assert q.details_label == "Show the 12 samples" and q.details.split("\n") == names
    assert model.Existing(q.choice("skip").key) is model.Existing.SKIP
    assert model.Existing(q.choice("overwrite").key) is model.Existing.OVERWRITE


def test_overwrite_question_all():
    q = texts.overwrite_question(40, 40)
    assert [c.key for c in q.choices] == ["overwrite", "cancel"]     # Skip would do nothing: not offered
    assert q.default == "cancel" and q.cancel == "cancel"
    assert q.body.startswith("All 40 samples already have an assembly")
    one = texts.overwrite_question(1, 1)
    assert "This sample already has an assembly" in one.body


def test_texts_does_not_need_tk():
    code = ("import sys; import tesseract_win.gui.texts; "
            "print(any(m == 'tkinter' or m.startswith('tkinter.') or m == '_tkinter' for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_gui_modules_do_not_load_pillow_or_messagebox():
    code = ("import sys; import tesseract_win.gui.theme, tesseract_win.gui.widgets, tesseract_win.gui.icons; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('PIL', 'tkinterdnd2') "
            "or m == 'tkinter.messagebox'))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_pillow_is_imported_only_inside_make_icon():
    path = os.path.join(GUI_DIR, "icons.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    owners = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in inner.names] + [getattr(inner, "module", "") or ""]
                    if any(n.split(".")[0] == "PIL" for n in names):
                        owners.append(node.name)
    assert owners == ["make_icon"]
    for node in tree.body:                                   # nothing at module level
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert not any(n.split(".")[0] == "PIL" for n in names)
    for name in ("theme.py", "widgets.py", "texts.py", "__init__.py"):
        with open(os.path.join(GUI_DIR, name), encoding="utf-8") as handle:
            source = handle.read()
        assert "PIL" not in source and "import messagebox" not in source and "tkinter.messagebox" not in source.replace(
            "``tkinter.messagebox``", "")


def test_every_file_has_the_two_header_lines():
    files = [os.path.join(GUI_DIR, n) for n in ("__init__.py", "theme.py", "widgets.py", "texts.py", "icons.py")]
    files += [os.path.join(REPO, "tools", "gui_gallery.py"), os.path.abspath(__file__)]
    for path in files:
        with open(path, encoding="utf-8") as handle:
            first, second = handle.readline().rstrip("\n"), handle.readline().rstrip("\n")
        assert first == "# SPDX-License-Identifier: MIT", path
        assert second == "# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin", path


# =========================================================================== the palettes

HOUSE_LIGHT = dict(
    bg="#ecf0f6", surface="#ffffff", surface_alt="#e3e8f0", surface_sunk="#f6f8fc", border="#c9d2e0",
    border_strong="#a5b1c4", text="#121820", text_soft="#3d4754", muted="#5b6573", accent="#2563eb",
    accent_hover="#3b76ee", accent_active="#1d4ed8", accent_soft="#e9f0fe", on_accent="#ffffff",
    ok="#0f7032", warn="#8a5300", bad="#b42318", ok_soft="#e7f5ec", warn_soft="#fdf3e2",
    bad_soft="#fdeceb", focus="#1d4ed8", row_alt="#f6f8fc")
HOUSE_DARK = dict(
    bg="#111419", surface="#191d24", surface_alt="#232a36", surface_sunk="#14181e", border="#38424f",
    border_strong="#4d5868", text="#e9edf4", text_soft="#c3cbd7", muted="#98a3b3", accent="#6ea0ff",
    accent_hover="#8db4ff", accent_active="#4d87f5", accent_soft="#1d2836", on_accent="#0b1220",
    ok="#4ade80", warn="#fbbf24", bad="#fb7185", ok_soft="#14261c", warn_soft="#2a2113",
    bad_soft="#2c1619", focus="#8db4ff", row_alt="#1d222a")
PALETTES = [theme.LIGHT, theme.DARK]


def test_palettes_are_the_house_palettes():
    assert theme.LIGHT.as_dict() == HOUSE_LIGHT
    assert theme.DARK.as_dict() == HOUSE_DARK
    assert theme.LIGHT.name == "light" and theme.DARK.name == "dark"


def test_contrast_function():
    assert theme.contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert theme.contrast("#ffffff", "#000000") == pytest.approx(21.0)
    assert theme.contrast("#777777", "#777777") == pytest.approx(1.0)
    assert theme.relative_luminance("#ffffff") == pytest.approx(1.0)


@pytest.mark.parametrize("palette", PALETTES, ids=lambda p: p.name)
@pytest.mark.parametrize("ink", ["text", "text_soft", "muted"])
@pytest.mark.parametrize("ground", ["surface", "bg", "surface_sunk", "row_alt"])
def test_text_contrast_is_at_least_4_5(palette, ink, ground):
    ratio = theme.contrast(palette[ink], palette[ground])
    assert ratio >= 4.5, "%s on %s in %s: %.2f" % (ink, ground, palette.name, ratio)


@pytest.mark.parametrize("palette", PALETTES, ids=lambda p: p.name)
def test_coloured_words_and_buttons_are_readable(palette):
    for ink in ("ok", "warn", "bad", "accent"):
        for ground in ("surface", "bg", "row_alt", "accent_soft"):
            assert theme.contrast(palette[ink], palette[ground]) >= 4.5, (ink, ground)
    for ink, ground in (("ok", "ok_soft"), ("warn", "warn_soft"), ("bad", "bad_soft"), ("accent", "accent_soft"),
                        ("text", "ok_soft"), ("text", "warn_soft"), ("text", "bad_soft"), ("text", "accent_soft"),
                        ("on_accent", "accent"), ("on_accent", "accent_active")):
        assert theme.contrast(palette[ink], palette[ground]) >= 4.5, (ink, ground)
    # the keyboard focus ring must stand out from what it is drawn on (3:1 for non-text marks)
    for ground in ("surface", "bg"):
        assert theme.contrast(palette.focus, palette[ground]) >= 3.0


def test_grounds_and_style_names():
    assert theme.GROUNDS["surface"] == ""
    assert set(theme.GROUNDS) == {"surface", "page", "sunk", "alt", "info", "ok", "warn", "bad"}
    assert len(set(theme.GROUNDS.values())) == len(theme.GROUNDS)


def test_pick_family():
    have = ["DejaVu Sans", "segoe ui", "Noto Sans"]
    assert theme.pick_family(have, theme.TEXT_FAMILIES) == "segoe ui"       # capitals do not matter
    assert theme.pick_family(["Noto Sans", "DejaVu Sans"], theme.TEXT_FAMILIES) == "Noto Sans"
    assert theme.pick_family(["Courier"], theme.MONO_FAMILIES, "fallback") == "fallback"
    assert theme.TEXT_FAMILIES[:2] == ("Segoe UI Variable Text", "Segoe UI")
    assert theme.TITLE_FAMILIES[:2] == ("Segoe UI Variable Display", "Segoe UI Semibold")
    assert theme.MONO_FAMILIES == ("Cascadia Mono", "Consolas", "DejaVu Sans Mono")
    assert theme.FONT_SIZES["small"] == 9 and theme.FONT_SIZES["body"] == 10
    assert theme.FONT_SIZES["heading"] == 11 and theme.FONT_SIZES["title"] == 16


# =========================================================================== numbers

@pytest.mark.parametrize("text, expected", [
    ("10", 10), (" 10 ", 10), ("0", 0), ("+7", 7), ("-3", -3), ("007", 7),
    ("", None), ("abc", None), ("1e3", None), ("5.5", None), ("5,5", None), ("1 000", None),
    ("0x10", None), ("--3", None), ("3-", None), ("١٢", 12),
])
def test_parse_whole_number(text, expected):
    assert w.parse_number(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("5.5", 5.5), ("5,5", 5.5), ("5", 5.0), (" 2,8 ", 2.8), (".5", 0.5), (",5", 0.5), ("5.", 5.0),
    ("5,", 5.0), ("-1.5", -1.5), ("", None), (".", None), (",", None), ("5.5.5", None), ("5,5,5", None),
    ("1.234,5", None), ("1e3", None), ("five", None), ("5 .5", None),
])
def test_parse_decimal_number(text, expected):
    value = w.parse_number(text, decimal=True)
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected)


def test_settle_whole_number_in_range():
    assert w.settle_number("25", 0, 40, 10) == (25, "")
    assert w.settle_number("0", 0, 40, 10) == (0, "")            # both ends belong to the range
    assert w.settle_number("40", 0, 40, 10) == (40, "")
    value, message = w.settle_number(" 12 ", 0, 40, 10)
    assert value == 12 and isinstance(value, int) and message == ""


@pytest.mark.parametrize("text", ["41", "-1", "", "abc", "12.5", "1e1", "999999999999"])
def test_settle_whole_number_goes_back_to_the_last_good_value(text):
    value, message = w.settle_number(text, 0, 40, 10)
    assert value == 10
    assert message == "Use a whole number from 0 to 40. Set back to 10."


def test_settle_decimal_number():
    assert w.settle_number("5.5", 0.1, 20, 5.0, decimal=True) == (5.5, "")
    assert w.settle_number("5,5", 0.1, 20, 5.0, decimal=True) == (5.5, "")     # comma from the number pad
    assert w.settle_number("0.1", 0.1, 20, 5.0, decimal=True) == (0.1, "")
    assert w.settle_number("20", 0.1, 20, 5.0, decimal=True) == (20.0, "")
    assert w.settle_number("2.84", 0.1, 20, 5.0, decimal=True)[0] == pytest.approx(2.8)   # one decimal place
    for bad in ("0", "0.04", "20.1", "25", "-2", "x", ""):
        value, message = w.settle_number(bad, 0.1, 20, 5.0, decimal=True)
        assert value == 5.0
        assert message == "Use a number from 0.1 to 20. Set back to 5.0."


def test_format_number():
    assert w.format_number(500) == "500"
    assert w.format_number(5.0, decimal=True) == "5.0"
    assert w.format_number(5.55, decimal=True, decimals=2) == "5.55"
    assert w.format_number(100000) == "100000"                   # no thousands marks inside a field


# =========================================================================== shortening

def test_shorten_middle_keeps_both_ends():
    name = "KP_002_S2_L001_R1_001.fastq.gz"
    short = w.shorten_middle(name, 20)
    assert len(short) == 20
    assert short.count(w.ELLIPSIS) == 1
    head, tail = short.split(w.ELLIPSIS)
    assert name.startswith(head) and name.endswith(tail)
    assert len(tail) >= len(head) and len(tail) - len(head) <= 1     # the end gets the odd character
    assert tail.endswith(".fastq.gz")


def test_shorten_middle_leaves_short_text_alone():
    assert w.shorten_middle("KP_001", 20) == "KP_001"
    assert w.shorten_middle("exactly-20-characters", 21) == "exactly-20-characters"
    assert w.shorten_middle("", 5) == ""


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 9, 10, 29])
def test_shorten_middle_never_exceeds_the_limit(limit):
    name = "Klebsiella_pneumoniae_ward7_isolate_2026_0041_R1.fastq.gz"
    short = w.shorten_middle(name, limit)
    assert len(short) <= max(limit, 1)
    assert w.ELLIPSIS in short


def test_shorten_to_width_with_real_widths():
    widths = {"i": 3, "W": 11, "\u2026": 9}
    measure = lambda ch: widths.get(ch, 7)                       # noqa: E731
    text = "WWWWiiiiWWWWiiiiWWWW"
    short = w.shorten_to_width(text, 60, measure)
    assert sum(measure(ch) for ch in short) <= 60
    assert short.startswith("W") and short.endswith("W") and w.ELLIPSIS in short
    assert w.shorten_to_width(text, 10000, measure) == text
    assert w.shorten_to_width(text, 5, measure) == w.ELLIPSIS        # nothing fits beside the ellipsis


def test_shorten_end():
    text = "Left out: no Read 2 file found."
    short = w.shorten_end(text, 12, lambda _ch: 1)
    assert short.endswith(w.ELLIPSIS) and len(short) <= 12 and text.startswith(short[:-1])
    assert w.shorten_end(text, 100, lambda _ch: 1) == text
    assert not w.shorten_end("Ready. Very", 8, lambda _ch: 1)[:-1].endswith(" ")   # no space before the dots


def test_place_popup_stays_inside_the_screen():
    w.set_work_area_provider(lambda x, y: (0, 0, 800, 600))
    try:
        # room below and to the right: stays where asked
        assert w.place_popup(None, 200, 100, 114, 120, pointer=(100, 100), gap=12) == (114, 120)
        # too far right: pushed left, still below the pointer
        x, y = w.place_popup(None, 200, 100, 714, 120, pointer=(700, 100), gap=12)
        assert x + 200 <= 800 and y == 120
        # too low: goes above the pointer, never under it
        x, y = w.place_popup(None, 200, 100, 114, 560, pointer=(100, 540), gap=12)
        assert y + 100 <= 540 - 12 + 1 and y >= 0
        # a second monitor to the left (negative coordinates)
        w.set_work_area_provider(lambda x, y: (-1280, 0, 0, 1024))
        x, y = w.place_popup(None, 300, 80, -150, 1000, pointer=(-164, 980), gap=12)
        assert -1280 <= x and x + 300 <= 0 and y + 80 <= 980
    finally:
        w.set_work_area_provider(None)


# =========================================================================== pictures

def _png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_png_encoder_writes_a_valid_file():
    rgba = bytes([255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0, 9, 9, 9, 9] * 2)     # 4 x 2
    data = icons.encode_png(4, 2, rgba)
    assert _png_size(data) == (4, 2)
    assert data[24:29] == bytes([8, 6, 0, 0, 0])                   # 8 bits, RGBA
    position, chunks = 8, {}
    while position < len(data):
        (length,) = struct.unpack(">I", data[position:position + 4])
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length:position + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks[kind] = body
        position += 12 + length
    assert list(chunks) == [b"IHDR", b"IDAT", b"IEND"]
    raw = zlib.decompress(chunks[b"IDAT"])
    assert raw == b"\x00" + rgba[:16] + b"\x00" + rgba[16:]
    with pytest.raises(ValueError):
        icons.encode_png(4, 2, rgba[:-1])


def test_bitmap_rounded_rect():
    bmp = icons.Bitmap(20, 20)
    bmp.rounded_rect(0, 0, 20, 20, 6, fill="#ffffff", outline="#ff0000", width=1)
    data = bmp.to_rgba()

    def pixel(x, y):
        return tuple(data[(y * 20 + x) * 4:(y * 20 + x) * 4 + 4])

    assert pixel(0, 0)[3] == 0                                    # outside the round corner
    assert pixel(10, 10) == (255, 255, 255, 255)                  # the fill
    assert pixel(10, 0) == (255, 0, 0, 255)                       # a sharp hairline on the straight edge
    assert pixel(10, 1) == (255, 255, 255, 255)
    assert 0 < pixel(1, 1)[3] < 255 or pixel(2, 1)[3] > 0         # the corner is softened
    assert _png_size(bmp.to_png()) == (20, 20)


def test_stretch_rgba_widens_the_middle_only():
    bmp = icons.Bitmap(5, 5)
    bmp.rounded_rect(0, 0, 5, 5, 0, fill="#102030", outline="#ffffff", width=1)
    rgba, width, height = icons.stretch_rgba(bmp.to_rgba(), 5, 5, 2, 2, 10, 4)
    assert (width, height) == (14, 8) and len(rgba) == 14 * 8 * 4
    assert tuple(rgba[0:4]) == (255, 255, 255, 255)                           # corner kept
    assert tuple(rgba[(3 * 14 + 7) * 4:(3 * 14 + 7) * 4 + 4]) == (16, 32, 48, 255)   # middle repeated
    assert tuple(rgba[(7 * 14 + 13) * 4:(7 * 14 + 13) * 4 + 4]) == (255, 255, 255, 255)


def test_colour_helpers():
    assert icons.rgb("#2563eb") == (37, 99, 235)
    assert icons.rgb("#fff") == (255, 255, 255)
    assert icons.mix("#000000", "#ffffff", 0.5) == "#808080"
    assert icons.mix("#123456", "#abcdef", 0) == "#123456"
    assert icons.mix("#123456", "#abcdef", 1) == "#abcdef"
    with pytest.raises(ValueError):
        icons.rgb("blue")
    assert icons.MARK_BLUE == theme.LIGHT.accent                   # the mark is in the house blue


def test_mark_is_a_cube_within_a_cube_joined_at_the_corners():
    full = icons.mark_geometry(detail=True)
    assert len(full["lines"]) == 8 + 12 + 12                       # corner joints, outer cube, inner cube
    small = icons.mark_geometry(detail=False)
    assert len(small["lines"]) == 4 + 4 + 4                        # the same figure seen head-on
    for x0, y0, x1, y1, width, colour in full["lines"] + small["lines"]:
        assert all(0.05 <= v <= 0.95 for v in (x0, y0, x1, y1)) and width > 0 and colour.startswith("#")


def test_mark_drawn_without_pillow():
    bmp = icons._draw_mark(32)
    data = bmp.to_rgba()
    assert _png_size(bmp.to_png()) == (32, 32)
    assert data[3] == 0                                            # the tile has round corners
    blue = sum(1 for i in range(0, len(data), 4) if data[i + 2] > 200 and data[i] < 90 and data[i + 3] == 255)
    assert blue > 30


def test_make_icon_with_pillow():
    pytest.importorskip("PIL")
    image = icons.make_icon(64)
    assert image.size == (64, 64) and image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0 and image.getpixel((32, 6))[3] == 255
    big = icons.make_icon(256)
    assert big.size == (256, 256)
    pixels = big.getdata()
    blue = sum(1 for r, g, b, a in pixels if a == 255 and b > 200 and r < 80)
    assert blue > 2000                                             # the tesseract is there, in blue


def test_icon_asset_is_in_place():
    path = os.path.join(os.path.dirname(GUI_DIR), "assets", "icon.png")
    with open(path, "rb") as handle:
        assert _png_size(handle.read(32)) == (256, 256)


# =========================================================================== with a display

def _usable(display):
    """Can a Tk window be opened there? (A socket of a display that has gone does not count.)"""
    try:
        import tkinter
        probe = tkinter.Tk(screenName=display or None)
        probe.withdraw()
        probe.destroy()
        return True
    except Exception:
        return False


def _display():
    candidates = []
    wanted = os.environ.get("TESSERACT_WIN_TEST_DISPLAY")
    if wanted:
        candidates.append(wanted)
    elif sys.platform.startswith("linux"):
        if os.path.exists("/tmp/.X11-unix/X99"):
            candidates.append(":99")
        if os.environ.get("DISPLAY"):
            candidates.append(os.environ["DISPLAY"])
    else:
        candidates.append("")            # Windows and macOS need no display name
    for candidate in candidates:
        if _usable(candidate):
            return candidate
    return None


DISPLAY = _display()
needs_display = pytest.mark.skipif(DISPLAY is None, reason="no display (DISPLAY is not set)")


def _pump(root, seconds=0.05):
    end = time.time() + seconds
    while True:
        root.update()
        if time.time() >= end:
            break
        time.sleep(0.01)
    root.update_idletasks()


class Window:
    """A Tk root with the theme, a page and a help line, for one test."""

    def __init__(self, dark=False, scale=1.0, high_contrast=False):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk(screenName=DISPLAY or None)
        self.root.geometry("900x640+0+0")
        self.look = theme.init(self.root, is_dark=dark, is_high_contrast=high_contrast, scale=scale)
        self.page = w.Frame(self.root, ground="page", padding=12)
        self.page.pack(fill="both", expand=True)
        self.card = w.Card(self.page)
        self.card.pack(fill="both", expand=True)
        self.help = w.HelpLine(self.page)
        self.help.pack(fill="x")

    def pump(self, seconds=0.05):
        _pump(self.root, seconds)

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass


@pytest.fixture
def win():
    window = Window()
    yield window
    window.close()


def _columns():
    return [
        w.Column("sample", "Sample", width=140, editable=True, shorten="middle"),
        w.Column("read1", "Read 1", width=160, shorten="middle", hover="first file"),
        w.Column("size", "Size", width=70, anchor="e"),
        w.Column("status", "Status", width=200, stretch=True, status=True),
    ]


def _rows(count):
    rows = []
    for index in range(count):
        name = "KP_%03d" % index
        rows.append({"id": "r%d" % index, "checked": index % 3 != 2,
                     "status": "ok" if index % 5 else "warn",
                     "values": {"sample": name, "read1": name + "_S%d_L001_R1_001.fastq.gz" % index,
                                "size": "%d MB" % (300 + index), "status": "Ready"}})
    return rows


def _table(win, count=6, **options):
    events = []
    options.setdefault("on_toggle", lambda ids, checked: events.append(("toggle", list(ids), checked)))
    options.setdefault("on_remove", lambda ids: events.append(("remove", list(ids))))
    options.setdefault("on_rename", lambda row, old, new: events.append(("rename", row, old, new)))
    options.setdefault("on_select", lambda ids: events.append(("select", list(ids))))
    options.setdefault("on_activate", lambda row: events.append(("activate", row)))
    table = w.CheckTable(win.card, _columns(), check_title="Trim", check_hover="tick to trim",
                         height=8, help_line=win.help, **options)
    table.pack(fill="both", expand=True)
    table.set_rows(_rows(count))
    win.pump(0.15)
    return table, events


def _click(tree, x, y, double=False):
    """A click as the mouse would send it. Tk makes the second of two quick presses a double click."""
    for _ in range(2 if double else 1):
        tree.event_generate("<Button-1>", x=x, y=y)
        tree.event_generate("<ButtonRelease-1>", x=x, y=y)


def _key(widget, *sequences):
    """Press keys on a widget. A key goes to the widget that has the keyboard, so give it that first."""
    widget.focus_force()
    widget.update()
    if widget.focus_get() is not widget:
        pytest.skip("this display does not hand out the keyboard")
    for sequence in sequences:
        widget.event_generate(sequence)
    widget.update()


def _cell_centre(table, row_id, column):
    box = table.tree.bbox(row_id, column)
    assert box, "cell %s/%s is not on screen" % (row_id, column)
    return box[0] + box[2] // 2, box[1] + box[3] // 2


@pytest.mark.gui
@needs_display
@pytest.mark.parametrize("dark, scale, high_contrast", [
    (False, 1.0, False), (True, 1.0, False), (False, 1.5, False), (True, 1.5, False), (False, 1.0, True),
])
def test_every_widget_can_be_made_and_destroyed(dark, scale, high_contrast):
    window = Window(dark=dark, scale=scale, high_contrast=high_contrast)
    try:
        look, card, help_line = window.look, window.card, window.help
        assert look.dark is dark and look.high_contrast is high_contrast
        made = [
            w.PageHeader(card, texts.PAGE_TITLE["trimming"], texts.PAGE_INTRO["trimming"]),
            w.Stepper(window.page, help_line=help_line),
            w.Section(card, "Section", "A sentence."),
            w.Well(card),
            w.WrapLabel(card, texts.ADVICE),
            w.LinkLabel(card, texts.BUTTON["try_example"], lambda: None),
            w.label(card, "label"), w.button(card, "plain"), w.button(card, "danger", kind="danger"),
            w.button(card, "default", kind="default"), w.button(card, "tool", kind="tool"),
            w.tick(card, "tick"), w.entry(card), w.hairline(card),
            w.TickRow(card, "Tick", value=True, help_text="help", help_line=help_line),
            w.TickRow(card, "Tick with number", number=dict(value=5.0, minimum=0.1, maximum=20, decimal=True),
                      suffix="Mb", help_text="help", help_line=help_line),
            w.NumberRow(card, "Number", number=dict(value=50, minimum=0, maximum=10000), suffix="bases"),
            w.ChoiceRow(card, "Mode", [(m, texts.MODE_LABEL[m], texts.MODE_HOVER[m]) for m in model.ASSEMBLY_MODES],
                        help_line=help_line),
            w.NumberField(card, value=10, minimum=1, maximum=36),
            w.Disclosure(card, help_line=help_line),
            w.NoticeBar(card),
            w.StageChips(card),
            w.WorkingIndicator(card, "working"),
            w.LogPane(card, height=3),
            w.CheckTable(card, _columns(), check_title="Trim", height=3),
            w.CheckTable(card, _columns(), checks=False, height=3),
            w.BottomBar(window.page, help_line=help_line),
            w.HelpLine(window.page),
            w.ttk.Progressbar(card, maximum=10, value=4),
            w.ttk.Scrollbar(card, orient="horizontal"),
            w.ttk.Combobox(card, values=["a", "b"]),
            w.ttk.Spinbox(card, from_=0, to=5),
            w.ttk.Notebook(card),
            w.ttk.Labelframe(card, text="frame"),
        ]
        for widget in made:
            if widget.winfo_manager() == "":
                widget.pack(anchor="w")
        made[20].show("info", "An information.", "info", actions=[("Do it", lambda: None)])
        made[20].show("bad", "Something is wrong.", "bad")
        made[21].set(model.Stage.TRIM, "running")
        made[22].start()
        made[23].append("one\ntwo")
        made[24].set_rows(_rows(4))
        made[25].set_rows(_rows(4))
        made[19].open()
        window.pump(0.2)
        dialog = w.Dialog(window.root, title="Question", body="Body", details="a\nb",
                          choices=[("yes", "Yes"), ("no", "No", "danger"), ("why", "Why", "plain", "A reason.")],
                          default="yes", cancel="no")
        dialog.open()
        window.pump(0.1)
        dialog.invoke("yes")
        assert dialog.result == "yes"
        for widget in made:
            widget.destroy()
        window.pump(0.05)
        assert len(look.icons) > 20                               # the pictures are kept alive
    finally:
        window.close()


@pytest.mark.gui
@needs_display
def test_theme_scale_fonts_and_styles():
    window = Window(dark=True, scale=1.5)
    try:
        look = window.look
        assert look.scale == 1.5 and look.px(16) == 24 and look.px(0) == 0 and look.px(1) >= 1
        assert look.px(-8) == -12
        assert theme.current(window.card) is look and theme.px(10, window.card) == 15
        assert theme.init(window.root) is look                    # chosen once at start-up
        assert look.palette is theme.DARK
        for name in ("small", "body", "bold", "heading", "title", "mono", "link"):
            assert look.font(name).actual("size") != 0
        assert look.font("small").cget("size") == 9 and look.font("body").cget("size") == 10
        assert look.font("heading").cget("size") == 11 and look.font("title").cget("size") == 16
        assert bool(look.font("link").cget("underline"))
        # the row height of tables follows the font
        assert look.row_height >= look.font("body").metrics("linespace") + 4
        assert int(look.style.lookup("Treeview", "rowheight")) == look.row_height
        # a plain button is exactly as tall as a text field
        button, field = w.button(window.card, "Browse..."), w.entry(window.card)
        assert button.winfo_reqheight() == field.winfo_reqheight() == look.control_height
        # every ground has its twin of every style, with the right colour behind it
        style = look.style
        for ground, prefix in theme.GROUNDS.items():
            for base in ("TLabel", "Muted.TLabel", "TButton", "Primary.TButton", "TCheckbutton", "TRadiobutton",
                         "TEntry", "TFrame", "Link.TButton", "Running.Chip.TLabel", "Vertical.TScrollbar"):
                assert style.lookup(prefix + base, "background") == look.ground_colour(ground), prefix + base
        assert style.lookup("OnPage.Primary.TButton", "foreground") == theme.DARK.on_accent
        assert style.lookup("TButton", "foreground", ["disabled"]) == look.disabled_fg
        # dark mode: nothing falls back to white or to system grey
        light_colours = {"#ffffff", "white", "#d9d9d9", "#dcdad5", "SystemButtonFace", ""}
        for name in ("TFrame", "TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TEntry", "TCombobox",
                     "TSpinbox", "Treeview", "Treeview.Heading", "TScrollbar", "TProgressbar", "TNotebook",
                     "TNotebook.Tab", "TLabelframe", "TLabelframe.Label", "TPanedwindow", "TScale",
                     "TMenubutton", "TSeparator", "TSizegrip", "."):
            assert style.lookup(name, "background") not in light_colours, name
        assert style.lookup("Treeview", "fieldbackground") == theme.DARK.surface
        import tkinter as tk
        for cls, widget in (("Text", tk.Text(window.root)), ("Canvas", tk.Canvas(window.root)),
                            ("Listbox", tk.Listbox(window.root)), ("Label", tk.Label(window.root)),
                            ("Frame", tk.Frame(window.root)), ("Toplevel", tk.Toplevel(window.root))):
            colour = widget.cget("background")
            assert colour in (theme.DARK.surface, theme.DARK.bg), (cls, colour)
        assert window.root.cget("background") == theme.DARK.bg
    finally:
        window.close()


@pytest.mark.gui
@needs_display
def test_icon_set_keeps_its_pictures(win):
    icons_ = win.look.icons
    first = icons_.tick("on")
    assert icons_.tick("on") is first                              # made once
    assert {str(icons_.tick(s)) for s in ("on", "off", "na")} == {str(icons_.tick("on")), str(icons_.tick("off")),
                                                                    str(icons_.tick("na"))}
    assert len({str(icons_.tick(s)) for s in ("on", "off", "na")}) == 3
    assert first.width() == first.height() == icons_.box_size
    big = icons.IconSet(win.root, win.look.palette.as_dict(), 2.0)
    assert big.tick("on").width() == 2 * 16 + 2 * 6                # drawn at the scale, not stretched
    assert len(icons_.spinner("accent", 16)) == 12
    assert icons_.chevron("down").width() == icons_.px(12)
    window_icons = icons_.window_icons()
    assert [i.width() for i in window_icons][-4:] == [48, 32, 24, 16]
    assert window_icons[0].width() == 256                          # assets/icon.png, read without Pillow
    win.root.iconphoto(True, *window_icons)


# --------------------------------------------------------------------------- CheckTable

@pytest.mark.gui
@needs_display
def test_table_click_on_a_tick_box_toggles_the_row(win):
    table, events = _table(win)
    assert table.is_checked("r0") is True and table.is_checked("r2") is False
    x, y = _cell_centre(table, "r0", "#0")
    _click(table.tree, x, y)
    assert table.is_checked("r0") is False
    assert events == [("toggle", ["r0"], False)]
    assert str(table.tree.item("r0", "image")[0]) == str(win.look.icons.tick("off"))
    _click(table.tree, x, y)
    assert table.is_checked("r0") is True
    assert str(table.tree.item("r0", "image")[0]) == str(win.look.icons.tick("on"))
    assert table.selection() == []                                 # ticking does not select


@pytest.mark.gui
@needs_display
def test_table_row_that_cannot_be_ticked(win):
    table, events = _table(win)
    table.update_row("r1", checked=None, dim=True, status="bad", values={"status": "Left out: no Read 2 file found."})
    win.pump()
    assert str(table.tree.item("r1", "image")[0]) == str(win.look.icons.tick("na"))
    x, y = _cell_centre(table, "r1", "#0")
    _click(table.tree, x, y)
    assert table.is_checked("r1") is None and events == []
    assert "dim" in table.tree.item("r1", "tags")
    assert table.check_all(False) and table.is_checked("r1") is None
    assert "r1" not in table.checked_ids()


@pytest.mark.gui
@needs_display
def test_table_space_toggles_the_selected_rows(win):
    table, events = _table(win)
    table.select(["r0", "r1", "r2"])                               # ticked, ticked, unticked
    table.tree.focus_force()
    win.pump()
    del events[:]
    table.tree.event_generate("<space>")
    win.pump()
    assert [table.is_checked(r) for r in ("r0", "r1", "r2")] == [True, True, True]     # any off: all on
    assert events == [("toggle", ["r2"], True)]
    table.tree.event_generate("<space>")
    win.pump()
    assert [table.is_checked(r) for r in ("r0", "r1", "r2")] == [False, False, False]  # all on: all off
    assert events[-1] == ("toggle", ["r0", "r1", "r2"], False)


@pytest.mark.gui
@needs_display
def test_table_heading_of_the_tick_column_toggles_all(win):
    table, events = _table(win)
    del events[:]
    _click(table.tree, 12, 8)                                      # the "Trim" heading
    win.pump()
    assert all(table.is_checked(r) for r in table.ids())
    assert events == [("toggle", ["r2", "r5"], True)]
    _click(table.tree, 12, 8)
    win.pump()
    assert not any(table.is_checked(r) for r in table.ids())
    assert table.toggle_all() == table.ids() and len(table.checked_ids()) == 6


@pytest.mark.gui
@needs_display
def test_table_delete_removes_the_selected_rows(win):
    table, events = _table(win)
    table.select(["r1", "r2"])
    table.tree.focus_force()
    win.pump()
    del events[:]
    table.tree.event_generate("<Delete>")
    win.pump()
    assert table.ids() == ["r0", "r3", "r4", "r5"] and len(table) == 4 and "r1" not in table
    assert ("remove", ["r1", "r2"]) in events
    assert table.selection() == ["r3"]                             # the keyboard stays on the list
    # rows are striped again after the removal
    assert [("odd" in table.tree.item(r, "tags")) for r in table.ids()] == [False, True, False, True]
    fixed = w.CheckTable(win.card, _columns(), checks=False, removable=False)
    fixed.set_rows(_rows(2))
    fixed.select(["r0"])
    fixed.tree.event_generate("<Delete>")
    assert len(fixed) == 2


@pytest.mark.gui
@needs_display
def test_table_rename_in_place(win):
    def check(row_id, text):
        if not text.strip():
            return texts.RENAME_EMPTY
        if not all(ch.isalnum() or ch in "-_." for ch in text):
            return texts.RENAME_BAD_CHARACTERS
        return ""

    table, events = _table(win, validate_rename=check)
    x, y = _cell_centre(table, "r1", "sample")
    _click(table.tree, x, y, double=True)                  # double-click on the name
    win.pump()
    assert table.editing == ("r1", "sample")
    editor = table.editor
    editor.focus_force()
    win.pump()
    assert editor.get() == "KP_001"
    box, place = table.tree.bbox("r1", "sample"), editor.place_info()
    assert (int(place["x"]), int(place["y"]), int(place["width"]), int(place["height"])) == tuple(box)
    # Esc cancels
    editor.delete(0, "end")
    editor.insert(0, "other")
    editor.event_generate("<Escape>")
    win.pump()
    assert table.editing is None and table.value("r1", "sample") == "KP_001"
    assert not [e for e in events if e[0] == "rename"]
    # a name that is not acceptable: said in the help line, Enter does not take it
    assert table.begin_rename("r1")
    editor = table.editor
    editor.focus_force()
    win.pump()
    editor.delete(0, "end")
    editor.insert(0, "KP 001 (rerun)")
    editor.event_generate("<KeyRelease>")
    assert win.help.text == texts.RENAME_BAD_CHARACTERS and editor.instate(["invalid"])
    editor.event_generate("<Return>")
    win.pump()
    assert table.editing == ("r1", "sample")
    # Enter accepts a good one
    editor.delete(0, "end")
    editor.insert(0, "KP_001_rerun")
    editor.event_generate("<KeyRelease>")
    assert not editor.instate(["invalid"])
    editor.event_generate("<Return>")
    win.pump()
    assert table.editing is None and table.editor is None
    assert table.value("r1", "sample") == "KP_001_rerun"
    assert table.tree.set("r1", "sample") == "KP_001_rerun"
    assert ("rename", "r1", "KP_001", "KP_001_rerun") in events
    assert win.help.text != texts.RENAME_BAD_CHARACTERS
    # F2 edits the focused row; a column that is not editable cannot be edited
    table.select(["r3"])
    table.tree.focus_force()
    win.pump()
    table.tree.event_generate("<F2>")
    win.pump()
    assert table.editing == ("r3", "sample")
    table.editor.event_generate("<Escape>")
    assert table.begin_rename("r3", "size") is False
    table.set_enabled(False)
    assert table.begin_rename("r3") is False


@pytest.mark.gui
@needs_display
def test_table_leaving_the_field_accepts_a_valid_name(win):
    table, events = _table(win)
    other = w.entry(win.card)
    other.pack()
    win.pump()
    assert table.begin_rename("r0")
    table.editor.delete(0, "end")
    table.editor.insert(0, "NEW_NAME")
    table._end_rename(True)                                        # what <FocusOut> calls
    assert table.value("r0", "sample") == "NEW_NAME" and ("rename", "r0", "KP_000", "NEW_NAME") in events
    assert table.begin_rename("r0")
    table.editor.delete(0, "end")                                  # empty: not valid, so the old name stays
    table._end_rename(True)
    assert table.value("r0", "sample") == "NEW_NAME" and table.editing is None
    # scrolling closes the field
    table.set_rows(_rows(60))
    win.pump(0.1)
    assert table.begin_rename("r0")
    table.tree.yview_moveto(0.5)
    win.pump(0.1)
    assert table.editing is None


@pytest.mark.gui
@needs_display
def test_table_long_names_are_shortened_in_the_middle(win):
    table, _events = _table(win)
    long_path = "D:\\Sequencing\\Run 12\\" + "KP_000_S0_L001_R1_001.fastq.gz"
    table.update_row("r0", tips={"read1": long_path})
    table.tree.column("read1", width=win.look.px(110))
    table._layout()
    shown = table.shown("r0", "read1")
    full = table.value("r0", "read1")
    assert shown != full and w.ELLIPSIS in shown
    head, tail = shown.split(w.ELLIPSIS)
    assert full.startswith(head) and full.endswith(tail) and tail
    assert table.tree.set("r0", "read1") == shown
    assert win.look.text_width(shown) <= win.look.px(110)
    # the tooltip carries the full text: the given one, or the value itself
    assert table.tip_for("cell", "r0", "read1") == long_path
    assert table.tip_for("cell", "r1", "read1") == table.value("r1", "read1")
    assert table.tip_for("heading", "", "read1") == "first file"
    assert table.tip_for("heading", "", "#0") == "tick to trim"
    assert table.tip_for("cell", "r1", "size") == ""               # fits: nothing to say
    # wide again: the full name is back
    table.tree.column("read1", width=win.look.px(420))
    table._layout()
    assert table.shown("r0", "read1") == full and table.tip_for("cell", "r1", "read1") == ""
    # status words that do not fit end in an ellipsis
    table.update_row("r2", values={"status": "Left out: no Read 2 file found. Each sample needs two files."})
    table.tree.column("status", width=win.look.px(120))
    table._layout()
    assert table.shown("r2", "status").endswith(w.ELLIPSIS)
    assert table.tip_for("cell", "r2", "status").endswith("needs two files.")


@pytest.mark.gui
@needs_display
def test_table_status_words_are_coloured(win):
    table, _events = _table(win)
    table.update_row("r1", status="bad", values={"status": "Failed"})
    table.update_row("r3", status="accent", values={"status": "Assembling"})
    table.select(["r3"])
    win.pump(0.15)
    shown = [item for item in table._status_labels if item.winfo_manager() == "place"]
    assert len(shown) == 6                                         # one per row in view
    by_text = {item.cget("text").strip(): item for item in shown}
    palette = win.look.palette
    assert by_text["Failed"].cget("foreground") == palette.bad
    assert by_text["Assembling"].cget("foreground") == palette.accent
    assert by_text["Assembling"].cget("background") == win.look.select_bg      # on the selected row
    ready = [item for item in shown if item.cget("text").strip() == "Ready"]
    assert len(ready) == 4
    colours = [item.cget("foreground") for item in sorted(ready, key=lambda item: item.winfo_y())]
    assert colours == [palette.warn, palette.ok, palette.ok, palette.warn]     # r0, r2, r4, r5 (see _rows)
    assert {item.cget("background") for item in ready} <= {palette.surface, palette.row_alt}
    # the words lie exactly over their cell
    failed = by_text["Failed"]
    assert (failed.winfo_x(), failed.winfo_y(), failed.winfo_width(), failed.winfo_height()) == tuple(
        table.tree.bbox("r1", "status"))
    # a click on the words selects the row underneath
    failed.event_generate("<Button-1>", x=5, y=5)
    failed.event_generate("<ButtonRelease-1>", x=5, y=5)
    win.pump()
    assert table.selection() == ["r1"]


@pytest.mark.gui
@needs_display
def test_table_rows_and_callbacks(win):
    table, events = _table(win, count=4)
    assert [("odd" in table.tree.item(r, "tags")) for r in table.ids()] == [False, True, False, True]
    table.add_row("new", {"sample": "ZZ", "read1": "ZZ_R1.fastq.gz", "size": "1 MB", "status": "Ready"},
                  checked=False, status="ok")
    assert table.ids()[-1] == "new" and table.row("new")["checked"] is False
    with pytest.raises(ValueError):
        table.add_row("new", {})
    table.add_row("first", {"sample": "AA"}, index=0)
    assert table.ids()[0] == "first" and "odd" in table.tree.item("r0", "tags")
    del events[:]
    table.select(["r2"])
    win.pump()
    assert ("select", ["r2"]) in events
    x, y = _cell_centre(table, "r2", "size")
    _click(table.tree, x, y, double=True)                  # not editable: the row is activated
    assert ("activate", "r2") in events
    table.tree.focus_force()
    win.pump()
    table.tree.event_generate("<Return>")
    assert events.count(("activate", "r2")) == 2
    table.tree.event_generate("<Control-a>")
    assert table.selection() == table.ids()
    assert table.set_checked(["r0", "nope"], False) == ["r0"]      # quiet unless asked
    assert not [e for e in events if e[0] == "toggle"]
    table.check_all(True)
    assert events[-1][0] == "toggle" and events[-1][2] is True
    table.clear()
    assert len(table) == 0 and table.tree.get_children() == ()


@pytest.mark.gui
@needs_display
def test_table_empty_state(win):
    table, _events = _table(win, count=0)
    clicked = []
    table.set_empty(texts.EMPTY_STATE, texts.BUTTON["try_example"], lambda: clicked.append(1),
                    texts.EMPTY_STATE_LINK_LEAD)
    win.pump(0.1)
    assert table._empty is not None and table._empty.winfo_ismapped()
    links = [c for c in w._descendants(table._empty) if isinstance(c, w.LinkLabel)]
    assert len(links) == 1 and links[0].cget("text") == "Try the example"
    links[0].invoke()
    assert clicked == [1]
    table.set_rows(_rows(2))
    win.pump(0.1)
    assert not table._empty.winfo_ismapped()
    table.clear()
    win.pump(0.1)
    assert table._empty.winfo_ismapped()


@pytest.mark.gui
@needs_display
def test_table_stays_fast_with_500_rows(win):
    table, events = _table(win, count=0)
    rows = _rows(500)
    started = time.perf_counter()
    table.set_rows(rows)
    win.pump(0.0)
    filled = time.perf_counter() - started
    assert len(table) == 500
    started = time.perf_counter()
    for fraction in (0.2, 0.5, 0.9, 0.0):
        table.tree.yview_moveto(fraction)
        win.pump(0.0)
    scrolled = time.perf_counter() - started
    started = time.perf_counter()
    table.tree.column("read1", width=win.look.px(90))              # every name must be shortened again
    table._layout()
    win.pump(0.0)
    resized = time.perf_counter() - started
    started = time.perf_counter()
    table.check_all(False)
    table.check_all(True)
    table.update_row("r250", status="bad", values={"status": "Failed"})
    table.remove_rows(["r%d" % i for i in range(0, 500, 2)])
    win.pump(0.0)
    changed = time.perf_counter() - started
    assert len(table) == 250
    assert filled < 2.0 and scrolled < 1.0 and resized < 2.0 and changed < 2.0, (filled, scrolled, resized, changed)
    # only the rows in view carry status words: the count does not grow with the table
    assert len(table._status_labels) <= table.tree.winfo_height() // win.look.row_height + 2
    assert table.shown("r1", "read1") != table.value("r1", "read1")
    assert [e for e in events if e[0] == "toggle"][-1] == ("toggle", ["r%d" % i for i in range(500)], True)


# --------------------------------------------------------------------------- hover text

@pytest.mark.gui
@needs_display
def test_explain_shows_the_text_in_the_help_line_on_focus(win):
    first = w.TickRow(win.card, "Remove adapters", value=True, help_text="First text.", help_line=win.help)
    second = w.button(win.card, "Browse...")
    w.explain(second, "Second text.", win.help)
    first.pack(anchor="w")
    second.pack(anchor="w")
    win.help.set_default("Default text.")
    win.pump()
    assert win.help.text == "Default text."
    first.tick.focus_force()
    win.pump()
    assert win.help.text == "First text."
    second.focus_force()
    win.pump()
    assert win.help.text == "Second text."
    win.root.focus_force()
    win.pump()
    assert win.help.text == "Default text."
    # of mouse and keyboard the one used last speaks; a message outranks both
    win.help.hover_text("hover", owner=first)
    win.help.focus_text("focus", owner=second)
    assert win.help.text == "focus"                                # a resting mouse does not silence Tab
    win.help.hover_text("hover", owner=first)
    assert win.help.text == "hover"
    win.help.say("Use a whole number from 0 to 40. Set back to 10.", "warn", seconds=0.15)
    assert win.help.text.startswith("Use a whole number") and win.help.kind == "warn"
    win.pump(0.35)
    assert win.help.text == "hover" and win.help.kind == "info"   # the message has gone by itself
    win.help.clear_hover(owner=first)
    assert win.help.text == "focus"
    win.help.clear()
    assert win.help.text == "Default text."


@pytest.mark.gui
@needs_display
def test_tooltip_appears_and_goes_away(win):
    target = w.button(win.card, "Add files...")
    target.pack(anchor="w", padx=40, pady=40)
    tip = w.explain(target, texts.HOVER["add_files"], win.help)
    win.root.lift()
    win.pump(0.1)
    target.event_generate("<Motion>", warp=True, x=10, y=10)       # put the mouse on the button
    win.pump(0.2)
    pointer = win.root.winfo_pointerxy()
    if win.root.winfo_containing(*pointer) is not target:
        pytest.skip("the pointer cannot be moved on this display")
    target.event_generate("<Enter>")
    assert not tip.visible                                         # not at once
    win.pump(0.25)
    assert not tip.visible                                         # not yet: about half a second
    win.pump(0.6)
    assert tip.visible
    manager = w._tips(win.root)
    assert manager.shown_text == texts.HOVER["add_files"]
    window = manager.window
    # below and right of the pointer, never under it, inside the screen, wrapped
    assert window.winfo_rooty() > pointer[1] and window.winfo_rootx() > pointer[0]
    assert window.winfo_rootx() + window.winfo_width() <= win.root.winfo_screenwidth()
    assert window.winfo_width() <= win.look.px(340) + win.look.px(30)
    assert bool(window.overrideredirect())
    assert win.help.text == texts.HOVER["add_files"]              # the same words in the help line
    # a second <Enter> while it shows does not restart it (no flicker)
    target.event_generate("<Enter>")
    assert manager.window is window
    # it does not time out
    win.pump(1.2)
    assert tip.visible
    # a click takes it away
    target.event_generate("<ButtonPress-1>", x=10, y=10)
    assert not tip.visible
    target.event_generate("<ButtonRelease-1>", x=10, y=10)
    # shown again, then the page changes under the resting mouse: it goes by itself
    tip.show_now()
    win.pump(0.1)
    assert tip.visible
    target.pack_forget()
    win.pump(0.5)
    assert not tip.visible
    tip.show_now()
    win.pump(0.05)
    w.hide_tooltips(win.root)
    assert not tip.visible


@pytest.mark.gui
@needs_display
def test_tooltip_near_the_bottom_right_corner_stays_on_screen(win):
    width, height = win.root.winfo_screenwidth(), win.root.winfo_screenheight()
    win.root.geometry("300x120+%d+%d" % (width - 300, height - 120))
    target = w.button(win.card, "Corner")
    target.pack(side="bottom", anchor="e")
    tip = w.Tooltip(target, texts.TRIM_HOVER["keep_trimmed_reads"])
    win.pump(0.15)
    target.event_generate("<Motion>", warp=True, x=target.winfo_width() - 4, y=target.winfo_height() - 4)
    win.pump(0.2)
    pointer = win.root.winfo_pointerxy()
    if win.root.winfo_containing(*pointer) is not target:
        pytest.skip("the pointer cannot be moved on this display")
    tip.show_now()
    win.pump(0.15)
    assert tip.visible
    window = w._tips(win.root).window
    assert window.winfo_rootx() >= 0 and window.winfo_rooty() >= 0
    assert window.winfo_rootx() + window.winfo_width() <= width
    assert window.winfo_rooty() + window.winfo_height() <= height
    assert window.winfo_rooty() + window.winfo_height() <= pointer[1]      # above the pointer, not under it


# --------------------------------------------------------------------------- navigation

@pytest.mark.gui
@needs_display
def test_stepper(win):
    picked = []
    stepper = w.Stepper(win.page, command=picked.append, help_line=win.help)
    stepper.pack(fill="x")
    win.pump()
    assert stepper.step_keys == ["samples", "trimming", "assembly", "run"] and stepper.current == "samples"
    stepper.set_sub("samples", texts.step_ready(40))
    stepper.set_sub("trimming", texts.STEP_STANDARD)
    stepper.set_sub("assembly", texts.STEP_CHANGED, "accent")
    parts = stepper._parts
    assert parts["samples"]["sub"].cget("text") == "40 ready"
    assert parts["assembly"]["sub"].cget("text") == "changed"
    assert parts["assembly"]["sub"].cget("foreground") == win.look.palette.accent
    assert parts["trimming"]["sub"].cget("foreground") == win.look.palette.muted
    assert stepper.sub("trimming") == "standard"
    # the current step is marked and is not a link; the others are
    assert parts["samples"]["title"].cget("foreground") == win.look.palette.text
    assert str(parts["samples"]["cell"].cget("takefocus")) == "0"
    assert str(parts["trimming"]["cell"].cget("takefocus")) == "1"
    assert parts["trimming"]["title"].cget("cursor") == "hand2"
    parts["samples"]["title"].event_generate("<Button-1>")
    assert picked == []
    parts["assembly"]["title"].event_generate("<Button-1>")
    assert picked == ["assembly"] and stepper.current == "samples"     # the caller moves it
    stepper.set_current("assembly")
    assert stepper.current == "assembly"
    # keyboard: Tab to a step, Enter or Space
    parts["trimming"]["cell"].focus_force()
    win.pump()
    parts["trimming"]["cell"].event_generate("<Return>")
    parts["trimming"]["cell"].event_generate("<space>")
    assert picked == ["assembly", "trimming", "trimming"]
    # one step can be closed; during a run all are locked
    stepper.set_enabled("run", False)
    assert stepper.pick("run") is False
    stepper.set_enabled("run", True)
    stepper.set_current("run")
    assert stepper._hover_text("samples") == texts.HOVER["step"] and stepper._hover_text("run") == ""
    stepper.lock()
    assert stepper._hover_text("samples") == texts.HOVER["step_locked"]
    del picked[:]
    for key in stepper.step_keys:
        parts[key]["title"].event_generate("<Button-1>")
        assert str(parts[key]["cell"].cget("takefocus")) == "0"
    assert picked == [] and stepper.can_pick("samples") is False
    assert parts["samples"]["title"].cget("foreground") == win.look.disabled_fg
    stepper.lock(False)
    assert stepper.pick("samples") is True and picked == ["samples"]
    with pytest.raises(KeyError):
        stepper.set_current("nowhere")


@pytest.mark.gui
@needs_display
def test_bottom_bar(win):
    calls = []
    bar = w.BottomBar(win.page, on_back=lambda: calls.append("back"), on_next=lambda: calls.append("next"),
                      on_start=lambda: calls.append("start"), help_line=win.help)
    bar.pack(fill="x")
    bar.bind_keys(win.root)
    win.pump()
    # Start is the only primary button; Back and Next are plain
    assert "Primary" in bar.start.cget("style")
    assert "Primary" not in bar.back.cget("style") and "Primary" not in bar.next.cget("style")
    assert not bar.next.winfo_ismapped()                           # no next step named yet
    bar.set_next("trimming")
    win.pump()
    assert bar.next.cget("text") == "Next: Trimming" and bar.next.winfo_ismapped()
    # disabled: the reason stands beside it
    bar.set_hint(texts.FIRST_TIME_HINT)
    bar.set_start(0, enabled=False, reason=texts.start_reason(0, 0, False))
    assert bar.start.instate(["disabled"]) and bar.start.cget("text") == "Start"
    assert bar.message_text == "Add samples to begin."
    bar.start.invoke()
    win.root.event_generate("<Alt-s>")
    assert calls == []
    # enabled: the count on the button, the hint (or a note with a link) beside it
    bar.set_start(38)
    assert bar.start.instate(["!disabled"]) and bar.start.cget("text") == "Start (38 samples)"
    assert bar.message_text == texts.FIRST_TIME_HINT
    followed = []
    bar.set_note("", link_text="2 left out: why?", link_command=lambda: followed.append(1))
    win.pump()
    assert bar.link.winfo_ismapped() and bar.link.cget("text") == "2 left out: why?"
    bar.link.invoke()
    assert followed == [1]
    bar.set_note("")
    win.pump()
    assert not bar.link.winfo_ismapped() and bar.message_text == texts.FIRST_TIME_HINT
    # Start is not the Enter key: Space or Alt+S, never Return
    bar.start.focus_force()
    win.pump()
    bar.start.event_generate("<Return>")
    bar.start.event_generate("<KP_Enter>")
    win.pump()
    assert calls == []
    bar.start.event_generate("<space>")
    bar.start.event_generate("<KeyRelease-space>")
    win.pump()
    assert calls == ["start"]
    win.root.focus_force()
    win.pump()
    win.root.event_generate("<Alt-s>")
    win.root.event_generate("<Alt-n>")
    win.root.event_generate("<Alt-b>")
    assert calls == ["start", "start", "next", "back"]
    bar.set_back(enabled=False)
    win.root.event_generate("<Alt-b>")
    assert calls[-1] == "back" and len(calls) == 4
    bar.set_back(visible=False)
    bar.set_next(None)
    win.pump()
    assert not bar.back.winfo_ismapped() and not bar.next.winfo_ismapped()


# --------------------------------------------------------------------------- options

@pytest.mark.gui
@needs_display
def test_disclosure(win):
    toggled = []
    more = w.Disclosure(win.card, command=toggled.append, help_line=win.help)
    more.pack(fill="x")
    inside = w.label(more.body, "inside")
    inside.pack()
    win.pump()
    assert more.header.cget("text") == "More options"
    assert more.is_open is False and not more.body.winfo_ismapped()        # closed by default
    assert not more.mark.winfo_ismapped() and more.changed is False
    more.set_changed(True)
    win.pump()
    assert more.mark.winfo_ismapped() and more.mark.cget("text") == "changed"
    more.header.invoke()
    win.pump()
    assert more.is_open and inside.winfo_ismapped() and toggled == [True]
    more.header.focus_force()
    win.pump()
    more.header.event_generate("<Return>")
    win.pump()
    assert not more.is_open and not more.body.winfo_ismapped() and toggled == [True, False]
    assert more.mark.winfo_ismapped()                              # a closed section still shows the mark
    more.set_changed(False)
    win.pump()
    assert not more.mark.winfo_ismapped()
    assert w.Disclosure(win.card, opened=True).is_open


@pytest.mark.gui
@needs_display
def test_number_field(win):
    changes = []
    field = w.NumberField(win.card, value=10, minimum=0, maximum=40, command=changes.append, help_line=win.help)
    other = w.entry(win.card)
    field.pack()
    other.pack()
    win.pump()
    assert field.get() == "10" and field.value == 10
    # out of range: back to the last good value when the field is left, and the help line says so
    field.focus_force()
    win.pump()
    field.delete(0, "end")
    field.insert(0, "99")
    other.focus_force()
    win.pump(0.1)
    assert field.get() == "10" and changes == []
    assert win.help.text == "Use a whole number from 0 to 40. Set back to 10." and win.help.kind == "warn"
    # not a number
    field.delete(0, "end")
    field.insert(0, "ten")
    assert field.commit() == 10 and field.get() == "10" and field.last_message
    # a good value is taken, the callback hears it once
    field.delete(0, "end")
    field.insert(0, " 25 ")
    _key(field, "<Return>")
    assert field.get() == "25" and changes == [25] and field.last_message == ""
    field.commit()
    assert changes == [25]
    # the last good value is now 25
    field.delete(0, "end")
    field.insert(0, "-1")
    assert field.value == 25 and "Set back to 25." in field.last_message
    # arrow keys step and stop at the ends; Esc restores
    field.set_value(39)
    _key(field, "<Up>", "<Up>")
    assert field.value == 40 and changes[-1] == 40
    _key(field, "<Down>")
    assert field.value == 39
    field.delete(0, "end")
    field.insert(0, "7")
    _key(field, "<Escape>")
    assert field.get() == "39"
    field.set_value(1000)
    assert field.value == 40                                       # set from the program: kept in range
    field.set_enabled(False)
    field.set_value(5)
    assert field.get() == "5" and field.instate(["disabled"])


@pytest.mark.gui
@needs_display
def test_decimal_number_field_takes_dot_and_comma(win):
    field = w.NumberField(win.card, value=5.0, minimum=0.1, maximum=20, decimal=True, help_line=win.help)
    field.pack()
    win.pump()
    assert field.get() == "5.0"
    for typed, expected, shown in (("5,5", 5.5, "5.5"), ("2.8", 2.8, "2.8"), ("12", 12.0, "12.0"), (",5", 0.5, "0.5")):
        field.delete(0, "end")
        field.insert(0, typed)
        assert field.commit() == pytest.approx(expected) and field.get() == shown
    field.delete(0, "end")
    field.insert(0, "25")
    assert field.commit() == pytest.approx(0.5) and field.get() == "0.5"
    assert win.help.text == "Use a number from 0.1 to 20. Set back to 0.5."
    _key(field, "<Up>")
    assert field.value == pytest.approx(0.6)


@pytest.mark.gui
@needs_display
def test_tick_and_choice_rows(win):
    seen = []
    row = w.TickRow(win.card, texts.ASSEMBLY_LABEL["know_genome_size"], value=False,
                    number=dict(value=5.0, minimum=0.1, maximum=20, decimal=True),
                    suffix=texts.ASSEMBLY_SUFFIX["genome_size_mb"], command=seen.append,
                    help_text=texts.ASSEMBLY_HOVER["know_genome_size"], help_line=win.help)
    row.pack(anchor="w")
    win.pump()
    assert row.checked is False and row.field.instate(["disabled"])        # the number waits for the tick
    assert row.tick.cget("text") == "I know the genome size: about" and row.suffix.cget("text") == "Mb"
    row.tick.invoke()
    assert row.checked is True and seen == [True] and row.field.instate(["!disabled"])
    row.set(False)
    assert seen == [True] and row.field.instate(["disabled"])              # set from the program: no callback
    row.var.set(True)                                                      # ... or through the variable
    assert row.field.instate(["!disabled"])
    row.var.set(False)
    assert row.field.instate(["disabled"])
    row.set_enabled(False)
    assert row.tick.instate(["disabled"])
    row.set_enabled(True)
    assert row.tick.instate(["!disabled"]) and row.field.instate(["disabled"])
    assert row.tip is not None and row.tip.text == texts.ASSEMBLY_HOVER["know_genome_size"]
    assert row.winfo_reqheight() >= win.look.control_height
    plain = w.TickRow(win.card, "Remove adapters", value=True)
    plain.pack(anchor="w")
    win.pump()
    assert plain.field is None and plain.checked and plain.winfo_reqheight() == row.winfo_reqheight()

    picked = []
    choice = w.ChoiceRow(win.card, texts.ASSEMBLY_LABEL["mode"],
                         [(m, texts.MODE_LABEL[m], texts.MODE_HOVER[m]) for m in model.ASSEMBLY_MODES],
                         command=picked.append, help_line=win.help)
    choice.pack(anchor="w")
    win.pump()
    assert choice.value == "standard" and list(choice.buttons) == list(model.ASSEMBLY_MODES)
    assert choice.buttons["standard"].cget("text") == "Standard (recommended)"
    choice.buttons["careful"].invoke()
    assert choice.value == "careful" and picked == ["careful"]
    choice.set("fast")
    assert choice.value == "fast" and picked == ["careful"]
    choice.buttons["aggressive"].focus_force()
    win.pump()
    assert win.help.text == texts.MODE_HOVER["aggressive"]         # each choice explains itself
    choice.set_enabled(False)
    assert all(b.instate(["disabled"]) for b in choice.buttons.values())

    number = w.NumberRow(win.card, texts.TRIM_LABEL["min_length"], number=dict(value=50, minimum=0, maximum=10000),
                         suffix=texts.TRIM_SUFFIX["min_length"], help_line=win.help)
    assert number.field.value == 50 and number.suffix.cget("text") == "bases"
    assert number.field.help_line is win.help


# --------------------------------------------------------------------------- run page pieces

@pytest.mark.gui
@needs_display
def test_notice_bar(win):
    bars = w.NoticeBar(win.page)
    bars.pack(fill="x")
    win.pump()
    assert bars.kinds() == [] and bars.winfo_reqheight() <= 1      # no bar: no room taken
    done = []
    bars.show("battery", texts.ON_BATTERY, "warn")
    bars.show("memory", texts.low_memory("2.9 GB"), "warn")
    bars.show("resume", "The last run was interrupted.", "info",
              actions=[("Continue the run", lambda: done.append("continue"))], dismissible=False)
    win.pump()
    assert bars.kinds() == ["battery", "memory", "resume"]
    assert bars.level("battery") == "warn" and bars.text("memory").startswith("Only 2.9 GB")
    bars.button_of("resume").invoke()
    assert done == ["continue"]
    # the same kind again replaces the words in place
    bars.show("battery", texts.battery_low(18), "bad")
    win.pump()
    assert bars.kinds() == ["battery", "memory", "resume"]
    assert bars.text("battery").startswith("Battery at 18 %") and bars.level("battery") == "bad"
    assert "Bad.Notice" in bars._bars["battery"]["frame"].cget("style")
    # dismissed by the program, by an empty text, by a Notice event, by the user
    assert bars.dismiss("memory") is True and bars.dismiss("memory") is False
    bars.show("battery", "")
    assert bars.kinds() == ["resume"]
    bars.handle(model.Notice(kind="slept", text=texts.slept("22:14", "07:31"), level="info"))
    assert bars.kinds() == ["resume", "slept"] and bars.level("slept") == "info"
    bars.handle(model.Notice(kind="slept", text=""))
    assert bars.kinds() == ["resume"]
    closed = []
    bars.show("info", "A note.", "info", on_dismiss=closed.append)
    bars._bars["info"]["close"].invoke()
    assert closed == ["info"] and bars.kinds() == ["resume"]
    assert bars._bars["resume"]["close"] is None                   # not dismissible: no close button
    bars.clear()
    win.pump()
    assert bars.kinds() == [] and bars.winfo_children() == []


@pytest.mark.gui
@needs_display
def test_stage_chips(win):
    chips = w.StageChips(win.card)
    chips.pack(anchor="w")
    win.pump()
    assert chips.stage_keys == [s.value for s in model.Stage]
    assert chips.visible_stages() == ["trim", "check_reads", "assemble", "summarise"]     # lanes only when needed
    assert [chips._chips[k].cget("text") for k in chips.stage_keys] == [
        "Join lanes", "Trim", "Check reads", "Assemble", "Summarise"]
    chips.show_stage(model.Stage.JOIN_LANES)
    win.pump()
    assert chips.visible_stages()[0] == "join_lanes" and chips._chips["join_lanes"].winfo_ismapped()
    for state in w.StageChips.STATES:
        chips.set(model.Stage.TRIM, state)
        assert chips.state_of("trim") == state
        assert chips._chips["trim"].cget("style") == "%s.Chip.TLabel" % state.capitalize()
    chips.set(model.Stage.ASSEMBLE, "running")
    animator = w._animator(win.root)
    assert chips in animator._clients                              # the ring of a running chip turns
    before = str(chips._chips["assemble"].cget("image"))
    win.pump(0.35)
    assert str(chips._chips["assemble"].cget("image")) != before
    chips.reset()
    assert all(chips.state_of(k) == "waiting" for k in chips.stage_keys) and chips not in animator._clients
    with pytest.raises(ValueError):
        chips.set(model.Stage.TRIM, "exploded")
    with pytest.raises(KeyError):
        chips.set("polish", "done")


@pytest.mark.gui
@needs_display
def test_working_indicator(win):
    ring = w.WorkingIndicator(win.card, "Working")
    ring.pack(anchor="w")
    win.pump()
    animator = w._animator(win.root)
    still = str(ring.ring.cget("image"))
    assert ring.running is False and ring not in animator._clients
    ring.start()
    seen = set()
    for _ in range(6):
        win.pump(0.1)
        seen.add(str(ring.ring.cget("image")))
    assert len(seen) >= 3                                          # it moves while the window is alive
    ring.stop()
    assert str(ring.ring.cget("image")) == still and ring not in animator._clients
    ring.set_text("Step 3 of 7")
    assert ring.words.cget("text") == "Step 3 of 7"
    ring.start()
    ring.destroy()
    win.pump(0.25)
    assert ring not in animator._clients and animator._job is None        # nothing keeps ticking


@pytest.mark.gui
@needs_display
def test_log_pane(win):
    log = w.LogPane(win.card, max_lines=2000, height=6)
    log.pack(fill="both", expand=True)
    win.pump()
    assert str(log.text.cget("state")) == "disabled"               # read-only
    assert "Mono" in str(log.text.cget("font")) or log.text.cget("font") == str(win.look.font("mono"))
    log.append("first line")
    log.append(["second", "third\n"])
    log.append("fourth\nfifth")
    assert log.get_text().split("\n") == ["first line", "second", "third", "fourth", "fifth", ""]
    assert log.line_count == 5
    log.append("line %d" % i for i in range(2500))
    win.pump()
    assert log.line_count == 2000                                  # capped
    lines = log.get_text().rstrip("\n").split("\n")
    assert len(lines) == 2000 and lines[-1] == "line 2499" and lines[0] == "line 500"
    assert log.following                                           # the end is in view
    # the user scrolls up: new lines no longer drag the view along
    log.text.yview_moveto(0.2)
    win.pump()
    assert not log.following
    top_before = log.text.get("@0,0 linestart", "@0,0 lineend")
    log.append(["more %d" % i for i in range(50)])
    win.pump()
    assert not log.following and log.line_count == 2000
    assert log.text.get("@0,0 linestart", "@0,0 lineend") == top_before
    # back at the end, it follows again
    log.text.yview_moveto(1.0)
    win.pump()
    log.append("the last one")
    win.pump()
    assert log.following and log.get_text().rstrip("\n").endswith("the last one")
    # typing does nothing
    log.text.focus_force()
    win.pump()
    log.text.event_generate("<Key-x>")
    assert "x" != log.get_text()[-2:-1] and log.line_count == 2000
    log.clear()
    assert log.line_count == 0 and log.get_text() == ""


# --------------------------------------------------------------------------- dialogs

def _run_dialog(win, dialog, action, delay=120, guard=None):
    """Show a dialog modally; ``action(dialog)`` is run once it is open.

    If the action does not close the dialog, the button ``guard`` is pressed after four
    seconds so that the test run never hangs; name a button the test does not expect.
    """
    win.root.after(delay, lambda: action(dialog))
    rescue = win.root.after(4000, lambda: dialog.invoke(guard) if guard else dialog._cancelled())
    answer = dialog.show()
    win.root.after_cancel(rescue)
    return answer


@pytest.mark.gui
@needs_display
def test_dialog_returns_the_key_of_the_chosen_button(win):
    question = texts.stop_question("KP_002", model.RunStatus.ASSEMBLING, 14 * 60, 12)
    make = lambda: w.Dialog(win.root, title=question.title, body=question.body, choices=question.choices,   # noqa: E731
                            default=question.default, cancel=question.cancel)
    assert _run_dialog(win, make(), lambda d: d.invoke("now")) == "now"
    assert _run_dialog(win, make(), lambda d: d.invoke("after_current")) == "after_current"
    # Esc, the close box and Enter all mean "Keep running" here
    assert _run_dialog(win, make(), lambda d: _key(d, "<Escape>"), guard="now") == "keep"
    close_box = lambda d: d.tk.call(d.protocol("WM_DELETE_WINDOW"))          # noqa: E731  what the X does
    assert _run_dialog(win, make(), close_box, guard="now") == "keep"
    assert _run_dialog(win, make(), lambda d: _key(d.buttons["keep"], "<Return>"), guard="now") == "keep"
    # Enter on another button that has the keyboard presses that button, not the default one
    assert _run_dialog(win, make(), lambda d: _key(d.buttons["after_current"], "<Return>"), guard="now") == "after_current"
    # the arrow keys move between the buttons
    assert _run_dialog(win, make(), lambda d: _key(d.buttons["keep"], "<Right>", "<Right>", "<Return>"),
                       guard="keep") == "now"

    dialog = make()
    seen = {}

    def look_at(d):
        seen["title"] = d.title()
        seen["labels"] = {key: b.cget("text") for key, b in d.buttons.items()}
        seen["styles"] = {key: b.cget("style") for key, b in d.buttons.items()}
        seen["body"] = d.body.cget("text")
        seen["grab"] = d.grab_current() is d
        seen["mapped"] = d.winfo_viewable()
        px, py, pw, ph = win.root.winfo_rootx(), win.root.winfo_rooty(), win.root.winfo_width(), win.root.winfo_height()
        centre = d.winfo_rootx() + d.winfo_width() / 2.0
        seen["centred"] = abs(centre - (px + pw / 2.0)) <= 2 and py <= d.winfo_rooty() <= py + ph
        seen["resizable"] = d.resizable()
        d.invoke("keep")

    assert _run_dialog(win, dialog, look_at) == "keep"
    assert seen["title"] == "Stop the run?"
    assert seen["labels"] == {"keep": "Keep running", "after_current": "Stop after KP_002", "now": "Stop now"}
    assert "Default" in seen["styles"]["keep"] and "Danger" in seen["styles"]["now"]
    assert "Primary" not in "".join(seen["styles"].values())       # only Start is primary-coloured
    assert seen["body"] == question.body and seen["grab"] and seen["mapped"] and seen["centred"]
    assert tuple(seen["resizable"]) in ((False, False), (0, 0))
    assert not dialog.winfo_exists()


@pytest.mark.gui
@needs_display
def test_dialog_from_a_question_with_details(win):
    names = ["KP_%03d" % i for i in range(12)]
    question = texts.overwrite_question(12, 40, 8, "not trimmed; mode Careful", names)
    seen = {}

    def look_at(d):
        seen["keys"] = list(d.buttons)
        seen["details_closed"] = d.details is not None and not d.details.is_open
        seen["details_label"] = d.details.header.cget("text")
        d.details.header.invoke()
        d.update_idletasks()
        seen["details_text"] = d.details_text.get("1.0", "end-1c")
        seen["details_read_only"] = str(d.details_text.cget("state")) == "disabled"
        # Enter on the details header opens it; it must not answer the question
        _key(d.details.header, "<Return>")
        seen["still_open"] = bool(d.winfo_exists()) and not d.details.is_open
        d.invoke("overwrite")

    dialog = w.Dialog(win.root, title=question.title, body=question.body, choices=question.choices,
                      default=question.default, cancel=question.cancel, details=question.details,
                      details_label=question.details_label)
    assert _run_dialog(win, dialog, look_at) == "overwrite"
    assert seen["keys"] == ["skip", "overwrite", "cancel"]
    assert seen["details_closed"] and seen["details_label"] == "Show the 12 samples"
    assert seen["details_text"].split("\n") == names and seen["details_read_only"] and seen["still_open"]
    # Dialog.ask does the same from the Question itself; Esc is Cancel, the default is Skip
    def find():
        return [c for c in win.root.winfo_children() if isinstance(c, w.Dialog)][0]

    win.root.after(120, lambda: _key(find(), "<Escape>"))
    rescue = win.root.after(4000, lambda: find().invoke("overwrite"))
    assert w.Dialog.ask(win.root, question) == "cancel"
    win.root.after_cancel(rescue)
    win.root.after(120, lambda: _key(find().buttons[question.default], "<Return>"))
    rescue = win.root.after(4000, lambda: find().invoke("overwrite"))
    assert w.Dialog.ask(win.root, question) == "skip"
    win.root.after_cancel(rescue)


@pytest.mark.gui
@needs_display
def test_dialog_hooks_and_defaults(win):
    made = []
    hook = made.append
    w.Dialog.created_hooks.append(hook)
    try:
        dialog = w.Dialog(win.root, title="Only OK", body="Nothing to choose.")
        assert made == []                                          # called when the window opens
        assert _run_dialog(win, dialog, lambda d: _key(d, "<Escape>")) == "ok"
        assert made == [dialog]
    finally:
        w.Dialog.created_hooks.remove(hook)
    tuples = w.Dialog(win.root, title="T", body="B", choices=[("a", "A"), ("b", "B", "danger")], default="b")
    assert tuples.default == "b" and tuples.cancel == "b"          # the last button unless told otherwise
    assert "Danger" in tuples.buttons["b"].cget("style")
    tuples.destroy()
