# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Branch coverage from gcov data through lcov.

This module follows the measurement scripts of the reference evaluation
(``conventional-coverage.py`` and ``lcov_report.py``) with the same semantics:

* ``lcov --capture --directory . --output-file F --rc lcov_branch_coverage=1``
  run in the directory that holds the ``.gcno`` and ``.gcda`` files (with ``--no-markers``
  added when the program definition says its exclusion markers are ignored, see
  :attr:`GcovTools.no_markers`);
* the initial capture (``--capture --initial``) over a copy of the build tree
  that keeps only the ``.gcno`` files the binary links (found from the
  ``.gcda`` paths embedded in the binary), failing on any lcov warning;
* parsing of ``SF``, ``FN``, ``FNDA``, ``DA`` and ``BRDA`` records, merging of
  records that several objects emit for the same included source, summing
  duplicate ``(block, branch)`` counts on one line, counting a branch only on
  lines that have a ``DA`` record, and source paths relative to the compile
  root.

A branch key is ``file:line:block_id:branch_id``. The covered keys of a capture
are those with a count above zero; the universe is the set of keys of the
initial capture.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .common import AgentSoftwareTestingError, printable, sha256_file, sha256_hex

MAX_LCOV_BYTES = 1024 * 1024 * 1024
MAX_LCOV_LINE_BYTES = 16 * 1024 * 1024
MAX_LCOV_LINES = 20_000_000
#: Largest ``.gcda`` file accepted from a shard (a real one is a few megabytes at most).
MAX_GCDA_BYTES = 64 * 1024 * 1024
_WARNING_RE = re.compile(r"\bwarning:", re.IGNORECASE)
#: What gcov says about a damaged ``.gcda`` file. lcov still exits 0 and counts what it could read.
_DAMAGED_PROFILE_RE = re.compile(
    r"\.gcda:(profile mismatch|not a gcov data file|stamp mismatch|version\b|overread|corrupt)", re.IGNORECASE
)
_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e\t]{4,}")


class CaptureError(AgentSoftwareTestingError):
    """A coverage capture failed or produced nothing."""


@dataclass(frozen=True)
class GcovTools:
    """The external tools, configurable for other images.

    ``lcov`` is the lcov executable; ``gcov`` (when set) is passed to lcov as
    ``--gcov-tool``; ``strict_warnings`` makes any lcov warning in the initial
    capture an error, as in the canonical pipeline.
    """

    lcov: str = "lcov"
    gcov: str | None = None
    strict_warnings: bool = True
    #: Longest time one lcov run may take (``None`` for no limit). The tool and
    #: the gcov processes it started are killed when it runs out.
    timeout_s: float | None = 3600.0
    #: Pass ``--no-markers`` to lcov: ``LCOV_EXCL_*`` comments in the sources then do not remove
    #: branches from the universe or from a capture. The default (lcov's own) honours them. A program
    #: definition chooses per program (``build.exclusion_markers``, SPEC.md section 4.3), because the
    #: published universes follow both rules: Confetti's 632 branches honour the markers (636
    #: without), xmlwf's 6,293 ignore them (6,285 with).
    no_markers: bool = False

    def lcov_command(self, *args: str) -> list[str]:
        command = [self.lcov, *args]
        if self.no_markers:
            command.append("--no-markers")
        if self.gcov:
            command += ["--gcov-tool", self.gcov]
        return command

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        return env

    def versions(self) -> dict[str, str | None]:
        return {"lcov": _first_line([self.lcov, "--version"]), "gcov": _first_line([self.gcov or "gcov", "--version"])}


def run_tool(
    command: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> tuple[int, str]:
    """Run an external tool; return ``(exit status, combined output)``.

    The tool runs in a session of its own so that a run that exceeds
    ``timeout_s`` is killed together with the processes it started (lcov runs
    gcov); the timeout is a :class:`CaptureError`, never a silent result.
    """

    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise CaptureError(f"cannot run {command[0]}: {printable(exc, 200)}") from exc
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()
        raise CaptureError(f"{command[0]} did not finish within {timeout_s:g} seconds and was killed") from None
    return proc.returncode, output.decode("utf-8", errors="replace")


def _first_line(command: list[str]) -> str | None:
    try:
        done = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = done.stdout.strip().splitlines()
    return lines[0].strip() if lines else None


# ---------------------------------------------------------------------------
# LCOV records
# ---------------------------------------------------------------------------


def normalize_source_path(raw_name: str, gcov_root: Path) -> str | None:
    """Source path relative to the compile root, as the canonical reporter does."""

    if not raw_name:
        return None
    path = Path(raw_name)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(gcov_root.resolve()).as_posix()
        except ValueError:
            parts = path.parts
            for index in range(len(parts) - 1, -1, -1):
                if parts[index] == gcov_root.name and index + 1 < len(parts):
                    return Path(*parts[index + 1 :]).as_posix()
            # Sources outside the build tree keep their full path so that
            # distinct headers with equal base names stay distinct.
            return path.as_posix()
    return path.as_posix()


def parse_lcov_records(info_path: Path | str) -> list[dict[str, Any]]:
    """Parse an lcov ``.info`` file into one record per ``SF`` section."""

    path = Path(info_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CaptureError(f"cannot read lcov output {path}: {exc}") from exc
    if size > MAX_LCOV_BYTES:
        raise CaptureError(f"lcov output {path} is larger than {MAX_LCOV_BYTES} bytes")
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    lines = 0
    with open(path, "rb") as handle:
        for encoded in handle:
            lines += 1
            if lines > MAX_LCOV_LINES:
                raise CaptureError(f"lcov output {path} has more than {MAX_LCOV_LINES} lines")
            if len(encoded) > MAX_LCOV_LINE_BYTES:
                raise CaptureError(f"lcov output {path} has a line longer than {MAX_LCOV_LINE_BYTES} bytes")
            line = encoded.decode("utf-8", errors="replace").rstrip("\r\n")
            try:
                if line == "end_of_record":
                    if current is not None:
                        records.append(current)
                    current = None
                elif line.startswith("TN:"):
                    continue
                elif line.startswith("SF:"):
                    current = {
                        "file": line[3:],
                        "function_lines": {},
                        "function_counts": {},
                        "line_counts": {},
                        "branches_by_line": {},
                    }
                elif current is None:
                    continue
                elif line.startswith("FN:"):
                    line_raw, name = line[3:].split(",", 1)
                    current["function_lines"][name] = int(line_raw or 0)
                elif line.startswith("FNDA:"):
                    count_raw, name = line[5:].split(",", 1)
                    current["function_counts"][name] = current["function_counts"].get(name, 0) + int(
                        float(count_raw or 0)
                    )
                elif line.startswith("DA:"):
                    line_raw, count_raw, *_rest = line[3:].split(",")
                    number = int(line_raw or 0)
                    current["line_counts"][number] = current["line_counts"].get(number, 0) + int(
                        float(count_raw or 0)
                    )
                elif line.startswith("BRDA:"):
                    line_raw, block_raw, branch_raw, taken_raw = line[5:].split(",", 3)
                    number = int(line_raw or 0)
                    count = 0 if taken_raw == "-" else int(float(taken_raw or 0))
                    current["branches_by_line"].setdefault(number, []).append(
                        {"block_id": int(block_raw or 0), "branch_id": int(branch_raw or 0), "count": count}
                    )
            except ValueError as exc:
                raise CaptureError(f"malformed lcov record in {path} line {lines}: {line[:200]!r} ({exc})") from exc
    return records


def merge_duplicate_lcov_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge records emitted for one source included by several objects.

    ``geninfo`` may emit one ``SF`` record per compilation unit for a shared
    header. They describe one source collectively, so they are merged (counts
    summed); treating them as separate files would inflate the universe.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(str(record.get("file") or ""), []).append(record)

    merged_records: list[dict[str, Any]] = []
    for raw_source, source_records in grouped.items():
        if len(source_records) == 1:
            merged_records.append(source_records[0])
            continue
        function_lines: dict[str, int] = {}
        function_counts: dict[str, int] = {}
        line_counts: dict[int, int] = {}
        branch_counts: dict[int, dict[tuple[int, int], int]] = {}
        for record in source_records:
            for name, raw_line in dict(record.get("function_lines", {})).items():
                number = int(raw_line or 0)
                previous = function_lines.get(str(name))
                if previous is not None and previous != number:
                    raise CaptureError(
                        f"LCOV function identity is ambiguous within source {raw_source!r}: "
                        f"{name!r} occurs at lines {previous} and {number}"
                    )
                function_lines[str(name)] = number
            for name, raw_count in dict(record.get("function_counts", {})).items():
                function_counts[str(name)] = function_counts.get(str(name), 0) + int(raw_count or 0)
            for raw_line, raw_count in dict(record.get("line_counts", {})).items():
                number = int(raw_line)
                line_counts[number] = line_counts.get(number, 0) + int(raw_count or 0)
            for raw_line, branches in dict(record.get("branches_by_line", {})).items():
                number = int(raw_line)
                bucket = branch_counts.setdefault(number, {})
                for branch in branches or ():
                    key = (int(branch.get("block_id", 0) or 0), int(branch.get("branch_id", 0) or 0))
                    bucket[key] = bucket.get(key, 0) + int(branch.get("count", 0) or 0)
        merged_records.append(
            {
                "file": raw_source,
                "function_lines": function_lines,
                "function_counts": function_counts,
                "line_counts": line_counts,
                "branches_by_line": {
                    number: [
                        {"block_id": block, "branch_id": branch, "count": count}
                        for (block, branch), count in sorted(items.items())
                    ]
                    for number, items in sorted(branch_counts.items())
                },
            }
        )
    return merged_records


def branch_counts(info_path: Path | str, compile_root: Path | str) -> dict[str, int]:
    """Map every branch key of an lcov file to its execution count.

    A branch is counted only on a line that has a ``DA`` record (as in the
    canonical report), duplicate ``(block, branch)`` entries of one line are
    summed, and two raw sources that normalise to the same path are an error.
    """

    root = Path(compile_root)
    records = merge_duplicate_lcov_records(parse_lcov_records(info_path))
    origins: dict[str, str] = {}
    counts: dict[str, int] = {}
    for record in records:
        raw_source = str(record.get("file") or "")
        relative = normalize_source_path(raw_source, root)
        if relative is None:
            continue
        previous = origins.get(relative)
        if previous is not None:
            raise CaptureError(
                f"LCOV source identity is ambiguous: {previous!r} and {raw_source!r} both normalize to {relative!r}"
            )
        origins[relative] = raw_source
        branches_by_line = dict(record.get("branches_by_line", {}))
        for number in sorted(dict(record.get("line_counts", {}))):
            summed: dict[tuple[int, int], int] = {}
            for branch in branches_by_line.get(number, ()):
                key = (int(branch.get("block_id", 0) or 0), int(branch.get("branch_id", 0) or 0))
                summed[key] = summed.get(key, 0) + int(branch.get("count", 0) or 0)
            for (block, branch_id), count in summed.items():
                counts[f"{relative}:{number}:{block}:{branch_id}"] = count
    return counts


# ---------------------------------------------------------------------------
# Private copies of the build tree
# ---------------------------------------------------------------------------


def copy_tree(
    src: Path | str,
    dst: Path | str,
    *,
    skip_suffixes: tuple[str, ...] = (".gcda",),
    read_only: bool = False,
) -> list[Path]:
    """Copy a directory tree; every file is a real copy, never a hard link.

    A copy cannot reach the original through a write to a file, which hard
    links would allow. Symbolic links are recreated, not followed; files with
    a suffix in ``skip_suffixes`` and special files (pipes, sockets, devices)
    are left out. With ``read_only`` the write bits are removed from every file
    and, after the last file is in place, from every directory. Iterative, so
    deep trees are fine. Returns the directories of the copy, ``dst`` first.
    """

    directories: list[Path] = []
    stack = [(Path(src), Path(dst))]
    while stack:
        source_dir, target_dir = stack.pop()
        target_dir.mkdir(parents=True, exist_ok=True)
        directories.append(target_dir)
        with os.scandir(source_dir) as entries:
            listing = list(entries)
        for entry in listing:
            target = target_dir / entry.name
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), target)
            elif entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), target))
            elif entry.name.endswith(skip_suffixes) or not entry.is_file(follow_symlinks=False):
                continue
            else:
                shutil.copy2(entry.path, target)
                if read_only:
                    os.chmod(target, stat.S_IMODE(os.stat(target).st_mode) & ~0o222)
    if read_only:
        set_directories_writable(directories, False)
    return directories


def set_directories_writable(directories: Iterable[Path], writable: bool) -> None:
    """Add or remove the owner's write bit of each directory (a directory the program can write to is one it can change)."""

    for directory in directories:
        mode = stat.S_IMODE(os.stat(directory).st_mode)
        os.chmod(directory, (mode | 0o200) if writable else (mode & ~0o222))


def find_files(root: Path | str, suffix: str) -> list[Path]:
    """All regular files below ``root`` whose name ends with ``suffix`` (iterative)."""

    found: list[Path] = []
    stack = [Path(root)]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.name.endswith(suffix):
                    found.append(Path(entry.path))
    return found


# ---------------------------------------------------------------------------
# Universe (initial capture)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Universe:
    keys: frozenset[str]
    embedded_profile_count: int
    info_path: Path
    log: str
    #: Notes files (relative to the compile root) of the objects linked into the binary.
    profiles: frozenset[PurePosixPath] = frozenset()
    #: SHA-256 over the sorted lines ``<sha256 of the notes file>  <relative path>\n``.
    gcno_set_sha256: str = ""

    def expected_profile_data(self) -> frozenset[PurePosixPath]:
        """The ``.gcda`` files a run of the binary can write, relative to the compile root."""

        return frozenset(profile.with_suffix(".gcda") for profile in self.profiles)


def embedded_profiles(binary: Path | str, compile_root: Path | str) -> set[PurePosixPath]:
    """``.gcno`` paths (relative to the compile root) of the objects linked into ``binary``.

    The gcov runtime embeds the absolute path of each object's ``.gcda`` file
    in the binary. Those below ``compile_root`` name the objects that make up
    the program, as ``strings binary`` would show them.
    """

    marker = PurePosixPath(Path(compile_root).as_posix()).as_posix().rstrip("/") + "/"
    data = Path(binary).read_bytes()
    profiles: set[PurePosixPath] = set()
    for match in _PRINTABLE_RUN.finditer(data):
        value = match.group().decode("ascii")
        if not value.endswith(".gcda"):
            continue
        _prefix, separator, relative = value.partition(marker)
        if not separator or not relative:
            continue
        profile = PurePosixPath(relative).with_suffix(".gcno")
        if profile.is_absolute() or ".." in profile.parts:
            raise CaptureError(f"unsafe embedded gcov profile path {value!r}")
        profiles.add(profile)
    return profiles


def choose_compile_root(
    build_root: Path | str,
    binary: Path | str,
    explicit: Path | str | None = None,
) -> Path:
    """The directory the build was compiled under.

    An explicit value is made absolute and used as given. Otherwise the build
    root is tried as a resolved path (symbolic links followed, as ``getcwd``
    reports it) and as a plain absolute path; the first one below which the
    binary embeds profile paths wins. When neither matches the resolved path is
    returned and :func:`initial_capture` explains the mismatch.
    """

    if explicit is not None:
        return Path(os.path.abspath(explicit))
    resolved = Path(build_root).resolve()
    plain = Path(os.path.abspath(build_root))
    for candidate in (resolved, plain):
        if embedded_profiles(binary, candidate):
            return candidate
    return resolved


def initial_capture(
    build_root: Path | str,
    binary: Path | str,
    work_dir: Path | str,
    tools: GcovTools,
    compile_root: Path | str | None = None,
) -> Universe:
    """Capture the branch universe of ``binary`` from the build tree's ``.gcno`` files.

    The tree is copied into ``work_dir``; every ``.gcno`` the
    binary does not link is removed; ``lcov --capture --initial`` runs on the
    copy. The result must contain at least one branch and none covered.
    """

    build_root = Path(build_root)
    compile_root = Path(compile_root) if compile_root is not None else build_root.resolve()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    stale = find_files(build_root, ".gcda")
    if stale:
        raise CaptureError(f"the build tree contains stale profiles, for example {stale[0]}")

    profiles = embedded_profiles(binary, compile_root)
    if not profiles:
        raise CaptureError(
            f"the binary {binary} embeds no gcov profile path below {compile_root.as_posix()}/; "
            "build the program at its compile path or pass the compile root it was built under"
        )
    missing = sorted(str(p) for p in profiles if not (build_root / p).is_file())
    if missing:
        raise CaptureError(f"gcov notes files of the binary are missing from the build tree: {missing[0]}")

    base_root = work_dir / "target-base-gcov-root"
    copy_tree(build_root, base_root)
    for note in find_files(base_root, ".gcno"):
        if PurePosixPath(note.relative_to(base_root).as_posix()) not in profiles:
            note.unlink()

    info = work_dir / "target-base.info"
    command = tools.lcov_command(
        "--capture",
        "--initial",
        "--directory",
        str(base_root),
        "--output-file",
        str(info),
        "--rc",
        "lcov_branch_coverage=1",
    )
    returncode, log = run_tool(command, env=tools.environment(), timeout_s=tools.timeout_s)
    (work_dir / "target-base-capture.log").write_text(log, encoding="utf-8", errors="replace")
    if returncode != 0 or not info.is_file() or info.stat().st_size == 0:
        raise CaptureError(f"initial coverage capture failed (exit {returncode}): {log[-3000:]}")
    if tools.strict_warnings and _WARNING_RE.search(log):
        raise CaptureError(f"initial coverage capture printed warnings: {log[-3000:]}")

    counts = branch_counts(info, compile_root)
    if not counts:
        raise CaptureError("initial coverage capture found no branches")
    covered = sum(1 for count in counts.values() if count > 0)
    if covered:
        raise CaptureError(f"initial capture reports {covered} covered branches; it must report none")
    return Universe(
        frozenset(counts),
        len(profiles),
        info,
        log,
        profiles=frozenset(profiles),
        gcno_set_sha256=gcno_set_digest(build_root, profiles),
    )


def gcno_set_digest(build_root: Path | str, profiles: Iterable[PurePosixPath]) -> str:
    """Digest of the notes files that define a universe: names and contents, sorted by name."""

    lines = [
        f"{sha256_file(Path(build_root) / profile)}  {profile.as_posix()}\n"
        for profile in sorted(profiles, key=lambda item: item.as_posix())
    ]
    return sha256_hex("".join(lines).encode("utf-8"))


# ---------------------------------------------------------------------------
# Capture of a directory with .gcda files
# ---------------------------------------------------------------------------


def collect_profile_data(gcda_root: Path | str, expected: frozenset[PurePosixPath]) -> list[PurePosixPath]:
    """The ``.gcda`` files of a shard, after checking that nothing else is there.

    ``gcda_root`` is the tree ``GCOV_PREFIX`` points at, the only place a
    program under test can write in a shard. Only regular files whose
    relative path is in ``expected`` (the ``.gcda`` names derived from the
    ``.gcno`` files the binary links), in directories on the way to them,
    are accepted. A symbolic link, a pipe, a directory or file nobody
    expected, or an oversized file raises :class:`CaptureError`: a program
    that writes anything else there is not producing coverage data, and its
    measurement is not used. (A program can still write a well-formed file at
    an expected name; see SPEC.md section 6.7.) Returns the relative paths of
    the files present.
    """

    root = Path(gcda_root)
    expected_dirs = {parent for path in expected for parent in path.parents if parent != PurePosixPath(".")}
    present: list[PurePosixPath] = []
    stack: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath("."))]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as entries:
            listing = list(entries)
        for entry in listing:
            rel = prefix / entry.name if prefix != PurePosixPath(".") else PurePosixPath(entry.name)
            if entry.is_symlink():
                raise CaptureError(f"profile data of a shard contains a symbolic link: {printable(rel.as_posix())}")
            if entry.is_dir(follow_symlinks=False):
                if rel not in expected_dirs:
                    raise CaptureError(f"profile data of a shard contains an unexpected directory: {printable(rel.as_posix())}")
                stack.append((Path(entry.path), rel))
            elif entry.is_file(follow_symlinks=False):
                if rel not in expected:
                    raise CaptureError(f"profile data of a shard contains an unexpected file: {printable(rel.as_posix())}")
                size = entry.stat(follow_symlinks=False).st_size
                if size > MAX_GCDA_BYTES:
                    raise CaptureError(
                        f"profile data file {printable(rel.as_posix())} is {size} bytes, limit is {MAX_GCDA_BYTES}"
                    )
                present.append(rel)
            else:
                raise CaptureError(f"profile data of a shard contains a special file: {printable(rel.as_posix())}")
    present.sort(key=lambda item: item.as_posix())
    return present


def capture_covered(
    gcda_root: Path | str,
    info_path: Path | str,
    tools: GcovTools,
    compile_root: Path | str,
) -> frozenset[str]:
    """Run lcov in ``gcda_root`` and return the covered branch keys.

    ``gcda_root`` holds the ``.gcno`` files and the ``.gcda`` files side by
    side. Raises :class:`CaptureError` when lcov fails or its output is missing
    or empty: a failed capture is never reported as zero coverage.
    """

    gcda_root = Path(gcda_root)
    info = Path(info_path)
    info.unlink(missing_ok=True)
    command = tools.lcov_command(
        "--capture",
        "--directory",
        ".",
        "--output-file",
        str(info),
        "--rc",
        "lcov_branch_coverage=1",
    )
    returncode, output = run_tool(command, cwd=gcda_root, env=tools.environment(), timeout_s=tools.timeout_s)
    if not info.is_file() or info.stat().st_size == 0:
        raise CaptureError(f"coverage capture produced no output (exit {returncode}): {output[-3000:]}")
    if returncode != 0:
        raise CaptureError(f"coverage capture failed (exit {returncode}): {output[-3000:]}")
    damaged = _DAMAGED_PROFILE_RE.search(output)
    if damaged:
        raise CaptureError(f"coverage data is damaged ({damaged.group(1)}), so its branches cannot be counted")
    counts = branch_counts(info, compile_root)
    if not counts:
        raise CaptureError("coverage capture contains no branch records")
    return frozenset(key for key, count in counts.items() if count > 0)
