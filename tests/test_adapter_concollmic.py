# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Promise A3 (tests/README.md): the ConcoLLMic adapter runs code that a language model wrote and recorded in a
YAML file, so that code is untrusted. It must never start the program under test, never write outside its sandbox,
refuse what a test case must not do and give up on code that does not end. Its output is a valid bundle with the
times of the recorded files.
"""

from __future__ import annotations

import base64
import json
import socket
import textwrap
from pathlib import Path

import pytest

from agent_software_testing.adapters import concollmic
from agent_software_testing.adapters.common import ToolInfo
from agent_software_testing.adapters.concollmic import RecordSettings, convert_directory, parse_time_ms, record_code
from agent_software_testing.cli import main
from agent_software_testing.program import find_program
from agent_software_testing.validate import validate_bundle

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "concollmic"
BASE = "2026-03-04 05:06:07"


def info(**over):
    values = dict(name="ConcoLLMic", program_id="demo", source_sha256="1" * 64, family="llm", model="a-model", wall_s=3600)
    values.update(over)
    return ToolInfo(**values)


def b64d(text: str) -> bytes:
    return base64.b64decode(text)


def code(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def yaml_case(index: int, source: str | None, created: str) -> str:
    lines = [f"id: {index}", "src_id:", f"create_time: '{created}'"]
    if source is None:
        lines.append("exec_code:")
    else:
        lines.append("exec_code: |")
        lines += [("  " + line) if line else "" for line in source.rstrip("\n").split("\n")]
    lines += ["returncode:"]
    return "\n".join(lines) + "\n"


def make_queue(root: Path, cases: list[tuple[str | None, str]]) -> Path:
    """One ``id:NNNNNN.yaml`` per (code, create_time)."""

    queue = root / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    for index, (source, created) in enumerate(cases):
        (queue / f"id:{index:06d}.yaml").write_text(yaml_case(index, source, created), encoding="utf-8")
    return queue


def convert(tmp_path: Path, cases: list[tuple[str | None, str]], name: str = "bundle", **options):
    queue = make_queue(tmp_path / f"q-{name}", cases)
    out = tmp_path / name
    options.setdefault("binary_name", "prog")
    options.setdefault("work_dir", tmp_path / "work")
    options.setdefault("jobs", 2)
    (tmp_path / "work").mkdir(exist_ok=True)
    report = convert_directory(queue, out, info(), **options)
    return report, out


def read_cases(out: Path) -> list[dict]:
    return [json.loads(line) for line in (out / "cases.jsonl").read_text().splitlines()]


def decoded(case: dict) -> dict:
    return {
        "argv": [b64d(a) for a in case["argv"]],
        "files": {b64d(f["path"]): (f["kind"], f.get("mode")) for f in case["files"]},
        "env": {k: b64d(v) for k, v in case["env"].items()},
        "cwd": b64d(case["cwd"]),
    }


def test_the_program_is_never_run_and_nothing_is_written_outside_the_sandbox(tmp_path):
    marker = tmp_path / "ran"
    source = code(
        f'''
        def execute_program(timeout):
            import os, subprocess
            subprocess.run(["sh", "-c", "touch {marker}"])
            subprocess.call(["touch", "{marker}"])
            os.system("touch {marker}")
            os.popen("touch {marker}").read()
            subprocess.Popen(["touch", "{marker}"]).wait()
            try:
                os.execv("/usr/bin/touch", ["touch", "{marker}"])
            except BaseException:
                raise
        '''
    )
    (tmp_path / "work").mkdir()
    recording = record_code(source, 1, RecordSettings(work_dir=str(tmp_path / "work")))
    assert recording.failure is None and not marker.exists()
    runs = recording.data["runs"]
    assert [r["kind"] for r in runs] == [
        "subprocess.run", "subprocess.call", "os.system", "os.popen", "subprocess.Popen", "os.execv",
    ]
    assert all(b64d(r["argv"][0]) in (b"sh", b"touch") for r in runs)


@pytest.mark.parametrize(
    "attempt",
    [
        'open({outside!r}, "w")',
        '__import__("os").remove({victim!r})',
        '__import__("os").rename({victim!r}, {outside!r})',
        '__import__("socket").create_connection(("127.0.0.1", {port}), 1)',
        '__import__("os").fork()',
        '__import__("ctypes").CDLL(None)',
    ],
)
def test_the_guard_refuses_what_a_test_case_must_not_do(tmp_path, attempt):
    """Write or remove outside the sandbox, a network connection, a new process, native code: each is refused, and
    nothing outside changes (a user who may write there would otherwise hide a missing guard)."""

    outside, victim = tmp_path / "outside", tmp_path / "victim"
    victim.write_text("keep")
    server = socket.socket()  # something to connect to, so that only the guard can keep the code out
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.5)
    source = "def execute_program(timeout):\n    " + attempt.format(outside=str(outside), victim=str(victim), port=server.getsockname()[1]) + "\n"
    (tmp_path / "work").mkdir()
    recording = record_code(source, 1, RecordSettings(work_dir=str(tmp_path / "work")))
    assert recording.data["outcome"]["state"] == "raised"
    assert not outside.exists() and victim.read_text() == "keep"
    with pytest.raises(OSError):  # nobody connected
        server.accept()
    server.close()


def test_a_stuck_case_is_given_up_and_its_processes_are_killed(tmp_path):
    source = "def execute_program(timeout):\n    while True:\n        pass\n"
    (tmp_path / "work").mkdir()
    recording = record_code(source, 1, RecordSettings(work_dir=str(tmp_path / "work"), timeout_s=1.5))
    assert recording.failure and recording.failure[0] == concollmic.K_RECORDER_TIMEOUT


def test_t_ms_comes_from_create_time(tmp_path):
    simple = "def execute_program(timeout):\n    import subprocess\n    subprocess.run(['./prog', 'x'])\n"
    cases = [(simple, "2026-03-04 05:06:09.500"), (simple, "2026-03-04 05:06:07.000"), (simple, "2026-03-04 07:06:07.000")]
    report, out = convert(tmp_path, cases, name="default")
    assert [c["t_ms"] for c in read_cases(out)] == [0, 2500, 7_200_000]
    assert [c["origin"] for c in read_cases(out)] == ["concollmic:1", "concollmic:0", "concollmic:2"]
    report, out = convert(tmp_path, cases, name="start", start_time_ms=parse_time_ms("2026-03-04 05:06:04.000"))
    assert [c["t_ms"] for c in read_cases(out)] == [3000, 5500, 7_203_000]
    report, out = convert(tmp_path, cases, name="before", start_time_ms=parse_time_ms("2026-03-04 05:06:08.000"))
    assert [c["t_ms"] for c in read_cases(out)] == [0, 1500, 7_199_000]  # earlier cases are at 0
    report, out = convert(tmp_path, cases, name="budget", drop_after_budget=True)
    assert report.written_cases == 2 and report.beyond_budget == 1
    assert report.failed_by_reason == {concollmic.K_BEYOND: 1}


def test_the_fixture_queue_converts(tmp_path, programs_dir):
    (tmp_path / "work").mkdir()
    program = find_program("demo", programs_dir)
    report = convert_directory(FIXTURES, tmp_path / "b", info(), program=program, work_dir=tmp_path / "work", report_path=tmp_path / "report.json")
    assert report.source_cases == 3 and report.written_cases == 2 and sum(report.failed_by_reason.values()) == 1
    cases = read_cases(tmp_path / "b")
    assert [c["t_ms"] for c in cases] == [0, 2250]
    assert decoded(cases[1])["argv"] == [b"-v", b"--level=3"] and decoded(cases[1])["env"] == {"MODE": b"strict"}
    assert validate_bundle(tmp_path / "b", program=program).ok
    document = json.loads((tmp_path / "report.json").read_text())
    assert document["sources"][2]["status"] == "failed" and document["written_cases"] == 2


def run_cli(capsys, *argv):
    code_ = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code_, captured.out, captured.err


def test_adapt_concollmic_on_the_command_line(tmp_path, programs_dir, capsys):
    (tmp_path / "work").mkdir()
    out = tmp_path / "bundle"
    code_, stdout, stderr = run_cli(
        capsys, "adapt", "concollmic", FIXTURES, "--out", out, "--program-id", "demo", "--programs", programs_dir,
        "--tool-name", "ConcoLLMic", "--family", "llm", "--wall-s", "3600", "--work-dir", tmp_path / "work",
        "--start-time", "2026-01-02 03:04:00.250", "--report", tmp_path / "r.json", "--jobs", "2",
    )
    assert code_ == 0, stderr
    document = json.loads(stdout)
    assert document["source_cases"] == 3 and document["written_cases"] == 2 and sum(document["failed_by_reason"].values()) == 1
    assert [c["t_ms"] for c in read_cases(out)] == [5000, 7250]
    assert "valid bundle with 2 case(s)" in stderr and (tmp_path / "r.json").exists()
    # the output directory is never overwritten
    code_, _out, err = run_cli(
        capsys, "adapt", "concollmic", FIXTURES, "--out", out, "--program-id", "demo", "--programs", programs_dir,
        "--tool-name", "ConcoLLMic", "--wall-s", "3600", "--work-dir", tmp_path / "work",
    )
    assert code_ == 1
    # the fixed track is not available, and the start time is checked
    code_, _out, err = run_cli(
        capsys, "adapt", "concollmic", FIXTURES, "--out", tmp_path / "f", "--program-id", "demo", "--programs", programs_dir,
        "--tool-name", "ConcoLLMic", "--wall-s", "3600", "--track", "fixed",
    )
    assert code_ == 1
    code_, _out, err = run_cli(
        capsys, "adapt", "concollmic", FIXTURES, "--out", tmp_path / "s", "--program-id", "demo", "--programs", programs_dir,
        "--tool-name", "ConcoLLMic", "--wall-s", "3600", "--start-time", "yesterday",
    )
    assert code_ == 1

