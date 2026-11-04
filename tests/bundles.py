# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Write bundles by hand so tests can make them valid or broken on purpose."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any


def b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(data).decode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ref(data: bytes) -> str:
    return "sha256:" + sha(data)


SOURCE_SHA = {
    "demo": "1" * 64,
    "fixfile": "2" * 64,
    "fixstdin": "3" * 64,
    "nofixed": "4" * 64,
}


def tool_doc(program: str = "demo", *, track: str = "open", wall_s: int = 3600, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": "agent-software-testing/tool@0.1",
        "tool": {
            "name": "Test tool",
            "version": "1.0",
            "family": "fuzzer",
            "model": None,
            "config_sha256": None,
            "url": None,
        },
        "program": {"id": program, "source_sha256": SOURCE_SHA[program]},
        "track": track,
        "repeat": 1,
        "budget": {"wall_s": wall_s, "cores": 1, "notes": ""},
        "resources": {"output_tokens": None, "input_tokens": None, "cpu_s": None},
        "notes": "",
    }
    doc.update(over)
    return doc


def case(index: int = 1, t_ms: int = 0, **over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": f"{index:06d}",
        "t_ms": t_ms,
        "argv": [],
        "stdin": None,
        "files": [],
        "env": {},
        "cwd": b64("."),
        "timeout_ms": 5000,
        "origin": "test",
    }
    record.update(over)
    return record


def file_entry(path: str, body: bytes, *, mode: str = "0644", kind: str = "file") -> dict[str, Any]:
    return {"path": b64(path), "kind": kind, "mode": mode, "blob": ref(body)}


def write_bundle(
    root: Path,
    cases: list[dict[str, Any]],
    blobs: list[bytes] | None = None,
    tool: dict[str, Any] | None = None,
    *,
    raw_lines: list[str] | None = None,
) -> Path:
    """Write ``cases`` as given (no normalisation). ``blobs`` are stored under their hash."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "blobs").mkdir(exist_ok=True)
    (root / "tool.json").write_text(json.dumps(tool if tool is not None else tool_doc()))
    lines = raw_lines if raw_lines is not None else [json.dumps(c) for c in cases]
    (root / "cases.jsonl").write_text("".join(line + "\n" for line in lines))
    for body in blobs or []:
        (root / "blobs" / sha(body)).write_bytes(body)
    return root


def mutate(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(record)
    out.update(changes)
    return out
