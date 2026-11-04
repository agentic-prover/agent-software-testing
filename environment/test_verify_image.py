"""Tests for environment/verify_image.py (synthetic tool output only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_image as vi  # noqa: E402

LOCK = json.loads((Path(__file__).resolve().parent / "toolchain.lock.json").read_text())

OUTPUTS = {
    ("ldd", "--version"): "ldd (Ubuntu GLIBC 2.35-0ubuntu3.8) 2.35\nCopyright",
    ("gcc", "-dumpfullversion"): "11.4.0\n",
    ("gcov", "--version"): "gcov (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0\n",
    ("lcov", "--version"): "lcov: LCOV version 1.14\n",
    ("clang-15", "--version"): "Ubuntu clang version 15.0.7\nTarget: x86_64\n",
}
OS_RELEASE = 'PRETTY_NAME="Ubuntu 22.04.5 LTS"\nNAME="Ubuntu"\n'


def runner(argv):
    return OUTPUTS.get(tuple(argv), "")


def check(**changes):
    outputs = dict(OUTPUTS)
    outputs.update(changes.pop("outputs", {}))
    return vi.verify(
        LOCK,
        lambda argv: outputs.get(tuple(argv), ""),
        changes.get("os_release", OS_RELEASE),
        changes.get("python", "3.10.12"),
        changes.get("which", lambda name: f"/usr/bin/{name}"),
    )


def test_matching_image_has_no_problems() -> None:
    assert check() == []


def test_version_mismatches_are_reported() -> None:
    problems = check(outputs={("gcc", "-dumpfullversion"): "12.3.0\n"})
    assert any(p.startswith("gcc:") for p in problems)
    assert any("gcc major" in p for p in problems)
    assert any(p.startswith("clang:") for p in check(outputs={("clang-15", "--version"): "clang version 16.0.6"}))
    assert any(p.startswith("python:") for p in check(python="3.12.1"))
    assert any(p.startswith("os:") for p in check(os_release='PRETTY_NAME="Debian 12"\n'))


def test_missing_commands_are_reported() -> None:
    problems = check(which=lambda name: None if name == "lcov" else "/x")
    assert "missing command: lcov" in problems


def test_unreadable_lock_gives_status_2(tmp_path: Path) -> None:
    assert vi.main(["--lock", str(tmp_path / "nope.json")]) == 2
