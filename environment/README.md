# Replay environment

The `toolchain` target of the `Dockerfile` builds (with the base image pinned as described under "Image") and
`verify_image.py` accepts the result. `build_programs.py` builds the coverage build of all 18 programs in the image.
The branch universe of each build, computed by the runner's own initial capture, equals the published universe
(`agent-software-testing-results/universe/<id>.txt.gz`) key by key and has `branch_total` keys. `Dockerfile`,
`toolchain.lock.json` and `build_programs.py` are adapted from the toolchain and build driver that produced the
reference branch totals, with all symbolic-execution tooling removed. The unit tests (`python -m pytest environment`)
cover the definitions, the planned commands, source fetching and digest checks, and complete builds of small
synthetic projects.

## Contents

| File | Purpose |
|---|---|
| `Dockerfile` | Ubuntu 22.04 from a dated archive snapshot with gcc/gcov 11.4, lcov 1.14, clang 15 (sanitisers) and the libraries two programs link. Targets: `toolchain` (default) and `builds`. |
| `toolchain.lock.json` | Declared versions and required commands. The package inventory is `null` until an image build records it. |
| `verify_image.py` | Compares the running image with `toolchain.lock.json`. |
| `build_programs.py` | Downloads each pinned source, checks its SHA-256 and builds the coverage and ASan+UBSan builds. |
| `third_party/concollmic/` | Three third-party build inputs (two Krep patches, the libmatheval driver) with the licence texts that apply to them; see "Third-party build inputs". |
| `test_build_programs.py`, `test_verify_image.py`, `test_dockerfile.py` | Tests for the two scripts, and text checks of the Dockerfile and the lock file (TLS verification stays on, the base image is pinned by its recorded digest). |

## Image

The image contains no KLEE, no LLVM bitcode tools and nothing specific to any test
generator. It differs from the image used for the reference measurement in these ways.

* The package list is written from the build requirements of the 18 programs, not copied
  from the measurement image. The measurement image also carried the packages
  that KLEE's build installs; `flex`, `bison`, `guile-3.0-dev` and the Ogg/Vorbis
  development packages are kept because configure scripts of some programs probe for them.
* The base image is pinned by digest in the Dockerfile and in `toolchain.lock.json`
  (`ubuntu:jammy-20251001`, the multi-architecture index digest).
* The base image must not be newer than the package snapshot. A newer `ubuntu:22.04` holds a
  `libc6` newer than the `libc6-dev` of the snapshot (22 June 2026), and the package installation stops with
  "held broken packages". Pass the pinned image with
  `--build-arg BASE_IMAGE=ubuntu:jammy-20251001@sha256:09506232a8004baa32c47d68f1e5c307d648fdd59f5e7eaa42aaf87914100db3`
  (the multi-platform index of that tag, read with `docker buildx imagetools inspect`). `verify_image.py` accepts the
  result: gcc and gcov 11.4.0-1ubuntu1~22.04.3, lcov 1.15-1 (which reports 1.14), clang-15 15.0.7, Python 3.10.12.
* `tzdata` is installed. The reference build host had the zoneinfo database, and gnulib's "working mktime" probe
  (`m4/mktime.m4`, `indiana_test`) only sees the C library's mktime defect when it exists. Without it configure keeps
  libc's mktime, `lib/mktime.c` is not linked into diff, du and ls, and their universes lack its 88 branches (4,281,
  7,243 and 4,353 instead of 4,369, 7,331 and 4,441).
* Python is the distribution's 3.10; the runner and these scripts must stay compatible
  with it.

Build and check:

```
docker build --target toolchain -t agent-software-testing:toolchain -f environment/Dockerfile .
docker run --rm agent-software-testing:toolchain python3 /opt/agent-software-testing/environment/verify_image.py
```

TLS verification is never disabled. The base image has no certificate bundle, so the first
`apt-get` calls use the image's own plain-HTTP archive sources, whose indexes and packages
apt checks against the signing keys that ship in the image, and install only
`ca-certificates`; the sources are then pointed at the dated HTTPS snapshot. The snapshot's
apt calls set `Acquire::Check-Valid-Until=false` on their command line (an old snapshot's
indexes are past their Valid-Until date); nothing of that is written into the image.

## Third-party build inputs

Three small files that the builder needs for two programs (Krep and libmatheval) are kept in this repository, apart
from our own code, in [third_party/concollmic/](third_party/concollmic/README.md), together with the licence texts
that apply to them. They were written by the authors of ConcoLLMic and published in the artifact repository of that
project (the address, the commit and the digests of the upstream files are in the README of that directory). The
files here differ from the upstream files only in white space at the end of lines; whether `patch` and the compiler
give the same result for the upstream bytes has not been tested in a build. The artifact repository has no licence
file of its own, and the README of the directory says how the licence of the ConcoLLMic repository is applied to
the files.

**They are not under the Apache License 2.0 of the rest of this repository.** The licence of the ConcoLLMic
repository allows the GNU General Public License, version 3, for non-commercial use (academic use included) and
asks for a separate licence from the authors for commercial use; the Krep patches contain lines of Krep, which is
BSD 2-Clause. Read the README of the directory before you use or change the files. Nothing else in this repository
contains or modifies them: `build_programs.py` only reads them and checks their digest. The Python package is built
from `src/` only and does not carry the directory (`.gitattributes` also keeps git from normalising their line
endings or white space, which would change the digests).

The builder looks in this directory when `--assets` is not given, and `--assets DIR` replaces it (`DIR` has the
same layout, `<program>/<file>`). It refuses to build the affected programs unless each file has exactly the
pinned SHA-256 below; the same values are in `FILES` of `build_programs.py`, in the README of the directory and in
the tests, so a change to a file or to a digest in one place fails the tests.

| Program | File (below `third_party/concollmic/` or `--assets`) | SHA-256 | Purpose |
|---|---|---|---|
| krep | `krep/Makefile.diff` | `381cd3f517a7a802cbb0c1433b763a7369a833cb36212d26d79eaa60dfa2dea4` | make-only build, no host vectors |
| krep | `krep/krep.diff` | `e9d7296e6d4618eb95dd1c51e89d9ec07b085bd989e0a48c70b8d0852eb07b59` | sequential partitions instead of a thread pool |
| libmatheval | `libmatheval/test.c` | `3b1236126e160287cbafb30a7598bac8e8462d3d3a442549c80cf74c32443e55` | standard-input driver that is the measured binary |

The branch totals of Krep (1,231) and libmatheval (591) are those of builds made with exactly these bytes. Other
files give other universes, so replacing them is a change of the measurement (SPEC section 10). The jq shim
(`programs/jq/compat/jq-single-process.c`) is part of this repository and is pinned by digest as well.

## Building the programs

From a checkout (`--assets` is optional: the files of `third_party/concollmic/` are used):

```
python3 environment/build_programs.py --all
python3 environment/build_programs.py --dry-run krep    # print the commands only
python3 environment/build_programs.py --verify-definitions
```

The `toolchain` image holds the script, not the `third_party/` directory. Inside it, name the directory for Krep and
libmatheval, for example from a mounted checkout:
`python3 /opt/agent-software-testing/environment/build_programs.py --all --assets /path/to/environment/third_party/concollmic`.

At image build time the `builds` target copies the directory into the image and passes it to the builder:
`docker build --target builds -t agent-software-testing:builds -f environment/Dockerfile .`
(`--build-context assets=<dir>` with `docker buildx build` replaces the files by another directory).

Useful options: `--definitions DIR` (program definitions; default `../programs`), `--variant coverage|sanitizer|both`, `--jobs N`, `--offline` (every archive
must already be in `--cache`, default `<out>/sources`), `--sanitizer-cc`, `--sanitizer-cxx`.
Archives are fetched over HTTPS only and verified against `source.sha256` of the program
definition (and, for the extra Ogg, Vorbis and Brotli sources, against the digests in the
script). A cached file with the wrong digest is an error and is never replaced. A build
directory that already exists is never replaced either: remove it yourself and rerun.

### Output layout

```
<out>/                          default /opt/agent-software-testing/builds ($AGENT_SOFTWARE_TESTING_BUILDS)
  sources/                      archive cache
  <program id>/
    gcov/                       coverage build root: the --build argument of the runner
      obj-gcov/...              objects, .gcno files, the binary named by program.json "binary"
    asan-ubsan/                 sanitiser build root
      obj-native/...            the binary is "binary" with obj-gcov replaced by obj-native
    meta/
      gcov-build.json           build record (schema agent-software-testing/build@0.1)
      gcov-build.log
      asan-ubsan-build.json
      asan-ubsan-build.log
```

The source tree is extracted into the build root next to `obj-gcov/`, except for the
in-tree programs oggenc and woff2, whose source tree is `obj-gcov/` itself (the build
record's `source_root` says which). **A build must stay at the absolute path it was
compiled at**, because GCC writes absolute object paths into the coverage notes; mount or
copy the image's `/opt/agent-software-testing/builds` unchanged.

### What the builds do

Coverage builds use `gcc` with `-g -O0 -fprofile-arcs -ftest-coverage` (which is
`--coverage`) and the matching linker flags, configured out of tree in `obj-gcov/` with the
configure arguments of the program definition. The sanitiser builds use `clang-15` with
`-g -O1 -fno-omit-frame-pointer -fno-optimize-sibling-calls -fsanitize=address,undefined
-fno-sanitize-recover=all`. Per-program quirks, carried over from the reference build
driver, are in the `RECIPES` table of `build_programs.py`:

| Program | Quirk |
|---|---|
| bc | make targets `lib`, `bc`; one job; `MAKEINFO=true` |
| gcal | `lib` and `src` targets only; `CHECK_CFLAGS`/`CHECK_LIBS` preset to bypass the test-framework probe |
| gzip | `gl_cv_crc_pclmul=no` (portable CRC); `gl_cv_func_printf_enomem=yes`: gnulib's out-of-memory probe of `printf` aborts under Rosetta ("rosetta error: mmap_anonymous_rw mmap failed"), configure then compiles the floating-point code of `lib/vasnprintf.c` into gzip (4,009 branches instead of 3,083); natively the probe answers yes, and the preset makes the build independent of the host |
| jq | shim object compiled without coverage flags and linked through `LIBS` |
| krep | no configure; two patches; `make -f ../Makefile VPATH=..`; the patched Makefile appends `-O1` after the environment's `-O0` |
| bc, gawk, jq | the generated grammar and scanner sources (`bc/bc.c`, `bc/scan.c`; `awkgram.c`, `command.c`; `src/lexer.c`, `src/parser.c`, with their `.y`/`.l` files) are copied to the `obj-gcov/` paths GCC recorded (`build.stage_sources` of their definitions), as for libmatheval; without the copies the initial lcov capture prints `could not open` warnings and the runner refuses it |
| xmlwf | `build.exclusion_markers` is `ignored`: lcov runs with `--no-markers`, so the eight branches Expat marks `LCOV_EXCL_START/STOP` count (6,293; with the markers 6,285). Confetti is the opposite case: its published 632 branches honour the markers (636 without) |
| libmatheval | `make -C lib`, then the driver is compiled with the coverage flags and linked statically into `obj-gcov/program`; afterwards the generated parser and scanner sources are copied to the paths GCC recorded (`build.stage_sources` of its definition) |
| confetti | `autoreconf -fvi` only if the archive ships no `configure` |
| oggenc | in-tree, `./autogen.sh` then `configure`; coverage build links the image's libogg and libvorbis; sanitiser build first builds pinned libogg and libvorbis statically with the sanitiser flags and fails if the binary still links them dynamically |
| woff2 | in-tree `make all` with the pinned Brotli unpacked in `brotli/`; flags go in through the environment for coverage and as make variables for the sanitiser build |

### Deviations from the reference build driver

* One combined `address,undefined` sanitiser build per program. The reference driver built
  ASan and UBSan separately. A case that triggers both kinds of error reports only the
  first one here.
* The define in gcal's preset `CHECK_CFLAGS` is `AGENT_SOFTWARE_TESTING_GCAL_NO_TEST_SUITE`, not the
  reference name. No gcal source refers to either.
* libmatheval is configured with the same arguments as the reference build hook
  (`--disable-nls --disable-shared --enable-static`). Its configure script does not know
  `--disable-nls`: it prints `configure: WARNING: unrecognized options: --disable-nls` and
  ignores the option. The builder stops on
  such a warning, because an ignored option means that a build differs from its definition, but
  tolerates this one option for this program (`ignored_configure_options` in its recipe, which
  `--verify-definitions` requires to be among `build.configure`). The definition keeps the option,
  as the reference hook had it; the universe of the build is the published one (591 keys), so the
  option changes nothing. Any other unrecognised option still stops the build.
* Downloads use Python's `urllib` instead of `curl`.
* There is no bitcode variant.

## Acceptance procedure for a new image

1. `verify_image.py` reports no mismatch.
2. `build_programs.py --verify-definitions` reports no problem.
3. All 18 coverage builds and sanitiser builds complete.
4. For each program, step 3 of the replay protocol (`lcov --capture --initial` over the
   notes the binary links) yields exactly `branch_total` branches. A different number means
   the image or a build differs from the one the totals were measured on; do not publish
   results from that image.
5. Each sanitiser binary runs on a trivial input of its fixed track without a sanitiser
   start-up error.
6. Record the base image digest, `dpkg` entry count and checksum in `toolchain.lock.json`
   and set its `status`.
