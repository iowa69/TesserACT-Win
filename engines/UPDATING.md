<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin -->
# Bringing in a new release of TesserACT or scepter

The engines are built from pinned, unmodified upstream source. The Windows support is a
compatibility layer in added files plus scripts that change a staged copy, so a new release
is not a new port: it is one command, a short list to read, and a push.

Work on Linux, on a branch. Half an hour when upstream changed nothing the port depends on.

## 1. Get the source and its commit

Download the release archive from the engine's repository, or check the tag out. Note the
full commit of the tag (`git rev-parse v1.7.0^{commit}`, 40 characters). The command never
downloads anything itself.

## 2. Try it first, without changing anything

```
python engines/update.py tesseract --from ~/Downloads/TesserACT-1.7.0.tar.gz --version 1.7.0 --commit <commit> --dry-run
python engines/update.py scepter   --from ~/src/fastplus                    --version 1.2.0 --commit <commit> --dry-run
```

`--from` is a folder or an archive (`.tar.gz`, `.tgz`, `.tar`, `.zip`). With `--dry-run`
everything happens in a temporary copy of the project and the report is the one the real
run would print. Read it to the end: it finishes with **"What a person must look at now"**.

## 3. Do it

The same command without `--dry-run`. It

1. checks that the source states the version you gave;
2. replaces `engines/<engine>/upstream` (the old tree is kept in
   `build/update/<engine>/upstream-before` until the next update);
3. rewrites the pins in `tesseract_win/versions.py`: version, tag, commit and, for
   TesserACT, the sha256 of the bash `tesseract` script;
4. stages a copy and applies the scripted Windows changes;
5. compares the `tesseract` script with the one the Python port reproduces;
6. builds the changed source on Linux and, for TesserACT, lists C-library functions the
   program did not import before;
7. compares the old and the new source for things a person should see: new system headers,
   new Linux-only paths, new environment variables, new or removed options and tests.

Exit status 0: every guard passed. Exit status 1: a guard failed; the new source and the
pins are in place and the Windows changes do not fit them yet. Fix what the list says and
run the same command again. Exit status 2: the command could not start (wrong version,
wrong folder, a pin missing from `versions.py`) and changed nothing.

The command is safe to repeat, and safe to stop. Run again with the same source, it leaves
the tree alone and compares it once more with the tree it replaced, so the list at the end
is the whole list every time, not only what is left. Stopped half way, it finishes the
replacement on the next run. (A `--dry-run` made after the real run compares with what is
now in the project, and so reports that nothing changed.)

To go back to the release you had, give the kept tree to the same command, with the old
version and commit (`git diff tesseract_win/versions.py` shows them):

```
python engines/update.py tesseract --from build/update/tesseract/upstream-before --version 1.6.2 --commit <old commit>
```

What is copied is the release and nothing else: `.git`, object files, and at the top of
the source a `build` folder and built programs are left out.

## 4. What each line of the report means

| The report says | What it means | What to do |
|---|---|---|
| `FAILED staging and the scripted Windows changes stopped` with `rule R1`/`R2` or `T1` | Upstream changed a line that the port rewrites. | Read the new code at that place. If Windows still needs the change, correct the exact text in `engines/win/tesseract/win_rules.py` (or `engines/win/scepter/transform.py`). If upstream fixed it themselves, remove the rule. |
| `FAILED ...` with `guard G...` and a count | Something the port relies on appears more or less often: a new `/proc` or `/dev` path, a new pipe, a new call inside `util.cpp`. | Read each new place the message lists. Cover it in the compatibility layer if Windows needs it, then correct the expected count in `win_rules.py`. |
| `FAILED ...` scepter, `long` / `fseek` / `%ld` at a file and line | New code that assumes a 64-bit `long`, which is 32 bits on Windows. | If the line is safe, add it to the reviewed lines of `transform.py` with the reason; if not, add a transformation. |
| `LLP64 rewrite ... changed ... but ... is pinned` or the item "record what the 64-bit rewrite found" | The number of `long` rewrites is new for this release. | Add the printed counts to `LLP64_EXPECTED` in `engines/win/tesseract/recipe.py`. A big jump means new arithmetic to read (audit `engine-port.md`, section 2). |
| `LOOK the tesseract script is not the one the Python port matches` | The bash script changed. The list names each section, shows the lines, and says which file of `tesseract_win/` follows that section. | Bring `readcheck.py` / `postflight.py` / `settings_map.py` in line, then replace the evidence as `tests/parity/README.md` says ("A new upstream release"). Until then `pytest -m parity` fails, on purpose. A change in the version line or in comments needs only the new evidence. |
| `LOOK the Linux program imports C-library functions it did not import before` | A new dependency on the operating system or on the math library. | For each name: does MinGW-w64 have it, or must `ts_compat.h` supply it? For `exp`, `log`, `pow`, `sin`: can the result reach `contigs.fasta` (audit `engine-port.md`, section 6.2)? Then add the name to `engines/win/tesseract/libc_imports.txt`. |
| `FAILED the changed source does not build on Linux` | A scripted change no longer fits, or new code uses a name the compatibility header redefines (`stat`, `environ`, `fclose`, `main` ...). | The compiler message names the line. Correct `engines/win/<engine>/`. |
| `FAILED the list of C++ tests left out on Windows no longer fits` | Upstream removed a test that `tests-skip.txt` names. | Remove it from `engines/win/tesseract/tests-skip.txt`. |
| "the new C++ test ... uses fork()" | It cannot be built for Windows. | Add it to `tests-skip.txt` with the reason. |
| "scepter's options changed" | The trimming page builds a command line from these options. | Check `scepter_command` in `tesseract_win/settings_map.py`; run `pytest tests/test_settings_map.py`. |
| `skipped the Linux build` | No compiler here, or `--skip-build`. | Nothing: CI builds it. |

## 5. Check by hand

The command cannot see these:

- The release notes: a changed default, a new output file, a new check in `SUMMARY.txt`.
  If results change for the same reads, say so in the release notes of TesserACT-Win.
- A new output file of the assembler that should be the same on Linux and Windows: add it
  to `DETERMINISTIC_FILES` in `engines/equivalence.py`. A new file that carries times or
  paths must not be added.
- A new self-test answer that rightly differs between platforms: add it, with the reason,
  to `PLATFORM_ANSWERS` in `engines/selftest.py`.
- New options worth offering on the Trimming or Assembly page (decisions D5 and D6 of the
  plan say what version 1 offers).
- `NOTICE` and `packaging/`: the engine versions and licences shown to the user.

## 6. Test here

```
python engines/build.py --target linux
python engines/selftest.py run --engines build/engines/linux --out build/selftest-linux.txt
bash engines/ci/run_upstream_suites.sh linux
python -m pytest -q
```

`run_upstream_suites.sh` runs the engines' own tests on the changed source. A failure there
on Linux means a Windows change altered the engine: that must never be accepted.

## 7. Push: CI proves the result

```
git add -A && git commit -m "TesserACT 1.7.0" && git push
```

The Build workflow then, with no further action:

1. builds both engines on Linux and on Windows from the same changed source;
2. runs both self-tests and requires the same answers on both platforms;
3. runs the engines' own suites on both (on Windows, a check may fail only if it is listed
   with its reason in `engines/ci/expected-fail-windows.txt`);
4. trims and assembles the same read sets on both and requires every output file to be
   identical, byte for byte (`engines/equivalence.py`); run the workflow by hand with
   "real isolate" ticked, or push a tag, to include the real isolate as well;
5. freezes the program and runs its example.

The update is finished when that run is green. If step 4 shows a difference, the report
names the read set and the file: that is a real difference between the Linux and the
Windows engine, and the release must wait until it is understood.
