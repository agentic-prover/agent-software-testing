# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Reading and writing test-case bundles (contract section 2).

A bundle is a directory::

    bundle/
      tool.json       metadata
      cases.jsonl     one case per line, ordered by t_ms
      blobs/<sha256>  content-addressed bytes (stdin and file bodies)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from .case import Case, parse_case
from .common import (
    MAX_BLOB_BYTES,
    MAX_CASE_LINE_BYTES,
    SCHEMA_TOOL,
    SHA256_RE,
    AgentSoftwareTestingError,
    IntegrityError,
    b64encode,
    dumps_json,
    loads_strict,
    open_regular_file,
    printable,
    read_json_file,
    read_regular_bytes,
    sha256_hex,
)

TOOL_FILE = "tool.json"
CASES_FILE = "cases.jsonl"
BLOBS_DIR = "blobs"
#: Chunk size used to skip over the rest of a line that is too long.
_SKIP_CHUNK = 1024 * 1024


class BundleError(AgentSoftwareTestingError):
    """The bundle cannot be read (as opposed to failing validation)."""


class BlankLineError(BundleError):
    """A blank line in cases.jsonl (the format has none)."""


class BlobError(BundleError, IntegrityError):
    """A blob cannot be read or no longer has the content its name promises."""


class Bundle:
    """A bundle on disk. Opening does not validate; see :mod:`agent_software_testing.validate`.

    ``verify_blobs`` makes :meth:`read_blob` hash what it reads and refuse a
    blob whose content no longer matches its name (a file changed after the
    validation); the replay turns that into a failure of the run.
    """

    def __init__(self, root: Path | str, *, verify_blobs: bool = True):
        self.root = Path(root)
        if not self.root.is_dir() or self.root.is_symlink():
            raise BundleError(f"bundle is not a directory: {printable(self.root)}")
        self.verify_blobs = verify_blobs
        self._tool: dict[str, Any] | None = None
        #: Set while reading cases.jsonl: True if the last line has no final LF.
        self.last_line_unterminated = False

    # -- metadata ---------------------------------------------------------

    @property
    def tool(self) -> dict[str, Any]:
        if self._tool is None:
            try:
                document = read_json_file(self.root / TOOL_FILE, max_bytes=1024 * 1024)
            except (OSError, AgentSoftwareTestingError) as exc:
                raise BundleError(f"cannot read {TOOL_FILE}: {printable(exc, 300)}") from exc
            if not isinstance(document, dict):
                raise BundleError(f"{TOOL_FILE} must be a JSON object")
            self._tool = document
        return self._tool

    @property
    def program_id(self) -> str:
        return str(self.tool["program"]["id"])

    @property
    def budget_s(self) -> int:
        return int(self.tool["budget"]["wall_s"])

    # -- cases ------------------------------------------------------------

    @property
    def cases_path(self) -> Path:
        return self.root / CASES_FILE

    def iter_raw_cases(self) -> Iterator[tuple[int, Any]]:
        """Yield ``(line number, parsed JSON value)`` for each line.

        Lines that cannot be used yield a :class:`BundleError` instead of a
        value so that the validator can report them and carry on: invalid JSON,
        a line that is too long, a blank line (:class:`BlankLineError`).
        After a complete pass ``last_line_unterminated`` says whether the file
        ends without a line feed.
        """

        try:
            fd = open_regular_file(self.cases_path)
        except (OSError, AgentSoftwareTestingError) as exc:
            raise BundleError(f"cannot open {CASES_FILE}: {printable(exc, 200)}") from exc
        self.last_line_unterminated = False
        # Split on '\n' only: str.splitlines also breaks at U+0085 and other
        # separators that may legally occur inside JSON strings. Lines are read
        # with a bound, so a file without line feeds cannot exhaust memory.
        limit = MAX_CASE_LINE_BYTES + 2  # room for a CR LF
        with os.fdopen(fd, "rb") as handle:
            number = 0
            while True:
                line = handle.readline(limit)
                if not line:
                    break
                number += 1
                too_long = False
                if len(line) >= limit and not line.endswith(b"\n"):
                    too_long = True
                    while True:  # skip the rest of this line without keeping it
                        rest = handle.readline(_SKIP_CHUNK)
                        if not rest or rest.endswith(b"\n"):
                            line = rest
                            break
                self.last_line_unterminated = not line.endswith(b"\n")
                stripped = line.rstrip(b"\r\n")
                if too_long or len(stripped) > MAX_CASE_LINE_BYTES:
                    yield number, BundleError(f"line longer than {MAX_CASE_LINE_BYTES} bytes")
                    continue
                if not stripped.strip():
                    yield number, BlankLineError("blank line (cases.jsonl has no blank lines)")
                    continue
                try:
                    yield number, loads_strict(stripped.decode("utf-8"))
                except (ValueError, RecursionError) as exc:  # includes UnicodeDecodeError, JSONDecodeError
                    yield number, BundleError(f"invalid JSON: {printable(exc, 200)}")

    def iter_cases(self) -> Iterator[Case]:
        """Yield parsed cases. Raises on a malformed line: validate first."""

        for number, raw in self.iter_raw_cases():
            if isinstance(raw, BundleError):
                raise BundleError(f"{CASES_FILE}:{number}: {raw}")
            case, errors = parse_case(raw, number)
            if case is None or errors:
                detail = "; ".join(str(e) for e in errors[:3])
                raise BundleError(f"{CASES_FILE}:{number} is not a valid case ({detail})")
            yield case

    # -- blobs ------------------------------------------------------------

    def blob_path(self, digest: str) -> Path:
        if not SHA256_RE.match(digest):
            raise BundleError(f"invalid blob digest {digest!r}")
        return self.root / BLOBS_DIR / digest

    def read_blob(self, digest: str) -> bytes:
        """The bytes of a blob. With ``verify_blobs`` they are hashed again."""

        try:
            data = read_regular_bytes(self.blob_path(digest), max_bytes=MAX_BLOB_BYTES)
        except (OSError, AgentSoftwareTestingError) as exc:
            raise BlobError(f"blob {digest} cannot be read: {printable(exc, 200)}") from exc
        if self.verify_blobs and sha256_hex(data) != digest:
            raise BlobError(f"blob {digest} no longer has the content its name promises")
        return data


class BundleWriter:
    """Write a bundle: add cases, then :meth:`finish`.

    Cases are collected in memory (without their blob bytes, which go to disk
    at once), sorted by ``t_ms`` (stable) at the end, numbered ``000001`` and
    up, and written to ``cases.jsonl``. The target directory must not exist or
    must be empty.
    """

    def __init__(self, root: Path | str, tool: dict[str, Any]):
        self.root = Path(root)
        if self.root.exists() and (not self.root.is_dir() or any(self.root.iterdir())):
            raise BundleError(f"refusing to write into a non-empty path: {self.root}")
        (self.root / BLOBS_DIR).mkdir(parents=True, exist_ok=True)
        self.tool = tool
        self._cases: list[dict[str, Any]] = []
        self._blob_sizes: dict[str, int] = {}
        self._finished = False

    def add_blob(self, data: bytes) -> str:
        """Store ``data`` and return its reference ``sha256:<hex>``."""

        digest = sha256_hex(data)
        target = self.root / BLOBS_DIR / digest
        if digest not in self._blob_sizes:
            if not target.exists():
                partial = target.with_name(f".{digest}.partial")
                partial.write_bytes(data)
                os.replace(partial, target)
            self._blob_sizes[digest] = len(data)
        return f"sha256:{digest}"

    def add_case(
        self,
        *,
        t_ms: int,
        argv: list[bytes] | tuple[bytes, ...] = (),
        stdin: bytes | None = None,
        stdin_tty: bool = False,
        stdin_pipe: bool = False,
        stdin_atime: int | None = None,
        stdin_mtime: int | None = None,
        files: list[dict[str, Any]] | None = None,
        env: dict[str, bytes] | None = None,
        cwd: bytes = b".",
        timeout_ms: int | None = None,
        origin: str | None = None,
    ) -> None:
        """Add one case. ``files`` entries use raw bytes: ``{"path": b"input",
        "kind": "file", "mode": "0644", "body": b"..."}`` (``body`` for file,
        fifo and tty, ``target`` bytes for symlink; a file may have ``atime`` and ``mtime`` in seconds). ``stdin_tty`` makes standard input
        a pseudo-terminal fed with ``stdin``, ``stdin_pipe`` a pipe fed with it; ``stdin_atime`` and ``stdin_mtime`` (seconds) are the
        times of the standard input file. Members that are not set are not written, so bundles that do not use them stay as they were."""

        record: dict[str, Any] = {
            "id": "",
            "t_ms": int(t_ms),
            "argv": [b64encode(a) for a in argv],
            "stdin": None if stdin is None else self.add_blob(stdin),
        }
        if stdin_tty:
            record["stdin_tty"] = True
        if stdin_pipe:
            record["stdin_pipe"] = True
        if stdin_atime is not None:
            record["stdin_atime"] = int(stdin_atime)
        if stdin_mtime is not None:
            record["stdin_mtime"] = int(stdin_mtime)
        encoded_files: list[dict[str, Any]] = []
        for entry in files or []:
            item: dict[str, Any] = {"path": b64encode(entry["path"]), "kind": entry["kind"]}
            if entry["kind"] in ("file", "fifo"):
                item["mode"] = entry.get("mode", "0644")
                item["blob"] = self.add_blob(entry["body"])
                if entry["kind"] == "file":
                    for key in ("atime", "mtime"):
                        if entry.get(key) is not None:
                            item[key] = int(entry[key])
            elif entry["kind"] == "tty":
                item["blob"] = self.add_blob(entry["body"])
            elif entry["kind"] == "dir":
                item["mode"] = entry.get("mode", "0755")
            elif entry["kind"] == "symlink":
                item["target"] = b64encode(entry["target"])
            encoded_files.append(item)
        record["files"] = encoded_files
        record["env"] = {name: b64encode(value) for name, value in (env or {}).items()}
        record["cwd"] = b64encode(cwd)
        if timeout_ms is not None:
            record["timeout_ms"] = int(timeout_ms)
        if origin is not None:
            record["origin"] = origin
        self._cases.append(record)

    def finish(self) -> Path:
        if self._finished:
            raise BundleError("bundle already finished")
        self._finished = True
        ordered = sorted(self._cases, key=lambda record: record["t_ms"])  # stable
        width = max(6, len(str(len(ordered))))
        lines = []
        for number, record in enumerate(ordered, start=1):
            record["id"] = f"{number:0{width}d}"
            lines.append(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
        (self.root / CASES_FILE).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        (self.root / TOOL_FILE).write_text(dumps_json(self.tool), encoding="utf-8")
        return self.root


def make_tool_document(
    *,
    name: str,
    program_id: str,
    source_sha256: str,
    wall_s: int,
    version: str | None = None,
    family: str = "other",
    model: str | None = None,
    config_sha256: str | None = None,
    url: str | None = None,
    track: str = "open",
    repeat: int = 1,
    cores: int | None = None,
    budget_notes: str = "",
    output_tokens: int | None = None,
    input_tokens: int | None = None,
    cpu_s: float | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Build a ``tool.json`` document (contract section 2).

    ``wall_s`` is required because the budget is a fact about the campaign that
    only the submitter knows. ``cores`` is written only when given: absent
    means not stated (SPEC.md section 5.2).
    """

    budget: dict[str, Any] = {"wall_s": wall_s}
    if cores is not None:
        budget["cores"] = cores
    budget["notes"] = budget_notes
    return {
        "schema": SCHEMA_TOOL,
        "tool": {
            "name": name,
            "version": version,
            "family": family,
            "model": model,
            "config_sha256": config_sha256,
            "url": url,
        },
        "program": {"id": program_id, "source_sha256": source_sha256},
        "track": track,
        "repeat": repeat,
        "budget": budget,
        "resources": {"output_tokens": output_tokens, "input_tokens": input_tokens, "cpu_s": cpu_s},
        "notes": notes,
    }
