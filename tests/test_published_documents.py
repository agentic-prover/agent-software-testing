# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Promises D1 to D3 (tests/README.md): the documents other people write against (program definitions, example
bundles, JSON Schemas) are valid, agree with the runner and with each other.

Skipped where the directories are absent, for example in a source distribution of the package alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bundles import b64, case, file_entry, tool_doc

from agent_software_testing.common import branch_set_document
from agent_software_testing.kinds import ASAN_KINDS, UBSAN_KINDS
from agent_software_testing.program import load_program
from agent_software_testing.validate import validate_bundle, validate_document

ROOT = Path(__file__).resolve().parents[1]
PROGRAMS = sorted((ROOT / "programs").glob("*/program.json"))
EXAMPLES = sorted(p for p in (ROOT / "docs" / "examples").glob("*") if (p / "tool.json").exists())
SCHEMAS = ROOT / "schemas"

jsonschema = pytest.importorskip("jsonschema", reason="optional: only used to cross-check the schemas")
if not hasattr(jsonschema, "Draft202012Validator"):
    pytest.skip("the schemas are Draft 2020-12; jsonschema 4.0 or later is needed", allow_module_level=True)


def validator(name: str):
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / f"{name}.schema.json").read_text()))


@pytest.mark.skipif(not PROGRAMS, reason="no programs/ directory in this checkout")
def test_every_program_definition_is_valid_and_matches_its_schema():
    """D1. The folder name is the id; a definition with a wrong total or a missing field would make every replay of
    that program fail, or worse pass with the wrong denominator."""

    problems = []
    for path in PROGRAMS:
        report = validate_document(path)
        problems += [f"{path.parent.name}: {e}" for e in report.errors]
        if load_program(path).id != path.parent.name:
            problems.append(f"{path.parent.name}: the id is not the name of the folder")
        problems += [f"{path.parent.name}: {e.message}" for e in validator("program").iter_errors(json.loads(path.read_text()))]
    assert problems == []


@pytest.mark.skipif(not EXAMPLES, reason="no example bundles in this checkout")
def test_the_example_bundles_people_copy_are_valid():
    """D1. The first thing a new submitter runs is the validator on the example."""

    problems = []
    for path in EXAMPLES:
        report = validate_bundle(path, programs_dir=ROOT / "programs")
        problems += [f"{path.name}: {e}" for e in report.errors]
        problems += [f"{path.name}: {e.message}" for e in validator("tool").iter_errors(json.loads((path / "tool.json").read_text()))]
        for line in (path / "cases.jsonl").read_text().splitlines():
            problems += [f"{path.name}: {e.message}" for e in validator("case").iter_errors(json.loads(line))]
    assert problems == []


def test_what_the_runner_writes_satisfies_its_schemas(rig):
    """D2. replay.json, the branch sets and the bundle files the adapters and BundleWriter write validate against
    the published schemas and against the runner's own validator."""

    files = [
        {"path": b"input", "kind": "file", "mode": "0644", "body": b"B"},
        {"path": b"d", "kind": "dir", "mode": "0755"},
        {"path": b"l", "kind": "symlink", "target": b"input"},
        {"path": b"p", "kind": "fifo", "mode": "0600", "body": b"x"},
    ]
    bundle = rig.bundle([case_for_writer(files), {"t_ms": 10}], wall_s=100)
    for line in (bundle / "cases.jsonl").read_text().splitlines():
        validator("case").validate(json.loads(line))
    validator("tool").validate(json.loads((bundle / "tool.json").read_text()))
    run = rig.replay(bundle)
    assert run.code == 0, run.err
    for name, schema in (("replay.json", "replay"), ("coverage-branches.json", "branch-set"), ("snapshots/0.json", "branch-set")):
        validator(schema).validate(run.json(name))
        assert validate_document(run.dir / name).ok


def case_for_writer(files):
    return {"t_ms": 0, "argv": [b"-x"], "stdin": b"A", "files": files, "env": {"GZIP": b"-9"}, "cwd": b"d", "origin": "test"}


def test_the_schemas_refuse_what_the_validator_refuses():
    """D2. An outsider who checks a bundle with the schema alone is not told that a bad one is fine."""

    good = case(1, 0, argv=[b64("a")], files=[file_entry("f", b"x")])
    validator("case").validate(good)
    for bad in [
        {**good, "t_ms": -1},
        {**good, "argv": ["not base64!"]},
        {**good, "surprise": True},
        {**good, "timeout_ms": 30001},
        {**good, "env": {"PATH": b64("x")}},
        {**good, "env": {"LD_PRELOAD": b64("x")}},
        {**good, "stdin": "sha256:xyz"},
        {**good, "files": [{"path": b64("a"), "kind": "device"}]},
        {**good, "files": [file_entry("f", b"x")] * 65},
        {k: v for k, v in good.items() if k != "argv"},
    ]:
        assert not validator("case").is_valid(bad), bad
    tool = validator("tool")
    assert tool.is_valid(tool_doc())
    for bad in [{**tool_doc(), "track": "x"}, {**tool_doc(), "repeat": 0}, {k: v for k, v in tool_doc().items() if k != "budget"}]:
        assert not tool.is_valid(bad), bad
    sets = validator("branch-set")
    assert sets.is_valid(branch_set_document("demo", 10, ["a.c:1:0:0"]))
    assert not sets.is_valid({**branch_set_document("demo", 10, ["a.c:1:0:0"]), "covered_branch_keys": ["not-a-key"]})


def test_the_public_kind_vocabulary_is_the_same_in_the_code_and_in_the_schema():
    """D3. The counts file may only name kinds the schema lists; the two lists are one list."""

    schema = json.loads((SCHEMAS / "signature-counts.schema.json").read_text())
    kinds = schema["properties"]["by_sanitizer_and_kind"]["properties"]
    assert set(kinds["asan"]["propertyNames"]["enum"]) == ASAN_KINDS
    assert set(kinds["ubsan"]["propertyNames"]["enum"]) == UBSAN_KINDS
