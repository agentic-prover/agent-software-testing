# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Run one test case in a fresh directory (contract section 4, step 5).

Layout of one case directory::

    <scratch>/case-XXXXXXXX/
        root/     the case's files; the default working directory
        home/     HOME
        tmp/      TMPDIR
        stdin     the standard input file (only when the case has stdin)
        stderr    the last output of the program on standard error (sanitiser pass)

Files are created component by component through directory file descriptors
with ``O_NOFOLLOW`` and ``O_EXCL``, so nothing the case lists can make the
runner write outside ``root``. Symbolic links are created last. The directory
is removed afterwards with an iterative walk that holds one descriptor at a
time, so trees thousands of levels deep (a program may create them) are removed
without running into path length limits.
"""

from __future__ import annotations

import errno
import os
import resource
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .case import Case, check_tree
from .common import (
    BASE_LOCALE,
    BASE_PATH,
    BASE_TZ,
    CASE_MARKER_ENV,
    AgentSoftwareTestingError,
    IntegrityError,
    env_name_forbidden,
    path_problems,
)

STDERR_TAIL_BYTES = 64 * 1024
KILL_GRACE_S = 2.0
#: Where Linux lists processes. Tests point the stray-process scan at a fake tree.
PROC_ROOT = "/proc"
#: Most of an ``environ`` file that is read when looking for a case marker.
MAX_ENVIRON_BYTES = 4 * 1024 * 1024
#: How often the scan for stray processes is repeated for one case (a stray
#: that forks while it is being killed leaves children for the next pass).
STRAY_SCAN_PASSES = 5
#: Hexadecimal digits of the case marker (SPEC.md section 6.3 step 3).
MARKER_HEX_DIGITS = 32
#: Most digits of a marker that a replay may fix (the rest stays random per case).
MAX_MARKER_PREFIX = 16
#: ``prctl`` option that makes a process the adopter of the orphans among its descendants (Linux).
PR_SET_CHILD_SUBREAPER = 36
#: How often the descendants of a process are listed again while the list still grows.
DESCENDANT_WALKS = 4
#: What a time limit measures (``run_case(timeout_clock=...)``, SPEC.md section 6.3 step 7). ``wall`` is the limit of the
#: specification and of the ``canonical-2026`` profile. ``cpu`` is an opt-in deviation: the limit is the CPU time the program
#: may use (``RLIMIT_CPU``), so a node under load does not turn a slow case into a timeout.
TIMEOUT_CLOCKS = ("wall", "cpu")
#: With the ``cpu`` clock a case that waits (it uses no CPU) is still stopped, after this many times its limit in wall-clock time.
CPU_CLOCK_WALL_FACTOR = 4
#: A kill by ``SIGKILL`` or ``SIGXCPU`` counts as the CPU limit having been reached when the CPU time measured is at least
#: this share of the limit (the kernel counts in ticks and the figure of ``wait4`` is scaled).
CPU_LIMIT_SHARE = 0.95

#: How long the runner keeps the master side of a pseudo-terminal open after it has written the bytes of the case,
#: unless the program has finished earlier: then a program that reads to the end sees the end. ``klee-replay`` waits
#: half of its default timeout of five seconds (``wait_for_timeout_or_exit`` in ``file-creator.c``).
TTY_HOLD_S = 2.5

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class SandboxError(AgentSoftwareTestingError):
    """A case could not be set up in its directory."""


@dataclass(frozen=True)
class Limits:
    """Resource limits applied to the program through ``setrlimit``.

    ``cpu_s`` of ``None`` derives the limit from the case timeout (timeout
    rounded up plus a margin). The address space is unlimited by default
    because sanitiser builds reserve very large virtual ranges.
    """

    cpu_s: int | None = None
    file_size_bytes: int = 256 * 1024 * 1024
    open_files: int = 1024
    address_space_bytes: int | None = None
    core_bytes: int = 0
    #: ``False`` sets no limit at all (the ``canonical-2026`` profile, SPEC.md
    #: section 11.17): the container then has to bound the program.
    apply: bool = True

    def cpu_for(self, timeout_ms: int) -> int:
        if self.cpu_s is not None:
            return self.cpu_s
        return (timeout_ms + 999) // 1000 + 5

    @classmethod
    def none(cls) -> "Limits":
        """No resource limits."""

        return cls(apply=False)


@dataclass
class RunResult:
    status: str  # ok | nonzero_exit | terminated_by_signal | timeout | setup_error
    exit_code: int | None = None
    signal: int | None = None
    timed_out: bool = False
    duration_s: float = 0.0
    stderr_tail: bytes | None = None
    error: str | None = None
    #: Processes of the case that outlived its main process and its process
    #: group and were found by their environment marker and killed (SPEC.md
    #: section 6.3, step 7). Always 0 where ``/proc`` does not exist.
    stray_killed: int = 0
    #: The process ids behind ``stray_killed``, so that a worker that adopted them can collect them.
    stray_pids: tuple[int, ...] = ()
    #: CPU seconds (user and system) the main process and the children it collected used, or for a timeout the larger of that
    #: and what the process group had used when the time limit was reached (read from ``/proc`` before the kill, Linux only).
    #: ``None`` where nothing could be measured. A timeout with little CPU time was waiting or starved, not computing.
    cpu_s: float | None = None


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _names_a_locale(name: str | bytes) -> bool:
    """``LANG``, ``LANGUAGE`` or an ``LC_*`` variable other than ``LC_ALL``.

    ``LC_ALL`` is left out on purpose: it outranks every other locale variable, so in ``canonical-2026`` a case
    that sets only it replaces the base value without changing the rest of the environment (the environments
    recorded next to KLEE tests all have ``LANG``).
    """

    text = name if isinstance(name, str) else name.decode("ascii", "replace")
    return text in ("LANG", "LANGUAGE") or (text.startswith("LC_") and text != "LC_ALL")


def build_env(
    case_env: tuple[tuple[str, bytes], ...] | Mapping[str, bytes],
    *,
    home: str,
    tmpdir: str,
    extra: Mapping[str, str | bytes] | None = None,
    marker: str | None = None,
    recorded_locale: bool = False,
) -> dict[bytes, bytes]:
    """Base environment, then the case's variables, then the runner's own.

    The base holds ``LANG=C`` and ``TZ=UTC``. ``LANG`` is the locale variable of lowest priority, so the case's
    variables, which come after the base, take effect: a case that sets ``LANG``, ``LANGUAGE``, any ``LC_*``
    variable or ``TZ`` has its own value stand, whatever the profile (SPEC.md section 5.7); a case that sets no
    locale variable runs in the C locale, as before.

    ``recorded_locale`` (the ``canonical-2026`` profile, SPEC.md section 11.17): the base environment leaves out
    its locale and ``TZ=UTC`` when the case's own variables name a locale (``LANG``, ``LANGUAGE`` or an ``LC_*``
    variable other than ``LC_ALL``, see :func:`_names_a_locale`), because the earlier pipeline ran programs with
    exactly the environment recorded next to a test (``LANG=C`` and ``LC_CTYPE=C.UTF-8``, which turns on the
    multibyte code of grep, sed, du and others). Such a recorded environment names no ``TZ`` either, and the
    earlier pipeline did not add one (the time zone is then that of the image, UTC): gcal asks ``getenv("TZ")``
    and the branches that print the zone are covered only with a ``TZ`` set. A case that sets ``TZ`` has it. Every
    other case gets exactly the environment this profile gave before the base locale became ``LANG``: the base
    locale of the earlier pipeline was ``LC_ALL=C``.

    ``marker`` is the random token of the case; it becomes the variable
    ``AGENT_SOFTWARE_TESTING_CASE`` (SPEC.md section 6.3) that lets the runner find
    processes the program leaves behind. ``extra`` carries ``GCOV_PREFIX`` and
    friends (or the sanitiser options); it is applied last and may set names a
    case cannot.
    """

    env: dict[bytes, bytes] = {
        b"PATH": BASE_PATH.encode(),
        b"HOME": os.fsencode(home),
        b"TMPDIR": os.fsencode(tmpdir),
    }
    named_locale = recorded_locale and any(
        _names_a_locale(name) for name in (case_env.keys() if isinstance(case_env, Mapping) else (n for n, _v in case_env))
    )
    if not named_locale:
        # The standard profile's base locale is LANG=C. A canonical-2026 case that names no locale gets exactly the
        # environment that profile gave before: its earlier pipeline's base locale was LC_ALL=C. A recorded
        # environment (one that names a locale) gets neither base locale variable, nor the base TZ.
        env[b"LC_ALL" if recorded_locale else b"LANG"] = BASE_LOCALE.encode()
        env[b"TZ"] = BASE_TZ.encode()
    if marker is not None:
        env[CASE_MARKER_ENV.encode()] = marker.encode()
    items = case_env.items() if isinstance(case_env, Mapping) else case_env
    for name, value in items:
        if env_name_forbidden(name):
            raise SandboxError(f"case sets forbidden variable {name}")
        env[name.encode("ascii")] = value
    for name, value in (extra or {}).items():
        env[os.fsencode(name)] = os.fsencode(value)
    return env


# ---------------------------------------------------------------------------
# Creating the case's files
# ---------------------------------------------------------------------------


def _walk_dirs(root_fd: int, components: tuple[bytes, ...], *, create: bool) -> int:
    """Return a descriptor for the directory at ``components`` below ``root_fd``.

    The caller owns the returned descriptor. With ``create`` missing
    directories are made with mode 0700 and get their final modes last.
    """

    fd = os.dup(root_fd)
    try:
        for component in components:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class _FifoWriter(threading.Thread):
    """Writes a blob into a named pipe once a reader opens it."""

    def __init__(self, dir_fd: int, name: bytes, data: bytes, stop: threading.Event):
        super().__init__(daemon=True)
        self.dir_fd = dir_fd
        self.name_bytes = name
        self.data = data
        self.stop = stop

    def abandon(self) -> None:
        """Release the directory descriptor of a writer that was never started."""

        try:
            os.close(self.dir_fd)
        except OSError:
            pass

    def run(self) -> None:
        flags = os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = None
        try:
            while not self.stop.is_set():
                try:
                    fd = os.open(self.name_bytes, flags, dir_fd=self.dir_fd)
                    break
                except OSError as exc:
                    if exc.errno == errno.ENXIO:  # no reader yet
                        self.stop.wait(0.001)
                        continue
                    return
            if fd is None:
                return
            view = memoryview(self.data)
            while len(view) and not self.stop.is_set():
                try:
                    written = os.write(fd, view)
                    view = view[written:]
                except BlockingIOError:
                    select.select([], [fd], [], 0.05)
                except OSError:
                    break  # the reader went away
        finally:
            if fd is not None:
                os.close(fd)
            os.close(self.dir_fd)


class _TtyFeeder(threading.Thread):
    """Writes a blob into the master side of a pseudo-terminal, then holds the master open.

    This is what ``klee-replay`` does for a standard input or a symbolic file whose ``stat`` object records a
    character device: it opens a pseudo-terminal, hands the slave to the program and writes the bytes into the
    master from a second process. The master is closed when the program has finished (``stop``) or after
    :data:`TTY_HOLD_S` seconds, whichever comes first, so a program that reads until the end of input gets it.
    """

    def __init__(self, master: int, data: bytes, stop: threading.Event, slave: int | None = None):
        super().__init__(daemon=True)
        self.master = master
        #: A slave descriptor the feeder keeps open until it lets go (a terminal that a ``tty`` file points to:
        #: some systems drop what is written to a master while no slave is open; the settings of the slave are
        #: those of a freshly opened one, because they are not changed).
        self.slave = slave
        self.data = data
        self.stop = stop
        self.dir_fd = -1  # no directory descriptor; kept so that writers share one interface

    def abandon(self) -> None:
        """Release the descriptors of a feeder that was never started, or that has finished."""

        for descriptor in (self.master, self.slave):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def run(self) -> None:
        try:
            os.set_blocking(self.master, False)
            view = memoryview(self.data)
            while len(view) and not self.stop.is_set():
                try:
                    written = os.write(self.master, view[:65536])
                    view = view[written:]
                except BlockingIOError:
                    select.select([], [self.master], [], 0.05)
                except OSError:
                    break  # the slave side is gone
            self.stop.wait(TTY_HOLD_S)
        finally:
            self.abandon()


class _PipeFeeder(threading.Thread):
    """Writes a blob into the write end of an anonymous pipe, then closes it.

    This is what ``klee-replay`` does for a standard input whose ``stdin_stat`` object records a named pipe or no
    file type at all (``create_pipe`` in ``tools/klee-replay/file-creator.c``): the program reads from a pipe, so
    descriptor 0 cannot be repositioned and ``fstat`` reports a FIFO, and the bytes are written from a second
    process. That process keeps the write end open until the program has exited or half of the replay timeout has
    passed; here it is closed as soon as the last byte is written (or when ``stop`` is set), so a program that
    reads to the end sees the end at once. A program can tell the two apart only by waiting.
    """

    def __init__(self, write_end: int, data: bytes, stop: threading.Event):
        super().__init__(daemon=True)
        self.write_end = write_end
        self.data = data
        self.stop = stop
        self.dir_fd = -1  # no directory descriptor; kept so that writers share one interface

    def abandon(self) -> None:
        """Release the descriptor of a feeder that was never started, or that has finished."""

        try:
            os.close(self.write_end)
        except OSError:
            pass

    def run(self) -> None:
        try:
            os.set_blocking(self.write_end, False)
            view = memoryview(self.data)
            while len(view) and not self.stop.is_set():
                try:
                    written = os.write(self.write_end, view[:65536])
                    view = view[written:]
                except BlockingIOError:
                    select.select([], [self.write_end], [], 0.05)
                except OSError:
                    break  # the program closed its end without reading everything
        finally:
            self.abandon()


def open_terminal(*, raw: bool) -> tuple[int, int, str]:
    """A pseudo-terminal: ``(master, slave, slave device name)``, both descriptors close-on-exec.

    With ``raw`` the slave is put in the mode ``klee-replay`` gives a terminal that is standard input: no input
    processing (only ``IGNBRK``), no output post-processing of newlines, no line discipline (``c_lflag`` 0),
    reads return as soon as one byte is there.
    """

    master, slave = os.openpty()
    try:
        name = os.ttyname(slave)
        if raw:
            attributes = termios.tcgetattr(slave)
            attributes[0] = termios.IGNBRK
            attributes[1] &= ~(termios.ONLCR | termios.OCRNL | termios.ONLRET | getattr(termios, "OLCUC", 0))
            attributes[3] = 0
            attributes[6][termios.VMIN] = 1
            attributes[6][termios.VTIME] = 0
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    return master, slave, name


def _set_times(fd: int, atime: int | None, mtime: int | None) -> None:
    """Give a file the access and modification time of its entry (seconds).

    ``klee-replay`` does the same with ``futimes`` and ignores a failure; so does the runner: a value the
    kernel or the file system refuses (they differ in how far from the present they accept) leaves the time as
    created. The one given time is combined with the current value of the other.
    """

    if atime is None and mtime is None:
        return
    try:
        current = os.fstat(fd)
        os.utime(
            fd,
            (current.st_atime if atime is None else atime, current.st_mtime if mtime is None else mtime),
        )
    except (OSError, OverflowError, ValueError):
        pass


def check_case_safety(case: Case) -> None:
    """Re-check what keeps a case inside its root, whether or not it was validated.

    Every path and the working directory must be clean relative paths, symbolic
    link targets must stay inside the root, and the working directory must be a
    directory the case creates and the process can enter. Raises
    :class:`SandboxError` for the first problem.
    """

    for entry in case.files:
        problems = path_problems(entry.path)
        if problems:
            raise SandboxError(f"unsafe path {entry.path!r}: {problems[0][1]}")
    problems = path_problems(case.cwd, allow_dot=True)
    if problems:
        raise SandboxError(f"unsafe working directory {case.cwd!r}: {problems[0][1]}")
    errors = check_tree(case)
    if errors:
        raise SandboxError(f"unsafe case: {errors[0].message}")


def materialise(
    case: Case,
    read_blob: Callable[[str], bytes],
    root: Path,
    stop: threading.Event | None = None,
) -> list[_FifoWriter | _TtyFeeder]:
    """Create the case's files below the existing empty directory ``root``.

    Returns the (already started) writer threads for the case's named pipes and terminals;
    the caller sets ``stop`` when the program has finished and joins them.
    """

    stop = stop or threading.Event()
    writers: list[_FifoWriter | _TtyFeeder] = []
    check_case_safety(case)  # never trust the caller: nothing may leave the root
    root_fd = os.open(root, _DIR_FLAGS)
    try:
        dir_modes: dict[tuple[bytes, ...], int] = {}
        try:
            # 1. directories, explicit and implied
            for entry in case.files:
                comps = entry.components
                if entry.kind == "dir":
                    os.close(_walk_dirs(root_fd, comps, create=True))
                    dir_modes[comps] = entry.mode
                else:
                    os.close(_walk_dirs(root_fd, comps[:-1], create=True))
            # 2. regular files, named pipes and terminals
            for entry in case.files:
                if entry.kind not in ("file", "fifo", "tty"):
                    continue
                comps = entry.components
                parent = _walk_dirs(root_fd, comps[:-1], create=False)
                try:
                    name = comps[-1]
                    if entry.kind == "file":
                        assert entry.blob is not None
                        flags = (
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_CLOEXEC", 0)
                        )
                        fd = os.open(name, flags, 0o600, dir_fd=parent)
                        try:
                            data = read_blob(entry.blob)
                            view = memoryview(data)
                            while len(view):
                                view = view[os.write(fd, view) :]
                            os.fchmod(fd, entry.mode)
                            _set_times(fd, entry.atime, entry.mtime)
                        finally:
                            os.close(fd)
                    elif entry.kind == "tty":
                        assert entry.blob is not None
                        # klee-replay: a symbolic link to the slave of a fresh pseudo-terminal; the program opens the
                        # device with the settings of a fresh slave (a line discipline, not the raw mode of a
                        # standard input), the feeder holds the runner's slave open until it lets go
                        master, slave, device = open_terminal(raw=False)
                        try:
                            os.symlink(device, name, dir_fd=parent)
                        except BaseException:
                            os.close(master)
                            os.close(slave)
                            raise
                        writers.append(_TtyFeeder(master, read_blob(entry.blob), stop, slave))
                    else:
                        assert entry.blob is not None
                        os.mkfifo(name, 0o600, dir_fd=parent)
                        probe = os.open(
                            name,
                            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                            dir_fd=parent,
                        )
                        try:
                            os.fchmod(probe, entry.mode)
                        finally:
                            os.close(probe)
                        writer = _FifoWriter(os.dup(parent), name, read_blob(entry.blob), stop)
                        writers.append(writer)
                finally:
                    os.close(parent)
            # 3. symbolic links last
            for entry in case.files:
                if entry.kind != "symlink":
                    continue
                comps = entry.components
                parent = _walk_dirs(root_fd, comps[:-1], create=False)
                try:
                    assert entry.target is not None
                    os.symlink(entry.target, comps[-1], dir_fd=parent)
                finally:
                    os.close(parent)
            # 4. final directory modes, deepest first
            for comps in sorted(dir_modes, key=len, reverse=True):
                parent = _walk_dirs(root_fd, comps[:-1], create=False)
                try:
                    os.chmod(comps[-1], dir_modes[comps], dir_fd=parent)
                finally:
                    os.close(parent)
        except OSError as exc:
            raise SandboxError(f"cannot create the case's files: {exc}") from exc
        # start the writers only when everything else worked
        for writer in writers:
            writer.start()
    except BaseException:
        stop.set()
        for writer in writers:
            if writer.ident is None:  # never started
                writer.abandon()
        raise
    finally:
        os.close(root_fd)
    return writers


# ---------------------------------------------------------------------------
# Removing a directory tree of any depth
# ---------------------------------------------------------------------------


def _make_accessible(fd: int) -> None:
    try:
        os.fchmod(fd, 0o700)
    except OSError:
        pass


def _scan_and_unlink(fd: int) -> list[bytes]:
    """Delete the non-directory entries of the directory ``fd``; return its subdirectories."""

    for attempt in (0, 1):
        try:
            subdirs: list[bytes] = []
            with os.scandir(fd) as entries:
                listing = [(e.name, e.is_dir(follow_symlinks=False)) for e in entries]
            break
        except PermissionError:
            if attempt:
                raise
            _make_accessible(fd)
    for name, is_dir in listing:
        raw = os.fsencode(name)
        if is_dir:
            subdirs.append(raw)
            continue
        try:
            os.unlink(raw, dir_fd=fd)
        except PermissionError:
            _make_accessible(fd)
            os.unlink(raw, dir_fd=fd)
    return subdirs


def remove_tree(path: Path | str) -> None:
    """Delete ``path`` and everything below it, without recursion and without
    holding more than one directory descriptor, whatever the depth.

    Symbolic links are removed, never followed. Directories with restrictive
    modes are made accessible first. Raises ``FileNotFoundError`` when ``path``
    does not exist.
    """

    path = Path(path)
    start_fd = os.open(path, _DIR_FLAGS)
    fd = start_fd
    try:
        todo: list[list[bytes]] = [_scan_and_unlink(fd)]
        names: list[bytes] = []
        while True:
            level = todo[-1]
            if level:
                name = level.pop()
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except PermissionError:
                    os.chmod(name, 0o700, dir_fd=fd)
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                names.append(name)
                todo.append(_scan_and_unlink(fd))
            else:
                todo.pop()
                if not names:
                    break
                name = names.pop()
                try:
                    parent = os.open(b"..", _DIR_FLAGS, dir_fd=fd)
                except PermissionError:
                    _make_accessible(fd)
                    parent = os.open(b"..", _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = parent
                try:
                    os.rmdir(name, dir_fd=fd)
                except PermissionError:
                    _make_accessible(fd)
                    os.rmdir(name, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(path)


# ---------------------------------------------------------------------------
# Running the program
# ---------------------------------------------------------------------------


def _limiter(limits: Limits, cpu_s: int, cpu_only: bool = False) -> Callable[[], None] | None:
    """The resource limits as a function for ``preexec_fn``; ``cpu_only`` sets the CPU limit alone (the ``cpu`` clock of a
    profile that applies no other limit)."""

    if not limits.apply and not cpu_only:
        return None
    wanted: list[tuple[int, int]] = [(resource.RLIMIT_CPU, cpu_s)]
    if limits.apply:
        wanted += [
            (resource.RLIMIT_FSIZE, limits.file_size_bytes),
            (resource.RLIMIT_NOFILE, limits.open_files),
            (resource.RLIMIT_CORE, limits.core_bytes),
        ]
        if limits.address_space_bytes is not None and hasattr(resource, "RLIMIT_AS"):
            wanted.append((resource.RLIMIT_AS, limits.address_space_bytes))

    def apply() -> None:  # runs in the child between fork and exec: keep it minimal
        for which, value in wanted:
            try:
                _soft, hard = resource.getrlimit(which)
                if hard != resource.RLIM_INFINITY and value > hard:
                    value = hard
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                pass

    return apply


def _discard(stream) -> None:
    """Read a pipe to its end and drop what comes out of it; closes the stream."""

    try:
        descriptor = stream.fileno()
        while os.read(descriptor, 65536):
            pass
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _preexec(
    limits: Limits, cpu_s: int, own_process_group_only: bool, cpu_only: bool = False
) -> Callable[[], None] | None:
    """The function that runs in the child between fork and exec: resource limits and, for a case with a
    terminal, a process group instead of a session of its own.

    Normally the program starts in a session of its own (``setsid``, ``start_new_session``). A session leader that
    opens a terminal, for example the device a ``tty`` file points to, makes it its controlling terminal, and
    closing the master side (the end of the input) would then kill it with ``SIGHUP``. ``klee-replay`` runs the
    program with ``setpgrp`` in the session of the replay process, which has no controlling terminal to give, and
    the program just sees the end of its input; so does a case with a terminal here. (Ignoring ``SIGHUP`` instead
    would change what a program that inspects its signal dispositions covers: gzip does.)
    """

    limiter = _limiter(limits, cpu_s, cpu_only)
    if not own_process_group_only:
        return limiter

    def apply() -> None:
        os.setpgrp()
        if limiter is not None:
            limiter()

    return apply


def _environ_entries(path: str) -> list[bytes] | None:
    """The NUL-separated entries of a ``/proc/<pid>/environ`` file, ``None`` when unreadable."""

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    chunks: list[bytes] = []
    size = 0
    try:
        while size < MAX_ENVIRON_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return b"".join(chunks).split(b"\0")


def _process_group_of(base: str) -> int | None:
    """The process group id in ``<base>/stat``, ``None`` when it cannot be read.

    The file reads ``pid (comm) state ppid pgrp ...`` and ``comm`` may contain
    spaces and parentheses, so the fields are counted from the last ``)``.
    """

    try:
        with open(os.path.join(base, "stat"), "rb") as handle:
            data = handle.read(4096)
        fields = data[data.rindex(b")") + 1 :].split()
        return int(fields[2])
    except (OSError, ValueError, IndexError):
        return None


def _children_of(pid: int, proc_root: str | os.PathLike) -> list[int] | None:
    """The direct children of ``pid`` as listed in ``<proc_root>/<pid>/task/<tid>/children``.

    The kernel keeps the children of a process in the entry of the thread that created them, so every thread
    is read. ``[]`` when the process is gone, ``None`` when it exists but the kernel does not list children
    (it was built without ``CONFIG_PROC_CHILDREN``).
    """

    tasks = os.path.join(proc_root, str(pid), "task")
    try:
        threads = os.listdir(tasks)
    except OSError:
        return []
    found: list[int] = []
    listed = False
    for thread in threads:
        try:
            with open(os.path.join(tasks, thread, "children"), "rb") as handle:
                data = handle.read()
            found.extend(int(item) for item in data.split())
        except (OSError, ValueError):
            continue
        listed = True
    return found if listed or not threads else None


def descendants_of(root: int, *, proc_root: str | os.PathLike = PROC_ROOT) -> set[int] | None:
    """Every process below ``root`` (children, their children, and so on), without ``root`` itself.

    ``None`` where the kernel does not list children, so that the caller can fall back to scanning all of
    ``/proc``. The tree is walked again while it still grows: a process that is re-parented to ``root`` while the
    walk runs (its parent exited between two reads) shows up in the next walk.
    """

    seen: set[int] = set()
    for _ in range(DESCENDANT_WALKS):
        before = len(seen)
        pending = [root]
        visited = {root}
        while pending:
            pid = pending.pop()
            children = _children_of(pid, proc_root)
            if children is None:
                if pid == root:
                    return None
                continue
            for child in children:
                seen.add(child)
                if child not in visited:
                    visited.add(child)
                    pending.append(child)
        if len(seen) == before:
            break
    return seen


def find_marked_processes(
    token: str,
    *,
    proc_root: str | os.PathLike = PROC_ROOT,
    uid: int | None = None,
    exclude: frozenset[int] = frozenset(),
    skip_pgid: int | None = None,
    within: int | None = None,
    token_is_prefix: bool = False,
) -> list[int]:
    """Process ids of this user whose initial environment holds ``AGENT_SOFTWARE_TESTING_CASE=<token>``.

    Reads ``<proc_root>/<pid>/environ`` for every process of the same user. A
    process whose environment is unreadable (another user, not dumpable) or no
    longer holds the marker (it replaced its environment) is not found; see
    SPEC.md sections 6.7 and 11. Members of the process group ``skip_pgid`` are
    left out: the group kill has signalled them already, and they may still be
    visible for a moment while they die.

    With ``within`` only the descendants of that process are looked at instead of every process of the node
    (a shard worker is a child subreaper, so everything a case leaves behind hangs below it); where the kernel
    does not list children all processes are looked at. With ``token_is_prefix`` a marker that starts with
    ``token`` matches: all cases of one replay share the first digits of their markers.
    """

    needle = f"{CASE_MARKER_ENV}={token}".encode()
    if within is None:
        candidates = None
    else:
        candidates = descendants_of(within, proc_root=proc_root)
    if candidates is None:
        try:
            names = os.listdir(proc_root)
        except OSError:
            return []
    else:
        names = [str(pid) for pid in sorted(candidates)]
    uid = os.getuid() if uid is None else uid
    me = os.getpid()
    found: list[int] = []
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me or pid in exclude:
            continue
        base = os.path.join(proc_root, name)
        try:
            if os.stat(base).st_uid != uid:
                continue
        except OSError:
            continue
        entries = _environ_entries(os.path.join(base, "environ"))
        if entries is None:
            continue
        if token_is_prefix:
            if not any(entry.startswith(needle) for entry in entries):
                continue
        elif needle not in entries:
            continue
        if skip_pgid is not None and _process_group_of(base) == skip_pgid:
            continue
        found.append(pid)
    return found


def kill_marked_processes(
    token: str,
    *,
    proc_root: str | os.PathLike = PROC_ROOT,
    exclude: frozenset[int] = frozenset(),
    skip_pgid: int | None = None,
    passes: int = STRAY_SCAN_PASSES,
    kill: Callable[[int, int], None] = os.kill,
    within: int | None = None,
    token_is_prefix: bool = False,
    killed_out: set[int] | None = None,
) -> int:
    """SIGKILL every process that carries the marker; return how many were found.

    The scan is repeated (up to ``passes`` times) while it still finds processes
    that were not seen before, because a stray that forks while it is being
    killed leaves children behind. ``within`` and ``token_is_prefix`` are those of
    :func:`find_marked_processes`; the ids of the processes killed are added to ``killed_out``.
    """

    killed: set[int] = set()
    for _ in range(passes):
        found = find_marked_processes(
            token,
            proc_root=proc_root,
            exclude=exclude,
            skip_pgid=skip_pgid,
            within=within,
            token_is_prefix=token_is_prefix,
        )
        pids = [pid for pid in found if pid not in killed]
        if not pids:
            break
        for pid in pids:
            killed.add(pid)
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    if killed_out is not None:
        killed_out.update(killed)
    return len(killed)


def kill_replay_strays(prefix: str) -> int:
    """SIGKILL every process of this user whose marker starts with ``prefix`` (all cases of one replay).

    A full scan of ``/proc``; the replay runs it once per batch and when it stops. Returns how many were found;
    0 where ``/proc`` does not exist.
    """

    if not _PROC_AVAILABLE:
        return 0
    return kill_marked_processes(prefix, token_is_prefix=True)


# ---------------------------------------------------------------------------
# Adopting and collecting the processes a case leaves behind (shard workers)
# ---------------------------------------------------------------------------


def become_subreaper() -> bool:
    """Make this process the adopter of its orphaned descendants (Linux ``PR_SET_CHILD_SUBREAPER``).

    A process that leaves its process group or session and whose parent exits is then re-parented to this
    process and not to ``init``, so everything a case leaves behind stays below the shard worker that ran it.
    ``False`` where the call does not exist or fails.
    """

    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (ImportError, OSError, AttributeError, ValueError):
        return False


def reap_children() -> int:
    """Collect every child of this process that has exited and is not yet waited for; return how many.

    Only for a process that has no child it still waits for itself: a shard worker between two cases, when the
    only children left are processes it adopted (see :func:`become_subreaper`).
    """

    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if pid == 0:
            return reaped
        reaped += 1


def reap_killed(pids: Iterable[int], timeout_s: float = KILL_GRACE_S) -> None:
    """Wait until the children among ``pids`` (processes that were sent ``SIGKILL``) are collected.

    A pid that is not a child of this process (a grandchild whose parent is still dying) is left to
    :func:`reap_children`, which runs after the next case.
    """

    pending = set(pids)
    deadline = time.monotonic() + timeout_s
    while pending:
        for pid in sorted(pending):
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pending.discard(pid)
                continue
            if done:
                pending.discard(pid)
        if not pending or time.monotonic() >= deadline:
            return
        time.sleep(0.001)


def kill_descendants(root: int, *, proc_root: str | os.PathLike = PROC_ROOT, timeout_s: float = 5.0) -> int:
    """SIGKILL every descendant of ``root`` marked or not, collect them, and return how many were found.

    For a shard worker that is stopping: all its descendants are processes of cases it ran. Where the kernel
    does not list children nothing is done (the caller has the marker scan).
    """

    found: set[int] = set()
    deadline = time.monotonic() + timeout_s
    while True:
        below = descendants_of(root, proc_root=proc_root)
        if not below:
            return len(found)
        for pid in below:
            found.add(pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        reap_children()
        if time.monotonic() >= deadline:
            return len(found)
        time.sleep(0.002)


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _try_reap(proc: subprocess.Popen) -> bool:
    """Collect the leader if it has exited, keeping the CPU time it used (``proc.agent_software_testing_cpu_s``); where ``waitid``
    does not exist (macOS) this replaces :meth:`Popen.wait`, whose resource usage is thrown away."""

    if proc.returncode is not None:
        return True
    try:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
    except ChildProcessError:
        proc.poll()  # collected by someone else: let Popen settle the status
        return True
    if pid == 0:
        return False
    proc.returncode = os.waitstatus_to_exitcode(status)
    proc.agent_software_testing_cpu_s = usage.ru_utime + usage.ru_stime  # type: ignore[attr-defined]
    return True


def _wait_for_exit(proc: subprocess.Popen, timeout_s: float) -> bool:
    """Wait until the leader exits; ``True`` means it did not before the timeout.

    On Linux the exit is observed without reaping (``waitid`` with ``WNOWAIT``)
    so that stragglers in the process group can be killed while the group id
    is still ours. Elsewhere the child is reaped here (:func:`_try_reap`).
    """

    deadline = time.monotonic() + timeout_s
    waitid = getattr(os, "waitid", None)
    delay = 0.0005
    while True:
        if waitid is not None:
            exited = waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        else:
            exited = _try_reap(proc)
        if exited:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.02 if waitid is not None else 0.05)


def _collect(proc: subprocess.Popen) -> tuple[int, float | None]:
    """Reap the leader: its exit status (negative for a signal) and the CPU seconds it and the children it collected used.

    ``None`` for the CPU time when the leader was collected by someone else.
    """

    if proc.returncode is not None:
        return proc.returncode, getattr(proc, "agent_software_testing_cpu_s", None)
    try:
        _pid, status, usage = os.wait4(proc.pid, 0)
    except ChildProcessError:
        return proc.wait(), None
    proc.returncode = os.waitstatus_to_exitcode(status)
    return proc.returncode, usage.ru_utime + usage.ru_stime


def group_cpu_seconds(root: int, *, proc_root: str | os.PathLike = PROC_ROOT) -> float | None:
    """CPU seconds (user and system, with the children each process has already collected) used so far by ``root`` and
    every process below it.

    Read from ``<proc_root>/<pid>/stat`` (fields 14 to 17) for ``root`` and its descendants as :func:`descendants_of` lists
    them, so Linux only; where the kernel does not list children only ``root`` counts. ``None`` when ``root``'s entry cannot be
    read (Linux without ``/proc``, other systems, a process that is gone). A process that exits between the listing and the
    reading is missed, so the figure is a lower bound.
    """

    try:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return None
    below = descendants_of(root, proc_root=proc_root)
    total = 0
    counted_root = False
    for pid in [root, *sorted(below or ())]:
        try:
            with open(os.path.join(proc_root, str(pid), "stat"), "rb") as handle:
                data = handle.read(4096)
            fields = data[data.rindex(b")") + 1 :].split()
            total += sum(int(fields[index]) for index in (11, 12, 13, 14))  # utime stime cutime cstime
        except (OSError, ValueError, IndexError):
            continue
        counted_root = counted_root or pid == root
    return total / ticks_per_second if counted_root else None


def _abandon(proc: subprocess.Popen, marker: str, scan_strays: bool, stray_scope: int | None) -> None:
    """Kill what a case started when the call that runs it is interrupted, and collect the main process."""

    _kill_group(proc.pid, signal.SIGKILL)
    if scan_strays and _PROC_AVAILABLE:
        try:
            kill_marked_processes(marker, skip_pgid=proc.pid, within=stray_scope)
        except Exception:  # noqa: BLE001 - the interruption that brought us here must not be replaced
            pass
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _read_tail(path: Path, count: int = STDERR_TAIL_BYTES) -> bytes:
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - count))
            return handle.read(count)
    except OSError:
        return b""


def run_case(
    case: Case,
    read_blob: Callable[[str], bytes],
    *,
    binary: Path | str,
    program_name: str,
    scratch: Path | str,
    extra_env: Mapping[str, str | bytes] | None = None,
    limits: Limits | None = None,
    keep_stderr: bool = False,
    keep_dir: bool = False,
    timeout_ms: int | None = None,
    scan_strays: bool = True,
    recorded_locale: bool = False,
    output_pipe: bool = False,
    stdin_in_root: bool = False,
    stray_scope: int | None = None,
    marker_prefix: str = "",
    timeout_clock: str = "wall",
) -> RunResult:
    """Run ``case`` once against ``binary`` and return what happened.

    The program is started with ``argv[0]`` set to ``program_name``, a fresh
    session, the base environment plus the case's variables plus ``extra_env``
    plus the case marker, resource limits, and output discarded (the last
    64 KiB of standard error are kept when ``keep_stderr`` is set). On timeout
    the process group gets SIGTERM, then SIGKILL after two seconds. When the
    main process is gone, the group is killed, and then every process of the
    same user that still carries the case marker (SPEC.md section 6.3, step 7);
    ``RunResult.stray_killed`` counts those. ``timeout_ms`` replaces the case's
    own time limit (the ``canonical-2026`` profile uses 30,000 for every case).
    With ``output_pipe`` standard output and standard error are one pipe that a thread of the runner reads and
    discards, instead of ``/dev/null`` (the ``canonical-2026`` profile: the earlier pipeline captured the output;
    grep, for one, stops printing when standard output is ``/dev/null``). ``recorded_locale``: see
    :func:`build_env`. With ``stdin_in_root`` the standard input file is the file ``fd0`` in the working directory,
    opened for reading and writing, instead of a file next to it (the ``canonical-2026`` profile: ``klee-replay``
    created ``fd0`` in the directory it ran the program in, so ``ls``, ``du``, ``grep -r .`` and the like saw it);
    a case that has a file of that name keeps standard input outside. The option applies to a case whose standard
    input is a file, which the case itself says: a case without ``stdin``, with ``stdin_tty`` or with ``stdin_pipe``
    (an anonymous pipe, so no ``fd0``) is laid out the same with or without it. A case with ``stdin_atime`` or
    ``stdin_mtime`` has them set on the standard input file, whichever profile runs it.

    ``timeout_clock``: ``wall`` (the default; SPEC.md section 6.3 step 7) stops the case when the wall-clock time limit has
    passed. ``cpu`` (an opt-in deviation) gives the program the limit as CPU time instead: ``RLIMIT_CPU`` equals the
    limit rounded up to whole seconds, set on the program alone (the other resource limits are those of ``limits``), and the
    case is a ``timeout`` when the kernel stops it for that reason; a case that waits and uses no CPU is stopped, as a
    ``timeout`` too, after four times the limit in wall-clock time. A node under load then no longer turns a slow case
    into a timeout. ``RunResult.cpu_s`` holds the CPU time the case used, with either clock.

    ``stray_scope`` is the process id of a child subreaper (a shard worker, see :func:`become_subreaper`): the
    scan for stray processes then reads only the descendants of that process, not every process of the node; the
    result is the same, because everything the case starts hangs below the subreaper. ``marker_prefix`` fixes the
    first hexadecimal digits of the marker (the 32 digits stay random after them), so that a replay can find the
    processes of all its cases with one scan. If the call is interrupted by an exception (the worker is being
    stopped) the case's process group and the processes carrying its marker are killed before it propagates.
    """

    if len(marker_prefix) > MAX_MARKER_PREFIX or len(marker_prefix) % 2 or any(c not in "0123456789abcdef" for c in marker_prefix):
        raise ValueError(f"marker_prefix must be at most {MAX_MARKER_PREFIX} lower-case hexadecimal digits, in pairs")
    if timeout_clock not in TIMEOUT_CLOCKS:
        raise ValueError(f"timeout_clock must be one of {', '.join(TIMEOUT_CLOCKS)}")
    limits = limits or Limits()
    time_limit_ms = case.timeout_ms if timeout_ms is None else timeout_ms
    cpu_clock = timeout_clock == "cpu"
    cpu_limit_s = max(1, -(-time_limit_ms // 1000))
    wall_limit_s = time_limit_ms / 1000.0 * (CPU_CLOCK_WALL_FACTOR if cpu_clock else 1)
    marker = marker_prefix + secrets.token_hex((MARKER_HEX_DIGITS - len(marker_prefix)) // 2)
    scratch = Path(scratch)
    sandbox = Path(tempfile.mkdtemp(prefix="case-", dir=scratch))
    stop = threading.Event()
    writers: list[_FifoWriter | _TtyFeeder | _PipeFeeder] = []
    result = RunResult(status="setup_error")
    try:
        root = sandbox / "root"
        home = sandbox / "home"
        tmpdir = sandbox / "tmp"
        for directory in (root, home, tmpdir):
            directory.mkdir(mode=0o700)
        stdin_handle = stderr_handle = None
        has_terminal = case.stdin_tty or any(e.kind == "tty" for e in case.files)
        tty_slave: int | None = None
        tty_feeder: _TtyFeeder | None = None
        pipe_read: int | None = None
        pipe_feeder: _PipeFeeder | None = None
        drain: threading.Thread | None = None
        try:
            env = build_env(
                case.env, home=str(home), tmpdir=str(tmpdir), extra=extra_env, marker=marker, recorded_locale=recorded_locale
            )
            writers = materialise(case, read_blob, root, stop)
            cwd = root if case.cwd == b"." else Path(os.fsdecode(os.fsencode(root) + b"/" + case.cwd))
            if not cwd.is_dir():
                raise SandboxError(f"working directory {case.cwd!r} does not exist")
            if case.stdin is not None and case.stdin_tty:
                # a pseudo-terminal in the raw mode klee-replay uses for a character-device standard input
                master, tty_slave, _device = open_terminal(raw=True)
                tty_feeder = _TtyFeeder(master, read_blob(case.stdin), stop)
            elif case.stdin is not None and case.stdin_pipe:
                # an anonymous pipe, as klee-replay delivers a standard input that is no file: descriptor 0 cannot be
                # repositioned, fstat reports a FIFO, and there is no file fd0 in the working directory
                data = read_blob(case.stdin)
                pipe_read, pipe_write = os.pipe()
                pipe_feeder = _PipeFeeder(pipe_write, data, stop)
            elif case.stdin is not None:
                data = read_blob(case.stdin)
                if stdin_in_root and not os.path.lexists(cwd / "fd0"):
                    try:
                        stdin_path = cwd / "fd0"
                        stdin_path.write_bytes(data)
                        stdin_handle = open(stdin_path, "r+b", buffering=0)
                    except OSError:  # a working directory the program may enter but the runner may not write to
                        stdin_handle = None
                if stdin_handle is None:
                    stdin_path = sandbox / "stdin"
                    stdin_path.write_bytes(data)
                    stdin_handle = open(stdin_path, "rb")
                # klee-replay: futimes with the times of the stat object, after the bytes are written
                _set_times(stdin_handle.fileno(), case.stdin_atime, case.stdin_mtime)
            if keep_stderr:
                stderr_handle = open(sandbox / "stderr", "wb")
            cpu_s = cpu_limit_s if cpu_clock else limits.cpu_for(time_limit_ms)
            started = time.monotonic()
            proc = subprocess.Popen(
                [program_name.encode(), *case.argv],
                executable=os.fspath(binary),
                cwd=cwd,
                env=env,
                stdin=next((src for src in (tty_slave, pipe_read, stdin_handle) if src is not None), subprocess.DEVNULL),
                stdout=subprocess.PIPE if output_pipe else subprocess.DEVNULL,
                stderr=(
                    stderr_handle
                    if stderr_handle is not None
                    else (subprocess.STDOUT if output_pipe else subprocess.DEVNULL)
                ),
                start_new_session=not has_terminal,
                close_fds=True,
                preexec_fn=_preexec(limits, cpu_s, has_terminal, cpu_only=cpu_clock and not limits.apply),
            )
            if tty_feeder is not None:
                tty_feeder.start()
                writers.append(tty_feeder)
            if pipe_feeder is not None:
                pipe_feeder.start()
                writers.append(pipe_feeder)
            if output_pipe:
                drain = threading.Thread(target=_discard, args=(proc.stdout,), daemon=True)
                drain.start()
        except IntegrityError:
            raise  # a blob changed after the validation: the run is not trustworthy
        except (OSError, subprocess.SubprocessError, ValueError, AgentSoftwareTestingError) as exc:
            result.error = str(exc)
            return result
        finally:
            if stdin_handle is not None:
                stdin_handle.close()
            if tty_slave is not None:
                os.close(tty_slave)  # the program has its own copy; the runner keeps only the master
            if tty_feeder is not None and tty_feeder.ident is None:  # never started
                tty_feeder.abandon()
            if pipe_read is not None:
                os.close(pipe_read)  # the program has its own copy; the runner keeps only the write end
            if pipe_feeder is not None and pipe_feeder.ident is None:  # never started
                pipe_feeder.abandon()
            if stderr_handle is not None:
                stderr_handle.close()

        sampled_cpu: float | None = None
        try:
            timed_out = _wait_for_exit(proc, wall_limit_s)
            if timed_out:
                sampled_cpu = group_cpu_seconds(proc.pid) if _PROC_AVAILABLE else None  # before the kill: it ends the counting
                _kill_group(proc.pid, signal.SIGTERM)
                if _wait_for_exit(proc, KILL_GRACE_S):
                    _kill_group(proc.pid, signal.SIGKILL)
            # Stragglers (children the program left behind) die with the group.
            _kill_group(proc.pid, signal.SIGKILL)
            # Children that left the group (setsid) are found by the case marker.
            if scan_strays and _PROC_AVAILABLE:
                killed: set[int] = set()
                result.stray_killed = kill_marked_processes(
                    marker, skip_pgid=proc.pid, within=stray_scope, killed_out=killed
                )
                result.stray_pids = tuple(sorted(killed))
            returncode, reaped_cpu = _collect(proc)
        except BaseException:
            _abandon(proc, marker, scan_strays, stray_scope)
            raise
        if drain is not None:
            drain.join(timeout=2.0)  # the pipe closes when the group is gone; a stray holding it open is not waited for
        result.duration_s = time.monotonic() - started
        measured = [value for value in (sampled_cpu, reaped_cpu) if value is not None]
        result.cpu_s = round(max(measured), 3) if measured else None
        if (
            cpu_clock
            and not timed_out
            and returncode in (-signal.SIGKILL, -signal.SIGXCPU)
            and reaped_cpu is not None
            and reaped_cpu >= CPU_LIMIT_SHARE * cpu_limit_s
        ):
            timed_out = True  # the kernel stopped the program at its CPU limit
        result.timed_out = timed_out
        if timed_out:
            result.status = "timeout"
        elif returncode == 0:
            result.status = "ok"
        elif returncode < 0:
            result.status = "terminated_by_signal"
        else:
            result.status = "nonzero_exit"
        if returncode >= 0:
            result.exit_code = returncode
        else:
            result.signal = -returncode
        if keep_stderr:
            result.stderr_tail = _read_tail(sandbox / "stderr")
        return result
    finally:
        stop.set()
        for writer in writers:
            writer.join(timeout=2.0)
        if not keep_dir:
            try:
                remove_tree(sandbox)
            except FileNotFoundError:
                pass
            except OSError:
                shutil.rmtree(sandbox, ignore_errors=True)


_PROC_AVAILABLE = os.path.isdir(os.path.join(PROC_ROOT, "self"))


def signal_name(number: int | None) -> str | None:
    if number is None:
        return None
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"SIG{number}"
