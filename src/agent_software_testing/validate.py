# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Validation of bundles and of the other documents (no third-party schema library).

``validate_bundle`` applies every rule of contract section 2 and returns a
:class:`ValidationReport` whose ``errors`` list names each problem precisely
(file and line, case id, field, stable code, message).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bundle import BLOBS_DIR, CASES_FILE, TOOL_FILE, BlankLineError, Bundle, BundleError
from .case import Case, parse_case
from .common import (
    BRANCH_KEY_FORMAT,
    FIXED_INPUT_NAME,
    MAX_BLOB_BYTES,
    MAX_CASE_BYTES,
    MAX_CASES,
    MAX_WALL_S,
    SCHEMA_BRANCH_SET,
    SCHEMA_PROGRAM,
    SCHEMA_REPLAY,
    SCHEMA_SIGNATURE_COUNTS,
    SCHEMA_TOOL,
    SHA256_RE,
    TOOL_FAMILIES,
    TRACKS,
    AgentSoftwareTestingError,
    NotRegularFileError,
    ValidationError,
    is_plain_int,
    open_regular_file,
    printable,
    read_json_file,
)
from .kinds import KINDS_BY_SANITIZER, MAX_KIND_CHARS
from .program import Program, find_program, validate_program_document

MAX_REPORTED_ERRORS = 1000
#: ``file:line:block_id:branch_id``; the three numbers are decimal without
#: sign and without leading zeros (SPEC.md section 3.1).
BRANCH_KEY_RE = re.compile(r".+:(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)\Z", re.DOTALL)
PROGRAM_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


@dataclass
class ValidationReport:
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, where: str, code: str, message: str) -> None:
        if len(self.errors) >= MAX_REPORTED_ERRORS:
            self.truncated = True
            return
        self.errors.append(ValidationError(where, code, message))

    def warn(self, where: str, code: str, message: str) -> None:
        if len(self.warnings) < MAX_REPORTED_ERRORS:
            self.warnings.append(ValidationError(where, code, message))

    def extend(self, errors: list[ValidationError]) -> None:
        for error in errors:
            self.add(error.where, error.code, error.message)

    def codes(self) -> list[str]:
        return [e.code for e in self.errors]


# ---------------------------------------------------------------------------
# tool.json
# ---------------------------------------------------------------------------

_TOOL_TOP = {"schema", "tool", "program", "track", "repeat", "budget", "resources", "notes"}
_TOOL_REQUIRED = ("schema", "tool", "program", "track", "repeat", "budget")
_TOOL_TOOL = {"name", "version", "family", "model", "config_sha256", "url"}
_TOOL_BUDGET = {"wall_s", "cores", "notes"}
_TOOL_RESOURCES = {"output_tokens", "input_tokens", "cpu_s"}


def validate_tool_document(doc: Any, where: str = TOOL_FILE) -> list[ValidationError]:
    errors: list[ValidationError] = []

    def err(code: str, message: str, field: str = "") -> None:
        errors.append(ValidationError(f"{where} {field}".rstrip(), code, message))

    if not isinstance(doc, dict):
        return [ValidationError(where, "tool.type", "tool.json must be a JSON object")]
    for key in sorted(set(doc) - _TOOL_TOP):
        err("tool.unknown_key", f"unknown key {key!r}", key)
    for key in _TOOL_REQUIRED:
        if key not in doc:
            err("tool.missing_key", f"missing required key {key!r}", key)
    if "schema" in doc and doc["schema"] != SCHEMA_TOOL:
        err("tool.schema", f"schema must be {SCHEMA_TOOL!r}, got {doc['schema']!r}", "schema")

    tool = doc.get("tool")
    if "tool" in doc:
        if not isinstance(tool, dict):
            err("tool.tool", "tool must be an object", "tool")
        else:
            for key in sorted(set(tool) - _TOOL_TOOL):
                err("tool.unknown_key", f"unknown key {key!r}", f"tool.{key}")
            name = tool.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                err("tool.name", "tool.name must be a non-empty string of at most 200 characters", "tool.name")
            if tool.get("family") not in TOOL_FAMILIES:
                err("tool.family", f"tool.family must be one of {list(TOOL_FAMILIES)}", "tool.family")
            for key in ("version", "model", "url"):
                if tool.get(key) is not None and not isinstance(tool[key], str):
                    err("tool.type", f"tool.{key} must be a string or null", f"tool.{key}")
            digest = tool.get("config_sha256")
            if digest is not None and (not isinstance(digest, str) or not SHA256_RE.match(digest)):
                err("tool.config_sha256", "config_sha256 must be null or 64 lowercase hex digits", "tool.config_sha256")

    program = doc.get("program")
    if "program" in doc:
        if not isinstance(program, dict):
            err("tool.program", "program must be an object", "program")
        else:
            for key in sorted(set(program) - {"id", "source_sha256"}):
                err("tool.unknown_key", f"unknown key {key!r}", f"program.{key}")
            if not isinstance(program.get("id"), str) or not PROGRAM_ID_PATTERN.match(program["id"]):
                err("tool.program_id", "program.id must match [a-z0-9][a-z0-9_-]*", "program.id")
            digest = program.get("source_sha256")
            if not isinstance(digest, str) or not SHA256_RE.match(digest):
                err("tool.source_sha256", "program.source_sha256 must be 64 lowercase hex digits", "program.source_sha256")

    if "track" in doc and doc["track"] not in TRACKS:
        err("tool.track", f"track must be one of {list(TRACKS)}", "track")
    if "repeat" in doc and (not is_plain_int(doc["repeat"]) or doc["repeat"] < 1):
        err("tool.repeat", "repeat must be an integer of at least 1", "repeat")

    budget = doc.get("budget")
    if "budget" in doc:
        if not isinstance(budget, dict):
            err("tool.budget", "budget must be an object", "budget")
        else:
            for key in sorted(set(budget) - _TOOL_BUDGET):
                err("tool.unknown_key", f"unknown key {key!r}", f"budget.{key}")
            if not is_plain_int(budget.get("wall_s")) or not 1 <= budget["wall_s"] <= MAX_WALL_S:
                err(
                    "tool.budget_wall",
                    f"budget.wall_s must be an integer from 1 to {MAX_WALL_S} (seven days)",
                    "budget.wall_s",
                )
            if "cores" in budget and (not is_plain_int(budget["cores"]) or budget["cores"] < 1):
                err("tool.budget_cores", "budget.cores must be an integer of at least 1", "budget.cores")
            if "notes" in budget and not isinstance(budget["notes"], str):
                err("tool.type", "budget.notes must be a string", "budget.notes")

    resources = doc.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            err("tool.resources", "resources must be an object", "resources")
        else:
            for key in sorted(set(resources) - _TOOL_RESOURCES):
                err("tool.unknown_key", f"unknown key {key!r}", f"resources.{key}")
            for key in ("output_tokens", "input_tokens"):
                value = resources.get(key)
                if value is not None and (not is_plain_int(value) or value < 0):
                    err("tool.resources", f"resources.{key} must be null or a non-negative integer", f"resources.{key}")
            value = resources.get("cpu_s")
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            ):
                err("tool.resources", "resources.cpu_s must be null or a non-negative number", "resources.cpu_s")
    if "notes" in doc and not isinstance(doc["notes"], str):
        err("tool.type", "notes must be a string", "notes")
    return errors


# ---------------------------------------------------------------------------
# Blobs
# ---------------------------------------------------------------------------


class _BlobChecker:
    """Checks each blob once: present, regular, small enough, hash matches."""

    def __init__(self, bundle: Bundle, verify_hash: bool):
        self.dir = bundle.root / BLOBS_DIR
        self.verify_hash = verify_hash
        self._cache: dict[str, tuple[int | None, tuple[str, str] | None]] = {}

    def check(self, digest: str) -> tuple[int | None, tuple[str, str] | None]:
        """Return ``(size, problem)``; ``problem`` is ``(code, message)`` or ``None``."""

        cached = self._cache.get(digest)
        if cached is not None:
            return cached
        result = self._check(digest)
        self._cache[digest] = result
        return result

    def _check(self, digest: str) -> tuple[int | None, tuple[str, str] | None]:
        import hashlib

        path = self.dir / digest
        try:
            fd = open_regular_file(path)
        except FileNotFoundError:
            return None, ("blob.missing", f"blob {digest} does not exist in {BLOBS_DIR}/")
        except NotRegularFileError:
            return None, ("blob.not_regular", f"blob {digest} is not a regular file")
        except OSError as exc:
            return None, ("blob.unreadable", f"blob {digest} cannot be opened (symlink?): {printable(exc, 200)}")
        try:
            info = os.fstat(fd)
            if info.st_size > MAX_BLOB_BYTES:
                return info.st_size, (
                    "blob.too_large",
                    f"blob {digest} is {info.st_size} bytes, limit is {MAX_BLOB_BYTES}",
                )
            if self.verify_hash:
                sha = hashlib.sha256()
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        sha.update(chunk)
                if sha.hexdigest() != digest:
                    return info.st_size, (
                        "blob.hash",
                        f"blob {digest} has content with sha256 {sha.hexdigest()}",
                    )
            return info.st_size, None
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


def validate_bundle(
    path: Path | str,
    *,
    program: Program | None = None,
    programs_dir: Path | str | None = None,
    strict_budget: bool = False,
    verify_blob_hashes: bool = True,
) -> ValidationReport:
    """Validate the bundle at ``path`` against contract section 2.

    ``program`` is the program definition to check against; when omitted it is
    looked up by ``tool.program.id`` in ``programs_dir`` (default search: see
    :func:`agent_software_testing.program.locate_programs_dir`). The fixed-track check
    needs it. A case with ``t_ms`` beyond ``budget.wall_s`` is not replayed and
    only counted in a warning (SPEC.md section 5.8, rule 9); ``strict_budget``
    makes it an error.
    """

    report = ValidationReport()
    root = Path(path)
    try:
        if root.is_symlink() or not root.is_dir():
            report.add(str(root), "bundle.not_directory", "bundle must be a real directory")
            return report
        bundle = Bundle(root)
    except BundleError as exc:
        report.add(str(root), "bundle.not_directory", str(exc))
        return report

    # tool.json ----------------------------------------------------------
    tool: dict[str, Any] | None = None
    try:
        document = read_json_file(root / TOOL_FILE, max_bytes=1024 * 1024)
    except (OSError, AgentSoftwareTestingError) as exc:
        report.add(TOOL_FILE, "tool.unreadable", str(exc))
    else:
        tool_errors = validate_tool_document(document)
        report.extend(tool_errors)
        if isinstance(document, dict):
            tool = document
    track = tool.get("track") if tool else None
    budget_ms: int | None = None
    if tool and isinstance(tool.get("budget"), dict) and is_plain_int(tool["budget"].get("wall_s")):
        budget_ms = tool["budget"]["wall_s"] * 1000

    # program definition ---------------------------------------------------
    program_id = None
    if tool and isinstance(tool.get("program"), dict):
        pid = tool["program"].get("id")
        program_id = pid if isinstance(pid, str) else None
    if program is None and program_id and PROGRAM_ID_PATTERN.match(program_id):
        try:
            program = find_program(program_id, programs_dir)
        except AgentSoftwareTestingError as exc:
            report.add(f"programs/{program_id}/program.json", "program.invalid", str(exc))
    if program is not None and tool and isinstance(tool.get("program"), dict):
        if program.id != program_id:
            report.add(TOOL_FILE, "tool.program_mismatch", f"bundle is for {program_id!r} but the program definition is {program.id!r}")
        digest = tool["program"].get("source_sha256")
        if isinstance(digest, str) and digest != program.source_sha256:
            report.add(
                TOOL_FILE,
                "tool.source_mismatch",
                "program.source_sha256 differs from the pinned source of the program definition",
            )
    if program is None and tool is not None:
        message = f"no definition found for program {program_id!r}; program-specific checks skipped"
        if track == "fixed":
            report.add(TOOL_FILE, "tool.program_unknown", message + " (the fixed track cannot be checked)")
        else:
            report.warn(TOOL_FILE, "tool.program_unknown", message)
    fixed = None
    if track == "fixed" and program is not None:
        if program.fixed_track is None:
            report.add(TOOL_FILE, "tool.fixed_track_unavailable", f"program {program.id!r} has no fixed track")
        else:
            fixed = program.fixed_track

    # blobs directory -------------------------------------------------------
    blobs_dir = root / BLOBS_DIR
    if blobs_dir.is_symlink() or not blobs_dir.is_dir():
        report.add(BLOBS_DIR, "bundle.blobs_dir", "blobs/ must be a real directory")
    checker = _BlobChecker(bundle, verify_blob_hashes)

    # cases.jsonl -------------------------------------------------------------
    referenced: set[str] = set()
    seen_ids: dict[str, int] = {}
    previous_t = -1
    total_cases = 0
    over_budget = 0
    unlisted_surfaces: dict[str, int] = {}
    try:
        raw_cases = bundle.iter_raw_cases()
        for number, raw in raw_cases:
            if isinstance(raw, BlankLineError):
                report.add(f"{CASES_FILE}:{number}", "cases.blank_line", str(raw))
                continue
            if isinstance(raw, BundleError):
                report.add(f"{CASES_FILE}:{number}", "cases.json", str(raw))
                continue
            total_cases += 1
            if total_cases > MAX_CASES:
                report.add(CASES_FILE, "cases.too_many", f"more than {MAX_CASES} cases; the rest is not examined")
                break
            case, case_errors = parse_case(raw, number)
            report.extend(case_errors)
            if case is None:
                continue
            where = case.where
            # unique ids
            first = seen_ids.get(case.id)
            if first is not None:
                report.add(where, "case.duplicate_id", f"id {case.id!r} already used on line {first}")
            else:
                seen_ids[case.id] = number
            # order and budget
            if case.t_ms < previous_t:
                report.add(
                    where,
                    "cases.order",
                    f"t_ms {case.t_ms} is earlier than the previous case ({previous_t}); cases must be ordered by t_ms",
                )
            previous_t = max(previous_t, case.t_ms)
            if budget_ms is not None and case.t_ms > budget_ms:
                over_budget += 1
                if strict_budget:
                    report.add(
                        where,
                        "case.over_budget",
                        f"t_ms {case.t_ms} is beyond the budget of {budget_ms} ms",
                    )
            # blobs
            case_bytes = 0
            for digest in _ordered_blobs(case):
                referenced.add(digest)
                size, problem = checker.check(digest)
                if problem is not None:
                    report.add(where, problem[0], problem[1])
                if size is not None:
                    case_bytes += size
            case_bytes += sum(len(a) for a in case.argv) + sum(len(v) for _name, v in case.env)
            if case_bytes > MAX_CASE_BYTES:
                report.add(
                    where,
                    "case.too_large",
                    f"stdin, file bodies, argv and env total {case_bytes} bytes, limit is {MAX_CASE_BYTES}",
                )
            if track == "open" and program is not None:
                for surface in _surfaces_used(case):
                    if surface not in program.inputs:
                        unlisted_surfaces[surface] = unlisted_surfaces.get(surface, 0) + 1
            if fixed is not None:
                report.extend(_check_fixed(case, fixed))
        if bundle.last_line_unterminated:
            report.add(CASES_FILE, "cases.final_newline", "the last line of cases.jsonl has no line feed")
    except BundleError as exc:
        report.add(CASES_FILE, "cases.unreadable", str(exc))

    for surface, count in sorted(unlisted_surfaces.items()):
        report.warn(
            CASES_FILE,
            "case.unlisted_surface",
            f"{count} open-track case(s) use '{surface}', which the definition of {program_id!r} does not list in inputs",
        )
    if tool is not None and isinstance(tool.get("budget"), dict):
        wall = tool["budget"].get("wall_s")
        if is_plain_int(wall) and wall != STANDARD_BUDGET_S:
            report.warn(TOOL_FILE, "tool.budget_nonstandard", f"budget.wall_s is {wall}; the standard budget is {STANDARD_BUDGET_S}")
    extras = sorted(entry.name for entry in os.scandir(root) if entry.name not in (TOOL_FILE, CASES_FILE, BLOBS_DIR))
    if extras:
        report.warn(str(root), "bundle.extra_files", f"files besides tool.json, cases.jsonl and blobs/: {', '.join(extras[:5])}")

    if over_budget and not strict_budget:
        report.warn(CASES_FILE, "case.over_budget", f"{over_budget} case(s) have t_ms beyond the budget and will not be replayed")

    # unreferenced and stray blobs ------------------------------------------------
    stray = unreferenced = 0
    if blobs_dir.is_dir() and not blobs_dir.is_symlink():
        with os.scandir(blobs_dir) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    stray += 1
                elif not SHA256_RE.match(entry.name):
                    stray += 1
                elif entry.name not in referenced:
                    unreferenced += 1
    if stray:
        report.warn(BLOBS_DIR, "blob.stray", f"{stray} entries in blobs/ are not named by a sha256 digest")
    if unreferenced:
        report.warn(BLOBS_DIR, "blob.unreferenced", f"{unreferenced} blob(s) are not referenced by any case")

    report.stats.update(
        {
            "cases": total_cases,
            "blobs_referenced": len(referenced),
            "over_budget": over_budget,
            "program": program_id,
            "track": track,
        }
    )
    return report


STANDARD_BUDGET_S = 21_600


def _surfaces_used(case: Case) -> list[str]:
    """Input surfaces (as named in a program's ``inputs``) a case makes use of."""

    used = []
    if case.argv:
        used.append("argv")
    if case.files:
        used.append("files")
    if case.stdin is not None:
        used.append("stdin")
    if case.env:
        used.append("env")
    return used


def _ordered_blobs(case: Case) -> list[str]:
    """Blob digests in a stable order, stdin first; one entry per use."""

    return case.blob_digests()


def _check_fixed(case: Case, fixed) -> list[ValidationError]:
    """Fixed track: argv is the program's template and the only input is ``input`` or stdin."""

    errors: list[ValidationError] = []
    where = case.where

    def err(code: str, message: str) -> None:
        errors.append(ValidationError(where, code, message))

    expected = tuple(token.encode("utf-8") for token in fixed.expected_argv())
    if case.argv != expected:
        shown = [token.decode("utf-8", "replace") for token in case.argv]
        err(
            "fixed.argv",
            f"argv {shown} does not match the fixed invocation {list(fixed.expected_argv())}",
        )
    if case.env:
        err("fixed.env", "the fixed track does not allow environment variables")
    if case.cwd != b".":
        err("fixed.cwd", "the fixed track runs in the case root (cwd must be '.')")
    if case.stdin_tty:
        err("fixed.stdin", "the fixed track delivers stdin as a plain file or pipe; stdin_tty is not allowed")
    if case.stdin_pipe or case.stdin_atime is not None or case.stdin_mtime is not None:
        err("fixed.stdin", "the fixed track fixes how stdin is delivered; stdin_pipe, stdin_atime and stdin_mtime are not allowed")
    if fixed.input_channel == "file":
        if case.stdin is not None:
            err("fixed.stdin", "the fixed track delivers the input as a file; stdin must be null")
        entries = case.files
        ok = (
            len(entries) == 1
            and entries[0].kind == "file"
            and entries[0].path == FIXED_INPUT_NAME.encode()
        )
        if not ok:
            err("fixed.files", f"the fixed track needs exactly one file entry, a regular file named {FIXED_INPUT_NAME!r}")
    else:
        if case.files:
            err("fixed.files", "the fixed track delivers the input on stdin; files must be empty")
    return errors


# ---------------------------------------------------------------------------
# Other documents
# ---------------------------------------------------------------------------


def validate_branch_set_document(doc: Any, where: str = "branch-set") -> list[ValidationError]:
    errors: list[ValidationError] = []

    def err(code: str, message: str) -> None:
        errors.append(ValidationError(where, code, message))

    if not isinstance(doc, dict):
        return [ValidationError(where, "set.type", "a branch set must be a JSON object")]
    if doc.get("schema") != SCHEMA_BRANCH_SET:
        err("set.schema", f"schema must be {SCHEMA_BRANCH_SET!r}")
    if not isinstance(doc.get("program"), str) or not PROGRAM_ID_PATTERN.match(doc["program"]):
        err("set.program", "program must be a program id")
    if doc.get("branch_key_format") != BRANCH_KEY_FORMAT:
        err("set.key_format", f"branch_key_format must be {BRANCH_KEY_FORMAT!r}")
    total = doc.get("branch_total")
    covered = doc.get("branch_covered")
    keys = doc.get("covered_branch_keys")
    if not is_plain_int(total) or total < 0:
        err("set.total", "branch_total must be a non-negative integer")
    if not is_plain_int(covered) or covered < 0:
        err("set.covered", "branch_covered must be a non-negative integer")
    if not isinstance(keys, list):
        err("set.keys", "covered_branch_keys must be a list")
        return errors
    previous = None
    bad = 0
    for key in keys:
        if not isinstance(key, str) or not BRANCH_KEY_RE.match(key):
            bad += 1
            continue
        if previous is not None and key <= previous:
            err("set.order", f"keys must be sorted and unique ({key!r} follows {previous!r})")
            break
        previous = key
    if bad:
        err("set.key_format", f"{bad} key(s) are not of the form file:line:block_id:branch_id")
    if is_plain_int(covered) and covered != len(keys):
        err("set.count", f"branch_covered is {covered} but {len(keys)} keys are listed")
    if is_plain_int(total) and total and len(keys) > total:
        err("set.count", f"{len(keys)} covered keys exceed branch_total {total}")
    if "t_s" in doc and (not is_plain_int(doc["t_s"]) or doc["t_s"] < 0):
        err("set.t_s", "t_s must be a non-negative integer")
    return errors


def validate_replay_document(doc: Any, where: str = "replay.json") -> list[ValidationError]:
    errors: list[ValidationError] = []

    def err(code: str, message: str) -> None:
        errors.append(ValidationError(where, code, message))

    if not isinstance(doc, dict):
        return [ValidationError(where, "replay.type", "replay.json must be a JSON object")]
    if doc.get("schema") != SCHEMA_REPLAY:
        err("replay.schema", f"schema must be {SCHEMA_REPLAY!r}")
    for key in (
        "program",
        "tool",
        "track",
        "repeat",
        "budget_s",
        "interval_s",
        "branch_total",
        "branch_covered",
        "cases",
        "status_counts",
        "snapshots",
        "flake",
        "runner",
    ):
        if key not in doc:
            err("replay.missing_key", f"missing required key {key!r}")
    snapshots = doc.get("snapshots")
    if isinstance(snapshots, list):
        previous_t = -1
        previous_c = -1
        for item in snapshots:
            if not (
                isinstance(item, dict)
                and is_plain_int(item.get("t_s"))
                and is_plain_int(item.get("covered"))
                and is_plain_int(item.get("cases"))
            ):
                err("replay.snapshot", "each snapshot needs integer t_s, cases and covered")
                break
            if item["t_s"] <= previous_t:
                err("replay.snapshot_order", "snapshot times must increase")
                break
            if item["covered"] < previous_c:
                err("replay.snapshot_monotone", "cumulative coverage cannot decrease between snapshots")
                break
            previous_t, previous_c = item["t_s"], item["covered"]
        if (
            snapshots
            and is_plain_int(doc.get("branch_covered"))
            and isinstance(snapshots[-1], dict)
            and snapshots[-1].get("covered") != doc["branch_covered"]
        ):
            err("replay.final", "the last snapshot must equal branch_covered")
    elif "snapshots" in doc:
        err("replay.snapshot", "snapshots must be a list")
    cases = doc.get("cases")
    if isinstance(cases, dict):
        parts = [cases.get(k) for k in ("total", "replayed", "skipped_beyond_budget")]
        if not all(is_plain_int(p) and p >= 0 for p in parts):
            err("replay.cases", "cases needs non-negative integers total, replayed and skipped_beyond_budget")
        elif parts[0] != parts[1] + parts[2]:
            err("replay.cases", "cases.total must equal replayed plus skipped_beyond_budget")
    flake = doc.get("flake")
    if isinstance(flake, dict):
        if not isinstance(flake.get("checked"), bool):
            err("replay.flake", "flake.checked must be a boolean")
        elif flake["checked"] and not is_plain_int(flake.get("branches_differing")):
            err("replay.flake", "a checked flake record needs branches_differing")
    if "profile" in doc and not (isinstance(doc["profile"], str) and doc["profile"]):
        err("replay.profile", "profile must be a non-empty string")
    if "stray_killed" in doc and (not is_plain_int(doc["stray_killed"]) or doc["stray_killed"] < 0):
        err("replay.stray_killed", "stray_killed must be a non-negative integer")
    if "timeouts" in doc:
        _check_timeouts(doc["timeouts"], doc.get("status_counts"), err)
    if "local_zone" in doc:
        _check_local_zone(doc["local_zone"], err)
    if "warnings" in doc and not (isinstance(doc["warnings"], list) and all(isinstance(w, str) for w in doc["warnings"])):
        err("replay.warnings", "warnings must be a list of strings")
    build = doc.get("build")
    if "build" in doc:
        if not isinstance(build, dict):
            err("replay.build", "build must be an object")
        else:
            for key in ("program_json_sha256", "binary_sha256", "gcno_set_sha256"):
                value = build.get(key)
                if not isinstance(value, str) or not SHA256_RE.match(value):
                    err("replay.build", f"build.{key} must be 64 lowercase hex digits")
    return errors


def _check_timeouts(timeouts: Any, status_counts: Any, err: Any) -> None:
    """The ``timeouts`` member of ``replay.json`` (SPEC.md section 7.1)."""

    if not isinstance(timeouts, dict):
        err("replay.timeouts", "timeouts must be an object")
        return
    if timeouts.get("clock") not in ("wall", "cpu"):
        err("replay.timeouts", "timeouts.clock must be 'wall' or 'cpu'")
    numbers = [timeouts.get(key) for key in ("count", "cpu_measured", "under_load")]
    if not all(is_plain_int(n) and n >= 0 for n in numbers):
        err("replay.timeouts", "timeouts needs non-negative integers count, cpu_measured and under_load")
    elif not numbers[2] <= numbers[1] <= numbers[0]:
        err("replay.timeouts", "timeouts must satisfy under_load <= cpu_measured <= count")
    elif isinstance(status_counts, dict) and is_plain_int(status_counts.get("timeout")) and numbers[0] != status_counts["timeout"]:
        err("replay.timeouts", "timeouts.count must equal status_counts.timeout")
    ids = timeouts.get("under_load_case_ids")
    if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
        err("replay.timeouts", "timeouts.under_load_case_ids must be a list of strings")
    elif len(ids) > 100 or (is_plain_int(numbers[2]) and len(ids) > numbers[2]):
        err("replay.timeouts", "timeouts.under_load_case_ids lists at most 100 ids, and no more than under_load")


def _check_local_zone(zone: Any, err: Any) -> None:
    """The ``local_zone`` member of ``replay.json`` (SPEC.md section 7.1)."""

    if not isinstance(zone, dict):
        err("replay.local_zone", "local_zone must be an object")
        return
    offset = zone.get("offset_s")
    if offset is not None and not is_plain_int(offset):
        err("replay.local_zone", "local_zone.offset_s must be an integer or null")
    for key in ("abbreviation", "localtime_link", "tz_env"):
        if zone.get(key) is not None and not isinstance(zone[key], str):
            err("replay.local_zone", f"local_zone.{key} must be a string or null")
    digest = zone.get("localtime_sha256")
    if digest is not None and not (isinstance(digest, str) and SHA256_RE.match(digest)):
        err("replay.local_zone", "local_zone.localtime_sha256 must be 64 lowercase hex digits or null")
    if zone.get("utc") is not None and not isinstance(zone["utc"], bool):
        err("replay.local_zone", "local_zone.utc must be a boolean or null")


_SIGNATURE_STATUS_KEYS = ("ok", "nonzero_exit", "terminated_by_signal", "timeout", "setup_error")


def validate_signature_counts_document(doc: Any, where: str = "signatures-counts.json") -> list[ValidationError]:
    """The public output of the sanitiser pass: counts by sanitiser and kind, nothing else of the reports.

    Every kind must be one of the names of :mod:`agent_software_testing.kinds`, so a
    document that contains text a program chose (SPEC.md section 8) is invalid.
    """

    errors: list[ValidationError] = []

    def err(code: str, message: str) -> None:
        errors.append(ValidationError(where, code, message))

    if not isinstance(doc, dict):
        return [ValidationError(where, "counts.type", "signatures-counts.json must be a JSON object")]
    if doc.get("schema") != SCHEMA_SIGNATURE_COUNTS:
        err("counts.schema", f"schema must be {SCHEMA_SIGNATURE_COUNTS!r}")
    for key in ("schema", "program", "tool", "repeat", "signatures", "by_sanitizer_and_kind", "cases", "status_counts"):
        if key not in doc:
            err("counts.missing_key", f"missing required key {key!r}")
    if "program" in doc and (not isinstance(doc["program"], str) or not PROGRAM_ID_PATTERN.match(doc["program"])):
        err("counts.program", "program must be a program id")
    if "tool" in doc and not isinstance(doc["tool"], dict):
        err("counts.tool", "tool must be an object")
    if "repeat" in doc and (not is_plain_int(doc["repeat"]) or doc["repeat"] < 1):
        err("counts.repeat", "repeat must be an integer of at least 1")
    total = doc.get("signatures")
    if "signatures" in doc and (not is_plain_int(total) or total < 0):
        err("counts.signatures", "signatures must be a non-negative integer")
    by_kind = doc.get("by_sanitizer_and_kind")
    if "by_sanitizer_and_kind" in doc:
        if not isinstance(by_kind, dict):
            err("counts.by_kind", "by_sanitizer_and_kind must be an object")
        else:
            counted = 0
            for sanitizer, kinds in by_kind.items():
                if sanitizer not in KINDS_BY_SANITIZER:
                    err("counts.sanitizer", f"unknown sanitiser {printable(sanitizer, 40)!r}")
                    continue
                if not isinstance(kinds, dict):
                    err("counts.by_kind", f"{sanitizer} must map kinds to counts")
                    continue
                for kind, count in kinds.items():
                    if not isinstance(kind, str) or len(kind) > MAX_KIND_CHARS or kind not in KINDS_BY_SANITIZER[sanitizer]:
                        err("counts.kind", f"kind {printable(kind, 80)!r} is not a known {sanitizer} kind")
                    if not is_plain_int(count) or count < 1:
                        err("counts.count", f"count of {printable(kind, 80)!r} must be an integer of at least 1")
                    else:
                        counted += count
            if is_plain_int(total) and counted != total:
                err("counts.total", f"signatures is {total} but the counts add up to {counted}")
    cases = doc.get("cases")
    if "cases" in doc:
        if not isinstance(cases, dict) or not all(
            is_plain_int(cases.get(k)) and cases[k] >= 0 for k in ("replayed", "with_reports", "skipped_beyond_budget")
        ):
            err("counts.cases", "cases needs non-negative integers replayed, with_reports and skipped_beyond_budget")
    status = doc.get("status_counts")
    if "status_counts" in doc:
        if not isinstance(status, dict) or not all(
            is_plain_int(status.get(k)) and status[k] >= 0 for k in _SIGNATURE_STATUS_KEYS
        ):
            err("counts.status", "status_counts needs a non-negative integer for each of the five statuses")
    return errors


def validate_document(path: Path | str) -> ValidationReport:
    """Validate any single document or a bundle directory, detected by content."""

    target = Path(path)
    if target.is_dir():
        if (target / TOOL_FILE).exists() or (target / CASES_FILE).exists():
            return validate_bundle(target)
        report = ValidationReport()
        report.add(str(target), "document.unknown", "directory is not a bundle (no tool.json or cases.jsonl)")
        return report
    report = ValidationReport()
    try:
        document = read_json_file(target)
    except (OSError, AgentSoftwareTestingError) as exc:
        report.add(str(target), "document.unreadable", str(exc))
        return report
    schema = document.get("schema") if isinstance(document, dict) else None
    where = str(target)
    if schema == SCHEMA_TOOL:
        report.extend(validate_tool_document(document, where))
    elif schema == SCHEMA_PROGRAM:
        report.extend(validate_program_document(document, where))
    elif schema == SCHEMA_BRANCH_SET:
        report.extend(validate_branch_set_document(document, where))
    elif schema == SCHEMA_REPLAY:
        report.extend(validate_replay_document(document, where))
    elif schema == SCHEMA_SIGNATURE_COUNTS:
        report.extend(validate_signature_counts_document(document, where))
    else:
        report.add(where, "document.unknown", f"unknown or missing schema {schema!r}")
    return report
