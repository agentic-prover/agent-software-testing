# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Promises M1 to M10 (tests/README.md): the numbers a replay publishes are the numbers the cases earned.

Everything goes through the command line and the files it writes. The fake toolchain stands in for gcc, gcov and
lcov (see tests/fake_toolchain.py for what each input covers).
"""

from __future__ import annotations

import json
import re

import pytest
from support import A, B, BASE, DASH_X, H, Q, U, UNIVERSE_SIZE, case

from agent_software_testing.validate import validate_document


def test_snapshots_hold_the_cases_due_by_then_and_the_budget_is_inclusive(rig):
    """M1. A case belongs to the first snapshot time T with t_ms <= 1000*T; the budget itself is replayed, a
    millisecond later is not; the curve, the last snapshot and the final set tell one story."""

    bundle = rig.bundle(
        [
            case(0, b"A"),
            case(10_000, b"B"),  # exactly on the first interval: due at 10 s
            case(10_001, b"U"),  # one millisecond later: due at 20 s
            case(25_000, b"H"),  # exactly the budget: replayed
            case(25_001, b"Q"),  # beyond the budget: skipped
        ],
        wall_s=25,
    )
    run = rig.replay(bundle, "--interval", 10)
    assert run.code == 0, run.err
    summary = run.summary
    assert summary["flake"]["branches_differing"] == 0  # the second replay holds the same cases, the boundary one included

    assert summary["snapshots"] == [
        {"t_s": 0, "cases": 1, "covered": 2},
        {"t_s": 10, "cases": 2, "covered": 3},
        {"t_s": 20, "cases": 3, "covered": 4},
        {"t_s": 25, "cases": 4, "covered": 5},
    ]
    assert summary["cases"] == {"total": 5, "replayed": 4, "skipped_beyond_budget": 1}
    assert run.covered == sorted([BASE, A, B, U, H])  # Q was never replayed
    assert Q not in run.covered
    assert summary["branch_covered"] == len(run.covered) == 5 and summary["branch_total"] == UNIVERSE_SIZE

    assert (run.dir / "timeline.csv").read_text() == "t_s,covered\n0,2\n10,3\n20,4\n25,5\n"
    for snap in summary["snapshots"]:
        doc = run.json(f"snapshots/{snap['t_s']}.json")
        assert doc["branch_covered"] == snap["covered"] == len(doc["covered_branch_keys"])
        assert doc["cases"] == snap["cases"] and doc["t_s"] == snap["t_s"]
    assert run.json("snapshots/25.json")["covered_branch_keys"] == run.covered
    assert run.json("snapshots/0.json")["covered_branch_keys"] == sorted([BASE, A])


def test_a_snapshot_without_new_cases_repeats_the_one_before(rig):
    """M1. A reader finds a file and a row for every snapshot time, also when nothing was due."""

    run = rig.replay(rig.bundle([case(0, b"A"), case(25_000, b"B")], wall_s=25), "--interval", 10, "--no-flake-check")
    assert [(s["t_s"], s["cases"], s["covered"]) for s in run.summary["snapshots"]] == [
        (0, 1, 2), (10, 1, 2), (20, 1, 2), (25, 2, 3),
    ]
    assert run.json("snapshots/10.json")["covered_branch_keys"] == run.json("snapshots/0.json")["covered_branch_keys"]


def test_cases_that_crash_time_out_or_fail_are_counted_and_whatever_coverage_they_wrote_stays(rig):
    """M2. Every replayed case contributes what its process wrote; the status counts say what happened."""

    bundle = rig.bundle(
        [
            case(0, b"A"),
            case(1, b"FAIL B"),  # writes its coverage, then exits with status 3
            case(2, b"CRASH U"),  # killed by SIGSEGV before it writes anything
            case(3, b"SLEEP H", timeout_ms=400),  # runs into its time limit before it writes anything
        ],
        wall_s=100,
    )
    run = rig.replay(bundle, "--no-flake-check")
    assert run.summary["status_counts"] == {
        "ok": 1, "nonzero_exit": 1, "terminated_by_signal": 1, "timeout": 1, "setup_error": 0,
    }
    assert run.summary["timeouts"]["count"] == 1
    assert run.covered == sorted([BASE, A, B])  # B came from the case that failed; U and H from nowhere


def test_the_result_does_not_depend_on_how_many_shards_or_which_kind_serves_them(rig):
    """M3. Shard count and shard mode change speed only."""

    cases = [case(1000 * i, data) for i, data in enumerate([b"A", b"B", b"U", b"H", b"Q", b"A U", b"-", b"B H"])]
    bundle = rig.bundle(cases, wall_s=20)
    runs = [
        rig.replay(bundle, "--interval", 5, "--workers", 1, "--shard-mode", "thread"),
        rig.replay(bundle, "--interval", 5, "--workers", 3, "--shard-mode", "process"),
        rig.replay(bundle, "--interval", 5, "--workers", 8, "--shard-mode", "thread"),  # more shards than cases
    ]
    assert all(r.code == 0 for r in runs), [r.err for r in runs]
    first = runs[0]
    assert first.summary["branch_covered"] == 6
    for other in runs[1:]:
        assert other.summary["snapshots"] == first.summary["snapshots"]
        assert other.summary["status_counts"] == first.summary["status_counts"]
        assert other.covered == first.covered
        for t in (0, 5, 10, 15, 20):
            assert other.json(f"snapshots/{t}.json")["covered_branch_keys"] == first.json(f"snapshots/{t}.json")["covered_branch_keys"]


def test_the_flake_check_reports_what_differs_and_only_the_first_replay_counts(rig):
    """M4. A branch that shows in one replay of the corpus and not in the next is reported as noise and stays
    in the result; a repeatable corpus reports none; the check can be switched off and says so."""

    even, odd = rig.tmp / "even", rig.tmp / "odd"
    flaky = rig.bundle([case(0, b"A"), case(1, b"B", argv=[f"--flake={even}".encode(), f"--flake-odd={odd}".encode()])], wall_s=100)
    run = rig.replay(flaky, "--workers", 1)
    assert run.summary["flake"] == {"checked": True, "branches_differing": 2, "only_in_first": 1, "only_in_second": 1}
    assert "src/prog.c:12:0:1" in run.covered  # seen in the first replay only, and published
    assert DASH_X not in run.covered  # seen in the second replay only, and never filled in

    steady = rig.bundle([case(0, b"A"), case(1, b"B U")], wall_s=100)
    run = rig.replay(steady)
    assert run.summary["flake"] == {"checked": True, "branches_differing": 0, "only_in_first": 0, "only_in_second": 0}

    run = rig.replay(steady, "--no-flake-check")
    assert run.summary["flake"] == {"checked": False}


def test_an_empty_corpus_is_zero_coverage_not_a_failure(rig):
    """M5. Zero cases is a valid result with every snapshot at zero."""

    run = rig.replay(rig.bundle([], wall_s=20), "--interval", 10)
    assert run.code == 0, run.err
    assert run.summary["branch_covered"] == 0 and run.covered == []
    assert [s["covered"] for s in run.summary["snapshots"]] == [0, 0, 0]
    assert run.summary["flake"] == {"checked": False}


def _make_stale_profile(rig):
    (rig.build / "obj" / "prog.gcda").write_text("{}")


def _make_build_that_writes_no_profiles(rig):
    program = rig.build / "bin" / "prog"
    lines = [line for line in program.read_text().splitlines() if line.startswith(("#!", "# gcda"))]
    program.write_text("\n".join(lines) + "\nimport sys\nsys.exit(0)\n")


def _make_lcov_warn(rig):
    rig.monkeypatch.setenv("FAKE_LCOV_WARN", "1")


def _make_capture_fail_after_output(rig):
    rig.monkeypatch.setenv("FAKE_LCOV_FAIL_CAPTURE", "1")


def _make_capture_of_damaged_profile(rig):
    rig.monkeypatch.setenv("FAKE_LCOV_DAMAGED", "1")


def _make_capture_with_foreign_branch(rig):
    rig.monkeypatch.setenv("FAKE_LCOV_EXTRA", "1")


REFUSALS = {
    "lcov warns": (_make_lcov_warn, [b"A"]),
    "a capture that fails after writing an output file": (_make_capture_fail_after_output, [b"A"]),
    "a capture with a branch the universe does not hold": (_make_capture_with_foreign_branch, [b"A"]),
    "a capture of damaged profile data": (_make_capture_of_damaged_profile, [b"A"]),
    "stale profile data in the build": (_make_stale_profile, [b"A"]),
    "build that never writes profiles": (_make_build_that_writes_no_profiles, [b"A"]),
    "a program that writes a stray file into the profile tree": (lambda rig: None, [b"A JUNK"]),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_a_replay_that_cannot_be_trusted_stops_and_publishes_nothing(rig, name):
    """M6. No exit status 0, no output folder, no half-written leftovers, and never a zero that stands for
    a failure."""

    prepare, inputs = REFUSALS[name]
    prepare(rig)
    run = rig.replay(rig.bundle([case(i, data) for i, data in enumerate(inputs)], wall_s=100))
    assert run.code == 1, run.out
    assert run.err.strip() != ""
    assert not run.dir.exists()
    assert [p.name for p in run.dir.parent.iterdir() if p.name.startswith(".")] == []
    assert list(rig.work.iterdir()) == []  # the scratch space is cleaned up as well


def test_one_invalid_case_stops_the_whole_replay(rig):
    """M6. The validator is the gate: a bundle with a single bad case among good ones is not replayed in part."""

    bundle = rig.bundle([case(0, b"A"), case(1, b"B")], wall_s=100)
    with open(bundle / "cases.jsonl", "a") as handle:
        handle.write(json.dumps({"id": "000003", "t_ms": 2, "argv": [], "files": [{"path": "Li4vZXNjYXBl", "kind": "dir"}]}) + "\n")
    lcov_calls = rig.tmp / "lcov-calls"
    rig.monkeypatch.setenv("FAKE_LCOV_LOG", str(lcov_calls))
    run = rig.replay(bundle)
    assert run.code == 1 and not run.dir.exists()
    assert not lcov_calls.exists()  # refused before any work was done, not after hours of it


def test_the_universe_must_be_the_one_the_program_definition_names(rig, tmp_path):
    """M6. A build with more or fewer branches than branch_total is the wrong build or the wrong definition."""

    definition = json.loads((rig.programs / "demo" / "program.json").read_text())
    definition["branch_total"] = 11
    (tmp_path / "programs" / "demo").mkdir(parents=True)
    (tmp_path / "programs" / "demo" / "program.json").write_text(json.dumps(definition))
    run = rig.replay(rig.bundle([case(0, b"A")]), programs=tmp_path / "programs")
    assert run.code == 1 and not run.dir.exists()


def test_results_are_never_written_over_existing_ones(rig):
    """M7. A non-empty output folder is refused at once and left as it was; an empty one is fine."""

    bundle = rig.bundle([case(0, b"A")], wall_s=100)
    out = rig.tmp / "taken"
    out.mkdir()
    (out / "earlier.txt").write_text("keep me")
    lcov_calls = rig.tmp / "lcov-calls"
    rig.monkeypatch.setenv("FAKE_LCOV_LOG", str(lcov_calls))
    run = rig.replay(bundle, out=out)
    assert run.code == 1
    assert not lcov_calls.exists()  # refused at the start, before a replay that may take hours
    assert [p.name for p in out.iterdir()] == ["earlier.txt"] and (out / "earlier.txt").read_text() == "keep me"

    empty = rig.tmp / "empty"
    empty.mkdir()
    run = rig.replay(bundle, "--no-flake-check", out=empty)
    assert run.code == 0 and (empty / "replay.json").is_file()


def test_the_record_names_exactly_what_was_replayed_and_leaks_nothing_of_the_machine(rig):
    """M8. replay.json carries the digests of the bundle files and of the build, the program and tool as
    submitted, and no path or user name of the machine that replayed."""

    import hashlib

    bundle = rig.bundle([case(0, b"A")], wall_s=100)
    run = rig.replay(bundle, "--no-flake-check")
    s = run.summary
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    assert s["bundle"] == {"tool_json_sha256": sha(bundle / "tool.json"), "cases_jsonl_sha256": sha(bundle / "cases.jsonl")}
    assert s["build"]["binary_sha256"] == sha(rig.build / "bin" / "prog")
    assert s["build"]["program_json_sha256"] == sha(rig.programs / "demo" / "program.json")
    assert re.fullmatch(r"[0-9a-f]{64}", s["build"]["gcno_set_sha256"])
    tool_json = json.loads((bundle / "tool.json").read_text())
    assert s["tool"] == tool_json["tool"] and s["program"] == tool_json["program"] and s["repeat"] == tool_json["repeat"]
    assert s["profile"] == "standard" and s["budget_s"] == 100 and s["interval_s"] == 900

    import getpass

    for path in run.dir.rglob("*"):
        if path.is_file():
            text = path.read_text()
            for private in (str(rig.tmp), getpass.getuser()):
                assert private not in text, f"{path.name} mentions {private}"
    for name in ("replay.json", "coverage-branches.json", "snapshots/0.json"):
        assert validate_document(run.dir / name).ok


def test_who_made_the_cases_does_not_change_the_measurement(rig):
    """M9. Tool name, family, model and each case's origin are labels; the same cases give the same numbers."""

    def bundle(name, family, model, origin):
        tool = json.loads(json.dumps(__import__("bundles").tool_doc("demo", wall_s=100)))
        tool["tool"].update(name=name, family=family, model=model)
        cases = [case(0, b"A", origin=origin), case(5, b"B U -", origin=origin), case(9, b"H", origin=origin)]
        return rig.bundle([{**c, "argv": [b"-x"] if i == 1 else []} for i, c in enumerate(cases)], tool=tool)

    one = rig.replay(bundle("AFL++", "fuzzer", None, "queue"), "--no-flake-check")
    two = rig.replay(bundle("Some Agent", "agent", "a-model", "llm"), "--no-flake-check")
    assert one.covered == two.covered == sorted([BASE, A, B, U, H, DASH_X])
    assert one.summary["snapshots"] == two.summary["snapshots"]


def test_the_earlier_pipeline_profile_takes_every_case_into_its_last_snapshot(rig):
    """M10. The standard profile skips cases after the budget; canonical-2026 replays them in the last snapshot.
    A number is only comparable inside one profile, so the profile must change what is replayed."""

    bundle = rig.bundle([case(0, b"A"), case(30_000, b"B")], wall_s=20)
    standard = rig.replay(bundle, "--interval", 10, "--no-flake-check")
    canonical = rig.replay(bundle, "--interval", 10, "--no-flake-check", "--profile", "canonical-2026")
    assert standard.summary["profile"] == "standard" and canonical.summary["profile"] == "canonical-2026"
    assert standard.covered == sorted([BASE, A]) and standard.summary["cases"]["skipped_beyond_budget"] == 1
    assert canonical.covered == sorted([BASE, A, B]) and canonical.summary["cases"]["skipped_beyond_budget"] == 0
