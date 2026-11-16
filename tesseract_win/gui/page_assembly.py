# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Step 3, Assembly (decision D6).

On the page: "I know the genome size: about [5.0] Mb" and "Leave out contigs shorter than
[500] bases". Behind "More options", closed by default: Assembly mode, Join contigs across
gaps (scaffolding), Polish the final sequence, Assemble even if the read check fails,
Processor threads. Always on and without a control: the HTML report, the assembly graph,
the ``assemblies`` folder and the lower priority.

The page shows and changes ``app.state.assembly`` (a ``model.AssemblySettings``). The
genome size takes "5.5" as well as "5,5". Everything else is shared with the Trimming
page (:class:`page_trimming.OptionsPage`).
"""

from __future__ import annotations

from typing import Any

from .. import settings_map
from ..model import ASSEMBLY_MODES, AssemblySettings, default_threads
from . import state as st
from . import texts
from . import widgets as w
from .page_trimming import OptionsPage

__all__ = ["AssemblyPage"]


class AssemblyPage(OptionsPage):
    """Step 3: the options of the assembler."""

    step_key = "assembly"
    reset_hover = texts.HOVER["reset_assembly"]
    more_fields = st.ASSEMBLY_MORE_FIELDS

    def settings(self) -> AssemblySettings:
        return self.state.assembly

    def fresh(self) -> AssemblySettings:
        return AssemblySettings()

    def store(self, settings: AssemblySettings) -> None:
        self.state.assembly = settings

    def more_changed(self) -> bool:
        return st.assembly_more_changed(self.state.assembly)

    # -- building
    def build(self, parent: Any, below: Any) -> None:
        px = self.look.px
        label, hover, suffix = texts.ASSEMBLY_LABEL, texts.ASSEMBLY_HOVER, texts.ASSEMBLY_SUFFIX
        assembly = self.state.assembly

        low, high = settings_map.number_range("genome_size_mb")
        size = self.tick_row(parent, "know_genome_size", label["know_genome_size"], hover["know_genome_size"],
                             suffix=suffix["genome_size_mb"],
                             number=dict(value=assembly.genome_size_mb, minimum=low, maximum=high, decimal=True,
                                         command=lambda v: self.set_value("genome_size_mb", float(v))))
        size.pack(anchor="w")
        self.numbers["genome_size_mb"] = size.field
        self.rows["genome_size_mb"] = size

        low, high = settings_map.number_range("min_contig")
        short = self.tick_row(parent, "drop_short_contigs", label["drop_short_contigs"], hover["drop_short_contigs"],
                              suffix=suffix["min_contig"],
                              number=dict(value=assembly.min_contig, minimum=low, maximum=high,
                                          command=lambda v: self.set_value("min_contig", int(v))))
        short.pack(anchor="w")
        self.numbers["min_contig"] = short.field
        self.rows["min_contig"] = short

        self.more = w.Disclosure(below, help_line=self.help_line)
        self.more.pack(fill="x", pady=(px(6), 0))
        inner = self.more.body
        self.mode = w.ChoiceRow(inner, label["mode"],
                                [(mode, texts.MODE_LABEL[mode], texts.MODE_HOVER[mode]) for mode in ASSEMBLY_MODES],
                                value=assembly.mode, help_text=hover["mode"], help_line=self.help_line,
                                command=lambda value: self.set_value("mode", str(value)))
        self.mode.pack(anchor="w")
        self.rows["mode"] = self.mode
        for name in ("scaffold", "polish", "force"):
            self.tick_row(inner, name, label[name], hover[name]).pack(anchor="w")
        self.processors = default_threads()
        threads = w.NumberRow(inner, label["threads"], suffix=suffix["threads"].format(n=self.processors),
                              help_text=hover["threads"], help_line=self.help_line,
                              number=dict(value=assembly.threads, minimum=1, maximum=self.processors,
                                          command=lambda v: self.set_value("threads", int(v))))
        threads.pack(anchor="w")
        self.numbers["threads"] = threads.field
        self.rows["threads"] = threads

    # -- showing
    def show_settings(self, settings: AssemblySettings) -> None:
        super().show_settings(settings)
        self.mode.set(settings.mode if settings.mode in ASSEMBLY_MODES else "standard")
