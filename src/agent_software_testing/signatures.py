# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Sanitiser pass: bug signatures from the ASan+UBSan build (contract section 4, step 9).

Every case runs on the sanitiser build with the last 64 KiB of standard error
kept. Reports are parsed into signatures

    (sanitiser, kind, first stack frame inside the program's own source as file:function)

``signatures-private.json`` holds the signatures with their frames and the
cases that triggered them; ``signatures-counts.json`` holds counts of distinct
signatures by sanitiser and kind only (the public output).

A report is text on standard error, which the program under test can write
itself. The kind of a signature is therefore never taken from the report: it is
one of the names in :mod:`agent_software_testing.kinds`, or ``other`` (the text that was
not recognised is kept in the private file as ``detail``, cut short).
"""

from __future__ import annotations

import multiprocessing
import os
import posixpath
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .bundle import Bundle
from .case import Case
from .common import (
    SCHEMA_SIGNATURE_COUNTS,
    SCHEMA_SIGNATURES_PRIVATE,
    AgentSoftwareTestingError,
    default_workers,
    write_json,
)
from .kinds import ASAN_KINDS, MAX_KIND_CHARS, OTHER_KIND
from .program import Program
from .replay import STATUSES
from .sandbox import Limits, remove_tree, run_case
from .validate import validate_bundle

#: Same runtime policy as the evaluation's sanitiser replays.
DEFAULT_SANITIZER_ENV = {
    "ASAN_OPTIONS": "abort_on_error=1:detect_leaks=0:symbolize=1",
    "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1:symbolize=1",
}
COVERAGE_BINARY_PREFIX = "obj-gcov/"
SANITIZER_BINARY_PREFIX = "obj-native/"


def default_sanitizer_env() -> dict[str, str]:
    """The sanitiser options, plus the symbolizer path when the image sets one.

    The runner starts programs with a clean environment, so a symbolizer
    configured for the image (``ASAN_SYMBOLIZER_PATH``) has to be passed on
    explicitly; UBSan gets the same path unless it has its own.
    """

    env = dict(DEFAULT_SANITIZER_ENV)
    symbolizer = os.environ.get("ASAN_SYMBOLIZER_PATH")
    if symbolizer:
        env["ASAN_SYMBOLIZER_PATH"] = symbolizer
    ubsan_symbolizer = os.environ.get("UBSAN_SYMBOLIZER_PATH") or symbolizer
    if ubsan_symbolizer:
        env["UBSAN_SYMBOLIZER_PATH"] = ubsan_symbolizer
    return env


def sanitizer_binary(program_binary: str) -> str:
    """The sanitiser build's binary: ``obj-gcov/`` becomes ``obj-native/`` (SPEC.md 4.3)."""

    if program_binary.startswith(COVERAGE_BINARY_PREFIX):
        return SANITIZER_BINARY_PREFIX + program_binary[len(COVERAGE_BINARY_PREFIX) :]
    return program_binary
MAX_EXAMPLE_CASES = 20
#: Report text beyond this many characters is ignored when the kind is derived.
MAX_REPORT_TEXT_CHARS = 2000
#: Longest ``detail`` kept in the private file.
MAX_DETAIL_CHARS = 120

_ASAN_ERROR = re.compile(r"ERROR: AddressSanitizer: (?P<text>.+)")
_UBSAN_ERROR = re.compile(r"^(?P<file>.+?):(?P<line>[0-9]+):(?P<col>[0-9]+): runtime error: (?P<text>.+)$")
#: A symbolised frame. The line (and column) are optional: when the debug information has a file for the address
#: but no line, the symbolizer prints the bare path (``in start_up /src/app/main.c``).
_FRAME = re.compile(
    r"^\s*#(?P<n>[0-9]+)\s+0x[0-9a-fA-F]+\s+in\s+(?P<func>.+?)\s+"
    r"(?P<file>[^\s:()]+)(?::(?P<line>[0-9]+)(?::(?P<col>[0-9]+))?)?(?:\s+\([^)]*\))?\s*$"
)
_FRAME_START = re.compile(r"^\s*#[0-9]+\s+0x")

_ASAN_KINDS: list[tuple[str, str]] = [
    (r"requested allocation size", "allocation-size-too-big"),
    (r"allocator is out of memory", "out-of-memory"),
    (r"attempting double-free", "double-free"),
    (r"attempting free on address which was not malloc", "bad-free"),
    (r"attempting to call malloc_usable_size", "bad-malloc-usable-size"),
    (r"calloc parameters overflow", "calloc-overflow"),
    (r"reallocarray parameters overflow", "reallocarray-overflow"),
    (r"pvalloc parameters overflow", "pvalloc-overflow"),
    (r"invalid alignment requested in aligned_alloc", "invalid-aligned-alloc-alignment"),
    (r"invalid alignment requested in posix_memalign", "invalid-posix-memalign-alignment"),
    (r"invalid allocation alignment", "invalid-allocation-alignment"),
    (r"failed to allocate", "failed-to-allocate"),
    (r"(\w+)-param-overlap", "param-overlap"),
]

_UBSAN_KINDS: list[tuple[str, str]] = [
    (r"signed integer overflow", "signed-integer-overflow"),
    # INT_MIN / -1 and INT_MIN % -1: clang counts them under -fsanitize=signed-integer-overflow
    (r"(division|remainder) of .* by -1 cannot be represented", "signed-integer-overflow"),
    (r"unsigned integer overflow", "unsigned-integer-overflow"),
    (r"negation of .* cannot be represented", "negation-overflow"),
    (r"shift exponent .* (is negative|is too large)", "shift-exponent"),
    (r"left shift of negative value", "left-shift-negative"),
    (r"left shift of .* cannot be represented", "left-shift-overflow"),
    (r"division by zero", "division-by-zero"),
    (r"index .* out of bounds", "index-out-of-bounds"),
    (r"(load of|store to|member access within|member call on) misaligned address", "misaligned-access"),
    (r"(load of|store to|member access within|member call on|reference binding to) null pointer", "null-pointer-use"),
    (r"applying (non-zero|zero) offset", "pointer-overflow"),
    (r"pointer index expression with base .* overflowed", "pointer-overflow"),
    (r"(addition of unsigned offset to|subtraction of unsigned offset from) .* overflowed", "pointer-overflow"),
    (r"null pointer passed as argument", "nonnull-argument"),
    (r"returning null pointer from function", "nonnull-return"),
    (r"null pointer returned from function declared to never return null", "nonnull-return"),
    (r"load of value .*, which is not a valid value for type", "invalid-value-load"),
    (r"variable length array bound", "vla-bound"),
    (r"call to function .* through pointer to incorrect function type", "function-type-mismatch"),
    (r"implicit conversion from type", "implicit-conversion"),
    (r"execution reached the end of a value-returning function", "missing-return"),
    (r".* is outside the range of representable values of type", "float-cast-overflow"),
    (r".*address .* with insufficient space for an object of type", "insufficient-object-space"),
    (r"(member call on|member access within) address .* which does not point to an object of type", "bad-vptr"),
    (r"downcast of address", "bad-downcast"),
    (r"execution reached an unreachable program point", "unreachable"),
    (r"passing zero to .*, which is not a valid argument", "invalid-builtin"),
]


@dataclass(frozen=True)
class Signature:
    sanitizer: str  # asan | ubsan
    kind: str  # a name from agent_software_testing.kinds
    frame: str  # "file:function" below the source root, "?" when none
    #: What the report said when the kind is ``other`` (cut short; private).
    detail: str = field(default="", compare=False)

    def key(self) -> tuple[str, str, str]:
        return (self.sanitizer, self.kind, self.frame)


@dataclass
class SignatureRecord:
    signature: Signature
    #: ``(t_ms, line number, case id)`` of every case that triggered it.
    members: list[tuple[int, int, str]] = field(default_factory=list)

    @property
    def cases(self) -> int:
        return len(self.members)

    @property
    def first_case(self) -> str:
        return min(self.members)[2]

    @property
    def first_t_ms(self) -> int:
        return min(self.members)[0]

    @property
    def example_cases(self) -> list[str]:
        """Up to 20 case ids, the earliest first (independent of worker timing)."""

        return [member[2] for member in sorted(self.members)[:MAX_EXAMPLE_CASES]]


def _slug(text: str) -> str:
    text = re.sub(r"0x[0-9a-fA-F]+", "", text)
    text = re.sub(r"[0-9]+", "N", text)
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "unknown"


def _asan_head(text: str) -> str:
    return re.split(r" on | at | trying | \(|: ", text, maxsplit=1)[0]


def asan_kind(text: str) -> str:
    """Normalise the text after ``ERROR: AddressSanitizer:`` to a kind.

    The result is always a name from :data:`agent_software_testing.kinds.ASAN_KINDS`;
    text that matches none of them is ``other``.
    """

    text = text.strip()[:MAX_REPORT_TEXT_CHARS]
    for pattern, kind in _ASAN_KINDS:
        if re.match(pattern, text):
            return kind
    kind = _slug(_asan_head(text))
    return kind if kind in ASAN_KINDS and len(kind) <= MAX_KIND_CHARS else OTHER_KIND


def _ubsan_head(message: str) -> str:
    return message.split(":", 1)[0]


def ubsan_kind(message: str) -> str:
    """Normalise a UBSan runtime-error message to a kind (numbers and types removed).

    The result is always a name from :data:`agent_software_testing.kinds.UBSAN_KINDS`;
    a message that matches no known category is ``other``.
    """

    message = message.strip()[:MAX_REPORT_TEXT_CHARS]
    for pattern, kind in _UBSAN_KINDS:
        if re.match(pattern, message):
            return kind
    return OTHER_KIND


def _detail(kind: str, head: str) -> str:
    """What to keep privately about a report that was counted as ``other``."""

    return _slug(head)[:MAX_DETAIL_CHARS] if kind == OTHER_KIND else ""


def _under_root(path: str, source_root: str | None, base_dir: str | None = None) -> str | None:
    """Path relative to the source root, or ``None`` if ``path`` lies outside it.

    A relative path is taken relative to ``base_dir`` (the directory the
    compiler ran in; default: the source root), so ``../lib/a.c`` of an
    out-of-tree build resolves into the source root when that is where it lies.
    """

    cleaned = posixpath.normpath(path)
    if source_root is None:
        if posixpath.isabs(cleaned) or cleaned.startswith("..") or cleaned == ".":
            return None
        return cleaned
    root = posixpath.normpath(source_root)
    if not posixpath.isabs(cleaned):
        cleaned = posixpath.normpath(posixpath.join(base_dir or root, cleaned))
    prefix = root.rstrip("/") + "/"
    if cleaned == root or not cleaned.startswith(prefix):
        return None
    return cleaned[len(prefix) :]


def _first_frame(
    lines: list[str], start: int, source_root: str | None, base_dir: str | None
) -> tuple[str | None, int]:
    """Find the first frame under the root in the first stack after ``start``.

    Returns ``(file:function or None, index after the stack)``.
    """

    index = start
    while index < len(lines) and not _FRAME_START.match(lines[index]):
        if _ASAN_ERROR.search(lines[index]) or _UBSAN_ERROR.match(lines[index]):
            return None, index  # next report begins before any frame
        index += 1
    found: str | None = None
    while index < len(lines) and _FRAME_START.match(lines[index]):
        match = _FRAME.match(lines[index])
        if match and found is None:
            path = match.group("file")
            # Without a line number only an absolute path is taken as a source location: the symbolizer also prints
            # the name of an object file for frames inside the sanitiser runtime (``asan_interceptors.cpp.o``), and a
            # relative name like that would otherwise resolve to a file below the source root.
            if match.group("line") is None and not posixpath.isabs(path):
                relative = None
            else:
                relative = _under_root(path, source_root, base_dir)
            if relative is not None:
                func = re.split(r"\s*\(", match.group("func"), maxsplit=1)[0].strip() or "?"
                found = f"{relative}:{func}"
        index += 1
    return found, index


def parse_reports(
    stderr: bytes | str, source_root: str | None, base_dir: str | None = None
) -> list[Signature]:
    """Signatures of every AddressSanitizer and UBSan report in ``stderr``, in order.

    ``source_root`` is the program's source directory in the sanitiser build;
    only frames below it count. A relative path is resolved against
    ``base_dir`` (default: the source root), so it counts unless it climbs out
    of the source root with ``..``.
    """

    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
    lines = text.splitlines()
    signatures: list[Signature] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        asan = _ASAN_ERROR.search(line)
        if asan:
            frame, index = _first_frame(lines, index + 1, source_root, base_dir)
            kind = asan_kind(asan.group("text"))
            signatures.append(
                Signature("asan", kind, frame or "?", _detail(kind, _asan_head(asan.group("text").strip()[:MAX_REPORT_TEXT_CHARS])))
            )
            continue
        ub = _UBSAN_ERROR.match(line)
        if ub:
            frame, after = _first_frame(lines, index + 1, source_root, base_dir)
            if frame is None:
                relative = _under_root(ub.group("file"), source_root, base_dir)
                frame = f"{relative}:?" if relative is not None else "?"
            kind = ubsan_kind(ub.group("text"))
            signatures.append(
                Signature("ubsan", kind, frame, _detail(kind, _ubsan_head(ub.group("text").strip()[:MAX_REPORT_TEXT_CHARS])))
            )
            index = max(after, index + 1)
            continue
        index += 1
    return signatures


_WORKER: dict[str, Any] = {}


def _worker_init(state: dict[str, Any]) -> None:
    """Runs once in each worker process of the sanitiser pass: opens the bundle for this process."""

    _WORKER.clear()
    _WORKER.update(state)
    _WORKER["bundle"] = Bundle(state["bundle_dir"], verify_blobs=state["verify"])


def _worker_case(case: Case) -> tuple[int, int, str, str, list[Signature]]:
    w = _WORKER
    result = run_case(
        case,
        w["bundle"].read_blob,
        binary=w["binary"],
        program_name=w["argv0"],
        scratch=w["cases_dir"],
        extra_env=w["env"],
        limits=w["limits"],
        keep_stderr=True,
    )
    found = parse_reports(result.stderr_tail or b"", w["source_root"], w["base_dir"])
    return case.t_ms, case.line, case.id, result.status, list({x.key(): x for x in found}.values())


@dataclass
class SignatureOptions:
    workers: int | None = None
    limits: Limits = field(default_factory=lambda: Limits(file_size_bytes=64 * 1024 * 1024))
    sanitizer_env: dict[str, str] = field(default_factory=default_sanitizer_env)
    #: Binary path inside the sanitiser build (default: the program's ``binary``
    #: with the leading ``obj-gcov/`` replaced by ``obj-native/``).
    binary: str | None = None
    #: Program source directory used to decide which frames are "in the program".
    source_root: str | None = None
    strict_budget: bool = False
    verify_blob_hashes: bool = True
    work_dir: Path | None = None
    keep_work: bool = False
    progress: Any = None


@dataclass
class SignatureResult:
    out_dir: Path
    records: list[SignatureRecord]
    counts: dict[str, Any]


def collect_signatures(
    bundle_dir: Path | str,
    program: Program,
    build_root: Path | str,
    out_dir: Path | str,
    options: SignatureOptions | None = None,
) -> SignatureResult:
    options = options or SignatureOptions()
    out_dir = Path(out_dir)
    build_root = Path(build_root)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise AgentSoftwareTestingError(f"refusing to overwrite existing results: {out_dir}")
    report = validate_bundle(
        bundle_dir, program=program, strict_budget=options.strict_budget, verify_blob_hashes=options.verify_blob_hashes
    )
    if not report.ok:
        shown = "\n  ".join(str(e) for e in report.errors[:10])
        raise AgentSoftwareTestingError(f"the bundle is not valid ({len(report.errors)} errors):\n  {shown}")
    bundle = Bundle(bundle_dir, verify_blobs=options.verify_blob_hashes)
    budget_ms = int(bundle.tool["budget"]["wall_s"]) * 1000
    binary_rel = options.binary or sanitizer_binary(program.binary)
    binary = build_root / binary_rel
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise AgentSoftwareTestingError(f"the sanitiser binary is missing or not executable: {binary}")
    base_dir = str(build_root.resolve())
    source_root = options.source_root or base_dir
    workers = max(1, min(options.workers or default_workers(), max(1, int(report.stats.get("cases", 1)))))
    argv0 = PurePosixPath(program.binary).name

    work_parent = Path(os.path.abspath(options.work_dir)) if options.work_dir is not None else None
    scratch = Path(os.path.abspath(tempfile.mkdtemp(prefix="agent-software-testing-san-", dir=work_parent)))
    cases_dir = scratch / "cases"
    cases_dir.mkdir()
    records: dict[tuple[str, str, str], SignatureRecord] = {}
    status_counts: Counter[str] = Counter()
    cases_with_reports = 0
    cases_replayed = 0
    skipped = 0

    # Each case runs in a worker process of its own pool: the parsing of a sanitiser report is Python work, and
    # with threads the interpreter lock let only one or two of them run at a time. Records keep every member and
    # sort them on output, so the order in which workers finish does not change a result. One case at a time: a case
    # can run up to the 30 s timeout, and a batch of them on one worker would leave the others idle at the end.
    state = {
        "bundle_dir": str(bundle_dir),
        "verify": options.verify_blob_hashes,
        "binary": str(binary),
        "argv0": argv0,
        "cases_dir": str(cases_dir),
        "env": options.sanitizer_env,
        "limits": options.limits,
        "source_root": source_root,
        "base_dir": base_dir,
    }

    def in_budget():
        nonlocal skipped, cases_replayed
        for case in bundle.iter_cases():
            if case.t_ms > budget_ms:
                skipped += 1
                continue
            cases_replayed += 1
            yield case

    try:
        with multiprocessing.get_context("fork").Pool(workers, initializer=_worker_init, initargs=(state,)) as pool:
            for t_ms, line, case_id, status, found in pool.imap_unordered(_worker_case, in_budget(), chunksize=1):
                status_counts[status] += 1
                if found:
                    cases_with_reports += 1
                for signature in found:
                    record = records.get(signature.key())
                    if record is None:
                        record = records[signature.key()] = SignatureRecord(signature)
                    record.members.append((t_ms, line, case_id))

        if cases_replayed and status_counts.get("setup_error", 0) == cases_replayed:
            raise AgentSoftwareTestingError("every case failed to start, so the sanitiser pass found nothing to count")

        ordered = sorted(records.values(), key=lambda r: (r.signature.sanitizer, r.signature.kind, r.signature.frame))
        by_kind: dict[str, dict[str, int]] = {}
        for record in ordered:
            sanitizer = by_kind.setdefault(record.signature.sanitizer, {})
            sanitizer[record.signature.kind] = sanitizer.get(record.signature.kind, 0) + 1
        tool = bundle.tool
        counts = {
            "schema": SCHEMA_SIGNATURE_COUNTS,
            "program": tool["program"]["id"],
            "tool": dict(tool["tool"]),
            "repeat": tool["repeat"],
            "signatures": len(ordered),
            "by_sanitizer_and_kind": {s: dict(sorted(k.items())) for s, k in sorted(by_kind.items())},
            "cases": {
                "replayed": cases_replayed,
                "with_reports": cases_with_reports,
                "skipped_beyond_budget": skipped,
            },
            "status_counts": {name: status_counts.get(name, 0) for name in STATUSES},
        }
        private = {
            "schema": SCHEMA_SIGNATURES_PRIVATE,
            "program": tool["program"]["id"],
            "source_root": source_root,
            "signatures": [
                {
                    "sanitizer": r.signature.sanitizer,
                    "kind": r.signature.kind,
                    "frame": r.signature.frame,
                    "cases": r.cases,
                    "first_case": r.first_case,
                    "first_t_ms": r.first_t_ms,
                    "example_cases": r.example_cases,
                    **({"detail": r.signature.detail} if r.signature.detail else {}),
                }
                for r in ordered
            ],
        }
        partial = out_dir.with_name(f".{out_dir.name}.partial-{os.getpid()}")
        partial.mkdir(parents=True)
        try:
            write_json(partial / "signatures-private.json", private)
            write_json(partial / "signatures-counts.json", counts)
            if out_dir.exists():
                out_dir.rmdir()
            os.replace(partial, out_dir)
        finally:
            if partial.exists():
                remove_tree(partial)
        return SignatureResult(out_dir, ordered, counts)
    finally:
        if not options.keep_work and scratch.exists():
            try:
                remove_tree(scratch)
            except OSError:
                pass
