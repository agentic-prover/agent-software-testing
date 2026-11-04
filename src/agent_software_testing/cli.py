# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Command line: ``python -m agent_software_testing {validate,replay,signatures,adapt}``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .adapters import afl as afl_adapter
from .adapters import concollmic as concollmic_adapter
from .adapters import ktest as ktest_adapter
from .adapters.common import AdapterError, ToolInfo
from .bundle import Bundle, BundleError
from .common import (
    DEFAULT_TIMEOUT_MS,
    MAX_SNAPSHOT_INTERVALS,
    TOOL_FAMILIES,
    TRACKS,
    AgentSoftwareTestingError,
    printable,
    read_json_file,
)
from .gcov import GcovTools
from .program import Program, find_program, load_program
from .replay import PROFILE_CANONICAL_2026, PROFILE_STANDARD, PROFILES, ReplayOptions, replay
from .sandbox import TIMEOUT_CLOCKS
from .shards import SHARD_MODES
from .signatures import SignatureOptions, collect_signatures
from .validate import validate_bundle, validate_document


def _eprint(*parts: Any) -> None:
    print(*parts, file=sys.stderr)


def _safe(value: object, limit: int = 400) -> str:
    """Text from a bundle or a file name, made safe to print (see ``common.printable``)."""

    return printable(value, limit)


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------


def _add_program_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--program",
        metavar="PATH",
        help="program.json or the program's directory (default: look up the bundle's program id in --programs)",
    )
    parser.add_argument(
        "--programs",
        metavar="DIR",
        help="directory with <id>/program.json definitions (searched first; then the programs/ directory of the "
        "source checkout, then ./programs)",
    )


def _resolve_program(bundle_dir: Path, args: argparse.Namespace) -> Program:
    if args.program:
        return load_program(args.program)
    try:
        program_id = Bundle(bundle_dir).program_id
    except (BundleError, KeyError, TypeError) as exc:
        raise AgentSoftwareTestingError(f"cannot read the program id from the bundle's tool.json: {exc}") from exc
    program = find_program(program_id, args.programs)
    if program is None:
        raise AgentSoftwareTestingError(
            f"no definition for program {_safe(program_id, 80)!r}; pass --program or --programs"
        )
    return program


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.path)
    program = load_program(args.program) if args.program else None
    if path.is_dir() and ((path / "tool.json").exists() or (path / "cases.jsonl").exists()):
        report = validate_bundle(
            path,
            program=program,
            programs_dir=args.programs,
            strict_budget=args.strict_budget,
            verify_blob_hashes=not args.no_verify_hashes,
        )
    else:
        report = validate_document(path)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "errors": [e.to_json() for e in report.errors],
                    "warnings": [w.to_json() for w in report.warnings],
                    "truncated": report.truncated,
                    "stats": report.stats,
                },
                indent=2,
            )
        )
    else:
        for error in report.errors:
            print(f"error: {error}")
        for warning in report.warnings:
            print(f"warning: {warning}")
        if report.truncated:
            print("error: further errors not shown")
        cases = report.stats.get("cases")
        if report.ok:
            print(f"{_safe(path)}: valid" + (f" ({cases} cases)" if cases is not None else ""))
        else:
            print(f"{_safe(path)}: {len(report.errors)} error(s)")
    return 0 if report.ok else 1


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _tools_from(args: argparse.Namespace) -> GcovTools:
    timeout = None if args.lcov_timeout == 0 else float(args.lcov_timeout)
    return GcovTools(lcov=args.lcov, gcov=args.gcov, strict_warnings=not args.allow_lcov_warnings, timeout_s=timeout)


def _cmd_replay(args: argparse.Namespace) -> int:
    bundle_dir = Path(args.bundle)
    program = _resolve_program(bundle_dir, args)
    options = ReplayOptions(
        interval_s=args.interval,
        workers=args.workers,
        tools=_tools_from(args),
        profile=args.profile,
        capture_jobs=args.capture_jobs,
        scan_strays=not args.no_stray_scan,
        shard_mode=args.shard_mode,
        flake_check=not args.no_flake_check,
        strict_budget=args.strict_budget,
        verify_blob_hashes=not args.no_verify_hashes,
        compile_root=Path(args.compile_root) if args.compile_root else None,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        keep_work=args.keep_work,
        progress=None if args.quiet else (lambda message: _eprint(f"[replay] {message}")),
        timeout_clock=args.timeout_clock,
    )
    result = replay(bundle_dir, program, args.build, args.out, options)
    summary = result.summary
    flake = summary["flake"]
    flake_text = (
        f"{flake['branches_differing']} branch(es) differ on a second replay"
        if flake.get("checked")
        else "flake check not run"
    )
    strays = summary.get("stray_killed", 0)
    stray_text = f" {strays} stray process(es) killed;" if strays else ""
    under_load = summary.get("timeouts", {}).get("under_load", 0)
    if under_load:
        stray_text += (
            f" {under_load} case(s) hit their time limit with less than half of it as CPU time (a busy node?);"
        )
    print(
        f"{_safe(summary['program']['id'])}: {summary['branch_covered']}/{summary['branch_total']} branches covered; "
        f"{summary['cases']['replayed']} cases replayed, {summary['cases']['skipped_beyond_budget']} beyond the budget;"
        f"{stray_text} {flake_text}. Results in {_safe(result.out_dir)}"
    )
    return 0


# ---------------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------------


def _cmd_signatures(args: argparse.Namespace) -> int:
    bundle_dir = Path(args.bundle)
    program = _resolve_program(bundle_dir, args)
    options = SignatureOptions(
        workers=args.workers,
        binary=args.binary,
        source_root=args.source_root,
        strict_budget=args.strict_budget,
        verify_blob_hashes=not args.no_verify_hashes,
        work_dir=Path(args.work_dir) if args.work_dir else None,
    )
    result = collect_signatures(bundle_dir, program, args.build, args.out, options)
    print(
        f"{_safe(result.counts['program'])}: {result.counts['signatures']} distinct signature(s) from "
        f"{result.counts['cases']['with_reports']} case(s) with reports. Results in {_safe(result.out_dir)}"
    )
    return 0


# ---------------------------------------------------------------------------
# adapt
# ---------------------------------------------------------------------------


def _add_adapt_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", required=True, metavar="DIR", help="bundle directory to create (must not exist or be empty)")
    parser.add_argument("--program-id", required=True, metavar="ID", help="program the cases are for")
    _add_program_args(parser)
    parser.add_argument("--source-sha256", metavar="HEX", help="sha256 of the pinned source archive (default: from the program definition)")
    parser.add_argument("--tool-name", required=True)
    parser.add_argument("--tool-version")
    parser.add_argument("--family", choices=TOOL_FAMILIES, default="other")
    parser.add_argument("--model")
    parser.add_argument("--config-sha256", metavar="HEX")
    parser.add_argument("--tool-url")
    parser.add_argument("--track", choices=TRACKS, default="open")
    parser.add_argument("--repeat", type=_positive, default=1)
    parser.add_argument(
        "--wall-s",
        type=_positive,
        required=True,
        help="the wall-clock budget the campaign was allowed, in seconds (required: the adapter cannot know it; "
        "the standard budget is 21600)",
    )
    parser.add_argument(
        "--cores", type=_positive, help="CPU cores the tool used (written only when given; absent means not stated)"
    )
    parser.add_argument("--budget-notes", default="")
    parser.add_argument("--output-tokens", type=int)
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--cpu-s", type=float)
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--timeout-ms",
        type=_positive,
        default=DEFAULT_TIMEOUT_MS,
        help=f"per-case timeout (default {DEFAULT_TIMEOUT_MS}, the maximum)",
    )
    parser.add_argument("--no-validate", action="store_true", help="do not validate the bundle after writing it")


def _tool_info(args: argparse.Namespace) -> tuple[ToolInfo, Program | None]:
    program = None
    if args.program:
        program = load_program(args.program)
    else:
        program = find_program(args.program_id, args.programs)
    digest = args.source_sha256 or (program.source_sha256 if program else None)
    if not digest:
        raise AdapterError("pass --source-sha256 (no program definition found to take it from)")
    info = ToolInfo(
        name=args.tool_name,
        program_id=args.program_id,
        source_sha256=digest,
        version=args.tool_version,
        family=args.family,
        model=args.model,
        config_sha256=args.config_sha256,
        url=args.tool_url,
        track=args.track,
        repeat=args.repeat,
        wall_s=args.wall_s,
        cores=args.cores,
        budget_notes=args.budget_notes,
        output_tokens=args.output_tokens,
        input_tokens=args.input_tokens,
        cpu_s=args.cpu_s,
        notes=args.notes,
    )
    return info, program


def _finish_adapt(args: argparse.Namespace, report: Any, program: Program | None) -> int:
    print(json.dumps(report.to_json(), indent=2))
    if args.no_validate:
        return 0
    validation = validate_bundle(args.out, program=program, programs_dir=args.programs, strict_budget=False)
    for error in validation.errors[:20]:
        _eprint(f"error: {error}")
    for warning in validation.warnings[:20]:
        _eprint(f"warning: {warning}")
    if validation.ok:
        _eprint(f"{args.out}: valid bundle with {validation.stats.get('cases', 0)} case(s)")
        return 0
    _eprint(f"{args.out}: {len(validation.errors)} validation error(s)")
    return 1


def _cmd_adapt_ktest(args: argparse.Namespace) -> int:
    info, program = _tool_info(args)
    times = None
    if args.times:
        raw = read_json_file(args.times)
        if not isinstance(raw, dict) or not all(isinstance(v, int) and not isinstance(v, bool) for v in raw.values()):
            raise AdapterError("--times must be a JSON object of file path (relative to KTEST_DIR) to t_ms")
        times = {str(k): int(v) for k, v in raw.items()}
    report = ktest_adapter.convert_directory(
        args.input,
        args.out,
        info,
        start_time=args.start_time,
        times_ms=times,
        timeout_ms=args.timeout_ms,
        klee_replay=not args.as_written,
    )
    return _finish_adapt(args, report, program)


def _cmd_adapt_afl(args: argparse.Namespace) -> int:
    info, program = _tool_info(args)
    template: list[str] | None = None
    if args.argv_json is not None:
        template = json.loads(args.argv_json)
        if not isinstance(template, list) or not all(isinstance(t, str) for t in template):
            raise AdapterError("--argv-json must be a JSON list of strings")
    elif args.arg is not None:
        template = list(args.arg)
    elif args.track == "open":
        template = []  # no arguments, input on stdin
    report = afl_adapter.convert_directories(
        args.out,
        info,
        queue_dirs=args.queue or [],
        crash_dirs=args.crashes or [],
        hang_dirs=args.hangs or [],
        argv_template=template,
        fixed_track=program.fixed_track if program else None,
        start_time=args.start_time,
        timeout_ms=args.timeout_ms,
        dedupe=not args.no_dedupe,
    )
    return _finish_adapt(args, report, program)


def _cmd_adapt_concollmic(args: argparse.Namespace) -> int:
    info, program = _tool_info(args)
    report = concollmic_adapter.convert_from_args(args, info, program)
    return _finish_adapt(args, report, program)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_software_testing",
        description="Replay test cases on one pinned gcov build per program and measure branch coverage.",
    )
    parser.add_argument("--version", action="version", version=f"agent-software-testing {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("validate", help="validate a bundle (directory) or a tool, program, branch-set or replay document (file)")
    p.add_argument("path")
    _add_program_args(p)
    p.add_argument("--strict-budget", action="store_true", help="make a case with t_ms beyond the budget an error (by default it is a warning: the replay skips such cases and counts them)")
    p.add_argument("--no-verify-hashes", action="store_true", help="check that blobs exist and fit the limits, but do not hash them")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.set_defaults(func=_cmd_validate)

    p = commands.add_parser("replay", help="replay a bundle on the gcov build and write coverage outputs")
    p.add_argument("bundle")
    p.add_argument("--build", required=True, metavar="DIR", help="the program's gcov build tree (at its compile path)")
    p.add_argument("--out", required=True, metavar="DIR", help="results directory (must not exist or be empty)")
    _add_program_args(p)
    p.add_argument(
        "--interval",
        type=_positive,
        default=900,
        metavar="SECONDS",
        help=f"snapshot interval (default 900; the budget may be at most {MAX_SNAPSHOT_INTERVALS} intervals long)",
    )
    p.add_argument("--workers", type=_positive, metavar="N", help="worker shards (default: CPU count)")
    p.add_argument(
        "--shard-mode",
        choices=SHARD_MODES,
        help="how the shards run: 'process' gives each shard a worker process of its own for the whole replay "
        "(the default where processes can be started), 'thread' runs all shards as threads of this process. "
        "The results are the same; the mode used is recorded in replay.json",
    )
    p.add_argument(
        "--profile",
        choices=PROFILES,
        default=PROFILE_STANDARD,
        help=f"'{PROFILE_STANDARD}' is the protocol of the specification (default). '{PROFILE_CANONICAL_2026}' "
        "reproduces the earlier measurement pipeline for equivalence tests: whole-second bucketing, the last "
        "snapshot takes every remaining case (also those after the budget), a 30 s limit for every case and no "
        "resource limits (SPEC section 11.17)",
    )
    p.add_argument(
        "--timeout-clock",
        choices=TIMEOUT_CLOCKS,
        default="wall",
        help="what the time limit of a case measures. 'wall' (default) is the wall-clock limit of the specification and "
        "of the earlier pipeline. 'cpu' is a deviation from both: the program gets RLIMIT_CPU equal to the limit and a "
        "case that waits is stopped after four times the limit in wall-clock time, so that a busy node does not turn a "
        "slow case into a timeout (replay.json records the clock; SPEC section 6.3 step 7)",
    )
    p.add_argument(
        "--capture-jobs",
        type=_positive,
        metavar="N",
        help="most lcov runs at once (default: the smaller of the shard count and 8)",
    )
    p.add_argument(
        "--lcov-timeout",
        type=float,
        default=3600.0,
        metavar="SECONDS",
        help="longest time one lcov run may take; 0 for no limit (default 3600)",
    )
    p.add_argument(
        "--no-stray-scan",
        action="store_true",
        help="do not look for processes a case left behind by the marker in their environment (Linux only)",
    )
    p.add_argument("--compile-root", metavar="DIR", help="directory the build was compiled under, if not --build")
    p.add_argument("--lcov", default="lcov", help="lcov executable (default: lcov)")
    p.add_argument("--gcov", metavar="TOOL", help="gcov tool passed to lcov as --gcov-tool")
    p.add_argument("--allow-lcov-warnings", action="store_true", help="do not fail the initial capture on lcov warnings")
    p.add_argument("--no-flake-check", action="store_true", help="skip the second replay of the final corpus")
    p.add_argument("--strict-budget", action="store_true", help="reject bundles with cases beyond the budget instead of skipping them")
    p.add_argument("--no-verify-hashes", action="store_true")
    p.add_argument("--work-dir", metavar="DIR", help="parent directory for scratch files (default: system temp)")
    p.add_argument("--keep-work", action="store_true", help="keep the scratch directory")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=_cmd_replay)

    p = commands.add_parser("signatures", help="run a bundle on the ASan+UBSan build and count bug signatures")
    p.add_argument("bundle")
    p.add_argument("--build", required=True, metavar="DIR", help="the program's sanitiser build tree")
    p.add_argument("--out", required=True, metavar="DIR")
    _add_program_args(p)
    p.add_argument("--binary", metavar="REL", help="binary path inside the sanitiser build (default: the program's binary with obj-gcov/ replaced by obj-native/)")
    p.add_argument("--source-root", metavar="DIR", help="program source directory used to find 'own' frames (default: --build)")
    p.add_argument("--workers", type=_positive, metavar="N")
    p.add_argument("--strict-budget", action="store_true")
    p.add_argument("--no-verify-hashes", action="store_true")
    p.add_argument("--work-dir", metavar="DIR")
    p.set_defaults(func=_cmd_signatures)

    adapt = commands.add_parser("adapt", help="convert a tool's native output into a bundle")
    kinds = adapt.add_subparsers(dest="kind", required=True)

    p = kinds.add_parser("ktest", help="KLEE .ktest files (and .ktest.env sidecars)")
    p.add_argument("input", metavar="KTEST_DIR")
    _add_adapt_common(p)
    p.add_argument(
        "--start-time",
        type=float,
        metavar="EPOCH_S",
        help="campaign start as epoch seconds; t_ms is the file's modification time minus this "
        "(default: the oldest file's modification time, so the first test is at 0, which is NOT the campaign start)",
    )
    p.add_argument(
        "--times",
        metavar="FILE.json",
        help="JSON object of ktest file path, relative to KTEST_DIR (for example 'klee-out-0/test000001.ktest'), "
        "to t_ms; overrides modification times. Files the object does not list use their modification time",
    )
    p.add_argument(
        "--as-written",
        action="store_true",
        help="map each test as it is written instead of the way klee-replay replays it (by default tests that "
        "klee-replay cannot replay are skipped and its quirks are reproduced; see docs/adapters.md)",
    )
    p.set_defaults(func=_cmd_adapt_ktest)

    p = kinds.add_parser("afl", help="AFL++ queue, crashes and hangs directories")
    _add_adapt_common(p)
    p.add_argument("--queue", action="append", metavar="DIR")
    p.add_argument("--crashes", action="append", metavar="DIR")
    p.add_argument("--hangs", action="append", metavar="DIR")
    p.add_argument("--arg", action="append", metavar="TOKEN", help="one argument of the AFL++ command line; '@@' marks the input file (use --arg=TOKEN for tokens starting with '-')")
    p.add_argument("--argv-json", metavar="JSON", help="the argument list as a JSON array of strings (alternative to --arg)")
    p.add_argument("--start-time", type=float, metavar="EPOCH_S", help="fuzzer start, for files without a time: field")
    p.add_argument("--no-dedupe", action="store_true", help="keep identical inputs found at different times")
    p.set_defaults(func=_cmd_adapt_afl)

    p = kinds.add_parser(
        "concollmic",
        help="ConcoLLMic queue directory (YAML files whose exec_code runs the program), recorded without running it",
    )
    concollmic_adapter.add_arguments(p)
    _add_adapt_common(p)
    p.set_defaults(func=_cmd_adapt_concollmic)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except NotImplementedError as exc:
        _eprint(f"error: {_safe(exc, 2000)}")
        return 2
    except (AgentSoftwareTestingError, OSError, ValueError, RecursionError) as exc:
        # ValueError covers JSONDecodeError and UnicodeError; RecursionError is deeply nested JSON
        _eprint(f"error: {printable(exc, 4000, keep_newlines=True)}")
        return 1
    except KeyboardInterrupt:
        _eprint("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
