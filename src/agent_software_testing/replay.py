# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""The reference runner: replay a bundle and measure branch coverage (contract section 4).

Protocol
--------
1. The bundle is validated (cases beyond the budget are skipped and counted
   unless ``strict_budget`` is set).
2. The universe is captured from the pinned gcov build with ``lcov --capture
   --initial`` over the ``.gcno`` files the binary links; its size must equal
   the program's ``branch_total``.
3. Cases run in ``t_ms`` order in snapshot intervals (default every 900 s up to
   the budget). Each interval is spread over worker shards. A shard is a
   private copy of the build tree that the program cannot write (binary,
   notes files and sources are read-only; nothing is hard-linked to the
   original build) and a separate tree for its ``.gcda`` files, the only place
   the program writes profile data (``GCOV_PREFIX`` and
   ``GCOV_PREFIX_STRIP``). Before a capture the runner accepts only regular
   files named like the ``.gcda`` files of the objects the binary links, and
   copies them next to the notes files for lcov. After an interval the covered
   keys of all shards are unioned into the cumulative set. A shard is served
   by a worker process of its own for the whole replay (shard mode
   ``process``, the default; see :mod:`agent_software_testing.shards`) or by a thread of
   the runner (shard mode ``thread``); the results are the same.
4. The final corpus is replayed a second time on fresh shards and the number of
   branches that differ is reported (flake check).
5. ``replay.json``, ``timeline.csv``, ``coverage-branches.json`` and
   ``snapshots/<t_s>.json`` are written.

Two profiles exist. ``standard`` is the protocol of the specification.
``canonical-2026`` reproduces the earlier measurement pipeline for equivalence
tests (SPEC.md section 11.17): whole-second bucketing, the last snapshot takes
every remaining case, a 30 s limit for every case and no resource limits.

The union of the shards' covered keys equals what ``lcov -a`` over their
captures plus the initial capture would count: a key is covered exactly when
one shard's count is above zero.
"""

from __future__ import annotations

import dataclasses
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator

from . import FORMAT_VERSION, __version__
from .bundle import Bundle
from .case import Case
from .common import (
    MAX_SNAPSHOT_INTERVALS,
    SCHEMA_REPLAY,
    AgentSoftwareTestingError,
    IntegrityError,
    branch_set_document,
    default_workers,
    printable,
    sha256_file,
    write_json,
)
from .gcov import (
    GcovTools,
    Universe,
    capture_covered,
    choose_compile_root,
    collect_profile_data,
    copy_tree,
    initial_capture,
    set_directories_writable,
)
from .hostzone import canonical_zone_warning, probe_local_zone
from .program import Program
from .sandbox import TIMEOUT_CLOCKS, Limits, RunResult, remove_tree, run_case
from .shards import SHARD_MODES, CaseSettings, ShardPool, ShardWorkerError, processes_available
from .validate import validate_bundle

STATUSES = ("ok", "nonzero_exit", "terminated_by_signal", "timeout", "setup_error")
_STOP = object()

#: The protocol of the specification.
PROFILE_STANDARD = "standard"
#: The earlier measurement pipeline, for equivalence tests (SPEC.md section 11.17).
PROFILE_CANONICAL_2026 = "canonical-2026"
PROFILES = (PROFILE_STANDARD, PROFILE_CANONICAL_2026)
#: The time limit every case gets under the ``canonical-2026`` profile.
CANONICAL_TIMEOUT_MS = 30_000
#: A timeout that used less than this share of its limit as CPU time was not computing for most of it: the node was
#: busy, or the case waited (``timeouts.under_load`` in ``replay.json``).
UNDER_LOAD_CPU_SHARE = 0.5
#: Most case ids listed under ``timeouts.under_load_case_ids``.
MAX_LISTED_TIMEOUT_IDS = 100
#: Concurrent lcov runs when nothing else is set: each is a Perl process that
#: holds the coverage data of a whole build in memory.
DEFAULT_CAPTURE_JOBS = 8
#: Shards are copied of the build tree by this many threads at a time.
COPY_JOBS = 8
#: How the shards are served when ``ReplayOptions.shard_mode`` is not set: one worker process per shard where
#: processes can be started, threads of the runner otherwise.
DEFAULT_SHARD_MODE = "process" if processes_available() else "thread"


class ReplayError(AgentSoftwareTestingError):
    """The replay cannot produce a trustworthy measurement."""


@dataclass
class ReplayOptions:
    interval_s: int = 900
    workers: int | None = None
    tools: GcovTools = field(default_factory=GcovTools)
    #: Resource limits of the programs; ``None`` takes them from the profile.
    limits: Limits | None = None
    #: ``standard`` or ``canonical-2026``.
    profile: str = PROFILE_STANDARD
    #: Most lcov runs at once (default ``min(workers, 8)``).
    capture_jobs: int | None = None
    #: Look for and kill processes of a case that left its process group
    #: (Linux only, SPEC.md section 6.3 step 7).
    scan_strays: bool = True
    #: ``process`` (a worker process per shard), ``thread`` (a thread of the runner per shard) or ``None`` for
    #: :data:`DEFAULT_SHARD_MODE`. The results do not depend on it; ``replay.json`` records the mode used.
    shard_mode: str | None = None
    flake_check: bool = True
    strict_budget: bool = False
    verify_blob_hashes: bool = True
    #: Directory the build tree was compiled under, when it differs from
    #: ``build_root`` (default: the resolved ``build_root``).
    compile_root: Path | None = None
    #: Parent directory for scratch files (default: the system temp directory).
    work_dir: Path | None = None
    keep_work: bool = False
    progress: Callable[[str], None] | None = None
    #: What a time limit measures: ``wall`` (SPEC.md section 6.3 step 7, and the ``canonical-2026`` profile as the earlier
    #: pipeline ran it) or ``cpu`` (a deviation: ``RLIMIT_CPU`` equal to the limit and a wall-clock cap of four times the limit,
    #: so that a busy node does not turn a slow case into a timeout).
    timeout_clock: str = "wall"


@dataclass
class ReplayResult:
    out_dir: Path
    summary: dict[str, Any]
    covered: frozenset[str]


class _Peekable:
    def __init__(self, iterator: Iterator[Case]):
        self._it = iterator
        self._buffer: list[Case] = []

    def peek(self) -> Case | None:
        if not self._buffer:
            try:
                self._buffer.append(next(self._it))
            except StopIteration:
                return None
        return self._buffer[0]

    def take_while(self, limit_ms: int) -> Iterator[Case]:
        while True:
            case = self.peek()
            if case is None or case.t_ms > limit_ms:
                return
            self._buffer.pop()
            yield case

    def take_all(self) -> Iterator[Case]:
        while True:
            case = self.peek()
            if case is None:
                return
            self._buffer.pop()
            yield case

    def drain_count(self) -> int:
        count = 0
        while self.peek() is not None:
            self._buffer.pop()
            count += 1
        return count


class _Shard:
    """One worker's private copy of the build tree and its profile data.

    ``root`` is a real copy of the build (never hard links to the original),
    read-only: the binary, the notes files and the sources cannot be written by
    the program, and neither can the directories that hold them. ``gcda`` is the
    tree ``GCOV_PREFIX`` points at, the only writable part (besides the case
    directories). For a capture the runner checks ``gcda``, copies the profile
    files next to the notes files in ``root`` for lcov and removes them again.
    """

    def __init__(
        self,
        index: int,
        directory: Path,
        build_root: Path,
        program: Program,
        strip: int,
        expected: frozenset[PurePosixPath],
    ):
        self.index = index
        self.directory = directory
        self.root = directory / "gcov-root"
        self.gcda = directory / "gcda"
        self.cases = directory / "cases"
        self.info = directory / "coverage.info"
        self.expected = expected
        directory.mkdir(parents=True)
        self.directories = copy_tree(build_root, self.root, read_only=True)
        self.gcda.mkdir()
        self.cases.mkdir()
        self.binary = self.root / program.binary
        self.env = {"GCOV_PREFIX": str(self.gcda), "GCOV_PREFIX_STRIP": str(strip)}
        self.keys: frozenset[str] | None = None
        self.dirty = False

    def reset(self) -> None:
        """Remove everything the programs wrote into the profile tree."""

        with os.scandir(self.gcda) as entries:
            listing = list(entries)
        for entry in listing:
            if entry.is_dir(follow_symlinks=False):
                remove_tree(entry.path)
            else:
                os.unlink(entry.path)
        self.keys = None
        self.dirty = False

    def capture(self, tools: GcovTools, compile_root: Path) -> None:
        """Capture the covered keys of this shard into ``self.keys``."""

        present = collect_profile_data(self.gcda, self.expected)  # raises on anything unexpected
        if not present:
            self.dirty = False
            return
        set_directories_writable(self.directories, True)
        copied: list[Path] = []
        try:
            for relative in present:
                target = self.root / relative
                shutil.copyfile(self.gcda / relative, target)
                copied.append(target)
            self.keys = capture_covered(self.root, self.info, tools, compile_root)
        finally:
            for target in copied:
                try:
                    target.unlink()
                except OSError:
                    pass
            set_directories_writable(self.directories, False)
        self.dirty = False


def _say(options: ReplayOptions, message: str) -> None:
    if options.progress is not None:
        options.progress(message)


def _warn(warnings: list[str], message: str | None) -> None:
    """Keep a warning for ``replay.json`` and print it on standard error at once, whether or not progress is shown."""

    if message is None:
        return
    warnings.append(message)
    print(f"WARNING: {message}", file=sys.stderr, flush=True)


def _timeout_report(timeouts: list[tuple[int, str, float | None, int]], clock: str) -> dict[str, Any]:
    """The ``timeouts`` member of ``replay.json``: how many cases hit their time limit, and how many of those had used less
    than half of it as CPU time (``under_load``: the node was busy or the case waited, not computing).

    ``timeouts`` holds ``(line of cases.jsonl, case id, CPU seconds or None, limit in ms)`` per timed-out case of the first
    replay. The ids are those of the first :data:`MAX_LISTED_TIMEOUT_IDS` such cases in the order of the corpus.
    """

    ordered = sorted(timeouts, key=lambda item: (item[0], item[1]))
    measured = [item for item in ordered if item[2] is not None]
    under_load = [item for item in measured if item[2] < UNDER_LOAD_CPU_SHARE * item[3] / 1000.0]
    return {
        "clock": clock,
        "count": len(ordered),
        "cpu_measured": len(measured),
        "under_load": len(under_load),
        "under_load_case_ids": [item[1] for item in under_load[:MAX_LISTED_TIMEOUT_IDS]],
    }


def replay(
    bundle_dir: Path | str,
    program: Program,
    build_root: Path | str,
    out_dir: Path | str,
    options: ReplayOptions | None = None,
) -> ReplayResult:
    """Replay the bundle on the gcov build and write the outputs to ``out_dir``.

    ``out_dir`` must not exist or must be empty; results are never overwritten.
    """

    options = options or ReplayOptions()
    if program.ignores_exclusion_markers and not options.tools.no_markers:
        # the program's published universe counts branches inside LCOV_EXCL blocks (build.exclusion_markers)
        options = dataclasses.replace(options, tools=dataclasses.replace(options.tools, no_markers=True))
    out_dir = Path(out_dir)
    build_root = Path(build_root)
    if options.profile not in PROFILES:
        raise ReplayError(f"unknown profile {printable(options.profile, 80)!r}; choose one of {', '.join(PROFILES)}")
    if options.shard_mode is not None and options.shard_mode not in SHARD_MODES:
        raise ReplayError(f"unknown shard mode {printable(options.shard_mode, 80)!r}; choose one of {', '.join(SHARD_MODES)}")
    shard_mode = options.shard_mode or DEFAULT_SHARD_MODE
    if shard_mode == "process" and not processes_available():
        raise ReplayError("shard mode process needs a POSIX system and a Python interpreter to start workers with")
    if options.timeout_clock not in TIMEOUT_CLOCKS:
        raise ReplayError(
            f"unknown timeout clock {printable(options.timeout_clock, 80)!r}; choose one of {', '.join(TIMEOUT_CLOCKS)}"
        )
    canonical = options.profile == PROFILE_CANONICAL_2026
    limits = options.limits if options.limits is not None else (Limits.none() if canonical else Limits())
    timeout_override = CANONICAL_TIMEOUT_MS if canonical else None
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise ReplayError(f"refusing to overwrite existing results: {out_dir}")
    if options.interval_s < 1:
        raise ReplayError("interval_s must be at least 1")
    if not build_root.is_dir():
        raise ReplayError(f"build root is not a directory: {build_root}")

    # -- the time zone a case without TZ runs in, and what else the operator should know before hours are spent --------
    warnings: list[str] = []
    local_zone = probe_local_zone()
    if canonical:
        _warn(warnings, canonical_zone_warning(local_zone))
        if options.timeout_clock != "wall":
            _warn(
                warnings,
                f"canonical-2026 with --timeout-clock {options.timeout_clock}: the time limit of a case is CPU time, not "
                "the wall-clock limit of the earlier pipeline; results are not comparable with its numbers on programs "
                "whose cases hit the limit",
            )

    report = validate_bundle(
        bundle_dir,
        program=program,
        strict_budget=options.strict_budget,
        verify_blob_hashes=options.verify_blob_hashes,
    )
    if not report.ok:
        shown = "\n  ".join(str(e) for e in report.errors[:10])
        more = f"\n  ... and {len(report.errors) - 10} more" if len(report.errors) > 10 else ""
        raise ReplayError(f"the bundle is not valid ({len(report.errors)} errors):\n  {shown}{more}")
    bundle = Bundle(bundle_dir, verify_blobs=options.verify_blob_hashes)
    tool = bundle.tool
    budget_s = int(tool["budget"]["wall_s"])
    if budget_s > MAX_SNAPSHOT_INTERVALS * options.interval_s:
        needed = -(-budget_s // MAX_SNAPSHOT_INTERVALS)
        raise ReplayError(
            f"a budget of {budget_s} s at an interval of {options.interval_s} s needs more than "
            f"{MAX_SNAPSHOT_INTERVALS} snapshots; use an interval of at least {needed} s"
        )
    bundle_digests = _bundle_digests(bundle)

    binary_in_build = build_root / program.binary
    if not binary_in_build.is_file() or not os.access(binary_in_build, os.X_OK):
        raise ReplayError(f"the program binary is missing or not executable: {binary_in_build}")
    compile_root = choose_compile_root(build_root, binary_in_build, options.compile_root)
    strip = len(PurePosixPath(compile_root.as_posix()).parts) - 1
    argv0 = PurePosixPath(program.binary).name

    work_parent = Path(os.path.abspath(options.work_dir)) if options.work_dir is not None else None
    scratch = Path(os.path.abspath(tempfile.mkdtemp(prefix="agent-software-testing-", dir=work_parent)))
    partial = out_dir.with_name(f".{out_dir.name}.partial-{os.getpid()}")
    started = time.monotonic()
    shard_pool: ShardPool | None = None
    try:
        # -- stage generated sources recorded at other paths (rare) ----------
        _stage_sources(program, build_root)

        # -- universe -----------------------------------------------------------
        _say(options, "capturing the branch universe")
        universe = initial_capture(build_root, binary_in_build, scratch / "universe", options.tools, compile_root)
        if len(universe.keys) != program.branch_total:
            raise ReplayError(
                f"the build has {len(universe.keys)} branches but {program.id} is defined with "
                f"branch_total {program.branch_total}; wrong build or outdated definition"
            )
        build_record = {
            "program_json_sha256": program.document_sha256,
            "binary_sha256": sha256_file(binary_in_build),
            "gcno_set_sha256": universe.gcno_set_sha256,
        }

        # -- shards -----------------------------------------------------------------
        case_count = int(report.stats.get("cases", 1))
        workers = max(1, min(options.workers or default_workers(), max(1, case_count)))
        _say(options, f"preparing {workers} shard(s)")
        expected = universe.expected_profile_data()

        def make_shard(index: int) -> _Shard:
            return _Shard(index, scratch / "shards" / f"shard-{index:03d}", build_root, program, strip, expected)

        with ThreadPoolExecutor(max_workers=max(1, min(workers, COPY_JOBS))) as pool:
            shards = list(pool.map(make_shard, range(workers)))

        if shard_mode == "process":
            try:
                shard_pool = ShardPool.start(
                    bundle,
                    [shard.directory for shard in shards],
                    _case_settings(shards, argv0, options, limits, timeout_override, canonical),
                    scan_strays=options.scan_strays,
                )
            except ShardWorkerError as exc:
                if options.shard_mode is not None:
                    raise ReplayError(str(exc)) from exc
                _say(options, f"worker processes cannot be started ({exc}); using threads")
                shard_mode = "thread"
            else:
                _say(options, shard_pool.describe())
        runner = _Runner(bundle, shards, argv0, options, limits, timeout_override, canonical=canonical, pool=shard_pool)
        checkpoints = list(range(0, budget_s, options.interval_s)) + [budget_s]

        # -- pass 1: cumulative, with snapshots ------------------------------------
        partial.mkdir(parents=True)
        (partial / "snapshots").mkdir()
        stream = _Peekable(bundle.iter_cases())
        cumulative: frozenset[str] = frozenset()
        snapshots: list[dict[str, int]] = []
        replayed = 0
        for checkpoint in checkpoints:
            if canonical and checkpoint == budget_s:
                due = stream.take_all()  # the last snapshot takes every remaining case
            else:
                due = stream.take_while(checkpoint * 1000 + (999 if canonical else 0))
            batch = runner.run_batch(due)
            replayed += batch
            if batch:
                cumulative = _union_shards(shards, universe, options, compile_root, cumulative, runner)
            snapshots.append({"t_s": checkpoint, "cases": replayed, "covered": len(cumulative)})
            write_json(
                partial / "snapshots" / f"{checkpoint}.json",
                branch_set_document(program.id, program.branch_total, cumulative, t_s=checkpoint, cases=replayed),
            )
            _say(options, f"t={checkpoint}s: {replayed} cases replayed, {len(cumulative)} branches covered")
        skipped = stream.drain_count()
        replay_seconds = time.monotonic() - started
        if replayed and runner.status_counts.get("setup_error", 0) == replayed:
            raise ReplayError(f"every case failed to start; first error: {runner.first_setup_error}")

        # -- pass 2: flake check ----------------------------------------------------
        flake: dict[str, Any] = {"checked": False}
        flake_seconds = 0.0
        if options.flake_check and replayed:
            _say(options, "replaying the final corpus a second time (flake check)")
            flake_started = time.monotonic()
            for shard in shards:
                shard.reset()
            second_runner = _Runner(
                bundle, shards, argv0, options, limits, timeout_override, canonical=canonical, pool=shard_pool
            )
            again_stream = _Peekable(bundle.iter_cases())
            again = again_stream.take_all() if canonical else again_stream.take_while(budget_s * 1000)
            second_runner.run_batch(again)
            second = _union_shards(shards, universe, options, compile_root, frozenset(), second_runner)
            only_first = len(cumulative - second)
            only_second = len(second - cumulative)
            flake = {
                "checked": True,
                "branches_differing": only_first + only_second,
                "only_in_first": only_first,
                "only_in_second": only_second,
            }
            flake_seconds = time.monotonic() - flake_started

        # -- the bundle must be what was validated -------------------------------------
        if _bundle_digests(bundle) != bundle_digests:
            raise IntegrityError("tool.json or cases.jsonl changed while the bundle was being replayed")

        # -- outputs --------------------------------------------------------------------
        final_doc = branch_set_document(program.id, program.branch_total, cumulative)
        write_json(partial / "coverage-branches.json", final_doc)
        (partial / "timeline.csv").write_text(
            "t_s,covered\n" + "".join(f"{s['t_s']},{s['covered']}\n" for s in snapshots), encoding="utf-8"
        )
        versions = options.tools.versions()
        summary: dict[str, Any] = {
            "schema": SCHEMA_REPLAY,
            "program": dict(tool["program"]),
            "tool": dict(tool["tool"]),
            "track": tool["track"],
            "repeat": tool["repeat"],
            "bundle": dict(bundle_digests),
            "build": build_record,
            "profile": options.profile,
            "budget_s": budget_s,
            "interval_s": options.interval_s,
            "branch_total": program.branch_total,
            "branch_covered": len(cumulative),
            "cases": {"total": replayed + skipped, "replayed": replayed, "skipped_beyond_budget": skipped},
            "status_counts": {name: runner.status_counts.get(name, 0) for name in STATUSES},
            "stray_killed": runner.stray_killed,
            "timeouts": _timeout_report(runner.timeouts, options.timeout_clock),
            "snapshots": snapshots,
            "flake": flake,
            "runner": {
                "name": "agent-software-testing",
                "version": __version__,
                "format": FORMAT_VERSION,
                "workers": workers,
                "shard_mode": shard_mode,
                "lcov": versions["lcov"],
                "gcov": versions["gcov"],
            },
            "local_zone": local_zone,
            "warnings": warnings,
            "setup_error_example": runner.first_setup_error,
            "timing": {"replay_s": round(replay_seconds, 3), "flake_s": round(flake_seconds, 3)},
        }
        write_json(partial / "replay.json", summary)
        if out_dir.exists():
            out_dir.rmdir()
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, out_dir)
        return ReplayResult(out_dir, summary, cumulative)
    finally:
        if shard_pool is not None:
            shard_pool.close()  # the workers have their working directories in the scratch tree
        for leftover in (partial, scratch):
            if leftover == scratch and options.keep_work:
                continue
            if leftover.exists():
                try:
                    remove_tree(leftover)
                except OSError:
                    pass


def _bundle_digests(bundle: Bundle) -> dict[str, str]:
    return {
        "tool_json_sha256": sha256_file(bundle.root / "tool.json"),
        "cases_jsonl_sha256": sha256_file(bundle.cases_path),
    }


def _stage_sources(program: Program, build_root: Path) -> None:
    """Copy generated sources to the paths the compiler recorded (``build.stage_sources``)."""

    digest = sha256_file
    for source_rel, recorded_rel in program.stage_sources():
        source = build_root / source_rel
        recorded = build_root / recorded_rel
        if not source.is_file() or source.is_symlink():
            raise ReplayError(f"generated source to stage is missing: {source}")
        if recorded.exists() or recorded.is_symlink():
            if recorded.is_file() and digest(recorded) == digest(source):
                continue
            raise ReplayError(f"staging target exists with other content: {recorded}")
        recorded.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, recorded)


def _case_settings(
    shards: list[_Shard],
    argv0: str,
    options: ReplayOptions,
    limits: Limits,
    timeout_ms: int | None,
    canonical: bool,
) -> list[CaseSettings]:
    """What ``run_case`` gets for the cases of each shard, besides the case and its blobs.

    ``canonical-2026``: ``argv[0]`` is the path of the shard's binary, as in the earlier pipeline
    (``klee-replay`` and the replay scripts passed the path of the binary); the standard profile
    passes the file name only (SPEC.md section 6.3). The same profile lets the locale recorded in a
    case's environment stand instead of the base locale (``LANG=C``) and ``TZ=UTC`` (``recorded_locale``); the standard
    input file of a case is ``fd0`` in the working directory, where ``klee-replay`` put it (``stdin_in_root``). The
    profile does this for every case that has a standard input file; the tool and the ``origin`` of a case are not read.
    """

    return [
        CaseSettings(
            program_name=str(shard.binary) if canonical else argv0,
            binary=str(shard.binary),
            scratch=str(shard.cases),
            extra_env=tuple(shard.env.items()),
            limits=limits,
            timeout_ms=timeout_ms,
            scan_strays=options.scan_strays,
            canonical=canonical,
            timeout_clock=options.timeout_clock,
        )
        for shard in shards
    ]


class _Runner:
    """Runs cases on shards from a queue; accumulates status counts.

    With a ``pool`` each shard's cases are run by the shard's worker process, and the runner's thread for the
    shard only passes cases to it and collects the answers; without one the thread runs the case itself.
    """

    def __init__(
        self,
        bundle: Bundle,
        shards: list[_Shard],
        argv0: str,
        options: ReplayOptions,
        limits: Limits,
        timeout_ms: int | None,
        canonical: bool = False,
        pool: ShardPool | None = None,
    ):
        self.bundle = bundle
        self.shards = shards
        self.argv0 = argv0
        self.options = options
        self.limits = limits
        self.timeout_ms = timeout_ms
        self.canonical = canonical
        self.pool = pool
        self.settings = _case_settings(shards, argv0, options, limits, timeout_ms, canonical)
        self.status_counts: Counter[str] = Counter()
        self.stray_killed = 0
        self.first_setup_error: str | None = None
        #: ``(line of cases.jsonl, case id, CPU seconds or None, limit in ms)`` of every case that hit its time limit.
        self.timeouts: list[tuple[int, str, float | None, int]] = []

    def run_batch(self, cases: Iterable[Case]) -> int:
        """Run ``cases`` over all shards; return how many were run."""

        work: queue.Queue = queue.Queue(maxsize=max(8, len(self.shards) * 32))
        failure: list[BaseException] = []
        totals: list[Counter[str]] = [Counter() for _ in self.shards]

        def worker(shard: _Shard, local: Counter[str]) -> None:
            while True:
                item = work.get()
                if item is _STOP:
                    return
                if failure:
                    continue  # drain after a failure
                try:
                    result = self._run_one(shard, item)
                except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
                    failure.append(exc)
                    continue
                local[result.status] += 1
                local["\0stray_killed"] += result.stray_killed
                if result.status == "timeout":
                    limit_ms = self.timeout_ms if self.timeout_ms is not None else item.timeout_ms
                    self.timeouts.append((item.line, item.id, result.cpu_s, limit_ms))
                shard.dirty = True
                if result.status == "setup_error" and self.first_setup_error is None:
                    self.first_setup_error = result.error

        threads = [
            threading.Thread(target=worker, args=(shard, totals[i]), daemon=True)
            for i, shard in enumerate(self.shards)
        ]
        for thread in threads:
            thread.start()
        count = 0
        try:
            for case in cases:
                if failure:
                    break
                work.put(case)
                count += 1
        except BaseException:
            if self.pool is not None:
                self.pool.abort()  # the threads wait for their workers: let the workers end their cases
            raise
        finally:
            for _ in threads:
                work.put(_STOP)
            for thread in threads:
                thread.join()
        if failure:
            raise failure[0]
        for local in totals:
            self.stray_killed += local.pop("\0stray_killed", 0)
            self.status_counts.update(local)
        if self.pool is not None and self.options.scan_strays and count:
            # the safety net: one scan of all processes per batch, for the markers of this replay
            self.stray_killed += self.pool.sweep()
        return count

    def _run_one(self, shard: _Shard, case: Case) -> RunResult:
        if self.pool is not None:
            try:
                return self.pool.run(shard.index, case)
            except ShardWorkerError as exc:
                raise ReplayError(str(exc)) from exc
        return run_case(case, self.bundle.read_blob, **self.settings[shard.index].run_kwargs(case))


def _union_shards(
    shards: list[_Shard],
    universe: Universe,
    options: ReplayOptions,
    compile_root: Path,
    previous: frozenset[str],
    runner: _Runner,
) -> frozenset[str]:
    """Capture every shard that ran cases since its last capture and union the results.

    At most ``options.capture_jobs`` (default ``min(workers, 8)``) lcov runs are
    active at once.
    """

    dirty = [s for s in shards if s.dirty]
    if dirty:
        jobs = max(1, min(options.capture_jobs or DEFAULT_CAPTURE_JOBS, len(dirty)))
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(shard.capture, options.tools, compile_root) for shard in dirty]
            errors = [future.exception() for future in futures]
        for error in errors:
            if error is not None:
                raise error

    exited = runner.status_counts.get("ok", 0) + runner.status_counts.get("nonzero_exit", 0)
    if exited and all(s.keys is None for s in shards):
        raise ReplayError(
            f"{exited} case(s) exited normally but no shard holds profile data; "
            "check that the binary is a gcov build and that GCOV_PREFIX_STRIP matches its compile path"
        )
    covered = set(previous)
    for shard in shards:
        if shard.keys is not None:
            covered |= shard.keys
    extra = covered - universe.keys
    if extra:
        raise ReplayError(
            f"{len(extra)} covered branch(es) are not in the program's universe, for example "
            f"{sorted(extra)[0]!r}; the branch denominator changed"
        )
    return frozenset(covered)
