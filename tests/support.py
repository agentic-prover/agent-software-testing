# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""A small rig for the tests: a fake coverage build, a bundle writer and the command line.

The fake toolchain (tests/fake_toolchain.py) stands in for gcc, gcov and lcov, so the whole replay runs on any
machine. What the fake program covers for each input is listed at the top of that file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bundles import tool_doc
from fake_toolchain import make_build, make_lcov

from agent_software_testing.bundle import BundleWriter
from agent_software_testing.cli import main

#: the branches of the fake program's universe that an input covers, by name
BASE = "src/prog.c:10:0:0"  # every run
A, B = "src/prog.c:11:0:0", "src/prog.c:11:0:1"
DASH_X = "src/prog.c:12:0:0"
U = "src/util.c:5:0:0"
H, Q = "src/common.h:3:0:1", "src/common.h:3:0:0"
UNIVERSE_SIZE = 10


@dataclass
class Outcome:
    code: int
    out: str
    err: str
    dir: Path

    def json(self, name: str) -> Any:
        return json.loads((self.dir / name).read_text())

    @property
    def summary(self) -> dict:
        return self.json("replay.json")

    @property
    def covered(self) -> list[str]:
        return self.json("coverage-branches.json")["covered_branch_keys"]


class Rig:
    def __init__(self, tmp_path: Path, programs_dir: Path, capsys, monkeypatch):
        self.tmp = tmp_path
        self.programs = programs_dir
        self.capsys = capsys
        self.monkeypatch = monkeypatch
        self.build = make_build((tmp_path / "build").resolve())
        self.lcov = make_lcov(tmp_path / "tools" / "lcov")
        self.work = tmp_path / "work"
        self.work.mkdir()
        self._count = 0

    def bundle(self, cases: list[dict], *, wall_s: int = 3600, program: str = "demo", tool: dict | None = None) -> Path:
        """A bundle of ``cases`` (keyword arguments of ``BundleWriter.add_case``)."""

        self._count += 1
        writer = BundleWriter(self.tmp / f"bundle-{self._count}", tool or tool_doc(program, wall_s=wall_s))
        for case in cases:
            writer.add_case(**case)
        return writer.finish()

    def cli(self, *argv: Any) -> tuple[int, str, str]:
        code = main([str(a) for a in argv])
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def replay(self, bundle: Path, *extra: Any, out: Path | None = None, programs: Path | None = None) -> Outcome:
        self._count += 1
        out = out or self.tmp / f"out-{self._count}"
        workers = [] if "--workers" in map(str, extra) else ["--workers", 2]  # two shards are enough to test sharding
        code, text, err = self.cli(
            "replay", bundle, "--build", self.build, "--out", out, "--programs", programs or self.programs,
            "--lcov", self.lcov, "--work-dir", self.work, "--quiet", *workers, *extra,
        )
        return Outcome(code, text, err, out)

    def validate(self, bundle: Path, *extra: Any) -> tuple[int, dict]:
        code, text, _err = self.cli("validate", bundle, "--programs", self.programs, "--json", *extra)
        return code, json.loads(text)


def case(t_ms: int = 0, stdin: bytes | None = None, **more: Any) -> dict:
    """One case for ``Rig.bundle``; the fake program reads its input from standard input."""

    return {"t_ms": t_ms, "stdin": stdin, "timeout_ms": 5000, **more}
