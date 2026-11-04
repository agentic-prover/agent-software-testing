# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""One worker process per shard (shard mode ``process``, SPEC.md section 6.3 step 7 and section 11.15).

In shard mode ``thread`` the runner replays all shards as threads of one Python process: every case forks
under one interpreter lock and every scan for stray processes reads ``/proc`` under it, so one replay cannot use
more than a few cores however many shards it has. In shard mode ``process`` each shard has a worker process of its
own for the whole replay: it is started once, serves the cases of every snapshot interval, and idles while the
runner captures coverage between intervals. The runner (the parent) keeps the schedule; it sends each worker one
case at a time and gets back what ``run_case`` found: the status, the number of stray processes killed, the error
text of a case that could not be set up and the CPU time the case used. A case travels as its metadata only, the worker opens the bundle
by its path and reads the blobs itself.

Mechanism. The worker is a fresh interpreter (``python -I -c ...``) started with ``subprocess`` and connected by a
socket pair, speaking length-prefixed frames. ``multiprocessing`` was not used: with ``spawn`` and ``forkserver`` the
children re-import the ``__main__`` of whoever calls the library (a script without a main guard starts replays
recursively), the forkserver's children are not children of the runner (which the signal handling below needs), and
``fork`` from a runner that has threads and large sets in memory is unsafe. A frame carries a pickle that is read
back with an allow-list of classes: the worker accepts only the case classes and the settings below, the parent
accepts plain data only, so neither side executes anything the other sends.

Stray processes. A worker makes itself a child subreaper (Linux ``PR_SET_CHILD_SUBREAPER``): a process of a case that
leaves the case's process group or session and whose parent exits is re-parented to the worker, not to ``init``. After
a case the worker therefore looks for marked processes only among its own descendants
(``/proc/<pid>/task/<tid>/children``, walked recursively), kills them exactly as the full scan did, and collects
them with ``waitpid`` so that no zombie piles up. Where the subreaper or the children listing is not available (not
Linux, or a kernel without ``CONFIG_PROC_CHILDREN``) the worker scans all of ``/proc`` per case as before. The
runner scans all of ``/proc`` once per interval as a safety net, for the markers of this replay only: the first
digits of every marker are a token of the replay.

Stopping. SIGTERM and SIGINT to the runner (and the end of its pipe, if it is killed) make every worker kill the
process group and the descendants of the case it runs and exit; the runner then scans for markers once more.
"""

from __future__ import annotations

import dataclasses
import io
import os
import pickle
import queue
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bundle import BlobError, Bundle, BundleError
from .case import Case
from .common import AgentSoftwareTestingError, IntegrityError, NotRegularFileError
from .sandbox import (
    PROC_ROOT,
    Limits,
    RunResult,
    SandboxError,
    become_subreaper,
    descendants_of,
    kill_descendants,
    kill_replay_strays,
    reap_children,
    reap_killed,
    run_case,
    signal_name,
)

SHARD_MODES = ("thread", "process")
#: Longest frame either side accepts.
MAX_FRAME_BYTES = 256 * 1024 * 1024
#: How long a started worker may take to say it is ready (an interpreter under emulation starts slowly).
READY_TIMEOUT_S = 300.0
#: How long the workers get to finish after the stop message, and to die after SIGTERM, before SIGKILL.
STOP_GRACE_S = 30.0
TERM_GRACE_S = 10.0
#: The file in the shard directory that receives the worker's standard error.
WORKER_LOG = "worker.log"
#: Digits of the replay token that starts every marker.
REPLAY_TOKEN_DIGITS = 8
_LOG_TAIL_BYTES = 2000
_HEADER = struct.Struct(">I")
_BOOTSTRAP = (
    "import sys; sys.path.append(sys.argv[1]); "
    "from agent_software_testing.shards import worker_main; sys.exit(worker_main())"
)

_ERRORS_KEPT = {
    cls.__name__: cls
    for cls in (AgentSoftwareTestingError, IntegrityError, NotRegularFileError, BundleError, BlobError, SandboxError)
}
_WORKER_ACCEPTS = frozenset(
    {
        ("agent_software_testing.case", "Case"),
        ("agent_software_testing.case", "FileEntry"),
        ("agent_software_testing.sandbox", "Limits"),
        ("agent_software_testing.shards", "CaseSettings"),
    }
)


class ShardWorkerError(AgentSoftwareTestingError):
    """A shard worker could not be started, died, or failed in code of the runner."""


@dataclass(frozen=True)
class CaseSettings:
    """Everything ``run_case`` needs for the cases of one shard besides the case and its blobs."""

    program_name: str
    binary: str
    scratch: str
    extra_env: tuple[tuple[str, str], ...]
    limits: Limits
    timeout_ms: int | None
    scan_strays: bool
    #: The ``canonical-2026`` profile (SPEC.md section 11.17).
    canonical: bool
    marker_prefix: str = ""
    #: What the time limit measures: ``wall`` or ``cpu`` (see :func:`agent_software_testing.sandbox.run_case`).
    timeout_clock: str = "wall"

    def run_kwargs(self, case: Case) -> dict[str, Any]:
        return {
            "binary": self.binary,
            "program_name": self.program_name,
            "scratch": self.scratch,
            "extra_env": dict(self.extra_env),
            "limits": self.limits,
            "timeout_ms": self.timeout_ms,
            "scan_strays": self.scan_strays,
            "recorded_locale": self.canonical,
            "output_pipe": self.canonical,
            "stdin_in_root": self.canonical,
            "marker_prefix": self.marker_prefix,
            "timeout_clock": self.timeout_clock,
        }


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


class _Unpickler(pickle.Unpickler):
    def __init__(self, data: bytes, accepts: frozenset[tuple[str, str]]):
        super().__init__(io.BytesIO(data))
        self._accepts = accepts

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) in self._accepts:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"{module}.{name} is not allowed in a shard frame")


def send_frame(sock: socket.socket, message: Any) -> None:
    payload = pickle.dumps(message, protocol=4)
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def _receive(sock: socket.socket, count: int, *, first: bool) -> bytes | None:
    buffer = bytearray(count)
    view = memoryview(buffer)
    got = 0
    while got < count:
        n = sock.recv_into(view[got:])
        if n == 0:
            if first and got == 0:
                return None  # the other side closed between frames
            raise ShardWorkerError("the connection to the shard worker ended inside a frame")
        got += n
    return bytes(buffer)


def recv_frame(sock: socket.socket, accepts: frozenset[tuple[str, str]] = frozenset()) -> Any | None:
    """The next message, or ``None`` when the other side closed the connection between frames."""

    header = _receive(sock, _HEADER.size, first=True)
    if header is None:
        return None
    (size,) = _HEADER.unpack(header)
    if size > MAX_FRAME_BYTES:
        raise ShardWorkerError(f"a frame of {size} bytes is larger than the limit of {MAX_FRAME_BYTES}")
    payload = _receive(sock, size, first=False) if size else b""
    try:
        return _Unpickler(payload or b"", accepts).load()
    except (pickle.UnpicklingError, EOFError, AttributeError, ValueError) as exc:
        raise ShardWorkerError(f"a frame cannot be read: {exc}") from exc


# ---------------------------------------------------------------------------
# The runner's side
# ---------------------------------------------------------------------------


def _package_directory() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def processes_available() -> bool:
    """Whether worker processes can be started here (a POSIX system and an interpreter to start)."""

    return os.name == "posix" and bool(sys.executable) and hasattr(socket, "socketpair")


class ShardWorker:
    """The runner's handle on one worker process; one thread at a time calls :meth:`run`."""

    def __init__(self, index: int, directory: Path):
        self.index = index
        self.directory = Path(directory)
        self.process: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.info: dict[str, Any] = {}
        self.stopped = False

    @property
    def log_path(self) -> Path:
        return self.directory / WORKER_LOG

    def start(self, init: dict[str, Any]) -> None:
        try:
            parent, child = socket.socketpair()
        except OSError as exc:
            raise ShardWorkerError(f"cannot start shard worker {self.index}: {exc}") from exc
        try:
            with open(self.log_path, "wb") as log:
                self.process = subprocess.Popen(
                    [sys.executable, "-I", "-c", _BOOTSTRAP, _package_directory()],
                    stdin=child.fileno(),
                    stdout=child.fileno(),
                    stderr=log,
                    cwd=self.directory,
                    close_fds=True,
                )
        except OSError as exc:
            parent.close()
            raise ShardWorkerError(f"cannot start shard worker {self.index}: {exc}") from exc
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.sock = parent
        try:
            send_frame(parent, ("init", init))
        except OSError:
            pass  # the worker did not start; wait_ready reports why

    def wait_ready(self, timeout_s: float = READY_TIMEOUT_S) -> None:
        assert self.sock is not None
        self.sock.settimeout(timeout_s)
        try:
            reply = recv_frame(self.sock)
        except (OSError, ShardWorkerError) as exc:  # includes a timeout
            self.kill()
            raise ShardWorkerError(f"shard worker {self.index} did not become ready: {exc}") from exc
        finally:
            self.sock.settimeout(None)
        if reply is None:
            raise self._died("before it was ready")
        if reply[0] == "init-error":
            self.kill()
            raise ShardWorkerError(f"shard worker {self.index} cannot start: {reply[1]}")
        if reply[0] != "ready":
            self.kill()
            raise ShardWorkerError(f"shard worker {self.index} sent {reply[0]!r} instead of ready")
        self.info = dict(reply[1])

    def run(self, case: Case) -> RunResult:
        """Run ``case`` on the worker's shard; raises what ``run_case`` raised, or :class:`ShardWorkerError`."""

        assert self.sock is not None
        try:
            send_frame(self.sock, ("run", case))
            reply = recv_frame(self.sock)
        except (OSError, ShardWorkerError):
            reply = None
        if reply is None:
            raise self._died("while it ran a case")
        tag = reply[0]
        if tag == "done":
            return RunResult(status=reply[1], stray_killed=reply[2], error=reply[3], cpu_s=reply[4])
        if tag == "fail":
            _kind, name, message, trace = reply[1:5]
            kept = _ERRORS_KEPT.get(name)
            if kept is not None:
                raise kept(message)
            raise ShardWorkerError(f"shard worker {self.index} failed in the runner's own code ({name}: {message})\n{trace}")
        raise ShardWorkerError(f"shard worker {self.index} sent an unexpected message {tag!r}")

    def request_stop(self) -> None:
        try:
            if self.sock is not None:
                send_frame(self.sock, ("stop",))
        except OSError:
            pass

    def terminate(self) -> None:
        """SIGTERM the worker, which kills what its case started and exits. Safe in a signal handler."""

        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, number: int) -> None:
        process = self.process
        if process is not None and process.returncode is None:
            try:
                os.kill(process.pid, number)
            except (ProcessLookupError, PermissionError):
                pass

    def wait(self, timeout_s: float) -> bool:
        """``True`` when the worker has exited (and is collected)."""

        if self.process is None:
            return True
        try:
            self.process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False
        return True

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _log_tail(self) -> str:
        try:
            with open(self.log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _LOG_TAIL_BYTES))
                return handle.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _died(self, when: str) -> ShardWorkerError:
        if self.process is not None and not self.wait(5.0):
            self.kill()
            self.wait(5.0)
        code = self.process.returncode if self.process is not None else None
        if code is None:
            how = "status unknown"
        elif code < 0:
            how = f"killed by {signal_name(-code)}"
        else:
            how = f"exit status {code}"
        tail = self._log_tail()
        stopped = "; the replay was being stopped" if self.stopped else ""
        return ShardWorkerError(
            f"shard worker {self.index} died {when} ({how}{stopped})" + (f":\n{tail}" if tail else "")
        )


class ShardPool:
    """The workers of one replay, one per shard, started once and used for every batch."""

    def __init__(self, workers: list[ShardWorker], *, token: str, scan_strays: bool):
        self.workers = workers
        #: The first digits of every marker of this replay.
        self.token = token
        self.scan_strays = scan_strays
        self.aborting = False
        self.interrupted = False
        self.closed = False
        self._previous_handlers: dict[int, Any] = {}

    @classmethod
    def start(
        cls,
        bundle: Bundle,
        directories: list[Path],
        settings: list[CaseSettings],
        *,
        scan_strays: bool,
        subreaper: bool = True,
    ) -> "ShardPool":
        """Start one worker per shard directory and wait until all are ready.

        Raises :class:`ShardWorkerError` when a worker cannot be started; nothing is left running then.
        """

        token = secrets.token_hex(REPLAY_TOKEN_DIGITS // 2)
        pool = cls([ShardWorker(i, d) for i, d in enumerate(directories)], token=token, scan_strays=scan_strays)
        try:
            for worker, own in zip(pool.workers, settings):
                worker.start(
                    {
                        "bundle": os.path.abspath(bundle.root),
                        "verify_blobs": bundle.verify_blobs,
                        "settings": dataclasses.replace(own, marker_prefix=token),
                        "subreaper": subreaper,
                    }
                )
            for worker in pool.workers:
                worker.wait_ready()
            # after the workers have started, which inherit the dispositions the runner was started with
            pool._install_signal_handlers()
        except BaseException:
            pool.abort()
            pool.close()
            raise
        return pool

    def describe(self) -> str:
        scopes = sorted({str(w.info.get("stray_scope")) for w in self.workers})
        text = {
            "descendants": "stray processes are looked for among each worker's descendants",
            "all": "stray processes are looked for among all processes of the node",
            "none": "stray processes are not scanned for",
        }
        return f"{len(self.workers)} worker process(es); " + "; ".join(text.get(s, s) for s in scopes)

    def run(self, index: int, case: Case) -> RunResult:
        return self.workers[index].run(case)

    def sweep(self) -> int:
        """Kill the processes of this replay that are still around, wherever they are; return how many."""

        return kill_replay_strays(self.token)

    def abort(self) -> None:
        """Tell every worker to kill what its case started and exit. Safe to call from a signal handler."""

        self.aborting = True
        for worker in self.workers:
            worker.stopped = True
            worker.terminate()

    def close(self) -> None:
        """Stop the workers, make sure nothing they started is left, and restore the signal handlers."""

        if self.closed:
            return
        self.closed = True
        try:
            if not self.aborting:
                for worker in self.workers:
                    worker.stopped = True
                    worker.request_stop()
                self._wait_all(STOP_GRACE_S)
            for worker in self.workers:
                if not worker.wait(0):
                    worker.terminate()
            self._wait_all(TERM_GRACE_S)
            for worker in self.workers:
                if not worker.wait(0):
                    worker.kill()
            self._wait_all(TERM_GRACE_S)
            if self.aborting or self.scan_strays:
                self.sweep()
        finally:
            for worker in self.workers:
                worker.close()
            self._restore_signal_handlers()

    def _wait_all(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        for worker in self.workers:
            worker.wait(max(0.0, deadline - time.monotonic()))

    # -- signals ----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for number in (signal.SIGTERM, signal.SIGINT):
            if signal.getsignal(number) != signal.SIG_IGN:  # a signal the runner ignores stays ignored
                self._previous_handlers[number] = signal.signal(number, self._on_signal)

    def _restore_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for number, handler in self._previous_handlers.items():
            try:
                signal.signal(number, handler)
            except (TypeError, ValueError):
                pass
        self._previous_handlers.clear()

    def _on_signal(self, number: int, _frame: Any) -> None:
        self.abort()
        if self.interrupted:
            return  # a second signal while the runner cleans up changes nothing
        self.interrupted = True
        if number == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + number)


# ---------------------------------------------------------------------------
# The worker's side
# ---------------------------------------------------------------------------


class _Abort(BaseException):
    """Raised in the worker's main thread by a signal or by the end of the connection."""

    def __init__(self, number: int):
        super().__init__(number)
        self.number = number


class _Worker:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        #: A signal arrived; further signals no longer interrupt the clean-up.
        self.aborting = False
        self.interrupted = False
        self.subreaper = False
        self.stray_scope: int | None = None
        self.token = ""
        self.sweep_on_exit = True
        #: The signal that stands for "stop" in this process: one the runner did not ignore.
        self.stop_signal: int | None = None

    def on_signal(self, number: int, _frame: Any) -> None:
        if self.aborting:
            return
        self.aborting = True
        self.interrupted = True
        raise _Abort(number)

    def read_requests(self) -> None:
        """Runs in a thread: queue the runner's messages; the end of the connection without a stop is an abort."""

        try:
            while True:
                message = recv_frame(self.sock, _WORKER_ACCEPTS)
                if message is None:
                    break
                self.inbox.put(message)
                if message[0] == "stop":
                    return
        except (OSError, ShardWorkerError):
            pass
        # the runner is gone: stop as its SIGTERM would have made us stop
        if self.stop_signal is not None:
            os.kill(os.getpid(), self.stop_signal)
        else:  # every stop signal is ignored here: clean up from this thread
            self.interrupted = True
            self.clean_up()
            os._exit(128 + signal.SIGTERM)

    def serve(self) -> int:
        sock = self.sock
        first = recv_frame(sock, _WORKER_ACCEPTS)
        if first is None:
            return 0
        if first[0] != "init":
            return 70
        init = first[1]
        settings: CaseSettings = init["settings"]
        self.token = settings.marker_prefix
        try:
            bundle = Bundle(init["bundle"], verify_blobs=init["verify_blobs"])
        except AgentSoftwareTestingError as exc:
            send_frame(sock, ("init-error", f"cannot open the bundle: {exc}"))
            return 70
        me = os.getpid()
        self.subreaper = bool(init["subreaper"]) and become_subreaper()
        if not settings.scan_strays or not os.path.isdir(os.path.join(PROC_ROOT, "self")):
            scope = "none"
        elif self.subreaper and descendants_of(me) is not None:
            self.stray_scope, scope = me, "descendants"
        else:
            scope = "all"
        # Only the signals the runner did not ignore are taken over: a program inherits an ignored
        # disposition through exec, and the replay must not change what the program finds.
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            if signal.getsignal(number) != signal.SIG_IGN:
                signal.signal(number, self.on_signal)
                if self.stop_signal is None:
                    self.stop_signal = number
        threading.Thread(target=self.read_requests, daemon=True).start()
        send_frame(sock, ("ready", {"pid": me, "subreaper": self.subreaper, "stray_scope": scope}))

        while True:
            message = self.inbox.get()
            tag = message[0]
            if tag == "stop":
                self.sweep_on_exit = settings.scan_strays
                return 0
            if tag != "run":
                return 70
            case = message[1]
            result: RunResult | None = None
            try:
                result = run_case(
                    case, bundle.read_blob, stray_scope=self.stray_scope, **settings.run_kwargs(case)
                )
                reply: tuple = ("done", result.status, result.stray_killed, result.error, result.cpu_s)
            except Exception as exc:  # noqa: BLE001 - reported to the runner, which stops the replay
                kind = "common" if isinstance(exc, AgentSoftwareTestingError) else "other"
                trace = traceback.format_exc()
                reply = ("fail", kind, type(exc).__name__, str(exc)[:4000], trace[-8000:])
            if self.subreaper:
                reap_children()
                if result is not None and result.stray_pids:
                    reap_killed(result.stray_pids)
            send_frame(sock, reply)

    def clean_up(self) -> None:
        """Kill everything the worker started: its descendants, or by marker where it cannot list them."""

        if self.subreaper:
            kill_descendants(os.getpid())
        if self.interrupted and self.stray_scope is None and self.token:
            kill_replay_strays(self.token)


def _detach_standard_streams() -> None:
    """Keep the connection off descriptors 0 and 1: stray output of the worker goes to its log."""

    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(2, 1)
    os.close(devnull)


def worker_main() -> int:
    """Entry point of a shard worker process: serve the runner on descriptor 0 until it says stop."""

    sock = socket.socket(fileno=os.dup(0))
    _detach_standard_streams()
    worker = _Worker(sock)
    code = 70
    try:
        code = worker.serve()
    except _Abort as stopped:
        code = 128 + stopped.number
    except BaseException:  # noqa: BLE001 - the log of the worker is read by the runner
        traceback.print_exc()
    finally:
        worker.aborting = True  # no further signal may interrupt the clean-up
        if code != 0 or worker.sweep_on_exit:
            worker.clean_up()
        try:
            sock.close()
        except OSError:
            pass
    return code
