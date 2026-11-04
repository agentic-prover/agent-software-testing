# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Promises H1 to H3 (tests/README.md): a bundle is untrusted, and the validator is what stands between it and a
program that can write files and start processes.

Each hostile bundle is a valid one with one thing made wrong. The valid one must pass, so a validator that refuses
everything does not look strict.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from bundles import b64, case, file_entry, ref, sha, tool_doc, write_bundle

SRC = Path(__file__).resolve().parents[1] / "src"
BODY = b"file body"


def good_case(**over):
    base = case(
        1, 0, argv=[b64("-x")], stdin=ref(b"input"),
        files=[file_entry("dir/data", BODY), {"path": b64("link"), "kind": "symlink", "target": b64("dir/data")}],
        env={"LANG": b64("C")}, cwd=b64("dir"),
    )
    base.update(over)
    return base


GOOD_BLOBS = [BODY, b"input"]


def symlink(path, target):
    return {"path": b64(path), "kind": "symlink", "target": b64(target)}


def write(tmp_path, cases=None, *, blobs=None, tool=None, raw_lines=None):
    return write_bundle(
        tmp_path / "b", cases if cases is not None else [good_case()], GOOD_BLOBS if blobs is None else blobs, tool, raw_lines=raw_lines
    )


def with_second(**over):
    return [good_case(), {**good_case(), "id": "000002", "t_ms": 1, **over}]


# name -> a function that writes a bundle with one thing wrong and returns its folder
def hostile_cases():
    big = [file_entry(f"f{i}", b"x") for i in range(65)]
    yield "NUL in an argument", lambda t: write(t, [good_case(argv=[b64(b"a\0b")])])
    yield "257 arguments", lambda t: write(t, [good_case(argv=[b64("a")] * 257)])
    yield "argument with padding that is wrong", lambda t: write(t, [good_case(argv=["YQ"])])
    yield "argument that is base64 with stray bits", lambda t: write(t, [good_case(argv=["YR=="])])
    yield "path inside that climbs out", lambda t: write(t, [good_case(files=[file_entry("a/../../escape", BODY)], cwd=b64("."))])
    yield "absolute path", lambda t: write(t, [good_case(files=[file_entry("/tmp/escape", BODY)], cwd=b64("."))])
    yield "empty path component", lambda t: write(t, [good_case(files=[file_entry("a//b", BODY)], cwd=b64("."))])
    yield "dot component", lambda t: write(t, [good_case(files=[file_entry("a/./b", BODY)], cwd=b64("."))])
    yield "component of 256 bytes", lambda t: write(t, [good_case(files=[file_entry("a" * 256, BODY)], cwd=b64("."))])
    yield "65 files", lambda t: write(t, [good_case(files=big, cwd=b64("."))], blobs=[b"x", b"input"])
    yield "path listed twice", lambda t: write(t, [good_case(files=[file_entry("a", BODY), file_entry("a", BODY)], cwd=b64("."))])
    yield "file below a link", lambda t: write(t, [good_case(files=[symlink("l", "."), file_entry("l/x", BODY)], cwd=b64("."))])
    yield "file below a file", lambda t: write(t, [good_case(files=[file_entry("a", BODY), file_entry("a/b", BODY)], cwd=b64("."))])
    yield "link that climbs out of the case", lambda t: write(t, [good_case(files=[symlink("l", "../../etc")], cwd=b64("."))])
    yield "link to an absolute path", lambda t: write(t, [good_case(files=[symlink("l", "/etc/passwd")], cwd=b64("."))])
    yield "link that climbs out through a directory", lambda t: write(t, [good_case(files=[symlink("d/l", "../../x")], cwd=b64("."))])
    yield "working directory that is not created", lambda t: write(t, [good_case(cwd=b64("nowhere"))])
    yield "working directory that climbs out", lambda t: write(t, [good_case(cwd=b64(".."))])
    yield "working directory that is a file", lambda t: write(t, [good_case(cwd=b64("dir/data"))])
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "GCONV_PATH", "PATH", "HOME", "TMPDIR", "GCOV_PREFIX", "ASAN_OPTIONS",
                 "MALLOC_CONF", "AGENT_SOFTWARE_TESTING_CASE", "http_proxy", "HTTPS_PROXY"):
        yield f"environment variable {name}", lambda t, name=name: write(t, [good_case(env={name: b64("x")})])
    yield "password in an environment URL", lambda t: write(t, [good_case(env={"U": b64("https://user:secret@host/x")})])
    yield "timeout of 0", lambda t: write(t, [good_case(timeout_ms=0)])
    yield "timeout above 30 s", lambda t: write(t, [good_case(timeout_ms=30001)])
    yield "set-user-id mode", lambda t: write(t, [good_case(files=[file_entry("a", BODY, mode="4755")], cwd=b64("."))])
    yield "unknown member of a case", lambda t: write(t, [good_case(extra=1)])
    yield "unknown member of tool.json", lambda t: write(t, tool={**tool_doc(), "surprise": True})
    yield "wrong schema in tool.json", lambda t: write(t, tool={**tool_doc(), "schema": "agent-software-testing/tool@0.2"})
    yield "other source than the program's", lambda t: write(t, tool={**tool_doc(), "program": {"id": "demo", "source_sha256": "0" * 64}})
    yield "budget of 0", lambda t: write(t, tool=tool_doc(wall_s=0))
    yield "budget above seven days", lambda t: write(t, tool=tool_doc(wall_s=604801))
    yield "repeat 0", lambda t: write(t, tool={**tool_doc(), "repeat": 0})
    yield "same id twice", lambda t: write(t, with_second(id="000001"))
    yield "time going backwards", lambda t: write(t, [{**good_case(), "t_ms": 5}, {**good_case(), "id": "000002", "t_ms": 4}])
    yield "terminal and pipe at once", lambda t: write(t, [good_case(stdin_tty=True, stdin_pipe=True)])
    yield "blob that does not exist", lambda t: write(t, blobs=[b"input"])
    yield "blob whose content is not its name", lambda t: _wrong_blob(write(t))
    yield "blob that is a link", lambda t: _blob_as_link(write(t))
    yield "cases.jsonl without its last line feed", lambda t: _no_final_newline(write(t))
    yield "fixed track with other arguments", lambda t: write(
        t, [case(1, 0, argv=[b64("test"), b64("other")], files=[file_entry("input", b"x")])], blobs=[b"x"],
        tool=tool_doc("fixfile", track="fixed"),
    )
    yield "fixed track with a file of another name", lambda t: write(
        t, [case(1, 0, argv=[b64("test"), b64("input")], files=[file_entry("data", b"x")])], blobs=[b"x"],
        tool=tool_doc("fixfile", track="fixed"),
    )
    yield "fixed track that sets the environment", lambda t: write(
        t, [case(1, 0, argv=[b64("-q")], env={"LANG": b64("C")})], tool=tool_doc("fixstdin", track="fixed"),
    )
    yield "fixed track for a program without one", lambda t: write(t, [case(1, 0)], tool=tool_doc("nofixed", track="fixed"))


def _wrong_blob(root):
    (root / "blobs" / sha(b"input")).write_bytes(b"something else")
    return root


def _blob_as_link(root):
    target = root.parent / "elsewhere"
    target.write_bytes(b"input")
    (root / "blobs" / sha(b"input")).unlink()
    os.symlink(target, root / "blobs" / sha(b"input"))
    return root


def _no_final_newline(root):
    path = root / "cases.jsonl"
    path.write_text(path.read_text().rstrip("\n"))
    return root


HOSTILE = dict(hostile_cases())


def test_the_valid_bundle_these_start_from_is_valid(rig, tmp_path):
    code, report = rig.validate(write(tmp_path))
    assert code == 0 and report["ok"] and report["errors"] == [], report


@pytest.mark.parametrize("name", list(HOSTILE))
def test_a_hostile_bundle_is_refused(rig, tmp_path, name):
    """H1. Nothing that a case lists may reach outside its own directory, set what the runner owns, exceed the
    limits, or disagree with its blobs, its program or its track."""

    code, report = rig.validate(HOSTILE[name](tmp_path))
    assert code == 1 and not report["ok"] and report["errors"], f"accepted: {name}"


def test_a_bundle_at_every_limit_is_accepted(rig, tmp_path):
    """H1. The other side of the limits: the largest valid values pass, so a limit is not a refusal in disguise.
    Locale and time-zone variables are allowed; paths may be any bytes."""

    odd = b"caf\xe9:\xff"  # not UTF-8, with a colon
    files = [file_entry("a" * 255, b""), file_entry("placeholder", BODY, mode="644")]
    files[1]["path"] = b64(odd)
    files += [{"path": b64("deep/er/dir"), "kind": "dir", "mode": "0755"}, symlink("dangling", "missing")]
    at_limit = case(
        1, 0, argv=[b64("a")] * 256, files=files, env={"LC_ALL": b64("C"), "TZ": b64("NPT-5:45"), "LANG": b64("C")},
        timeout_ms=30000, stdin=ref(b""),
    )
    low = {**case(2, 0, timeout_ms=1), "id": "000002"}
    code, report = rig.validate(write(tmp_path, [at_limit, low], blobs=[b"", BODY], tool=tool_doc(wall_s=604800)))
    assert code == 0, report["errors"]


def test_every_problem_is_reported_not_only_the_first(rig, tmp_path):
    code, report = rig.validate(write(tmp_path, [good_case(argv=[b64(b"a\0")], timeout_ms=0, env={"LD_PRELOAD": b64("x")})]))
    assert code == 1 and len({e["code"] for e in report["errors"]}) >= 3


def test_cases_after_the_budget_are_a_warning_unless_the_budget_is_strict(rig, tmp_path):
    """H2. Such cases are skipped by the replay and counted; --strict-budget makes the validator refuse them."""

    bundle = write(tmp_path, [good_case(), {**good_case(), "id": "000002", "t_ms": 3_600_001}])
    code, report = rig.validate(bundle)
    assert code == 0 and any("budget" in w["message"] for w in report["warnings"])
    assert rig.validate(bundle, "--strict-budget")[0] == 1


def test_a_line_longer_than_the_limit_is_refused_even_when_it_is_valid_json(rig, tmp_path):
    """H3. cases.jsonl is untrusted input of any size. A line of more than 8 MiB cannot be a valid case (its bodies are
    in blobs/), so it is refused on its length, here with a case that would be fine if it were shorter."""

    bundle = write(tmp_path)
    line = (bundle / "cases.jsonl").read_text().rstrip("\n")
    (bundle / "cases.jsonl").write_text(line[:-1] + " " * (9 * 1024 * 1024) + "}\n")
    assert json.loads((bundle / "cases.jsonl").read_text())["id"] == "000001"
    code, report = rig.validate(bundle)
    assert code == 1 and not report["ok"]


def test_a_named_pipe_in_the_place_of_a_blob_is_refused_at_once(tmp_path):
    """H3. Opening a FIFO waits for a writer; a validator that does so hangs on a hostile bundle."""

    bundle = write(tmp_path)
    blob = bundle / "blobs" / sha(b"input")
    blob.unlink()
    os.mkfifo(blob)
    done = subprocess.run(
        [sys.executable, "-m", "agent_software_testing", "validate", str(bundle), "--programs", str(Path(__file__).parent / "fixtures" / "programs")],
        env={**os.environ, "PYTHONPATH": str(SRC)}, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 1


def test_a_bundle_folder_or_file_that_is_a_link_is_refused(rig, tmp_path):
    """H3. The folder, tool.json and cases.jsonl must be what they say: a link could point anywhere."""

    real = write(tmp_path)
    link = tmp_path / "linked"
    os.symlink(real, link)
    assert rig.validate(link)[0] == 1
    for name in ("tool.json", "cases.jsonl"):
        other = write(tmp_path / name.split(".")[0])
        (other / name).rename(other.parent / f"moved-{name}")
        os.symlink(other.parent / f"moved-{name}", other / name)
        assert rig.validate(other)[0] == 1, name


@pytest.mark.parametrize(
    ("value", "found"),
    [
        (b"https://user:secret@host/x", True),
        (b"see git+ssh://user:secret@host", True),
        (b"a1://user:secret@host", True),
        (b"1://user:secret@host", False),
        (b"://user:secret@host", False),
        (b"https://user@host", False),
        (b"https://host/user:secret@x", False),
        (b"first://a:b third://user:secret@host", True),
    ],
)
def test_a_url_with_a_password_is_recognised_in_an_environment_value(value, found):
    """H1. The check the validator and the adapters rely on, on its own."""

    sys.path.insert(0, str(SRC))
    from agent_software_testing.common import value_has_credentials

    assert value_has_credentials(value) is found


def test_a_long_environment_value_does_not_hold_up_the_validator():
    """H1. A bundle is hostile: the time to check a value grows with its length, not with its square."""

    sys.path.insert(0, str(SRC))
    from agent_software_testing.common import value_has_credentials

    started = time.monotonic()
    assert not value_has_credentials(b"v" * 4_000_000)
    assert not value_has_credentials(b"://v" * 1_000_000)
    assert time.monotonic() - started < 10
