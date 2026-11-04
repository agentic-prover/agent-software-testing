# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""KLEE ``.ktest`` files to cases.

File format (as read by KLEE's ``KTest.c``)::

    magic      "KTEST" or "BOUT\\n"                 (5 bytes)
    version    u32, big endian, 1 to 3
    numArgs    u32, then numArgs strings (u32 length + bytes)
    [version >= 2]  symArgvs u32, symArgvLen u32
    numObjects u32, then per object: name (u32 length + bytes), data (u32 length + bytes)

Version 4 (pointer metadata, KLEE 3.0 and later) is not supported.

Mapping to a case
-----------------
``args[0]`` is the program (bitcode) path and is dropped: the runner sets
``argv[0]``. The remaining arguments are the command line given to KLEE after
the bitcode. KLEE's POSIX runtime (``klee_init_env``) expands the options below
into symbolic arguments, files and standard input, whose concrete values the
test case holds as objects; the mapping resolves them the way ``klee-replay``
replays them natively:

``--sym-arg N``
    one argument; its value is the next ``argNN`` object, which holds ``N + 1`` bytes: ``klee_init_env`` makes all
    of them symbolic and then overwrites the last with NUL (``__get_sym_str``), so the argument is the first ``N``
    bytes up to the first NUL. The last byte of the object is whatever the solver chose and is never part of the
    argument (taking it would make every argument that fills its length one character too long).
``--sym-args MIN MAX LEN``
    as many arguments as the ``n_args`` object says; there is no ``n_args`` object when MIN == MAX
    (``klee_range`` returns the one possible value without making a symbolic object), and the objects of a later
    ``--sym-args`` must not be taken for it. Values from the next ``argNN`` objects, read like ``--sym-arg LEN``.
``--sym-files N SIZE``
    N files named ``A``, ``B``, ...; the body is the ``A-data`` (or
    ``A_data``) object. The type and mode come from ``A-data-stat`` (or
    ``A_data_stat``) when that holds a Linux x86-64 ``struct stat``: a regular
    file keeps its permission bits, a character device becomes a ``tty`` entry (a
    pseudo-terminal fed with the bytes, as ``klee-replay`` does), a directory
    becomes a ``dir`` entry, and anything else (invalid type bits, a socket, a
    block device) is written as a regular file with a note, which is what
    ``klee-replay`` makes of it (``0644`` when the permission bits are zero).
    ``klee-replay`` makes no file for a named pipe, so the adapter leaves it
    out. It stops before it starts the program when the stat object records a
    symbolic link (``create_link`` passes the replay directory to ``create_file``
    twice, so ``NAME.lnk`` cannot be created) or more than one link of a file
    that is not a directory (``link`` is called with a name relative to its own
    working directory): the earlier pipeline has no coverage for such a test, and
    the adapter skips it, with the reason in the report. ``klee_replay=False``
    describes what the test says instead (a link to the data up to its first NUL,
    KLEE's own ``readlink`` model, and a regular file for a named pipe; nothing
    is skipped).
``--sym-stdin N``
    standard input is the ``stdin`` object, and its ``stdin_stat`` object decides how ``klee-replay`` delivers it
    (``create_file`` in ``tools/klee-replay/file-creator.c``, tested in this order): a symbolic link makes it stop
    like for a symbolic file; a directory makes a directory ``fd0`` in the working directory and leaves standard
    input alone (the adapter writes the directory and no standard input); a character device is a pseudo-terminal
    (``stdin_tty``); a named pipe, or a standard input whose type bits are zero (a rule of ``klee-replay`` for
    descriptor 0 alone), is a pipe, which the case says with ``stdin_pipe`` (and there is
    no ``fd0``). Every other type, a regular file, a socket, a block device or invalid type bits, is a regular file
    that ``klee-replay`` gives the access and modification time of the stat object (``futimes``), and the case
    says so with ``stdin_atime`` and ``stdin_mtime``. A test without a ``stdin_stat`` object has an ordinary
    standard input file.
``--sym-stdout``, ``--fd-fail``, ``--max-fail N``, ``--save-all-writes``, ``--bout-file F``
    runtime options without a counterpart; dropped.

Once the arguments are resolved ``klee-replay`` cuts every argument of exactly two characters that starts
with ``A`` to ``A`` (``klee-replay.c`` does it to the strings it prints, and those are the ones it executes);
the adapter does the same.

Every other argument is concrete and kept in place. The optional sidecar
``<name>.ktest.env`` (``KEY=VALUE`` lines, as Agolic writes next to each KTest
it replays) becomes the case's environment; names a case may not
set (SPEC.md section 5.7) are dropped and noted. ``LANG``, ``LC_ALL`` and ``TZ`` are not among them: a test generator can set
them on purpose (``LC_ALL=C`` over ``LC_CTYPE=C.UTF-8``, ``TZ=NPT-5:45``), the program then takes other paths, and the
case keeps them.

Directories
-----------
KLEE writes its tests into ``klee-out-N/`` directories, and a collection of
campaigns is a tree of them. The converter walks the tree below the given
directory (symbolic links are not followed). A test is identified by its path
relative to that directory, written with ``/``; ``--times`` keys are these
relative paths, because file names repeat across directories.

Times
-----
``t_ms`` is the discovery time of the test relative to the campaign start. KLEE
does not record it; the file modification time is the only trace, and it is
only meaningful when the files have not been copied (a copy that does not
preserve times gives every file the copy time). The public replication
corpora of the reference evaluation were repacked without their times, so
converting them without an external ``--times`` file gives wrong times. When
neither a start time nor explicit times are given, the oldest file is taken as
time 0, which is not the campaign start, and the report says so.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..bundle import BundleWriter
from ..common import DEFAULT_TIMEOUT_MS, ENV_NAME_RE, MAX_CASES, MAX_PATH_BYTES, env_name_forbidden, value_has_credentials
from .common import AdapterError, AdapterReport, ToolInfo, new_writer, precheck_case

#: The ``origin`` of the cases this adapter writes (``origin`` is informative; no part of a replay reads it, SPEC.md 5.4).
KLEE_ORIGIN = "klee"
MAGICS = (b"KTEST", b"BOUT\n")
MAX_KTEST_FILE_BYTES = 64 * 1024 * 1024
MAX_KTEST_COUNT = 1_000_000
MAX_KTEST_TEXT_BYTES = 16 * 1024 * 1024
MAX_SIDECAR_BYTES = 16 * 1024 * 1024
SUPPORTED_VERSIONS = (1, 2, 3)

#: KLEE POSIX runtime options and how many values follow each.
RUNTIME_OPTIONS: dict[str, int] = {
    "sym-arg": 1,
    "sym-args": 3,
    "sym-files": 2,
    "sym-stdin": 1,
    "sym-stdout": 0,
    "fd-fail": 0,
    "max-fail": 1,
    "save-all-writes": 0,
    "bout-file": 1,
}
_ARG_OBJECT = re.compile(rb"arg[0-9]+\Z")
_STAT_BYTES = 144
_ST_NLINK_OFFSET = 16
_ST_MODE_OFFSET = 24
_ST_ATIME_OFFSET = 72  # st_atim.tv_sec
_ST_MTIME_OFFSET = 88  # st_mtim.tv_sec
#: File type bits of ``st_mode``, written out so that no platform's ``S_ISREG``
#: (which raises ``OverflowError`` for large values on some systems) is involved.
_S_IFMT = 0o170000
_S_IFREG = 0o100000
_S_IFDIR = 0o040000
_S_IFLNK = 0o120000
_S_IFCHR = 0o020000
_S_IFIFO = 0o010000
_OTHER_TYPES = {0o140000: "socket", 0o060000: "block device", 0o010000: "named pipe"}
#: What ``klee-replay`` does with a standard input whose ``stdin_stat`` object records a directory when the
#: mapping is not the replay's (``klee_replay=False``): it makes a directory ``fd0`` and leaves standard input as it
#: found it, which a case cannot express, so the bytes stay ordinary standard input and the report says so.
_STDIN_TYPES = {
    0o040000: "a directory, for which klee-replay leaves standard input as it found it",
}


@dataclass
class KTest:
    version: int
    args: list[bytes]
    sym_argvs: int
    sym_argv_len: int
    objects: list[tuple[bytes, bytes]]


class KTestFormatError(AdapterError):
    """The file is not a readable KTest."""


# ---------------------------------------------------------------------------
# Binary format
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes, offset: int):
        self.data = data
        self.offset = offset

    def take(self, size: int, field: str, maximum: int | None = None) -> bytes:
        if maximum is not None and size > maximum:
            raise KTestFormatError(f"{field} length {size} exceeds limit {maximum}")
        end = self.offset + size
        if end > len(self.data):
            raise KTestFormatError("truncated ktest file")
        chunk = self.data[self.offset : end]
        self.offset = end
        return chunk

    def u32(self, field: str = "uint32") -> int:
        return struct.unpack(">I", self.take(4, field))[0]


def parse_ktest(data: bytes) -> KTest:
    """Parse a KTest (versions 1 to 3, both magics)."""

    if len(data) > MAX_KTEST_FILE_BYTES:
        raise KTestFormatError(f"ktest file larger than {MAX_KTEST_FILE_BYTES} bytes")
    if data[:5] not in MAGICS:
        raise KTestFormatError("not a KTest file (bad magic)")
    reader = _Reader(data, 5)
    version = reader.u32("version")
    if version not in SUPPORTED_VERSIONS:
        raise KTestFormatError(f"unsupported KTest version {version}")
    num_args = reader.u32("argument count")
    if num_args > MAX_KTEST_COUNT:
        raise KTestFormatError(f"argument count {num_args} exceeds limit {MAX_KTEST_COUNT}")
    args = [reader.take(reader.u32("argument length"), "argument", MAX_KTEST_TEXT_BYTES) for _ in range(num_args)]
    sym_argvs = sym_argv_len = 0
    if version >= 2:
        sym_argvs = reader.u32("symbolic argv count")
        sym_argv_len = reader.u32("symbolic argv length")
    num_objects = reader.u32("object count")
    if num_objects > MAX_KTEST_COUNT:
        raise KTestFormatError(f"object count {num_objects} exceeds limit {MAX_KTEST_COUNT}")
    objects: list[tuple[bytes, bytes]] = []
    for _ in range(num_objects):
        name = reader.take(reader.u32("object name length"), "object name", MAX_KTEST_TEXT_BYTES)
        body = reader.take(reader.u32("object data length"), "object data", MAX_KTEST_FILE_BYTES)
        objects.append((name, body))
    return KTest(version, args, sym_argvs, sym_argv_len, objects)


def encode_ktest(ktest: KTest, *, magic: bytes = b"KTEST") -> bytes:
    """Serialise a :class:`KTest` (used by tests and tools that synthesise cases)."""

    if magic not in MAGICS:
        raise ValueError("unknown magic")
    if ktest.version not in SUPPORTED_VERSIONS:
        raise ValueError(f"unsupported KTest version {ktest.version}")
    out = bytearray(magic)
    out += struct.pack(">II", ktest.version, len(ktest.args))
    for arg in ktest.args:
        out += struct.pack(">I", len(arg)) + arg
    if ktest.version >= 2:
        out += struct.pack(">II", ktest.sym_argvs, ktest.sym_argv_len)
    out += struct.pack(">I", len(ktest.objects))
    for name, body in ktest.objects:
        out += struct.pack(">I", len(name)) + name + struct.pack(">I", len(body)) + body
    return bytes(out)


# ---------------------------------------------------------------------------
# Sidecar and mapping
# ---------------------------------------------------------------------------


def parse_sidecar(payload: bytes) -> dict[str, str]:
    """Parse a ``.ktest.env`` sidecar (``KEY=VALUE`` lines).

    UTF-8 with replacement, empty lines ignored, a line without ``=`` is an
    error, a later line overrides an earlier one.
    """

    if len(payload) > MAX_SIDECAR_BYTES:
        raise AdapterError(f"environment sidecar larger than {MAX_SIDECAR_BYTES} bytes")
    values: dict[str, str] = {}
    for raw in payload.decode("utf-8", errors="replace").splitlines():
        if not raw:
            continue
        if "=" not in raw:
            raise AdapterError("malformed environment sidecar line (no '=')")
        key, value = raw.split("=", 1)
        values[key] = value
    return values


@dataclass
class MappedCase:
    argv: list[bytes]
    stdin: bytes | None
    files: list[dict]
    env: dict[str, bytes]
    notes: list[str] = field(default_factory=list)
    #: Standard input is a character device in its ``stdin_stat`` object: a pseudo-terminal in a case.
    stdin_tty: bool = False
    #: Standard input is a named pipe, or has no file type, in its ``stdin_stat`` object: a pipe in a case.
    stdin_pipe: bool = False
    #: Access and modification time (seconds) of the standard input file, from its ``stdin_stat`` object.
    stdin_atime: int | None = None
    stdin_mtime: int | None = None


def _int_arg(token: bytes, option: str) -> int:
    try:
        return int(token.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AdapterError(f"--{option} expects an integer, got {token!r}") from exc


def _c_string(body: bytes) -> bytes:
    end = body.find(b"\0")
    return body if end < 0 else body[:end]


def _stat_type_and_permissions(body: bytes | None) -> tuple[int, int] | None:
    """``(file type bits, permission bits)`` of a Linux x86-64 ``struct stat`` object, ``None`` when absent or of another size."""

    if body is None or len(body) != _STAT_BYTES:
        return None
    mode = int.from_bytes(body[_ST_MODE_OFFSET : _ST_MODE_OFFSET + 4], "little")
    return mode & _S_IFMT, mode & 0o777


def _stat_nlink(body: bytes | None) -> int:
    """``st_nlink`` of a Linux x86-64 ``struct stat`` object (0 when there is none)."""

    if body is None or len(body) != _STAT_BYTES:
        return 0
    return int.from_bytes(body[_ST_NLINK_OFFSET : _ST_NLINK_OFFSET + 8], "little")


def _stat_times(body: bytes | None) -> dict[str, int]:
    """``atime`` and ``mtime`` (seconds) of a Linux x86-64 ``struct stat`` object, as ``klee-replay`` sets them."""

    if body is None or len(body) != _STAT_BYTES:
        return {}
    return {
        "atime": int.from_bytes(body[_ST_ATIME_OFFSET : _ST_ATIME_OFFSET + 8], "little", signed=True),
        "mtime": int.from_bytes(body[_ST_MTIME_OFFSET : _ST_MTIME_OFFSET + 8], "little", signed=True),
    }


def _regular_file(path: bytes, body: bytes, stat_body: bytes | None, permissions: int | None) -> dict:
    """A regular file as ``klee-replay`` creates it: permission bits of the stat object (``0644`` when they are
    zero or there is no stat object) and its access and modification time."""

    entry = {"path": path, "kind": "file", "mode": f"{(permissions or 0o644):04o}", "body": body}
    entry.update(_stat_times(stat_body))
    return entry


def _usable_link_target(target: bytes) -> bool:
    """A relative target of a link in the case root that does not climb out of it (it may dangle)."""

    if not target or b"\0" in target or len(target) > MAX_PATH_BYTES or target.startswith(b"/"):
        return False
    depth = 0  # the link sits in the case root
    for part in target.split(b"/"):
        if part in (b"", b"."):
            continue
        if part == b"..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def _sym_file_entry(
    name: str, body: bytes, stat_body: bytes | None, notes: list[str], klee_replay: bool = True
) -> dict | None:
    """The file entry for one symbolic file, following the type its ``stat`` object records.

    With ``klee_replay`` (the default) the entry is what ``klee-replay`` leaves in the replay directory, and a test
    that makes it stop before the program starts raises :class:`AdapterError` (it contributes nothing in the earlier
    pipeline, which replayed with ``klee-replay``): a symbolic link (``create_link`` passes the replay directory to
    ``create_file`` a second time, so creating ``NAME.lnk`` fails and ``klee-replay`` exits), and a file with more
    than one link in its stat object (``create_file`` links ``NAME`` to ``NAME.link2`` relative to its own working
    directory, which fails and exits). A named pipe gets no file (``None``). Without it, a link becomes a link to the
    contents and a named pipe a regular file, which is what the test would describe to a program.
    """

    path = name.encode()
    info = _stat_type_and_permissions(stat_body)
    if info is None:
        return {"path": path, "kind": "file", "mode": "0644", "body": body}
    file_type, permissions = info
    if klee_replay:
        if file_type == _S_IFLNK:
            raise AdapterError(
                f"symbolic file {name} is a symbolic link: klee-replay fails while creating {name}.lnk "
                "and does not start the program, so the test has no coverage in the earlier pipeline"
            )
        links = _stat_nlink(stat_body)
        if links > 1 and file_type != _S_IFDIR:
            raise AdapterError(
                f"symbolic file {name} records {links} links: klee-replay fails to link it and does not start "
                "the program, so the test has no coverage in the earlier pipeline"
            )
    if file_type == _S_IFREG:
        return _regular_file(path, body, stat_body, permissions)
    if file_type == _S_IFCHR:
        # klee-replay: a symbolic link to the slave of a pseudo-terminal that is fed the bytes (isatty is true)
        return {"path": path, "kind": "tty", "body": body}
    if file_type == _S_IFDIR:
        return {"path": path, "kind": "dir", "mode": f"{permissions:04o}"}
    if file_type == _S_IFLNK:
        target = _c_string(body)
        if _usable_link_target(target):
            return {"path": path, "kind": "symlink", "target": target}
        notes.append(
            f"symbolic file {name} is a symbolic link whose contents are no usable relative target; "
            "written as a regular file"
        )
    elif file_type in _OTHER_TYPES:
        if file_type == 0o010000:  # klee-replay makes a block device or socket a regular file, a named pipe none at all
            if klee_replay:
                notes.append(f"symbolic file {name} is a named pipe, for which klee-replay creates no file; left out")
                return None
            notes.append(f"symbolic file {name} is a {_OTHER_TYPES[file_type]}, which a case cannot express; written as a regular file")
        else:
            notes.append(f"symbolic file {name} is a {_OTHER_TYPES[file_type]}, which a case cannot express; written as a regular file")
            return _regular_file(path, body, stat_body, permissions)
    else:
        notes.append(f"symbolic file {name} has invalid file type bits {file_type:#o}; written as a regular file")
        return _regular_file(path, body, stat_body, permissions)  # klee-replay: create_reg_file
    return {"path": path, "kind": "file", "mode": "0644", "body": body}


def _lookup(objects: list[tuple[bytes, bytes]], *names: str) -> bytes | None:
    wanted = {n.encode() for n in names}
    for name, body in objects:
        if name in wanted:
            return body
    return None


def map_ktest(ktest: KTest, sidecar: dict[str, str] | None = None, *, klee_replay: bool = True) -> MappedCase:
    """Resolve a KTest into argv, standard input, files and environment.

    ``klee_replay`` (the default) maps the test the way ``klee-replay`` replays it, quirks included: a test it cannot
    replay raises :class:`AdapterError`, an argument of exactly two characters that starts with ``A`` is cut to ``A``,
    a named pipe leaves no file and a directory as standard input leaves a directory ``fd0`` and no standard input
    (see the module text). With ``klee_replay=False`` the mapping describes what the test says: a symbolic link
    becomes a link to its contents, a named pipe a regular file, and arguments are kept as they are.
    """

    if not ktest.args:
        raise AdapterError("the KTest has no arguments (not even the program path)")
    sym_arg_objects = [(n, b) for n, b in ktest.objects if _ARG_OBJECT.match(n)]
    n_args_objects = [b for n, b in ktest.objects if n == b"n_args"]
    next_arg = 0
    next_n_args = 0

    def take_arg(max_len: int) -> bytes:
        """The next symbolic argument: ``klee_init_env`` allocates ``max_len + 1`` bytes, makes all of them symbolic and
        then sets the last to NUL, so the value is the first ``max_len`` bytes up to the first NUL. ``klee-replay``
        takes the objects in order and exits when one has another size, so the adapter refuses such a test."""

        nonlocal next_arg
        if next_arg >= len(sym_arg_objects):
            raise AdapterError("fewer argNN objects than symbolic arguments")
        name, body = sym_arg_objects[next_arg]
        if len(body) != max_len + 1:
            raise AdapterError(
                f"{name.decode('latin-1')} holds {len(body)} bytes but the symbolic argument has length {max_len} "
                f"({max_len + 1} bytes expected; klee-replay refuses a test whose object sizes differ)"
            )
        next_arg += 1
        return _c_string(body[:max_len])

    notes: list[str] = []
    argv: list[bytes] = []
    sym_files = 0
    wants_stdin = False
    tokens = ktest.args[1:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith(b"--"):
            option = token[2:].decode("utf-8", "replace")
        elif token.startswith(b"-"):
            option = token[1:].decode("utf-8", "replace")
        else:
            option = None
        if option in RUNTIME_OPTIONS:
            count = RUNTIME_OPTIONS[option]
            values = tokens[index + 1 : index + 1 + count]
            if len(values) != count:
                raise AdapterError(f"--{option} is missing its value(s)")
            index += 1 + count
            if option == "sym-arg":
                argv.append(take_arg(_int_arg(values[0], option)))
            elif option == "sym-args":
                minimum, maximum, length = (_int_arg(v, option) for v in values)
                if minimum == maximum:
                    amount = minimum  # klee_range(MIN, MIN + 1) makes no symbolic object
                elif next_n_args < len(n_args_objects):
                    raw = n_args_objects[next_n_args]
                    next_n_args += 1
                    if len(raw) != 4:
                        raise AdapterError(f"n_args object has {len(raw)} bytes, expected 4")
                    amount = int.from_bytes(raw, "little")
                else:
                    raise AdapterError("--sym-args without an n_args object and MIN != MAX")
                if not minimum <= amount <= maximum:
                    raise AdapterError(f"n_args {amount} outside {minimum}..{maximum}")
                argv.extend(take_arg(length) for _ in range(amount))
            elif option == "sym-files":
                sym_files = _int_arg(values[0], option)
                _int_arg(values[1], option)
                if not 0 <= sym_files <= 26:
                    raise AdapterError("--sym-files supports 0 to 26 files")
            elif option == "sym-stdin":
                _int_arg(values[0], option)
                wants_stdin = True
            continue
        argv.append(token)
        index += 1
    if next_arg < len(sym_arg_objects):
        notes.append(f"{len(sym_arg_objects) - next_arg} argNN object(s) unused")
    if klee_replay:
        # klee-replay prints every argument and, to cut a symbolic file name that was given a character too many,
        # overwrites the second character of each argument of exactly two characters that starts with 'A' with NUL
        # (``if (s[0]=='A' && s[1] && !s[2]) s[1] = '\0'``); the program is started with the shortened strings
        cut = [position for position, value in enumerate(argv) if len(value) == 2 and value[:1] == b"A"]
        for position in cut:
            argv[position] = b"A"
        if cut:
            notes.append(f"{len(cut)} argument(s) of two characters starting with A cut to A, as klee-replay does")

    files: list[dict] = []
    for position in range(sym_files):
        name = chr(ord("A") + position)
        body = _lookup(ktest.objects, f"{name}-data", f"{name}_data")
        if body is None:
            raise AdapterError(f"missing data object for symbolic file {name}")
        stat_body = _lookup(ktest.objects, f"{name}-data-stat", f"{name}_data_stat")
        entry = _sym_file_entry(name, body, stat_body, notes, klee_replay)
        if entry is not None:
            files.append(entry)

    stdin = _lookup(ktest.objects, "stdin")
    if stdin is None and wants_stdin:
        raise AdapterError("missing stdin object for --sym-stdin")
    stdin_tty = False
    stdin_pipe = False
    stdin_times: dict[str, int] = {}
    if stdin is not None:
        stdin_stat = _lookup(ktest.objects, "stdin-stat", "stdin_stat")
        info = _stat_type_and_permissions(stdin_stat)
        if info is not None and info[0] == _S_IFLNK and klee_replay:
            raise AdapterError(
                "standard input is a symbolic link: klee-replay fails while creating fd0.lnk and does not start the "
                "program, so the test has no coverage in the earlier pipeline"
            )
        if info is not None and info[0] == _S_IFCHR:
            stdin_tty = True
        elif info is not None and info[0] == _S_IFDIR and klee_replay:
            # klee-replay makes a directory fd0 (mkdir, the permission bits of the stat object) in the working
            # directory and leaves standard input as the replay found it
            files.append({"path": b"fd0", "kind": "dir", "mode": f"{info[1]:04o}"})
            stdin = None
            notes.append("standard input is a directory: a directory fd0 is made and standard input is left alone, as klee-replay does")
        elif info is not None and info[0] in _STDIN_TYPES:
            notes.append(f"standard input is {_STDIN_TYPES[info[0]]}; a case cannot express that, written as ordinary standard input")
        elif info is not None and (info[0] == _S_IFIFO or (info[0] == 0 and klee_replay)):
            # create_pipe: the program reads a pipe and there is no fd0; a type of zero is klee-replay's own rule for
            # descriptor 0 (``target_fd == 0 && (st_mode & S_IFMT) == 0``), not something the test says
            stdin_pipe = True
        elif info is not None:
            # create_reg_file: futimes(fd, {st_atime, st_mtime}) after the bytes are written
            stdin_times = _stat_times(stdin_stat)

    env: dict[str, bytes] = {}
    for key, value in (sidecar or {}).items():
        if not ENV_NAME_RE.match(key):
            notes.append(f"environment name {key!r} dropped (invalid name)")
        elif env_name_forbidden(key):
            notes.append(f"environment variable {key} dropped (a case cannot set it)")
        elif "\0" in value:
            notes.append(f"environment variable {key} dropped (NUL in value)")
        elif value_has_credentials(value.encode("utf-8", "surrogateescape")):
            notes.append(f"environment variable {key} dropped (its value holds credentials)")
        else:
            env[key] = value.encode("utf-8")
    return MappedCase(
        argv=argv,
        stdin=stdin,
        files=files,
        env=env,
        notes=notes,
        stdin_tty=stdin_tty,
        stdin_pipe=stdin_pipe,
        stdin_atime=stdin_times.get("atime"),
        stdin_mtime=stdin_times.get("mtime"),
    )


# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------


def find_ktests(root: Path) -> list[tuple[str, Path]]:
    """Every ``*.ktest`` regular file below ``root`` as ``(relative path with '/', path)``, sorted by relative path.

    Symbolic links are not followed. Iterative, so deep trees are fine.
    """

    found: list[tuple[str, Path]] = []
    stack = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as entries:
            listing = list(entries)
        for entry in listing:
            relative = f"{prefix}{entry.name}"
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), f"{relative}/"))
            elif entry.name.endswith(".ktest") and entry.is_file(follow_symlinks=False):
                found.append((relative, Path(entry.path)))
                if len(found) > MAX_CASES:
                    raise AdapterError(f"more than {MAX_CASES} .ktest files below {root}")
    found.sort(key=lambda item: item[0])
    return found


def convert_directory(
    input_dir: Path | str,
    out_dir: Path | str,
    info: ToolInfo,
    *,
    start_time: float | None = None,
    times_ms: dict[str, int] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    origin: str = KLEE_ORIGIN,
    klee_replay: bool = True,
) -> AdapterReport:
    """Convert the ``*.ktest`` files below ``input_dir`` (any depth) into a bundle.

    A test is named by its path relative to ``input_dir`` (with ``/``). ``t_ms``
    of a case is ``times_ms[relative path]`` when given, else the file's
    modification time minus ``start_time`` (default: the oldest modification
    time of the tree, so the first test is at 0, which is not the campaign
    start; the report says so). See the module text on times. ``klee_replay``: see :func:`map_ktest`; a test that
    ``klee-replay`` cannot replay is skipped with its reason.
    """

    root = Path(input_dir)
    report = AdapterReport()
    paths = find_ktests(root)
    report.source_cases = len(paths)
    if not paths:
        raise AdapterError(f"no .ktest files in {root}")
    times_ms = times_ms or {}
    unmatched = set(times_ms) - {name for name, _path in paths}
    if unmatched:
        report.notes.append(
            f"{len(unmatched)} entr{'y' if len(unmatched) == 1 else 'ies'} of the times file match no .ktest path "
            "(keys are paths relative to the input directory, with '/')"
        )
    mtimes = {name: path.stat().st_mtime for name, path in paths}
    from_mtime = [name for name, _path in paths if name not in times_ms]
    if start_time is None and from_mtime:
        origin_time = min(mtimes[name] for name in from_mtime)
        report.notes.append(
            "no --start-time: t_ms is relative to the oldest file's modification time, which is not the campaign "
            "start; the times are only meaningful if the files were never copied"
        )
    else:
        origin_time = start_time if start_time is not None else 0.0
    writer: BundleWriter = new_writer(out_dir, info)
    seen: set[tuple] = set()
    clamped = 0

    def _found_ms(name: str) -> int:
        return int(times_ms[name]) if name in times_ms else int(round((mtimes[name] - origin_time) * 1000))

    # Identical tests are kept once, at their earliest time, so the earliest is met first.
    for name, path in sorted(paths, key=lambda item: (_found_ms(item[0]), item[0])):
        try:
            ktest = parse_ktest(path.read_bytes())
            sidecar_path = path.with_name(path.name + ".env")
            sidecar = None
            if sidecar_path.is_file() and not sidecar_path.is_symlink():
                sidecar = parse_sidecar(sidecar_path.read_bytes())
            mapped = map_ktest(ktest, sidecar, klee_replay=klee_replay)
        except (AdapterError, OSError) as exc:
            report.skip(name, str(exc))
            continue
        reason = precheck_case(argv=mapped.argv, stdin=mapped.stdin, files=mapped.files, env=mapped.env)
        if reason:
            report.skip(name, reason)
            continue
        identity = (
            tuple(mapped.argv),
            mapped.stdin,
            mapped.stdin_tty,
            mapped.stdin_pipe,
            mapped.stdin_atime,
            mapped.stdin_mtime,
            tuple(
                (f["path"], f["kind"], f.get("mode"), f.get("body"), f.get("target"), f.get("atime"), f.get("mtime"))
                for f in mapped.files
            ),
            tuple(sorted(mapped.env.items())),
        )
        if identity in seen:
            report.duplicates += 1
            continue
        seen.add(identity)
        t_ms = _found_ms(name)
        if name not in times_ms and t_ms < 0:
            clamped += 1
            t_ms = 0
        writer.add_case(
            t_ms=t_ms,
            argv=mapped.argv,
            stdin=mapped.stdin,
            stdin_tty=mapped.stdin_tty,
            stdin_pipe=mapped.stdin_pipe,
            stdin_atime=mapped.stdin_atime,
            stdin_mtime=mapped.stdin_mtime,
            files=mapped.files,
            env=mapped.env,
            timeout_ms=timeout_ms,
            origin=origin,
        )
        report.written_cases += 1
        report.notes.extend(f"{name}: {note}" for note in mapped.notes)
    if clamped:
        report.notes.append(f"{clamped} file(s) are older than --start-time; their t_ms is 0")
    writer.finish()
    return report
