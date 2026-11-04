#!/usr/bin/env python3
# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""How fast the runner replays cases with shards served by threads and by worker processes.

Builds a small C program with ``gcc --coverage``, writes a synthetic bundle of short cases (and a variant whose
cases sleep 50 ms, like programs that wait for a file or a timer), and replays one batch of it over 1, 4, 8 and 16
shards in each shard mode. Prints cases per second and the CPU used (100 % is one core, taken from the kernel's
counters for the whole machine on Linux, so processes that are not children of the runner count too). The
interval snapshots and the lcov captures are not part of the timing: they do not depend on the shard mode.

    python tools/bench_shards.py --cases 600 --workers 1,4,8,16
    python tools/bench_shards.py --idle-processes 3000 --modes thread,process-fullscan,process

``process-fullscan`` is the process mode with the child subreaper switched off, so every case scans all of
``/proc``, as in the thread mode. ``--idle-processes N`` starts N sleeping processes first, a busy node in
miniature: the scan of the thread mode then reads that many ``environ`` files for every case.

Needs gcc. Run it in the container of ``environment/Dockerfile``; the numbers of an emulated machine (Rosetta) are
slow in absolute terms, compare the ratios.
"""

from __future__ import annotations

import argparse
import os
import random
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_software_testing.bundle import Bundle, BundleWriter, make_tool_document  # noqa: E402
from agent_software_testing.gcov import choose_compile_root, set_directories_writable  # noqa: E402
from agent_software_testing.program import program_from_document  # noqa: E402
from agent_software_testing.replay import ReplayOptions, _case_settings, _Runner, _Shard  # noqa: E402
from agent_software_testing.sandbox import Limits, remove_tree  # noqa: E402
from agent_software_testing.shards import ShardPool  # noqa: E402

PROGRAM = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    int digits = 0, letters = 0, other = 0, c;
    for (int i = 1; i + 1 < argc; i++)
        if (strcmp(argv[i], "--nap") == 0) usleep(atoi(argv[i + 1]) * 1000);
    while ((c = getchar()) != EOF) {
        if (c >= '0' && c <= '9') digits++;
        else if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) letters++;
        else other++;
    }
    if (digits > letters) return 1;
    if (other > 40) return 2;
    return 0;
}
"""
IDLE = r"""
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    for (int i = 0; i < atoi(argv[1]); i++) {
        pid_t pid = fork();
        if (pid == 0) for (;;) pause();
        if (pid < 0) break;
    }
    for (;;) pause();
}
"""
MODES = {
    "thread": {"shard_mode": "thread", "subreaper": True},
    "process": {"shard_mode": "process", "subreaper": True},
    "process-fullscan": {"shard_mode": "process", "subreaper": False},
}


def run(*command: str, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def build_program(root: Path) -> Path:
    for name in ("src", "obj", "bin"):
        (root / name).mkdir(parents=True)
    (root / "src" / "prog.c").write_text(PROGRAM)
    run("gcc", "--coverage", "-O0", "-c", "-o", str(root / "obj" / "prog.o"), str(root / "src" / "prog.c"), cwd=root)
    run("gcc", "--coverage", "-o", str(root / "bin" / "prog"), str(root / "obj" / "prog.o"), cwd=root)
    return root


def program_document() -> dict:
    return {
        "schema": "agent-software-testing/program@0.1",
        "id": "demo",
        "name": "Demo",
        "version": "1.0",
        "source": {"url": "https://example.invalid/demo.tar.gz", "sha256": "1" * 64, "directory": "demo"},
        "build": {"configure": [], "notes": ""},
        "binary": "bin/prog",
        "branch_total": 8,
        "fixed_track": None,
        "inputs": ["argv", "files", "stdin"],
        "status": "measured",
    }


def write_bundle(root: Path, count: int, nap_ms: int) -> Path:
    rng = random.Random(5)
    tool = make_tool_document(name="bench", program_id="demo", source_sha256="1" * 64, wall_s=60)
    writer = BundleWriter(root, tool)
    alphabet = b"0123456789abcdefghijklmnopqrstuvwxyz ,.;:!?"
    for _ in range(count):
        data = bytes(rng.choice(alphabet) for _ in range(rng.randrange(8, 200)))
        writer.add_case(t_ms=0, stdin=data, argv=[b"--nap", str(nap_ms).encode()] if nap_ms else [], timeout_ms=10_000)
    return writer.finish()


def cpu_seconds() -> float:
    """CPU time used by everything on the machine (Linux), or by this process and its waited-for children."""

    try:
        with open("/proc/stat") as handle:
            fields = [int(v) for v in handle.readline().split()[1:]]
        # user nice system idle iowait irq softirq steal: everything but idle and iowait
        return (sum(fields[:3]) + sum(fields[5:8])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
        return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def measure(bundle_root: Path, build: Path, program, work: Path, mode: str, workers: int) -> dict:
    settings = MODES[mode]
    options = ReplayOptions(workers=workers, shard_mode=settings["shard_mode"])
    binary = build / program.binary
    strip = len(PurePosixPath(choose_compile_root(build, binary, None).as_posix()).parts) - 1
    shards = [_Shard(i, work / f"shard-{i:03d}", build, program, strip, frozenset()) for i in range(workers)]
    bundle = Bundle(bundle_root)
    pool = None
    started = time.perf_counter()
    try:
        if settings["shard_mode"] == "process":
            pool = ShardPool.start(
                bundle,
                [s.directory for s in shards],
                _case_settings(shards, "prog", options, Limits(), None, False),
                scan_strays=True,
                subreaper=settings["subreaper"],
            )
        start_s = time.perf_counter() - started
        runner = _Runner(bundle, shards, "prog", options, Limits(), None, pool=pool)
        cases = list(bundle.iter_cases())
        cpu0, wall0 = cpu_seconds(), time.perf_counter()
        count = runner.run_batch(cases)
        wall, cpu = time.perf_counter() - wall0, cpu_seconds() - cpu0
    finally:
        if pool is not None:
            pool.close()
        for shard in shards:
            set_directories_writable(shard.directories, True)
        remove_tree(work)
        work.mkdir()
    return {
        "mode": mode,
        "workers": workers,
        "cases": count,
        "seconds": wall,
        "rate": count / wall,
        "cpu": 100 * cpu / wall,
        "start": start_s,
        "status": dict(sorted(runner.status_counts.items())),
        "strays": runner.stray_killed,
    }


def start_idle(root: Path, count: int) -> subprocess.Popen:
    (root / "idle.c").write_text(IDLE)
    run("gcc", "-O1", "-o", str(root / "idle"), str(root / "idle.c"), cwd=root)
    process = subprocess.Popen([str(root / "idle"), str(count)], start_new_session=True, stdout=subprocess.DEVNULL)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            if sum(1 for name in os.listdir("/proc") if name.isdigit()) >= count:
                return process
        except OSError:
            break
        time.sleep(0.5)
    return process


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", type=int, default=600, help="cases per run of the fast variant (default 600)")
    parser.add_argument("--nap-cases", type=int, default=0, help="cases per run of the 50 ms variant (default: cases / 3)")
    parser.add_argument("--nap-ms", type=int, default=50)
    parser.add_argument("--workers", default="1,4,8,16", help="comma-separated shard counts (default 1,4,8,16)")
    parser.add_argument("--modes", default="thread,process", help=f"comma-separated, from {', '.join(MODES)}")
    parser.add_argument("--variants", default="fast,nap", help="fast, nap or both")
    parser.add_argument("--idle-processes", type=int, default=0, help="sleeping processes to start first (a busy node)")
    parser.add_argument("--work-dir", help="parent directory for scratch files (default: the system temp directory)")
    args = parser.parse_args()
    modes = args.modes.split(",")
    workers_list = [int(v) for v in args.workers.split(",")]
    for mode in modes:
        if mode not in MODES:
            parser.error(f"unknown mode {mode!r}")
    if shutil.which("gcc") is None:
        parser.error("gcc is needed to build the test program")
    nap_cases = args.nap_cases or max(1, args.cases // 3)
    work_root = Path(os.path.abspath(tempfile.mkdtemp(prefix="bench-shards-", dir=args.work_dir)))
    idle = None
    results = []
    try:
        build = build_program(work_root / "build")
        program = program_from_document(program_document())
        bundles = {
            "fast": write_bundle(work_root / "bundle-fast", args.cases, 0),
            "nap": write_bundle(work_root / "bundle-nap", nap_cases, args.nap_ms),
        }
        if args.idle_processes:
            idle = start_idle(work_root, args.idle_processes)
        scratch = work_root / "shards"
        scratch.mkdir()
        processes = sum(1 for name in os.listdir("/proc") if name.isdigit()) if os.path.isdir("/proc") else None
        print(f"cpus {len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()}, "
              f"processes on the machine {processes}, python {sys.version.split()[0]}")
        print(f"{'variant':8} {'mode':17} {'workers':>7} {'cases':>6} {'seconds':>8} {'cases/s':>8} {'cpu %':>7} {'start s':>8}  status counts")
        for variant in args.variants.split(","):
            for workers in workers_list:
                for mode in modes:
                    row = measure(bundles[variant], build, program, scratch, mode, workers)
                    row["variant"] = variant
                    results.append(row)
                    print(
                        f"{variant:8} {mode:17} {workers:>7} {row['cases']:>6} {row['seconds']:>8.2f} {row['rate']:>8.1f} "
                        f"{row['cpu']:>7.0f} {row['start']:>8.2f}  {row['status']} strays {row['strays']}",
                        flush=True,
                    )
        # the modes must agree on what happened to the cases
        for variant in args.variants.split(","):
            for workers in workers_list:
                same = {(tuple(r['status'].items()), r['strays']) for r in results if r["variant"] == variant and r["workers"] == workers}
                if len(same) != 1:
                    print(f"DIFFERENT status counts between the modes for {variant}, {workers} workers: {same}")
                    return 1
        if "thread" in modes:
            print("\nratio of cases/s to the thread mode")
            print(f"{'variant':8} {'mode':17} " + " ".join(f"{w:>7}" for w in workers_list))
            for variant in args.variants.split(","):
                for mode in modes:
                    if mode == "thread":
                        continue
                    ratios = []
                    for workers in workers_list:
                        rate = {r["mode"]: r["rate"] for r in results if r["variant"] == variant and r["workers"] == workers}
                        ratios.append(rate[mode] / rate["thread"])
                    print(f"{variant:8} {mode:17} " + " ".join(f"{r:>7.2f}" for r in ratios))
        return 0
    finally:
        if idle is not None:
            try:
                os.killpg(idle.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            idle.wait()
        for path in (work_root,):
            try:
                remove_tree(path)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
