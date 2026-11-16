#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Build the two engines from their pinned upstream source.

    python3 engines/build.py --target linux
    python3 engines/build.py --target windows
    python3 engines/build.py --target windows --engine scepter --jobs 4

For each engine this stages a fresh copy of ``engines/<engine>/upstream`` under
``build/stage/<target>/<engine>``, lets the engine's recipe apply its scripted
transformations and compile it, and puts the program in ``build/engines/<target>``
(or ``--out``), with ``om2detect.sketch`` and a ``BUILD_INFO.json`` beside it.

The upstream folders are never written to.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildlib  # noqa: E402

ENGINES = ("tesseract", "scepter")


def load_recipe(engine: str):
    path = buildlib.REPO / "engines" / "win" / engine / "recipe.py"
    if not path.exists():
        raise buildlib.BuildError("no recipe for %s (expected %s)" % (engine, path))
    spec = importlib.util.spec_from_file_location("recipe_" + engine, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", choices=buildlib.TARGETS,
                        default="windows" if os.name == "nt" else "linux")
    parser.add_argument("--engine", choices=ENGINES + ("all",), default="all")
    parser.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--cxx", default=None, help="compiler to use instead of the default")
    parser.add_argument("--out", default=None, help="output folder (default build/engines/<target>)")
    args = parser.parse_args(argv)

    try:
        tc = buildlib.detect_toolchain(args.target, args.cxx)
        out_dir = Path(args.out).resolve() if args.out else buildlib.REPO / "build" / "engines" / args.target
        out_dir.mkdir(parents=True, exist_ok=True)
        info_path = out_dir / "BUILD_INFO.json"
        info = {"target": args.target, "compiler": tc.version(), "cross": tc.cross, "engines": {}}
        buildlib.say("target %s, compiler: %s" % (args.target, info["compiler"]))
        for engine in (ENGINES if args.engine == "all" else (args.engine,)):
            recipe = load_recipe(engine)
            started = time.monotonic()
            buildlib.say("building %s ..." % engine)
            facts = recipe.build(tc=tc,
                                 upstream=buildlib.REPO / "engines" / engine / "upstream",
                                 stage=buildlib.REPO / "build" / "stage" / args.target / engine,
                                 out_dir=out_dir, jobs=args.jobs)
            binary = out_dir / (recipe.BINARY + tc.exe)
            facts = dict(facts or {})
            facts["binary"] = binary.name
            facts["sha256"] = buildlib.sha256(binary)
            facts["seconds"] = round(time.monotonic() - started, 1)
            if args.target == "windows":
                imports = buildlib.dll_imports(tc, binary)
                facts["dll_imports"] = imports
                foreign = [name for name in imports if not buildlib.is_system_dll(name)]
                if foreign:
                    raise buildlib.BuildError("%s needs DLLs that are not part of Windows: %s"
                                              % (binary.name, ", ".join(foreign)))
            info["engines"][engine] = facts
            buildlib.say("  %s  %s  (%.0f s)" % (binary.name, facts["sha256"][:16], facts["seconds"]))
        sketch = buildlib.REPO / "engines" / "tesseract" / "upstream" / "models" / "om2detect.sketch"
        if sketch.exists():
            shutil.copy2(sketch, out_dir / "om2detect.sketch")
        if args.engine != "all" and info_path.exists():       # keep the other engine's facts
            import json
            with open(info_path, encoding="utf-8") as handle:
                previous = json.load(handle)
            if previous.get("target") == args.target:
                merged = previous.get("engines", {})
                merged.update(info["engines"])
                info["engines"] = merged
        buildlib.write_json(info_path, info)
        buildlib.say("done: %s" % out_dir)
        return 0
    except buildlib.BuildError as exc:
        sys.stderr.write("build failed: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
