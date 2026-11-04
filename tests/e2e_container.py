#!/usr/bin/env python3
# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""A real replay in the container: real gcc, gcov and lcov on the pinned build of one program. Slow, so it is a command
of its own and not part of the test suite.

    AGENT_SOFTWARE_TESTING_BUILDS_IMAGE=agent-software-testing:builds python3 tests/e2e_container.py [PROGRAM]

The fake toolchain of the suite proves the logic; this proves the same logic against what the real tools print. It
replays the example bundle of the program (docs/examples/<program>-open, default gzip) twice, with other worker counts
and shard modes, in a container without network and with a read-only root, and checks

  * the universe is the one the program definition names (the replay stops otherwise),
  * some but not all branches are covered, no case could not be started and the second replay of the flake check agrees,
  * the two replays give the same branch sets and the same snapshots,
  * every file written validates with the validator of the runner.

The image has to hold the coverage build of the program at /opt/agent-software-testing/builds/<program>/gcov (an image made by
`docker build --target builds`, see environment/README.md). The checkout is mounted read-only, the outputs go to a new
folder that is left in place and named at the end. Containers have a name of their own and are removed by name.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("AGENT_SOFTWARE_TESTING_BUILDS_IMAGE")
LIMIT_S = 3600
FLAGS = [
    "--platform", "linux/amd64", "--network", "none", "--read-only", "--pids-limit", "4096", "--tmpfs", "/tmp:exec,size=2g",
    "-e", "PYTHONPATH=/cr/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
]


def container(name: str, scratch: str, *command: str) -> int:
    run = ["docker", "run", "--rm", "--name", name, *FLAGS, "-v", f"{ROOT}:/cr:ro", "-v", f"{scratch}:/work", "-w", "/work", IMAGE, *command]
    try:
        return subprocess.run(run, timeout=LIMIT_S).returncode
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main(argv: list[str]) -> int:
    if not IMAGE:
        print("set AGENT_SOFTWARE_TESTING_BUILDS_IMAGE to the image that holds the coverage builds (see the top of this file)")
        return 2
    program = argv[0] if argv else "gzip"
    bundle = f"/cr/docs/examples/{program}-open"
    definition = json.loads((ROOT / "programs" / program / "program.json").read_text())
    scratch = tempfile.mkdtemp(prefix="agent-software-testing-e2e-")
    print(f"outputs go to {scratch}", flush=True)
    problems: list[str] = []

    replays = [("a", ["--workers", "2", "--shard-mode", "process"]), ("b", ["--workers", "1", "--shard-mode", "thread"])]
    for label, options in replays:
        code = container(
            f"agent-software-testing-e2e-{os.getpid()}-{label}", scratch, "python3", "-m", "agent_software_testing", "replay", bundle,
            "--build", f"/opt/agent-software-testing/builds/{program}/gcov", "--out", f"/work/out-{label}", "--programs", "/cr/programs",
            "--work-dir", "/tmp", "--quiet", *options,
        )
        if code != 0:
            problems.append(f"replay {label} exited with status {code}")
    if problems:
        print("\n".join(problems))
        return 1

    out = {label: Path(scratch) / f"out-{label}" for label, _ in replays}
    summary = {label: json.loads((path / "replay.json").read_text()) for label, path in out.items()}
    for label, s in summary.items():
        if s["branch_total"] != definition["branch_total"]:
            problems.append(f"{label}: universe of {s['branch_total']} branches, the definition says {definition['branch_total']}")
        if not 0 < s["branch_covered"] < s["branch_total"]:
            problems.append(f"{label}: {s['branch_covered']} of {s['branch_total']} branches covered")
        if s["status_counts"]["setup_error"] or s["cases"]["replayed"] != s["cases"]["total"]:
            problems.append(f"{label}: cases that could not be run or were skipped: {s['status_counts']} {s['cases']}")
        if s["flake"] != {"checked": True, "branches_differing": 0, "only_in_first": 0, "only_in_second": 0}:
            problems.append(f"{label}: the second replay differs: {s['flake']}")
    a, b = summary["a"], summary["b"]
    if a["snapshots"] != b["snapshots"] or a["status_counts"] != b["status_counts"] or a["bundle"] != b["bundle"] or a["build"] != b["build"]:
        problems.append("the two replays differ in snapshots, status counts or digests")
    for name in ("coverage-branches.json", "timeline.csv"):
        if (out["a"] / name).read_bytes() != (out["b"] / name).read_bytes():
            problems.append(f"{name} differs between the two replays")
    for label, path in out.items():
        for document in ("replay.json", "coverage-branches.json"):
            done = subprocess.run(
                [sys.executable, "-m", "agent_software_testing", "validate", str(path / document)],
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True,
            )
            if done.returncode != 0:
                problems.append(f"{label}/{document} does not validate: {done.stdout.strip()[:200]}")
    print("\n".join(problems) if problems else f"ok: {program}, {a['branch_covered']} of {a['branch_total']} branches covered, both replays agree")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
