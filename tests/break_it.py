#!/usr/bin/env python3
# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Break the code on purpose and see whether the tests notice.

    python3 tests/break_it.py                  run every bug of tests/break_it_bugs.py
    python3 tests/break_it.py --list           list the bugs and the promise each one breaks
    python3 tests/break_it.py --only P3-a,P3-b run some of them
    python3 tests/break_it.py --jobs 4         run four at a time (default 4)

Every bug is one small edit of the source (an exact piece of text and its replacement), the kind a person or an
agent makes by accident: an off-by-one, a check that always passes, a comparison that was dropped. The edit is made
in a throwaway copy of this checkout, the tests run there and the result is printed as CAUGHT (some test failed) or
MISSED (all tests passed). The checkout itself is never touched. The copies are left in the work folder (--work, by
default a new folder in the system temporary directory) and are not removed by this script.

Bugs whose id starts with OK- are harmless changes (a reworded message, a tuned constant). They have to leave the tests
passing; a test that fails on one is too close to the wording or the tuning of the code.

A bug file may name, for a source file, the test files that can possibly notice a bug in it (TESTS_FOR); the other
test files are not run for that bug. Giving tests on the command line overrides this.

Exit status: 0 when every bug was caught and every harmless change passed, 1 otherwise, 2 when the script itself is wrong (a bug whose
text is not found exactly once, or a suite that fails before any bug is applied).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", re.M)


@dataclass
class Bug:
    id: str  # "<promise>-<letter>"
    file: str  # path relative to the checkout
    old: str  # exact text, found exactly once
    new: str
    what: str  # the bug, in a few plain words


def load_bugs() -> tuple[list[Bug], dict[str, list[str]]]:
    spec = importlib.util.spec_from_file_location("break_it_bugs", Path(__file__).with_name("break_it_bugs.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["break_it_bugs"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return [Bug(*row) for row in module.BUGS], getattr(module, "TESTS_FOR", {})


def run_pytest(directory: Path, args: list[str], limit_s: float, container: bool = False) -> tuple[int, str, bool]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if container:  # the tests of the copy, run on Linux by the copy's own tests/in_container.py
        command = [sys.executable, "tests/in_container.py", "--tb=no", "-rfE", *args]
    else:
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", *args]
    try:
        done = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True, timeout=limit_s)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return -1, out, True
    return done.returncode, done.stdout, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", help="comma-separated bug ids")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--work", help="folder for the throwaway copies (default: a new one in the system temp folder)")
    parser.add_argument("--limit", type=float, default=900.0, help="seconds one test run may take (a hang counts as caught)")
    parser.add_argument("--json", help="write the full result, with the failing tests of every bug, to this file")
    parser.add_argument("--container", action="store_true", help="run the tests of each copy on Linux in a container (tests/in_container.py; set AGENT_SOFTWARE_TESTING_IMAGE)")
    parser.add_argument("tests", nargs="*", default=["tests"], help="what to run in the copy (default: tests)")
    args = parser.parse_args()

    bugs, tests_for = load_bugs()
    if args.only:
        wanted = set(args.only.split(","))
        bugs = [b for b in bugs if b.id in wanted or b.id.split("-")[0] in wanted]
    if args.list:
        for bug in bugs:
            print(f"{bug.id:8} {bug.file:38} {bug.what}")
        return 0

    work = Path(args.work) if args.work else Path(tempfile.gettempdir()) / f"agent-software-testing-break-it-{time.strftime('%Y%m%d-%H%M%S')}"
    work.mkdir(parents=True, exist_ok=False)
    jobs = max(1, min(args.jobs, len(bugs) or 1))
    print(f"working copies in {work} (left in place)", flush=True)

    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
    slots: "queue.Queue[Path]" = queue.Queue()
    originals: dict[str, str] = {}
    for k in range(jobs):
        slot = work / f"copy-{k}"
        shutil.copytree(ROOT, slot, ignore=ignore)
        slots.put(slot)
    for bug in bugs:
        originals.setdefault(bug.file, (ROOT / bug.file).read_text(encoding="utf-8"))

    # every edit must find its text exactly once, or the table would flatter the tests
    stale = [b.id for b in bugs if originals[b.file].count(b.old) != 1]
    if stale:
        print("these bugs do not find their text exactly once in the source (update tests/break_it_bugs.py):", ", ".join(stale))
        return 2

    # the unbroken copy must pass, and its test list gives the tests that never caught anything
    code, out, hung = run_pytest(work / "copy-0", [*args.tests, "--collect-only"], args.limit, args.container)
    all_tests = [line.strip() for line in out.splitlines() if "::" in line]
    started = time.monotonic()
    code, out, hung = run_pytest(work / "copy-0", args.tests, args.limit, args.container)
    if code != 0 or hung:
        print("the tests fail on the unbroken code; fix that first\n" + out[-3000:])
        return 2
    print(f"unbroken code: all {len(all_tests)} tests pass in {time.monotonic() - started:.1f} s", flush=True)

    def attempt(bug: Bug) -> dict:
        slot = slots.get()
        try:
            target = slot / bug.file
            target.write_text(originals[bug.file].replace(bug.old, bug.new, 1), encoding="utf-8")
            t0 = time.monotonic()
            code, out, hung = run_pytest(slot, tests_for.get(bug.file, args.tests) if args.tests == ["tests"] else args.tests, args.limit, args.container)
            failed = sorted(set(FAILED_RE.findall(out)))
            if bug.id.startswith("OK-"):  # a harmless change: the tests have to keep passing
                verdict = "PASSES" if code == 0 and not hung else "BROKE"
            elif hung:
                verdict = "CAUGHT (hang)"
            elif code == 0:
                verdict = "MISSED"
            elif code == 1:
                verdict = "CAUGHT"
            else:
                verdict = f"ERROR (pytest exit {code})"
            return {"id": bug.id, "file": bug.file, "what": bug.what, "verdict": verdict, "failed": failed,
                    "seconds": round(time.monotonic() - t0, 1)}
        finally:
            (slot / bug.file).write_text(originals[bug.file], encoding="utf-8")
            slots.put(slot)

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for row in pool.map(attempt, bugs):
            results.append(row)
            first = row["failed"][0].split("::", 1)[-1] if row["failed"] else ""
            print(f"{row['verdict']:14} {row['id']:7} {len(row['failed']):3} failing  {row['seconds']:6.1f}s  {row['what']}"
                  + (f"  [{first}]" if first else ""), flush=True)

    caught = sum(r["verdict"].startswith("CAUGHT") for r in results)
    missed = [r for r in results if r["verdict"] == "MISSED"]
    errors = [r for r in results if r["verdict"].startswith("ERROR") or r["verdict"] == "BROKE"]
    controls = [r for r in results if r["id"].startswith("OK-")]
    print(f"\n{len(results) - len(controls)} bugs: {caught} caught, {len(missed)} missed, {len(errors)} errors; "
          f"{sum(r['verdict'] == 'PASSES' for r in controls)} of {len(controls)} harmless changes left the tests passing")
    used = {t for r in results if not r["id"].startswith("OK-") for t in r["failed"]}
    idle = [t for t in all_tests if t not in used]
    print(f"{len(all_tests) - len(idle)} of {len(all_tests)} tests failed for at least one bug")
    if idle:
        print("tests that caught none of the bugs:")
        for name in idle:
            print("  " + name)
    for r in missed:
        print("MISSED: " + r["id"] + " " + r["what"])
    if args.json:
        Path(args.json).write_text(json.dumps({"bugs": results, "tests": all_tests, "idle": idle}, indent=1), encoding="utf-8")
    return 1 if missed or errors else 0


if __name__ == "__main__":
    sys.exit(main())
