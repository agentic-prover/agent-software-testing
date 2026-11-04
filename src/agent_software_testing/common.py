# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Constants, error types and small helpers shared by every module."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import FORMAT_VERSION

# ---------------------------------------------------------------------------
# Format identifiers (contract sections 1 to 4)
# ---------------------------------------------------------------------------

SCHEMA_TOOL = f"agent-software-testing/tool@{FORMAT_VERSION}"
SCHEMA_PROGRAM = f"agent-software-testing/program@{FORMAT_VERSION}"
SCHEMA_BRANCH_SET = f"agent-software-testing/branch-set@{FORMAT_VERSION}"
SCHEMA_REPLAY = f"agent-software-testing/replay@{FORMAT_VERSION}"
SCHEMA_SIGNATURE_COUNTS = f"agent-software-testing/signature-counts@{FORMAT_VERSION}"
SCHEMA_SIGNATURES_PRIVATE = f"agent-software-testing/signatures-private@{FORMAT_VERSION}"

BRANCH_KEY_FORMAT = "file:line:block_id:branch_id"

TOOL_FAMILIES = ("symbolic", "fuzzer", "hybrid", "llm", "agent", "other")
TRACKS = ("fixed", "open")
FILE_KINDS = ("file", "dir", "symlink", "fifo", "tty")
PROGRAM_INPUTS = ("argv", "files", "stdin", "env")
INPUT_CHANNELS = ("file", "stdin")

# ---------------------------------------------------------------------------
# Limits of bundle format version 0.1 (contract section 2)
# ---------------------------------------------------------------------------

MAX_FILES_PER_CASE = 64
MAX_PATH_COMPONENTS = 64
MAX_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 1024
MAX_BLOB_BYTES = 32 * 1024 * 1024
MAX_CASE_BYTES = 64 * 1024 * 1024
MAX_ARGV_ENTRIES = 256
MAX_ARGV_BYTES = 128 * 1024
#: One argument, and one ``NAME=value`` environment string, may not be longer
#: than the kernel's limit for a single string (``MAX_ARG_STRLEN`` on Linux is
#: 131072 including the terminating NUL).
MAX_EXEC_STRING_BYTES = 128 * 1024 - 1
#: All arguments and all ``NAME=value`` strings of the case together, each
#: counted with its terminating NUL. Well below the usual ``ARG_MAX`` of 2 MiB,
#: which also has to hold the runner's own variables.
MAX_EXEC_TOTAL_BYTES = 1024 * 1024
MAX_ENV_VARS = 64
MAX_TIMEOUT_MS = 30_000
DEFAULT_TIMEOUT_MS = 30_000
MAX_CASES = 1_000_000
MAX_ID_CHARS = 64
MAX_ORIGIN_CHARS = 64
#: One line of ``cases.jsonl``. A valid case needs well under 3 MiB of JSON
#: (its bodies are in ``blobs/``), so a longer line cannot be a valid case.
MAX_CASE_LINE_BYTES = 8 * 1024 * 1024
#: Largest ``budget.wall_s`` (seven days) and largest number of snapshot
#: intervals (``wall_s`` divided by the interval) of a replay.
MAX_WALL_S = 604_800
MAX_SNAPSHOT_INTERVALS = 1000
#: Digits accepted in a JSON number (a longer one is hostile, not a valid value).
MAX_JSON_NUMBER_CHARS = 40

#: Environment variable names a case may not set (exact names). ``LANG``, ``LC_ALL`` and ``TZ`` are not among them
#: (SPEC.md section 5.7): a case may set them, and its value then stands (the base locale is ``LANG=C``).
FORBIDDEN_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "GLIBC_TUNABLES",
        "GCONV_PATH",
        "LOCPATH",
        "NLSPATH",
        "MSAN_OPTIONS",
        "TSAN_OPTIONS",
    }
)
#: Environment variable name prefixes a case may not use. The contract names
#: GCOV_, ASAN_ and UBSAN_; the others are additions (SPEC.md section 5.7): the
#: dynamic loader, allocator and locale-loading variables would change or
#: replace code in the measured program, the leak sanitiser options would
#: change what a run reports, and AGENT_SOFTWARE_TESTING_ is the runner's own namespace
#: (it marks the processes of a case, SPEC.md section 6.3).
FORBIDDEN_ENV_PREFIXES = ("GCOV_", "ASAN_", "UBSAN_", "LSAN_", "LD_", "MALLOC_", "AGENT_SOFTWARE_TESTING_")

#: The variable that marks every process of one case (SPEC.md section 6.3).
CASE_MARKER_ENV = "AGENT_SOFTWARE_TESTING_CASE"

#: The fixed base environment of every case, except for HOME and TMPDIR, which
#: point into the case directory. ``LANG`` and ``TZ`` are the defaults of a case that does not set them.
BASE_PATH = "/usr/bin:/bin"
BASE_LOCALE = "C"
BASE_TZ = "UTC"

#: The file name the fixed track uses for the single input file.
FIXED_INPUT_NAME = "input"
FIXED_INPUT_PLACEHOLDER = "{input}"

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
BLOB_REF_RE = re.compile(r"sha256:([0-9a-f]{64})\Z")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PROGRAM_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MODE_RE = re.compile(r"0?[0-7]{3}\Z")


class AgentSoftwareTestingError(Exception):
    """Base class for errors raised on purpose by this package."""


class NotRegularFileError(AgentSoftwareTestingError):
    """A path that has to be a regular file is a pipe, device, directory or similar."""


class IntegrityError(AgentSoftwareTestingError):
    """Something the runner already checked has changed (a blob or a bundle file)."""


_UNSAFE_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_UNSAFE_CHARS_NO_NEWLINE = re.compile("[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def printable(value: object, limit: int = 400, *, keep_newlines: bool = False) -> str:
    """Text that is safe to print: no lone surrogates, no control characters.

    Names, ids and messages can come from a hostile bundle. A lone surrogate
    makes ``print`` raise ``UnicodeEncodeError`` and a terminal escape
    sequence can rewrite what a person reads, so both are written as
    ``\\uXXXX`` / ``\\xXX``. Long texts are cut at ``limit`` characters.
    """

    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        text = f"{text[:limit]}... ({len(text)} characters)"
    pattern = _UNSAFE_CHARS if keep_newlines else _UNSAFE_CHARS_NO_NEWLINE

    def escape(match: "re.Match[str]") -> str:
        code = ord(match.group())
        return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"

    return pattern.sub(escape, text)


@dataclass(frozen=True)
class ValidationError:
    """One precise validation problem.

    ``where`` names the place (file, line, case id, field), ``code`` is a short
    stable identifier that tests and tools can match on, ``message`` is for
    people.
    """

    where: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{printable(self.where)}: [{self.code}] {printable(self.message, 800)}"

    def to_json(self) -> dict[str, str]:
        return {"where": self.where, "code": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# Hashing, base64 and JSON helpers
# ---------------------------------------------------------------------------


def default_workers() -> int:
    """Cores available to this process (respects cpusets and taskset on Linux)."""

    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except AttributeError:
        return max(1, os.cpu_count() or 1)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def open_regular_file(path: Path | str) -> int:
    """Open ``path`` read-only and return the descriptor of a regular file.

    The file is opened with ``O_NOFOLLOW`` (a final symbolic link is refused)
    and ``O_NONBLOCK`` (opening a named pipe with no writer returns at once
    instead of waiting for one), and only then examined: anything that is not
    a regular file is refused with :class:`NotRegularFileError` before a byte
    is read. Raises ``OSError`` when the file cannot be opened.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFileError(f"not a regular file: {printable(os.fspath(path))}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def sha256_file(path: Path | str) -> str:
    """Hash a regular file without following a final symlink."""

    digest = hashlib.sha256()
    fd = open_regular_file(path)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def read_regular_bytes(path: Path | str, *, max_bytes: int) -> bytes:
    """The content of a regular file of at most ``max_bytes`` bytes."""

    fd = open_regular_file(path)
    try:
        info = os.fstat(fd)
        if info.st_size > max_bytes:
            raise AgentSoftwareTestingError(f"file larger than {max_bytes} bytes: {printable(os.fspath(path))}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise AgentSoftwareTestingError(f"file larger than {max_bytes} bytes: {printable(os.fspath(path))}")
    return data


def b64decode_strict(text: str) -> bytes:
    """Decode canonical base64 or raise ``ValueError``.

    Rejects characters outside the alphabet, wrong padding and non-canonical
    encodings (non-zero trailing bits), so that every byte string has exactly
    one text form.
    """

    if not isinstance(text, str):
        raise ValueError("base64 value must be a string")
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("base64 value must be ASCII") from exc
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64: {exc}") from exc
    if base64.b64encode(decoded) != raw:
        raise ValueError("base64 value is not in canonical form")
    return decoded


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def is_plain_int(value: Any) -> bool:
    """True for an ``int`` that is not a ``bool``."""

    return isinstance(value, int) and not isinstance(value, bool)


def dumps_json(obj: Any, *, indent: int | None = 2) -> str:
    """Deterministic JSON text (keys keep insertion order, ASCII only)."""

    return json.dumps(obj, indent=indent, ensure_ascii=True) + "\n"


def write_json(path: Path | str, obj: Any, *, indent: int | None = 2) -> None:
    """Write JSON atomically next to the target and then rename."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial-{os.getpid()}")
    partial.write_text(dumps_json(obj, indent=indent), encoding="utf-8")
    os.replace(partial, target)


class DuplicateMemberError(ValueError):
    """A JSON object has two members with the same name (SPEC.md section 2)."""


def _no_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateMemberError(f"duplicate member name {printable(repr(key), 80)}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _bounded_int(text: str) -> int:
    if len(text) > MAX_JSON_NUMBER_CHARS:
        raise ValueError(f"integer of {len(text)} characters is not accepted")
    return int(text)


def _bounded_float(text: str) -> float:
    if len(text) > MAX_JSON_NUMBER_CHARS:
        raise ValueError(f"number of {len(text)} characters is not accepted")
    return float(text)


def loads_strict(text: str) -> Any:
    """Parse JSON the way SPEC.md section 2 requires.

    Duplicate member names, ``NaN``/``Infinity`` and numbers of more than
    :data:`MAX_JSON_NUMBER_CHARS` characters are errors. Hostile input can
    raise ``ValueError`` (which includes ``json.JSONDecodeError`` and
    ``UnicodeError``) or ``RecursionError`` (deeply nested arrays); callers
    that read untrusted documents catch both.
    """

    return json.loads(
        text,
        object_pairs_hook=_no_duplicate_members,
        parse_constant=_reject_constant,
        parse_int=_bounded_int,
        parse_float=_bounded_float,
    )


def parse_json_bytes(data: bytes, where: str = "document") -> Any:
    """Decode UTF-8 and parse with :func:`loads_strict`; every failure is a :class:`AgentSoftwareTestingError`."""

    try:
        return loads_strict(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise AgentSoftwareTestingError(f"invalid JSON in {printable(where)}: {printable(exc, 200)}") from exc


def read_json_file(path: Path | str, *, max_bytes: int = 256 * 1024 * 1024) -> Any:
    """Read a JSON document from a regular, non-symlink file."""

    return parse_json_bytes(read_regular_bytes(path, max_bytes=max_bytes), os.fspath(path))


def branch_set_document(
    program: str,
    branch_total: int,
    covered_keys: Iterable[str],
    **extra: Any,
) -> dict[str, Any]:
    """Build a branch-set document (contract section 1); keys sorted, unique."""

    keys = sorted(set(covered_keys))
    document: dict[str, Any] = {
        "schema": SCHEMA_BRANCH_SET,
        "program": program,
        "branch_key_format": BRANCH_KEY_FORMAT,
        "branch_total": branch_total,
        "branch_covered": len(keys),
    }
    document.update(extra)
    document["covered_branch_keys"] = keys
    return document


# ---------------------------------------------------------------------------
# Relative path rules
# ---------------------------------------------------------------------------


def split_path(raw: bytes) -> list[bytes]:
    return raw.split(b"/")


def path_problems(raw: bytes, *, allow_dot: bool = False) -> list[tuple[str, str]]:
    """Return ``(code, message)`` pairs for every rule a relative path breaks.

    Rules (contract section 2): not empty, not absolute, no NUL, no ``..``
    component, no empty or ``.`` component, at most 64 components of at most
    255 bytes, at most 1,024 bytes in total. ``allow_dot`` lets the single
    path ``.`` through (the case root, used for ``cwd``).
    """

    problems: list[tuple[str, str]] = []
    if allow_dot and raw == b".":
        return problems
    if not raw:
        return [("path.empty", "path is empty")]
    if b"\0" in raw:
        problems.append(("path.nul", "path contains a NUL byte"))
    if raw.startswith(b"/"):
        problems.append(("path.absolute", "path is absolute"))
    if len(raw) > MAX_PATH_BYTES:
        problems.append(
            ("path.too_long", f"path is {len(raw)} bytes, limit is {MAX_PATH_BYTES}")
        )
    components = split_path(raw.lstrip(b"/") if raw.startswith(b"/") else raw)
    if len(components) > MAX_PATH_COMPONENTS:
        problems.append(
            (
                "path.too_deep",
                f"path has {len(components)} components, limit is {MAX_PATH_COMPONENTS}",
            )
        )
    for component in components:
        if component == b"..":
            problems.append(("path.traversal", "path contains a '..' component"))
        elif component == b".":
            problems.append(("path.dot", "path contains a '.' component"))
        elif component == b"":
            problems.append(("path.empty_component", "path has an empty component"))
        elif len(component) > MAX_COMPONENT_BYTES:
            problems.append(
                (
                    "path.component_too_long",
                    f"a component is {len(component)} bytes, limit is {MAX_COMPONENT_BYTES}",
                )
            )
    # keep the first report of each code only
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for code, message in problems:
        if code not in seen:
            seen.add(code)
            unique.append((code, message))
    return unique


#: Proxy variables (any case). Replays run without a network, and recorded environments often carry
#: proxy URLs with embedded credentials, so a case may never set them (SPEC.md section 5.4).
PROXY_ENV_NAMES = frozenset({"http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy", "rsync_proxy"})

#: A URL with a user name and password, such as ``scheme://user:secret@host``.
CREDENTIAL_URL_RE = re.compile(rb"://[^/:@\s]+:[^/@\s]+@")
_SCHEME_LETTERS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_SCHEME_BYTES = _SCHEME_LETTERS | frozenset(b"0123456789+.-")


def env_name_forbidden(name: str) -> bool:
    return (
        name in FORBIDDEN_ENV_NAMES
        or name.startswith(FORBIDDEN_ENV_PREFIXES)
        or name.lower() in PROXY_ENV_NAMES
    )


def _has_scheme_before(value: bytes, end: int) -> bool:
    """True when the bytes before ``end`` end in a URL scheme: a letter, then only scheme characters."""

    while end > 0 and value[end - 1] in _SCHEME_BYTES:
        end -= 1
        if value[end] in _SCHEME_LETTERS:
            return True
    return False


def value_has_credentials(value: bytes) -> bool:
    """True when an environment value holds a URL with an embedded user name and password.

    The search starts from ``://`` and reads the scheme backwards, so the time grows with the length of the value
    and a long value cannot hold up the validator.
    """

    return any(_has_scheme_before(value, m.start()) for m in CREDENTIAL_URL_RE.finditer(value))


def parse_mode(text: str) -> int:
    """Parse an octal permission string such as ``"0644"``."""

    if not isinstance(text, str) or not MODE_RE.match(text):
        raise ValueError(f"invalid mode {text!r}: expected three octal digits such as 0644")
    return int(text, 8)


def format_mode(mode: int) -> str:
    return f"{mode & 0o777:04o}"
