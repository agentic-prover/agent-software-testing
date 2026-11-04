#!/usr/bin/env python3
"""Download, verify and build the pinned Agent Software Testing programs.

For every program this script produces

* one coverage build (gcc, ``-O0``, ``-fprofile-arcs -ftest-coverage``) below
  ``<out>/<id>/gcov``, and
* one ASan+UBSan build (clang, ``-fsanitize=address,undefined``) below
  ``<out>/<id>/asan-ubsan``,

plus a build record and a log per variant below ``<out>/<id>/meta``. The commands are
those of the build driver that produced the reference branch totals, including its
per-program quirks, with the symbolic-execution (bitcode) half removed.

Standard library only; Python 3.10 or later.

STATUS: exercised on synthetic projects (environment/test_build_programs.py) and on the real
sources of Confetti, Krep and libmatheval in the image of environment/Dockerfile. It has not been
run for the other 15 programs; see environment/README.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

REPOSITORY = Path(__file__).resolve().parent.parent
DEFAULT_PROGRAMS = REPOSITORY / "programs"
# Third-party build inputs (two Krep patches, the libmatheval driver), kept apart from our own code
# with their licences; used when --assets is not given and the directory exists.
DEFAULT_ASSETS = Path(__file__).resolve().parent / "third_party" / "concollmic"
DEFAULT_OUT = Path(os.environ.get("AGENT_SOFTWARE_TESTING_BUILDS", "/opt/agent-software-testing/builds"))

COVERAGE_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage"
COVERAGE_LDFLAGS = "-fprofile-arcs -ftest-coverage"
SANITIZER_COMPILE = (
    "-g",
    "-O1",
    "-fno-omit-frame-pointer",
    "-fno-optimize-sibling-calls",
    "-fsanitize=address,undefined",
    "-fno-sanitize-recover=all",
)
SANITIZER_LINK = ("-fsanitize=address,undefined", "-fno-sanitize-recover=all")

PROGRAM_SCHEMA = "agent-software-testing/program@0.1"
BUILD_SCHEMA = "agent-software-testing/build@0.1"

# Directory names inside <out>/<program id>/.
VARIANTS = {
    "coverage": {"dirname": "gcov", "objname": "obj-gcov"},
    "sanitizer": {"dirname": "asan-ubsan", "objname": "obj-native"},
}


# ---------------------------------------------------------------------------
# Pinned inputs that are not the program's main source archive
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FileSpec:
    """A small input file with a pinned digest.

    origin "repo": found below the programs directory of this repository.
    origin "asset": a third-party build input, found as ``<path>`` below the assets directory
    (``--assets``, by default ``third_party/concollmic`` next to this script) and refused
    unless its digest matches.
    """

    origin: str
    path: str
    sha256: str
    purpose: str


FILES: dict[str, FileSpec] = {
    "jq/compat/jq-single-process.c": FileSpec(
        "repo",
        "jq/compat/jq-single-process.c",
        "104fd7a168fdc3ae57b7d1f53254f7ff9961fec27e83e24342eb6c15ae4481d5",
        "single-process pthread subset linked into jq",
    ),
    "krep/Makefile.diff": FileSpec(
        "asset",
        "krep/Makefile.diff",
        "381cd3f517a7a802cbb0c1433b763a7369a833cb36212d26d79eaa60dfa2dea4",
        "krep make-only build patch",
    ),
    "krep/krep.diff": FileSpec(
        "asset",
        "krep/krep.diff",
        "e9d7296e6d4618eb95dd1c51e89d9ec07b085bd989e0a48c70b8d0852eb07b59",
        "krep source patch (no host vectors, no thread pool)",
    ),
    "libmatheval/test.c": FileSpec(
        "asset",
        "libmatheval/test.c",
        "3b1236126e160287cbafb30a7598bac8e8462d3d3a442549c80cf74c32443e55",
        "libmatheval standard-input driver",
    ),
}


@dataclasses.dataclass(frozen=True)
class Archive:
    url: str
    sha256: str


DEPENDENCIES: dict[str, Archive] = {
    "brotli": Archive(
        "https://github.com/google/brotli/archive/533843e3546cd24c8344eaa899c6b0b681c8d222.tar.gz",
        "60209cc58dfe13d260199b149c3585ddc89fd02f5ad4d3b47cebfef51a1c7ec7",
    ),
    "ogg": Archive(
        "https://gitlab.xiph.org/xiph/ogg/-/archive/06a5e0262cdc28aa4ae6797627a783b5010440f0/"
        "ogg-06a5e0262cdc28aa4ae6797627a783b5010440f0.tar.gz",
        "fd7ab06f9fe9ace6b75d4223cf9034c9648cbe9f16120a344ec82f27c605ca11",
    ),
    "vorbis": Archive(
        "https://gitlab.xiph.org/xiph/vorbis/-/archive/e3c9861ff096d52378e131ff8c334552e09cdffa/"
        "vorbis-e3c9861ff096d52378e131ff8c334552e09cdffa.tar.gz",
        "d48f3b15696d8a3bfc3803e8f1fc6a2f94c7ba5a48bff3c7854bfb8b9c56c89c",
    ),
}


# ---------------------------------------------------------------------------
# Per-program build recipes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Recipe:
    """What program.json does not say.

    style
        autotools  extract, configure out of tree in obj-*/, make
        make       as autotools without a configure step (krep)
        driver     autotools, then compile and link a driver by hand (libmatheval)
        tree       source extracted into obj-*/ and built in place with make (woff2)
        oggenc     in-tree build driven by autogen.sh; sanitiser build builds the
                   pinned libogg and libvorbis first
    """

    style: str = "autotools"
    env: tuple[tuple[str, str], ...] = ()
    make_variables: tuple[str, ...] = ()
    make_targets: tuple[str, ...] = ()
    max_jobs: int | None = None
    compat: tuple[str, ...] = ()  # keys of FILES compiled into objects and linked via LIBS
    patches: tuple[str, ...] = ()  # keys of FILES
    driver: str | None = None  # key of FILES
    deps: tuple[str, ...] = ()  # keys of DEPENDENCIES
    autoreconf_fallback: bool = False  # run autoreconf -fvi when the archive ships no configure
    # Options of build.configure that the program's configure script warns about ("unrecognized options") and
    # ignores; they are passed all the same (the reference build did) and tolerated. Any other unrecognised
    # option stops the build.
    ignored_configure_options: tuple[str, ...] = ()

    @property
    def has_configure(self) -> bool:
        return self.style in ("autotools", "driver", "oggenc")


_COREUTILS = Recipe()

RECIPES: dict[str, Recipe] = {
    "bc": Recipe(make_variables=("MAKEINFO=true",), make_targets=("lib", "bc"), max_jobs=1),
    "confetti": Recipe(autoreconf_fallback=True),
    "diff": Recipe(),
    "du": _COREUTILS,
    "gawk": Recipe(),
    "gcal": Recipe(
        env=(
            ("CHECK_CFLAGS", "-DAGENT_SOFTWARE_TESTING_GCAL_NO_TEST_SUITE=1"),
            ("CHECK_LIBS", "-lm"),
        ),
        make_targets=("lib", "src"),
    ),
    "grep": Recipe(),
    # gl_cv_crc_pclmul: portable CRC. gl_cv_func_printf_enomem: gnulib's "printf survives out-of-memory conditions" probe
    # sets an address-space limit and calls printf with a huge precision. Natively it answers "yes" for glibc; under
    # Rosetta (an amd64 container on Apple silicon) the emulator aborts the probe ("rosetta error: mmap_anonymous_rw
    # mmap failed"), configure then answers "no" and compiles the large floating-point code of lib/vasnprintf.c into
    # gzip: 926 more branches than the published universe (3,083). The preset makes the build independent of the host.
    "gzip": Recipe(env=(("gl_cv_crc_pclmul", "no"), ("gl_cv_func_printf_enomem", "yes"))),
    "jq": Recipe(compat=("jq/compat/jq-single-process.c",)),
    "krep": Recipe(
        style="make",
        make_variables=("-f", "../Makefile", "VPATH=.."),
        patches=("krep/Makefile.diff", "krep/krep.diff"),
    ),
    # libmatheval's configure script does not know --disable-nls (it has no gettext support to switch off) and
    # warns about it; the option has no effect, as the universe of the build shows (591 branches).
    "libmatheval": Recipe(
        style="driver",
        make_targets=("lib",),
        driver="libmatheval/test.c",
        ignored_configure_options=("--disable-nls",),
    ),
    "libyaml": Recipe(),
    "ls": _COREUTILS,
    "oggenc": Recipe(style="oggenc", deps=("ogg", "vorbis")),
    "sed": Recipe(),
    "woff2": Recipe(style="tree", deps=("brotli",)),
    "xmlwf": Recipe(),
    "xorriso": Recipe(),
}


# ---------------------------------------------------------------------------
# Program definitions
# ---------------------------------------------------------------------------

_DEFINITION_FIELDS: dict[str, tuple[type, ...]] = {
    "schema": (str,),
    "id": (str,),
    "name": (str,),
    "version": (str,),
    "source": (dict,),
    "build": (dict,),
    "binary": (str,),
    "branch_total": (int,),
    "fixed_track": (dict, type(None)),
    "inputs": (list,),
    "status": (str,),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_definition(doc: Any, *, directory: str | None = None) -> list[str]:
    """Return a list of problems with a program.json document (empty when valid)."""

    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["document is not an object"]
    missing = sorted(set(_DEFINITION_FIELDS) - set(doc))
    extra = sorted(set(doc) - set(_DEFINITION_FIELDS))
    if missing:
        errors.append(f"missing fields: {missing}")
    if extra:
        errors.append(f"unknown fields: {extra}")
    for name, types in _DEFINITION_FIELDS.items():
        if name in doc and (not isinstance(doc[name], types) or isinstance(doc[name], bool)):
            errors.append(f"{name}: wrong type {type(doc[name]).__name__}")
    if errors:
        return errors
    if doc["schema"] != PROGRAM_SCHEMA:
        errors.append(f"schema must be {PROGRAM_SCHEMA!r}")
    if directory is not None and doc["id"] != directory:
        errors.append(f"id {doc['id']!r} differs from directory {directory!r}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", doc["id"]):
        errors.append("id must match [a-z0-9][a-z0-9_-]*")
    source = doc["source"]
    if sorted(source) != ["directory", "sha256", "url"]:
        errors.append(f"source keys must be directory, sha256, url; got {sorted(source)}")
    else:
        if not all(isinstance(source[k], str) and source[k] for k in source):
            errors.append("source values must be non-empty strings")
        elif not _SHA256.match(source["sha256"]):
            errors.append("source.sha256 must be 64 lowercase hex digits")
        elif not source["url"].startswith("https://"):
            errors.append("source.url must be https")
    build = doc["build"]
    if not {"configure", "notes"} <= set(build) or not set(build) <= {"configure", "notes", "stage_sources", "exclusion_markers"}:
        errors.append(
            f"build keys must be configure, notes and optionally stage_sources and exclusion_markers; got {sorted(build)}"
        )
    elif build.get("exclusion_markers", "honored") not in ("honored", "ignored"):
        errors.append("build.exclusion_markers must be 'honored' or 'ignored'")
    else:
        if not (isinstance(build["configure"], list) and all(isinstance(a, str) for a in build["configure"])):
            errors.append("build.configure must be a list of strings")
        if not isinstance(build["notes"], str) or not build["notes"]:
            errors.append("build.notes must be a non-empty string")
        stage = build.get("stage_sources", [])
        if not isinstance(stage, list):
            errors.append("build.stage_sources must be a list")
        else:
            for entry in stage:
                if not (
                    isinstance(entry, dict)
                    and sorted(entry) == ["recorded_path", "source"]
                    and all(isinstance(entry[k], str) and entry[k] for k in entry)
                ):
                    errors.append("build.stage_sources entries need exactly source and recorded_path")
                    continue
                for key in ("source", "recorded_path"):
                    path = Path(entry[key])
                    if path.is_absolute() or ".." in path.parts:
                        errors.append(f"build.stage_sources {key} must be a relative path without ..")
    if doc["binary"].startswith("/") or ".." in Path(doc["binary"]).parts or not doc["binary"].startswith("obj-gcov/"):
        errors.append("binary must be a relative path below obj-gcov/")
    if doc["branch_total"] <= 0:
        errors.append("branch_total must be positive")
    track = doc["fixed_track"]
    if track is not None:
        if sorted(track) != ["argv_template", "input_channel"]:
            errors.append("fixed_track keys must be argv_template, input_channel")
        else:
            template, channel = track["argv_template"], track["input_channel"]
            if not (isinstance(template, list) and all(isinstance(a, str) for a in template)):
                errors.append("fixed_track.argv_template must be a list of strings")
            elif channel not in ("file", "stdin"):
                errors.append("fixed_track.input_channel must be file or stdin")
            else:
                holders = sum(a == "{input}" for a in template)
                if any("{input}" in a and a != "{input}" for a in template):
                    errors.append("{input} must be a whole argv element")
                if channel == "file" and holders != 1:
                    errors.append("a file-channel template needs exactly one {input}")
                if channel == "stdin" and holders != 0:
                    errors.append("a stdin-channel template must not contain {input}")
    if not doc["inputs"] or not set(doc["inputs"]) <= {"argv", "files", "stdin", "env"}:
        errors.append("inputs must be a non-empty subset of argv, files, stdin, env")
    expected_status = "measured" if track is not None else "open-track-only"
    if doc["status"] != expected_status:
        errors.append(f"status must be {expected_status!r} for this fixed_track")
    return errors


def load_definition(programs_dir: Path, program_id: str) -> dict[str, Any]:
    path = programs_dir / program_id / "program.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    problems = validate_definition(doc, directory=program_id)
    if problems:
        raise SystemExit(f"{path}: " + "; ".join(problems))
    return doc


def program_ids(programs_dir: Path) -> list[str]:
    return sorted(p.parent.name for p in programs_dir.glob("*/program.json"))


def verify_definitions(programs_dir: Path) -> list[str]:
    """Check every program.json and its agreement with RECIPES; return problems."""

    problems: list[str] = []
    found = program_ids(programs_dir)
    for pid in found:
        path = programs_dir / pid / "program.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{pid}: invalid JSON: {exc}")
            continue
        problems.extend(f"{pid}: {p}" for p in validate_definition(doc, directory=pid))
        recipe = RECIPES.get(pid)
        if recipe is None:
            problems.append(f"{pid}: no build recipe")
            continue
        if isinstance(doc, dict) and isinstance(doc.get("build"), dict):
            configure = doc["build"].get("configure")
            if not recipe.has_configure and configure:
                problems.append(f"{pid}: recipe style {recipe.style!r} has no configure step")
        if isinstance(doc, dict) and isinstance(doc.get("build"), dict):
            passed = doc["build"].get("configure") or []
            for option in recipe.ignored_configure_options:
                if option not in passed:
                    problems.append(f"{pid}: recipe tolerates {option!r} but build.configure does not pass it")
        for key in (*recipe.compat, *recipe.patches, *([recipe.driver] if recipe.driver else [])):
            if key not in FILES:
                problems.append(f"{pid}: unknown file key {key!r}")
        for key in recipe.deps:
            if key not in DEPENDENCIES:
                problems.append(f"{pid}: unknown dependency key {key!r}")
    for pid in sorted(set(RECIPES) - set(found)):
        problems.append(f"{pid}: recipe without program.json")
    for key, spec in FILES.items():
        if spec.origin == "repo":
            file = programs_dir / spec.path
            if not file.is_file():
                problems.append(f"{key}: missing repository file {file}")
            elif sha256_file(file) != spec.sha256:
                problems.append(f"{key}: digest differs from the pinned value")
    return problems


# ---------------------------------------------------------------------------
# Fetching and verification
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_name(url: str, sha256: str) -> str:
    base = Path(urllib.parse.urlparse(url).path).name or "archive"
    return f"{sha256[:16]}-{base}"


def fetch(url: str, sha256: str, cache: Path, *, offline: bool, allow_file_urls: bool = False) -> Path:
    """Return the cached archive for url, downloading it first unless offline.

    A cached file whose digest differs is an error; it is never replaced.
    """

    scheme = urllib.parse.urlparse(url).scheme
    if scheme != "https" and not (scheme == "file" and allow_file_urls):
        raise SystemExit(f"refusing non-https source URL: {url}")
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / cache_name(url, sha256)
    if not target.exists():
        if offline:
            raise SystemExit(f"offline and not cached: {target.name} ({url})")
        partial = target.with_name(f".{target.name}.partial-{os.getpid()}")
        try:
            try:
                with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:  # noqa: S310
                    shutil.copyfileobj(response, out, 1 << 20)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                raise SystemExit(f"cannot download {url}: {error}") from error
            if sha256_file(partial) != sha256:
                raise SystemExit(f"SHA-256 mismatch for {url}")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
    if sha256_file(target) != sha256:
        raise SystemExit(f"cached archive has the wrong SHA-256: {target}")
    return target


def top_level_names(archive: Path) -> set[str]:
    names: set[str] = set()
    with tarfile.open(archive) as handle:
        for member in handle:
            parts = [p for p in member.name.split("/") if p not in ("", ".")]
            if parts:
                names.add(parts[0])
    return names


def resolve_file(key: str, programs_dir: Path, assets_dir: Path | None, *, required: bool) -> Path:
    spec = FILES[key]
    base = programs_dir if spec.origin == "repo" else assets_dir
    if base is None:
        if required:
            raise SystemExit(
                f"{key} ({spec.purpose}) is a third-party build input; it is expected in "
                "environment/third_party/concollmic or in the directory given with --assets DIR "
                "(see environment/README.md)"
            )
        return Path("<assets>") / spec.path
    path = base / spec.path
    if not path.is_file():
        if required:
            raise SystemExit(f"missing {spec.origin} file for {key}: {path}")
        return path
    if required and sha256_file(path) != spec.sha256:
        raise SystemExit(f"{key}: SHA-256 differs from the pinned value ({spec.sha256}): {path}")
    return path


# ---------------------------------------------------------------------------
# Build plans
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Step:
    tag: str
    argv: tuple[str, ...]
    cwd: Path
    env: tuple[tuple[str, str], ...] = ()

    def record(self, root: Path) -> dict[str, Any]:
        try:
            cwd = self.cwd.relative_to(root).as_posix()
        except ValueError:
            cwd = str(self.cwd)
        return {"tag": self.tag, "argv": list(self.argv), "cwd": cwd, "env": dict(self.env)}


@dataclasses.dataclass(frozen=True)
class Context:
    out: Path
    jobs: int
    cc: str = "gcc"
    cxx: str = "g++"
    sanitizer_cc: str = "clang-15"
    sanitizer_cxx: str = "clang++-15"


def _make_commands(recipe: Recipe, jobs: int, extra: Sequence[str] = ()) -> list[list[str]]:
    count = jobs if recipe.max_jobs is None else min(jobs, recipe.max_jobs)
    prefix = ["make", f"-j{count}", *recipe.make_variables, *extra]
    if not recipe.make_targets:
        return [prefix]
    return [[*prefix, "-C", target] for target in recipe.make_targets]


def plan_build(
    doc: Mapping[str, Any],
    recipe: Recipe,
    variant: str,
    ctx: Context,
    *,
    archive: Path,
    dependencies: Mapping[str, Path],
    files: Mapping[str, Path],
) -> tuple[Path, Path, list[Step]]:
    """Return (build root, source root, steps) for one program and variant."""

    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    coverage = variant == "coverage"
    root = ctx.out / doc["id"] / VARIANTS[variant]["dirname"]
    obj = root / VARIANTS[variant]["objname"]
    configure_args = list(doc["build"]["configure"])
    compile_flags = " ".join(SANITIZER_COMPILE)
    link_flags = " ".join(SANITIZER_LINK)
    steps: list[Step] = []

    def step(tag: str, argv: Sequence[str], cwd: Path, env: Mapping[str, str] | None = None) -> None:
        steps.append(Step(tag, tuple(argv), cwd, tuple(sorted((env or {}).items()))))

    def tool_env() -> dict[str, str]:
        if coverage:
            env = {
                "CC": ctx.cc,
                "CFLAGS": COVERAGE_CFLAGS,
                "LDFLAGS": COVERAGE_LDFLAGS,
                "FORCE_UNSAFE_CONFIGURE": "1",
            }
            env.update(dict(recipe.env))
            return env
        env = dict(recipe.env)
        env.update(
            {
                "CC": ctx.sanitizer_cc,
                "CXX": ctx.sanitizer_cxx,
                "CFLAGS": compile_flags,
                "CXXFLAGS": compile_flags,
                "LDFLAGS": link_flags,
                "FORCE_UNSAFE_CONFIGURE": "1",
            }
        )
        return env

    def extract(tag: str, archive_path: Path, destination: Path) -> None:
        step(f"mkdir-{tag}", ["mkdir", "-p", str(destination)], root)
        step(f"extract-{tag}", ["tar", "-xf", str(archive_path), "--strip-components=1", "-C", str(destination)], root)

    style = recipe.style

    # ---- in-tree styles ---------------------------------------------------
    if style == "tree":
        extract("source", archive, obj)
        for key in recipe.deps:
            extract(key, dependencies[key], obj / key)
        if coverage:
            env = {
                "CC": ctx.cc,
                "CXX": ctx.cxx,
                "CFLAGS": COVERAGE_CFLAGS,
                "CXXFLAGS": COVERAGE_CFLAGS,
                "LFLAGS": COVERAGE_LDFLAGS,
                "LDFLAGS": COVERAGE_LDFLAGS,
            }
            step("make", ["make", f"-j{ctx.jobs}", "all"], obj, env)
        else:
            variables = [
                f"CC={ctx.sanitizer_cc}",
                f"CXX={ctx.sanitizer_cxx}",
                f"CFLAGS={compile_flags}",
                f"CXXFLAGS={compile_flags}",
                f"LFLAGS={link_flags}",
                f"LDFLAGS={link_flags}",
            ]
            step("make", ["make", f"-j{ctx.jobs}", *variables, "all"], obj)
        return root, obj, steps

    if style == "oggenc":
        extract("source", archive, obj)
        if coverage:
            step("autogen", ["./autogen.sh"], obj)
            env = {
                "CC": ctx.cc,
                "CXX": ctx.cxx,
                "CFLAGS": COVERAGE_CFLAGS,
                "CXXFLAGS": COVERAGE_CFLAGS,
                "LDFLAGS": COVERAGE_LDFLAGS,
            }
            step("configure", ["./configure", *configure_args], obj, env)
            step("make", ["make", f"-j{ctx.jobs}"], obj, env)
            return root, obj, steps
        deps = obj / "dependencies"
        prefix = deps / "vorbis-install"
        step("mkdir-prefix", ["mkdir", "-p", str(prefix)], root)
        env = {
            "CC": ctx.sanitizer_cc,
            "CXX": ctx.sanitizer_cxx,
            "CFLAGS": compile_flags,
            "CXXFLAGS": compile_flags,
            "LDFLAGS": link_flags,
        }
        extract("ogg", dependencies["ogg"], deps / "ogg")
        step("autoreconf-ogg", ["autoreconf", "-fvi"], deps / "ogg")
        step("configure-ogg", ["./configure", f"--prefix={prefix}", "--disable-shared", "--enable-static"], deps / "ogg", env)
        step("make-ogg", ["make", f"-j{ctx.jobs}"], deps / "ogg", env)
        step("install-ogg", ["make", "install"], deps / "ogg", env)
        env_vorbis = {
            **env,
            "PKG_CONFIG_PATH": f"{prefix}/lib/pkgconfig",
            "CPPFLAGS": f"-I{prefix}/include",
            "LDFLAGS": f"{link_flags} -L{prefix}/lib",
        }
        extract("vorbis", dependencies["vorbis"], deps / "vorbis")
        step("autoreconf-vorbis", ["autoreconf", "-fvi"], deps / "vorbis", env_vorbis)
        step("configure-vorbis", ["./configure", f"--prefix={prefix}", "--disable-shared", "--enable-static"], deps / "vorbis", env_vorbis)
        step("make-vorbis", ["make", f"-j{ctx.jobs}"], deps / "vorbis", env_vorbis)
        step("install-vorbis", ["make", "install"], deps / "vorbis", env_vorbis)
        step("autogen", ["./autogen.sh"], obj, env_vorbis)
        step("configure", ["./configure", *configure_args], obj, env_vorbis)
        step("make", ["make", f"-j{ctx.jobs}"], obj, env_vorbis)
        return root, obj, steps

    # ---- out-of-tree styles ----------------------------------------------
    extract("source", archive, root)
    step("mkdir-obj", ["mkdir", "-p", str(obj)], root)
    for key in recipe.patches:
        step(f"patch-{Path(key).name}", ["patch", "--batch", "--forward", "-p1", "--directory=..", f"--input={files[key]}"], obj)

    objects: list[Path] = []
    for index, key in enumerate(recipe.compat):
        target = obj / f"agent-software-testing-compat-{index}.o"
        objects.append(target)
        if coverage:
            argv = [ctx.cc, "-g", "-O0", "-fPIC", "-c", str(files[key]), "-o", str(target)]
        else:
            argv = [ctx.sanitizer_cc, *SANITIZER_COMPILE, "-I", str(root), "-fPIC", "-c", str(files[key]), "-o", str(target)]
        step(f"compat-{index}", argv, obj)

    env = tool_env()
    if coverage and objects:
        env["LIBS"] = " ".join(str(o) for o in objects)
    extra = [f"LIBS={' '.join(str(o) for o in objects)}"] if (objects and not coverage) else []

    if recipe.has_configure:
        if recipe.autoreconf_fallback:
            step("autoreconf-if-needed", ["sh", "-c", "[ -x ../configure ] || autoreconf -fvi .."], obj, env)
        step("configure", ["../configure", *configure_args], obj, env)
    for index, command in enumerate(_make_commands(recipe, ctx.jobs, extra)):
        step(f"make-{index}", command, obj, env)

    if style == "driver":
        assert recipe.driver is not None
        step("install-driver", ["cp", str(files[recipe.driver]), str(root / "test.c")], root)
        if coverage:
            argv = [
                ctx.cc,
                *COVERAGE_CFLAGS.split(),
                "-I../lib",
                "../test.c",
                "lib/.libs/libmatheval.a",
                "-lm",
                "-o",
                "program",
            ]
        else:
            argv = [
                ctx.sanitizer_cc,
                *SANITIZER_COMPILE,
                "-I",
                str(root),
                str(root / "test.c"),
                str(obj / "lib/.libs/libmatheval.a"),
                "-lm",
                *SANITIZER_LINK,
                "-o",
                str(obj / "program"),
            ]
        step("link-driver", argv, obj)
    return root, root, steps


def stage_sources(doc: Mapping[str, Any], root: Path) -> list[str]:
    """Copy generated sources to the location GCC recorded (``build.stage_sources``).

    A destination that already holds identical content is left alone; different
    content, or a missing source, is an error. Returns the staged destinations.
    """

    staged: list[str] = []
    for entry in doc["build"].get("stage_sources", []):
        source = root / entry["source"]
        destination = root / entry["recorded_path"]
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"generated source to stage is missing: {source}")
        if destination.exists() or destination.is_symlink():
            if destination.is_file() and sha256_file(destination) == sha256_file(source):
                continue
            raise SystemExit(f"staging target exists with other content: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        staged.append(entry["recorded_path"])
    return staged


def binary_path(doc: Mapping[str, Any], variant: str) -> str:
    """Path of the program binary relative to the build root of a variant."""

    relative = doc["binary"]
    prefix = "obj-gcov/"
    assert relative.startswith(prefix)
    return VARIANTS[variant]["objname"] + "/" + relative[len(prefix):]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tool_line(argv: Sequence[str]) -> str | None:
    try:
        result = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError:
        return None
    lines = result.stdout.strip().splitlines()
    return lines[0] if lines else None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial-{os.getpid()}")
    partial.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


_UNRECOGNIZED_OPTIONS = re.compile(r"^configure: WARNING: unrecognized options: ([^\n]*)", re.MULTILINE)


def unexpected_unrecognized_options(log_text: str, tolerated: Sequence[str] = ()) -> list[str]:
    """Options a configure script reported as unrecognised (``configure: WARNING: unrecognized options: A, B``)
    that are not in ``tolerated``, sorted."""

    reported: set[str] = set()
    for match in _UNRECOGNIZED_OPTIONS.finditer(log_text):
        reported.update(part.strip() for part in match.group(1).split(",") if part.strip())
    return sorted(reported - set(tolerated))


def run_steps(steps: Sequence[Step], log_path: Path, ignored_configure_options: Sequence[str] = ()) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        for item in steps:
            if shutil.which(item.argv[0]) is None and not Path(item.argv[0]).exists() and not item.argv[0].startswith("."):
                raise SystemExit(f"missing tool: {item.argv[0]} (step {item.tag})")
            log.write(json.dumps({"step": item.tag, "argv": list(item.argv), "env": dict(item.env)}) + "\n")
            log.flush()
            item.cwd.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(dict(item.env))
            result = subprocess.run(
                list(item.argv),
                cwd=item.cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            log.flush()
            if result.returncode != 0:
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
                print("\n".join(tail), file=sys.stderr)
                raise SystemExit(f"step {item.tag} failed with status {result.returncode}; see {log_path}")
            if item.tag.startswith("configure"):
                text = log_path.read_text(encoding="utf-8", errors="replace")
                ignored = unexpected_unrecognized_options(text, ignored_configure_options)
                if ignored:
                    raise SystemExit(f"configure ignored a pinned option ({', '.join(ignored)}); see {log_path}")


def _dynamic_project_libraries(binary: Path) -> list[str]:
    if shutil.which("readelf") is None:
        return []
    result = subprocess.run(["readelf", "-d", str(binary)], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    needed = re.findall(r"Shared library: \[([^\]]+)\]", result.stdout)
    return [n for n in needed if re.fullmatch(r"lib(?:ogg|vorbis(?:enc|file)?)\.so(?:\..*)?", n)]


def build_one(
    doc: Mapping[str, Any],
    recipe: Recipe,
    variant: str,
    ctx: Context,
    *,
    archive: Path,
    dependencies: Mapping[str, Path],
    files: Mapping[str, Path],
    program_sha256: str,
) -> Path:
    root, source_root, steps = plan_build(doc, recipe, variant, ctx, archive=archive, dependencies=dependencies, files=files)
    if root.exists():
        raise SystemExit(f"refusing to replace an existing build: {root}")
    meta = ctx.out / doc["id"] / "meta"
    label = VARIANTS[variant]["dirname"]
    record_path = meta / f"{label}-build.json"
    log_path = meta / f"{label}-build.log"
    started = _now()
    base_record: dict[str, Any] = {
        "schema": BUILD_SCHEMA,
        "program": doc["id"],
        "variant": variant,
        "build_root": str(root),
        "source_root": source_root.relative_to(root).as_posix() if source_root != root else "",
        "binary": binary_path(doc, variant),
        "program_sha256": program_sha256,
        "source_sha256": doc["source"]["sha256"],
        "image": os.environ.get("AGENT_SOFTWARE_TESTING_IMAGE_ID"),
        "started_utc": started,
    }
    _write_json(record_path, {**base_record, "status": "incomplete"})
    try:
        root.mkdir(parents=True)
        run_steps(steps, log_path, recipe.ignored_configure_options)
        binary = root / binary_path(doc, variant)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise SystemExit(f"missing executable {binary}")
        extra: dict[str, Any] = {}
        if variant == "coverage":
            for pattern in ("*.gcda", "*.gcov"):
                for stale in root.rglob(pattern):
                    stale.unlink()
            notes = sorted(root.rglob("*.gcno"))
            if not notes:
                raise SystemExit("the coverage build produced no .gcno files")
            extra["gcno_files"] = len(notes)
            extra["staged_sources"] = stage_sources(doc, root)
            extra["compiler"] = _tool_line([ctx.cc, "--version"])
        else:
            if doc["id"] == "oggenc":
                dynamic = _dynamic_project_libraries(binary)
                if dynamic:
                    raise SystemExit(f"sanitised oggenc links project libraries dynamically: {dynamic}")
            extra["compiler"] = _tool_line([ctx.sanitizer_cc, "--version"])
        record = {
            **base_record,
            **extra,
            "status": "complete",
            "binary_sha256": sha256_file(binary),
            "completed_utc": _now(),
            "steps": [s.record(root) for s in steps],
        }
        _write_json(record_path, record)
    except BaseException as error:
        _write_json(record_path, {**base_record, "status": "failed", "error": f"{type(error).__name__}: {error}", "ended_utc": _now()})
        raise
    return root


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the pinned Agent Software Testing programs (coverage and ASan+UBSan builds).",
    )
    parser.add_argument("programs", nargs="*", metavar="PROGRAM", help="program ids to build")
    parser.add_argument("--all", action="store_true", help="build every program in --programs")
    parser.add_argument("--definitions", dest="programs_dir", type=Path, default=DEFAULT_PROGRAMS, help="directory of program definitions (default: ../programs)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="build output directory (default: $AGENT_SOFTWARE_TESTING_BUILDS or /opt/agent-software-testing/builds)")
    parser.add_argument("--cache", type=Path, help="source archive cache (default: <out>/sources)")
    parser.add_argument("--assets", type=Path, help="directory with the third-party build inputs (default: third_party/concollmic next to this script); laid out as <program>/<file>")
    parser.add_argument("--variant", choices=("coverage", "sanitizer", "both"), default="both")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="parallel make jobs")
    parser.add_argument("--cc", default="gcc", help="C compiler of the coverage build")
    parser.add_argument("--cxx", default="g++", help="C++ compiler of the coverage build")
    parser.add_argument("--sanitizer-cc", default="clang-15")
    parser.add_argument("--sanitizer-cxx", default="clang++-15")
    parser.add_argument("--offline", action="store_true", help="never download; every archive must be in the cache")
    parser.add_argument("--dry-run", action="store_true", help="print the planned commands; touch nothing")
    parser.add_argument("--list", action="store_true", help="list the programs and exit")
    parser.add_argument("--verify-definitions", action="store_true", help="check all program.json files and recipes, then exit")
    parser.add_argument("--allow-file-urls", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None, *, recipes: Mapping[str, Recipe] | None = None) -> int:
    args = _parser().parse_args(argv)
    recipes = RECIPES if recipes is None else recipes
    programs_dir: Path = args.programs_dir

    if args.verify_definitions:
        problems = verify_definitions(programs_dir)
        for problem in problems:
            print(problem)
        print(f"{len(program_ids(programs_dir))} program definitions checked, {len(problems)} problems")
        return 1 if problems else 0

    if args.list:
        for pid in program_ids(programs_dir):
            doc = load_definition(programs_dir, pid)
            print(f"{pid:12s} {doc['version']:36.36s} {doc['branch_total']:>7d}  {doc['status']}")
        return 0

    ids = program_ids(programs_dir) if args.all else list(args.programs)
    if not ids:
        print("name at least one program or pass --all", file=sys.stderr)
        return 2
    unknown = [pid for pid in ids if pid not in recipes or not (programs_dir / pid / "program.json").is_file()]
    if unknown:
        print(f"unknown programs: {unknown}", file=sys.stderr)
        return 2

    # --assets wins; otherwise the directory next to this script when it exists; otherwise None, so that a
    # copy of the script without that directory stops with the message that names --assets.
    assets_dir: Path | None = args.assets if args.assets is not None else (DEFAULT_ASSETS if DEFAULT_ASSETS.is_dir() else None)

    variants = ["coverage", "sanitizer"] if args.variant == "both" else [args.variant]
    ctx = Context(
        out=args.out.resolve() if not args.dry_run else args.out,
        jobs=max(1, args.jobs),
        cc=args.cc,
        cxx=args.cxx,
        sanitizer_cc=args.sanitizer_cc,
        sanitizer_cxx=args.sanitizer_cxx,
    )
    cache = args.cache or (ctx.out / "sources")

    for pid in ids:
        doc = load_definition(programs_dir, pid)
        recipe = recipes[pid]
        required = not args.dry_run
        files = {
            key: resolve_file(key, programs_dir, assets_dir, required=required)
            for key in (*recipe.compat, *recipe.patches, *([recipe.driver] if recipe.driver else []))
        }
        if args.dry_run:
            archive = cache / cache_name(doc["source"]["url"], doc["source"]["sha256"])
            dependencies = {key: cache / cache_name(DEPENDENCIES[key].url, DEPENDENCIES[key].sha256) for key in recipe.deps}
        else:
            archive = fetch(doc["source"]["url"], doc["source"]["sha256"], cache, offline=args.offline, allow_file_urls=args.allow_file_urls)
            names = top_level_names(archive)
            if names != {doc["source"]["directory"]}:
                raise SystemExit(f"{pid}: archive top level {sorted(names)} differs from source.directory")
            dependencies = {
                key: fetch(DEPENDENCIES[key].url, DEPENDENCIES[key].sha256, cache, offline=args.offline, allow_file_urls=args.allow_file_urls)
                for key in recipe.deps
            }
        program_sha256 = hashlib.sha256((programs_dir / pid / "program.json").read_bytes()).hexdigest()
        for variant in variants:
            if args.dry_run:
                root, source_root, steps = plan_build(doc, recipe, variant, ctx, archive=archive, dependencies=dependencies, files=files)
                print(f"# {pid} {variant}: build root {root}, binary {binary_path(doc, variant)}")
                for item in steps:
                    env = " ".join(f"{k}={v!r}" for k, v in item.env)
                    print(f"  [{item.tag}] (cd {item.cwd} && {env + ' ' if env else ''}{' '.join(item.argv)})")
                continue
            print(f"building {pid} ({variant})", flush=True)
            root = build_one(
                doc,
                recipe,
                variant,
                ctx,
                archive=archive,
                dependencies=dependencies,
                files=files,
                program_sha256=program_sha256,
            )
            print(f"  built {root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
