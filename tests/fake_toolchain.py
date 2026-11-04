# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""A stand-in for gcc/gcov/lcov so the replay pipeline can be tested anywhere.

``make_build(root)`` writes a "gcov build": ``.gcno`` files that are JSON
(source, lines and branches), a program script that embeds the path of its
``.gcda`` files the way a gcov binary does and, when it exits normally, writes
its coverage as JSON to ``$GCOV_PREFIX`` with ``GCOV_PREFIX_STRIP`` components
removed (the gcov runtime's rule). ``make_lcov(path)`` writes an ``lcov`` that
reads those files and prints genuine lcov ``.info`` records (``SF``, ``DA``,
``BRDA`` with ``-`` for branches in lines that never ran, one record per
object even when two objects include the same header).

What the fake program does with its input (file named by the first argument,
else standard input):

* always covers ``src/prog.c`` line 10, branch 0
* ``A`` in the input covers line 11 branch 0, ``B`` covers line 11 branch 1
* argument ``-x`` covers line 12 branch 0
* ``U`` covers ``src/util.c`` line 5 branch 0 (a second object)
* ``H`` covers ``src/common.h`` line 3 branch 1 (prog object) and ``Q``
  covers the same header's branch 0 from the util object, so the header has two
  lcov records that must be merged
* ``SLEEP`` sleeps 30 s, ``CRASH`` raises SIGSEGV before writing, ``FAIL``
  exits with status 3 after writing
* ``--flake=PATH`` counts its runs in the file PATH and covers line 12
  branch 1 on every other run (the first, third, ...), which makes a branch
  appear in one replay of a corpus and not in the next
* ``JUNK`` writes a file ``junk.txt`` into the profile tree (``GCOV_PREFIX``)
* ``FORGE`` writes a well-formed profile file of its own (line 12 branch 0)
  before it exits: what a program that writes ``.gcda`` data by hand does
* ``TAMPER`` tries to append to its own file, to remove it and to create a
  file next to it, and exits with status 5 if any of that worked
* ``--flake-odd=PATH`` is the other way round: it covers line 12 branch 0 on every second run (the second, fourth, ...)
* ``--pidfile=PATH`` writes the process id to PATH at once
* ``--daemon=PATH`` starts a process that leaves the process group of the case (``fork``, ``setsid``), writes its
  process id to PATH and sleeps 30 s; the program goes on after PATH exists
* ``--signals=PATH`` writes whether SIGHUP, SIGINT and SIGTERM are ignored or at their default to PATH
* ``--observe=PATH`` writes JSON to PATH about the standard input before reading it (``fifo``, ``regular``,
  ``seekable``, ``atime``, ``mtime``, whether a file ``fd0`` is in the working directory), the values of ``LANG``,
  ``LC_ALL`` and ``TZ`` (null when unset), and after reading the number of bytes read and their sum modulo 2^32
* ``--probe=PATH`` writes JSON to PATH about what the program was given: its environment, its working directory
  and what the working directory, ``HOME`` and ``TMPDIR`` held when it started
* ``--leave`` creates a file ``left-behind`` in the working directory, ``HOME`` and ``TMPDIR``
* ``--no-stdin`` does not read the standard input at all (a pipe nobody reads)
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

UNIVERSE_SIZE = 10  # prog.c 6 + common.h 2 + util.c 2

_PROGRAM = r'''#!{python}
# gcda: {root}/obj/prog.gcda
# gcda: {root}/obj/util.gcda
import json, os, signal, sys, time

ROOT = {root!r}
OBJECTS = {{
    "obj/prog.gcda": {{"src/prog.c": [], "src/common.h": []}},
    "obj/util.gcda": {{"src/util.c": [], "src/common.h": []}},
}}


def read_input(argv):
    args = [a for a in argv if not a.startswith("-")]
    if args and os.path.isfile(args[0]):
        with open(args[0], "rb") as handle:
            return handle.read()
    return sys.stdin.buffer.read()


def write_gcda(covered):
    prefix = os.environ.get("GCOV_PREFIX", "")
    strip = int(os.environ.get("GCOV_PREFIX_STRIP", "0"))
    for relative, sources in OBJECTS.items():
        absolute = ROOT + "/" + relative
        parts = absolute.split("/")[1:]
        target = os.path.join(prefix, *parts[strip:]) if prefix else absolute
        os.makedirs(os.path.dirname(target), exist_ok=True)
        merged = {{}}
        if os.path.exists(target):
            with open(target) as handle:
                merged = json.load(handle)["records"]
        for source in sources:
            mine = covered.get((relative, source), [])
            have = merged.get(source, [])
            merged[source] = sorted(set(map(tuple, have)) | set(map(tuple, mine)))
        with open(target, "w") as handle:
            json.dump({{"records": merged}}, handle)


def observe_stdin():
    """What descriptor 0 and the environment look like, before anything is read (a read can change the atime)."""
    import errno, stat
    st = os.fstat(0)
    try:
        os.lseek(0, 0, os.SEEK_CUR)
        seekable = True
    except OSError as exc:
        seekable = False if exc.errno == errno.ESPIPE else None
    return {{
        "fifo": stat.S_ISFIFO(st.st_mode),
        "regular": stat.S_ISREG(st.st_mode),
        "seekable": seekable,
        "atime": int(st.st_atime),
        "mtime": int(st.st_mtime),
        "fd0": os.path.lexists("fd0"),
        "LANG": os.environ.get("LANG"),
        "LC_ALL": os.environ.get("LC_ALL"),
        "TZ": os.environ.get("TZ"),
    }}


def main():
    argv = sys.argv[1:]
    observe = next((a[len("--observe="):] for a in argv if a.startswith("--observe=")), None)
    report = observe_stdin() if observe is not None else None
    data = b"" if "--no-stdin" in argv else read_input(argv)
    if report is not None:
        report["bytes"] = len(data)
        report["sum"] = sum(data) % 2**32
        with open(observe, "w") as handle:
            json.dump(report, handle)
    covered = {{("obj/prog.gcda", "src/prog.c"): [[10, 0, 0]]}}

    def add(obj, source, branch):
        covered.setdefault((obj, source), []).append(list(branch))

    if b"A" in data:
        add("obj/prog.gcda", "src/prog.c", (11, 0, 0))
    if b"B" in data:
        add("obj/prog.gcda", "src/prog.c", (11, 0, 1))
    if "-x" in argv:
        add("obj/prog.gcda", "src/prog.c", (12, 0, 0))
    if b"U" in data:
        add("obj/util.gcda", "src/util.c", (5, 0, 0))
    if b"H" in data:
        add("obj/prog.gcda", "src/common.h", (3, 0, 1))
    if b"Q" in data:
        add("obj/util.gcda", "src/common.h", (3, 0, 0))
    for arg in argv:
        if arg.startswith("--pidfile="):
            with open(arg[len("--pidfile="):], "w") as handle:
                handle.write(str(os.getpid()))
        if arg.startswith("--probe="):
            with open(arg[len("--probe="):], "w") as handle:
                json.dump({{
                    "env": dict(os.environ),
                    "cwd": os.getcwd(),
                    "cwd_list": sorted(os.listdir(".")),
                    "home_list": sorted(os.listdir(os.environ["HOME"])),
                    "tmp_list": sorted(os.listdir(os.environ["TMPDIR"])),
                }}, handle)
        if arg == "--leave":
            for directory in (".", os.environ["HOME"], os.environ["TMPDIR"]):
                open(os.path.join(directory, "left-behind"), "w").close()
        if arg.startswith("--signals="):
            names = ("SIGHUP", "SIGINT", "SIGTERM")
            with open(arg[len("--signals="):], "w") as handle:
                handle.write(" ".join(
                    n + "=" + ("ign" if signal.getsignal(getattr(signal, n)) == signal.SIG_IGN else "dfl")
                    for n in names
                ))
        if arg.startswith("--daemon="):
            target = arg[len("--daemon="):]
            if os.fork() == 0:
                os.setsid()
                with open(target, "w") as handle:
                    handle.write(str(os.getpid()))
                time.sleep(30)
                os._exit(0)
            for _ in range(1000):
                if os.path.exists(target) and os.path.getsize(target):
                    break
                time.sleep(0.01)
        if arg.startswith("--flake="):
            counter = arg[len("--flake="):]
            runs = int(open(counter).read()) if os.path.exists(counter) else 0
            with open(counter, "w") as handle:
                handle.write(str(runs + 1))
            if runs % 2 == 0:
                add("obj/prog.gcda", "src/prog.c", (12, 0, 1))
    for arg in argv:
        if arg.startswith("--flake-odd="):
            counter = arg[len("--flake-odd="):]
            runs = int(open(counter).read()) if os.path.exists(counter) else 0
            with open(counter, "w") as handle:
                handle.write(str(runs + 1))
            if runs % 2 == 1:
                add("obj/prog.gcda", "src/prog.c", (12, 0, 0))
    if b"JUNK" in data:
        with open(os.path.join(os.environ["GCOV_PREFIX"], "junk.txt"), "w") as handle:
            handle.write("x")
    if b"FORGE" in data:
        target = os.path.join(os.environ["GCOV_PREFIX"], "obj", "prog.gcda")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as handle:
            json.dump({{"records": {{"src/prog.c": [[12, 0, 0]]}}}}, handle)
    if b"TAMPER" in data:
        worked = False
        for action in (
            lambda: open(__file__, "a").close(),
            lambda: os.unlink(__file__),
            lambda: open(os.path.join(os.path.dirname(__file__), "planted"), "w").close(),
        ):
            try:
                action()
                worked = True
            except OSError:
                pass
        if worked:
            sys.exit(5)
    if b"SLEEP" in data:
        time.sleep(30)
    if b"CRASH" in data:
        os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(5)
    write_gcda(covered)
    if b"FAIL" in data:
        sys.exit(3)


main()
'''

_LCOV = r'''#!{python}
import json, os, sys


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print("lcov: LCOV version 1.14-fake")
        return 0
    if os.environ.get("FAKE_LCOV_LOG"):  # every call's arguments, one JSON list per line
        with open(os.environ["FAKE_LCOV_LOG"], "a") as log:
            log.write(json.dumps(args) + "\n")
    initial = "--initial" in args
    directory = args[args.index("--directory") + 1]
    output = args[args.index("--output-file") + 1]
    if os.environ.get("FAKE_LCOV_FAIL"):
        print("geninfo: ERROR: fake failure", file=sys.stderr)
        return 1
    if os.environ.get("FAKE_LCOV_WARN"):
        print("geninfo: WARNING: something looks odd")
    if os.environ.get("FAKE_LCOV_EMPTY"):
        open(output, "w").close()
        return 0
    notes = []
    for current, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if name.endswith(".gcno"):
                notes.append(os.path.join(current, name))
    notes.sort()
    out = []
    for note in notes:
        stem = note[: -len(".gcno")]
        gcda = stem + ".gcda"
        if not initial and not os.path.exists(gcda):
            continue
        with open(note) as handle:
            records = json.load(handle)["records"]
        covered = {{}}
        if not initial:
            with open(gcda) as handle:
                covered = json.load(handle)["records"]
        for record in records:
            source = record["source"]
            have = set(map(tuple, covered.get(record["rel"], [])))
            out.append("TN:")
            out.append("SF:" + source)
            lines = sorted({{b[0] for b in record["branches"]}})
            for line in lines:
                hit = any(tuple(b) in have for b in record["branches"] if b[0] == line)
                out.append("FN:%d,fn_%d" % (line, line))
                out.append("FNDA:%d,fn_%d" % (1 if hit else 0, line))
            for line in lines:
                hit = any(tuple(b) in have for b in record["branches"] if b[0] == line)
                out.append("DA:%d,%d" % (line, 1 if hit else 0))
            for line, block, branch in record["branches"]:
                hit_here = tuple((line, block, branch)) in have
                hit_line = any(tuple(b) in have for b in record["branches"] if b[0] == line)
                if initial:
                    taken = "0"
                elif hit_here:
                    taken = "1"
                else:
                    taken = "0" if hit_line else "-"
                out.append("BRDA:%d,%d,%d,%s" % (line, block, branch, taken))
            out.append("end_of_record")
    if os.environ.get("FAKE_LCOV_EXTRA") and not initial:  # a branch the universe does not hold
        out += ["TN:", "SF:src/extra.c", "DA:1,1", "BRDA:1,0,0,1", "end_of_record"]
    with open(output, "w") as handle:
        handle.write("\n".join(out) + ("\n" if out else ""))
    if os.environ.get("FAKE_LCOV_DAMAGED") and not initial:  # lcov exits 0 and says so only in its output
        print("obj/prog.gcda:profile mismatch for 'fn_10'")
    if os.environ.get("FAKE_LCOV_FAIL_CAPTURE") and not initial:  # fails after writing a plausible file
        print("geninfo: ERROR: fake failure after the output was written", file=sys.stderr)
        return 1
    return 0


sys.exit(main())
'''


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _note(root: Path, relative_object: str, records: list[tuple[str, str, list[list[int]]]]) -> None:
    payload = {
        "records": [
            {"source": f"{root}/{rel}", "rel": rel, "branches": branches} for _name, rel, branches in records
        ]
    }
    target = root / relative_object
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


def make_build(root: Path) -> Path:
    """Create the fake gcov build at ``root`` (use a resolved path) and return it."""

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    prog_branches = [[10, 0, 0], [10, 0, 1], [11, 0, 0], [11, 0, 1], [12, 0, 0], [12, 0, 1]]
    header = [[3, 0, 0], [3, 0, 1]]
    util = [[5, 0, 0], [5, 0, 1]]
    _note(root, "obj/prog.gcno", [("p", "src/prog.c", prog_branches), ("h", "src/common.h", header)])
    _note(root, "obj/util.gcno", [("u", "src/util.c", util), ("h", "src/common.h", header)])
    _note(root, "obj/unlinked.gcno", [("x", "src/unlinked.c", [[1, 0, 0], [1, 0, 1]])])
    _write_executable(root / "bin" / "prog", _PROGRAM.format(python=sys.executable, root=str(root)))
    return root


def make_lcov(path: Path) -> Path:
    _write_executable(Path(path), _LCOV.format(python=sys.executable))
    return Path(path)
