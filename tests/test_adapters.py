# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Promises A1 and A2 (tests/README.md): converting a tool's own output into a bundle keeps what the tool made and
invents nothing, so the replay measures the cases the tool produced.

Each test converts through the command line, validates, replays on the fake build and reads the coverage.
"""

from __future__ import annotations

import json
import os
import struct

import pytest
from support import A, B, BASE, DASH_X, U

from agent_software_testing.adapters.ktest import KTest, KTestFormatError, encode_ktest, parse_ktest


def ktest(args, objects):
    return encode_ktest(KTest(3, [b"/x/prog.bc", *[a.encode() for a in args]], 0, 0, objects))


def write(path, data, mtime=None):
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_klee_tests_become_the_arguments_and_input_the_program_was_run_with(rig, tmp_path):
    """A1. Symbolic arguments lose the last byte klee_init_env overwrites with NUL, standard input arrives whole,
    times are relative to the oldest test, an identical test is dropped and a file that is no test, or a test that lacks
    the input it asks for, is reported and left out."""

    src = tmp_path / "klee-out"
    src.mkdir()
    base = 1_700_000_000
    write(src / "test000001.ktest", ktest(["--sym-stdin", "2"], [(b"stdin", b"AU")]), base)
    write(src / "test000002.ktest", ktest(["--sym-arg", "2"], [(b"arg0", b"-xZ")]), base + 120)  # the argument is "-x"
    write(src / "test000003.ktest", ktest(["--sym-stdin", "2"], [(b"stdin", b"AU")]), base + 130)  # same as the first
    write(src / "test000004.ktest", b"not a ktest file", base + 140)
    write(src / "test000005.ktest", ktest(["--sym-stdin", "2"], []), base + 150)  # says it has input and has none
    (src / "notes.txt").write_text("not a test")
    code, out, err = rig.cli(
        "adapt", "ktest", src, "--out", tmp_path / "bundle", "--program-id", "demo", "--programs", rig.programs,
        "--tool-name", "KLEE", "--family", "symbolic", "--wall-s", 1000,
    )
    assert code == 0, err
    report = json.loads(out)
    assert (report["source_cases"], report["written_cases"], report["duplicates"]) == (5, 2, 1)
    assert [s["name"] for s in report["skipped_examples"]] == ["test000004.ktest", "test000005.ktest"]

    cases = [json.loads(line) for line in (tmp_path / "bundle" / "cases.jsonl").read_text().splitlines()]
    assert [c["t_ms"] for c in cases] == [0, 120_000]
    run = rig.replay(tmp_path / "bundle", "--no-flake-check")
    assert run.code == 0, run.err
    assert run.covered == sorted([BASE, A, U, DASH_X])


def test_an_identical_klee_test_is_kept_at_its_earliest_time(rig, tmp_path):
    """A1. The same test found twice counts from the first time it was found, whichever path comes first."""

    src = tmp_path / "klee-out"
    (src / "run-a").mkdir(parents=True)
    (src / "run-b").mkdir()
    base = 1_700_000_000
    write(src / "run-a" / "test000001.ktest", ktest(["--sym-stdin", "2"], [(b"stdin", b"AU")]), base + 3600)
    write(src / "run-b" / "test000001.ktest", ktest(["--sym-stdin", "2"], [(b"stdin", b"AU")]), base + 1)
    write(src / "run-b" / "test000002.ktest", ktest(["--sym-stdin", "2"], [(b"stdin", b"U")]), base)
    code, out, err = rig.cli(
        "adapt", "ktest", src, "--out", tmp_path / "bundle", "--program-id", "demo", "--programs", rig.programs,
        "--tool-name", "KLEE", "--family", "symbolic", "--wall-s", 10000,
    )
    assert code == 0, err
    assert json.loads(out)["duplicates"] == 1
    cases = [json.loads(line) for line in (tmp_path / "bundle" / "cases.jsonl").read_text().splitlines()]
    assert [c["t_ms"] for c in cases] == [0, 1000]


def test_afl_queue_files_are_cases_at_their_discovery_time_and_other_files_are_ignored(rig, tmp_path):
    """A1. The time: field is the time of the case; the template says whether the bytes are a file or standard
    input; identical bytes count once, at their earliest time; .state and README entries are no cases."""

    queue = tmp_path / "out" / "queue"
    (queue / ".state").mkdir(parents=True)
    write(queue / "id:000000,time:0,orig:seed", b"A")
    write(queue / "id:000001,src:000000,time:2500,op:havoc", b"B")
    write(queue / "id:000002,src:000001,time:3000,op:havoc", b"B")  # the same bytes later
    write(queue / "README.txt", b"U")
    write(queue / ".state" / "id:000003,time:1", b"U")
    code, out, err = rig.cli(
        "adapt", "afl", "--queue", queue, "--out", tmp_path / "bundle", "--program-id", "demo", "--programs", rig.programs,
        "--tool-name", "AFL++", "--family", "fuzzer", "--wall-s", 100, "--argv-json", json.dumps(["-x", "@@"]),
    )
    assert code == 0, err
    assert json.loads(out)["written_cases"] == 2 and json.loads(out)["duplicates"] == 1
    cases = [json.loads(line) for line in (tmp_path / "bundle" / "cases.jsonl").read_text().splitlines()]
    assert [c["t_ms"] for c in cases] == [0, 2500]
    run = rig.replay(tmp_path / "bundle", "--no-flake-check")
    assert run.covered == sorted([BASE, A, B, DASH_X])  # "-x" from the template, the bytes through the file "input"


def test_an_adapter_states_only_what_it_was_told_about_the_campaign(rig, tmp_path):
    """A2. Budget and cores describe the campaign and are published with it; the adapter cannot know them, so it
    writes the budget it is given, writes cores only when given, and applies the longest case timeout."""

    queue = tmp_path / "q"
    queue.mkdir()
    write(queue / "id:000000,time:0", b"A")
    common = ["--queue", queue, "--program-id", "demo", "--programs", rig.programs, "--tool-name", "T", "--arg=-q"]
    assert rig.cli("adapt", "afl", *common, "--out", tmp_path / "one", "--wall-s", 777)[0] == 0
    assert rig.cli("adapt", "afl", *common, "--out", tmp_path / "two", "--wall-s", 777, "--cores", 3)[0] == 0
    one = json.loads((tmp_path / "one" / "tool.json").read_text())
    two = json.loads((tmp_path / "two" / "tool.json").read_text())
    assert one["budget"]["wall_s"] == 777 and "cores" not in one["budget"]
    assert two["budget"]["cores"] == 3
    assert json.loads((tmp_path / "one" / "cases.jsonl").read_text().splitlines()[0])["timeout_ms"] == 30_000
    with pytest.raises(SystemExit):  # no budget given: the command line asks for one
        rig.cli("adapt", "afl", *common, "--out", tmp_path / "three")
    assert not (tmp_path / "three").exists()
    assert rig.cli("adapt", "afl", *common, "--out", tmp_path / "one", "--wall-s", 777)[0] == 1  # never over an existing bundle


def test_the_ktest_file_format_is_read_back_exactly():
    """A1. The binary format: arguments and objects round-trip with every byte value, and a truncated file is an
    error rather than a shorter test."""

    original = KTest(3, [b"/x/prog.bc", b"--sym-arg", b"4", b"\xff plain"], 3, 7, [(b"arg0", b"ab\0\0\0"), (b"stdin", bytes(range(256))), (b"empty", b"")])
    data = encode_ktest(original)
    assert data[:5] == b"KTEST" and struct.unpack(">I", data[5:9])[0] == 3
    assert parse_ktest(data) == original
    for cut in (4, 9, len(data) // 2, len(data) - 1):
        with pytest.raises(KTestFormatError):
            parse_ktest(data[:cut])
