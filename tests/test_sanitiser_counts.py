# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Promises S1 to S3 (tests/README.md): what is public about sanitiser findings is a count per kind and nothing a
program wrote, and one bug is one signature.

The program under test is a script that prints standard input to standard error, so each case is the report it
should appear to have caused. All names in the reports are made up.
"""

from __future__ import annotations

import json
import stat
import sys

import pytest
from support import case

from agent_software_testing.signatures import asan_kind, ubsan_kind
from agent_software_testing.validate import validate_document


@pytest.fixture
def sanitiser_build(tmp_path):
    root = (tmp_path / "san").resolve()
    (root / "bin").mkdir(parents=True)
    program = root / "bin" / "prog"
    program.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write(sys.stdin.read())\n")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return root


def asan(kind, *frames, allocation=()):
    text = f"==1==ERROR: AddressSanitizer: {kind} on address 0x602000000015 at pc 0x1 bp 0x2 sp 0x3\nREAD of size 1 at 0x6 thread T0\n"
    text += "".join(f"    #{i} 0x4011b5 in {frame}\n" for i, frame in enumerate(frames))
    if allocation:
        text += "allocated by thread T0 here:\n" + "".join(f"    #{i} 0x4 in {frame}\n" for i, frame in enumerate(allocation))
    return text


def run_signatures(rig, build, reports):
    bundle = rig.bundle([case(i, report.encode()) for i, report in enumerate(reports)], wall_s=100)
    out = rig.tmp / "sig"
    code, text, err = rig.cli(
        "signatures", bundle, "--build", build, "--out", out, "--programs", rig.programs, "--work-dir", rig.work, "--workers", 2
    )
    assert code == 0, err
    return out


def test_one_bug_is_one_signature_and_public_output_names_only_kinds_and_counts(rig, sanitiser_build):
    """S1. A signature is (sanitiser, kind, first frame in the program's own source of the first stack). The same
    bug from several cases counts once; a runtime frame, the allocation stack or a report that invents a kind never
    leaks into the public file."""

    root = sanitiser_build
    reports = [
        asan("heap-buffer-overflow", "__interceptor_memcpy ../runtime/x.cpp:69", f"fn_alpha {root}/lib/a.c:10", f"main {root}/m.c:3"),
        asan("heap-buffer-overflow", f"fn_alpha {root}/lib/a.c:10"),  # the same bug found again
        asan("heap-buffer-overflow", f"fn_beta {root}/lib/b.c:20"),  # same kind, other place
        asan("heap-use-after-free", "memcpy ../runtime/y.cpp:1", allocation=[f"fn_delta {root}/lib/d.c:1"]),  # no own frame in the first stack
        asan("SEGV", "strlen ../runtime/z.cpp:2"),
        f"{root}/lib/c.c:5:3: runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'\n"
        f"    #0 0x4 in fn_gamma {root}/lib/c.c:5:3\n",
        "==1==ERROR: AddressSanitizer: a-kind-the-program-made-up-SECRETWORD\n",
        "nothing wrong here\n",
    ]
    out = run_signatures(rig, root, reports)
    counts = json.loads((out / "signatures-counts.json").read_text())

    assert counts["signatures"] == 6
    assert counts["by_sanitizer_and_kind"] == {
        "asan": {"heap-buffer-overflow": 2, "heap-use-after-free": 1, "segv": 1, "other": 1},
        "ubsan": {"signed-integer-overflow": 1},
    }
    assert counts["cases"] == {"replayed": 8, "with_reports": 7, "skipped_beyond_budget": 0}
    assert sum(counts["status_counts"].values()) == 8
    assert validate_document(out / "signatures-counts.json").ok

    public = (out / "signatures-counts.json").read_text()
    for private in ("fn_alpha", "fn_beta", "fn_gamma", "fn_delta", "a.c", "SECRETWORD", str(root), "0x602"):
        assert private not in public, private

    frames = {(s["sanitizer"], s["kind"], s["frame"]) for s in json.loads((out / "signatures-private.json").read_text())["signatures"]}
    assert frames == {
        ("asan", "heap-buffer-overflow", "lib/a.c:fn_alpha"),
        ("asan", "heap-buffer-overflow", "lib/b.c:fn_beta"),
        ("asan", "heap-use-after-free", "?"),
        ("asan", "segv", "?"),
        ("asan", "other", "?"),
        ("ubsan", "signed-integer-overflow", "lib/c.c:fn_gamma"),
    }


def test_a_pass_whose_cases_never_started_is_no_result_and_not_zero_bugs(rig, sanitiser_build):
    """S1. A binary that cannot start reads as a failure, and nothing is published."""

    binary = next(p for p in sanitiser_build.rglob("*") if p.is_file() and p.stat().st_mode & stat.S_IXUSR)
    binary.write_text("#!/no/such/interpreter\n")
    bundle = rig.bundle([case(0, b"A")], wall_s=100)
    out = rig.tmp / "sig"
    code, _text, err = rig.cli(
        "signatures", bundle, "--build", sanitiser_build, "--out", out, "--programs", rig.programs, "--work-dir", rig.work
    )
    assert code != 0 and "failed to start" in err
    assert not out.exists()


def test_text_a_program_prints_cannot_make_a_long_or_new_kind(rig, sanitiser_build):
    """S2. Anything unrecognised is 'other', however long, so the public count stays a small fixed vocabulary."""

    out = run_signatures(rig, sanitiser_build, ["==1==ERROR: AddressSanitizer: " + "x" * 50_000 + "\n", "a.c:1:1: runtime error: " + "y" * 50_000 + "\n"])
    counts = json.loads((out / "signatures-counts.json").read_text())
    assert counts["by_sanitizer_and_kind"] == {"asan": {"other": 1}, "ubsan": {"other": 1}}
    assert len((out / "signatures-counts.json").read_text()) < 2000


@pytest.mark.parametrize(
    "text,kind",
    [
        ("heap-buffer-overflow on address 0x1 at pc 0x2", "heap-buffer-overflow"),
        ("stack-buffer-overflow on address 0x1", "stack-buffer-overflow"),
        ("heap-use-after-free on address 0x1", "heap-use-after-free"),
        ("SEGV on unknown address 0x0 (pc 0x1)", "segv"),
        ("attempting double-free on 0x1 in thread T0", "double-free"),
        ("attempting free on address which was not malloc()-ed: 0x1", "bad-free"),
        ("negative-size-param: (size=-1)", "negative-size-param"),
        ("odr-violation: detail of it", "odr-violation"),
        ("a report nobody has seen before", "other"),
    ],
)
def test_addresssanitizer_reports_are_sorted_into_the_published_kinds(text, kind):
    """S3. The normalisation tables are part of the measurement; changing one changes published counts."""

    assert asan_kind(text) == kind


@pytest.mark.parametrize(
    "message,kind",
    [
        ("signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'", "signed-integer-overflow"),
        ("shift exponent 40 is too large for 32-bit type 'int'", "shift-exponent"),
        ("shift exponent -1 is negative", "shift-exponent"),
        ("index 5 out of bounds for type 'int [4]'", "index-out-of-bounds"),
        ("load of null pointer of type 'int'", "null-pointer-use"),
        ("member access within null pointer of type 'struct s'", "null-pointer-use"),
        ("division by zero", "division-by-zero"),
        ("an error nobody has seen before", "other"),
    ],
)
def test_undefinedbehaviorsanitizer_messages_are_sorted_into_the_published_kinds(message, kind):
    assert ubsan_kind(message) == kind
