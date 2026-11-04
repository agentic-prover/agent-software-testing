# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Pieces every adapter shares."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bundle import BundleWriter, make_tool_document
from ..common import MAX_BLOB_BYTES, MAX_CASE_BYTES, AgentSoftwareTestingError


class AdapterError(AgentSoftwareTestingError):
    """A native test case cannot be expressed as a case of this format."""


@dataclass
class AdapterReport:
    """What an adapter did: counts and the reasons for every skipped case."""

    source_cases: int = 0
    written_cases: int = 0
    duplicates: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append({"name": name, "reason": reason})

    def to_json(self) -> dict[str, Any]:
        return {
            "source_cases": self.source_cases,
            "written_cases": self.written_cases,
            "duplicates": self.duplicates,
            "skipped": len(self.skipped),
            "skipped_examples": self.skipped[:20],
            "notes_total": len(self.notes),
            "notes": self.notes[:50],
        }


@dataclass
class ToolInfo:
    """Metadata every adapter needs for ``tool.json``."""

    name: str
    program_id: str
    source_sha256: str
    version: str | None = None
    family: str = "other"
    model: str | None = None
    config_sha256: str | None = None
    url: str | None = None
    track: str = "open"
    repeat: int = 1
    #: The campaign's wall-clock budget in seconds. Required to write ``tool.json``:
    #: it is a fact about the campaign that only the submitter knows, so there
    #: is no default.
    wall_s: int | None = None
    #: CPU cores the tool used; written only when given (absent means not stated).
    cores: int | None = None
    budget_notes: str = ""
    output_tokens: int | None = None
    input_tokens: int | None = None
    cpu_s: float | None = None
    notes: str = ""

    def document(self) -> dict[str, Any]:
        if self.wall_s is None:
            raise AdapterError("the campaign budget is required: pass --wall-s (seconds the campaign was allowed)")
        return make_tool_document(
            name=self.name,
            program_id=self.program_id,
            source_sha256=self.source_sha256,
            version=self.version,
            family=self.family,
            model=self.model,
            config_sha256=self.config_sha256,
            url=self.url,
            track=self.track,
            repeat=self.repeat,
            wall_s=self.wall_s,
            cores=self.cores,
            budget_notes=self.budget_notes,
            output_tokens=self.output_tokens,
            input_tokens=self.input_tokens,
            cpu_s=self.cpu_s,
            notes=self.notes,
        )


def new_writer(out_dir: Path | str, info: ToolInfo) -> BundleWriter:
    return BundleWriter(out_dir, info.document())


def check_body_size(*bodies: bytes) -> str | None:
    """A reason when the bodies cannot fit the per-blob and per-case limits."""

    for body in bodies:
        if len(body) > MAX_BLOB_BYTES:
            return f"a body of {len(body)} bytes exceeds the blob limit of {MAX_BLOB_BYTES}"
    if sum(len(b) for b in bodies) > MAX_CASE_BYTES:
        return f"bodies total more than the case limit of {MAX_CASE_BYTES} bytes"
    return None


def precheck_case(
    *,
    argv: list[bytes],
    stdin: bytes | None,
    files: list[dict[str, Any]],
    env: dict[str, bytes],
) -> str | None:
    """Cheap checks so adapters never write a case the validator would reject.

    Returns a reason, or ``None`` when the case is fine as far as these checks
    can tell. Blob limits are checked on the raw bodies; the validator remains
    the final authority.
    """

    from ..common import (
        MAX_ARGV_BYTES,
        MAX_ARGV_ENTRIES,
        MAX_ENV_VARS,
        MAX_EXEC_STRING_BYTES,
        MAX_EXEC_TOTAL_BYTES,
        MAX_FILES_PER_CASE,
        env_name_forbidden,
        value_has_credentials,
        path_problems,
    )

    if len(argv) > MAX_ARGV_ENTRIES:
        return f"{len(argv)} arguments, limit is {MAX_ARGV_ENTRIES}"
    if sum(len(a) for a in argv) > MAX_ARGV_BYTES:
        return f"arguments are larger than {MAX_ARGV_BYTES} bytes"
    if any(b"\0" in a for a in argv):
        return "an argument contains a NUL byte"
    if any(len(a) > MAX_EXEC_STRING_BYTES for a in argv):
        return f"an argument is longer than {MAX_EXEC_STRING_BYTES} bytes"
    if len(files) > MAX_FILES_PER_CASE:
        return f"{len(files)} files, limit is {MAX_FILES_PER_CASE}"
    if len(env) > MAX_ENV_VARS:
        return f"{len(env)} environment variables, limit is {MAX_ENV_VARS}"
    for name, value in env.items():
        if env_name_forbidden(name):
            return f"environment variable {name} cannot be set by a case"
        if b"\0" in value:
            return f"environment variable {name} contains a NUL byte"
        if value_has_credentials(value):
            return f"environment variable {name} holds credentials"
        if len(name) + 1 + len(value) > MAX_EXEC_STRING_BYTES:
            return f"environment variable {name} is longer than {MAX_EXEC_STRING_BYTES} bytes"
    exec_bytes = sum(len(a) + 1 for a in argv) + sum(len(n) + 1 + len(v) + 1 for n, v in env.items())
    if exec_bytes > MAX_EXEC_TOTAL_BYTES:
        return f"arguments and environment together are {exec_bytes} bytes, limit is {MAX_EXEC_TOTAL_BYTES}"
    bodies = [b for b in [stdin] if b is not None]
    for entry in files:
        problems = path_problems(entry["path"])
        if problems:
            return f"file path {entry['path']!r}: {problems[0][1]}"
        if entry["kind"] in ("file", "fifo", "tty"):
            bodies.append(entry["body"])
    return check_body_size(*bodies)
