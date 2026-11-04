# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""ConcoLLMic test cases to cases, in *record mode*.

A ConcoLLMic test case is a YAML file (``queue/id:*.yaml``). Its ``exec_code`` field holds Python source
that defines ``execute_program(timeout)``; that function writes the input the model chose (often to
temporary files) and runs the program with ``subprocess``. A test case is therefore not a byte string
but a small program, and the earlier measurement replayed it by running the function against the
instrumented build.

Record mode keeps the function but never lets it start the target program. Every case is executed in a
separate Python process, in an empty working directory and an empty temporary directory, with a minimal
environment. ``subprocess.run``, ``Popen``, ``call``, ``check_call``, ``check_output``, ``os.system``,
``os.popen``, ``os.exec*``, ``os.spawn*`` and ``os.posix_spawn`` are replaced by recorders that note what
was asked for and report success. For each program run the recorder keeps

* the argument vector (``argv[0]`` and the shell, if one was named, are dropped),
* the standard input (``input=``, ``stdin=`` with a pipe, a file or a file descriptor, writes to a pipe),
* the working directory and the environment (compared with the minimal environment, so that only changes remain),
* the time limit the code asked for, and the resource limits it set,
* every file, directory and link the code had created at that moment, with its bytes.

The adapter then writes one Agent Software Testing case per program run. Files move into the case root with their
paths relative to the working directory of the run; absolute paths in the argument vector that point into
the sandbox are rewritten the same way. A case that runs the program several times becomes several cases
with the same ``t_ms``. ``t_ms`` is the ``create_time`` of the YAML file relative to the campaign start
(``--start-time``) or, without it, to the earliest file.

What record mode cannot express is reported with its reason and the case is left out unless
``--keep-lossy`` says to write it without that part: resource limits (``preexec_fn`` or ``resource.setrlimit``),
environment variables the format reserves (``LD_PRELOAD``, ``MALLOC_*``, ``GCOV_*`` and others; the locale variables
and ``TZ`` are not reserved and are kept; a variable the recorder's own environment already has with that value
(``LANG=C``, ``TZ=UTC``) needs no entry), a working directory
outside the case root, special files, another program started by the code (a compiler, for example), a terminal on
standard input where the format has no ``stdin_tty``, and everything beyond the limits of the format. A terminal made
with ``pty.openpty()`` and ``os.dup2`` becomes ``stdin_tty`` with an empty input. A case whose code looks at the result of the program
in a way a recorder cannot satisfy (it returns exit status 0 and empty output) is kept when it already started the
program and is marked as ending with an exception.

**The original environment.** In the tool's own replay the code runs in the project directory of the program, where
``./name`` exists, and in images that also hold the tool's instrumented build (``/woff2-instr`` and the like). Code
probes for both and picks its working directory accordingly. Record mode lets ``./name`` exist in the working directory
(``--program-binary`` places a real copy, for code that reads or copies it, at ``--program-path`` when the program sits
in a subdirectory of the project directory, as ``oggenc/oggenc`` does), ``--assume-exists`` names further paths of
the original environment (``stat``, ``access`` and ``chdir`` answer for them), and ``--skip-runs-under`` leaves out runs
that went to the tool's own build, which a replay of the coverage build does not measure.

**Safety.** The code in the YAML files was written by a language model and is executed here. The recorder
refuses network access, process creation, writes outside its sandbox directories and loading of native
code (an audit hook, resource limits, a wall-clock limit, a process group that is killed at the end).
That is a guard against accidents, not a security boundary: code that is hostile on purpose can get around
any measure inside the interpreter. Run the conversion in the container you use for replays, with
networking disabled and a read-only root file system (SPEC section 6.7).

The YAML files are read by a small reader for the subset ConcoLLMic writes (top-level keys, plain,
quoted and block scalars); no YAML library is needed.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import concurrent.futures
import inspect
import json
import math
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..bundle import BundleWriter
from ..case import Case, FileEntry, check_tree
from ..common import (
    DEFAULT_TIMEOUT_MS,
    ENV_NAME_RE,
    MAX_BLOB_BYTES,
    MAX_TIMEOUT_MS,
    env_name_forbidden,
    path_problems,
)
from ..program import Program
from .common import AdapterError, AdapterReport, ToolInfo, new_writer, precheck_case

#: Whether the format of this checkout can say "standard input is a terminal" (``stdin_tty``, SPEC section 5.4).
#: When it cannot, a case that needs one is left out like any other case the format cannot hold.
WRITER_HAS_TTY = "stdin_tty" in inspect.signature(BundleWriter.add_case).parameters

#: Seconds the code is told it may take (``execute_program(timeout)``): the limit of the standard replay.
DEFAULT_CALL_TIMEOUT = 30
#: Wall-clock seconds one case may take to record.
DEFAULT_RECORD_TIMEOUT = 60.0
#: Program runs one case may ask for before it is given up as a search loop.
DEFAULT_MAX_RUNS = 16
#: Largest YAML file read.
MAX_YAML_BYTES = 64 * 1024 * 1024
#: Names ConcoLLMic images give the program's working tree (``/woff2-instr`` while searching, ``/woff2-gcov``
#: for the coverage build). Code that names such a directory means "the program's working directory".
PROJECT_DIR_RE = re.compile(rb"(/[A-Za-z0-9_.-]+-(?:instr|gcov))(?=/|\Z)")
#: Variables the runner sets itself; a case that leaves them out is noted, not rejected.
MANAGED_ENV = (b"PATH", b"HOME", b"TMPDIR", b"LANG", b"TZ")

# Reasons (short, stable; the report counts cases by them)
K_YAML = "unreadable YAML"
K_NO_TIME = "no create_time"
K_NO_CODE = "no exec_code"
K_SYNTAX = "syntax error in exec_code"
K_NO_FUNCTION = "exec_code defines no execute_program"
K_RECORDER_TIMEOUT = "recording timed out"
K_RECORDER_FAILED = "recorder failed"
K_RAISED = "code raised before running the program"
K_NO_RUN = "code did not run the program"
K_SANDBOX = "refused by the sandbox"
K_SHELL = "shell syntax"
K_TOO_MANY_RUNS = "too many program runs"
K_HELPER = "runs another program"
K_TTY = "terminal on standard input"
K_LIMITS = "resource limits"
K_ENV = "reserved environment variable"
K_CWD = "working directory outside the case"
K_SANDBOX_PATH = "sandbox path in arguments"
K_FORMAT = "beyond a limit of the format"
K_FILES = "file the format cannot hold"
K_SKIPPED = "excluded by option"
K_BEYOND = "after the budget"


# ============================================================================================
# A reader for the YAML ConcoLLMic writes
# ============================================================================================


class YamlError(AdapterError):
    """A queue file is not in the YAML subset this reader understands."""


_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*))?\Z")
_BLOCK = re.compile(r"([|>])(?:([1-9])([+-]?)|([+-]?)([1-9]?))[ \t]*(?:#.*)?\Z")
_NULLS = frozenset({"", "~", "null", "Null", "NULL"})
_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
    "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": chr(0x2028), "P": chr(0x2029),
}
_HEX = {"x": 2, "u": 4, "U": 8}


def parse_record(text: str) -> dict[str, Any]:
    """The top-level mapping of a ConcoLLMic YAML file.

    Scalars come back as strings (``None`` for an empty value or ``null``), block sequences of scalars as
    lists of strings, other nested values and flow collections as their raw text. Nothing is converted to
    numbers or dates; the caller reads what it needs. A key that occurs twice keeps its last value (as
    PyYAML does).
    """

    if text.startswith(chr(0xFEFF)):
        text = text[1:]
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    record: dict[str, Any] = {}
    index = 0
    started = False
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if line.startswith("---") and (len(line) == 3 or line[3] in " \t"):
            if started:
                break
            index += 1
            continue
        if line.startswith("...") and (len(line) == 3 or line[3] in " \t"):
            break
        if line[0] in " \t":
            raise YamlError(f"line {index + 1}: unexpected indentation")
        match = _KEY.match(line.rstrip())
        if match is None:
            raise YamlError(f"line {index + 1}: not a 'key: value' line")
        started = True
        try:
            value, index = _read_value(lines, index + 1, match.group(2))
        except YamlError as exc:
            raise YamlError(f"{match.group(1)}: {exc}") from exc
        record[match.group(1)] = value
    return record


def _read_value(lines: list[str], index: int, rest: str | None) -> tuple[Any, int]:
    if rest is None or rest == "" or rest.startswith("#"):
        return _read_nested(lines, index)
    first = rest[0]
    if first in "|>":
        return _read_block_scalar(lines, index, rest)
    if first in "'\"":
        return _read_quoted(lines, index, rest)
    if first in "[{":
        return _read_flow_collection(lines, index, rest)
    return _read_plain(lines, index, rest)


def _plain_value(text: str) -> str | None:
    cut = re.search(r"[ \t]#", text)
    if cut is not None:
        text = text[: cut.start()]
    text = text.strip()
    return None if text in _NULLS else text


def _item_value(text: str) -> str | None:
    text = text.strip()
    if len(text) >= 2 and text[0] in "'\"":
        end = _closing_quote(text[1:], text[0])
        if end is not None:
            return _unescape(text[1 : 1 + end], text[0])
    return _plain_value(text)


def _read_plain(lines: list[str], index: int, rest: str) -> tuple[Any, int]:
    if re.search(r"[ \t]#", rest) is not None:  # a comment ends the scalar
        return _plain_value(rest), index
    pieces = [rest.strip()]
    j = index
    while j < len(lines) and lines[j][:1] in (" ", "\t") and lines[j].strip() and not lines[j].strip().startswith("#"):
        pieces.append(lines[j].strip())
        j += 1
    if len(pieces) == 1:
        return _plain_value(pieces[0]), index
    return _fold_flow([(piece, False) for piece in pieces]), j


def _read_nested(lines: list[str], index: int) -> tuple[Any, int]:
    j = index
    block: list[str] = []
    while j < len(lines):
        line = lines[j]
        if not line.strip() or line[0] in " \t" or line.startswith("- ") or line == "-":
            block.append(line)
            j += 1
        else:
            break
    meaningful = [line.strip() for line in block if line.strip() and not line.strip().startswith("#")]
    if not meaningful:
        return None, j
    if all(item == "-" or item.startswith("- ") for item in meaningful):
        return [_item_value(item[2:]) if item != "-" else None for item in meaningful], j
    return "\n".join(block).rstrip("\n"), j


def _read_flow_collection(lines: list[str], index: int, rest: str) -> tuple[Any, int]:
    text = rest
    j = index
    depth = sum(text.count(c) for c in "[{") - sum(text.count(c) for c in "]}")
    while depth > 0 and j < len(lines):
        text += "\n" + lines[j]
        depth += sum(lines[j].count(c) for c in "[{") - sum(lines[j].count(c) for c in "]}")
        j += 1
    return text.strip(), j


def _read_block_scalar(lines: list[str], index: int, header: str) -> tuple[str, int]:
    match = _BLOCK.match(header.rstrip())
    if match is None:
        raise YamlError(f"cannot read the block scalar header {header[:40]!r}")
    style = match.group(1)
    digit = match.group(2) or match.group(5)
    chomp = match.group(3) or match.group(4) or ""
    if digit:
        indent = int(digit)
    else:
        indent = 0
        for line in lines[index:]:
            if line.strip():
                indent = len(line) - len(line.lstrip(" "))
                break
    content: list[str] = []
    j = index
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            content.append(line[indent:] if len(line) > indent else "")
        elif indent and line.startswith(" " * indent):
            content.append(line[indent:])
        else:
            break
        j += 1
    trailing = 0
    while content and content[-1] == "":
        content.pop()
        trailing += 1
    if not content:
        return ("\n" * trailing if chomp == "+" else ""), j
    text = "\n".join(content) if style == "|" else _fold_block(content)
    if chomp == "-":
        return text, j
    if chomp == "+":
        return text + "\n" * (trailing + 1), j
    return text + "\n", j


def _fold_block(content: list[str]) -> str:
    out = ""
    blanks = 0
    previous_regular = False
    started = False
    for line in content:
        if line == "":
            blanks += 1
            continue
        more_indented = line[0] in " \t"
        if not started:
            out += "\n" * blanks
            started = True
        elif previous_regular and not more_indented:
            out += " " if blanks == 0 else "\n" * blanks
        else:
            out += "\n" * (blanks + 1)
        out += line
        previous_regular = not more_indented
        blanks = 0
    return out


def _fold_flow(pieces: list[tuple[str, bool]]) -> str:
    """Join the lines of a multi-line flow scalar: one break is a space, ``n`` blank lines are ``n`` breaks."""

    out = ""
    blanks = 0
    joined_without_space = False
    started = False
    for text, escaped_break in pieces:
        if text == "" and started and not joined_without_space:
            blanks += 1
            continue
        if started:
            if joined_without_space:
                pass
            elif blanks:
                out += "\n" * blanks
            else:
                out += " "
        out += text
        started = True
        blanks = 0
        joined_without_space = escaped_break
    if blanks:
        out += "\n" * blanks
    return out


def _read_quoted(lines: list[str], index: int, rest: str) -> tuple[str, int]:
    quote = rest[0]
    text = rest[1:]
    j = index
    pieces: list[tuple[str, bool]] = []
    first = True
    while True:
        end = _closing_quote(text, quote)
        if end is not None:
            tail = text[end + 1 :].strip()
            if tail and not tail.startswith("#"):
                raise YamlError("text after the closing quote")
            body = text[:end] if first else text[:end].lstrip(" \t")
            pieces.append((_unescape(body, quote), False))
            break
        escaped_break = False
        body = text if first else text.strip(" \t")
        if first:
            body = body.rstrip(" \t")
        if quote == '"':
            trailing = len(body) - len(body.rstrip("\\"))
            if trailing % 2 == 1:
                body = body[:-1]
                escaped_break = True
        pieces.append((_unescape(body, quote), escaped_break))
        if j >= len(lines):
            raise YamlError("unterminated quoted scalar")
        text = lines[j]
        j += 1
        first = False
    return _fold_flow(pieces), j


def _closing_quote(text: str, quote: str) -> int | None:
    position = 0
    while position < len(text):
        char = text[position]
        if quote == "'":
            if char == "'":
                if text[position + 1 : position + 2] == "'":
                    position += 2
                    continue
                return position
        else:
            if char == "\\":
                position += 2
                continue
            if char == '"':
                return position
        position += 1
    return None


def _unescape(body: str, quote: str) -> str:
    if quote == "'":
        return body.replace("''", "'")
    out: list[str] = []
    position = 0
    while position < len(body):
        char = body[position]
        if char != "\\":
            out.append(char)
            position += 1
            continue
        position += 1
        if position >= len(body):
            break
        code = body[position]
        if code in _HEX:
            width = _HEX[code]
            digits = body[position + 1 : position + 1 + width]
            try:
                out.append(chr(int(digits, 16)))
            except ValueError as exc:
                raise YamlError(f"bad escape \\{code}{digits}") from exc
            position += 1 + width
            continue
        if code not in _ESCAPES:
            raise YamlError(f"unknown escape \\{code}")
        out.append(_ESCAPES[code])
        position += 1
    return "".join(out)


_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?\Z")


def parse_time_ms(text: str) -> int:
    """A ``create_time`` such as ``2026-09-25 16:55:24.129`` as milliseconds. The clock has no zone; it is read as UTC."""

    match = _TIME.match(text.strip())
    if match is None:
        raise ValueError(f"not a timestamp: {text[:40]!r}")
    year, month, day, hour, minute, second = (int(match.group(n)) for n in range(1, 7))
    fraction = (match.group(7) or "0").ljust(3, "0")[:3]
    return calendar.timegm((year, month, day, hour, minute, second)) * 1000 + int(fraction)


# ============================================================================================
# The queue
# ============================================================================================


@dataclass(frozen=True)
class QueueCase:
    """One ``id:*.yaml`` file, reduced to what record mode needs."""

    name: str
    index: int | None
    create_ms: int | None
    exec_code: str | None
    problem: str | None = None


def load_queue(directory: Path | str) -> list[QueueCase]:
    root = Path(directory)
    if not root.is_dir():
        raise AdapterError(f"not a directory: {root}")
    cases: list[QueueCase] = []
    for path in sorted(root.iterdir()):
        if not (path.name.startswith("id:") and path.suffix in (".yaml", ".yml")):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        cases.append(load_case_file(path))
    return cases


def load_case_file(path: Path) -> QueueCase:
    name = path.name
    try:
        size = path.stat().st_size
        if size > MAX_YAML_BYTES:
            return QueueCase(name, None, None, None, f"{K_YAML}: file of {size} bytes")
        record = parse_record(path.read_bytes().decode("utf-8", errors="replace"))
    except (OSError, YamlError) as exc:
        return QueueCase(name, None, None, None, f"{K_YAML}: {exc}")
    index: int | None = None
    for candidate in (record.get("id"), name[3:].split(",", 1)[0].split(".", 1)[0]):
        try:
            index = int(str(candidate).strip())
            break
        except ValueError:
            continue
    created = record.get("create_time")
    create_ms: int | None = None
    if isinstance(created, str):
        try:
            create_ms = parse_time_ms(created)
        except ValueError:
            create_ms = None
    code = record.get("exec_code")
    return QueueCase(name, index, create_ms, code if isinstance(code, str) and code.strip() else None)


# ============================================================================================
# Record mode: the child process
# ============================================================================================

_RECORDER_SOURCE = r'''# Record mode, child side. Written by the ConcoLLMic adapter of agent-software-testing next to a job file.
#
# Usage: python recorder.py JOB.json
#
# Runs the execute_program function of ONE ConcoLLMic test case with every way of starting a
# program replaced by recording: the program is never run. What the code asked for (argument
# vector, standard input, working directory, environment, resource limits) and the files it had
# created at that moment are written to the result file named in the job. The parent process
# turns that into Agent Software Testing cases.
#
# This is a convenience guard, not a security boundary: an audit hook refuses network access,
# process creation, writes outside the sandbox directories and loading of native code, but
# code that is hostile on purpose can get around any in-process measure. Run the adapter in a
# container without network access (see docs/adapters.md).
import base64
import builtins
import errno
import hashlib
import io
import json
import os
import signal
import stat
import sys
import time
import traceback

MAX_ENTRIES = 256  # files, directories and links snapshotted per program run


def b64(data):
    return base64.b64encode(data).decode("ascii")


with open(sys.argv[1], encoding="utf-8") as _handle:
    JOB = json.load(_handle)

ROOT = JOB["root"]
TMP = JOB["tmp"]
HOME = JOB["home"]
MAX_BLOB = JOB["max_blob"]  # the largest file or standard input one case can hold
STDIN_CAP = MAX_BLOB + 1
STORE_BYTES = max(96 * 1024 * 1024, 4 * MAX_BLOB)  # all distinct contents kept for the result (identical ones once)
RESULT_FD = os.open(JOB["result"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

BLOBS = {}  # sha256 hex -> bytes: contents of files and standard input, kept once
STORED = [0]
RUNS = []  # one record per program run the code asked for, in order
PENDING = []  # fake processes whose standard input may still be written
RLIMITS = {}  # resource name -> [soft, hard] as the code set them for itself
NOTES = []
# The adapter starts the recorder with PYTHONCOERCECLOCALE=0: under LANG=C and no LC_ALL, Python would otherwise
# replace the C locale with C.UTF-8 at startup (PEP 538) and hand the program an LC_CTYPE the replay does not set.
# The interpreter has already read it, so drop it before the program's environment is captured.
os.environ.pop("PYTHONCOERCECLOCALE", None)
BASE_ENV = dict(os.environ)


class Abort(BaseException):
    """The recorder cannot interpret what the code asked for; the case is reported with this reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ExecDone(BaseException):
    """os.exec* replaced the process image in the original; nothing after it would have run."""


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def fsbytes(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
        return value if isinstance(value, bytes) else os.fsencode(value)
    if isinstance(value, str):
        return os.fsencode(value)
    raise TypeError("expected str, bytes or os.PathLike object, not " + type(value).__name__)


def program_unchanged(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest() == PROGRAM["digest"]
    except OSError:
        return False


def keep(data):
    """Remember the bytes for the result; returns their digest, or None when the store is full."""

    digest = hashlib.sha256(data).hexdigest()
    if digest not in BLOBS:
        if STORED[0] + len(data) > STORE_BYTES:
            return None
        BLOBS[digest] = data
        STORED[0] += len(data)
    return digest


def read_all(path):
    """Bytes of a regular file the sandbox owns, even when its mode forbids reading."""

    try:
        with open(path, "rb") as handle:
            return handle.read(STDIN_CAP)
    except PermissionError:
        mode = os.lstat(path).st_mode & 0o7777
        os.chmod(path, mode | 0o400)
        try:
            with open(path, "rb") as handle:
                return handle.read(STDIN_CAP)
        finally:
            os.chmod(path, mode)


PROGRAM = {"path": JOB.get("program_path"), "digest": None, "dirs": []}
if PROGRAM["path"]:
    with open(PROGRAM["path"], "rb") as _program:
        PROGRAM["digest"] = hashlib.sha256(_program.read()).hexdigest()
    _parent = os.path.dirname(PROGRAM["path"])
    while _parent != ROOT and _parent.startswith(ROOT + os.sep):  # directories made to hold the program (--program-path)
        PROGRAM["dirs"].append(_parent)
        _parent = os.path.dirname(_parent)


def snapshot():
    """Everything the code has created below the working directory and the temporary directory."""

    entries = []
    truncated = False
    opened = []  # directories made searchable while the snapshot is taken: (path, original mode)
    try:
        for base in (ROOT, TMP):
            stack = [base]
            while stack and not truncated:
                directory = stack.pop()
                try:
                    mode = os.lstat(directory).st_mode & 0o7777
                    if mode & 0o700 != 0o700:
                        # the code took away its own access (chmod 0400 to make lstat of a child fail): look inside
                        # anyway; the entry of the directory itself already holds the mode it has now
                        os.chmod(directory, mode | 0o700)
                        opened.append((directory, mode))
                    names = sorted(os.listdir(directory))
                except OSError:
                    continue
                for name in names:
                    if len(entries) >= MAX_ENTRIES:
                        truncated = True
                        break
                    path = os.path.join(directory, name)
                    try:
                        info = os.lstat(path)
                    except OSError:
                        continue
                    if path == PROGRAM["path"] and stat.S_ISREG(info.st_mode) and program_unchanged(path):
                        continue  # the program itself is not part of the case
                    entry = {"path": b64(os.fsencode(path)), "mode": info.st_mode & 0o777}
                    if stat.S_ISDIR(info.st_mode):
                        entry["kind"] = "dir"
                        stack.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        entry["kind"] = "file"
                        entry["size"] = info.st_size
                        if info.st_size <= MAX_BLOB:
                            try:
                                digest = keep(read_all(path))
                            except OSError as exc:
                                entry["unreadable"] = str(exc)[:200]
                            else:
                                if digest is None:
                                    entry["unreadable"] = "too much data in this case"
                                else:
                                    entry["blob"] = digest
                    elif stat.S_ISLNK(info.st_mode):
                        entry["kind"] = "symlink"
                        entry["target"] = b64(os.readlink(os.fsencode(path)))
                    elif stat.S_ISFIFO(info.st_mode):
                        entry["kind"] = "fifo"
                    else:
                        entry["kind"] = "other"
                    entries.append(entry)
    finally:
        for path, mode in reversed(opened):
            try:
                os.chmod(path, mode)
            except OSError:
                pass
    if PROGRAM["dirs"]:
        # a directory that only holds the program is not something the code made
        names = [os.fsdecode(base64.b64decode(entry["path"])) for entry in entries]
        empty = {d for d in PROGRAM["dirs"] if not any(name.startswith(d + os.sep) for name in names)}
        entries = [entry for entry, name in zip(entries, names) if name not in empty]
    return entries, truncated


# --------------------------------------------------------------------------------------------
# shell command lines (shell=True, os.system, os.popen): only a plain command is understood
# --------------------------------------------------------------------------------------------

SHELL_META = set("|&;<>()$`*?[]{}~!#\n\\")


def parse_shell(command):
    words = []
    current = []
    in_word = False
    quote = None
    for char in command:
        if quote == "'":
            if char == "'":
                quote = None
            else:
                current.append(char)
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in '$`\\':
                raise Abort("shell syntax: " + repr(char) + " inside double quotes")
            else:
                current.append(char)
        elif char in "'\"":
            quote = char
            in_word = True
        elif char in " \t":
            if in_word:
                words.append("".join(current))
                current = []
                in_word = False
        elif char in SHELL_META:
            raise Abort("shell syntax: " + repr(char) + " in " + repr(command[:80]))
        else:
            current.append(char)
            in_word = True
    if quote:
        raise Abort("shell syntax: unterminated quote in " + repr(command[:80]))
    if in_word:
        words.append("".join(current))
    assignments = {}
    while words and "=" in words[0] and words[0].split("=", 1)[0].replace("_", "a").isalnum() and not words[0][0].isdigit():
        name, value = words.pop(0).split("=", 1)
        assignments[os.fsencode(name)] = os.fsencode(value)
    if not words:
        raise Abort("shell syntax: no command in " + repr(command[:80]))
    return [os.fsencode(word) for word in words], assignments


def normalise_argv(args, shell):
    """The argument vector of a call, as bytes, and environment assignments of a shell command line."""

    if shell:
        if isinstance(args, (str, bytes, os.PathLike)):
            return parse_shell(os.fsdecode(fsbytes(args)))
        items = list(args)
        if len(items) != 1:
            raise Abort("shell syntax: positional parameters for sh -c")
        return parse_shell(os.fsdecode(fsbytes(items[0])))
    if isinstance(args, (str, bytes, os.PathLike)):
        return [fsbytes(args)], {}
    return [fsbytes(item) for item in args], {}


def env_pairs(env, assignments):
    if env is None:
        source = {os.fsencode(k): os.fsencode(v) for k, v in os.environ.items()}
    else:
        source = {fsbytes(k): fsbytes(v) for k, v in dict(env).items()}
    source.update(assignments)
    return source


# --------------------------------------------------------------------------------------------
# the fake process
# --------------------------------------------------------------------------------------------

PIPE, STDOUT, DEVNULL = -1, -2, -3


def drain_pipe(fd):
    """The bytes a program would read from the pipe ``fd`` (an ``os.pipe()`` the code filled and passed as ``stdin=``).

    What is in the pipe now is read, then what a thread of the code writes into it, until the write end is closed. A
    pipe whose write end stays open and receives nothing for a quarter of a second, or that holds more than a case can,
    ends the wait. The bytes are consumed, as the program would have consumed them.
    """

    import fcntl
    import select

    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except OSError:
        return {"mode": "empty"}
    chunks = []
    total = 0
    deadline = time.monotonic() + 5.0
    try:
        while total <= MAX_BLOB and time.monotonic() < deadline:
            try:
                data = os.read(fd, 1 << 16)
            except BlockingIOError:
                if not select.select([fd], [], [], 0.25)[0]:
                    break  # the write end is open and nothing arrives
                continue
            except OSError:
                break
            if not data:
                break  # every write end is closed
            total += len(data)
            if total <= MAX_BLOB:
                chunks.append(data)
    finally:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        except OSError:
            pass
    if total > MAX_BLOB:
        return {"mode": "data", "size": total, "too_large": True}
    return stdin_record(b"".join(chunks))


def inherited_stdin(fd):
    """Standard input the original program would have seen when it reads file descriptor ``fd``."""

    try:
        if os.isatty(fd):
            return {"mode": "tty"}
        info = os.fstat(fd)
    except OSError:
        return {"mode": "empty"}
    if stat.S_ISREG(info.st_mode):
        try:
            position = os.lseek(fd, 0, os.SEEK_CUR)
            data = os.pread(fd, STDIN_CAP, position)
        except OSError:
            return {"mode": "empty"}
        return stdin_record(data)
    if stat.S_ISFIFO(info.st_mode):
        return drain_pipe(fd)
    return {"mode": "empty"}


def stdin_record(data, too_large=False):
    if not data and not too_large:
        return {"mode": "empty"}
    if too_large or len(data) > MAX_BLOB:
        return {"mode": "data", "size": len(data), "too_large": True}
    digest = keep(data)
    if digest is None:
        return {"mode": "data", "size": len(data), "too_large": True}
    return {"mode": "data", "blob": digest, "size": len(data)}


class StdinWriter:
    def __init__(self, process):
        self._process = process
        self.closed = False

    def write(self, data):
        if self.closed:
            raise ValueError("write to closed file")
        self._process._write(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def fileno(self):
        raise io.UnsupportedOperation("fileno")

    def writable(self):
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakePopen:
    def __init__(
        self,
        args,
        bufsize=-1,
        executable=None,
        stdin=None,
        stdout=None,
        stderr=None,
        preexec_fn=None,
        close_fds=True,
        shell=False,
        cwd=None,
        env=None,
        universal_newlines=None,
        startupinfo=None,
        creationflags=0,
        restore_signals=True,
        start_new_session=False,
        pass_fds=(),
        *,
        text=None,
        encoding=None,
        errors=None,
        user=None,
        group=None,
        extra_groups=None,
        umask=-1,
        pipesize=-1,
        process_group=None,
        _kind="subprocess.Popen",
        _argv=None,
    ):
        if len(RUNS) >= JOB["max_runs"]:
            raise Abort("more than %d program runs (a search loop?)" % JOB["max_runs"])
        self.args = args
        self.text_mode = bool(universal_newlines or text or encoding or errors)
        self.encoding = encoding or "utf-8"
        self.errors = errors or "strict"
        self.returncode = 0
        self.pid = 2 ** 22 - 3 - len(RUNS)
        self._chunks = []
        self._size = 0
        self._oversize = False
        self._stdin_mode = "none"
        self._finished = False
        self._communicated = False
        unsupported = []

        if _argv is not None:
            argv, assignments = _argv
        else:
            argv, assignments = normalise_argv(args, shell)
        record = {"kind": _kind, "argv": [b64(a) for a in argv], "shell": bool(shell), "unsupported": unsupported}
        record["executable"] = None if executable is None else b64(fsbytes(executable))

        directory = os.getcwd()
        if cwd is not None:
            directory = os.path.normpath(os.path.join(directory, os.fsdecode(fsbytes(cwd))))
        record["cwd"] = b64(os.fsencode(directory))
        pairs = env_pairs(env, assignments)
        record["env"] = [[b64(k), b64(v)] for k, v in sorted(pairs.items())]
        record["timeout"] = None

        # standard input
        if stdin == PIPE:
            self._stdin_mode = "pipe"
        elif stdin == DEVNULL:
            self._stdin_mode = "empty"
        elif stdin is None:
            record["stdin"] = inherited_stdin(0)
            self._stdin_mode = "fixed"
        else:
            fd = stdin if isinstance(stdin, int) else stdin.fileno()
            record["stdin"] = inherited_stdin(fd)
            self._stdin_mode = "fixed"
        self.stdin = StdinWriter(self) if self._stdin_mode == "pipe" else None
        self._out_kind = stdout == PIPE
        self._err_kind = stderr == PIPE
        empty = "" if self.text_mode else b""
        self.stdout = (io.StringIO(empty) if self.text_mode else io.BytesIO(empty)) if self._out_kind else None
        self.stderr = (io.StringIO(empty) if self.text_mode else io.BytesIO(empty)) if self._err_kind else None

        if preexec_fn is not None:
            unsupported.append("preexec_fn " + getattr(preexec_fn, "__name__", "?"))
        if user is not None or group is not None or extra_groups is not None:
            unsupported.append("user, group or extra_groups")
        if umask is not None and umask >= 0:
            unsupported.append("umask")
        if RLIMITS:
            record["rlimits"] = {name: list(value) for name, value in sorted(RLIMITS.items())}
        entries, truncated = snapshot()
        record["tree"] = entries
        record["tree_truncated"] = truncated
        RUNS.append(record)
        PENDING.append(self)
        self._record = record

    # -- standard input ---------------------------------------------------
    def _write(self, data):
        if self.text_mode:
            if not isinstance(data, str):
                raise TypeError("write() argument must be str, not " + type(data).__name__)
            data = data.encode(self.encoding, self.errors)
        else:
            if isinstance(data, str):
                raise TypeError("a bytes-like object is required, not 'str'")
            data = bytes(data)
        self._size += len(data)
        if self._size > MAX_BLOB:
            self._oversize = True  # the case cannot hold it; do not keep it in memory
        else:
            self._chunks.append(data)

    def _outputs(self):
        empty = "" if self.text_mode else b""
        return (empty if self._out_kind else None), (empty if self._err_kind else None)

    def _finish(self):
        if self._finished:
            return
        self._finished = True
        record = self._record
        if self._stdin_mode == "pipe":
            record["stdin"] = stdin_record(b"".join(self._chunks), too_large=self._oversize)
            if self._oversize:
                record["stdin"]["size"] = self._size
        elif self._stdin_mode == "empty":
            record["stdin"] = {"mode": "empty"}

    def _timeout(self, timeout):
        if timeout is not None and self._record["timeout"] is None:
            self._record["timeout"] = timeout

    # -- the subprocess.Popen interface -------------------------------------
    def communicate(self, input=None, timeout=None):
        self._timeout(timeout)
        if input is not None and self._stdin_mode == "pipe":
            self._write(input)
        elif input is not None:
            NOTES.append("input given to a process whose standard input is not a pipe was ignored")
        self._communicated = True
        self._finish()
        return self._outputs()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._timeout(timeout)
        self._finish()
        return self.returncode

    def send_signal(self, sig):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.stdin is not None:
            self.stdin.close()
        self._finish()

    def __del__(self):
        pass

    def __repr__(self):
        return "<Popen: returncode: 0 args: %r>" % (self.args,)


def fake_run(*popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    import subprocess

    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs["stdout"] = PIPE
        kwargs["stderr"] = PIPE
    kwargs.setdefault("_kind", "subprocess.run")
    with FakePopen(*popenargs, **kwargs) as process:
        process._timeout(timeout)
        stdout, stderr = process.communicate(input, timeout=timeout)
    return subprocess.CompletedProcess(process.args, 0, stdout, stderr)


def fake_call(*popenargs, timeout=None, **kwargs):
    kwargs.setdefault("_kind", "subprocess.call")
    with FakePopen(*popenargs, **kwargs) as process:
        return process.wait(timeout=timeout)


def fake_check_call(*popenargs, **kwargs):
    fake_call(*popenargs, **kwargs)
    return 0


def fake_check_output(*popenargs, timeout=None, **kwargs):
    kwargs.setdefault("stdout", PIPE)
    return fake_run(*popenargs, timeout=timeout, check=True, **kwargs).stdout


def fake_getstatusoutput(cmd, *, encoding=None, errors=None):
    fake_run(cmd, shell=True, stdout=PIPE, stderr=STDOUT, text=True)
    return 0, ""


def fake_getoutput(cmd, *, encoding=None, errors=None):
    return fake_getstatusoutput(cmd)[1]


def started(args, *, kind, env=None, executable=None, shell=False, argv=None):
    process = FakePopen(args, executable=executable, env=env, shell=shell, _kind=kind, _argv=argv)
    process._finish()
    return process


def fake_system(command):
    started(command, kind="os.system", shell=True)
    return 0


class PopenFile:
    """What os.popen returns: reads are empty, writes are the standard input of the program."""

    def __init__(self, process, mode):
        self._process = process
        self._mode = mode
        self.closed = False

    def read(self, *args):
        return ""

    def readline(self, *args):
        return ""

    def readlines(self, *args):
        return []

    def __iter__(self):
        return iter(())

    def write(self, data):
        self._process._write(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        self._process._finish()
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_popen(cmd, mode="r", buffering=-1):
    if mode not in ("r", "w"):
        raise ValueError("invalid mode %r" % mode)
    process = FakePopen(cmd, shell=True, text=True, stdin=PIPE if mode == "w" else None, _kind="os.popen")
    return PopenFile(process, mode)


def exec_family(name):
    with_env = name in ("execve", "execvpe", "execle", "execlpe")
    listed = name.startswith("execl")

    def replacement(path, *rest):
        env = None
        if listed:
            items = list(rest)
            if with_env:
                env = items.pop()
            argv = [fsbytes(item) for item in items]
        else:
            argv = [fsbytes(item) for item in rest[0]]
            if with_env:
                env = rest[1]
        started(None, kind="os." + name, env=env, executable=path, argv=(argv or [fsbytes(path)], {}))
        raise ExecDone()

    replacement.__name__ = name
    return replacement


def spawn_family(name):
    listed = name.startswith("spawnl")
    with_env = name in ("spawnve", "spawnvpe", "spawnle", "spawnlpe")

    def replacement(mode, path, *rest):
        env = None
        if listed:
            items = list(rest)
            if with_env:
                env = items.pop()
            argv = [fsbytes(item) for item in items]
        else:
            argv = [fsbytes(item) for item in rest[0]]
            if with_env:
                env = rest[1]
        started(None, kind="os." + name, env=env, executable=path, argv=(argv or [fsbytes(path)], {}))
        return 0

    replacement.__name__ = name
    return replacement


def fake_posix_spawn(path, argv, env, **kwargs):
    started(None, kind="os.posix_spawn", env=env, executable=path, argv=([fsbytes(item) for item in argv], {}))
    return 2 ** 22 - 2


# --------------------------------------------------------------------------------------------
# limits the code sets for itself (they are inherited by the programs it starts)
# --------------------------------------------------------------------------------------------


def install_resource():
    try:
        import resource
    except ImportError:
        return
    names = {getattr(resource, name): name for name in dir(resource) if name.startswith("RLIMIT_")}
    real_get = resource.getrlimit

    def getrlimit(which):
        name = names.get(which)
        if name in RLIMITS:
            return tuple(RLIMITS[name])
        return real_get(which)

    def setrlimit(which, limits):
        soft, hard = limits
        RLIMITS[names.get(which, str(which))] = [int(soft), int(hard)]

    resource.getrlimit = getrlimit
    resource.setrlimit = setrlimit
    if hasattr(resource, "prlimit"):
        def prlimit(pid, which, limits=None):
            if pid not in (0, os.getpid()):
                raise PermissionError(errno.EPERM, "record mode: prlimit of another process")
            old = getrlimit(which)
            if limits is not None:
                setrlimit(which, limits)
            return old

        resource.prlimit = prlimit


# --------------------------------------------------------------------------------------------
# paths the original environment had (the code of a case may probe for them)
# --------------------------------------------------------------------------------------------

VIRTUAL_CWD = [None]


def install_assumed_paths():
    """Make the paths named by the job exist for stat, access and chdir, as they did where the case was found."""

    given = [os.path.normpath(p) for p in JOB.get("assume_exists", [])]
    if not given:
        return
    directories = {p for p, raw in zip(given, JOB["assume_exists"]) if raw.endswith("/")}
    files = set(given) - directories
    for path in list(files | directories):
        parent = os.path.dirname(path)
        while parent not in ("", "/"):
            directories.add(parent)
            parent = os.path.dirname(parent)
    directories.discard("/")
    files -= directories
    real_stat, real_lstat, real_access, real_chdir, real_getcwd = os.stat, os.lstat, os.access, os.chdir, os.getcwd

    def lookup(path):
        try:
            text = os.fsdecode(fsbytes(path)) if not isinstance(path, int) else None
        except TypeError:
            return None
        if text is None:
            return None
        if not text.startswith("/"):
            text = os.path.join(getcwd_(), text)
        text = os.path.normpath(text)
        return "dir" if text in directories else "file" if text in files else None

    def fake(kind):
        mode = (stat.S_IFDIR | 0o755) if kind == "dir" else (stat.S_IFREG | 0o755)
        now = int(time.time())
        return os.stat_result((mode, 0, 0, 1, os.getuid(), os.getgid(), 0 if kind == "file" else 4096, now, now, now))

    def stat_(path, *args, **kwargs):
        try:
            return real_stat(path, *args, **kwargs)
        except FileNotFoundError:
            kind = lookup(path)
            if kind is None:
                raise
            return fake(kind)

    def lstat_(path, *args, **kwargs):
        try:
            return real_lstat(path, *args, **kwargs)
        except FileNotFoundError:
            kind = lookup(path)
            if kind is None:
                raise
            return fake(kind)

    def access_(path, mode, *args, **kwargs):
        if not real_access(path, os.F_OK) and lookup(path) is not None:
            return True
        return real_access(path, mode, *args, **kwargs)

    def chdir_(path):
        try:
            real_chdir(path)
        except FileNotFoundError:
            if lookup(path) != "dir":
                raise
            VIRTUAL_CWD[0] = os.path.normpath(os.fsdecode(fsbytes(path)))
            return
        VIRTUAL_CWD[0] = None

    def getcwd_():
        return VIRTUAL_CWD[0] if VIRTUAL_CWD[0] is not None else real_getcwd()

    os.stat, os.lstat, os.access, os.chdir, os.getcwd = stat_, lstat_, access_, chdir_, getcwd_


def install_find_library():
    """ctypes.util.find_library starts ldconfig, gcc and ld; look in the usual directories instead."""

    import ctypes.util
    import glob

    directories = (
        "/lib", "/lib64", "/usr/lib", "/usr/lib64", "/usr/local/lib", "/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
        "/lib/aarch64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
    )

    def find_library(name):
        for directory in directories:
            hits = sorted(glob.glob(os.path.join(directory, "lib%s.so*" % name)))
            if hits:
                return os.path.basename(hits[0])
        return None

    ctypes.util.find_library = find_library


# --------------------------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------------------------

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
DEVICES_OK = {"/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/urandom", "/dev/random"}
DENIED = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "pty.spawn",
    "os.startfile", "os.kill", "os.killpg", "os.setuid", "os.setgid", "os.chroot", "ctypes.dlopen", "ctypes.dlsym",
    "ctypes.call_function", "socket.bind", "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
    "socket.gethostbyname_ex", "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
    "urllib.Request", "http.client.connect", "ftplib.connect", "smtplib.connect", "telnetlib.Telnet.open",
}
# event -> positions of the arguments that name paths the code changes
MUTATING = {
    "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "os.rename": (0, 1), "os.link": (0, 1), "os.symlink": (1,),
    "os.chmod": (0,), "os.chown": (0,), "os.truncate": (0,), "os.utime": (0,), "os.mkfifo": (0,), "os.mknod": (0,),
    "os.setxattr": (0,), "os.removexattr": (0,), "os.lchmod": (0,), "os.chflags": (0,),
    "shutil.rmtree": (0,), "shutil.copyfile": (1,), "shutil.copymode": (1,), "shutil.copystat": (1,),
    "shutil.copytree": (1,), "shutil.move": (1,), "shutil.chown": (0,), "shutil.make_archive": (0,),
    "shutil.unpack_archive": (1,),
}
ALLOWED_ROOTS = []


def inside(path):
    if isinstance(path, int):
        return True
    if isinstance(path, (bytes, bytearray)):
        path = os.fsdecode(bytes(path))
    elif isinstance(path, os.PathLike):
        path = os.fsdecode(os.fspath(path))
    elif not isinstance(path, str):
        return True
    if path in DEVICES_OK:
        return True
    real = os.path.realpath(os.path.abspath(path))
    return any(real == root or real.startswith(root + os.sep) for root in ALLOWED_ROOTS)


def refuse(what):
    raise PermissionError(errno.EPERM, "record mode: " + what)


def audit(event, args):
    if event in DENIED and not (JOB.get("allow_native") and event.startswith("ctypes.")):
        refuse(event + " is not allowed")
    if event == "open":
        path, mode, flags = args[0], args[1], args[2]
        writing = False
        if isinstance(flags, int):
            writing = bool(flags & WRITE_FLAGS)
        elif isinstance(mode, str):
            writing = any(char in mode for char in "wax+")
        if writing and not inside(path):
            refuse("write outside the sandbox: " + os.fsdecode(fsbytes(path))[:200])
    elif event in MUTATING:
        for position in MUTATING[event]:
            if position < len(args) and not inside(args[position]):
                refuse("%s outside the sandbox: %s" % (event, os.fsdecode(fsbytes(args[position]))[:200]))
    elif event == "socket.__new__":
        import socket

        if len(args) > 1 and args[1] in (socket.AF_INET, getattr(socket, "AF_INET6", -1)):
            refuse("network sockets are not allowed")


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------


def limit_resources():
    try:
        import resource
    except ImportError:
        return
    settings = [("RLIMIT_CORE", 0), ("RLIMIT_CPU", JOB["cpu_s"]), ("RLIMIT_FSIZE", 512 * 1024 * 1024)]
    if sys.platform.startswith("linux"):
        settings.append(("RLIMIT_AS", 4 * 1024 * 1024 * 1024))
    for name, value in settings:
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
        except (ValueError, OSError):
            pass


def install():
    import random
    import subprocess
    import tempfile

    limit_resources()
    for name in ("PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE"):
        os.environ.pop(name, None)
    subprocess.Popen = FakePopen
    subprocess.run = fake_run
    subprocess.call = fake_call
    subprocess.check_call = fake_check_call
    subprocess.check_output = fake_check_output
    subprocess.getoutput = fake_getoutput
    subprocess.getstatusoutput = fake_getstatusoutput
    os.system = fake_system
    os.popen = fake_popen
    for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"):
        setattr(os, name, exec_family(name))
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe"):
        setattr(os, name, spawn_family(name))
    os.posix_spawn = fake_posix_spawn
    os.posix_spawnp = fake_posix_spawn
    os.kill = lambda pid, sig: None
    pid = 1000 + JOB["seed"] % 30000  # names made from the process id (a directory "x_<pid>") must not vary from one conversion to the next
    os.getpid = lambda: pid
    os.killpg = lambda pgid, sig: None
    time.sleep = lambda seconds: None
    install_resource()
    install_assumed_paths()
    install_find_library()
    random.seed(JOB["seed"])
    stream = random.Random(JOB["seed"] ^ 0x5BD1E995)  # what os.urandom and unseeded generators draw: fixed by the file

    def urandom(count):
        return stream.randbytes(count)

    os.urandom = urandom
    random._urandom = urandom
    seed_original = random.Random.seed

    def seed(self, a=None, version=2):
        seed_original(self, stream.getrandbits(128) if a is None else a, version)

    random.Random.seed = seed
    tempfile._Random = lambda: random.Random(JOB["seed"])
    tempfile._name_sequence = None
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    ALLOWED_ROOTS.extend(os.path.realpath(path) for path in (ROOT, TMP, HOME))
    sys.addaudithook(audit)


def preload():
    """Import what test cases usually need before the guard is installed."""

    import base64 as _base64  # noqa: F401
    import binascii  # noqa: F401
    import collections  # noqa: F401
    import hashlib  # noqa: F401
    import pathlib  # noqa: F401
    import random  # noqa: F401
    import shutil  # noqa: F401
    import signal as _signal  # noqa: F401
    import struct  # noqa: F401
    import subprocess  # noqa: F401
    import tempfile  # noqa: F401
    import textwrap  # noqa: F401
    import zlib  # noqa: F401

    try:
        import pty  # noqa: F401
        import resource  # noqa: F401
    except ImportError:
        pass


def finish_all():
    for process in PENDING:
        process._finish()


def write_result(document):
    data = json.dumps(document).encode("utf-8")
    view = memoryview(data)
    while view:
        written = os.write(RESULT_FD, view)
        view = view[written:]
    os.close(RESULT_FD)


def main():
    preload()
    install()
    namespace = {"__name__": "__concollmic_case__", "__builtins__": builtins}
    outcome = {"state": "returned"}
    try:
        code = compile(JOB["code"], "<exec_code>", "exec")
    except (SyntaxError, ValueError) as exc:
        write_result({"syntax_error": "%s (line %s)" % (getattr(exc, "msg", exc), getattr(exc, "lineno", "?"))})
        return
    try:
        exec(code, namespace)
        function = namespace.get("execute_program")
        if not callable(function):
            write_result({"missing_function": True})
            return
        function(JOB["call_timeout"])
    except Abort as exc:
        outcome = {"state": "aborted", "reason": exc.reason}
    except ExecDone:
        pass
    except BaseException as exc:  # includes SystemExit: the code ended the process itself
        outcome = {
            "state": "raised",
            "type": type(exc).__name__,
            "message": str(exc)[:400],
            "traceback_tail": traceback.format_exc()[-1200:],
        }
    finish_all()
    write_result({
        "outcome": outcome,
        "runs": RUNS,
        "notes": NOTES,
        "blobs": {digest: b64(data) for digest, data in BLOBS.items()},
        "base_env": [[b64(os.fsencode(k)), b64(os.fsencode(v))] for k, v in sorted(BASE_ENV.items())],
    })


try:
    main()
except BaseException:  # the recorder itself failed
    try:
        write_result({"internal_error": traceback.format_exc()[-2000:]})
    except BaseException:
        pass
os._exit(0)
'''


@dataclass
class RecordSettings:
    """How one case is executed."""

    work_dir: str | None = None
    timeout_s: float = DEFAULT_RECORD_TIMEOUT
    call_timeout: int = DEFAULT_CALL_TIMEOUT
    max_runs: int = DEFAULT_MAX_RUNS
    python: str = ""
    allow_native: bool = False
    assume_exists: tuple[str, ...] = ()
    #: File name of the program. The working directory of a ConcoLLMic case holds it (the project directory of the
    #: tool's replay), so code that probes for ``./name`` finds it.
    binary_name: str = ""
    #: A file to place as ``./<binary_name>`` in the working directory (a copy of the program; the case does not
    #: hold it unless the code copies or changes it). Without it the program exists only for probes.
    program_binary: str | None = None
    #: Where the program sits, relative to the working directory, when that is not ``./<binary_name>``: the project
    #: directory of the tool's replay holds ``oggenc/oggenc`` or ``tests/run-parser-test-suite``, and the code probes
    #: for exactly that path. Its last component is ``binary_name``.
    program_path: str | None = None


@dataclass
class Recording:
    """What the recorder reported for one case (a failure, or the JSON document) and its sandbox paths."""

    data: dict[str, Any] | None
    failure: tuple[str, str] | None
    root: bytes = b""
    tmp: bytes = b""
    home: bytes = b""


SANDBOX_PREFIX = "concollmic-record-"


def _remove_tree(path: str) -> None:
    """Remove a sandbox directory made by :func:`record_code`, including directories the code made unreadable."""

    if not os.path.basename(path).startswith(SANDBOX_PREFIX) or os.path.islink(path):
        raise AdapterError(f"refusing to remove {path!r}: not a recording sandbox")
    for directory, names, _files in os.walk(path):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        for name in names:
            child = os.path.join(directory, name)
            if not os.path.islink(child):
                try:
                    os.chmod(child, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)


def record_code(code: str, seed: int, settings: RecordSettings | None = None) -> Recording:
    """Run ``execute_program`` of one test case in record mode (see the module text)."""

    settings = settings or RecordSettings()
    sandbox = os.path.realpath(tempfile.mkdtemp(prefix=SANDBOX_PREFIX, dir=settings.work_dir))
    try:
        paths = {name: os.path.join(sandbox, name) for name in ("root", "tmp", "home", "ctl")}
        for path in paths.values():
            os.mkdir(path, 0o700)
        recorder = os.path.join(paths["ctl"], "recorder.py")
        job_path = os.path.join(paths["ctl"], "job.json")
        result_path = os.path.join(paths["ctl"], "result.json")
        Path(recorder).write_text(_RECORDER_SOURCE, encoding="utf-8")
        relative = settings.program_path or settings.binary_name
        program_path = None
        if settings.program_binary and settings.binary_name:
            program_path = os.path.join(paths["root"], relative)
            os.makedirs(os.path.dirname(program_path), mode=0o700, exist_ok=True)
            shutil.copyfile(settings.program_binary, program_path)
            os.chmod(program_path, 0o755)
        job = {
            "code": code,
            "root": paths["root"],
            "tmp": paths["tmp"],
            "home": paths["home"],
            "result": result_path,
            "seed": seed,
            "call_timeout": settings.call_timeout,
            "max_runs": settings.max_runs,
            "allow_native": settings.allow_native,
            "program_path": program_path,
            "assume_exists": list(settings.assume_exists)
            + ([os.path.join(paths["root"], relative)] if settings.binary_name and not program_path else []),
            "max_blob": MAX_BLOB_BYTES,
            "cpu_s": int(math.ceil(settings.timeout_s)) + 5,
        }
        Path(job_path).write_text(json.dumps(job), encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": paths["home"],
            "TMPDIR": paths["tmp"],
            "LANG": "C",
            "TZ": "UTC",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = subprocess.Popen(
            [settings.python or sys.executable, "-s", "-B", recorder, job_path],
            cwd=paths["root"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        failure: tuple[str, str] | None = None
        try:
            process.wait(timeout=settings.timeout_s)
        except subprocess.TimeoutExpired:
            failure = (K_RECORDER_TIMEOUT, f"more than {settings.timeout_s:g} s")
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)  # nothing the code started may outlive the case
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
        recording = Recording(None, failure, paths["root"].encode(), paths["tmp"].encode(), paths["home"].encode())
        if failure is not None:
            return recording
        try:
            raw = Path(result_path).read_bytes()
        except OSError:
            recording.failure = (K_RECORDER_FAILED, f"the recorder ended with status {process.returncode} and no result")
            return recording
        try:
            recording.data = json.loads(raw)
        except ValueError as exc:
            recording.failure = (K_RECORDER_FAILED, f"unreadable result: {exc}")
        return recording
    finally:
        _remove_tree(sandbox)


# ============================================================================================
# From recorded runs to cases
# ============================================================================================


@dataclass(frozen=True)
class Problem:
    """Why a run cannot be written. ``lossy`` problems are dropped when ``keep_lossy`` is set."""

    kind: str
    detail: str = ""
    lossy: bool = False

    def text(self) -> str:
        return f"{self.kind}: {self.detail}" if self.detail else self.kind


@dataclass
class CaseSpec:
    """One case, before it is written."""

    argv: list[bytes]
    stdin: bytes | None
    files: list[dict[str, Any]]
    env: dict[str, bytes]
    cwd: bytes
    timeout_ms: int
    lossy: list[str] = field(default_factory=list)
    stdin_tty: bool = False


@dataclass
class MapSettings:
    """What the mapping from a recorded run to a case needs to know."""

    binary_name: bytes
    project_dirs: tuple[bytes, ...] = ()
    skip_under: tuple[bytes, ...] = ()
    keep_lossy: bool = False


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)


def _blob(recording: Recording, digest: str) -> bytes:
    return base64.b64decode((recording.data or {}).get("blobs", {})[digest])


def _under(path: bytes, prefix: bytes) -> bool:
    prefix = prefix.rstrip(b"/") or b"/"
    return path == prefix or path.startswith(prefix.rstrip(b"/") + b"/")


class PathMap:
    """Absolute paths of the recorder's sandbox (and of the program's working tree) to paths in the case root."""

    def __init__(self, root: bytes, tmp: bytes, home: bytes, project_dirs: Sequence[bytes] = ()):
        self.root = root.rstrip(b"/")
        self.tmp = tmp.rstrip(b"/")
        self.home = home.rstrip(b"/")
        self.project_dirs = tuple(p.rstrip(b"/") for p in project_dirs)

    def sandbox(self, path: bytes) -> bytes | None:
        """The path below the case root of a path in the sandbox, or ``None``."""

        path = posixpath.normpath(path) if path else path
        for base in (self.root, self.tmp):
            if path == base:
                return b"."
            if path.startswith(base + b"/"):
                return path[len(base) + 1 :]
        return None

    def project(self, path: bytes) -> bytes | None:
        """The path below the working tree of the program, for a path that names that tree, or ``None``."""

        path = posixpath.normpath(path) if path else path
        prefixes = list(self.project_dirs)
        found = PROJECT_DIR_RE.match(path)
        if found:
            prefixes.append(found.group(1))
        for prefix in prefixes:
            if path == prefix:
                return b"."
            if path.startswith(prefix + b"/"):
                return path[len(prefix) + 1 :]
        return None

    def in_home(self, path: bytes) -> bool:
        return path == self.home or path.startswith(self.home + b"/")

    def mentions_sandbox(self, data: bytes) -> bool:
        return any(base in data for base in (self.root, self.tmp, self.home))


def _timeout_ms(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
        return max(1, min(MAX_TIMEOUT_MS, int(math.ceil(value * 1000))))
    return DEFAULT_TIMEOUT_MS


def map_run(
    run: dict[str, Any], recording: Recording, base_env: dict[bytes, bytes], settings: MapSettings
) -> tuple[CaseSpec | None, list[Problem], list[str]]:
    """Turn one recorded program run into a case.

    Returns ``(spec, problems, notes)``. ``spec`` is ``None`` when the run cannot be written;
    ``problems`` lists every reason, ``notes`` the harmless differences that were kept.
    """

    problems: list[Problem] = []
    notes: list[str] = []
    paths = PathMap(recording.root, recording.tmp, recording.home, settings.project_dirs)
    argv = [_unb64(item) for item in run["argv"]]
    executable = _unb64(run["executable"]) if run.get("executable") else None
    program = executable if executable is not None else (argv[0] if argv else b"")
    name = program.rsplit(b"/", 1)[-1]
    if name != settings.binary_name:
        return None, [Problem(K_HELPER, name.decode("utf-8", "replace")[:80])], notes
    cwd_abs = _unb64(run["cwd"])
    for prefix in settings.skip_under:
        if (program.startswith(b"/") and _under(program, prefix)) or _under(cwd_abs, prefix):
            return None, [Problem(K_SKIPPED, f"runs under {prefix.decode('utf-8', 'replace')}")], notes

    # working directory
    cwd_rel = paths.sandbox(cwd_abs)
    if cwd_rel is None:
        cwd_rel = paths.project(cwd_abs)
        if cwd_rel is not None:
            notes.append(f"working directory {cwd_abs.decode('utf-8', 'replace')} read as the case root")
            cwd_rel = b"."
        else:
            problems.append(Problem(K_CWD, cwd_abs.decode("utf-8", "replace")[:120], lossy=True))
            cwd_rel = b"."

    # arguments
    def rewrite_path(token: bytes) -> bytes:
        relative = paths.sandbox(token)
        if relative is None:
            relative = paths.project(token)
            if relative is not None:
                notes.append(f"argument {token.decode('utf-8', 'replace')[:60]} read as a path in the working tree")
        if relative is None:
            if paths.in_home(token):
                problems.append(Problem(K_FILES, "an argument names a path in the home directory", lossy=True))
            return token
        rewritten = posixpath.relpath(relative, cwd_rel)
        if token.endswith(b"/") and not rewritten.endswith(b"/"):
            rewritten += b"/"  # a trailing slash is part of the argument (krep tests for it)
        return rewritten

    arguments: list[bytes] = []
    for token in argv[1:]:
        if token.startswith(b"/"):
            arguments.append(rewrite_path(token))
        elif token.startswith(b"-") and b"=/" in token:
            head, _, tail = token.partition(b"=")
            arguments.append(head + b"=" + rewrite_path(tail))
        else:
            arguments.append(token)
    if any(paths.mentions_sandbox(token) for token in arguments):
        problems.append(Problem(K_SANDBOX_PATH, "an argument contains the path of the recording sandbox"))

    # environment
    env_now = {_unb64(k): _unb64(v) for k, v in run["env"]}
    env: dict[str, bytes] = {}
    for key, value in sorted(env_now.items()):
        if base_env.get(key) == value:
            continue
        text = key.decode("utf-8", "replace")
        if not ENV_NAME_RE.match(text) or env_name_forbidden(text):
            problems.append(Problem(K_ENV, text, lossy=True))
        elif paths.mentions_sandbox(value):
            problems.append(Problem(K_SANDBOX_PATH, f"environment variable {text} contains the path of the recording sandbox"))
        else:
            env[text] = value
    dropped = [key.decode("utf-8", "replace") for key in MANAGED_ENV if key in base_env and key not in env_now]
    if dropped:
        notes.append("environment without " + ", ".join(dropped))

    # standard input
    stdin_info = run.get("stdin") or {"mode": "empty"}
    stdin: bytes | None = None
    stdin_tty = False
    if stdin_info["mode"] == "tty":
        if WRITER_HAS_TTY:
            stdin_tty, stdin = True, b""  # a terminal that nothing was typed on
        else:
            problems.append(Problem(K_TTY, "standard input is a terminal", lossy=True))
    elif stdin_info["mode"] == "data":
        if stdin_info.get("too_large"):
            problems.append(Problem(K_FORMAT, f"standard input of {stdin_info.get('size')} bytes exceeds the blob limit of {MAX_BLOB_BYTES}"))
        else:
            stdin = _blob(recording, stdin_info["blob"]) or None

    # what a case cannot say about the process
    if run.get("rlimits"):
        problems.append(Problem(K_LIMITS, ", ".join(f"{name}={v[0]}" for name, v in sorted(run["rlimits"].items())), lossy=True))
    for text in run.get("unsupported", []):
        problems.append(Problem(K_LIMITS, text, lossy=True))
    timeout_ms = _timeout_ms(run.get("timeout"))
    if run.get("tree_truncated"):
        problems.append(Problem(K_FORMAT, "the code created more files than a snapshot holds"))

    # files
    files: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for entry in run.get("tree", []):
        absolute = _unb64(entry["path"])
        relative = paths.sandbox(absolute)
        if relative is None or relative == b".":
            continue
        if relative in seen:
            problems.append(Problem(K_FORMAT, f"{relative.decode('utf-8', 'replace')[:80]} exists in both the working and the temporary directory"))
            continue
        seen.add(relative)
        kind = entry["kind"]
        mode = format(entry["mode"] & 0o777, "04o")
        if kind == "dir":
            files.append({"path": relative, "kind": "dir", "mode": mode})
        elif kind == "file":
            if "blob" not in entry:
                size = entry.get("size", 0)
                reason = entry.get("unreadable") or f"{size} bytes"
                problems.append(Problem(K_FORMAT, f"file {relative.decode('utf-8', 'replace')[:80]} cannot be held ({reason})"))
                continue
            files.append({"path": relative, "kind": "file", "mode": mode, "body": _blob(recording, entry["blob"])})
        elif kind == "symlink":
            target = _unb64(entry["target"])
            if target.startswith(b"/"):
                inside = paths.sandbox(target)
                if inside is None:
                    problems.append(Problem(K_FILES, f"symbolic link to {target.decode('utf-8', 'replace')[:80]}", lossy=True))
                    continue
                target = posixpath.relpath(inside, posixpath.dirname(relative) or b".")
            files.append({"path": relative, "kind": "symlink", "target": target})
        else:
            problems.append(Problem(K_FILES, f"{kind} {relative.decode('utf-8', 'replace')[:80]}", lossy=True))
    files.sort(key=lambda item: item["path"])

    # the limits of the format, and a tree the runner would accept
    if not any(p.kind in (K_SANDBOX_PATH, K_FORMAT) for p in problems):
        reason = precheck_case(argv=arguments, stdin=stdin, files=files, env=env)
        if reason is None and path_problems(cwd_rel, allow_dot=True):
            reason = f"working directory {cwd_rel!r} is not a valid path"
        if reason is None:
            tree_errors = check_tree(_case_for_check(arguments, files, env, cwd_rel, timeout_ms))
            if tree_errors:
                reason = tree_errors[0].message
        if reason is not None:
            problems.append(Problem(K_FORMAT, reason))

    hard = [p for p in problems if not p.lossy]
    lossy = [p for p in problems if p.lossy]
    if hard or (lossy and not settings.keep_lossy):
        return None, problems, notes
    spec = CaseSpec(arguments, stdin, files, env, cwd_rel, timeout_ms, [p.text() for p in lossy], stdin_tty)
    return spec, problems, notes


def _case_for_check(argv: list[bytes], files: list[dict[str, Any]], env: dict[str, bytes], cwd: bytes, timeout_ms: int) -> Case:
    entries = []
    for item in files:
        mode = int(item.get("mode", "0644"), 8)
        entries.append(
            FileEntry(
                path=item["path"],
                kind=item["kind"],
                mode=0 if item["kind"] == "symlink" else mode,
                blob="0" * 64 if item["kind"] in ("file", "fifo") else None,
                target=item.get("target"),
            )
        )
    return Case(
        id="check", t_ms=0, argv=tuple(argv), stdin=None, files=tuple(entries),
        env=tuple((k, v) for k, v in env.items()), cwd=cwd, timeout_ms=timeout_ms, origin=None,
    )


# ============================================================================================
# One queue file
# ============================================================================================


@dataclass
class SourceOutcome:
    """What became of one queue file."""

    name: str
    t_ms: int
    status: str  # recorded | recorded-exception | failed
    specs: list[CaseSpec] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)  # for the runs that were left out, or the whole file
    notes: list[str] = field(default_factory=list)
    runs: int = 0
    exception: str | None = None


def record_source(case: QueueCase, t_ms: int, record: RecordSettings, mapping: MapSettings) -> SourceOutcome:
    """Record one queue file and map its program runs to cases."""

    outcome = SourceOutcome(case.name, t_ms, "failed")
    if case.problem:
        kind, _, detail = case.problem.partition(": ")
        outcome.problems.append(Problem(kind, detail))
        return outcome
    if case.exec_code is None:
        outcome.problems.append(Problem(K_NO_CODE))
        return outcome
    seed = zlib.crc32(case.name.encode("utf-8", "replace")) & 0x7FFFFFFF
    recording = record_code(case.exec_code, seed, record)
    if recording.failure is not None:
        outcome.problems.append(Problem(*recording.failure))
        return outcome
    data = recording.data or {}
    if "internal_error" in data:
        outcome.problems.append(Problem(K_RECORDER_FAILED, data["internal_error"].strip().splitlines()[-1][:200]))
        return outcome
    if "syntax_error" in data:
        outcome.problems.append(Problem(K_SYNTAX, data["syntax_error"]))
        return outcome
    if data.get("missing_function"):
        outcome.problems.append(Problem(K_NO_FUNCTION))
        return outcome
    state = data.get("outcome", {})
    runs = data.get("runs", [])
    outcome.runs = len(runs)
    outcome.notes.extend(data.get("notes", []))
    if state.get("state") == "aborted":
        reason = state.get("reason", "")
        kind = K_SHELL if reason.startswith("shell syntax") else K_TOO_MANY_RUNS if reason.startswith("more than") else K_RECORDER_FAILED
        outcome.problems.append(Problem(kind, reason))
        return outcome
    if state.get("state") == "raised":
        outcome.exception = f"{state.get('type')}: {state.get('message')}"
    if not runs:
        if outcome.exception is not None:
            sandbox = "record mode:" in (state.get("message") or "")
            outcome.problems.append(Problem(K_SANDBOX if sandbox else K_RAISED, outcome.exception[:300]))
        else:
            outcome.problems.append(Problem(K_NO_RUN))
        return outcome

    base_env = {_unb64(k): _unb64(v) for k, v in data.get("base_env", [])}
    helpers = sorted({_run_name(run) for run in runs} - {mapping.binary_name})
    helper_text = ", ".join(h.decode("utf-8", "replace")[:30] for h in helpers)
    for run in runs:
        if _run_name(run) != mapping.binary_name:
            outcome.problems.append(Problem(K_HELPER, _run_name(run).decode("utf-8", "replace")[:80]))
            continue
        spec, problems, notes = map_run(run, recording, base_env, mapping)
        outcome.notes.extend(notes)
        if spec is not None and helpers:
            # the program may depend on what the other program did
            if not mapping.keep_lossy:
                outcome.problems.append(Problem(K_HELPER, "the code also runs " + helper_text, lossy=True))
                continue
            spec.lossy.append(f"{K_HELPER}: the code also runs {helper_text}")
        if spec is None:
            outcome.problems.extend(problems)
            continue
        outcome.specs.append(spec)
    outcome.status = "recorded-exception" if outcome.exception else "recorded"
    if not outcome.specs:
        outcome.status = "failed"
    return outcome


def _run_name(run: dict[str, Any]) -> bytes:
    executable = _unb64(run["executable"]) if run.get("executable") else None
    argv = [_unb64(item) for item in run["argv"]]
    program = executable if executable is not None else (argv[0] if argv else b"")
    return program.rsplit(b"/", 1)[-1]


# ============================================================================================
# The conversion
# ============================================================================================


@dataclass
class ConcoReport(AdapterReport):
    """Counts of what record mode did, on top of the counts every adapter reports."""

    recorded_sources: int = 0
    recorded_with_exception: int = 0
    failed_sources: int = 0
    runs_recorded: int = 0
    runs_written: int = 0
    runs_left_out: int = 0
    multi_run_sources: int = 0
    lossy_kept: int = 0
    beyond_budget: int = 0
    failed_by_reason: Counter = field(default_factory=Counter)
    left_out_by_reason: Counter = field(default_factory=Counter)
    note_kinds: Counter = field(default_factory=Counter)
    sources: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        document = super().to_json()
        document.update(
            recorded_sources=self.recorded_sources,
            recorded_with_exception=self.recorded_with_exception,
            failed_sources=self.failed_sources,
            runs_recorded=self.runs_recorded,
            runs_written=self.runs_written,
            runs_left_out=self.runs_left_out,
            multi_run_sources=self.multi_run_sources,
            lossy_kept=self.lossy_kept,
            beyond_budget=self.beyond_budget,
            failed_by_reason=dict(self.failed_by_reason.most_common()),
            left_out_by_reason=dict(self.left_out_by_reason.most_common()),
            note_kinds=dict(self.note_kinds.most_common()),
        )
        return document


def _note_kind(note: str) -> str:
    return re.sub(r"[0-9]+|/[^ ]*", "N", note)[:80]


def convert_directory(
    input_dir: Path | str,
    out_dir: Path | str,
    info: ToolInfo,
    *,
    program: Program | None = None,
    binary_name: str | None = None,
    start_time_ms: int | None = None,
    drop_after_budget: bool = False,
    project_dirs: Sequence[str] = (),
    skip_runs_under: Sequence[str] = (),
    keep_lossy: bool = False,
    jobs: int | None = None,
    record_timeout: float = DEFAULT_RECORD_TIMEOUT,
    call_timeout: int = DEFAULT_CALL_TIMEOUT,
    max_runs: int = DEFAULT_MAX_RUNS,
    allow_native: bool = False,
    assume_exists: Sequence[str] = (),
    program_binary: Path | str | None = None,
    program_path: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    work_dir: Path | str | None = None,
    report_path: Path | str | None = None,
) -> ConcoReport:
    """Write a bundle from the queue directory of a ConcoLLMic run (see the module text)."""

    if info.track == "fixed":
        raise AdapterError(
            "ConcoLLMic's code chooses arguments, files and environment itself; use the open track (the default)"
        )
    name = binary_name or (posixpath.basename(program.binary) if program is not None else None)
    if not name:
        raise AdapterError("the program's binary name is unknown: pass the program definition or --binary-name")
    if program_path is not None:
        normal = posixpath.normpath(program_path)
        if normal.startswith("/") or normal == ".." or normal.startswith("../") or posixpath.basename(normal) != name:
            raise AdapterError(f"--program-path must be a relative path inside the working directory that ends in {name!r}, not {program_path!r}")
        program_path = normal
    queue = load_queue(input_dir)
    if not queue:
        raise AdapterError("no ConcoLLMic cases (files named id:*.yaml) found in the directory")
    writer = new_writer(out_dir, info)  # refuses a directory that is not empty before any case is run

    # time
    stamped = [case.create_ms for case in queue if case.create_ms is not None]
    start_ms = start_time_ms if start_time_ms is not None else (min(stamped) if stamped else 0)
    report = ConcoReport(source_cases=len(queue))
    budget_ms = (info.wall_s or 0) * 1000
    mapping = MapSettings(
        binary_name=name.encode("utf-8"),
        project_dirs=tuple(p.encode("utf-8") for p in project_dirs),
        skip_under=tuple(p.encode("utf-8") for p in skip_runs_under),
        keep_lossy=keep_lossy,
    )
    record = RecordSettings(
        work_dir=None if work_dir is None else str(work_dir),
        timeout_s=record_timeout,
        call_timeout=call_timeout,
        max_runs=max_runs,
        allow_native=allow_native,
        assume_exists=tuple(assume_exists),
        binary_name=name,
        program_binary=None if program_binary is None else str(program_binary),
        program_path=program_path,
    )

    outcomes: list[SourceOutcome | None] = [None] * len(queue)
    pending: list[tuple[int, QueueCase, int]] = []
    for position, case in enumerate(queue):
        if case.create_ms is None and not case.problem:
            outcomes[position] = SourceOutcome(case.name, 0, "failed", problems=[Problem(K_NO_TIME)])
            continue
        t_ms = max(0, (case.create_ms or 0) - start_ms)
        if drop_after_budget and budget_ms and t_ms > budget_ms:
            outcomes[position] = SourceOutcome(case.name, t_ms, "failed", problems=[Problem(K_BEYOND, f"t_ms {t_ms}")])
            report.beyond_budget += 1
            continue
        pending.append((position, case, t_ms))

    workers = max(1, jobs or min(4, os.cpu_count() or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(record_source, case, t_ms, record, mapping): position for position, case, t_ms in pending
        }
        for future in concurrent.futures.as_completed(futures):
            outcomes[futures[future]] = future.result()

    # write, in time order
    ordered = sorted(
        (o for o in outcomes if o is not None), key=lambda o: (o.t_ms, o.name)
    )
    index_of = {case.name: case.index for case in queue}
    for outcome in ordered:
        if outcome.runs:
            report.runs_recorded += outcome.runs
        for note in outcome.notes:
            report.note_kinds[_note_kind(note)] += 1
        label = outcome.name
        if outcome.status == "failed":
            report.failed_sources += 1
            reasons = sorted({p.kind for p in outcome.problems}) or ["unknown"]
            for reason in reasons:
                report.failed_by_reason[reason] += 1
            report.skip(label, "; ".join(p.text() for p in outcome.problems)[:400] or "unknown")
        else:
            if outcome.status == "recorded-exception":
                report.recorded_with_exception += 1
            else:
                report.recorded_sources += 1
            if len(outcome.specs) > 1:
                report.multi_run_sources += 1
            left_out = outcome.runs - len(outcome.specs)
            if left_out > 0:
                report.runs_left_out += left_out
                for reason in sorted({p.kind for p in outcome.problems}):
                    report.left_out_by_reason[reason] += 1
                report.skip(label, f"{left_out} run(s) left out: " + "; ".join(p.text() for p in outcome.problems)[:300])
            for number, spec in enumerate(outcome.specs):
                origin = f"concollmic:{index_of.get(label)}"
                if len(outcome.specs) > 1:
                    origin += f".{number}"
                if spec.lossy:
                    report.lossy_kept += 1
                    origin += ":lossy"
                tty = {"stdin_tty": True} if spec.stdin_tty else {}
                writer.add_case(
                    t_ms=outcome.t_ms,
                    argv=spec.argv,
                    stdin=spec.stdin,
                    **tty,
                    files=spec.files,
                    env={k: v for k, v in spec.env.items()},
                    cwd=spec.cwd,
                    timeout_ms=min(spec.timeout_ms, timeout_ms),
                    origin=origin[:64],
                )
                report.written_cases += 1
                report.runs_written += 1
        report.sources.append(_source_document(outcome, index_of.get(label)))
    if report.beyond_budget:
        report.notes.append(f"{report.beyond_budget} case(s) after the budget were left out (--drop-after-budget)")
    if not stamped and start_time_ms is None:
        report.notes.append("no case has a create_time: every case is at t_ms 0")
    writer.tool["notes"] = (
        (info.notes + " " if info.notes else "")
        + f"Recorded from ConcoLLMic exec_code in record mode: {report.source_cases} source case(s), "
        f"{report.recorded_sources + report.recorded_with_exception} recorded, {report.failed_sources} left out."
    )
    writer.finish()
    if report_path is not None:
        Path(report_path).write_text(json.dumps({**report.to_json(), "sources": report.sources}, indent=1) + "\n", encoding="utf-8")
    return report


def _source_document(outcome: SourceOutcome, index: int | None) -> dict[str, Any]:
    return {
        "file": outcome.name,
        "id": index,
        "t_ms": outcome.t_ms,
        "status": outcome.status,
        "runs": outcome.runs,
        "cases": len(outcome.specs),
        "problems": [p.text() for p in outcome.problems],
        "notes": sorted(set(outcome.notes)),
        "exception": outcome.exception,
    }


# ============================================================================================
# Command line
# ============================================================================================


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The options of ``adapt concollmic`` (the common ones come from ``_add_adapt_common``)."""

    parser.add_argument("input", metavar="QUEUE_DIR", help="the queue directory of a ConcoLLMic run (files named id:*.yaml)")
    parser.add_argument(
        "--start-time",
        metavar="TIME",
        help="campaign start, in the clock of create_time ('2026-09-25 16:55:20.350' or epoch seconds read as UTC); "
        "t_ms is create_time minus this (default: the earliest create_time, so the first case is at 0, which is NOT the campaign start)",
    )
    parser.add_argument(
        "--drop-after-budget",
        action="store_true",
        help="leave out cases whose t_ms is beyond --wall-s (the standard profile never replays them; canonical-2026 does)",
    )
    parser.add_argument("--binary-name", metavar="NAME", help="file name of the program the code runs (default: from the program definition)")
    parser.add_argument(
        "--project-dir",
        action="append",
        default=[],
        metavar="PATH",
        help="an absolute directory the code uses for the program's working tree, besides the /<name>-instr and /<name>-gcov of ConcoLLMic's images (repeatable)",
    )
    parser.add_argument(
        "--skip-runs-under",
        action="append",
        default=[],
        metavar="PATH",
        help="leave out program runs whose executable or working directory lies below PATH, for example the "
        "tool's own instrumented build, which a replay of the coverage build does not measure (repeatable)",
    )
    parser.add_argument("--keep-lossy", action="store_true", help="write a case that record mode can only express in part (terminal, resource limits, reserved variables, other programs) without the part it cannot express; the origin ends in ':lossy'")
    parser.add_argument("--jobs", type=int, metavar="N", help="cases recorded at once (default: 4 or the number of cores)")
    parser.add_argument("--record-timeout", type=float, default=DEFAULT_RECORD_TIMEOUT, metavar="SECONDS", help=f"wall-clock limit for recording one case (default {DEFAULT_RECORD_TIMEOUT:g})")
    parser.add_argument("--call-timeout", type=int, default=DEFAULT_CALL_TIMEOUT, metavar="SECONDS", help=f"value passed to execute_program(timeout) (default {DEFAULT_CALL_TIMEOUT}, the limit of the standard replay)")
    parser.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS, metavar="N", help=f"program runs one case may ask for (default {DEFAULT_MAX_RUNS})")
    parser.add_argument(
        "--assume-exists",
        action="append",
        default=[],
        metavar="PATH",
        help="a path that existed where the case was found and that its code may probe for, such as "
        "/woff2-instr/woff2_decompress (end a directory with '/'); stat, access and chdir answer as if it were there (repeatable)",
    )
    parser.add_argument(
        "--program-binary",
        metavar="FILE",
        help="a copy of the program (for example the binary of the coverage build) to place as ./NAME (or at --program-path) in the "
        "working directory of every recording, for code that reads, copies or measures it; without it the program only appears to exist",
    )
    parser.add_argument(
        "--program-path",
        metavar="REL",
        help="where the program sits relative to the working directory when that is not ./NAME, for programs whose binary lies "
        "in a subdirectory of the project directory of the tool's replay (oggenc/oggenc, tests/run-parser-test-suite); "
        "used by --program-binary and by the probes of the code",
    )
    parser.add_argument("--allow-native-code", action="store_true", help="let the code load shared libraries with ctypes (some cases build their input with the system's Brotli library); off by default, because native code is not covered by the guard")
    parser.add_argument("--work-dir", metavar="DIR", help="parent directory of the recording sandboxes (default: the system temporary directory)")
    parser.add_argument("--report", metavar="FILE.json", help="write the full report, with one entry per queue file, to this file")


def _parse_start(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(round(float(text) * 1000))
    except ValueError:
        pass
    try:
        return parse_time_ms(text)
    except ValueError as exc:
        raise AdapterError(f"--start-time must be epoch seconds or a timestamp such as '2026-09-25 16:55:20.350': {exc}") from exc


def convert_from_args(args: argparse.Namespace, info: ToolInfo, program: Program | None) -> ConcoReport:
    return convert_directory(
        args.input,
        args.out,
        info,
        program=program,
        binary_name=args.binary_name,
        start_time_ms=_parse_start(args.start_time),
        drop_after_budget=args.drop_after_budget,
        project_dirs=args.project_dir,
        skip_runs_under=args.skip_runs_under,
        keep_lossy=args.keep_lossy,
        jobs=args.jobs,
        record_timeout=args.record_timeout,
        call_timeout=args.call_timeout,
        max_runs=args.max_runs,
        allow_native=args.allow_native_code,
        assume_exists=args.assume_exists,
        program_binary=args.program_binary,
        program_path=args.program_path,
        timeout_ms=args.timeout_ms,
        work_dir=args.work_dir,
        report_path=args.report,
    )
