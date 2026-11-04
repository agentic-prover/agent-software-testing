# Agent Software Testing specification

Version 0.1. The data formats carry the identifiers `@0.1`.

This document defines how test cases produced by any test generator are submitted, replayed
on one pinned coverage build of each program, and reported. It is normative except where a
section is marked informative. The keywords MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are
used as in RFC 2119 and RFC 8174.

Machine-readable forms of the data formats are in `schemas/`. They check structure only;
the rules in this document that need the file system, decoded values or several records at
once (hashes, limits, ordering, track rules) are checked by `python -m agent_software_testing
validate`. Where a schema and this text differ, this text governs and the schema is wrong.

## Contents

1. Purpose and scope
2. Conventions and terms
3. Measurement
4. Programs and builds
5. Test-case bundle
6. Replay protocol
7. Outputs
8. Sanitiser pass and the counts-only rule
9. Statistics and reporting
10. Versioning
11. Known limitations
- Appendix A. Worked example

---

## 1. Purpose and scope

Test generators that produce whole-program test cases include symbolic executors, coverage
guided fuzzers, hybrids of both, systems driven by language models and agents that plan
campaigns. Each is usually evaluated with its own coverage measurement, on its own builds
and with its own treatment of arguments, files and standard input. Their numbers are
therefore not comparable.

Agent Software Testing fixes the measurement. A tool runs its campaign wherever and however it likes
and hands in the test cases it produced, with the time each was found. A runner replays
every case on one pinned gcov build of the program, in one pinned image, and reports branch
coverage and sanitiser findings in the same way for every tool.

**In scope.** Command-line programs and parser front ends whose behaviour is determined by
arguments, files in the working directory, standard input and environment variables. A test
case is the complete description of one program execution (section 5).

**Out of scope.** Libraries tested through an in-process harness; coverage of the test
generator itself; execution time, memory use or any other property of a replayed program;
ranking tools (section 9.5); judging whether a sanitiser finding is a genuine defect.

**Roles.**

* A *submitter* produces bundles (section 5) from campaigns of a tool.
* A *runner* replays bundles (section 6). The reference runner is `python -m agent_software_testing`.
  Another implementation conforms if it follows sections 3 to 8.
* A *publisher* assembles replay outputs into a results release (sections 7.5 and 10).

## 2. Conventions and terms

**Data formats.** JSON is RFC 8259, encoded as UTF-8 without a byte order mark. Objects
MUST NOT contain duplicate member names, and the constants `NaN`, `Infinity` and
`-Infinity` are not JSON. A member written as an integer MUST be a JSON number without
fraction or exponent. A reader MAY reject a number of more than 40 characters, and the
reference validator does. A SHA-256 digest is written as 64 lowercase
hexadecimal digits. *Base64* is the standard alphabet of RFC 4648 section 4 with padding and
without line breaks, and MUST be canonical: decoding and re-encoding gives the same text, so
a value with non-zero padding bits is invalid. Timestamps, where present, are UTC in the
form `YYYY-MM-DDThh:mm:ssZ`. Paths inside bundles use `/` as separator.

**Terms.**

| Term | Meaning |
|---|---|
| program | One of the programs in `programs/` (section 4). Identified by `id`. |
| coverage build | The program compiled with `gcc` and gcov instrumentation in the pinned image. |
| sanitiser build | The program compiled with AddressSanitizer and UndefinedBehaviorSanitizer. |
| build root | The directory of a build that contains the source tree and `obj-gcov/` (or `obj-native/`). |
| compile root | The absolute path at which a coverage build was compiled. Normally its build root. |
| tool | The test generator under evaluation, in one configuration and version. |
| campaign | One execution of a tool on one program with a time budget. |
| repeat | The index (1, 2, ...) of an independent campaign of the same tool, program, track and configuration. |
| case | One execution of the program: arguments, files, standard input, environment. |
| bundle | The directory a submitter hands in: `tool.json`, `cases.jsonl`, `blobs/`. |
| track | `fixed` or `open` (section 4.2). |
| budget | The wall-clock time the campaign was allowed, `budget.wall_s`. |
| universe | The set of all branches of the coverage build (section 3.3). |
| snapshot | The cumulative coverage after replaying all cases found up to a given time. |
| shard | One worker of the runner with its own private copy of the build tree. |

## 3. Measurement

### 3.1 Branch identity

A branch is identified by the key

```
file:line:block_id:branch_id
```

`line`, `block_id` and `branch_id` are decimal integers without sign or leading zeros
(except `0` itself); a validator rejects a key in which one of them has a leading zero. `file` is a `/`-separated path. A key is parsed from the right: the
last three `:`-separated fields are the integers and the rest is `file`, so a file name
may contain `:`.

The keys come from the lcov tracefile of the coverage build. The tracefile is read as
follows, which is the behaviour of the measurement that produced the reference branch
totals.

1. A record begins with `SF:<path>` and ends with `end_of_record`. Lines outside a record
   and `TN:` lines are ignored. Records with identical `SF` text are merged first: their
   `DA` counts and their `BRDA` counts per `(line, block, branch)` are added.
2. Within a record, `DA:<line>,<count>[,...]` declares an instrumented line.
   `BRDA:<line>,<block>,<branch>,<taken>` declares one branch; `<taken>` is `-` (the block
   was never executed, counted as 0) or a non-negative number.
3. A branch is **counted only on a line that has a `DA` line in the same (merged) record**.
   `BRDA` lines on other lines are ignored. Several `BRDA` lines with the same
   `(line, block, branch)` are one branch whose count is their sum.
4. A branch is *covered* when its count is greater than zero.
5. The `SF` path is normalised. An absolute path whose resolved form (symbolic links
   followed) lies below the resolved compile root is replaced by its path relative to the
   compile root. Any other absolute path that has a component equal to the last component
   of the compile root path is replaced by the part after the last such component, if there
   is one (a rule for build trees measured under another mount point). Any other absolute
   path and every relative path is kept, but written as a plain POSIX path: `.` components
   and repeated separators are removed (`./a.c` becomes `a.c`, `a//b.c` becomes `a/b.c`) and
   nothing else is resolved. If two different `SF` texts normalise to the same file name the
   runner MUST fail, because the identity would be ambiguous.

Consequences. Keys are relative to the **build root**, not to the directory of the source
archive: for programs built in tree (oggenc, WOFF2) the file names start with `obj-gcov/`;
generated sources of out-of-tree builds appear under `obj-gcov/` as well (for example
`obj-gcov/src/lexer.c` for jq); branches in system headers that gcov reports keep their
absolute path (for example `/usr/include/c++/11/bits/basic_string.h`). No file is excluded
from the universe or from the counts.

Block and branch numbers depend on the compiler version and flags. Keys are comparable only
between measurements on the pinned image.

### 3.2 Branch-set document

A set of covered branches is exchanged as a JSON object:

```json
{"schema": "agent-software-testing/branch-set@0.1", "program": "xmlwf",
 "branch_key_format": "file:line:block_id:branch_id",
 "branch_total": 6293, "branch_covered": 1349,
 "covered_branch_keys": ["lib/siphash.h:170:0:0", "..."]}
```

| Member | Rule |
|---|---|
| `schema` | The string `agent-software-testing/branch-set@0.1`. |
| `program` | A program id. |
| `branch_key_format` | The string `file:line:block_id:branch_id`. |
| `branch_total` | The size of the universe of the program (section 3.3). |
| `branch_covered` | The number of elements of `covered_branch_keys`. |
| `covered_branch_keys` | Array of keys, unique and sorted ascending by Unicode code point of the key string. Every key MUST belong to the universe, so `branch_covered <= branch_total`. |
| `t_s`, `cases` | Only in snapshot documents (section 7.3): the snapshot time in seconds and the number of cases replayed up to it. |

Branch-set documents of other measurements (the `measure` values `canonical`,
`standard` and `common-observer` of the results repository) use the same key format under
another schema id and may carry further members such as `method` or `repeat`; readers of the
results repository accept them.

### 3.3 Universe

The universe of a program is the set of branches that exist in its coverage build,
covered or not. A runner determines it as follows before replaying anything.

1. Stage the generated sources listed in `build.stage_sources` of the program definition
   (section 4.3), if any. The build tree MUST contain no `.gcda` file; otherwise the runner
   fails.
2. Find the objects linked into the program binary. GCC embeds the path of the `.gcda` file
   of every instrumented object in the binary. Take every run of at least four printable
   ASCII characters in the binary that ends in `.gcda` and contains the compile root path
   followed by `/`; the part after it, with the suffix replaced by `.gcno`, names a notes
   file. The binary MUST name at least one, and every named file MUST exist in the build
   tree. A name that is absolute or contains `..` is an error.
3. Make a private copy of the build tree and delete from it every `.gcno` file that is not
   named in step 2. The copy is made of real files; nothing in it is a hard link to the
   build.
4. Run `lcov --capture --initial --directory <copy> --rc lcov_branch_coverage=1
   --output-file <info>`, with `--no-markers` added when the program definition says
   `build.exclusion_markers` is `ignored` (section 4.3). The run MUST succeed, MUST write a non-empty file and MUST NOT
   print a line that contains the word `warning:` (the text `warning:`, in any case, at the
   start of the line or after a character that is not a letter, digit or underscore);
   otherwise the runner fails. The run has a time limit (the reference runner: one hour);
   exceeding it is a failure.
5. Read the tracefile as in section 3.1. The universe is the set of keys of all counted
   branches, covered or not. It MUST be non-empty, MUST contain no covered branch, and MUST
   have exactly `branch_total` elements; otherwise the runner fails with a universe
   mismatch.

### 3.4 Coverage data

Cases run with `GCOV_PREFIX` set to the shard's *profile tree* and `GCOV_PREFIX_STRIP` set to
the number of components of the compile root path (the root directory `/` is not counted).
GCC then writes each `.gcda` file at the path of the corresponding `.gcno` file relative to
the compile root, below the profile tree. The profile tree is a directory of the shard that
is separate from the shard's copy of the build (section 6.2, step 4), and it is the only
part of the shard that the program under test can write. The data of a process is written
when the process exits through `exit` or by returning from `main` (section 11.1).

Before a capture the runner examines the profile tree. It MUST contain nothing but regular
files whose paths are the `.gcda` names derived from the `.gcno` files named in section 3.3
step 2 (the same relative path with the suffix `.gcda`), at most 64 MiB each, and the
directories on the way to them. A symbolic link, a pipe, a directory or file with any other
name, or an oversized file is an error and the runner fails: it does not drop the file and
go on. The runner then copies the files next to the `.gcno` files of the shard's build copy
(which it can write; the program is not running), runs `lcov --capture --directory . --output-file
<info> --rc lcov_branch_coverage=1` (with `--no-markers` under the same condition) in that copy, removes the copies, reads the tracefile as
in section 3.1 and yields the covered keys of that shard. A capture that fails, exceeds
its time limit, writes nothing, contains no branch records or reports damaged profile data
is an error and is never read as coverage, low or zero. At most `min(shards, 8)` captures run at once in the reference runner. The
*cumulative set* is the union of the covered keys of all shards and of the cumulative set of
the previous snapshot. Every key in it MUST be in the universe; otherwise the runner fails
(the denominator changed).

The check cannot tell a genuine profile file from one a program wrote on purpose
(section 11.14).

## 4. Programs and builds

### 4.1 Program definition

Each program has `programs/<id>/program.json`. Schema id `agent-software-testing/program@0.1`.

```json
{"schema": "agent-software-testing/program@0.1", "id": "krep", "name": "Krep", "version": "...",
 "source": {"url": "...", "sha256": "...", "directory": "..."},
 "build": {"configure": ["..."], "notes": "..."},
 "binary": "obj-gcov/krep",
 "branch_total": 1231,
 "fixed_track": {"argv_template": ["test", "{input}"], "input_channel": "file"},
 "inputs": ["argv", "files"], "status": "measured"}
```

| Member | Type | Rule |
|---|---|---|
| `schema` | string | `agent-software-testing/program@0.1` |
| `id` | string | `[a-z0-9][a-z0-9_-]*`; equals the name of the directory. Used in bundles, outputs and the results repository. |
| `name` | string | Display name, non-empty. |
| `version` | string | The upstream version string of the source, non-empty, in the form upstream writes it (`1.14`, `0.2.5`, `1.0.0-beta.4`, `1.5.8.pl02`). For a source pinned to a revision instead of a release (Krep, oggenc, WOFF2) it is the version that revision declares in its own sources; the revision itself is identified by `source.url` and `source.sha256`. It never holds a commit hash. Informative. |
| `source.url` | string | URL of the pinned source archive (HTTPS in this repository). |
| `source.sha256` | string | SHA-256 of that archive. A bundle names it in `program.source_sha256` (section 5.2), which binds the bundle to one program revision. |
| `source.directory` | string | The single top-level directory of the archive. |
| `build.configure` | array of strings | Arguments passed to `configure`; empty for programs without a configure step. Required. |
| `build.notes` | string | Prose summary of build steps the other members do not express. Optional. The build recipe of the reference builder (`environment/build_programs.py`) is authoritative. |
| `build.stage_sources` | array | Optional. Entries `{"source", "recorded_path"}`, both paths relative to the build root. Section 4.3. |
| `build.exclusion_markers` | string | Optional, `honored` (the default) or `ignored`. With `ignored` lcov runs with `--no-markers`, so branches inside `LCOV_EXCL_*` blocks of the source belong to the universe. Section 4.3. |
| `binary` | string | Path of the program binary relative to the coverage build root: relative, without `..`; in this repository always below `obj-gcov/`. |
| `branch_total` | integer | The size of the universe (section 3.3), positive. |
| `fixed_track` | object or null | Section 4.2. `null` when the program has no fixed invocation. Required (the member may not be omitted). |
| `inputs` | array | The input surfaces the program definition exposes: a non-empty list of distinct values from `argv`, `files`, `stdin`, `env`. Informative: validators SHOULD warn, and MUST NOT reject, an open-track case that uses a surface not listed. |
| `status` | string | `measured` when `fixed_track` is not null, `open-track-only` otherwise. Informative. |

Readers of program definitions, the validator and the runner among them, ignore members they
do not know; the definitions in this repository contain exactly the members above. The
reference builder (`environment/build_programs.py`) is stricter on purpose: it rejects a
definition with an unknown member, because an unknown member could be a build instruction
it would otherwise skip without a word.

### 4.2 Tracks

The **fixed track** tests a tool's ability to find inputs for a given invocation. The
invocation is part of the program definition; the tool supplies only the bytes that
invocation consumes.

```json
"fixed_track": {"argv_template": ["test", "{input}"], "input_channel": "file"}
```

* `argv_template` is an array of strings. The element `{input}` stands for the path of the
  input file.
* `input_channel` is `file` (the template contains `{input}` and the case supplies one file
  named `input`) or `stdin` (the template contains no `{input}` and the case supplies
  standard input).

The **open track** lets the tool choose the command-line arguments, the files in the
working directory, standard input and the environment. Every program supports the open
track. A tool may submit bundles for either track or both; results of different tracks are
never combined.

Six programs have a fixed track, taken from the harness invocations of the reference
evaluation: Confetti, libmatheval and LibYAML (standard input, no arguments), Krep
(`test {input}`), oggenc and WOFF2 (`{input}`). The other twelve programs are open-track
only. (The reference replay pipeline passed LibYAML the input as a file argument; the harness
of the evaluation and the fuzzer launcher read standard input. Whether the two ways of
delivering the input cover the same branches has not been measured; section 11.17 lists it
among the remaining differences.)

### 4.3 Builds

For every program there are exactly two builds, made by `environment/build_programs.py` in
the pinned image (`environment/`). Submitters never supply binaries.

| Build | Compiler and flags | Build root |
|---|---|---|
| coverage | `gcc` 11.4, `-g -O0 -fprofile-arcs -ftest-coverage`, matching link flags | `/opt/agent-software-testing/builds/<id>/gcov` |
| sanitiser | `clang` 15, `-g -O1 -fno-omit-frame-pointer -fno-optimize-sibling-calls -fsanitize=address,undefined -fno-sanitize-recover=all` | `/opt/agent-software-testing/builds/<id>/asan-ubsan` |

* Build commands, per-program configure options, patches and driver sources are those of
  the build recipes. Programs whose source needs a driver (libmatheval) are measured through
  that driver.
* GCC records absolute object paths in the coverage notes and embeds them in the binary. A
  coverage build is used at the path it was compiled at. A tree that was moved is used by
  telling the runner its compile root; the runner then looks for the embedded paths below
  that root (section 3.3 step 2), which fails if it is wrong.
* The program binary of the sanitiser build is `binary` with the leading `obj-gcov/`
  replaced by `obj-native/`, relative to its build root.
* **Generated sources.** GCC records a generated source at the location where the build
  wrote it, which can differ from where the file exists afterwards. `build.stage_sources`
  lists such copies: for each entry the file `source` is copied to `recorded_path`, both
  relative to the build root. A destination that already holds identical content is left
  alone; a missing source, or a destination with other content, is a failure. The builder
  makes these copies after the build and the runner repeats them, so a staged build is not
  changed by a replay. Four programs have entries: libmatheval (`lib/parser.c`, `lib/parser.y`,
  `lib/scanner.c`, `lib/scanner.l`, recorded under `obj-gcov/lib/`), bc (`bc/bc.c`, `bc/bc.y`,
  `bc/scan.c`, `bc/scan.l`, under `obj-gcov/bc/`), gawk (`awkgram.c`, `awkgram.y`, `command.c`,
  `command.y`, under `obj-gcov/`) and jq (`src/lexer.c`, `src/lexer.l`, `src/parser.c`,
  `src/parser.y`, under `obj-gcov/src/`). Without them the initial capture prints
  `could not open` warnings and the runner refuses it (section 3.3 step 4).
* **Exclusion markers.** lcov removes branches inside `LCOV_EXCL_START`/`LCOV_EXCL_STOP` blocks and
  lines marked `LCOV_EXCL_BR_LINE` unless it runs with `--no-markers`. The published universes follow
  both rules: Confetti's 632 branches leave out the four its sources mark (636 without), xmlwf's 6,293
  count the eight inside Expat's blocks (6,285 with the markers). `build.exclusion_markers` records the
  rule of each program; only xmlwf is `ignored`.

### 4.4 Build records

`build_programs.py` writes `<out>/<id>/meta/<variant>-build.json` for each build (variant
`gcov` or `asan-ubsan`), schema `agent-software-testing/build@0.1`, with at least: `program`,
`variant`, `status` (`complete`, `failed` or `incomplete`), `build_root` (absolute),
`binary`, `binary_sha256`, `program_sha256` (SHA-256 of `program.json`), `source_sha256`,
`gcno_files` (coverage build), `image` (image identity or null) and the executed steps. A
publisher MUST check that the builds used for a results release have `status = complete`
and record their `binary_sha256` values. The reference runner takes the build root from its
command line and does not read build records in v0.1.

## 5. Test-case bundle

### 5.1 Layout

```
bundle/
  tool.json       metadata (section 5.2)
  cases.jsonl     one case per line, ordered by t_ms (sections 5.3 and 5.4)
  blobs/<sha256>  content-addressed bytes: standard input and file bodies
```

A bundle is for one tool, one program, one track and one repeat. It contains nothing
else, and `blobs/` contains only regular files named by their SHA-256; a validator warns about
anything else (section 5.8). `cases.jsonl` MAY be empty; the result is then zero coverage and
a valid output.

### 5.2 tool.json

```json
{"schema": "agent-software-testing/tool@0.1",
 "tool": {"name": "AFL++", "version": "5.02c", "family": "fuzzer", "model": null,
          "config_sha256": null, "url": null},
 "program": {"id": "krep", "source_sha256": "<sha256 of the pinned source archive>"},
 "track": "fixed", "repeat": 1,
 "budget": {"wall_s": 21600, "cores": 1, "notes": ""},
 "resources": {"output_tokens": null, "input_tokens": null, "cpu_s": null},
 "notes": ""}
```

Required members are `schema`, `tool` (with `name` and `family`), `program` (with `id` and
`source_sha256`), `track`, `repeat` and `budget` (with `wall_s`). All others are optional and
absent means unknown. A writer SHOULD give every member.

| Member | Rule |
|---|---|
| `schema` | `agent-software-testing/tool@0.1` |
| `tool.name` | Non-empty string, at most 200 characters. Together with `version`, `model` and `config_sha256` it identifies the system in published results; two configurations of one tool MUST differ in at least one of them. |
| `tool.version` | String or null. |
| `tool.family` | One of `symbolic`, `fuzzer`, `hybrid`, `llm`, `agent`, `other`. |
| `tool.model` | String or null. The language model identifier for `llm` and `agent` tools and hybrids that use one; SHOULD be given when a model is used. |
| `tool.config_sha256` | SHA-256 of the submitter's configuration file or command line, or null. |
| `tool.url` | String or null. |
| `program.id` | The id of an existing program. |
| `program.source_sha256` | MUST equal `source.sha256` of that program definition. |
| `track` | `fixed` or `open`. |
| `repeat` | Integer at least 1. |
| `budget.wall_s` | Integer from 1 to 604,800 (seven days): the wall-clock time the campaign was allowed. The standard budget is 21,600 (six hours); other budgets are accepted and marked in results. A tool or adapter states it; none assumes it. |
| `budget.cores` | Integer at least 1: the number of CPU cores available to the tool during the campaign. Absent means not stated, and tables show it so. A writer, adapters included, writes it only when it knows it. |
| `budget.notes` | String (hardware, number of workers, anything that affects comparison). |
| `resources.output_tokens`, `resources.input_tokens` | Non-negative integer or null: total tokens exchanged with a language model over the campaign. |
| `resources.cpu_s` | Non-negative number or null: CPU seconds consumed by the tool. |
| `notes` | String. |

No members other than those listed are allowed in `tool.json` or in its nested objects.

### 5.3 cases.jsonl

The file is UTF-8 text with one JSON object per line, lines separated by a single `U+000A`,
no blank lines, the last line terminated. Cases MUST be ordered by non-decreasing `t_ms`;
cases with equal `t_ms` are replayed in file order. At most 1,000,000 cases, and a line is
at most 8 MiB (a valid case needs less than 3 MiB: its bodies are in `blobs/`). A reader
checks the length while it reads, so a file without line feeds is refused, not loaded.

### 5.4 Case fields

```json
{"id": "000123", "t_ms": 812340,
 "argv": ["LXE=", "aW5wdXQ="],
 "stdin": "sha256:<hex>",
 "files": [{"path": "aW5wdXQ=", "kind": "file", "mode": "0644", "blob": "sha256:<hex>"},
           {"path": "ZGly", "kind": "dir", "mode": "0755"},
           {"path": "bGluaw==", "kind": "symlink", "target": "aW5wdXQ="},
           {"path": "cGlwZQ==", "kind": "fifo", "mode": "0644", "blob": "sha256:<hex>"},
           {"path": "dHR5", "kind": "tty", "blob": "sha256:<hex>"}],
 "env": {"GZIP": "LTk="}, "cwd": "Lg==", "timeout_ms": 10000, "origin": "solver"}
```

`id`, `t_ms` and `argv` are required. The other members are optional and take the default
given in the table when absent; a bundle writer SHOULD write all of them. No other members
are allowed.

| Member | Rule |
|---|---|
| `id` | String matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, unique within the bundle. |
| `t_ms` | Integer at least 0: milliseconds from the start of the campaign to the moment the tool produced the case. Self-reported (section 11.9). |
| `argv` | Array of base64 strings: the arguments after `argv[0]`. The runner sets `argv[0]` itself (section 6.3). A decoded argument MUST NOT contain a NUL byte. |
| `stdin` | `null` (empty standard input; the default) or a blob reference `sha256:<hex>`. |
| `stdin_tty` | Boolean, `false` by default. With `true`, standard input is a pseudo-terminal that the runner feeds with the bytes of the `stdin` blob instead of a file (section 6.3 step 5); `stdin` MUST then be a blob (an empty blob is allowed). |
| `stdin_pipe` | Boolean, `false` by default. With `true`, standard input is an anonymous pipe that the runner feeds with the bytes of the `stdin` blob and then closes (section 6.3 step 5): the program cannot reposition descriptor 0, `fstat` reports a FIFO, and there is no `fd0` file; the end of the input follows the last byte. `stdin` MUST then be a blob (an empty blob is allowed), `stdin_tty` MUST NOT be `true`, and `stdin_atime` and `stdin_mtime` MUST be absent. |
| `stdin_atime`, `stdin_mtime` | Optional integers, seconds since the Unix epoch (from -2^63 to 2^63 - 1): the access and modification time of the standard input file. When present the runner sets them after the bytes are written, as it does for `atime` and `mtime` of a `file` entry, and a value the kernel or file system refuses leaves the time as created. Absent, the file has the times of its creation. `stdin` MUST be a blob, and `stdin_tty` and `stdin_pipe` MUST NOT be `true`: a pipe and a terminal have no times to set. |
| `files` | Array of file entries (below); empty by default. |
| `env` | Object mapping variable names to base64 values; empty by default. Names match `[A-Za-z_][A-Za-z0-9_]*` and are case-sensitive (`path` is not `PATH`). A decoded value MUST NOT contain NUL. Section 5.7 lists the names a case may not use. |
| `cwd` | Base64 of a relative path: the working directory of the process relative to the case root. `Lg==` (`.`) is the default and denotes the root. Otherwise it MUST be the path of a directory the case creates and the process can enter: that directory and every directory on the way to it has the owner's search (execute) permission bit in its `mode` (a directory that is only implied has mode `0700`). |
| `timeout_ms` | Integer from 1 to 30,000; default 30,000. |
| `origin` | String of at most 64 characters, or null; absent by default. Informative: how the case arose (for example `seed`, `solver`, `mutation`, `llm`). No part of a replay depends on it, nor on the name of the tool. |

A **file entry** has `path` (base64), `kind` and further members by kind:

| `kind` | Members | Created as |
|---|---|---|
| `file` | `blob` (required), `mode`, `atime`, `mtime` | Regular file with the bytes of the blob (an empty blob is a valid empty file). `atime` and `mtime` are optional integers, seconds since the Unix epoch (from -2^63 to 2^63 - 1); when present the runner sets them after the bytes are written (`utimensat`), and a value the kernel or file system refuses leaves the time as created. |
| `dir` | `mode` | Directory. |
| `symlink` | `target` (required, base64) | Symbolic link whose contents are the decoded target. No `mode`, no `blob`. |
| `fifo` | `blob` (required), `mode` | Named pipe. A writer thread of the runner waits for a reader to open it and writes the blob into it, then closes it. |
| `tty` | `blob` (required) | Symbolic link to the slave side of a pseudo-terminal that the runner opens for the case and feeds with the bytes of the blob (section 6.3 step 5). No `mode`. |

`mode` is a string of three octal digits, optionally preceded by a `0` (`644` and `0644` are
the same). The default is `0644` for files and named pipes and `0755` for directories.
Set-user-id, set-group-id and sticky bits are not expressible, so a four-digit mode with a
first digit other than `0` (`4755`, `1755`) is invalid. `path` and `target` are base64 of raw
bytes; they need not be valid UTF-8.

### 5.5 Materialisation of files

Every case runs in a fresh *case directory*; the files are created in its *case root*.
Entries are created component by component with no symbolic link followed, so nothing a
case lists can reach outside the case root. In this order:

1. Directories: every `dir` entry, and every parent directory of another entry that no
   entry creates (an *implied* directory, mode `0700`).
2. Regular files (with their times), named pipes and terminals, in listed order. A `tty` entry opens a
   pseudo-terminal and creates a symbolic link at its path whose target is the device of the
   slave side (the one place where a link may point outside the case root; the runner makes
   it, the case cannot choose the target).
3. Symbolic links, in listed order. A link may dangle.
4. The modes of the `dir` entries, deepest first, so that a restrictive directory mode
   does not stop files being created inside it.

Listing a path twice, creating below a symbolic link or below a non-directory, or a
`cwd` that is not a directory the case creates, is an error and the case is not run (status
`setup_error`). The validator rejects such bundles. A symbolic link target, resolved
lexically against the directory that contains the link, MUST stay inside the case root:
absolute targets and targets that climb above the case root are invalid.

### 5.6 Limits

| Item | Limit |
|---|---|
| Cases per bundle | 1,000,000 |
| Entries in `files` per case | 64 |
| Components per path | 64 |
| Bytes per path component | 255 |
| Bytes per path (decoded) | 1,024 |
| Path content | no absolute path, no empty, `.` or `..` component, no NUL |
| Bytes per blob | 32 MiB |
| Bytes per case (sum of the decoded sizes of `stdin`, every referenced file blob, `argv` and `env`) | 64 MiB |
| Entries in `argv` | 256 |
| Total decoded bytes of `argv` (the decoded arguments added up, without terminators) | 128 KiB (131,072) |
| Decoded bytes of one argument | 131,071 |
| Variables in `env` | 64 |
| One environment string `NAME=value`, decoded | 131,071 |
| `argv` and `env` together, every argument and every `NAME=value` counted with its terminating NUL | 1 MiB |
| Bytes per line of `cases.jsonl` | 8 MiB |
| `timeout_ms` | 1 to 30,000 (default 30,000) |

The per-string limit is the kernel's (`MAX_ARG_STRLEN` on Linux is 131,072 including the
terminator) and the combined limit stays well below the usual `ARG_MAX` of 2 MiB, which has
to hold the runner's own variables as well. A case within these limits does not fail to
start because its arguments are too long.

`cwd` is a path subject to the path rules, except that `.` is allowed.

### 5.7 Track rules

**Fixed track.** The program definition MUST have a `fixed_track`. For every case:

* if `input_channel` is `file`: `files` has exactly one entry, a regular file whose decoded
  path is `input`, `stdin` is `null`, and the decoded `argv` equals `argv_template` with
  `{input}` replaced by `input`;
* if `input_channel` is `stdin`: `files` is empty and the decoded `argv` equals
  `argv_template` (which contains no `{input}`); `stdin` is a blob or `null`;
* `env` is empty and `cwd` is `.`, `stdin_tty` and `stdin_pipe` are absent or `false`, and `stdin_atime`
  and `stdin_mtime` are absent.

**Open track.** None of these rules applies. The environment, files and arguments are the
tool's choice within the limits of section 5.6 and the reserved names below.

**Reserved environment names.** A case MUST NOT name `PATH`, `HOME`, `TMPDIR`,
`GLIBC_TUNABLES`, `GCONV_PATH`, `LOCPATH`, `NLSPATH`, `MSAN_OPTIONS` or
`TSAN_OPTIONS`, or any name beginning with `GCOV_`, `ASAN_`, `UBSAN_`, `LSAN_`, `LD_`,
`MALLOC_` or `AGENT_SOFTWARE_TESTING_`, nor any proxy variable (`http_proxy`, `https_proxy`, `ftp_proxy`,
`all_proxy`, `no_proxy`, `rsync_proxy`, in any case). No environment value may hold a URL with an
embedded user name and password (`scheme://user:password@host`); validators reject such a case and
adapters drop the variable. The runner sets `PATH`, `HOME` and `TMPDIR` itself, and
`AGENT_SOFTWARE_TESTING_CASE` (section 6.3); the others would change the measurement: the dynamic loader,
allocator, locale-loading and character-set variables can make a program load code from the case's
own files (`GCONV_PATH` makes `iconv` load a shared object from a directory the case controls) or
change how it allocates, and the sanitiser options change what a run reports.

A case MAY set the locale and the time zone: `LANG`, `LANGUAGE`, any `LC_*` variable and `TZ`.
A test generator can choose them on
purpose, `LANG=C` with `LC_CTYPE=C.UTF-8`, `LC_ALL=C` over a `LC_CTYPE` of `C.UTF-8`, or a zone such
as `NPT-5:45`, and the program then runs other code (the multibyte and the single-byte paths of
`sed`, `gawk`, `grep` and `diff`, the zone formatting of `du`); a replay that drops them measures a
different run. The base environment holds `LANG=C` and `TZ=UTC`, and the case's own variables take
effect: the runner passes every variable of the case after the base (section 6.3 step 3), so a case
that sets a locale variable or `TZ` has its value stand, and a case that sets none runs in the `C`
locale at UTC. The values are passed on unchanged and not checked. It stays deterministic because the image is pinned: it carries the zoneinfo
database of its `tzdata` package and does not install the `locales` package, so a locale it does
not have is not set and the program stays in the `C` locale, as any process would in that image.

### 5.8 Validation

A bundle is valid when all of the following hold. A validator reports every violation it
finds, with the file and the case id, and succeeds only when there is none. Rules marked
(W) produce a warning instead.

1. `tool.json` and every line of `cases.jsonl` are valid JSON objects whose members are
   those of sections 5.2 and 5.4, with the types and value constraints given there.
   Unknown members are errors.
2. `program.id` names a program, `program.source_sha256` equals its `source.sha256`, and
   `track` is allowed for it (`fixed` needs a `fixed_track`).
3. Case ids are unique; `t_ms` is non-decreasing.
4. All base64 values decode canonically; path, argument and environment rules of sections
   5.4 to 5.6 hold, the size limits of an argument, an environment string and of both
   together included; reserved names do not occur; no path is listed twice or conflicts
   with another entry; `stdin_tty` and `stdin_pipe` are booleans and, when `true`, `stdin` is a blob and
   the other is not `true`; `stdin_atime` and `stdin_mtime` are integers from -2^63 to 2^63 - 1 and need
   a `stdin` blob that is neither a terminal nor a pipe.
5. Every blob reference names a file `blobs/<hex>` that exists, is a regular file, whose
   SHA-256 equals its name and whose size is at most 32 MiB; the per-case size limit holds.
   A validator and a runner open every file of the bundle without waiting (a named pipe
   in the place of a file is refused at once) and examine what they opened before they read it:
   a symbolic link, a pipe, a device or a directory in the place of `tool.json`,
   `cases.jsonl` or a blob is an error.
6. Files in `blobs/` that no case references, and entries of `blobs/` that are not named by
   a SHA-256 digest (W).
7. The track rules of section 5.7 hold.
8. Symbolic link targets satisfy section 5.5, and `cwd` names a directory the case creates
   and the process can enter.
9. Cases with `t_ms` greater than `1000 * budget.wall_s` (W): they will not be replayed, and
   the number is reported. A validator or runner MAY be asked to treat this as an error.

A validator MAY also warn about files in the bundle directory other than `tool.json`,
`cases.jsonl` and `blobs/`, about an open-track case that uses a surface the program's
`inputs` does not list, and about a `budget.wall_s` other than 21,600 (results with another
budget are marked non-standard).

## 6. Replay protocol

### 6.1 Overview

A runner takes a valid bundle and the coverage build of its program, and produces the
outputs of section 7. Everything in this section is the behaviour of the reference runner;
a conforming runner MUST produce the same coverage for the same bundle up to the replay
noise of section 11.2.

### 6.2 Steps

1. **Validate** the bundle (section 5.8). Any error stops the run before anything else.
   Cases beyond the budget are not an error; they are skipped and counted.
2. **Universe** (section 3.3). Stop on a mismatch with `branch_total`.
3. **Plan.** The snapshot times are `0, I, 2I, ...` up to the largest multiple of the
   interval `I` below the budget `W = budget.wall_s`, followed by `W` itself. `I` is 900 s;
   a runner MAY offer another interval, but results that are published use 900 s. A runner
   refuses a replay with more than 1,000 intervals (`W > 1000 * I`); with the largest budget
   of 604,800 s that is an interval of at least 605 s. A case is *due* at the first snapshot
   time `T` with `t_ms <= 1000 * T`. Cases with `t_ms > 1000 * W` are not replayed; their
   number is reported as skipped. (The `canonical-2026` profile of section 11.17 plans
   differently.)
4. **Shards.** Make `N >= 1` shards. A shard is made of

   * a *build copy*: a copy of the build tree without `.gcda` files, made of real files
     (nothing in it is a hard link to the build, to another shard or to anything the program
     can reach), in which the write permission is removed from every file and directory. The
     binary, the `.gcno` files and the sources are therefore not writable by the program, and
     neither can a file be created, renamed or removed in their directories;
   * a *profile tree*, the directory `GCOV_PREFIX` points at (section 3.4), the only part of the
     shard that the program writes;
   * its own working area for the case directories.

   Shards MUST NOT share writable files. The permission bits protect the build copy and the
   other shards against accidents and against honest programs that write or delete where
   they should not; the program runs as the runner's user and can change permissions of
   its own files, so they are no defence against a program that does so on purpose
   (sections 6.7 and 11.14). `N` defaults to the number of available cores, is at most the
   number of cases and does not influence the result. The disk space needed is `N` times
   the size of the build.
5. **Replay** the snapshots in time order. For snapshot `T`, run the cases due at `T`
   concurrently over the shards, each shard running its cases one after the other. Which
   shard runs which case is immaterial. Each case follows section 6.3.
6. **Capture** at the end of every snapshot that replayed cases (section 3.4): capture every
   shard that ran cases since its last capture and form the cumulative set. A snapshot with
   no due cases repeats the previous cumulative set, and its file `snapshots/<t_s>.json`
   (section 7.3) is written all the same, so that a reader finds a file for every snapshot
   time; the bound of 1,000 intervals limits their number. Shards keep accumulating across
   snapshots, so each capture is cumulative.
7. **Flake check** (section 6.5).
8. **Write the outputs** (section 7). The output directory MUST NOT exist or MUST be empty
   before the run; the runner writes into a temporary sibling and renames it when complete,
   so a failed run leaves no partial output and an existing result is never overwritten.

### 6.3 Case execution

For each case the runner does the following. The sanitiser pass uses the same procedure
except where section 8 says otherwise.

1. **Case directory.** Create a new directory with the subdirectories `root` (the case
   root), `home` and `tmp`, all initially empty and owned by the runner's user. Create the
   standard input file `stdin` there if the case has standard input that is a file (not with
   `stdin_tty` or `stdin_pipe`), and set `stdin_atime` and `stdin_mtime` on it when the case has
   them, after the bytes are written. Materialise `files` in
   `root` (section 5.5). Remove the case directory after the case, whatever its depth.
   A case does not see another case's files inside its case directory, and `HOME` and
   `TMPDIR` are private to the case. Everything outside the case directory is shared: a
   program that writes to `/tmp`, `/var/tmp` or any other absolute path outside the case
   root changes what later cases (of any shard, at any time) find there, so a replay can
   depend on the order of the cases and on which shard ran which, and the flake check
   (section 6.5) then measures part of that. The specification does not isolate those
   paths; the operator's container can give each shard its own (section 6.7).
   Before it creates anything, the runner checks the case again against the rules of
   section 5.5 (paths, symbolic link targets, `cwd`), whether or not the bundle was
   validated; a case that fails is a `setup_error`.
2. **Program.** The binary named by `program.json` in the shard's copy of the build. The
   argument vector is `[<file name of the binary>, argv...]`.
3. **Environment.** Exactly:

   | Variable | Value |
   |---|---|
   | `PATH` | `/usr/bin:/bin` |
   | `HOME` | the `home` directory of the case directory |
   | `TMPDIR` | the `tmp` directory of the case directory |
   | `LANG` | `C` |
   | `TZ` | `UTC` |

   followed by the variables of the case's `env` (a case's own locale variables outrank the base
   `LANG=C`, and its `TZ` replaces `TZ=UTC`, section 5.7), followed by the runner's own variables:
   `GCOV_PREFIX` and `GCOV_PREFIX_STRIP` (section 3.4) and `AGENT_SOFTWARE_TESTING_CASE`, a token of
   32 lower-case hexadecimal digits that is new for every case and marks the processes of that
   case (step 7, which also says how much of it a runner may fix). No other variable is passed, so the process does not inherit the runner's
   environment.

   A case whose environment ends up without `TZ` (only in the `canonical-2026` profile, section 11.17) runs in the time zone of the
   machine or container, which the C library reads from `/etc/localtime`: the specified environment sets `TZ=UTC`, so there
   the zone of the machine does not matter. The runner measures the zone of a process without `TZ` once, before it replays,
   and records it in `replay.json` (`local_zone`, section 7.1); under `canonical-2026` it also warns, on standard error and in
   `warnings`, when that zone is not UTC. A container that binds the host's `/etc/localtime` (Singularity and Apptainer do by
   default) gives every such case the zone of the node.
4. **Working directory** `cwd` below the case root.
5. **Standard input.** The `stdin` file opened read-only at offset 0, or `/dev/null` when
   the case has none. Standard output and standard error go to `/dev/null`.
   With `stdin_pipe`, standard input is instead the read end of a fresh anonymous pipe: the
   runner writes the bytes of the blob into it from a thread of its own and closes the write end
   after the last byte (or when the program has finished), so the program cannot seek on
   descriptor 0, `fstat` reports a FIFO, and a read to the end of the input ends. There is no
   `stdin` file and, in the `canonical-2026` profile, no `fd0` in the working directory. This is
   `klee-replay` for a `stdin_stat` object that records a named pipe or no file type at all
   (`create_pipe` in `tools/klee-replay/file-creator.c`); `klee-replay` keeps its write end open
   until the program exits or half of its timeout has passed, which a program can tell apart from
   an immediate close only by waiting. If the program never reads the bytes, the writer stops
   when the program has finished.
   With `stdin_tty`, standard input is instead the slave side of a fresh pseudo-terminal in
   raw mode (input flags only `IGNBRK`, no output post-processing of newlines, no line
   discipline, a read returns as soon as one byte is there). The same holds for a `tty` file
   entry, except that the program opens the device itself and finds the settings of a
   freshly opened slave (line discipline on). In both cases the runner writes the bytes of
   the blob into the master side from a thread of its own and keeps the master open until the
   program has finished or 2.5 seconds after the last byte, whichever comes first, and then
   closes it: a program that reads until the end of its input sees the end (or a read error,
   as on Linux) after at most 2.5 seconds. These are the semantics of `klee-replay` for a
   character device (`create_char_dev` in `tools/klee-replay/file-creator.c`), which is how a
   KLEE test whose `stdin_stat` or `A-data-stat` object records `S_IFCHR` was replayed in the
   reference evaluation: `isatty` is true for the program. A program that is a session leader and opens a
   terminal would make it its controlling terminal and be sent `SIGHUP` when the master
   closes, so the runner starts the program of such a case in a process group of its own
   within the runner's session (`setpgrp`, as `klee-replay` does) instead of in a session
   of its own. If the runner cannot
   open a pseudo-terminal the case is a `setup_error`; it MUST NOT fall back to a file.
6. **Process.** A new session (`setsid`). Resource limits, set with `setrlimit` before the
   program starts: core dumps 0; file size 256 MiB (64 MiB in the sanitiser pass); open
   files 1,024; CPU time `ceil(timeout_ms / 1000) + 5` seconds. No limit on address space,
   because sanitiser builds reserve very large virtual ranges; memory and the number of
   processes are bounded by the container (section 6.7). (The `canonical-2026` profile
   sets no limit at all, section 11.17.)
7. **Time limit and processes left behind.** The wall-clock limit is `timeout_ms`. If the
   main process has not exited by then, the runner sends `SIGTERM` to the process group,
   waits 2 seconds, then sends `SIGKILL` to the group. When the main process has exited,
   any process left in its group is killed. A program can also leave a process behind
   that is no longer in its group (it called `setsid` or `setpgid`). On Linux the runner
   therefore reads `/proc/<pid>/environ` of every process of its own user and sends
   `SIGKILL` to each whose initial environment contains `AGENT_SOFTWARE_TESTING_CASE=<token>` of this
   case, except the members of the case's own process group (the group kill has signalled
   them), and repeats the scan, at most five times, until it finds no process it has not
   killed. The number killed is the case's `stray_killed`; `replay.json` adds them up
   (section 7.1). Where `/proc` does not exist nothing is scanned and the count is 0. A
   process that replaced its environment, that cannot be read (it is not dumpable) or
   that belongs to another user is not found; section 11.15 says what remains and what
   the container should do.

   The scan may be limited to the places where a case's processes can be, as long as it
   finds the same processes. The reference runner serves each shard with a worker process
   of its own (`--shard-mode process`, the default where processes can be started), and
   the worker is a child subreaper (Linux `PR_SET_CHILD_SUBREAPER`): a process of a case
   that left the process group or the session and whose parent exited is re-parented to
   the worker, not to `init`. After a case the worker reads `environ` only of its own
   descendants (listed through `/proc/<pid>/task/<tid>/children`, recursively, until no
   new process shows up), kills the marked ones in the same passes as above, counts them
   in the same way and collects them with `waitpid`, so that no zombie is left. Where the
   subreaper or the children listing is not available, and with `--shard-mode thread`, the
   scan reads every process of the user, as described above. The guarantee does not
   change: no process that a case started survives it, because every process of a case is
   a descendant of the worker that ran it. Once per snapshot interval, before the capture,
   the runner also scans all of `/proc` for the markers of its own replay (the first eight
   digits of the markers of one replay are the same; the other 24 are random per case) and
   adds what it finds to `stray_killed`. A worker that is stopped (`SIGTERM` or `SIGINT`
   to the runner, or the end of the runner) kills the process group and the descendants of
   the case it runs, and every other descendant, before it exits.

   **Timeouts and load.** For every case that times out the runner measures the CPU time its processes used: on Linux from
   `/proc` just before the first signal (the main process and its descendants, with the children each has collected), and
   in every case from the resource usage of the main process once it is collected; the larger figure counts. `replay.json`
   counts the timeouts of the first replay that used less than half of their time limit as CPU time (`timeouts.under_load`,
   section 7.1) and lists the ids of the first 100. Such a case was waiting, or the node left it little CPU: it did not run
   into its limit by computing, and the same corpus on a quieter node may not time out there. A walk of a directory tree
   (`du /`, `ls -R /`, `krep -r /`) is the usual case. The count does not change what the replay does. The limit stays the
   wall-clock limit of this step. The reference runner offers an opt-in deviation, `--timeout-clock cpu`: the program gets
   `RLIMIT_CPU` equal to `timeout_ms` rounded up to whole seconds (and, in the `canonical-2026` profile, no other limit), a
   case that the kernel stops for that reason is a `timeout`, and a case that uses no CPU is still stopped, as a `timeout`,
   after four times the limit in wall-clock time. `replay.json` records the clock (`timeouts.clock`). Results of the two clocks
   are not comparable on programs whose cases hit the limit (section 11.17).

8. **Status.** `timeout` (the main process had not exited at the time limit),
   otherwise `ok` (exit status 0), `nonzero_exit` (other exit status) or
   `terminated_by_signal` (the main process was killed by a signal). `setup_error`: the
   case directory or the process could not be set up and nothing was executed.

The runner does not use the status to select or discard cases: every replayed case
contributes whatever coverage data its process wrote.

### 6.4 Snapshots

Snapshot `T` reports the cumulative set after all cases due at times up to `T` were
replayed, the number of cases replayed up to `T`, and the number of covered branches. The
last snapshot (`T = W`) is the *final* coverage of the campaign. The initial snapshot
`T = 0` contains the cases with `t_ms = 0` (seeds present before the campaign started).

### 6.5 Flake check

After the last snapshot the runner removes all coverage data from the shards and replays the
whole bundle (every case with `t_ms <= 1000 * W`) a second time, in file order. It forms the
final cumulative set `B` of that replay and reports

* `branches_differing`, the size of the symmetric difference of the first final set `A` and `B`;
* `only_in_first` (`|A \ B|`) and `only_in_second` (`|B \ A|`).

Only the first replay enters the results; `B` is never used to fill branches. A flake of 0
means the replay is repeatable; a larger value is the replay noise of this program on this
bundle (section 11.2) and limits what differences in coverage can be interpreted. The check
is skipped, and `replay.json` says so, when it is switched off or no case was replayed.

### 6.6 Failure handling

The runner fails, with a non-zero exit status and no output directory, on: a bundle that
does not validate; a build root that does not exist or whose binary is missing; a universe
that cannot be captured or whose size differs from `branch_total`; a capture that fails; a
covered key outside the universe; an ambiguous source identity (section 3.1); cases that all
failed to start; cases that exited normally while no shard holds profile data (the binary is
not a gcov build or the compile root is wrong); an I/O error while writing outputs. A case
that crashes, times out or cannot be materialised is not a failure of the run.

### 6.7 Security

Cases are untrusted input to programs that can write files and start processes (gawk's
`system`, sed's `e`, xorriso's external commands). Isolation is the operator's job: the
runner MUST NOT be used outside a sandbox that confines writes to the case directories and
shard areas, denies network access, bounds memory and the number of processes and keeps the
rest of the file system read-only. The reference runner does not create that sandbox and
does not check that it exists; it assumes the container of `environment/Dockerfile`, started
with networking disabled (`--network none`), a read-only root file system, scratch space
mounted writable, and memory and process limits. The container MUST have a process view of
its own, which Docker gives by default and Singularity and Apptainer give with `--pid`: with
the node's process view, `/proc/<pid>/root` leads a case that walks the file system to the
node's disks, which changes what du and similar programs cover and can hang them. A bundle is data: its `blobs/` files are
never executed by the runner; only the program's own behaviour can execute them.

Three limits of what the reference runner itself guarantees, all inherited from running the
program as the runner's own user:

* **Submitted bundles are trusted not to forge coverage.** The program under test receives
  the path of its profile tree in `GCOV_PREFIX` and can write there. A real `.gcda` file is
  a documented binary format that gawk (`printf`), sed (`w`), xorriso or any program that
  writes files can produce, so a bundle can contain cases that make the program write a file
  that claims coverage it did not reach. The runner accepts only regular files with the
  expected names (section 3.4); it does not and cannot tell a genuine file from a forged
  one. The validator looks at the bundle, not at what its cases make a program do, and the
  flake check (section 6.5) replays the same cases again, so it reproduces a forgery as
  faithfully as it reproduces a measurement. Neither detects deliberate forgery. A publisher
  who accepts bundles from outside MUST take that into account: it publishes what a
  submitter can show (the campaign logs, section 11.9), and does not treat a validated and
  replayed bundle as evidence that its submitter is honest.
* **Processes can survive a case.** The group kill and the marker scan of section 6.3 step 7
  are best effort. The container SHOULD give every shard its own process-ID namespace or
  its own cgroup whose processes are all killed when the shard finishes its batch (for
  example `cgroup.kill`, or one container per shard), and SHOULD bound the number of
  processes (`--pids-limit`). Section 11.15. macOS only: a process the case started can
  still be seen for a moment after a timeout, because macOS collects the main process earlier
  than Linux does. The standard replay runs on Linux.
* **The build copy is write-protected by permission bits only.** A program that changes
  the permissions of its own files can write the build copy of its shard and so change
  what later cases run, in its own shard and, because every shard runs as the same user, in
  other shards too. The original build is never touched. Run each shard as its own user or in
  its own container when submitters are not trusted. Section 6.2 step 4.

## 7. Outputs

A replay writes one directory:

```
out/
  replay.json            summary (section 7.1)
  timeline.csv           t_s,covered
  coverage-branches.json final branch-set document (section 3.2)
  snapshots/<t_s>.json   branch-set document at each snapshot time (decimal seconds)
```

The sanitiser pass writes `signatures-counts.json` and `signatures-private.json` to a
directory of its own (section 8).

### 7.1 replay.json

Schema id `agent-software-testing/replay@0.1`. No other members than the following are allowed.

| Member | Content |
|---|---|
| `schema` | `agent-software-testing/replay@0.1` |
| `program` | `{"id", "source_sha256"}` as in `tool.json`. |
| `tool` | The `tool` object of `tool.json`, unchanged. |
| `track`, `repeat` | From `tool.json`. |
| `bundle` | `{"tool_json_sha256", "cases_jsonl_sha256"}`: SHA-256 of the two files, computed when the bundle has been validated and checked again when the replay is complete; a difference fails the run. |
| `build` | `{"program_json_sha256", "binary_sha256", "gcno_set_sha256"}`: SHA-256 of the `program.json` file the runner used, of the program binary of the coverage build, and of the notes files that define the universe (the sorted lines `<SHA-256 of the file>  <path relative to the compile root>` for the `.gcno` files named in section 3.3 step 2, each with its line feed). A result names the exact definition and build it was measured on. |
| `profile` | `standard` or `canonical-2026` (section 11.17). |
| `budget_s` | `budget.wall_s` of the bundle. |
| `interval_s` | The snapshot interval used. |
| `branch_total` | `branch_total` of the program. |
| `branch_covered` | Covered branches at the end of the replay; equals `covered` of the last snapshot. |
| `cases` | `{"total", "replayed", "skipped_beyond_budget"}`; `total = replayed + skipped_beyond_budget`. |
| `status_counts` | Counts of `ok`, `nonzero_exit`, `terminated_by_signal`, `timeout`, `setup_error` over the first replay; all five keys present. |
| `stray_killed` | Optional: the number of processes that outlived their case and were killed by the marker scan of section 6.3 step 7, over the first replay. A value above 0 means that bundle's programs start background processes. |
| `timeouts` | Optional: `{"clock", "count", "cpu_measured", "under_load", "under_load_case_ids"}`, over the first replay: the clock the time limit measured (`wall`, or `cpu` with `--timeout-clock cpu`, section 6.3 step 7), the number of cases that hit their limit (equals `status_counts.timeout`), how many of them had a CPU time measured, how many of those used less than half of their limit as CPU time, and the ids of the first 100 of these in the order of the corpus. |
| `snapshots` | Array of `{"t_s", "cases", "covered"}` in increasing time order, `cases` being the cumulative number of replayed cases. Non-empty; `covered` never decreases. |
| `flake` | `{"checked": false}` or `{"checked": true, "branches_differing", "only_in_first", "only_in_second"}`. |
| `runner` | `{"name", "version", "format", "workers", "shard_mode", "lcov", "gcov"}`: the runner, the format version it implements, the number of shards, how they were served (`process`: a worker process per shard, `thread`: a thread of the runner per shard; optional) and the first line of `lcov --version` and `gcov --version` (or null). |
| `local_zone` | Optional: the time zone of a process without `TZ`, which is what a case without `TZ` gets (section 6.3 step 3), measured before the replay: `{"offset_s", "abbreviation", "utc", "localtime_link", "tz_env"}`, the offset in seconds east of UTC and the abbreviation now, whether the zone is UTC (offset 0 now, on 1 January and on 1 July 2026, and the abbreviation `UTC`; null if it could not be read), the zone name that `/etc/localtime` links to without the path (null if it is not a link), the `TZ` of the runner's own environment (null if unset) and, optionally, `localtime_sha256`, the SHA-256 of the file `/etc/localtime` leads to (a zone file that a container binds over the target of the link does not show in the link's name); `error` is added if the measurement failed. |
| `warnings` | Optional: the warnings the runner printed on standard error before the replay, as strings (the `canonical-2026` profile: a time zone that is not UTC, or a CPU clock). Empty when there were none. |
| `setup_error_example` | Optional: text of one `setup_error`, or null. |
| `timing` | Optional: `{"replay_s", "flake_s"}`, wall-clock seconds of the replay and of the flake check. |

A runner MUST NOT write host-specific data (user names, host names, absolute paths of the
machine) into any output other than `setup_error_example`.

### 7.2 timeline.csv

Header `t_s,covered`, then one row per snapshot, in time order, with the snapshot time in
seconds and the number of covered branches. UNIX line endings.

### 7.3 coverage-branches.json and snapshots

Both are branch-set documents (section 3.2). `coverage-branches.json` equals the snapshot
of `T = W`, without `t_s` and `cases`. Each file `snapshots/<t_s>.json` has the members of a
branch-set document plus `t_s` and `cases`. There is one for every snapshot time, also for
a time at which no case was due; it then repeats the previous set.

### 7.4 Atomicity and reproducibility

Outputs are written once and never modified. For a given bundle, build and image, the
following are reproducible up to replay noise and are the only ones a publisher compares:
`branch_covered`, `snapshots`, `status_counts`, `bundle.cases_jsonl_sha256` and the contents
of the branch sets. `timing`, `runner.workers` and `runner.shard_mode` are not, and neither are `timeouts` (apart from `clock`
and `count`: which timeouts had used little CPU depends on the load of the machine), `local_zone` and `warnings` (they describe
the machine).

### 7.5 Results releases

A publisher collects the `coverage-branches.json` (compressed), the `timeline.csv` and the
run table entry of each replay into a results release (repository `agent-software-testing-results`).
A release records the specification version, the tag of this repository whose `programs/`
were used, the image identity and the runner version in its `RELEASE.json` (section 10).
Systems with fewer than three complete repeats on a program are kept and marked incomplete.

## 8. Sanitiser pass and the counts-only rule

The sanitiser pass runs the cases of a bundle on the sanitiser build to find memory errors
and undefined behaviour. It is optional, independent of coverage, and a separate command
(`python -m agent_software_testing signatures`).

**Execution.** As in section 6.3 with these differences. The program is the sanitiser binary
of section 4.3 (`binary` with `obj-gcov/` replaced by `obj-native/`, below the sanitiser build
root). The reference runner derives it by that replacement; `--binary` names another path
below the sanitiser build root. The runner's own environment variables are

```
ASAN_OPTIONS=abort_on_error=1:detect_leaks=0:symbolize=1
UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1:symbolize=1
```

and `GCOV_*` are not set. The last 64 KiB of standard error of every case are kept and
parsed; standard output is discarded. Cases run once each; cases with `t_ms > 1000 * W` are
skipped and counted. There is no time ordering and no snapshot: the result is one set of
signatures for the whole bundle. The symbolizer MUST be available, because signatures
need function names and file paths from the stack traces
(`ASAN_SYMBOLIZER_PATH` points to `llvm-symbolizer` in the pinned image).

**Findings.** A case has a *finding* for every AddressSanitizer line
`ERROR: AddressSanitizer: <text>` and every UndefinedBehaviorSanitizer line
`<file>:<line>:<column>: runtime error: <text>` in its standard error. A case can have
several. Findings in the standard error are all that is read; the exit status is not used.
A finding is text on a stream the program writes: a program can print a line that looks
like a report (gawk, sed and any program that can write to standard error do so on
request), and nothing in the pass distinguishes that from a report of the sanitiser
runtime. A submitter can therefore raise the number of signatures of a program with
cases that print reports, and a signature is a claim about the program's output, not a
verified finding (section 11.16).

**Signature.** The *signature* of a finding is the triple

```
(sanitiser, kind, frame)
```

* `sanitiser` is `asan` or `ubsan`.
* `kind` is a lower-case hyphenated name from a fixed list; it is never copied from the
  report. For AddressSanitizer it is the error type (`heap-buffer-overflow`,
  `stack-buffer-overflow`, `heap-use-after-free`, `segv`, `double-free`, `bad-free`, ...).
  For UndefinedBehaviorSanitizer it is a normalised category of the message
  (`signed-integer-overflow`, `shift-exponent`, `index-out-of-bounds`, `null-pointer-use`,
  `division-by-zero`, ...). Text that matches no known category has the kind `other`, so
  that a kind never contains a type name, a function or any other text a program chose.
  The lists of kinds and the normalisation tables are part of the measurement; they are in
  `src/agent_software_testing/kinds.py` and `src/agent_software_testing/signatures.py` and the schema of
  `signatures-counts.json` lists the kinds. Changing them changes the measurement
  (section 10). The private file keeps the unrecognised text of an `other` signature, cut
  to 120 characters, as `detail`.
* `frame` is `<file>:<function>` of the first frame of the first stack trace after the
  finding whose source file lies below the source root, with `<file>` relative to that root.
  The source root is the build root of the sanitiser build unless the operator names
  another directory. For an UndefinedBehaviorSanitizer finding whose stack trace has no such
  frame, the location in the `runtime error:` line is used with function `?`. If there is no
  frame in the program's own source at all, `frame` is `?`.

Distinct triples are distinct signatures. Signatures are a deduplication device, not a count
of defects: one defect can have several signatures and several defects can share one.
Triage to defects is outside this specification.

**Counts-only rule.** The signature triples, the frames, the function names, the crashing
inputs, the stack traces and the sanitiser output are *private*. They MUST NOT appear in
any file of a results release, on the website, in `replay.json` or in any other public
output of the project. The runner keeps them in `signatures-private.json` (the signatures
with frames, the number of cases for each, the first case and up to 20 example case ids),
which is never published. Public output is `signatures-counts.json`
(schema `agent-software-testing/signature-counts@0.1`), which contains counts only:

```json
{"schema": "agent-software-testing/signature-counts@0.1", "program": "gzip",
 "tool": {"name": "example-generator", "...": "..."}, "repeat": 1,
 "signatures": 3,
 "by_sanitizer_and_kind": {"asan": {"heap-buffer-overflow": 1},
                           "ubsan": {"signed-integer-overflow": 2}},
 "cases": {"replayed": 18234, "with_reports": 12, "skipped_beyond_budget": 0},
 "status_counts": {"ok": 18200, "nonzero_exit": 20, "terminated_by_signal": 14,
                   "timeout": 0, "setup_error": 0}}
```

(The numbers are illustrative.) `signatures` is the number of distinct signatures of the
bundle; `by_sanitizer_and_kind` counts distinct signatures per sanitiser and kind. The
results repository aggregates these into counts per system, program, sanitiser and kind, and
the number of repeats in which each kind was seen. Counting distinct signatures over several
repeats needs the private files and is done by the operator, who publishes the count only.
Defects are disclosed to the maintainers of the affected program before any
program-specific detail is published; a public count never names a function, file or input.

## 9. Statistics and reporting

### 9.1 Repeats

A published result for a tool on a program and track requires at least three independent
repeats: campaigns of the same configuration, started afresh with their own random seeds and
(for model-driven tools) their own sampling, each submitted as its own bundle with `repeat`
1, 2, 3, ... Replaying one campaign three times is not three repeats. A system with fewer
repeats is reported as incomplete and excluded from comparisons.

### 9.2 Summary of one tool on one program

Report the number of repeats `n`, the median of the final covered branches, and their
minimum and maximum, together with the branch total and the median as a percentage of it. Do
not report means or standard deviations of three values as if they described a distribution.

### 9.3 Comparing two tools on one program

For tools A and B with `n_A` and `n_B` repeats on one program and track report:

* the Vargha–Delaney effect size `A12 = (R_A - n_A (n_A + 1)/2) / (n_A n_B)`, where `R_A` is
  the sum of the ranks of A's final coverages in the combined sample (ties get mid-ranks).
  `A12 = 0.5` means no difference; `A12 = 1` means every A run covers more than every B run;
* the Mann–Whitney `U` statistic (`U_A = A12 * n_A * n_B`) with its two-sided p-value,
  computed exactly when `n_A, n_B <= 8` and by the normal approximation with tie correction
  otherwise.

With three repeats each, the smallest attainable two-sided exact p-value is 0.1, so a
p-value below 0.05 is impossible. Results with three repeats therefore carry the effect size
and `U`, and MUST NOT be described as significant or not significant. If p-values are
adjusted for several comparisons, the method (for example Holm) is stated.

### 9.4 Unique branches and overlap

For tools A and B on one program, let `S_A` and `S_B` be the unions of the covered keys over
all repeats of each, restricted to the branches that both measurements count (the
intersection of their universes; within Agent Software Testing builds the universes are identical).
Report `|S_A \ S_B|`, `|S_B \ S_A|` and `|S_A ∩ S_B|`. Overlap is reported only for programs
on which both tools have all their repeats.

### 9.5 No global ranking

A publisher MUST NOT present a single ordering of tools across programs or tracks, nor a
single score that stands for a tool's overall result. Results are shown per program and
track. Pairwise comparisons are shown per program. A summary across programs (for example
the geometric mean of per-program median ratios between two named tools) is permitted only
beside the per-program results, labelled as a summary, and MUST NOT be used to order more
than two tools. Default orderings in tables are alphabetical.

### 9.6 What every table states

Every table or figure of results states, for each system: the track, the budget and number
of cores (or that they are not stated), the model for tools that use one, the resources
reported (tokens, CPU time), the number of repeats, the measurement (`measure` of the
results repository), the replay noise (`flake`) of the cells shown or its maximum, and the
specification and programs versions.

## 10. Versioning

* **This specification** has the version `MAJOR.MINOR`. During `0.x`, any change that makes
  a valid bundle invalid, changes a key, or changes a measured number MUST increase `MINOR`;
  readers accept exactly the version they implement. From `1.0`, incompatible changes
  increase `MAJOR`, compatible additions `MINOR`. Editorial changes do not change the
  version.
* **Data formats** carry the version in their schema id (`agent-software-testing/tool@0.1`,
  `agent-software-testing/branch-set@0.1`, ...). A runner MUST reject a document whose schema id it
  does not implement.
* **Programs.** A change to a program's source pin, build recipe, driver, patches or
  `branch_total` changes the measurement. It requires a new tag of this repository. Results measured on different tags of a program MUST NOT be mixed
  in a comparison. Adding a program does not change existing ones.
* **Environment.** A change of the image (toolchain, packages, base image) requires the
  acceptance procedure of `environment/README.md`; it is a change of the measurement and is
  handled like a change of programs. So is a change of the signature normalisation tables
  (section 8) for sanitiser counts.
* **Results.** A results release (`agent-software-testing-results`) is immutable. Its `RELEASE.json`
  records the specification version, the tag of this repository, the image identity, the
  runner version and the date. Corrections are published as new releases that say what they
  replace.

## 11. Known limitations

Informative, but a publisher SHOULD state those that matter for a table.

1. **Coverage of killed runs is lost.** GCC writes `.gcda` data when the process exits. A
   process that is killed by a signal (`SIGSEGV`, `SIGABRT`, the runner's timeout kill)
   writes nothing, so everything it covered is missing from the replay. This matches the
   measurement that produced the reference numbers. Tools that find many crashes, and
   programs that crash on rare paths, are therefore under-measured, and a crashing input
   counts only through the coverage of its non-crashing neighbours. The runner does not call
   `__gcov_dump`.
2. **Replay noise.** Programs that read entropy, time or process identifiers can take
   different paths on identical inputs. xmlwf and xorriso have been observed to differ by up
   to about ten branches between two replays of the same corpus. The flake check
   (section 6.5) measures this for every replay; differences of that order between tools are
   not interpretable.
3. **Training data.** The 18 programs are public open-source projects. Language-model tools
   may have seen their sources, test suites, documentation and bug reports during training,
   and the source archives contain the upstream tests that a tool with file access can read.
   Agent Software Testing cannot separate recall from search or reasoning.
4. **Budgets and hardware.** A wall-clock budget is not comparable across machines. Results
   from different hardware, different core counts or different model-serving conditions are
   comparable only when the tools ran on the same machine under the same load. `budget.cores`
   and `budget.notes` record what the submitter did; Agent Software Testing does not verify it.
5. **One build, one compiler.** Branches are those of a gcc 11.4 `-O0` build of one source
   revision. They are not the branches of an optimised build, of a clang build or of the
   build a tool instrumented. Cases transfer by their input bytes only; behaviour that
   depends on the build (undefined behaviour, uninitialised memory) may differ.
6. **Subset of the fixed track.** Only six programs have a fixed track. The fixed track
   measures input generation for one agreed invocation; it does not measure choosing the
   invocation, which only the open track does.
7. **Case model.** A case cannot express: sockets, devices other than a terminal, ownership,
   extended attributes, ACLs, timestamps other than the access and modification time of a
   regular file and of the standard input file, set-id bits, signals sent to the process,
   interactive input, input that arrives over time, or more than one process. Standard input
   is a regular file unless the case asks for a pipe (`stdin_pipe`) or a terminal (`stdin_tty`),
   which matters for programs that test the input type. The replay sets a default locale (`LANG=C`)
   and time zone (UTC) for the variables the case leaves unset, and fixes the search path.
8. **Host state.** A program can read files outside the case root that exist in the image
   (`/etc`, `/usr`, `/proc`). The image is fixed and the root file system should be read-only,
   but such reads are part of the measured behaviour. A program can also write outside the
   case root, wherever the container lets it: `/tmp`, `/var/tmp` and every other absolute
   path are shared by all cases, shards and snapshots of a replay (only `HOME` and `TMPDIR`
   are private to a case). A program that leaves a file there, or reads one a former case
   left, behaves differently depending on which cases ran before it on that file system.
   The result then depends on the order of the cases and on the assignment of cases to
   shards, which `N` is not supposed to influence (section 6.2, step 4), and the flake check
   counts the differences that come from it together with the others. The reference runner
   does not isolate these paths; a container that mounts a fresh `/tmp` for every shard, and
   a read-only root file system otherwise, removes most of it.
9. **Self-reported times.** The runner cannot verify `t_ms` or `budget.wall_s`. A bundle that
   gives every case `t_ms = 0` or an inflated budget replays without complaint. Timelines and
   comparisons rest on the submitter's honesty; a publisher SHOULD publish the campaign logs
   or the tool's configuration alongside, and MAY record `config_sha256`.
10. **Branch coverage is not bug finding.** More covered branches do not mean more defects
    found, and sanitiser signatures are not defects (section 8). A combined ASan+UBSan build
    reports only the first error of a process, so a case that triggers both kinds is counted
    once.
11. **Language-model campaigns vary.** Repeats of one model-driven configuration differ
    through sampling, service changes and tool use; three repeats give a coarse picture.
    Token counts of different models are not comparable without the model's tokenizer and
    price list.
12. **Isolation is external.** The runner relies on the operator for network isolation and
    for bounding memory and processes (section 6.7). A run outside the intended container
    may be unsafe and may differ in behaviour.
13. **Acceptance run for one program.** The coverage builds of all 18 programs were made and
    their universes match; the full acceptance procedure has run for one program (Confetti).
14. **Submitted bundles are trusted not to forge coverage.** The program under test can
    write its own profile data: it is told where the profile tree is (`GCOV_PREFIX`), the
    tree is writable by the program, and `.gcda` is a binary format that programs such as
    gawk, sed or xorriso can write. The runner checks that the profile tree holds only
    regular files with the expected names (section 3.4); it cannot check that their content
    is what the instrumented binary would have written. The validator checks the bundle,
    not what its cases make a program do, and the flake check replays the same cases, so
    neither detects deliberate forgery: a forged profile file is reproduced by the second
    replay as faithfully as a genuine one. The same holds for `t_ms` (item 9). Coverage
    numbers from a replay show what the bundle's cases made the program write, and
    they are evidence of coverage only as far as the submitter is trusted.
15. **Processes can outlive their case.** The group kill and the marker scan (section 6.3,
    step 7) do not find a process that changed its environment before it left (it called
    `clearenv` and `execve` with an empty environment, or overwrote its environment block),
    one that cannot be read through `/proc` (it made itself not dumpable, or it changed
    its user), one that is created after the scan, and nothing on a system without
    `/proc`. Such a process can run on in the shard, write profile data of its own
    after the case ended (into a later snapshot, or after the final capture), or change
    files that later cases read. The reference runner reports only the processes it
    found (`stray_killed`). A container SHOULD give each shard a PID namespace of its own
    or put it in a cgroup that is killed as a whole when the shard has finished a batch
    (`cgroup.kill`, `docker kill` of a per-shard container), and SHOULD set a limit on the
    number of processes. A worker of the reference runner adopts the orphans among its
    descendants and kills all its descendants, marked or not, when it stops; that cleans
    up after a replay, but it is not the boundary asked for here, and during a replay the
    scan still finds only processes that carry the marker.
16. **Sanitiser reports are program output.** A program can print text that looks like an
    AddressSanitizer or UndefinedBehaviorSanitizer report (section 8). The sanitiser pass
    counts it. Kinds are drawn from a fixed list, so such text cannot put names into a
    public count, but a signature is a claim that the program printed a report, not proof that
    the sanitiser runtime found a defect. Counts of bundles from outside need the same
    trust as item 14.
17. **The `canonical-2026` profile and what it does not reproduce.** The replay pipeline
    that produced the reference numbers differs from the standard protocol, and the
    differences change coverage. The option `--profile canonical-2026` of the reference
    runner reproduces nine of them, so that one corpus can be run through both
    pipelines and the numbers compared (`docs/canonical-profile.md`):

    | | `standard` | `canonical-2026` |
    |---|---|---|
    | Time limit | `timeout_ms` of the case (default 30,000) | 30 s for every case, whatever `timeout_ms` says |
    | Resource limits | core 0, file size 256 MiB, 1,024 open files, CPU `ceil(timeout/1000)+5` s | none |
    | Which snapshot takes a case | the first `T` with `t_ms <= 1000 * T` | the first `T` with `t_ms // 1000 <= T`: whole seconds, so a case at 900.999 s belongs to `T = 900` |
    | The last snapshot (`T = W`) | the cases with `t_ms <= 1000 * W`; later cases are counted as skipped | every remaining case, also those with `t_ms > 1000 * W` (the campaign's orderly shutdown); nothing is skipped |
    | Locale | `LANG=C` unless the case sets a locale variable itself (section 6.3 step 3) | the case's own locale variables, as in the standard profile; else `LC_ALL=C` (the reference pipeline's base locale), unless the case's `env` names `LANG`, `LANGUAGE` or an `LC_*` variable other than `LC_ALL`; then the base environment has no locale variable and the case's variables stand, as in the reference pipeline (it replayed with the environment recorded next to a test) |
    | Time zone | `TZ=UTC` unless the case sets `TZ` itself (section 6.3 step 3) | the case's own `TZ`, as in the standard profile; else `TZ=UTC` unless the case's `env` names a locale (a recorded environment): then no `TZ`, because the reference pipeline's programs had none; gcal asks `getenv("TZ")` and covers 3 to 4 more branches (`src/file-io.c`, `src/gcal.c`) when it is set. A case without `TZ` then runs in the zone of the machine or container (`/etc/localtime`), which the profile takes to be UTC, the zone of the reference image: the runner records the zone (`local_zone`) and warns when it is not UTC |
    | Standard input file | the file `stdin` beside the working directory, opened read-only (section 6.3 step 5) | the file `fd0` in the working directory, opened for reading and writing, as `klee-replay` made it, for every case whose standard input is a file (`stdin` is set, and neither `stdin_pipe` nor `stdin_tty` is `true`); a case with a file of that name keeps standard input outside: `grep -r .`, `ls`, `du` and the programs that walk the directory list it (a sample of 1,000 grep tests covered 2,511 branches without it and 2,557 with it, as `klee-replay` does). Only the case matters; the runner never reads the tool or the `origin`. In both profiles a case with `stdin_pipe` has a pipe and no file, and `stdin_atime` and `stdin_mtime` are set on the file |
    | Output | standard output and standard error go to `/dev/null` (section 6.3 step 5) | one pipe for both, read and discarded by the runner: the reference pipeline captured the output of every program, and a program can tell: grep stops printing when standard output is `/dev/null` (`dev_null_output`), so a grep run covers 233 more branches (those that print a line) with a pipe |
    | `argv[0]` | the file name of the binary (section 6.3 step 2) | the path of the shard's copy of the binary, as `klee-replay` and the replay scripts passed it; programs branch on a `/` in it (xmlwf's `--version` strips the directory, gnulib's `set_program_name` looks for `/.libs/`) |

    The flake check replays the cases the profile replays in pass 1. What the profile does
    **not** reproduce, so that a difference in coverage between the two pipelines can still
    come from it: (a) the *input path*: the reference pipeline passed the absolute path of a file
    named by the SHA-256 of its content in a directory shared by all cases, Agent Software Testing
    passes `input` (or the path the case chose) in a fresh case root, which matters to
    programs that derive file names or branch on the path (woff2_decompress writes its
    output next to the input, oggenc names its output after the input), and the reference
    pipeline gave LibYAML its input as a file argument where the fixed track of
    this specification uses standard input; (b) the *working directory*: a single shared
    directory that is emptied before each case, with `HOME` in it and a `TMPDIR` that was never
    cleaned, against a fresh case directory with private `home` and `tmp`; (c) the number of
    *shards* (20 against one per core) and the assignment of cases to them, which matters
    through item 8; (d) the capture: the reference pipeline merged the lcov captures of the
    shards with `lcov -a` and read the merged file once, the reference runner reads
    each shard's capture and unions the sets, which gives the same keys when every shard's
    capture has the same instrumented lines as the initial capture and otherwise either fails
    (a key outside the universe) or drops a branch; (e) the strictness of a failed lcov run
    (the reference runner stops on a failed or empty capture); (f) the shard layout
    (separate profile tree and read-only build copy), the case marker `AGENT_SOFTWARE_TESTING_CASE`
    in the environment, the stray-process scan, and the re-check of cases before they run;
    (g) de-duplication of cases and the origin of times, which are the adapters' concern; (h) the *date and the host*:
    gcal prints the calendar of the current month, so one corpus covers 3,310 to 3,332 branches depending on the
    day it is replayed, and a test that names `..`, `/tmp` or another
    absolute path reads what the host holds there (xorriso with `-path-list` and `..` writes the parent of the working
    directory into an image, which a replay on another host cannot reproduce); the profile does not pin the
    clock to a day, because that would reproduce a published number and not what the tests do
    on the day they are replayed: a comparison states the day instead; (i) the *load of the
    replay node*: the limit of 30 s is wall-clock time, so a case that walks a directory tree (`du /`, `ls -R /`, `krep -r /`)
    times out on a busy node and not on a quiet one, and its coverage changes with it. `replay.json` counts the timeouts that used less than half of their limit
    as CPU time (`timeouts.under_load`) and lists their ids, so that a reader can see whether load caused them. The option
    `--timeout-clock cpu` (section 6.3 step 7) removes the effect of load at the price of departing from the reference pipeline,
    whose limit was wall-clock time; it is never part of the profile, and the runner says so when the two are combined;
    (j) the *standard output of the KLEE baseline replays*: the replays that measured the KLEE baselines sent standard
    output to `/dev/null`; the profile keeps the
    pipe for every case (one rule for all tools), so a published grep baseline set lacks the printing branches that a
    replay with this profile covers.
    Until a corpus has been replayed through both pipelines and the numbers have been
    compared, results of the two MUST NOT stand in one table.

18. **Terminals need pseudo-terminals.** A case with `stdin_tty` or a `tty` file needs the operating system's
    pseudo-terminals (`/dev/ptmx` and `/dev/pts` in a container). Without them the case is a `setup_error`; the
    runner does not fall back to a file, because the program would then cover other branches (`isatty` false).
    What a program sees when the master closes (end of file, a read error, `SIGHUP`) is up to the operating
    system; the reference runner starts the program of such a case with `setpgrp` instead of `setsid` and was tested on Linux only. The
    bytes of a terminal are delivered by a thread of the runner, so a very large terminal input is slower than a
    file.

19. **What `klee-replay` does, and what a case cannot say.** The KTest adapter replays a test the way
    `klee-replay` does (KLEE 3.1, `tools/klee-replay`, with `klee_init_env` and `fd_init.c` of the POSIX runtime),
    quirks included, so that the numbers of the reference pipeline can be reproduced; `klee_replay=False` in the
    adapter's functions describes what the test says instead. On the tests checked, the runner (`--profile
    canonical-2026`) and a `klee-replay` built from the KLEE 3.1 sources cover the same branches up to the
    differences listed under item 17. The adapter follows `klee-replay` in these places:
    (a) *symbolic arguments.* `--sym-arg N` and every argument of `--sym-args MIN MAX N` is an object of `N + 1`
    bytes whose last byte `klee_init_env` overwrites with NUL after `klee_make_symbolic` (`__get_sym_str`); the
    solver leaves that byte free and the test records it, often a printable character (`-r-` for `N = 2`). The
    adapter takes the first `N` bytes up to the first NUL and refuses a test whose object has
    another size (`klee-replay` exits then). `--sym-args MIN MAX N` with `MIN == MAX` has no `n_args` object
    (`klee_range` returns the value without one), so the adapter does not take the object of the next
    `--sym-args` for it. (b) *Tests `klee-replay` cannot replay* are skipped with the reason in the report, because the
    reference pipeline has no coverage for them: a symbolic file or a standard input whose stat object records a symbolic
    link (`create_link` passes the replay directory to `create_file` a second time, so `NAME.lnk` cannot be created and the
    process exits), and a symbolic file other than a directory with
    more than one link (`create_file` calls `link` with a name relative to its own working directory and exits).
    (c) A symbolic file of type `S_IFIFO` leaves no file. (d) A standard input of type `S_IFDIR` leaves a directory
    `fd0` in the working directory and standard input as the replay had it (none). (e) `klee-replay` cuts every
    argument of exactly two characters that starts with `A` to `A` (a leftover of its symbolic-file naming; it holds for
    concrete arguments too), and the adapter does the same. (f) A character device is a pseudo-terminal (section 6.3
    step 5), a directory a directory with the permission bits of its stat object, and a socket, a block device
    and any other type bits a regular file with the permission bits (`0644` when zero) and the access and
    modification time of the stat object. (g) The environment is the `.ktest.env` sidecar of the test (the whole
    environment of the KLEE process, with `LANG=C` and `LC_CTYPE=C.UTF-8`, no `TZ`); without it a replay runs under
    the environment of section 6.3 and covers fewer branches in the programs that read
    environment variables or the locale (gzip, grep, sed, du, ls, gawk, and xmlwf through `EXPAT_*_DEBUG`). With
    the sidecars, the arguments, `TZ` and the standard input file as in item 17, the covered branches of Krep equal the
    published set key for key (and the 25 points of its curve equal the published curve), and those of xmlwf (1,187) and sed (1,202) equal the
    published counts. In samples of 1,000 tests of twelve programs the runner and the real `klee-replay` cover the same branches
    (bc, grep, gcal, oggenc) or differ by at most four (replay noise, item 2), except for xorriso (item 17 (h): 351 of
    12,248 branches).
    (h) *Standard input.* `create_file` tests a symbolic link, a directory and a character device first; then a
    `stdin_stat` object that records a named pipe, or for descriptor 0 alone no file type at all (`st_mode & S_IFMT`
    is zero), makes `klee-replay` deliver standard input through a pipe and create no `fd0`; every other type
    gives a regular file `fd0` that `futimes` gives the access and modification time of the stat object. The adapter writes `stdin_pipe` for the first and `stdin_atime` and
    `stdin_mtime` for the second. Without the times gzip lacks one to six branches
    (`localtime` of the timestamp, the timestamp checks of `zip.c`), and without the pipe diff, gawk and oggenc lack a few
    (`lib/c-file-type.c` reports a fifo, `fseek` fails). `klee-replay` keeps the write end of the pipe open until
    the program has exited or half of its timeout has passed, the runner closes it after the last byte (section 6.3
    step 5).
    What a case still cannot say, so that a difference can come from it: the `umask(0)` that `klee-replay`
    runs the program with (no difference found) and the permission bits that `klee-replay` gives `fd0` from its
    stat object (the runner does not set them).

---

## Appendix A. Worked example (informative)

`docs/examples/krep-fixed/` is a complete bundle for the fixed track of Krep. Its
`tool.json` is a tool named `example-generator`. Its first case, after decoding, is:

| Member | Value |
|---|---|
| `id` | `000001` |
| `t_ms` | `0` |
| `argv` | `["test", "input"]` (stored as `["dGVzdA==", "aW5wdXQ="]`) |
| `stdin` | `null` |
| `files` | one regular file `input`, mode `0644`, with the bytes `hello world\n` (blob `sha256:a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447`) |
| `env` | `{}` |
| `cwd` | `.` (stored as `Lg==`) |
| `timeout_ms` | `10000` |
| `origin` | `seed` |

The runner executes `krep test input` (with `argv[0]` set to `krep`) in a fresh case root
that contains `input`, with the environment of section 6.3 and no standard input. The
matching fixed-track rule is `argv == ["test", "{input}"]` with `{input}` replaced by
`input`.

`docs/examples/gzip-open/` shows the open track: a case with only an argument, a case with an
argument and a file, one with standard input and an environment variable, and one that
creates a directory and a dangling symbolic link. Validate them with

```
python -m agent_software_testing validate docs/examples/krep-fixed
python -m agent_software_testing validate docs/examples/gzip-open
```

A bundle can be written by any program that can write JSON and compute SHA-256; section 5
and `docs/adapters.md` give what is needed.
