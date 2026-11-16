# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Versions of the program and of the engines it bundles.

The engine values are the pins. ``engines/update.py`` rewrites them when a new upstream
release is brought in; nothing else should edit them by hand.
"""

APP_VERSION = "0.2.0"

#: Pinned TesserACT release (engine ``tesseract-asm``), its tag and commit.
TESSERACT_VERSION = "1.6.2"
TESSERACT_TAG = "v1.6.2"
TESSERACT_COMMIT = "50ca70ff75243fe981ebcef756af096fa0512339"
#: sha256 of the bash ``tesseract`` script that ``readcheck`` and ``postflight`` reproduce.
#: If upstream changes the script, the parity tests fail until the port is brought in line.
TESSERACT_WRAPPER_SHA256 = "dfa075b96ca0cb1cfc8eabbf06c3bfab9e22257bfdb9eb648d9d7656d0086d4a"

#: Pinned scepter (repository "fastplus") version and commit.
SCEPTER_VERSION = "1.1.0"
SCEPTER_COMMIT = "f48b5beb82df069aa3e719d29496babca336a88d"

#: Format number of sample_status.json and last_run.json. A results folder written with a
#: higher number was made by a newer program and is not touched.
RESULTS_FORMAT = 1
