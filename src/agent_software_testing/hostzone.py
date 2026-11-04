# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""The time zone a program sees when its environment names none (SPEC.md sections 6.3 step 3, 7.1 and 11.17).

A case whose environment has no ``TZ`` runs in the zone of the machine or container: the C library reads
``/etc/localtime``. The ``canonical-2026`` profile leaves ``TZ`` out of a recorded environment (the earlier pipeline's
programs had none), so there the zone is whatever the replay machine holds, and the profile assumes UTC, the zone of
the reference image. Under Singularity or Apptainer the host's ``/etc/localtime`` is bound into the container by
default: a node on Asia/Baku time (UTC+04) then gives every such case ``+04``, and ``localtime``, ``strftime`` and the
programs that print a time cover other branches.

The runner therefore records the zone in ``replay.json`` (``local_zone``) and, under ``canonical-2026``, warns when it
is not UTC. The zone is measured the way a case sees it, by a child process whose environment has no ``TZ``: the
runner's own environment often has ``TZ=UTC`` (a launcher sets it), which says nothing about the cases.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Any, Mapping

from .common import printable

#: Where Linux and macOS keep the zone of the machine. A link into a ``zoneinfo`` directory names it.
LOCALTIME_PATH = "/etc/localtime"
#: 1 January and 1 July 2026, 00:00 UTC. A zone with daylight saving time, or one that changed its offset during the
#: year, differs from UTC at one of them even when it is at offset 0 on the day of the replay (Europe/London in winter).
REFERENCE_INSTANTS = (1767225600, 1782864000)
#: What the child prints: the offset (seconds east of UTC) and the abbreviation now, and the offsets at the reference instants.
_PROBE = (
    "import json, time; n = time.localtime(); "
    f"print(json.dumps([n.tm_gmtoff, n.tm_zone] + [time.localtime(t).tm_gmtoff for t in {list(REFERENCE_INSTANTS)}]))"
)
_UTC_NAME = "UTC"


def zone_name_from_link(target: str) -> str:
    """The zone a link target names, without the machine's path: ``/usr/share/zoneinfo/Asia/Baku`` gives ``Asia/Baku``.

    A target outside a ``zoneinfo`` directory gives its last component, never an absolute path (SPEC.md section 7.1:
    no host-specific paths in the outputs).
    """

    marker = "zoneinfo/"
    if marker in target:
        return target.rsplit(marker, 1)[1] or target.rstrip("/").rsplit("/", 1)[-1]
    return target.rstrip("/").rsplit("/", 1)[-1]


def is_utc(offset_s: int | None, abbreviation: str | None, reference_offsets_s: list[int] | tuple[int, ...]) -> bool | None:
    """Whether a process in this zone is in UTC: offset 0 now and at both reference instants, and the abbreviation ``UTC``.

    ``None`` when the zone could not be read. A zone at offset 0 that is named ``GMT`` is not UTC here: a program that
    prints the abbreviation covers other branches, and the profile assumes the image's zone.
    """

    if offset_s is None or abbreviation is None:
        return None
    return offset_s == 0 and all(value == 0 for value in reference_offsets_s) and abbreviation == _UTC_NAME


def probe_local_zone(
    environ: Mapping[str, str] | None = None,
    *,
    python: str | None = None,
    timeout_s: float = 30.0,
    localtime_path: str = LOCALTIME_PATH,
) -> dict[str, Any]:
    """The zone of a process that has no ``TZ``, as ``replay.json`` records it (``local_zone``).

    Members: ``offset_s`` (seconds east of UTC at the time of the call), ``abbreviation`` (what ``%Z`` prints),
    ``utc`` (:func:`is_utc`; ``None`` if the probe failed), ``localtime_link`` (the zone ``/etc/localtime`` links to,
    or ``None`` if it is a regular file, missing, or unreadable) and ``tz_env`` (the ``TZ`` of the runner's own
    environment, ``None`` if unset). ``localtime_sha256`` identifies the file ``/etc/localtime`` leads to (links followed)
    by its content, ``None`` if it cannot be read: a container that binds a zone file over the target of the link shows
    nothing in the link's name, but the content tells. ``error`` is added when the child process could not be run.
    """

    source = dict(os.environ if environ is None else environ)
    own_tz = source.pop("TZ", None)
    zone: dict[str, Any] = {
        "offset_s": None,
        "abbreviation": None,
        "utc": None,
        "localtime_link": None,
        "tz_env": None if own_tz is None else printable(own_tz, 80),
        "localtime_sha256": None,
    }
    try:
        zone["localtime_link"] = zone_name_from_link(os.readlink(localtime_path))
    except OSError:
        pass  # a regular file (a bound zone file), missing, or not readable
    try:
        with open(localtime_path, "rb") as handle:
            zone["localtime_sha256"] = hashlib.sha256(handle.read(1 << 20)).hexdigest()
    except OSError:
        pass
    executable = python or sys.executable
    try:
        if not executable:
            raise OSError("no Python interpreter to ask")
        done = subprocess.run(
            [executable, "-I", "-c", _PROBE], env=source, capture_output=True, text=True, timeout=timeout_s, check=False
        )
        if done.returncode != 0:
            raise OSError(printable(done.stderr.strip() or f"exit status {done.returncode}", 200))
        offset, abbreviation, *reference = json.loads(done.stdout)
        if not isinstance(offset, int) or not isinstance(abbreviation, str) or not all(isinstance(v, int) for v in reference):
            raise ValueError("unexpected answer")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        zone["error"] = printable(str(exc), 200)
        return zone
    zone["offset_s"] = offset
    zone["abbreviation"] = printable(abbreviation, 40)
    zone["utc"] = is_utc(offset, abbreviation, reference)
    return zone


def describe_zone(zone: Mapping[str, Any]) -> str:
    """For a warning: the abbreviation and the offset."""

    if zone.get("offset_s") is None:
        return "unknown (" + str(zone.get("error", "not measured")) + ")"
    offset = int(zone["offset_s"])
    sign = "+" if offset >= 0 else "-"
    hours, rest = divmod(abs(offset), 3600)
    return f"{zone.get('abbreviation')} (UTC{sign}{hours:02d}:{rest // 60:02d})"


def canonical_zone_warning(zone: Mapping[str, Any]) -> str | None:
    """The warning ``canonical-2026`` gives when a case without ``TZ`` would not run in UTC, else ``None``."""

    if zone.get("utc") is True:
        return None
    if zone.get("utc") is None:
        return (
            "canonical-2026: the time zone of a case without TZ could not be determined "
            f"({describe_zone(zone)}); the profile assumes UTC"
        )
    return (
        "canonical-2026 assumes that a case without TZ runs in UTC, the zone of the reference image, but a process "
        f"without TZ here is in {describe_zone(zone)}. Cases of a recorded environment (no TZ) will see this zone, and "
        "programs that print or compute a local time cover other branches. Bind a UTC zone file over "
        f"{LOCALTIME_PATH} (Singularity or Apptainer: --bind <UTC zone file>:{LOCALTIME_PATH}) or run on a machine in UTC"
    )
