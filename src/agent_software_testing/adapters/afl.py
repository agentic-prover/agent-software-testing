# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""AFL++ output directories to cases.

Input are the ``queue``, ``crashes`` and ``hangs`` directories of one or more
fuzzer instances plus the argument template AFL++ ran with. A case is a file
named ``id:...`` directly inside one of those directories (``.state``,
``README.txt`` and similar entries are ignored). The template decides how the
bytes reach the program:

* a template with an ``@@`` argument delivers the bytes as a file; ``@@`` is
  replaced by ``input`` and the case holds one file named ``input``;
* a template without ``@@`` delivers the bytes on standard input.

``t_ms`` is the ``time:`` field of the AFL++ file name (milliseconds since the
fuzzer started). Names without it (for example files from other tools in the
same directory) use the file's modification time minus ``start_time``; without
``start_time`` such files are an error, because their time would be a guess.
Identical bytes are kept once, at their earliest time (the evaluation's
standard replay does the same).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..common import DEFAULT_TIMEOUT_MS
from ..program import FixedTrack
from .common import AdapterError, AdapterReport, ToolInfo, new_writer, precheck_case

FILE_PLACEHOLDER = "@@"
INPUT_NAME = b"input"
_TIME = re.compile(r"(?:^|,)time:([0-9]+)(?:,|$)")
KIND_ORDER = {"queue": 0, "crashes": 1, "hangs": 2}


@dataclass(frozen=True)
class AflCase:
    path: Path
    kind: str  # queue | crashes | hangs
    t_ms: int | None  # None when the name has no time: field
    mtime: float


def _scan(directory: Path | str, kind: str) -> list[AflCase]:
    root = Path(directory)
    if not root.is_dir():
        raise AdapterError(f"not a directory: {root}")
    found: list[AflCase] = []
    for path in sorted(root.iterdir()):
        if not path.name.startswith("id:") or path.is_symlink() or not path.is_file():
            continue
        match = _TIME.search(path.name)
        found.append(
            AflCase(path, kind, int(match.group(1)) if match else None, path.stat().st_mtime)
        )
    return found


def collect_cases(
    queue_dirs: Sequence[Path | str] = (),
    crash_dirs: Sequence[Path | str] = (),
    hang_dirs: Sequence[Path | str] = (),
) -> list[AflCase]:
    cases: list[AflCase] = []
    for kind, directories in (("queue", queue_dirs), ("crashes", crash_dirs), ("hangs", hang_dirs)):
        for directory in directories:
            cases.extend(_scan(directory, kind))
    return cases


def delivery(
    argv_template: Sequence[str] | None, fixed: FixedTrack | None
) -> tuple[list[bytes], str]:
    """Return ``(argv, channel)`` where channel is ``"file"`` or ``"stdin"``."""

    if fixed is not None:
        return [t.encode("utf-8") for t in fixed.expected_argv()], fixed.input_channel
    if argv_template is None:
        raise AdapterError("an argument template is required on the open track (use --arg; may be empty)")
    uses_file = any(FILE_PLACEHOLDER in token for token in argv_template)
    argv = [token.replace(FILE_PLACEHOLDER, INPUT_NAME.decode()).encode("utf-8") for token in argv_template]
    return argv, "file" if uses_file else "stdin"


def convert_directories(
    out_dir: Path | str,
    info: ToolInfo,
    *,
    queue_dirs: Sequence[Path | str] = (),
    crash_dirs: Sequence[Path | str] = (),
    hang_dirs: Sequence[Path | str] = (),
    argv_template: Sequence[str] | None = None,
    fixed_track: FixedTrack | None = None,
    start_time: float | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    dedupe: bool = True,
) -> AdapterReport:
    """Write a bundle from AFL++ output directories."""

    if info.track == "fixed" and fixed_track is None:
        raise AdapterError("the fixed track needs the program's fixed invocation (pass the program definition)")
    argv, channel = delivery(argv_template, fixed_track if info.track == "fixed" else None)
    cases = collect_cases(queue_dirs, crash_dirs, hang_dirs)
    report = AdapterReport(source_cases=len(cases))
    if not cases:
        raise AdapterError("no AFL++ cases (files named id:*) found in the given directories")

    timed: list[tuple[int, int, str, AflCase]] = []
    for case in cases:
        if case.t_ms is not None:
            t_ms = case.t_ms
        elif start_time is not None:
            t_ms = max(0, int(round((case.mtime - start_time) * 1000)))
        else:
            raise AdapterError(
                f"{case.path.name} has no time: field; pass the fuzzer's start time (epoch seconds)"
            )
        timed.append((t_ms, KIND_ORDER[case.kind], case.path.name, case))
    timed.sort(key=lambda item: item[:3])

    writer = new_writer(out_dir, info)
    seen: set[str] = set()
    for t_ms, _kind_rank, _name, case in timed:
        try:
            body = case.path.read_bytes()
        except OSError as exc:
            report.skip(case.path.name, f"unreadable: {exc}")
            continue
        digest = hashlib.sha256(body).hexdigest()
        if dedupe and digest in seen:
            report.duplicates += 1
            continue
        files = [{"path": INPUT_NAME, "kind": "file", "mode": "0644", "body": body}] if channel == "file" else []
        stdin = body if channel == "stdin" else None
        reason = precheck_case(argv=argv, stdin=stdin, files=files, env={})
        if reason:
            report.skip(case.path.name, reason)
            continue
        seen.add(digest)
        writer.add_case(
            t_ms=t_ms,
            argv=argv,
            stdin=stdin,
            files=files,
            timeout_ms=timeout_ms,
            origin=case.kind,
        )
        report.written_cases += 1
    writer.finish()
    return report
