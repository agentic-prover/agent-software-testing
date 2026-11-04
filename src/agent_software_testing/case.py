# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""One test case: parsing and context-free checks (contract section 2).

``parse_case`` decodes one line of ``cases.jsonl`` and applies every rule that
needs nothing but the line itself: field types, base64, path rules, limits on
counts and sizes of argv, files and environment, duplicate and conflicting
paths, and symlink targets that must stay inside the case root. Rules that
need the bundle (blob presence and hashes, unique ids, order, budget, the
fixed-track template) are applied in :mod:`agent_software_testing.validate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

from .common import (
    BLOB_REF_RE,
    DEFAULT_TIMEOUT_MS,
    ENV_NAME_RE,
    FILE_KINDS,
    ID_RE,
    MAX_ARGV_BYTES,
    MAX_ARGV_ENTRIES,
    MAX_ENV_VARS,
    MAX_EXEC_STRING_BYTES,
    MAX_EXEC_TOTAL_BYTES,
    MAX_FILES_PER_CASE,
    MAX_ID_CHARS,
    MAX_ORIGIN_CHARS,
    MAX_PATH_BYTES,
    MAX_TIMEOUT_MS,
    ValidationError,
    b64decode_strict,
    env_name_forbidden,
    value_has_credentials,
    is_plain_int,
    parse_mode,
    path_problems,
    split_path,
)

CASE_KEYS = frozenset(
    {
        "id",
        "t_ms",
        "argv",
        "stdin",
        "stdin_tty",
        "stdin_pipe",
        "stdin_atime",
        "stdin_mtime",
        "files",
        "env",
        "cwd",
        "timeout_ms",
        "origin",
    }
)
REQUIRED_CASE_KEYS = ("id", "t_ms", "argv")
FILE_ENTRY_KEYS = {
    "file": {"path", "kind", "mode", "blob", "atime", "mtime"},
    "fifo": {"path", "kind", "mode", "blob"},
    "tty": {"path", "kind", "blob"},
    "dir": {"path", "kind", "mode"},
    "symlink": {"path", "kind", "target"},
}
DEFAULT_FILE_MODE = 0o644
DEFAULT_DIR_MODE = 0o755
MAX_SYMLINK_HOPS = 40
@dataclass(frozen=True)
class FileEntry:
    path: bytes
    kind: str  # file | dir | symlink | fifo | tty
    mode: int  # permission bits; 0 for symlinks (unused)
    blob: str | None  # sha256 hex of the body (file, fifo, tty)
    target: bytes | None  # symlink target
    #: Access and modification time of a regular file, seconds since the Unix epoch, ``None`` for "as created".
    atime: int | None = None
    mtime: int | None = None

    @property
    def components(self) -> tuple[bytes, ...]:
        return tuple(split_path(self.path))


@dataclass(frozen=True)
class Case:
    id: str
    t_ms: int
    argv: tuple[bytes, ...]  # without argv[0]
    stdin: str | None  # sha256 hex or None
    files: tuple[FileEntry, ...]
    env: tuple[tuple[str, bytes], ...]
    cwd: bytes  # relative path inside the case root, b"." for the root
    timeout_ms: int
    origin: str | None
    line: int = 0
    #: Standard input is a pseudo-terminal that the runner feeds with the ``stdin`` blob (SPEC.md 5.5, 6.3).
    stdin_tty: bool = False
    #: Standard input is an anonymous pipe that the runner feeds with the ``stdin`` blob and then closes (SPEC.md 5.4, 6.3).
    stdin_pipe: bool = False
    #: Access and modification time of the standard input file, seconds since the Unix epoch, ``None`` for "as created".
    stdin_atime: int | None = None
    stdin_mtime: int | None = None

    @property
    def where(self) -> str:
        return f"cases.jsonl:{self.line} (id {self.id})"

    def blob_digests(self) -> list[str]:
        digests = [self.stdin] if self.stdin else []
        digests.extend(entry.blob for entry in self.files if entry.blob)
        return digests


def _where(line: int, case_id: Any) -> str:
    return f"cases.jsonl:{line}" + (f" (id {case_id})" if isinstance(case_id, str) else "")


def parse_case(raw: Any, line: int) -> tuple[Case | None, list[ValidationError]]:
    """Parse and check one case object. Returns ``(case, errors)``.

    ``case`` is ``None`` when the structure is too broken to build one; when
    only rule violations were found a case is still returned so that later
    checks (blobs, order) can report on it too.
    """

    errors: list[ValidationError] = []
    if not isinstance(raw, dict):
        return None, [ValidationError(f"cases.jsonl:{line}", "case.type", "a case must be a JSON object")]
    where = _where(line, raw.get("id"))

    def err(code: str, message: str, field: str = "") -> None:
        errors.append(ValidationError(f"{where} {field}".rstrip(), code, message))

    for key in sorted(set(raw) - CASE_KEYS):
        err("case.unknown_key", f"unknown key {key!r}", key)
    for key in REQUIRED_CASE_KEYS:
        if key not in raw:
            err("case.missing_key", f"missing required key {key!r}", key)

    # id ---------------------------------------------------------------
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not ID_RE.match(case_id) or len(case_id) > MAX_ID_CHARS:
        err(
            "case.id",
            f"id must be 1 to {MAX_ID_CHARS} characters from [A-Za-z0-9._-], starting with a letter or digit",
            "id",
        )
        case_id = case_id if isinstance(case_id, str) else "?"

    # t_ms -------------------------------------------------------------
    t_ms = raw.get("t_ms")
    if not is_plain_int(t_ms) or t_ms < 0:
        err("case.t_ms", "t_ms must be a non-negative integer", "t_ms")
        t_ms = 0

    # argv -------------------------------------------------------------
    argv: list[bytes] = []
    raw_argv = raw.get("argv", [])
    if not isinstance(raw_argv, list):
        err("argv.type", "argv must be a list of base64 strings", "argv")
    else:
        if len(raw_argv) > MAX_ARGV_ENTRIES:
            err("argv.too_many", f"argv has {len(raw_argv)} entries, limit is {MAX_ARGV_ENTRIES}", "argv")
            raw_argv = raw_argv[:MAX_ARGV_ENTRIES]  # report once; do not spend time on the rest
        for index, item in enumerate(raw_argv):
            try:
                value = b64decode_strict(item)
            except ValueError as exc:
                err("argv.base64", str(exc), f"argv[{index}]")
                continue
            if b"\0" in value:
                err("argv.nul", "argument contains a NUL byte", f"argv[{index}]")
            if len(value) > MAX_EXEC_STRING_BYTES:
                err(
                    "argv.arg_too_large",
                    f"argument is {len(value)} bytes, limit is {MAX_EXEC_STRING_BYTES}",
                    f"argv[{index}]",
                )
            argv.append(value)
        total = sum(len(a) for a in argv)
        if total > MAX_ARGV_BYTES:
            err("argv.too_large", f"argv is {total} bytes, limit is {MAX_ARGV_BYTES}", "argv")

    # stdin ------------------------------------------------------------
    stdin: str | None = None
    if "stdin" in raw and raw["stdin"] is not None:
        match = BLOB_REF_RE.match(raw["stdin"]) if isinstance(raw["stdin"], str) else None
        if match is None:
            err("stdin.ref", "stdin must be null or 'sha256:<64 hex digits>'", "stdin")
        else:
            stdin = match.group(1)

    stdin_tty = False
    if "stdin_tty" in raw:
        if not isinstance(raw["stdin_tty"], bool):
            err("stdin.tty_type", "stdin_tty must be true or false", "stdin_tty")
        elif raw["stdin_tty"]:
            stdin_tty = True
            if stdin is None:
                err("stdin.tty_without_input", "stdin_tty needs stdin to be a blob (an empty blob is allowed)", "stdin_tty")

    stdin_pipe = False
    if "stdin_pipe" in raw:
        if not isinstance(raw["stdin_pipe"], bool):
            err("stdin.pipe_type", "stdin_pipe must be true or false", "stdin_pipe")
        elif raw["stdin_pipe"]:
            stdin_pipe = True
            if stdin is None:
                err("stdin.pipe_without_input", "stdin_pipe needs stdin to be a blob (an empty blob is allowed)", "stdin_pipe")
            if stdin_tty:
                err("stdin.pipe_and_tty", "stdin_pipe and stdin_tty cannot both be true", "stdin_pipe")

    stdin_times: dict[str, int | None] = {"stdin_atime": None, "stdin_mtime": None}
    for key in stdin_times:
        if key not in raw:
            continue
        value = raw[key]
        if not is_plain_int(value) or not -(2**63) <= value < 2**63:
            err("stdin.time", f"{key} must be an integer number of seconds from -2^63 to 2^63 - 1", key)
            continue
        stdin_times[key] = value
        if stdin is None:
            err("stdin.times_without_input", f"{key} needs stdin to be a blob (an empty blob is allowed)", key)
        elif stdin_tty or stdin_pipe:
            err("stdin.times_not_a_file", f"{key} applies to a standard input that is a file, not to a pipe or a terminal", key)

    # files ------------------------------------------------------------
    files: list[FileEntry] = []
    raw_files = raw.get("files", [])
    if not isinstance(raw_files, list):
        err("files.type", "files must be a list", "files")
        raw_files = []
    if len(raw_files) > MAX_FILES_PER_CASE:
        err("files.too_many", f"{len(raw_files)} files, limit is {MAX_FILES_PER_CASE}", "files")
        raw_files = raw_files[:MAX_FILES_PER_CASE]  # report once; do not spend time on the rest
    for index, item in enumerate(raw_files):
        entry = _parse_file_entry(item, index, err)
        if entry is not None:
            files.append(entry)

    # env --------------------------------------------------------------
    env: list[tuple[str, bytes]] = []
    raw_env = raw.get("env", {})
    if not isinstance(raw_env, dict):
        err("env.type", "env must be an object of name to base64 value", "env")
    else:
        env_items = raw_env.items()
        if len(raw_env) > MAX_ENV_VARS:
            err("env.too_many", f"{len(raw_env)} variables, limit is {MAX_ENV_VARS}", "env")
            env_items = islice(env_items, MAX_ENV_VARS)  # report once; do not spend time on the rest
        for name, value in env_items:
            if not isinstance(name, str) or not ENV_NAME_RE.match(name):
                err("env.name", f"invalid variable name {name!r}", "env")
                continue
            if env_name_forbidden(name):
                err("env.forbidden", f"{name} cannot be set by a case", f"env.{name}")
                continue
            try:
                decoded = b64decode_strict(value)
            except ValueError as exc:
                err("env.base64", str(exc), f"env.{name}")
                continue
            if b"\0" in decoded:
                err("env.nul", "value contains a NUL byte", f"env.{name}")
                continue
            if value_has_credentials(decoded):
                err("env.credentials", "value holds a URL with a user name and password", f"env.{name}")
                continue
            if len(name) + 1 + len(decoded) > MAX_EXEC_STRING_BYTES:
                err(
                    "env.too_large",
                    f"{name}=value is {len(name) + 1 + len(decoded)} bytes, limit is {MAX_EXEC_STRING_BYTES}",
                    f"env.{name}",
                )
            env.append((name, decoded))
    exec_bytes = sum(len(a) + 1 for a in argv) + sum(len(n) + 1 + len(v) + 1 for n, v in env)
    if exec_bytes > MAX_EXEC_TOTAL_BYTES:
        err(
            "exec.too_large",
            f"argv and env together are {exec_bytes} bytes (each string with its terminator), "
            f"limit is {MAX_EXEC_TOTAL_BYTES}",
            "env",
        )

    # cwd --------------------------------------------------------------
    cwd = b"."
    if "cwd" in raw:
        try:
            cwd = b64decode_strict(raw["cwd"])
        except ValueError as exc:
            err("cwd.base64", str(exc), "cwd")
        else:
            for code, message in path_problems(cwd, allow_dot=True):
                err(code, f"cwd: {message}", "cwd")

    # timeout ----------------------------------------------------------
    timeout_ms = raw.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if not is_plain_int(timeout_ms) or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        err("timeout.range", f"timeout_ms must be an integer from 1 to {MAX_TIMEOUT_MS}", "timeout_ms")
        timeout_ms = DEFAULT_TIMEOUT_MS

    # origin -----------------------------------------------------------
    origin = raw.get("origin")
    if origin is not None and (not isinstance(origin, str) or len(origin) > MAX_ORIGIN_CHARS):
        err("case.origin", f"origin must be null or a string of at most {MAX_ORIGIN_CHARS} characters", "origin")
        origin = None

    case = Case(
        id=case_id,
        t_ms=t_ms,
        argv=tuple(argv),
        stdin=stdin,
        files=tuple(files),
        env=tuple(env),
        cwd=cwd,
        timeout_ms=timeout_ms,
        origin=origin,
        line=line,
        stdin_tty=stdin_tty,
        stdin_pipe=stdin_pipe,
        stdin_atime=stdin_times["stdin_atime"],
        stdin_mtime=stdin_times["stdin_mtime"],
    )
    errors.extend(check_tree(case, where))
    return case, errors


def _parse_file_entry(item: Any, index: int, err) -> FileEntry | None:
    field = f"files[{index}]"
    if not isinstance(item, dict):
        err("file.type", "a file entry must be an object", field)
        return None
    kind = item.get("kind")
    if kind not in FILE_KINDS:
        err("file.kind", f"kind must be one of {list(FILE_KINDS)}", f"{field}.kind")
        return None
    allowed = FILE_ENTRY_KEYS[kind]
    for key in sorted(set(item) - allowed):
        err("file.unknown_key", f"key {key!r} is not allowed for kind {kind!r}", f"{field}.{key}")
    if "path" not in item:
        err("file.missing_key", "missing key 'path'", field)
        return None
    try:
        path = b64decode_strict(item["path"])
    except ValueError as exc:
        err("file.path_base64", str(exc), f"{field}.path")
        return None
    for code, message in path_problems(path):
        err(code, message, f"{field}.path")

    mode = {"dir": DEFAULT_DIR_MODE, "symlink": 0, "tty": 0}.get(kind, DEFAULT_FILE_MODE)
    if kind not in ("symlink", "tty") and "mode" in item:
        try:
            mode = parse_mode(item["mode"])
        except ValueError as exc:
            err("file.mode", str(exc), f"{field}.mode")

    blob: str | None = None
    if kind in ("file", "fifo", "tty"):
        match = BLOB_REF_RE.match(item["blob"]) if isinstance(item.get("blob"), str) else None
        if match is None:
            err("file.blob_ref", f"a {kind} entry needs blob 'sha256:<64 hex digits>'", f"{field}.blob")
        else:
            blob = match.group(1)

    target: bytes | None = None
    if kind == "symlink":
        if "target" not in item:
            err("file.missing_key", "a symlink entry needs 'target'", field)
        else:
            try:
                target = b64decode_strict(item["target"])
            except ValueError as exc:
                err("file.target_base64", str(exc), f"{field}.target")
            else:
                if not target or b"\0" in target:
                    err("symlink.target", "symlink target is empty or contains NUL", f"{field}.target")
                    target = None
                elif len(target) > MAX_PATH_BYTES:
                    err("symlink.target", f"symlink target longer than {MAX_PATH_BYTES} bytes", f"{field}.target")
                    target = None
    times: dict[str, int | None] = {"atime": None, "mtime": None}
    for key in times:
        if key in item:
            value = item[key]
            if not is_plain_int(value) or not -(2**63) <= value < 2**63:
                err("file.time", f"{key} must be an integer number of seconds from -2^63 to 2^63 - 1", f"{field}.{key}")
            else:
                times[key] = value
    return FileEntry(path=path, kind=kind, mode=mode, blob=blob, target=target, atime=times["atime"], mtime=times["mtime"])


# ---------------------------------------------------------------------------
# The virtual tree a case creates, and the checks on it
# ---------------------------------------------------------------------------


def _is_clean_path(path: bytes) -> bool:
    return not path_problems(path)


def check_tree(case: Case, where: str | None = None) -> list[ValidationError]:
    """Check duplicates, parent conflicts, ``cwd`` and symlink targets.

    The work is linear in the number of entries, which is limited first: a case
    with more than 64 entries is reported and not examined further.
    """

    where = where or case.where
    errors: list[ValidationError] = []

    def err(code: str, message: str, field: str = "") -> None:
        errors.append(ValidationError(f"{where} {field}".rstrip(), code, message))

    if len(case.files) > MAX_FILES_PER_CASE:
        err("files.too_many", f"{len(case.files)} files, limit is {MAX_FILES_PER_CASE}", "files")
        return errors

    nodes: dict[tuple[bytes, ...], str] = {(): "dir"}  # components -> kind
    links: dict[tuple[bytes, ...], bytes] = {}
    explicit_dirs: dict[tuple[bytes, ...], int] = {}  # directories listed by a dir entry -> mode
    for index, entry in enumerate(case.files):
        if not _is_clean_path(entry.path):
            continue  # already reported; cannot place it in the tree
        comps = entry.components
        field = f"files[{index}].path"
        ok = True
        for depth in range(1, len(comps)):
            parent = comps[:depth]
            existing = nodes.get(parent)
            if existing is None:
                nodes[parent] = "dir"
            elif existing != "dir":
                err(
                    "path.conflict",
                    f"{b'/'.join(parent)!r} is a {existing}, but another entry places something below it",
                    field,
                )
                ok = False
                break
        if not ok:
            continue
        existing = nodes.get(comps)
        if existing is not None:
            if entry.kind == "dir" and existing == "dir":
                # an explicit dir entry for a directory implied earlier is fine
                # unless it was itself listed twice
                if comps in explicit_dirs:
                    err("path.duplicate", f"path {entry.path!r} is listed twice", field)
                else:
                    explicit_dirs[comps] = entry.mode
            else:
                err(
                    "path.duplicate",
                    f"path {entry.path!r} is listed twice or is also used as a parent directory",
                    field,
                )
            continue
        nodes[comps] = entry.kind
        if entry.kind == "dir":
            explicit_dirs[comps] = entry.mode
        elif entry.kind == "symlink" and entry.target is not None:
            links[comps] = entry.target

    # cwd must be the root or a real directory of the case that the process can enter
    if _is_clean_path_or_dot(case.cwd):
        if case.cwd != b".":
            cwd_comps = tuple(split_path(case.cwd))
            if nodes.get(cwd_comps) != "dir":
                err("cwd.missing", f"cwd {case.cwd!r} is not a directory the case creates", "cwd")
            else:
                for depth in range(1, len(cwd_comps) + 1):
                    # implied directories are created with mode 0700; the owner's
                    # search bit decides whether the process can enter
                    if not explicit_dirs.get(cwd_comps[:depth], 0o700) & 0o100:
                        err(
                            "cwd.not_searchable",
                            f"cwd {case.cwd!r} cannot be entered: {b'/'.join(cwd_comps[:depth])!r} has no search permission",
                            "cwd",
                        )
                        break

    # symlink targets must stay inside the root
    for comps, target in sorted(links.items()):
        problem = _resolve_inside(nodes, links, comps[:-1], target)
        if problem is not None:
            err("symlink.escape", f"symlink {b'/'.join(comps)!r} -> {target!r}: {problem}", "files")
    return errors


def _is_clean_path_or_dot(path: bytes) -> bool:
    return not path_problems(path, allow_dot=True)


def _resolve_inside(
    nodes: dict[tuple[bytes, ...], str],
    links: dict[tuple[bytes, ...], bytes],
    start: tuple[bytes, ...],
    target: bytes,
) -> str | None:
    """Walk ``target`` from directory ``start`` and report how it leaves the root.

    Follows symlinks the case itself creates (so ``a/../..`` through a link is
    resolved the way the kernel would). Components that do not exist are walked
    as plain directories, which is what a program that creates them at run
    time would get. A loop of links cannot be resolved by the kernel and so
    reaches nothing. Returns ``None`` when the target stays inside the root.
    """

    if target.startswith(b"/"):
        return "target is absolute"
    pending = [list(split_path(target))]
    current = list(start)
    hops = 0
    while pending:
        remaining = pending[-1]
        if not remaining:
            pending.pop()
            continue
        part = remaining.pop(0)
        if part in (b"", b"."):
            continue
        if part == b"..":
            if not current:
                return "target climbs above the case root"
            current.pop()
            continue
        probe = tuple(current) + (part,)
        kind = nodes.get(probe)
        if kind == "symlink":
            hops += 1
            if hops > MAX_SYMLINK_HOPS:
                return None  # a loop: the kernel answers ELOOP, so nothing is reached
            link_target = links.get(probe)
            if link_target is None:
                return None
            if link_target.startswith(b"/"):
                return "a link on the way has an absolute target"
            pending.append(list(split_path(link_target)))
            continue
        current.append(part)
    return None
