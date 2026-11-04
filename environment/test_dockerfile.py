"""Tests for environment/Dockerfile and its lock file (text only: no image is built here)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ENVIRONMENT = Path(__file__).resolve().parent
DOCKERFILE = (ENVIRONMENT / "Dockerfile").read_text()
LOCK = json.loads((ENVIRONMENT / "toolchain.lock.json").read_text())


def code_lines() -> list[str]:
    return [line for line in DOCKERFILE.splitlines() if not line.lstrip().startswith("#")]


def test_tls_verification_is_never_switched_off() -> None:
    code = "\n".join(code_lines())
    assert "Verify-Peer" not in code and "Verify-Host" not in code
    assert "--no-check-certificate" not in code and "curl -k" not in code and "--insecure" not in code
    assert "APT::Get::AllowUnauthenticated" not in code and "--allow-unauthenticated" not in code
    assert "AllowInsecureRepositories" not in code


def test_the_snapshot_is_reached_over_https_only() -> None:
    sources = re.findall(r"snapshot\.ubuntu\.com", DOCKERFILE)
    assert sources
    assert "http://snapshot.ubuntu.com" not in DOCKERFILE


def test_the_base_image_is_pinned_by_its_recorded_digest() -> None:
    match = re.search(r"^ARG BASE_IMAGE=(\S+)$", DOCKERFILE, re.MULTILINE)
    assert match, "BASE_IMAGE must have a default"
    assert re.fullmatch(r"ubuntu:jammy-\d+@sha256:[0-9a-f]{64}", match.group(1))
    assert set(match.group(1).split("sha256:")[1]) != {"0"}, "the digest must be the recorded one, not a placeholder"
    assert match.group(1).endswith(LOCK["base_image"]["digest"])


def test_the_image_carries_tzdata() -> None:
    # gnulib's working-mktime probe needs the zoneinfo database to see the C library's mktime defect; without it
    # lib/mktime.c is not linked into diff, du and ls and their universes lose 88 branches (README, "Image").
    packages = DOCKERFILE.split("build-essential", 1)[1].split("rm -rf /var/lib/apt/lists", 1)[0]
    assert "tzdata" in re.findall(r"[\w.+-]+", packages)
