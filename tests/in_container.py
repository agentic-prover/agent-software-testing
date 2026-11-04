#!/usr/bin/env python3
# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Run the test suite on Linux, in the toolchain image, so that the tests that need /proc are not skipped.

    python3 tests/in_container.py                     the whole suite
    python3 tests/in_container.py tests/test_case_isolation.py -k group

The checkout is mounted read-only; the pytest of the machine this runs on is mounted too (it is plain Python), so the
image needs nothing installed except Python 3.11 or later (the toolchain image of the runner has 3.10, whose pytest
needs two more packages). The tests use the fake toolchain, so the image needs no gcc. The container has no network, a
limit on processes and a name of its own, and it is removed when the run ends, also when this script is interrupted.
Docker has to be available. An image of another architecture runs under emulation and is slow.

Environment: AGENT_SOFTWARE_TESTING_IMAGE names the image, for example python:3.12-slim (no default, so that nothing is pulled
by surprise). The pytest of this machine has to be for the same minor version of Python as the one in the image.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("AGENT_SOFTWARE_TESTING_IMAGE")
LIMIT_S = 3600


def main(argv: list[str]) -> int:
    if not IMAGE:
        print("set AGENT_SOFTWARE_TESTING_IMAGE to an image with Python 3.11 or later (see the top of this file)")
        return 2
    try:
        import pytest
    except ImportError:
        print("pytest is not installed here")
        return 2
    site = Path(pytest.__file__).resolve().parent.parent
    name = f"agent-software-testing-tests-{os.getpid()}"
    scratch = tempfile.mkdtemp(prefix="agent-software-testing-in-container-")  # left in place: this script deletes nothing
    print(f"scratch space of the run: {scratch}")
    if True:
        command = [
            "docker", "run", "--rm", "--name", name, "--platform", "linux/amd64", "--network", "none", "--pids-limit", "4096",
            "-v", f"{ROOT}:/cr:ro", "-v", f"{site}:/hostsite:ro", "-v", f"{scratch}:/scratch",
            "-e", "PYTHONPATH=/hostsite:/cr/src:/cr/tests", "-e", "PYTHONDONTWRITEBYTECODE=1", "-w", "/cr", IMAGE,
            "python3", "-m", "pytest", "-p", "no:cacheprovider", "-q", "--basetemp=/scratch/tmp", *(argv or ["tests"]),
        ]
        try:
            return subprocess.run(command, timeout=LIMIT_S).returncode
        except subprocess.TimeoutExpired:
            print(f"the run took more than {LIMIT_S} s and was stopped")
            return 1
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # no-op when --rm already removed it


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
