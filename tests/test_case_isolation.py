# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Promises C1 to C7 (tests/README.md): every case runs alone, in the environment the specification gives it, and
cannot change the build that later cases run on.

The fake program (tests/fake_toolchain.py) reports what it was given when a case passes it --probe=FILE or
--observe=FILE. The runner is started with a variable it must not pass on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

import pytest
from support import A, B, BASE, H, U, case

from agent_software_testing.case import parse_case
from agent_software_testing.sandbox import run_case

HERE_LINUX = os.path.isdir("/proc/self")


def is_gone(pid: int, wait_s: float = 5.0) -> bool:
    """True when no process has this id, allowing a moment for the system to collect it. A killed process that nobody
    has collected yet (a zombie, which a container without an init process keeps) counts as gone."""

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat") as handle:
                if handle.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def test_the_program_gets_exactly_the_specified_environment_and_not_the_runners(rig, monkeypatch):
    """C1. PATH, HOME, TMPDIR, LANG and TZ, then the case's own variables (which may set locale and zone), then
    the runner's: nothing of the environment the replay was started in."""

    monkeypatch.setenv("SECRET_TOKEN", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", str(rig.tmp))
    first, second = rig.tmp / "probe-1.json", rig.tmp / "probe-2.json"
    bundle = rig.bundle(
        [
            case(0, argv=[f"--probe={first}".encode()], env={"MY_OWN": b"1", "LC_CTYPE": b"C.UTF-8"}),
            case(1, argv=[f"--probe={second}".encode()], env={"TZ": b"NPT-5:45", "LANG": b"C.UTF-8"}),
        ],
        wall_s=100,
    )
    run = rig.replay(bundle, "--workers", 1, "--no-flake-check")
    assert run.code == 0, run.err
    one, two = json.loads(first.read_text())["env"], json.loads(second.read_text())["env"]

    assert "SECRET_TOKEN" not in one and "PYTHONPATH" not in one
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "TZ", "MY_OWN", "LC_CTYPE", "GCOV_PREFIX", "GCOV_PREFIX_STRIP", "AGENT_SOFTWARE_TESTING_CASE"}
    assert set(one) - allowed <= {"__CF_USER_TEXT_ENCODING"}  # macOS adds this one itself
    assert one["PATH"] == "/usr/bin:/bin" and one["LANG"] == "C" and one["TZ"] == "UTC"
    assert one["MY_OWN"] == "1" and one["LC_CTYPE"] == "C.UTF-8"
    assert re.fullmatch(r"[0-9a-f]{32}", one["AGENT_SOFTWARE_TESTING_CASE"])
    assert one["AGENT_SOFTWARE_TESTING_CASE"] != two["AGENT_SOFTWARE_TESTING_CASE"]
    assert one["GCOV_PREFIX_STRIP"].isdigit() and one["GCOV_PREFIX"] != str(rig.build)
    assert two["TZ"] == "NPT-5:45" and two["LANG"] == "C.UTF-8"  # the case's own values stand
    assert one["HOME"] != two["HOME"] and one["TMPDIR"] != two["TMPDIR"]  # private to each case


def test_a_case_starts_in_a_fresh_empty_directory_whatever_ran_before(rig):
    """C2. Files a case leaves in its working directory, HOME or TMPDIR are gone for the next case on the same
    shard, so the order of cases does not change coverage."""

    probe = rig.tmp / "probe.json"
    bundle = rig.bundle(
        [case(0, argv=[b"--leave"]), case(1, argv=[f"--probe={probe}".encode()])], wall_s=100
    )
    assert rig.replay(bundle, "--workers", 1, "--no-flake-check").code == 0
    seen = json.loads(probe.read_text())
    assert seen["cwd_list"] == []
    assert "left-behind" not in seen["home_list"] and "left-behind" not in seen["tmp_list"]  # the interpreter may add files of its own


def test_the_files_of_a_case_are_where_it_says_and_the_working_directory_is_respected(rig):
    """C3. Files, directories, links (also dangling ones) and the working directory are laid out as listed;
    the program reads them and coverage follows."""

    f = lambda path, body: {"path": path, "kind": "file", "body": body}  # noqa: E731
    bundle = rig.bundle(
        [
            case(0, argv=[b"in/data"], files=[f(b"in/data", b"A")]),
            case(1, argv=[b"data"], files=[f(b"in/data", b"B")], cwd=b"in"),
            case(2, argv=[b"ln"], files=[f(b"in/data", b"U"), {"path": b"ln", "kind": "symlink", "target": b"in/data"}]),
            case(3, argv=[b"in/data"], files=[f(b"in/data", b"H"), {"path": b"dangling", "kind": "symlink", "target": b"nowhere"}]),
        ],
        wall_s=100,
    )
    run = rig.replay(bundle, "--no-flake-check")
    assert run.code == 0, run.err
    assert run.summary["status_counts"]["ok"] == 4
    assert run.covered == sorted([BASE, A, B, U, H])


def test_a_case_that_would_leave_its_directory_is_not_run_even_when_nobody_validated_it(rig, tmp_path):
    """C4. The runner checks every case again before it creates anything (the bundle could have changed since
    the validator looked): a path that climbs out is a setup error and nothing appears outside."""

    raw = {
        "id": "000001", "t_ms": 0, "argv": [], "stdin": None, "env": {}, "cwd": "Lg==",
        "files": [{"path": "Li4vZXNjYXBl", "kind": "file", "mode": "0644", "blob": "sha256:" + hashlib.sha256(b"x").hexdigest()}],
    }
    unsafe, errors = parse_case(raw, 1)
    assert errors and unsafe is not None  # the validator would have refused it
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = run_case(unsafe, lambda digest: b"x", binary=rig.build / "bin" / "prog", program_name="prog", scratch=scratch)
    assert result.status == "setup_error"
    assert not (tmp_path / "escape").exists() and not (scratch / "escape").exists()
    assert list(scratch.iterdir()) == []


def test_a_program_cannot_change_the_build_and_no_profile_data_lands_in_it(rig):
    """C5. The binary and notes files a shard runs are read-only copies; the original build is never touched;
    profile data goes to a tree of the shard."""

    before = tree_digest(rig.build)
    run = rig.replay(rig.bundle([case(0, b"TAMPER A"), case(1, b"TAMPER B")], wall_s=100), "--no-flake-check")
    assert run.summary["status_counts"]["ok"] == 2 and run.summary["status_counts"]["nonzero_exit"] == 0  # TAMPER exits 5 if it could write
    assert tree_digest(rig.build) == before
    assert list(rig.build.rglob("*.gcda")) == []
    assert run.covered == sorted([BASE, A, B])


def test_a_case_that_runs_past_its_limit_is_stopped_with_everything_it_started(rig):
    """C6. The time limit is enforced on the whole process group; the replay does not wait for a sleeping program."""

    pidfile = rig.tmp / "pid"
    bundle = rig.bundle(
        [case(0, b"SLEEP", argv=[f"--pidfile={pidfile}".encode()], timeout_ms=3000), case(1, b"A")], wall_s=100
    )
    started = time.monotonic()
    run = rig.replay(bundle, "--workers", 1, "--no-flake-check")
    assert time.monotonic() - started < 25  # the program sleeps for 30 s
    assert run.summary["status_counts"]["timeout"] == 1 and run.covered == sorted([BASE, A])
    assert is_gone(int(pidfile.read_text()))


@pytest.mark.skipif(not HERE_LINUX, reason="finding a process that left its group needs /proc (run tests/in_container.py)")
@pytest.mark.parametrize("shard_mode", ["thread", "process"])
def test_a_process_that_left_its_group_is_found_and_killed(rig, shard_mode):
    """C6. A case can start a process in a session of its own; the replay finds it by the marker in its
    environment and counts it, whichever way the shards are served."""

    pidfile = rig.tmp / "daemon-pid"
    bundle = rig.bundle([case(0, b"A", argv=[f"--daemon={pidfile}".encode()])], wall_s=100)
    run = rig.replay(bundle, "--workers", 1, "--no-flake-check", "--shard-mode", shard_mode)
    assert run.summary["stray_killed"] >= 1
    assert is_gone(int(pidfile.read_text()))


@pytest.mark.parametrize("mode", ["file", "pipe"])
def test_standard_input_arrives_byte_for_byte_in_the_shape_the_case_asks_for(rig, mode):
    """C7. All 256 byte values, NUL included, in the order given; a file can be repositioned and a pipe cannot; no
    stray file appears next to the program in the standard profile; no input at all is an empty one."""

    data = bytes(range(256)) * 400
    seen = rig.tmp / f"seen-{mode}.json"
    empty = rig.tmp / "seen-empty.json"
    bundle = rig.bundle(
        [
            case(0, data, argv=[f"--observe={seen}".encode()], stdin_pipe=(mode == "pipe")),
            case(1, None, argv=[f"--observe={empty}".encode()]),
        ],
        wall_s=100,
    )
    assert rig.replay(bundle, "--no-flake-check").code == 0
    got = json.loads(seen.read_text())
    assert got["bytes"] == len(data) and got["sum"] == sum(data) % 2**32
    assert got["fifo"] == (mode == "pipe") and got["regular"] == (mode == "file")
    assert got["seekable"] == (mode == "file")
    assert got["fd0"] is False
    assert json.loads(empty.read_text())["bytes"] == 0
