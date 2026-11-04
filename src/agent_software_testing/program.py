# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Program definitions (``programs/<id>/program.json``, contract section 3)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import (
    FIXED_INPUT_NAME,
    FIXED_INPUT_PLACEHOLDER,
    INPUT_CHANNELS,
    PROGRAM_ID_RE,
    PROGRAM_INPUTS,
    SCHEMA_PROGRAM,
    SHA256_RE,
    AgentSoftwareTestingError,
    ValidationError,
    is_plain_int,
    parse_json_bytes,
    path_problems,
    read_regular_bytes,
    sha256_hex,
)

#: A program definition is a small file.
MAX_PROGRAM_JSON_BYTES = 1024 * 1024


@dataclass(frozen=True)
class FixedTrack:
    argv_template: tuple[str, ...]
    input_channel: str  # "file" or "stdin"

    def expected_argv(self) -> tuple[str, ...]:
        """The argv a fixed-track case must have (without argv[0])."""

        return tuple(
            token.replace(FIXED_INPUT_PLACEHOLDER, FIXED_INPUT_NAME)
            for token in self.argv_template
        )


@dataclass(frozen=True)
class Program:
    id: str
    name: str
    version: str
    source_url: str
    source_sha256: str
    source_directory: str
    binary: str
    branch_total: int
    fixed_track: FixedTrack | None
    inputs: tuple[str, ...]
    status: str
    raw: dict[str, Any]
    #: SHA-256 of the ``program.json`` file the definition was read from (of its
    #: canonical JSON text when it was built from a parsed document). Recorded
    #: in ``replay.json`` so that a result names the exact definition it used.
    document_sha256: str = ""

    @property
    def build(self) -> dict[str, Any]:
        build = self.raw.get("build")
        return build if isinstance(build, dict) else {}

    @property
    def ignores_exclusion_markers(self) -> bool:
        """Optional ``build.exclusion_markers``: ``"honored"`` (the default, lcov's own behaviour) or ``"ignored"``.

        With ``"ignored"`` lcov runs with ``--no-markers`` and branches inside ``LCOV_EXCL_*`` blocks of the
        source belong to the universe. The published universe of xmlwf counts such branches (6,293), that of
        Confetti does not (632), so the choice is made per program.
        """

        return self.build.get("exclusion_markers") == "ignored"

    def stage_sources(self) -> list[tuple[str, str]]:
        """Optional ``build.stage_sources``: ``[{"source": ..., "recorded_path": ...}]``.

        Some builds record generated sources at a path other than the one they
        were generated at (for example a VPATH object directory). The replay
        copies such a file to the recorded location in the build tree when it
        is missing. Most programs have none.
        """

        entries = self.build.get("stage_sources") or []
        result: list[tuple[str, str]] = []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("source"), str) and isinstance(
                entry.get("recorded_path"), str
            ):
                result.append((entry["source"], entry["recorded_path"]))
        return result


def _err(where: str, code: str, message: str) -> ValidationError:
    return ValidationError(where, code, message)


def validate_program_document(doc: Any, where: str = "program.json") -> list[ValidationError]:
    """Validate a parsed program definition; returns all problems found.

    Unknown keys are allowed (a program may carry notes or build details the
    runner does not use); every key the runner relies on is checked.
    """

    errors: list[ValidationError] = []
    if not isinstance(doc, dict):
        return [_err(where, "program.type", "program definition must be a JSON object")]
    if doc.get("schema") != SCHEMA_PROGRAM:
        errors.append(
            _err(where, "program.schema", f"schema must be {SCHEMA_PROGRAM!r}, got {doc.get('schema')!r}")
        )
    pid = doc.get("id")
    if not isinstance(pid, str) or not PROGRAM_ID_RE.match(pid):
        errors.append(_err(where, "program.id", "id must match [a-z0-9][a-z0-9_-]*"))
    for key in ("name", "version"):
        if not isinstance(doc.get(key), str) or not doc.get(key):
            errors.append(_err(where, f"program.{key}", f"{key} must be a non-empty string"))

    source = doc.get("source")
    if not isinstance(source, dict):
        errors.append(_err(where, "program.source", "source must be an object"))
    else:
        if not isinstance(source.get("url"), str) or not source.get("url"):
            errors.append(_err(where, "program.source.url", "source.url must be a non-empty string"))
        if not isinstance(source.get("sha256"), str) or not SHA256_RE.match(source["sha256"]):
            errors.append(
                _err(where, "program.source.sha256", "source.sha256 must be 64 lowercase hex digits")
            )
        directory = source.get("directory")
        if not isinstance(directory, str) or not directory:
            errors.append(
                _err(where, "program.source.directory", "source.directory must be a non-empty string")
            )

    build = doc.get("build")
    if not isinstance(build, dict):
        errors.append(_err(where, "program.build", "build must be an object"))
    else:
        configure = build.get("configure")
        if not isinstance(configure, list) or not all(isinstance(x, str) for x in configure):
            errors.append(
                _err(where, "program.build.configure", "build.configure must be a list of strings")
            )
        if "notes" in build and not isinstance(build["notes"], str):
            errors.append(_err(where, "program.build.notes", "build.notes must be a string"))
        if "exclusion_markers" in build and build["exclusion_markers"] not in ("honored", "ignored"):
            errors.append(
                _err(where, "program.build.exclusion_markers", "build.exclusion_markers must be 'honored' or 'ignored'")
            )
        stage = build.get("stage_sources")
        if stage is not None:
            if not isinstance(stage, list) or not all(
                isinstance(e, dict)
                and isinstance(e.get("source"), str)
                and isinstance(e.get("recorded_path"), str)
                for e in stage
            ):
                errors.append(
                    _err(
                        where,
                        "program.build.stage_sources",
                        "build.stage_sources must be a list of {source, recorded_path} objects",
                    )
                )

    binary = doc.get("binary")
    if not isinstance(binary, str):
        errors.append(_err(where, "program.binary", "binary must be a string"))
    else:
        try:
            encoded_binary = binary.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate: not a path anybody can write in a file name
            errors.append(_err(where, "program.binary", "binary is not valid Unicode text"))
        else:
            for code, message in path_problems(encoded_binary):
                errors.append(_err(where, f"program.binary.{code}", f"binary: {message}"))

    total = doc.get("branch_total")
    if not is_plain_int(total) or total <= 0:
        errors.append(_err(where, "program.branch_total", "branch_total must be a positive integer"))

    errors.extend(_validate_fixed_track(doc.get("fixed_track", None), where, "fixed_track" in doc))

    inputs = doc.get("inputs")
    if (
        not isinstance(inputs, list)
        or not inputs
        or len(set(map(str, inputs))) != len(inputs)
        or any(x not in PROGRAM_INPUTS for x in inputs)
    ):
        errors.append(
            _err(
                where,
                "program.inputs",
                f"inputs must be a non-empty list of distinct values from {list(PROGRAM_INPUTS)}",
            )
        )
    if not isinstance(doc.get("status"), str) or not doc.get("status"):
        errors.append(_err(where, "program.status", "status must be a non-empty string"))
    return errors


def _validate_fixed_track(value: Any, where: str, present: bool) -> list[ValidationError]:
    if not present:
        return [_err(where, "program.fixed_track", "fixed_track is required (null when there is none)")]
    if value is None:
        return []
    if not isinstance(value, dict):
        return [_err(where, "program.fixed_track", "fixed_track must be null or an object")]
    errors: list[ValidationError] = []
    template = value.get("argv_template")
    if not isinstance(template, list) or not all(isinstance(t, str) for t in template):
        errors.append(
            _err(where, "program.fixed_track.argv_template", "argv_template must be a list of strings")
        )
        template = []
    channel = value.get("input_channel")
    if channel not in INPUT_CHANNELS:
        errors.append(
            _err(
                where,
                "program.fixed_track.input_channel",
                f"input_channel must be one of {list(INPUT_CHANNELS)}",
            )
        )
    else:
        has_placeholder = any(FIXED_INPUT_PLACEHOLDER in t for t in template)
        if channel == "file" and not has_placeholder:
            errors.append(
                _err(
                    where,
                    "program.fixed_track.template",
                    f"input_channel 'file' needs {FIXED_INPUT_PLACEHOLDER!r} in argv_template",
                )
            )
        if channel == "stdin" and has_placeholder:
            errors.append(
                _err(
                    where,
                    "program.fixed_track.template",
                    f"input_channel 'stdin' must not use {FIXED_INPUT_PLACEHOLDER!r} in argv_template",
                )
            )
    return errors


def _canonical_digest(doc: dict[str, Any]) -> str:
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256_hex(text.encode("ascii"))


def program_from_document(doc: dict[str, Any], *, document_sha256: str | None = None) -> Program:
    """Build a :class:`Program` from a document that passed validation.

    ``document_sha256`` is the digest of the file the document was read from;
    without it the digest of the document's canonical JSON text is used.
    """

    errors = validate_program_document(doc)
    if errors:
        raise AgentSoftwareTestingError("invalid program definition: " + "; ".join(map(str, errors)))
    track = doc["fixed_track"]
    fixed = (
        None
        if track is None
        else FixedTrack(tuple(track["argv_template"]), track["input_channel"])
    )
    return Program(
        id=doc["id"],
        name=doc["name"],
        version=doc["version"],
        source_url=doc["source"]["url"],
        source_sha256=doc["source"]["sha256"],
        source_directory=doc["source"]["directory"],
        binary=doc["binary"],
        branch_total=doc["branch_total"],
        fixed_track=fixed,
        inputs=tuple(doc["inputs"]),
        status=doc["status"],
        raw=doc,
        document_sha256=document_sha256 or _canonical_digest(doc),
    )


def load_program(path: Path | str) -> Program:
    """Load ``program.json`` (a file) or ``<dir>/program.json``."""

    target = Path(path)
    if target.is_dir():
        target = target / "program.json"
    data = read_regular_bytes(target, max_bytes=MAX_PROGRAM_JSON_BYTES)
    return program_from_document(parse_json_bytes(data, os.fspath(target)), document_sha256=sha256_hex(data))


def checkout_programs_dir() -> Path:
    """The ``programs/`` directory of the source checkout this package was imported from."""

    return Path(__file__).resolve().parents[2] / "programs"


def _candidate_programs_dirs(explicit: Path | str | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    candidates.append(checkout_programs_dir())
    candidates.append(Path.cwd() / "programs")
    return [c for c in candidates if c.is_dir()]


def locate_programs_dir(explicit: Path | str | None = None) -> Path | None:
    """Find the directory holding ``<id>/program.json`` definitions.

    Order: the explicit argument, then ``programs`` of the source checkout this
    package was imported from (an editable install or ``PYTHONPATH=src``), and
    only then ``./programs``. The checkout comes before the current directory so
    that a directory which happens to ship its own ``programs/<id>/program.json``
    cannot replace the pinned definition; ``replay.json`` records the digest of
    the definition used.
    """

    found = _candidate_programs_dirs(explicit)
    return found[0] if found else None


def find_program(program_id: str, programs_dir: Path | str | None = None) -> Program | None:
    """Return the definition of ``program_id`` or ``None`` when not found.

    Every candidate directory (see :func:`locate_programs_dir`) is tried in
    order; the first one that has the program wins.
    """

    if not PROGRAM_ID_RE.match(program_id):
        return None
    for directory in _candidate_programs_dirs(programs_dir):
        candidate = directory / program_id / "program.json"
        if os.path.isfile(candidate):
            return load_program(candidate)
    return None
