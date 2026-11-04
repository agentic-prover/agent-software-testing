"""Tests for environment/build_programs.py.

Run from the repository root with ``python -m pytest environment``. The build tests use
small synthetic projects and whatever C compiler and make are on PATH (``cc`` is used
instead of gcc and clang-15); they say nothing about the real program sources.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_programs as bp  # noqa: E402

PROGRAMS = Path(__file__).resolve().parent.parent / "programs"
HAVE_TOOLCHAIN = all(shutil.which(tool) for tool in ("cc", "make", "tar", "patch"))
needs_toolchain = pytest.mark.skipif(not HAVE_TOOLCHAIN, reason="needs cc, make, tar and patch")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


def test_repository_definitions_are_valid() -> None:
    assert bp.verify_definitions(PROGRAMS) == []


def test_every_program_has_a_recipe_and_vice_versa() -> None:
    assert set(bp.program_ids(PROGRAMS)) == set(bp.RECIPES)


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda d: d.pop("binary"), "missing fields"),
        (lambda d: d.update(extra=1), "unknown fields"),
        (lambda d: d.update(branch_total="12"), "wrong type"),
        (lambda d: d["source"].update(sha256="abc"), "64 lowercase hex"),
        (lambda d: d["source"].update(url="http://example.org/x.tar.gz"), "https"),
        (lambda d: d.update(binary="/bin/ls"), "binary"),
        (lambda d: d.update(binary="obj-gcov/../x"), "binary"),
        (lambda d: d.update(status="measured"), "status"),
        (lambda d: d.update(inputs=["network"]), "inputs"),
        (lambda d: d.update(schema="agent-software-testing/program@9"), "schema"),
    ],
)
def test_validator_rejects_broken_definition(mutate, fragment) -> None:
    doc = json.loads((PROGRAMS / "xmlwf" / "program.json").read_text())
    mutate(doc)
    problems = bp.validate_definition(doc, directory="xmlwf")
    assert problems and any(fragment in p for p in problems), problems


def test_validator_checks_fixed_track_shape() -> None:
    doc = json.loads((PROGRAMS / "krep" / "program.json").read_text())
    assert bp.validate_definition(doc, directory="krep") == []
    bad = copy.deepcopy(doc)
    bad["fixed_track"]["argv_template"] = ["test"]
    assert any("exactly one {input}" in p for p in bp.validate_definition(bad))
    bad = copy.deepcopy(doc)
    bad["fixed_track"]["input_channel"] = "stdin"
    assert any("must not contain {input}" in p for p in bp.validate_definition(bad))
    bad = copy.deepcopy(doc)
    bad["fixed_track"]["argv_template"] = ["test", "x{input}"]
    assert any("whole argv element" in p for p in bp.validate_definition(bad))


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def plan(pid: str, variant: str):
    doc = bp.load_definition(PROGRAMS, pid)
    recipe = bp.RECIPES[pid]
    ctx = bp.Context(out=Path("/out"), jobs=8)
    files = {key: Path("/files") / key for key in (*recipe.compat, *recipe.patches, *([recipe.driver] if recipe.driver else []))}
    deps = {key: Path("/deps") / key for key in recipe.deps}
    return doc, bp.plan_build(doc, recipe, variant, ctx, archive=Path("/src/archive.tar"), dependencies=deps, files=files)


@pytest.mark.parametrize("pid", sorted(bp.RECIPES))
@pytest.mark.parametrize("variant", ["coverage", "sanitizer"])
def test_plan_for_every_program(pid: str, variant: str) -> None:
    doc, (root, source_root, steps) = plan(pid, variant)
    assert root == Path("/out") / pid / ("gcov" if variant == "coverage" else "asan-ubsan")
    assert steps and steps[0].argv[0] == "mkdir"
    text = " ".join(" ".join(s.argv) + " " + " ".join(v for _, v in s.env) for s in steps)
    if variant == "coverage":
        assert "-fprofile-arcs -ftest-coverage" in text and "-O0" in text
        assert "-fsanitize" not in text
    else:
        assert "-fsanitize=address,undefined" in text
        assert "-fprofile-arcs" not in text
    assert bp.binary_path(doc, variant).startswith("obj-gcov/" if variant == "coverage" else "obj-native/")


def test_quirks_are_kept() -> None:
    def commands(pid: str, variant: str = "coverage") -> list[str]:
        _doc, (_root, _src, steps) = plan(pid, variant)
        return [" ".join(s.argv) for s in steps]

    assert any("make -j1 MAKEINFO=true -C lib" in c for c in commands("bc"))
    assert any("make -j1 MAKEINFO=true -C bc" in c for c in commands("bc"))
    assert any(c.startswith("make -j8 -f ../Makefile VPATH=..") for c in commands("krep"))
    assert sum(c.startswith("patch --batch --forward -p1") for c in commands("krep")) == 2
    assert not any("configure" in c for c in commands("krep"))
    assert any("-C lib" in c for c in commands("gcal")) and any("-C src" in c for c in commands("gcal"))
    _doc, (_root, _src, steps) = plan("gzip", "coverage")
    assert any(("gl_cv_crc_pclmul", "no") in s.env for s in steps if s.tag == "configure")
    # gnulib's printf out-of-memory probe aborts under Rosetta; the preset keeps the universe at the published 3,083
    assert any(("gl_cv_func_printf_enomem", "yes") in s.env for s in steps if s.tag == "configure")
    _doc, (_root, _src, steps) = plan("jq", "coverage")
    configure = next(s for s in steps if s.tag == "configure")
    assert dict(configure.env)["LIBS"].endswith("agent-software-testing-compat-0.o")
    _doc, (_root, _src, steps) = plan("jq", "sanitizer")
    assert any(a.startswith("LIBS=") for s in steps if s.tag.startswith("make") for a in s.argv)
    assert any("-fPIC" in s.argv and "-c" in s.argv for s in steps)
    assert any("-I../lib" in c for c in commands("libmatheval"))
    assert any(c.startswith("./autogen.sh") for c in commands("oggenc"))
    sanitizer = commands("oggenc", "sanitizer")
    assert sum("--disable-shared --enable-static" in c for c in sanitizer) == 2
    assert any("autoreconf -fvi" in c for c in sanitizer)
    woff2 = commands("woff2", "sanitizer")
    assert any(c.startswith("make -j8 CC=clang-15 CXX=clang++-15") and c.endswith(" all") for c in woff2)
    assert any("brotli" in c for c in commands("woff2"))


def test_only_libmatheval_tolerates_an_ignored_configure_option() -> None:
    # libmatheval's configure script warns "unrecognized options: --disable-nls" and ignores the option (found by
    # the first real build); the reference build passed it all the same, and the universe is the published one.
    _doc, (_root, _src, steps) = plan("libmatheval", "coverage")
    assert "--disable-nls" in next(s for s in steps if s.tag == "configure").argv


def test_a_recipe_may_only_tolerate_an_option_that_the_definition_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(bp.RECIPES, "gzip", bp.Recipe(env=(("gl_cv_crc_pclmul", "no"),), ignored_configure_options=("--no-such-option",)))
    problems = bp.verify_definitions(PROGRAMS)
    assert any("gzip" in p and "--no-such-option" in p and "does not pass" in p for p in problems), problems


def test_unrecognized_configure_options_are_read_from_the_log() -> None:
    log = "checking x\nconfigure: WARNING: unrecognized options: --disable-nls, --with-foo\nmore\nconfigure: WARNING: unrecognized options: --disable-nls\n"
    assert bp.unexpected_unrecognized_options(log) == ["--disable-nls", "--with-foo"]
    assert bp.unexpected_unrecognized_options(log, ["--disable-nls"]) == ["--with-foo"]
    assert bp.unexpected_unrecognized_options(log, ["--disable-nls", "--with-foo"]) == []
    assert bp.unexpected_unrecognized_options("configure: WARNING: something else\n") == []


def test_a_tolerated_option_does_not_stop_the_build_but_another_one_does(tmp_path: Path) -> None:
    def configure_step(option: str) -> bp.Step:
        return bp.Step(tag="configure", argv=("echo", f"configure: WARNING: unrecognized options: {option}"), cwd=tmp_path / "work")

    bp.run_steps([configure_step("--disable-nls")], tmp_path / "ok.log", ["--disable-nls"])
    assert "unrecognized options: --disable-nls" in (tmp_path / "ok.log").read_text()
    with pytest.raises(SystemExit, match=r"ignored a pinned option \(--disable-nls\)"):
        bp.run_steps([configure_step("--disable-nls")], tmp_path / "strict.log")
    with pytest.raises(SystemExit, match=r"ignored a pinned option \(--other\)"):
        bp.run_steps([configure_step("--disable-nls, --other")], tmp_path / "other.log", ["--disable-nls"])


def test_libmatheval_stages_generated_sources(tmp_path: Path) -> None:
    doc = bp.load_definition(PROGRAMS, "libmatheval")
    root = tmp_path / "gcov"
    (root / "lib").mkdir(parents=True)
    for name in ("parser.c", "parser.y", "scanner.c", "scanner.l"):
        (root / "lib" / name).write_text(f"// {name}\n")
    assert len(bp.stage_sources(doc, root)) == 4
    assert (root / "obj-gcov" / "lib" / "parser.c").read_text() == "// parser.c\n"
    assert bp.stage_sources(doc, root) == []  # identical content: left alone
    (root / "obj-gcov" / "lib" / "parser.c").write_text("changed")
    with pytest.raises(SystemExit, match="other content"):
        bp.stage_sources(doc, root)
    (root / "obj-gcov" / "lib" / "parser.c").write_text("// parser.c\n")
    (root / "lib" / "scanner.l").unlink()
    with pytest.raises(SystemExit, match="missing"):
        bp.stage_sources(doc, root)


def test_the_generated_sources_of_bc_gawk_and_jq_are_staged(tmp_path: Path) -> None:
    # GCC records the bison/flex outputs and their grammars under obj-gcov, where only the copies of the staging step
    # exist; without them the initial lcov capture prints "could not open ..." warnings, which the runner refuses
    # (SPEC.md 3.3 step 4).
    expected = {
        "bc": {"obj-gcov/bc/bc.c": "bc/bc.c", "obj-gcov/bc/bc.y": "bc/bc.y", "obj-gcov/bc/scan.c": "bc/scan.c", "obj-gcov/bc/scan.l": "bc/scan.l"},
        "gawk": {"obj-gcov/awkgram.c": "awkgram.c", "obj-gcov/awkgram.y": "awkgram.y", "obj-gcov/command.c": "command.c", "obj-gcov/command.y": "command.y"},
        "jq": {"obj-gcov/src/lexer.c": "src/lexer.c", "obj-gcov/src/lexer.l": "src/lexer.l", "obj-gcov/src/parser.c": "src/parser.c", "obj-gcov/src/parser.y": "src/parser.y"},
    }
    for pid, mapping in expected.items():
        doc = bp.load_definition(PROGRAMS, pid)
        assert bp.validate_definition(doc) == []
        root = tmp_path / pid
        for source in mapping.values():
            (root / source).parent.mkdir(parents=True, exist_ok=True)
            (root / source).write_text(f"// {source}\n")
        assert len(bp.stage_sources(doc, root)) == 4
        for recorded, source in mapping.items():
            assert (root / recorded).read_text() == f"// {source}\n"
        assert bp.stage_sources(doc, root) == []  # identical content: left alone


def test_validator_checks_stage_sources() -> None:
    doc = json.loads((PROGRAMS / "libmatheval" / "program.json").read_text())
    assert bp.validate_definition(doc) == []
    doc["build"]["stage_sources"].append({"source": "/etc/passwd", "recorded_path": "x"})
    assert any("relative path" in p for p in bp.validate_definition(doc))
    doc["build"]["stage_sources"] = [{"source": "a"}]
    assert any("exactly source and recorded_path" in p for p in bp.validate_definition(doc))


def test_confetti_has_autoreconf_fallback_only() -> None:
    _d, (_r, _s, steps) = plan("confetti", "coverage")
    assert any(s.tag == "autoreconf-if-needed" for s in steps)
    _d, (_r, _s, steps) = plan("gzip", "coverage")
    assert not any(s.tag == "autoreconf-if-needed" for s in steps)


def test_dry_run_command_line(capsys, tmp_path) -> None:
    code = bp.main(["--definitions", str(PROGRAMS), "--out", str(tmp_path / "out"), "--dry-run", "--all"])
    assert code == 0
    assert "gzip" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def make_archive(path: Path, files: dict[str, bytes], top: str = "proj-1.0") -> None:
    with tarfile.open(path, "w:gz") as handle:
        for name, content in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(content)
            info.mode = 0o755 if name == "configure" else 0o644
            handle.addfile(info, io.BytesIO(content))


def test_fetch_checks_digest_and_never_replaces(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    make_archive(archive, {"x": b"1"})
    url = archive.as_uri()
    good = bp.sha256_file(archive)
    cache = tmp_path / "cache"
    path = bp.fetch(url, good, cache, offline=False, allow_file_urls=True)
    assert path.read_bytes() == archive.read_bytes()
    # second call is served from the cache even offline
    assert bp.fetch(url, good, cache, offline=True, allow_file_urls=True) == path
    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        bp.fetch(url, "0" * 64, tmp_path / "cache2", offline=False, allow_file_urls=True)
    assert not list((tmp_path / "cache2").glob("*"))
    path.write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="wrong SHA-256"):
        bp.fetch(url, good, cache, offline=True, allow_file_urls=True)
    assert path.read_bytes() == b"tampered"  # never replaced


def test_fetch_refuses_plain_http_and_file_urls_by_default(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="non-https"):
        bp.fetch("http://example.org/a.tar.gz", "0" * 64, tmp_path, offline=False)
    with pytest.raises(SystemExit, match="non-https"):
        bp.fetch((tmp_path / "a").as_uri(), "0" * 64, tmp_path, offline=False)
    with pytest.raises(SystemExit, match="offline"):
        bp.fetch("https://example.org/a.tar.gz", "0" * 64, tmp_path, offline=True)


def test_fetch_reports_a_failed_download_in_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A host without network access (name resolution fails) gets one line and no traceback.
    def refuse(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(bp.urllib.request, "urlopen", refuse)
    cache = tmp_path / "cache"
    with pytest.raises(SystemExit, match=r"cannot download https://example\.org/a\.tar\.gz: .*name resolution failed"):
        bp.fetch("https://example.org/a.tar.gz", "0" * 64, cache, offline=False)
    assert not list(cache.iterdir())  # no partial file is left behind


def test_assets_are_pinned_and_a_missing_file_is_named(tmp_path: Path) -> None:
    (tmp_path / "krep").mkdir()
    (tmp_path / "krep" / "krep.diff").write_text("not the pinned file")
    with pytest.raises(SystemExit, match="differs from the pinned value"):
        bp.resolve_file("krep/krep.diff", PROGRAMS, tmp_path, required=True)
    with pytest.raises(SystemExit, match="missing asset file") as missing:
        bp.resolve_file("krep/Makefile.diff", PROGRAMS, tmp_path, required=True)
    assert str(tmp_path / "krep" / "Makefile.diff") in str(missing.value)  # reported by its path
    # repository files verify without any assets directory
    assert bp.resolve_file("jq/compat/jq-single-process.c", PROGRAMS, None, required=True).is_file()


def test_without_an_assets_directory_the_third_party_inputs_are_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # resolve_file with no directory at all (a copy of the script without third_party/)
    with pytest.raises(SystemExit, match=r"third-party build input.*environment/third_party/concollmic.*--assets"):
        bp.resolve_file("krep/krep.diff", PROGRAMS, None, required=True)
    # main(): no --assets and the default directory does not exist
    monkeypatch.setattr(bp, "DEFAULT_ASSETS", tmp_path / "no-such-directory")
    with pytest.raises(SystemExit, match="third-party build input"):
        bp.main(["--definitions", str(PROGRAMS), "--out", str(tmp_path / "out"), "--cache", str(tmp_path / "cache"), "krep"])
    assert not (tmp_path / "out").exists()


def capture_build_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], program: str = "krep") -> dict[str, Path]:
    """Run main() for ``program`` with the download and the build replaced; return the ``files`` it resolved."""
    doc = bp.load_definition(PROGRAMS, program)
    archive = tmp_path / "source.tar.gz"
    make_archive(archive, {"x": b"1"}, top=doc["source"]["directory"])
    seen: dict[str, Path] = {}

    def fake_build_one(doc_, recipe, variant, ctx, *, files, **kwargs):  # noqa: ANN001, ANN202
        seen.update(files)
        return tmp_path / "built"

    monkeypatch.setattr(bp, "fetch", lambda *args, **kwargs: archive)
    monkeypatch.setattr(bp, "build_one", fake_build_one)
    code = bp.main(["--definitions", str(PROGRAMS), "--out", str(tmp_path / "out"), "--variant", "coverage", *argv, program])
    assert code == 0
    return seen


def test_the_default_assets_directory_is_used_when_assets_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert bp.DEFAULT_ASSETS == Path(bp.__file__).resolve().parent / "third_party" / "concollmic"
    assert bp.DEFAULT_ASSETS.is_dir(), "a full checkout carries the third-party build inputs"
    seen = capture_build_inputs(tmp_path, monkeypatch, [])
    assert set(seen) == {"krep/Makefile.diff", "krep/krep.diff"}
    for key, path in seen.items():
        assert path == bp.DEFAULT_ASSETS / key
        assert bp.sha256_file(path) == bp.FILES[key].sha256  # the digest check ran on the files it found
    # libmatheval's driver comes from there as well
    assert capture_build_inputs(tmp_path, monkeypatch, [], program="libmatheval") == {"libmatheval/test.c": bp.DEFAULT_ASSETS / "libmatheval/test.c"}


def test_assets_overrides_the_default_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    for key in ("krep/Makefile.diff", "krep/krep.diff"):
        (other / key).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bp.DEFAULT_ASSETS / key, other / key)
    seen = capture_build_inputs(tmp_path, monkeypatch, ["--assets", str(other)])
    assert seen == {key: other / key for key in ("krep/Makefile.diff", "krep/krep.diff")}
    # a different directory is used even when it is wrong: the default is not a fallback
    (other / "krep" / "krep.diff").write_text("not the pinned file")
    with pytest.raises(SystemExit, match="differs from the pinned value"):
        capture_build_inputs(tmp_path, monkeypatch, ["--assets", str(other)])
    (other / "krep" / "krep.diff").unlink()
    with pytest.raises(SystemExit, match="missing asset file for krep/krep.diff"):
        capture_build_inputs(tmp_path, monkeypatch, ["--assets", str(other)])


def test_the_dry_run_plan_prints_the_resolved_third_party_paths(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out = str(tmp_path / "out")
    assert bp.main(["--definitions", str(PROGRAMS), "--out", out, "--dry-run", "--variant", "coverage", "krep", "libmatheval"]) == 0
    plan = capsys.readouterr().out
    assert "<assets>" not in plan
    for key in ("krep/Makefile.diff", "krep/krep.diff", "libmatheval/test.c"):
        assert str(bp.DEFAULT_ASSETS / key) in plan
    other = tmp_path / "elsewhere"
    assert bp.main(["--definitions", str(PROGRAMS), "--out", out, "--dry-run", "--variant", "coverage", "--assets", str(other), "krep"]) == 0
    plan = capsys.readouterr().out
    assert str(other / "krep" / "krep.diff") in plan and str(bp.DEFAULT_ASSETS) not in plan
    assert not (tmp_path / "out").exists()


# The third-party build inputs: six files whose bytes are pinned. The three inputs are pinned in FILES and
# written in both READMEs; the three licence texts are verbatim copies, pinned here.
THIRD_PARTY_LICENCES = {
    "COPYING": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
    "LICENSE-CONCOLLMIC.txt": "78848b4ebc3a4f55e8c4608ecd3e6568ba742cef4b8f0f17b3249e8af39e0466",
    "LICENSE-KREP.txt": "5aa4ea1dc38ee28a33f1265e1a0212e3e9c531220438fc40db9d697994a1adc1",
}


def readme_digests(path: Path) -> dict[str, list[str]]:
    """Map each ``program/file`` named in a table row of a README to the SHA-256 values written in that row."""
    found: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        names = re.findall(r"`((?:krep|libmatheval)/[A-Za-z.]+)`", line)
        digests = re.findall(r"`([0-9a-f]{64})`", line)
        if names and digests:
            found.setdefault(names[0], []).extend(digests)
    return found


def test_the_third_party_files_are_the_pinned_bytes() -> None:
    root = bp.DEFAULT_ASSETS
    inputs = {key: spec for key, spec in bp.FILES.items() if spec.origin == "asset"}
    # exactly these files, nothing next to them (no caches, no stray edits)
    litter = {".DS_Store"}  # what the macOS Finder leaves in a directory it has shown
    present = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name not in litter)
    assert present == sorted({*inputs, *THIRD_PARTY_LICENCES, "README.md"})
    # every byte is pinned: FILES for the inputs, this table for the licence texts
    for key, spec in inputs.items():
        assert spec.path == key and bp.sha256_file(root / key) == spec.sha256, key
    for name, digest in THIRD_PARTY_LICENCES.items():
        assert bp.sha256_file(root / name) == digest, name
    # the READMEs that describe the files carry the same digests as the builder: any change to a file, to the
    # table or to FILES fails here
    for readme in (root / "README.md", Path(__file__).resolve().parent / "README.md"):
        written = readme_digests(readme)
        for key, spec in inputs.items():
            assert spec.sha256 in written.get(key, []), (readme, key)
    # the files are byte-exact copies: no carriage returns and no editor repairs of the patches' blank lines
    for key in inputs:
        assert b"\r" not in (root / key).read_bytes(), key


# ---------------------------------------------------------------------------
# Synthetic builds
# ---------------------------------------------------------------------------

HELLO_C = b"#include <stdio.h>\nint main(int argc, char **argv) {\n  if (argc > 1) { puts(argv[1]); return 3; }\n  return 0;\n}\n"

CONFIGURE = b"""#!/bin/sh
# synthetic out-of-tree configure
srcdir=$(dirname "$0")
echo "$@" > config.args
{
  echo "VPATH = $srcdir"
  echo "all: hello"
  printf 'hello: hello.c\\n\\t$(CC) $(CFLAGS) $(LDFLAGS) -o hello $<\\n'
} > Makefile
"""

PLAIN_MAKEFILE = b"""BASE_CFLAGS += -O1
CFLAGS += $(BASE_CFLAGS)
LDFLAGS += $(BASE_LDFLAGS)
all: hello
hello: hello.c
\t$(CC) $(CFLAGS) $(LDFLAGS) -o hello $<
"""


def synthetic_doc(pid: str, archive: Path, configure: list[str]) -> dict:
    return {
        "schema": bp.PROGRAM_SCHEMA,
        "id": pid,
        "name": pid,
        "version": "1.0",
        "source": {"url": archive.as_uri(), "sha256": bp.sha256_file(archive), "directory": "proj-1.0"},
        "build": {"configure": configure, "notes": "synthetic"},
        "binary": "obj-gcov/hello",
        "branch_total": 2,
        "fixed_track": None,
        "inputs": ["argv"],
        "status": "open-track-only",
    }


def run_build(tmp_path: Path, recipe: bp.Recipe, files_content: dict[str, bytes], variant: str, *, doc_binary: str = "obj-gcov/hello", configure=None, files=None):
    archive = tmp_path / "proj.tar.gz"
    make_archive(archive, files_content)
    doc = synthetic_doc("synthetic", archive, configure if configure is not None else [])
    doc["binary"] = doc_binary
    ctx = bp.Context(out=tmp_path / "out", jobs=2, cc="cc", cxx="c++", sanitizer_cc="cc", sanitizer_cxx="c++")
    cached = bp.fetch(doc["source"]["url"], doc["source"]["sha256"], tmp_path / "cache", offline=False, allow_file_urls=True)
    assert bp.top_level_names(cached) == {"proj-1.0"}
    root = bp.build_one(doc, recipe, variant, ctx, archive=cached, dependencies={}, files=files or {}, program_sha256="0" * 64)
    return doc, ctx, root


@needs_toolchain
def test_autotools_coverage_build(tmp_path: Path) -> None:
    doc, ctx, root = run_build(
        tmp_path, bp.Recipe(), {"configure": CONFIGURE, "hello.c": HELLO_C}, "coverage", configure=["--disable-nls"]
    )
    assert root == ctx.out / "synthetic" / "gcov"
    assert (root / "obj-gcov" / "hello").is_file()
    assert (root / "obj-gcov" / "config.args").read_text().strip() == "--disable-nls"
    record = json.loads((ctx.out / "synthetic" / "meta" / "gcov-build.json").read_text())
    assert record["status"] == "complete" and record["gcno_files"] >= 1
    assert record["binary"] == "obj-gcov/hello" and record["source_root"] == ""
    assert record["binary_sha256"] == bp.sha256_file(root / "obj-gcov" / "hello")
    assert [s["tag"] for s in record["steps"]][-2:] == ["configure", "make-0"]
    assert not list(root.rglob("*.gcda"))
    assert (ctx.out / "synthetic" / "meta" / "gcov-build.log").read_text().count('"step"') >= 4


@needs_toolchain
def test_autotools_sanitizer_build_and_refusal_to_replace(tmp_path: Path) -> None:
    doc, ctx, root = run_build(tmp_path, bp.Recipe(), {"configure": CONFIGURE, "hello.c": HELLO_C}, "sanitizer", doc_binary="obj-gcov/hello")
    assert root == ctx.out / "synthetic" / "asan-ubsan"
    assert (root / "obj-native" / "hello").is_file()
    record = json.loads((ctx.out / "synthetic" / "meta" / "asan-ubsan-build.json").read_text())
    assert record["binary"] == "obj-native/hello" and record["status"] == "complete"
    cached = tmp_path / "cache" / next((tmp_path / "cache").iterdir()).name
    with pytest.raises(SystemExit, match="refusing to replace"):
        bp.build_one(doc, bp.Recipe(), "sanitizer", ctx, archive=cached, dependencies={}, files={}, program_sha256="0" * 64)


@needs_toolchain
def test_make_style_with_patch(tmp_path: Path, monkeypatch) -> None:
    # the diff is generated from the real file text so that its context lines match
    original = HELLO_C.decode().splitlines(keepends=True)
    patched = [line.replace("return 0;", "return 7;") for line in original]
    patch_text = "".join(difflib.unified_diff(original, patched, "a/hello.c", "b/hello.c", n=2)).encode()
    assets = tmp_path / "assets"
    (assets / "synthetic").mkdir(parents=True)
    (assets / "synthetic" / "p.diff").write_bytes(patch_text)
    monkeypatch.setitem(bp.FILES, "synthetic/p.diff", bp.FileSpec("asset", "synthetic/p.diff", sha(patch_text), "test patch"))
    recipe = bp.Recipe(style="make", make_variables=("-f", "../Makefile", "VPATH=.."), patches=("synthetic/p.diff",))
    files = {"synthetic/p.diff": bp.resolve_file("synthetic/p.diff", PROGRAMS, assets, required=True)}
    _doc, ctx, root = run_build(tmp_path, recipe, {"Makefile": PLAIN_MAKEFILE, "hello.c": HELLO_C}, "coverage", files=files)
    assert "return 7" in (root / "hello.c").read_text()
    binary = root / "obj-gcov" / "hello"
    assert subprocess.run([str(binary)], check=False).returncode == 7


@needs_toolchain
def test_tree_style_builds_in_the_object_directory(tmp_path: Path) -> None:
    recipe = bp.Recipe(style="tree")
    _doc, ctx, root = run_build(tmp_path, recipe, {"Makefile": PLAIN_MAKEFILE, "hello.c": HELLO_C}, "coverage")
    assert (root / "obj-gcov" / "hello").is_file()
    assert (root / "obj-gcov" / "hello.c").is_file()  # source tree lives in obj-gcov
    record = json.loads((ctx.out / "synthetic" / "meta" / "gcov-build.json").read_text())
    assert record["source_root"] == "obj-gcov"


@needs_toolchain
def test_failed_build_is_recorded_and_stops(tmp_path: Path) -> None:
    broken = b"#!/bin/sh\nexit 3\n"
    archive = tmp_path / "p.tar.gz"
    make_archive(archive, {"configure": broken, "hello.c": HELLO_C})
    doc = synthetic_doc("broken", archive, [])
    ctx = bp.Context(out=tmp_path / "out", jobs=1, cc="cc")
    cached = bp.fetch(doc["source"]["url"], doc["source"]["sha256"], tmp_path / "cache", offline=False, allow_file_urls=True)
    with pytest.raises(SystemExit, match="step configure failed"):
        bp.build_one(doc, bp.Recipe(), "coverage", ctx, archive=cached, dependencies={}, files={}, program_sha256="0" * 64)
    record = json.loads((ctx.out / "broken" / "meta" / "gcov-build.json").read_text())
    assert record["status"] == "failed" and "step configure failed" in record["error"]


@needs_toolchain
def test_configure_warning_about_ignored_options_is_fatal(tmp_path: Path) -> None:
    noisy = b'#!/bin/sh\necho "configure: WARNING: unrecognized options: --bogus"\nprintf "all:\\n\\ttrue\\n" > Makefile\n'
    archive = tmp_path / "p.tar.gz"
    make_archive(archive, {"configure": noisy, "hello.c": HELLO_C})
    doc = synthetic_doc("noisy", archive, ["--bogus"])
    ctx = bp.Context(out=tmp_path / "out", jobs=1, cc="cc")
    cached = bp.fetch(doc["source"]["url"], doc["source"]["sha256"], tmp_path / "cache", offline=False, allow_file_urls=True)
    with pytest.raises(SystemExit, match="ignored a pinned option"):
        bp.build_one(doc, bp.Recipe(), "coverage", ctx, archive=cached, dependencies={}, files={}, program_sha256="0" * 64)
