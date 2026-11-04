#!/usr/bin/env python3
"""Check that the running image matches environment/toolchain.lock.json.

Run inside the replay image::

    python3 /opt/agent-software-testing/environment/verify_image.py

Exit status 0 when every declared version and required command matches, 1 otherwise,
2 when the lock file cannot be read. Standard library only; Python 3.10 or later.

STATUS: written for the image described by environment/Dockerfile. It has accepted the
`toolchain` image built from it once (linux/amd64, see environment/README.md); the parsing
of tool output below is otherwise checked only against the synthetic outputs in
environment/test_verify_image.py.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

DEFAULT_LOCK = Path(__file__).resolve().with_name("toolchain.lock.json")

Runner = Callable[[list[str]], str]


def run(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return f"<{exc}>"
    return result.stdout


def first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


def observed_versions(runner: Runner, os_release: str, python_version: str) -> dict[str, str]:
    """Extract the comparable version string of every declared tool."""

    pretty = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', os_release, re.M)
    glibc = re.search(r"(\d+\.\d+(?:\.\d+)?)\s*$", first_line(runner(["ldd", "--version"])))
    gcc = first_line(runner(["gcc", "-dumpfullversion"]))
    gcov = re.search(r"(\d+\.\d+\.\d+)", first_line(runner(["gcov", "--version"])))
    lcov = re.search(r"LCOV version (\S+)", first_line(runner(["lcov", "--version"])))
    clang = re.search(r"clang version (\S+)", first_line(runner(["clang-15", "--version"])))
    return {
        "os": pretty.group(1) if pretty else "",
        "glibc": glibc.group(1) if glibc else "",
        "gcc": gcc,
        "gcov": gcov.group(1) if gcov else "",
        "lcov": lcov.group(1).lstrip("v") if lcov else "",
        "clang": clang.group(1) if clang else "",
        "python": python_version,
    }


def matches(key: str, declared: str, observed: str) -> bool:
    if key == "os":
        # declared "Ubuntu 22.04 (jammy)", observed "Ubuntu 22.04.5 LTS"
        release = re.search(r"\d+\.\d+", declared)
        return bool(release) and declared.split()[0] in observed and release.group(0) in observed
    if key == "lcov":
        return observed.startswith(declared)
    return observed == declared


def verify(lock: Mapping[str, object], runner: Runner, os_release: str, python_version: str, which: Callable[[str], str | None]) -> list[str]:
    problems: list[str] = []
    declared = lock["declared_versions"]
    assert isinstance(declared, dict)
    seen = observed_versions(runner, os_release, python_version)
    for key, want in declared.items():
        have = seen.get(key, "")
        if not matches(key, str(want), have):
            problems.append(f"{key}: declared {want!r}, observed {have!r}")
    probe = lock.get("runtime_probe", {})
    assert isinstance(probe, dict)
    for command in probe.get("required_commands", []):
        if which(command) is None:
            problems.append(f"missing command: {command}")
    if probe.get("require_matching_gcc_gcov_major"):
        gcc_major = seen["gcc"].split(".")[0]
        gcov_major = seen["gcov"].split(".")[0]
        if not gcc_major or gcc_major != gcov_major:
            problems.append(f"gcc major {gcc_major!r} differs from gcov major {gcov_major!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args(argv)
    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read lock file: {exc}", file=sys.stderr)
        return 2
    try:
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        os_release = ""
    problems = verify(lock, run, os_release, platform.python_version(), shutil.which)
    for problem in problems:
        print(problem)
    print("image matches the lock file" if not problems else f"{len(problems)} mismatches")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
