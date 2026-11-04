# Adapters: from a tool's output to a bundle

A bundle (SPEC section 5) is `tool.json`, `cases.jsonl` and `blobs/`. Every test generator
has its own output format; an *adapter* maps one to a bundle. This page says how each
supported format maps, what is lost, and how to build a bundle for anything else.

| Source | Status | How |
|---|---|---|
| KLEE test files (`*.ktest`, with `*.ktest.env` sidecars) | implemented | `python -m agent_software_testing adapt ktest` |
| AFL++ output directories (`queue`, `crashes`, `hangs`) | implemented | `python -m agent_software_testing adapt afl` |
| Other tools that write AFL-style `id:*` files (some hybrid fuzzers' helpers do) | implemented through the AFL++ adapter | `adapt afl`, with `--start-time` when names carry no `time:` field |
| libFuzzer corpora | recipe, using the AFL++ adapter | a short renaming script (below) |
| Generators that already hold concrete inputs | recipe | write the bundle with a script (below) |
| Test-Comp test vectors | no automatic mapping (explained below) | harness-specific |
| ConcoLLMic cases (YAML files whose code runs the program) | implemented, in record mode | `python -m agent_software_testing adapt concollmic` |

Adapters check the format's limits before they write a case. A native test case that cannot
be expressed (it exceeds a limit, uses a reserved environment name, has a NUL byte in an
argument) is skipped, and the adapter prints a report with the reason for the first skipped
cases. After writing, an adapter validates the bundle and exits non-zero if it is not valid;
`--no-validate` turns this off.

## Options every adapter takes

```
--out DIR              bundle directory to create (it must not exist, or be empty)
--program-id ID        program the cases are for (required)
--program PATH         program.json or the program's directory (default: look up ID in --programs)
--programs DIR         directory with <id>/program.json (default: ./programs)
--source-sha256 HEX    SHA-256 of the pinned source archive (default: from the program definition)
--tool-name NAME       (required)   --tool-version V   --family symbolic|fuzzer|hybrid|llm|agent|other
--model M              language model, if the tool uses one
--config-sha256 HEX    --tool-url URL
--track fixed|open     default open
--repeat N             default 1
--wall-s S             the wall-clock budget the campaign was allowed, in seconds (required: only
                       the submitter knows it; the standard budget is 21600)
--cores N              cores the tool used; written only when given (absent means not stated)
--budget-notes TEXT    --output-tokens N   --input-tokens N   --cpu-s S   --notes TEXT
--timeout-ms MS        per-case time limit, default 30000 (the maximum)
--no-validate          do not validate the bundle after writing it
```

An adapter never invents a budget or a core count: `--wall-s` has to be given, and `cores` is left
out of `tool.json` unless `--cores` is.

`python -m agent_software_testing adapt <kind> --help` is authoritative for the current options.

## What `t_ms` should be

`t_ms` is the time at which the tool produced the case, in milliseconds from the start of
the campaign. Seeds the campaign started with are at 0. Use the tool's own clock when it
records one (AFL++ does, in the file name). Otherwise use file modification times only if
the files were written when the cases were found and the times survived any copy; a copy that
does not preserve times (plain `cp`, most download tools, repacking into an archive) gives every
file the copy time and makes all cases appear at once. Do not
spread times artificially. The runner cannot verify them (SPEC section 11.9).

**A corpus that was repacked without its modification times has no usable times** (all tests of
a campaign lie within one second), so converting it with the default rules gives times that are
wrong. Give the times from outside (`--times`, from the campaign's logs) or do not publish a
timeline for it; the final coverage does not depend on the times, only the curves do.

## KLEE test files

```
python -m agent_software_testing adapt ktest klee-out/ \
    --out bundle/ --program-id gzip \
    --tool-name KLEE --tool-version 3.1 --family symbolic --cores 6
```

The input is a directory; every `*.ktest` file below it, at any depth, is a test (symbolic links
are not followed). KLEE writes `klee-out-N/test000001.ktest`, and a collection of campaigns is a
tree of such directories in which the same file name occurs many times, so a test is addressed by
its path relative to the input directory, written with `/`. A file `NAME.ktest.env` next to
`NAME.ktest` holds the environment of the test, as `KEY=VALUE` lines; it is used when present.

**Format.** KTEST files of versions 1 to 3 with the magic `KTEST` or `BOUT\n`, as written
by KLEE. Version 4 (pointer metadata, KLEE 3.0
and later) is not supported.

**Mapping.** `args[0]` of the KTest is the program path and is dropped; the runner sets
`argv[0]`. The remaining arguments are the command line given to KLEE after the bitcode.
KLEE's POSIX runtime turns some of them into symbolic data; the test holds the concrete values
as named objects. The adapter resolves them in command-line order, the way `klee-replay` does:

| KLEE option | Objects used | Result |
|---|---|---|
| `--sym-arg N` | the next `argK` object; KLEE makes N+1 bytes symbolic and overwrites the last with NUL, so the argument is the first N bytes, up to the first NUL among them | one argument |
| `--sym-args MIN MAX LEN` | `n_args` (4 bytes, little endian) for the count, then that many `argK` objects | that many arguments (`MIN` if `MIN == MAX` and there is no `n_args`) |
| `--sym-files N SIZE` | `A-data`, `B-data`, ... (also `A_data`) and the type and mode from `A-data-stat` | N files named `A`, `B`, ... in the case root; see below for the type |
| `--sym-stdin SIZE` | `stdin`, and the type and times from `stdin-stat` (or `stdin_stat`) | standard input (a terminal when `stdin_stat` records a character device, a pipe when it records a named pipe or no type, a file with the recorded times otherwise) |
| `--sym-stdout`, `--fd-fail`, `--max-fail N`, `--save-all-writes` | none | dropped: no counterpart |

| any other argument | none | a concrete argument, kept in place |

By default the adapter maps a test the way `klee-replay` replays it, skipping the tests it cannot replay (with the reason in the report). `--as-written` maps what each test says instead. Proxy variables and any value holding a URL with a user name and password are always dropped from recorded environments.

**Standard input** is the `stdin` object. Its `stdin_stat` object records a type as well, and `klee-replay`
sets standard input up by it (`create_file`, which tests the types in this order): a symbolic link makes it stop
before the program starts, and the adapter skips the test with the reason; a **directory** leaves standard input as
the parent had it and makes a directory `fd0` in the working directory, which the adapter reproduces as a `dir`
entry `fd0` and no standard input; a **character device**
becomes a pseudo-terminal that a feeder process fills (so `isatty` is true and a read at the end of an
unterminated line fails instead of ending), and the adapter writes `stdin_tty` for it; a **named pipe**, or for
standard input alone a mode without any type bits, becomes a pipe (the program cannot seek, `fstat` says FIFO, and
there is no `fd0`), and the adapter writes `stdin_pipe`. Everything else (a regular file, a block device, a socket or
an invalid type) becomes a file `fd0` whose access and modification time `klee-replay` sets from the stat object
(`futimes`, seconds, after the bytes are written), and the adapter writes `stdin_atime` and `stdin_mtime`; a test
without a `stdin_stat` object, or with one of another size, gets an ordinary file and no times. gzip reads the
modification time of its input, also from standard input (`time_stamp`): `localtime` of an extreme value, or a
timestamp of zero, takes branches that the time of the replay does not, and gzip runs lost one to six branches
without the times. With `--as-written` a named pipe is a pipe as well, but a mode without any type bits stays a
file, because it is not what the test says. Cases that differ only in the times or in the pipe are different cases.
On libmatheval, a character-device standard input adds two branches (`scanner.c`'s `isatty` and `fgets`
returning null) that a replay of the same bytes through a file does not reach.

The type and mode of a symbolic file come from the `struct stat` of its `-stat` object (Linux
x86-64 layout, 144 bytes) when there is one. KLEE only *prefers* a regular file there, so a test
can record anything. A regular file keeps its permission bits. A **character device** becomes a `tty` entry: the runner makes a
pseudo-terminal, links the file name to its slave side and feeds it the bytes, as `klee-replay` does (so `isatty`
is true and `mmap` fails). A **directory** becomes a `dir`
entry. A **symbolic link** becomes a `symlink` entry whose target is the data object up to its
first NUL, which is what KLEE's own `readlink` model returns; a link whose contents are empty,
absolute or climb out of the case root cannot be expressed and becomes a regular file. Invalid
type bits (a value that is no file type at all, or none) and types a case cannot express (socket,
block device, named pipe) also become a regular file `0644`, and the report notes each such file. A
missing `-stat` object, or one of another size, gives a regular file `0644` without a note.
Names in the sidecar that a case may not set (`PATH`, `HOME`, ...) are dropped and noted in the
report. `LC_ALL` and `TZ` are kept: a test generator can set them on purpose, and the
program then takes other paths (SPEC.md 5.7).

**File times.** `klee-replay` gives every regular symbolic file the access and modification time of its
`stat` object (`futimes`, seconds); programs read them (gzip's `-l` and `-N`, `ls -l`, `find -newer`; gzip
covers more branches when `localtime` gets a time far outside the calendar). The
adapter writes `atime` and `mtime` of every regular file entry, and cases that differ only in them are
different cases.

**Times.** By default the file modification times, relative to the oldest file in the tree, which
is **not** the campaign start (the report says so). Give `--start-time EPOCH_SECONDS` when the
files carry the discovery times and the campaign start is known; a file older than that gets
`t_ms` 0 and the report counts them. Pass `--times FILE.json`, a JSON object from the path of a
test relative to the input directory (for example `"klee-out-0/test000001.ktest"`) to `t_ms`,
when the files were copied or repacked; paths that appear in the object take their time from it
and the others from their modification time, and the report notes entries that match no file.
**Duplicates** (same arguments, input, files and environment) are kept once.

**Limits of the mapping.**

* A corpus without `.ktest.env` sidecars, file times and pseudo-terminal types replays under the
  environment of SPEC section 6.3. A test that depends on an environment variable (gzip's `GZIP`, `ls` with
  `COLUMNS` or `TIME_STYLE`), on a file's modification time (`gzip -l`, `ls -l`) or on the type of its standard
  input or files then covers fewer branches than it does with them. Test files larger than the blob limit
  (32 MiB) are skipped with a report entry; the case limit is 64 MiB.

* Failure injection (`--fd-fail`) and symbolic standard output are not representable.
* Only the POSIX runtime's naming is understood. A KTest produced by a harness that calls
  `klee_make_symbolic` on its own buffers has other object names; write that bundle with
  a script instead.
* Which track. A KLEE run that makes arguments symbolic belongs to the **open** track. A
  fixed-track bundle needs the invocation to be fixed by the harness: `--sym-stdin N` for
  programs that read standard input (Confetti, libmatheval, LibYAML), or a harness that reads a
  single file. If the mapped case does not match the program's fixed invocation the validator
  rejects the bundle; it does not guess.
* Paths and files the program opens through absolute paths are not in the KTest. A case
  replays against whatever the pinned image contains at those paths.

## AFL++ output directories

```
python -m agent_software_testing adapt afl \
    --queue out/main/queue --queue out/s1/queue \
    --crashes out/main/crashes \
    --arg=-c --arg=@@ \
    --out bundle/ --program-id gzip --track open \
    --tool-name AFL++ --tool-version 4.30c --family fuzzer --cores 2
```

**Input.** Directories named with `--queue`, `--crashes` and `--hangs` (each may be repeated:
one per fuzzer instance). Files whose names begin with `id:` are cases; `.state`, `README.txt`
and the like are ignored.

**Delivery.** The template of the AFL++ command line decides how the bytes reach the program.
Give the arguments after the program name with `--arg TOKEN` (repeat it; write `--arg=-c` for
tokens that start with a dash) or as a JSON list with `--argv-json '["-c","@@"]'`.

* A template with a `@@` argument delivers the bytes as a file: `@@` becomes `input` and
  the case holds one regular file `input` (mode `0644`).
* A template without `@@` delivers the bytes on standard input.
* On the **fixed track** the invocation is taken from the program definition and `--arg` is
  ignored; the program's `fixed_track` decides between file and standard input. The adapter
  stops if the program has none.
* On the open track an empty template (no `--arg`) means no arguments and standard input.

**Time.** `t_ms` is the `time:` field of the file name (milliseconds since the fuzzer
started). Files whose names have none (for example inputs copied in from another tool) use
their modification time minus `--start-time EPOCH_SECONDS`; without `--start-time` such a file
is an error, because its time would be a guess. The start of a campaign is the `start_time`
line of `fuzzer_stats`.

**What is included.** Everything in the named directories. `queue` holds the inputs that found
new coverage and the initial seeds (at time 0). `crashes` and `hangs` are replayed only if you
name them. A crashing input still runs (and may cover code before it dies), but a process killed
by a signal writes no coverage data (SPEC section 11.1), and a hang is killed at the timeout and
adds nothing; naming `--hangs` is rarely useful. The `origin` of a case is `queue`, `crashes` or
`hangs`.

**Duplicates.** Identical bytes are kept once, at their earliest time. With several instances
this removes the copies that AFL++'s synchronisation imports into each queue (`sync:` in the
name). `--no-dedupe` keeps them.

**Harnesses.** If the fuzzer ran an in-process harness that is not the command-line program,
its corpus is still a set of byte strings and can be replayed as the program's input, but
inputs shaped for the harness's own parsing of options or framing may not exercise the program
the same way. State the harness in `--notes`.

## libFuzzer corpora

libFuzzer keeps its corpus as a directory of files named by a hash, with no record of when each
was found. A corpus directory that the fuzzer itself wrote has modification times that are the
discovery times. The AFL++ adapter reads any file named `id:...,time:<ms>`, so a short script
converts a corpus into that form (the script below is tested with the AFL++ adapter):

```python
#!/usr/bin/env python3
# usage: libfuzzer_to_afl_names.py CORPUS_DIR OUT_DIR START_EPOCH_SECONDS
import os, shutil, sys

corpus, out, start = sys.argv[1], sys.argv[2], float(sys.argv[3])
os.makedirs(out)
entries = sorted((os.stat(os.path.join(corpus, n)).st_mtime, n) for n in os.listdir(corpus))
for number, (mtime, name) in enumerate(entries):
    t_ms = max(0, int(round((mtime - start) * 1000)))
    shutil.copyfile(os.path.join(corpus, name),
                    os.path.join(out, f"id:{number:06d},time:{t_ms},orig:{name}"))
```

then

```
python -m agent_software_testing adapt afl --queue renamed/ --program-id krep --track fixed \
    --out bundle/ --tool-name libFuzzer --tool-version 17 --family fuzzer
```

Only the **fixed track** with a file or standard-input invocation makes sense for a libFuzzer
corpus, because libFuzzer mutates one byte buffer; there is no argument vector to take over. If
the corpus was copied, take the times from snapshots you saved during the campaign (one copy of
the corpus directory every few minutes, each new file stamped with the time of the first snapshot
that contains it) rather than from the file system. A single end-of-campaign snapshot has no
times.

## Test-Comp test vectors

A Test-Comp test suite is a set of XML files, each listing the values that the verifier-style
nondeterministic functions (`__VERIFIER_nondet_int` and its siblings) return in one execution of
a C program. The programs of Agent Software Testing have no such calls: they take arguments, files and
standard input. There is therefore no automatic mapping, and none is defined in v0.1.

A tool that takes part in Test-Comp can still be run on an Agent Software Testing program through a harness
that supplies the values from the program's input. In that case the *harness's* encoding of a
value sequence into bytes (for example, the values rendered in order as the standard input of
the program) defines the case, and the submitter builds the bundle from those bytes with a
script. Agent Software Testing does not define such an encoding. Test-Comp's coverage measurement
(TestCov) is a different measurement of a different program set.

## Writing a bundle with a script

Any program that can write JSON and compute SHA-256 can produce a bundle. This script, tested
against the validator, writes three open-track cases for gzip:

```python
import base64, hashlib, json, os

def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")

def write_bundle(out, tool, program_id, source_sha256, cases, track="open", repeat=1, wall_s=21600):
    """cases: dicts with t_ms, argv (list of bytes), optional stdin (bytes),
    files (list of (path_bytes, content_bytes)) and env (dict of str -> bytes)."""
    os.makedirs(os.path.join(out, "blobs"))

    def blob(data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = os.path.join(out, "blobs", digest)
        if not os.path.exists(path):
            with open(path, "wb") as handle:
                handle.write(data)
        return "sha256:" + digest

    with open(os.path.join(out, "cases.jsonl"), "w") as handle:
        for number, case in enumerate(cases, 1):
            record = {
                "id": f"{number:06d}",
                "t_ms": case["t_ms"],
                "argv": [b64(a) for a in case.get("argv", [])],
                "stdin": blob(case["stdin"]) if case.get("stdin") is not None else None,
                "files": [{"path": b64(p), "kind": "file", "mode": "0644", "blob": blob(c)}
                          for p, c in case.get("files", [])],
                "env": {k: b64(v) for k, v in case.get("env", {}).items()},
                "cwd": b64(b"."),
                "timeout_ms": 30000,
            }
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    with open(os.path.join(out, "tool.json"), "w") as handle:
        json.dump({
            "schema": "agent-software-testing/tool@0.1",
            "tool": {"name": tool, "version": None, "family": "other"},
            "program": {"id": program_id, "source_sha256": source_sha256},
            "track": track, "repeat": repeat,
            "budget": {"wall_s": wall_s},
        }, handle, indent=2)

write_bundle("bundle", "my-tool", "gzip", "<source.sha256 from programs/gzip/program.json>", [
    {"t_ms": 0, "argv": [b"--version"]},
    {"t_ms": 4200, "argv": [b"-c", b"data.txt"], "files": [(b"data.txt", b"hello\n")]},
    {"t_ms": 9100, "argv": [b"-dc"], "stdin": b"\x1f\x8b\x08\x00", "env": {"GZIP": b"-9"}},
])
```

For the **fixed track** write `argv` as the program's `argv_template` with `{input}` replaced
by `input`; add a single file `input` (or, for standard-input programs, `stdin`); and leave
`env` empty. `docs/examples/krep-fixed` is a complete fixed-track bundle.

## Checking the result

```
python -m agent_software_testing validate bundle/
```

lists every violation with the case id and a stable code. A bundle that validates can be
replayed (README, section "Run the reference runner"). Two checks the validator cannot do and
you should: that the `source_sha256` is the one of the program revision you ran your tool on
(the validator only compares it with the pinned archive), and that `t_ms` really is the time
the tool found each case.

## ConcoLLMic cases, recorded

`python -m agent_software_testing adapt concollmic QUEUE_DIR --out BUNDLE ...` converts the queue of a ConcoLLMic run.

**What a ConcoLLMic test case is.** One YAML file per iteration of the search
(`queue/id:000123,src:000120.yaml`). Three fields matter here: `id`, `create_time` (a time stamp without zone, to the
millisecond) and `exec_code`. The `exec_code` is Python source that defines `execute_program(timeout)`; the function
builds the input the model chose (very often in temporary files), runs the program with `subprocess.run`, and returns
the standard error and the exit status. The invocation, the files and the standard input are therefore the output of a
*program*, not bytes. A file whose `exec_code` is empty is an iteration in which the model produced no code; it is not a
test case. The files are read by a small reader of the YAML subset that ConcoLLMic writes; no YAML library is needed.

**Record mode.** The adapter does not let the code start the program. It runs `execute_program` in a separate Python
process, in an empty working directory and an empty temporary directory, with the environment of a case (`PATH`,
`HOME`, `TMPDIR`, `LANG`, `TZ` and nothing else). `subprocess.run`, `Popen`, `call`, `check_call`, `check_output`,
`getoutput`, `os.system`, `os.popen`, `os.exec*`, `os.spawn*` and `os.posix_spawn` are replaced by recorders that note
what was asked for and answer "exit status 0, empty output". At every run the recorder keeps the argument vector, the
standard input, the working directory, the environment, the time limit, the resource limits the code set, and a
snapshot of every file, directory and link that the code had created at that moment. One program run becomes one
case:

| Recorded | In the case |
|---|---|
| `argv[1:]` of the run; the first element must name the program of the bundle (`./krep`, `/x/krep` and `krep` do) | `argv`; an absolute path into the sandbox, also after `--option=`, is rewritten relative to the run's working directory |
| standard input: `input=`, bytes written to a pipe, a file or descriptor passed as `stdin=` (from its current position) | `stdin` |
| a terminal on standard input (`pty.openpty()` and `os.dup2` onto descriptor 0) | `stdin_tty` with an empty input |
| files, directories and links the code created in the working directory and the temporary directory, with permission bits and bytes | `files` (all of them, also those the arguments do not name; temporary names are deterministic) |
| working directory (`cwd=`, `os.chdir`) | `cwd`; the project directory of ConcoLLMic's images (`/<name>-instr`, `/<name>-gcov`, or `--project-dir`) is read as the case root |
| environment compared with the base environment | `env`: only what differs |
| `timeout=` | `timeout_ms`, at most 30,000 and at most `--timeout-ms` |
| several runs | several cases with the same `t_ms`, in order; what an earlier run wrote is not reproduced for a later one |
| `create_time` minus the campaign start | `t_ms` (`--start-time`; without it the earliest case is at 0, which is not the campaign start) |
| the id of the YAML file | `origin`, `concollmic:<id>[.<run>]` (and `:lossy`, below) |

The conversion is deterministic: random numbers and the names of temporary files are seeded by the file name, so
two conversions of one queue give the same cases.

**The original environment.** In the tool's own replay the code runs in the project directory of the program, where
`./name` exists, and in images that also hold the tool's instrumented build (`/woff2-instr` and the like). Code probes
for both and picks its working directory accordingly (`os.path.exists("/woff2-instr/woff2_decompress")`,
`cwd="/confetti-instr"`). Record mode lets `./name` exist in the working directory; `--program-binary FILE` places a real
copy there for code that reads or copies it; `--assume-exists PATH` names further paths of the original environment
(`stat`, `access` and `chdir` answer for them); and `--skip-runs-under PATH` leaves out the runs that went to the tool's
own build, which a replay of the coverage build does not measure.

**What record mode cannot express.** The case is left out of the bundle, with its reason in the report
(`failed_by_reason`, `left_out_by_reason`). `--keep-lossy` writes it without the part that cannot be said instead;
its origin then ends in `:lossy`.

| Reason | What the code did | `--keep-lossy` |
|---|---|---|
| resource limits | `preexec_fn` or `resource.setrlimit` (open files, address space, ...) to provoke an error path | written without the limit |
| reserved environment variable | `LD_PRELOAD`, `MALLOC_*`, `GCOV_*` and the other names of SPEC section 5.7 (the locale variables and `TZ` are not reserved: a variable whose value differs from the recorder's own is kept in `env`) | written without the variable |
| runs another program | a compiler for an `LD_PRELOAD` shim, a converter, another tool of the same project | the other program's runs are dropped, the rest written |
| working directory outside the case | an absolute directory that is neither the sandbox nor a project directory | written with the case root as working directory |
| special files | a named pipe, a device, a link to a path outside the case | written without them |
| terminal on standard input | only in a checkout whose format has no `stdin_tty` | written with empty standard input |
| beyond a limit of the format | the limits of SPEC section 5.6: files, path length and depth, size of a body, number of arguments | never written |
| shell syntax | `shell=True` or `os.system` with a pipe, redirection, glob or variable (a plain command line is understood) | never written |
| code stopped early | the code raised an exception before it ran the program, defines no `execute_program`, has a syntax error, ran more than `--max-runs` times (a search loop), or tried something the guard refuses (a write outside the sandbox, a socket, native code) | never written |

A case whose code raises *after* it started the program (for example `assert "expected" in result.stdout`, which a
recorder cannot satisfy because the program is not run) keeps its runs and is counted separately
(`recorded_with_exception`). Other options: `--drop-after-budget` leaves out cases later than `--wall-s`;
`--allow-native-code` lets the code load shared libraries with `ctypes` (some cases build their input with the
system's Brotli library); `--report FILE` writes one entry per YAML file; `--jobs`, `--record-timeout`,
`--call-timeout`, `--max-runs` and `--work-dir` set how cases are run.

**Safety.** The code in the YAML files was written by a language model and is executed. The recorder refuses network
access, process creation, writes outside its sandbox directories, signals and loading of native code (an audit hook,
resource limits, a wall-clock limit of 60 s per case, a process group that is killed at the end, directories made
accessible again so that the sandbox can be removed). This is a guard against accidents, not a security boundary;
code that is hostile on purpose can get around any measure inside the interpreter. Convert in the container you
replay in, with networking disabled and a read-only root file system (SPEC section 6.7).

**Checked against published branch sets.** Recorded bundles of eleven runs on Confetti, Krep, libmatheval and WOFF2
were replayed with `--profile canonical-2026` on the coverage builds made by `environment/build_programs.py` (Ubuntu
22.04, gcc 11.4, lcov 1.14, linux/amd64, no network) and compared key by key with the published branch sets. The
options `--drop-after-budget`, `--skip-runs-under` for the tool's instrumented builds, `--assume-exists` and
`--program-binary` for the probes of the code, and, for WOFF2, `--allow-native-code` select what the reference pipeline
measured. In no run does the replay cover a branch that the published set lacks. In four runs the covered branches equal
the published set. The other seven lack between one and twelve branches, and the left-out cases are the ones written to
reach them: a failed allocation, a failed `opendir`, a path longer than a buffer, more patterns than an argument list
holds. Replaying a bundle twice gave the same branches (flake 0). The curves agree at the checkpoints except where a
case lies a fraction of a second after a boundary, which the reference pipeline puts in the next checkpoint and the
`canonical-2026` profile, which counts whole seconds, in this one (SPEC section 11.17). Without the selection options,
runs that the tool made on its own instrumented build cover up to 117 more branches than the published sets. LibYAML,
oggenc and bc, the other programs ConcoLLMic supports, are not checked.
