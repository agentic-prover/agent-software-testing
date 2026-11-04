# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

FIXTURE_PROGRAMS = TESTS / "fixtures" / "programs"


@pytest.fixture
def programs_dir() -> Path:
    return FIXTURE_PROGRAMS


def gnu_toolchain_missing_reason() -> str | None:
    """Why the real gcc/gcov/lcov end-to-end test cannot run here, or ``None``."""

    gcc = shutil.which("gcc")
    if gcc is None:
        return "gcc is not installed"
    try:
        banner = subprocess.run([gcc, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return "gcc cannot be run"
    if "clang" in banner.lower() or "apple" in banner.lower():
        return "gcc is a wrapper for clang here, whose coverage files lcov cannot read"
    if shutil.which("gcov") is None:
        return "gcov is not installed"
    if shutil.which("lcov") is None:
        return "lcov is not installed"
    return None


@pytest.fixture
def rig(tmp_path, programs_dir, capsys, monkeypatch):
    from support import Rig

    return Rig(tmp_path, programs_dir, capsys, monkeypatch)
