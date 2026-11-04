# Agent Software Testing

One replay for every test generator. A tool runs its campaign wherever it likes and hands in
the test cases it produced, each with the time it was found. A runner replays all cases on
one pinned gcov build of the program, in one pinned image and reports branch coverage the
same way for every tool: a symbolic executor, a fuzzer, a hybrid, a language-model system
or an agent that plans campaigns.

Specification version 0.1. The specification, program definitions, validator,
reference runner and adapters are here. The toolchain image builds from this repository (with its
base image pinned, see [environment/README.md](environment/README.md)) and the coverage builds of all 18
programs are made in it. The branch universe of each equals the published one key by key. Confetti's
three reference corpora replayed through the reference runner agree key by key with the reference measurement
([docs/canonical-profile.md](docs/canonical-profile.md)).

## Why

Evaluations of test generators usually measure coverage inside the tool's own pipeline: its
instrumentation, its build, its treatment of arguments and files. Two tools are then not
measured by the same ruler and tools that choose command-line arguments, create files or
read standard input are hard to compare with tools that fuzz one input file. Agent Software Testing
replays the test cases of every tool on one pinned build of each program.

Agent Software Testing does not claim to measure bug-finding ability, tool efficiency or anything but
branch coverage of the replayed cases (and, optionally, counts of sanitiser findings). Its
known limitations are in SPEC section 11.

## Quick start

### Install

The runner needs Python 3.10 or later and no package outside the standard library. Either install it from this
checkout or put `src/` on the path; the commands below use the installed form:

```
python3 -m pip install -e .
python3 -m agent_software_testing --help

# or, without installing:
export PYTHONPATH=$PWD/src
python3 -m agent_software_testing --help
```

### Submit results

1. Run your tool on a program in [programs/](programs/README.md) and keep every test case
   with the time it was produced.
2. Write a bundle: `tool.json`, `cases.jsonl` and `blobs/` (SPEC section 5). For KLEE test
   files and AFL++ queues use the adapters; for anything else write the files directly,
   which takes a few lines of any language ([docs/adapters.md](docs/adapters.md)).
3. Validate it:

   ```
   python3 -m agent_software_testing validate path/to/bundle
   ```

   Two complete example bundles are in [docs/examples/](docs/examples/):
   `krep-fixed` (fixed track) and `gzip-open` (open track).
4. Replay it (below) or send the bundle to the maintainers of a results release. At least
   three independent repeats per tool and program are needed for a result (SPEC section 9.1).

### Run the reference runner

The runner needs the pinned image with the coverage build of the program, started without
network access. The image is not published to a registry; build it from this checkout's
`environment/Dockerfile` (`docker build --target builds -t agent-software-testing:builds -f environment/Dockerfile .`;
it compiles the coverage and sanitiser builds of all 18 programs into `/opt/agent-software-testing/builds`, which
[environment/README.md](environment/README.md) describes). The image holds the builds but not the runner, so
mount the checkout and put its `src/` on the path; `docs/examples/afl-walkthrough.md` has a complete container
line. The container must see only its own processes, which Docker does by default and Singularity and Apptainer do
with `--pid` (SPEC section 6.7). From the checkout:

```
python3 -m agent_software_testing validate <bundle>

python3 -m agent_software_testing replay <bundle> \
    --build <coverage build root> \
    --out <output directory>
```

The default profile is the protocol of the specification. `--profile canonical-2026`
reproduces the reference measurement pipeline (whole-second bucketing, the last snapshot takes
every remaining case, a 30 s limit for every case, no resource limits) so that one corpus can
be run through both and the numbers compared; what it still does not reproduce is listed in
[docs/canonical-profile.md](docs/canonical-profile.md) and SPEC section 11.17.

Two properties of the machine that replays can change coverage and `replay.json` reports both. A case whose
environment has no `TZ` (the recorded environments of `canonical-2026`) runs in the zone of the machine or container, so a
node that is not in UTC or a container that binds the host's `/etc/localtime` gives other results; the runner records the
zone (`local_zone`) and, under `canonical-2026`, warns on standard error when it is not UTC. And the limit of a case is
wall-clock time, so a busy node turns slow directory walks into timeouts; `timeouts` counts the timeouts that used less than half
of their limit as CPU time and lists their ids. `--timeout-clock cpu` makes the limit CPU time instead (a deviation from the
reference pipeline; see [docs/canonical-profile.md](docs/canonical-profile.md)).

The program is found from the bundle's `tool.json` in `programs/`; pass
`--program programs/<id>/program.json` to name it. `<coverage build root>` is the directory
`.../builds/<id>/gcov` produced by `environment/build_programs.py`; it must be at the path it
was compiled at (`--compile-root` if it was moved). The output directory must not exist or
must be empty. It receives `replay.json`, `timeline.csv`, `coverage-branches.json` and
`snapshots/` (SPEC section 7). Run `replay --help` for the remaining options (workers,
snapshot interval, the number of lcov runs at once and their time limit, skipping the flake
check). Each worker shard holds a full copy of the build, so the scratch space (`--work-dir`)
needs the size of the build times the number of workers.

By default each shard is served by a worker process of its own, started once and used for the
whole replay (about 25 MB of memory each); `--shard-mode thread` runs all shards as threads of
one process instead, which does not scale beyond a few cores. The coverage results are the same
in both modes and `replay.json` records the mode. `tools/bench_shards.py` measures the cases
per second of both modes on a synthetic bundle (it needs gcc; see its header).

The optional sanitiser pass counts bug signatures on the ASan+UBSan build:

```
python3 -m agent_software_testing signatures <bundle> \
    --build <sanitiser build root> \
    --out <output directory>
```

`--binary` is not needed: the sanitiser binary is the program's `binary` in `program.json` with the
`obj-gcov/` prefix replaced by `obj-native/` (for Krep, `obj-native/krep`).

It writes `signatures-counts.json` (public: counts by sanitiser and kind) and
`signatures-private.json` (frames and example cases; never publish it, SPEC section 8).

Convert existing outputs of other tools into bundles:

```
python3 -m agent_software_testing adapt ktest KTEST_DIR --out BUNDLE --program-id ID --tool-name NAME ...
python3 -m agent_software_testing adapt afl --queue DIR --out BUNDLE --program-id ID --tool-name NAME ...
```

`adapt --help` lists the options; the mapping rules are in [docs/adapters.md](docs/adapters.md).

Replaying runs untrusted test cases through programs that can write files and start
processes. Use the container of `environment/Dockerfile` with networking disabled and a
read-only root file system (SPEC section 6.7); the runner does not enforce this itself.
Give each worker shard its own process namespace or cgroup if you can (SPEC section 11.15) and
remember that a bundle is trusted not to forge coverage: a program can write its own profile
data (SPEC section 11.14).

## What is measured

* **Branch coverage** as gcov and lcov report it for a `-O0 --coverage` build of the program
  in a pinned Ubuntu 22.04 image (gcc 11.4, lcov 1.14). A branch is the key
  `file:line:block_id:branch_id`. Every program has a fixed universe of branches
  (`branch_total` in its `program.json`) and a replay whose universe differs is rejected.
* **Over time.** Cases carry the time the tool found them. The runner reports cumulative
  coverage every 900 seconds up to the budget, so curves are comparable across tools.
* **Two tracks.** In the *fixed track* the invocation is given and the tool supplies the
  input file or standard input. In the *open track* the tool chooses arguments, files,
  standard input and environment.
* **Replay noise** is measured for every replay by replaying the corpus a second time.
* **Sanitiser findings** (optional, a separate command): the same cases on an
  AddressSanitizer and UndefinedBehaviorSanitizer build. Public output contains counts of
  distinct signatures by kind only.

## Repository layout

```
SPEC.md              the specification (normative)
README.md  LICENSE  NOTICE
CONTRIBUTING.md  CODE_OF_CONDUCT.md  SECURITY.md  GOVERNANCE.md
.github/             issue forms and the pull request template
docs/
  adapters.md        how KLEE, AFL++, libFuzzer, Test-Comp and other outputs map to bundles
  canonical-profile.md  the profile that reproduces the reference pipeline, and what still differs
  PROPOSING-A-PROGRAM.md  what a new program has to meet
  examples/          two small, complete example bundles
programs/            one program.json per program, README with the table
environment/         Dockerfile, toolchain lock, build script, verify script, their tests
  third_party/       third-party build inputs for Krep and libmatheval, with their own licences
schemas/             JSON Schemas of the data formats
src/agent_software_testing/    reference runner, validator and adapters (Python standard library only)
tests/               tests of the runner
pyproject.toml  Makefile            packaging and test targets
```

Tests: `make test` runs both suites, `tests/` (the runner) and `environment/` (the build
scripts); `make test-runner` and `make test-environment` run one each.

Companion repositories (separate): `agent-software-testing-results` holds versioned results and
derived tables; `agent-software-testing-site` generates the website from it.

## Programs

Eighteen programs: bc, Confetti, diff, du, gawk, Gcal, grep, gzip, jq, Krep, libmatheval,
LibYAML, ls, oggenc, sed, WOFF2, xmlwf and xorriso, with 591 to 54,607 branches each. Six
have a fixed track. See [programs/README.md](programs/README.md).

## Contributing

Bug reports, fixes, adapters and program proposals are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) says where to
start, how to run the tests on your machine (`make test`) and how a change to the specification is proposed. Everyone
who takes part follows the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems are reported privately, see
[SECURITY.md](SECURITY.md). How decisions are made is in [GOVERNANCE.md](GOVERNANCE.md) and what a new program has
to meet is in [docs/PROPOSING-A-PROGRAM.md](docs/PROPOSING-A-PROGRAM.md).

## Licences

The code, the specification and the documentation are under the Apache License 2.0 ([LICENSE](LICENSE),
[NOTICE](NOTICE)). [environment/third_party/](environment/third_party/concollmic/README.md) holds third-party build
inputs (two patches for Krep and a driver for libmatheval) under their own licences; they are not covered by the
Apache License. The programs are fetched from their upstream sources and keep their own licences; this repository
does not contain their code, except the small shim in `programs/jq/compat/`.

Copyright The Agent Software Testing authors.
