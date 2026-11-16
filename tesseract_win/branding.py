# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Every vendor-facing string, defined exactly once.

No other module may hard-code the product, vendor or author name. Import from here so
the title bar, the reports, the About box and the release files can never drift apart.
"""

from .versions import APP_VERSION, SCEPTER_VERSION, TESSERACT_VERSION

APP_NAME = "TesserACT-Win"
APP_TAGLINE = "Trim and assemble bacterial genomes on Windows"
VENDOR = "IOWA-BioTech"
AUTHOR = "Giovanni Lorenzin"
#: The canonical visible attribution.
ATTRIBUTION = "%s — %s" % (VENDOR, AUTHOR)
COPYRIGHT = "© 2026 %s" % ATTRIBUTION
HOMEPAGE = "https://github.com/iowa69/TesserACT-Win"
TESSERACT_HOMEPAGE = "https://github.com/iowa69/TesserACT"
LICENSE_NAME = "MIT"

#: Folder name used under the user's local application data for settings and logs.
DATA_DIR_NAME = APP_NAME
#: Default name of the results folder the program suggests.
RESULTS_DIR_NAME = "TesserACT_results"


def window_title(running: str = "") -> str:
    """Title bar text; ``running`` replaces the tagline while a batch is in progress."""
    return "%s %s — %s — %s" % (APP_NAME, APP_VERSION, running or APP_TAGLINE, ATTRIBUTION)


def versions_line() -> str:
    """One line naming what a result was made with (for summaries and the About box)."""
    return "%s %s (TesserACT %s, scepter %s)" % (APP_NAME, APP_VERSION, TESSERACT_VERSION, SCEPTER_VERSION)


def about_text() -> str:
    return "\n".join([
        "%s %s" % (APP_NAME, APP_VERSION),
        APP_TAGLINE,
        "",
        "Assembler: TesserACT %s" % TESSERACT_VERSION,
        "Trimmer: scepter %s" % SCEPTER_VERSION,
        "",
        COPYRIGHT,
        "%s licence. %s" % (LICENSE_NAME, HOMEPAGE),
    ])
