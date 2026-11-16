<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin -->
# Parity with the bash `tesseract` script

`tesseract_win/readcheck.py` and `tesseract_win/postflight.py` reproduce the checks of the
bash `tesseract` 1.6.2 script (`engines/tesseract/upstream/tesseract`). This folder holds the
evidence and the tools. The specification is `docs/superpowers/audits/wrapper-port.md`.

| path | what |
|---|---|
| `golden/` | 161 cases captured from the real script. Evidence: never edited by hand. |
| `golden/SCRIPT_FINGERPRINT.json` | sections and literals of the script the goldens were captured from |
| `make_fixtures.py` | writes the 56 read sets into `fixtures/` (not in git; about 25 s, 79 MB) |
| `FIXTURES.sha256` | digest of those read sets, so that the generator cannot drift from the goldens |
| `fake_asm/` | stand-in engine (`fake_asm.sh`) and its canned output folders (`cases/`) |
| `harness.py` | used by the tests: loads the goldens and drives the port like the script |
| `model/` | the auditor's Python model of the script, its own parity check and mutation test |
| `run_golden.sh`, `make_toolbin.sh` | capture goldens again from a script (Linux, bash) |
| `compare_goldens.py`, `compare_toolchains.sh` | compare a new capture with `golden/` |
| `script_fingerprint.py` | says which sections and literals of a script changed |
| `port_mutations.py` | 97 one-line mistakes put into a copy of the two modules; the tests must catch each |

Everything the tools write goes to `fixtures/`, `toolbin/` and `out/`, which git ignores.

## The tests

```
python3 -m pytest tests/test_wrapper_parity.py -q     # goldens, fingerprint, real script side by side
python3 -m pytest tests/test_readcheck.py tests/test_postflight.py -q
```

Of the 161 goldens, 17 belong to a command line (usage errors, `--help`, `--version`) and are
skipped; 144 apply; 137 are identical byte for byte and 7 differ on purpose (an invalid genome
size is refused, a folder given as a read file is reported as unreadable), each asserted in a
test of its own.

## Are the goldens still what the script prints?

```
python3 make_fixtures.py && ./make_toolbin.sh
./run_golden.sh native noreal        # 145 cases without the engine, under a minute
./run_golden.sh native all           # all 161, with build/engines/linux/tesseract-asm (some minutes)
python3 compare_goldens.py           # out/golden-new against golden/
```

`run_golden.sh` never writes into `golden/` unless told to with `GOLD_OVERRIDE=golden`.

## A new upstream release

1. `tests/test_wrapper_parity.py` fails: the sha256 and the fingerprint of the script changed.
   The failure lists the sections and literals that changed.
2. `./run_golden.sh native all` and `python3 compare_goldens.py`: the cases that differ are
   what the port has to follow.
3. Bring `readcheck.py` and `postflight.py` in line (most changes are one constant or one
   message), then replace the evidence: `GOLD_OVERRIDE=golden ./run_golden.sh native all`,
   `python3 script_fingerprint.py ../../engines/tesseract/upstream/tesseract > golden/SCRIPT_FINGERPRINT.json`,
   and record the new sha256 in `tesseract_win/versions.py` (`engines/update.py` does that).
4. `python3 model/parity_check.py` keeps the auditor's model honest in the same way, and
   `python3 port_mutations.py` shows that the tests still pin every rule of the port.
