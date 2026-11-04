# Programs

Each directory holds one `program.json` (schema `agent-software-testing/program@0.1`, defined in
[SPEC.md](../SPEC.md) section 4). The 18 programs are those of the reference evaluation:
command-line programs and parser front ends written in C and C++, between 600 and
55,000 measured branches.

| id | Program | Version | Branch total | Fixed track | Inputs | Status |
|---|---|---|---:|---|---|---|
| bc | GNU bc | 1.08.2 | 1,922 | none | argv, files, stdin, env | open-track-only |
| confetti | Confetti (parse example) | 1.0.0-beta.4 | 632 | stdin, no arguments | stdin | measured |
| diff | GNU diff | 3.12 | 4,369 | none | argv, files, stdin, env | open-track-only |
| du | GNU du | 9.11 | 7,331 | none | argv, files, stdin, env | open-track-only |
| gawk | GNU Awk (gawk) | 5.4.1 | 20,356 | none | argv, files, stdin, env | open-track-only |
| gcal | GNU Gcal | 4.2.0 | 15,632 | none | argv, files, stdin, env | open-track-only |
| grep | GNU grep | 3.12 | 4,806 | none | argv, files, stdin, env | open-track-only |
| gzip | GNU gzip | 1.14 | 3,083 | none | argv, files, stdin, env | open-track-only |
| jq | jq | 1.8.2 | 18,280 | none | argv, files, stdin, env | open-track-only |
| krep | Krep | 1.2 | 1,231 | `test {input}` (file) | argv, files | measured |
| libmatheval | GNU libmatheval (stdin driver) | 1.1.11 | 591 | stdin, no arguments | stdin | measured |
| libyaml | LibYAML (run-parser-test-suite) | 0.2.5 | 4,275 | stdin, no arguments | stdin | measured |
| ls | GNU ls | 9.11 | 4,441 | none | argv, files, stdin, env | open-track-only |
| oggenc | oggenc (vorbis-tools) | 1.4.3 | 1,138 | `{input}` (file) | argv, files | measured |
| sed | GNU sed | 4.10 | 6,958 | none | argv, files, stdin, env | open-track-only |
| woff2 | woff2_decompress (WOFF2) | 1.0.2 | 11,412 | `{input}` (file) | files | measured |
| xmlwf | xmlwf (Expat) | 2.8.2 | 6,293 | none | argv, files, stdin, env | open-track-only |
| xorriso | GNU xorriso | 1.5.8.pl02 | 54,607 | none | argv, files, stdin, env | open-track-only |

Branch totals are the size of each program's branch universe in the pinned coverage build
(SPEC section 3.3). A replay whose universe differs from `branch_total` is invalid.

The version is the upstream version string of the source. Krep, oggenc and WOFF2 are pinned to a
commit instead of a release; for them it is the version the pinned revision declares in its own
sources (`VERSION` in `krep.c`, `AC_INIT` of vorbis-tools, `WOFF2_VERSION` of WOFF2), and the
commit is the one in the source archive below. WOFF2's build also pins a Brotli commit
(`533843e3546c`, see `build.notes`).

## Tracks

* **Fixed track.** The program has an established harness invocation. A tool supplies only
  the bytes that invocation consumes, as the file `input` or as standard input. Six programs
  define one: Confetti, libmatheval and LibYAML read standard input and take no arguments; Krep
  runs `krep test <file>`; oggenc and WOFF2 take the file as their only argument. These
  invocations are those of the evaluation's harnesses and fuzzer launcher. (The reference replay
  pipeline gave LibYAML the file as an argument; that the two ways of delivering the input cover
  the same branches has not been measured.)
* **Open track.** The tool chooses command-line arguments, files, standard input and
  environment. Every program supports it. The other twelve programs have no fixed track:
  their natural invocation depends on arguments, and no single harness invocation has been
  agreed (`fixed_track` is `null`, `status` is `open-track-only`).

## Program-specific notes

* **libmatheval** is measured through a small standard-input driver, not the library alone.
  GCC records its generated parser and scanner sources under `obj-gcov/lib`, although they
  exist under `lib/`; `build.stage_sources` in its definition lists the four copies that put
  them where the compiler recorded them. The builder makes the copies after the build and the
  replay runner repeats them (a no-op on a staged build).
* **oggenc** and **woff2** are built in tree: their source tree is `obj-gcov` itself.
  Branch keys are relative to the coverage build root (SPEC section 3.1), so their file
  names start with `obj-gcov/`, for example `obj-gcov/oggenc/audio.c`. Generated sources of
  out-of-tree programs behave alike (jq's `obj-gcov/src/lexer.c`), and branches in system
  headers keep their absolute paths (WOFF2 and Krep report some in `/usr/include/...`).
* **oggenc**'s coverage build links the image's libogg and libvorbis; coverage is therefore
  measured in vorbis-tools only. Its sanitiser build links pinned, sanitised copies of both
  libraries.
* **du** and **ls** are built from the same GNU coreutils archive, separately.
* **krep** and **libmatheval** need three third-party build inputs (two patches and a driver), which
  are in [environment/third_party/concollmic/](../environment/third_party/concollmic/README.md) under
  their own licences; see [environment/README.md](../environment/README.md).

Build recipes (make targets, environment, patches) live in
[environment/build_programs.py](../environment/build_programs.py); `build.configure` and
`build.notes` in each `program.json` summarise them, and
`build_programs.py --verify-definitions` checks that the two agree.

## Source archives

| id | Source archive |
|---|---|
| bc | <https://ftp.gnu.org/gnu/bc/bc-1.08.2.tar.gz> |
| confetti | <https://github.com/hgs3/confetti/releases/download/v1.0.0-beta.4/confetti-1.0.0-beta.4.tar.gz> |
| diff | <https://ftp.gnu.org/gnu/diffutils/diffutils-3.12.tar.xz> |
| du | <https://ftp.gnu.org/gnu/coreutils/coreutils-9.11.tar.xz> |
| gawk | <https://ftp.gnu.org/gnu/gawk/gawk-5.4.1.tar.xz> |
| gcal | <https://www.alteholz.dev/gnu/gcal-4.2.0.tar.xz> |
| grep | <https://ftp.gnu.org/gnu/grep/grep-3.12.tar.xz> |
| gzip | <https://ftp.gnu.org/gnu/gzip/gzip-1.14.tar.xz> |
| jq | <https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-1.8.2.tar.gz> |
| krep | <https://github.com/davidesantangelo/krep/archive/9c4d41ce92f4e9ef6dc786eaf0e5be488153c4b4.tar.gz> |
| libmatheval | <https://ftp.gnu.org/gnu/libmatheval/libmatheval-1.1.11.tar.gz> |
| libyaml | <https://github.com/yaml/libyaml/releases/download/0.2.5/yaml-0.2.5.tar.gz> |
| ls | <https://ftp.gnu.org/gnu/coreutils/coreutils-9.11.tar.xz> |
| oggenc | <https://github.com/xiph/vorbis-tools/archive/235540c05ad3f9cdc673ca09c237c9a9b5bda6eb.tar.gz> |
| sed | <https://ftp.gnu.org/gnu/sed/sed-4.10.tar.xz> |
| woff2 | <https://github.com/google/woff2/archive/0f4d304faa1c62994536dc73510305c7357da8d4.tar.gz> |
| xmlwf | <https://github.com/libexpat/libexpat/releases/download/R_2_8_2/expat-2.8.2.tar.xz> |
| xorriso | <https://ftp.gnu.org/gnu/xorriso/xorriso-1.5.8.pl02.tar.gz> |

All archives are fetched over HTTPS and verified against the SHA-256 in `program.json`.
Oggenc's sanitiser build and WOFF2 additionally fetch the Ogg, Vorbis and Brotli sources
pinned in `build_programs.py`.

## Adding a program

A new program needs: a pinned source archive (`url`, `sha256`, `directory`); a coverage
build and an ASan+UBSan build that the builder can produce without network access beyond
the pinned archives; a branch total measured by two independent universe captures that
agree; a `binary` path below `obj-gcov/`; and, if a fixed track is claimed, a harness
invocation that does not depend on the host. Adding a program is a minor version change of
the programs set (SPEC section 10) and does not change existing branch totals.
