# The `canonical-2026` profile: equivalence with the reference pipeline

The branch counts of the reference evaluation were produced by a replay pipeline of its own.
Agent Software Testing's standard protocol (SPEC section 6) differs from it in ways that change
coverage. `--profile canonical-2026` makes the reference runner do what the reference pipeline did in the nine
places where it matters most, so that **one corpus can be replayed through both and the
results compared** before a number of the reference pipeline and a number of Agent Software Testing
stand in the same table. Nothing in this profile is meant for published results; they use
the default profile.

```
python -m agent_software_testing replay BUNDLE --build BUILD --out OUT --profile canonical-2026 --workers 20
```

`replay.json` records `"profile": "canonical-2026"`.

## What the profile reproduces

| | `standard` (SPEC section 6) | `canonical-2026` |
|---|---|---|
| Time limit of a case | `timeout_ms` of the case, default 30,000 | 30,000 ms for every case; the case's own `timeout_ms` is ignored. Both are wall-clock time; `--timeout-clock cpu` is not part of the profile (below) |
| Resource limits | core 0, file size 256 MiB, 1,024 open files, CPU `ceil(timeout/1000) + 5` s | none (the container has to bound the program) |
| Snapshot that takes a case | the first `T` with `t_ms <= 1000 * T` | the first `T` with `t_ms // 1000 <= T`: whole seconds, so a case found at 900.999 s is in the snapshot of `T = 900` |
| The last snapshot (`T = W`) | the cases with `t_ms <= 1000 * W`; later ones are counted as skipped | every remaining case, also those after the budget (the campaign's orderly shutdown); `skipped_beyond_budget` is 0 |
| Locale | `LANG=C`, unless the case sets a locale variable itself: then the case's value | the case's own locale variables, as in the standard profile. Otherwise `LC_ALL=C` (the reference pipeline's base locale), unless the case's own environment names a locale (`LANG`, `LANGUAGE`, `LC_*` other than `LC_ALL`): then no base locale variable, and the recorded variables stand. The reference pipeline ran a program with exactly the environment recorded next to a KLEE test, which has `LANG=C` and `LC_CTYPE=C.UTF-8`; `LC_ALL=C` would override that and switch off the multibyte code of grep, sed, du, ls and others (grep: about 300 branches). |
| Time zone | `TZ=UTC`, unless the case sets `TZ` itself: then the case's value | the case's own `TZ`, as in the standard profile. Otherwise `TZ=UTC`, unless the case's own environment names a locale (a recorded environment): then no `TZ`, because the reference pipeline's programs had none and gcal asks `getenv("TZ")` (`src/file-io.c:1418`, `src/gcal.c:658`: 3 to 4 branches). A case without `TZ` runs in the zone of the machine or container (`/etc/localtime`); the profile takes it to be UTC and the runner checks (below) |
| Standard input file | the file `stdin` next to the working directory, opened read-only | for every case whose standard input is a file (`stdin` set, neither `stdin_pipe` nor `stdin_tty`) the file `fd0` in the working directory, opened for reading and writing: `klee-replay` created it there, so `grep -r .`, `ls`, `du` and the programs that walk the directory list it (a sample of 1,000 grep tests covered 2,511 branches without it and 2,557 with it, as `klee-replay` does). Only the case matters; the runner never reads the tool or the `origin`. A case that has a file of that name keeps standard input outside. Both profiles give a case with `stdin_pipe` a pipe and no file, and set `stdin_atime` and `stdin_mtime` on the standard input file |
| Output | standard output and standard error go to `/dev/null` | one pipe for both, read and discarded by the runner. The reference pipeline captured the output of every program, and a program can tell the difference: grep stops printing when standard output is `/dev/null`, so the branches that print a line are covered only with a pipe (a grep run covers 233 more branches with a pipe) |
| `argv[0]` | the file name of the binary | the path of the shard's copy of the binary: `klee-replay` and the replay scripts of the reference pipeline passed the path, and programs branch on a `/` in it (found on xmlwf, whose `--version` covers one more branch with a path) |

The flake check replays the cases that the profile replayed in the first pass.

A bundle that has cases after its budget is valid in both profiles. The validator warns that
such cases "will not be replayed", which is true of the standard profile only.

## What the replay machine decides: the time zone and the load

Two properties of the machine that replays change coverage without being part of the corpus. The runner measures
both and puts them in `replay.json`. A third is fixed by the standard, the process view: the container sees only its
own processes, which Docker does by default and Singularity and Apptainer do with `--pid`. Without it a case that walks
the file system can reach the node's disks through `/proc/<pid>/root`.

**The time zone.** A recorded environment has no `TZ`, and the profile adds none, so the programs of such a case use
the zone that the C library finds in `/etc/localtime`. The reference image is in UTC, and the profile takes that to be the zone
of the cases. Singularity and Apptainer bind the host's `/etc/localtime` into the container by default, so on a node in
a zone such as Asia/Baku (UTC+04) every case without `TZ` runs at `+04`: `localtime`, `mktime` and `strftime` give other values, and the
branches that depend on the hour, the day boundary or the printed zone differ. `replay.json` records the zone of a process
without `TZ` (`local_zone`: offset and abbreviation at the start of the replay, whether it is UTC, the zone `/etc/localtime`
links to, the SHA-256 of the file it leads to, and the `TZ` of the runner's own environment, which says nothing about the cases
because a launcher usually sets it). Under `canonical-2026` the runner also warns when the zone is not UTC, on standard error and in `warnings`. A container
fixes it by binding a UTC zone file over `/etc/localtime` (Singularity or Apptainer: `--bind <file>:/etc/localtime`).
The warning is the only thing that happens; the replay goes on.

**The load of the node.** The limit of 30 s is wall-clock time, as it was in the reference pipeline. A case that walks a
directory tree (`du /`, `ls -R /`, `krep -r /`) takes 10 to 14 s alone and
runs into the limit when sixteen replays share a node, and the coverage that the walks add comes and goes with the
timeouts. For every case that times out the runner measures the CPU time its processes used (from `/proc` before the
kill on Linux, and from the resource usage of the main process) and `replay.json` records, under `timeouts`, how many
timeouts used less than half of their limit as CPU time (`under_load`, 15 s of 30 s) and the ids of the first 100. A timeout
that computed for the whole limit is a real one; a timeout with a few seconds of CPU was waiting for a busy node. The
profile keeps the wall-clock limit, so its numbers are those of the reference pipeline. For an experiment that asks what the
coverage would be without the load, `--timeout-clock cpu` (also in the standard profile) gives the program `RLIMIT_CPU` equal
to the limit, so that only CPU time counts, and stops a case that uses no CPU at four times the limit in wall-clock time. It
**departs from the reference pipeline**: a case that is slow but not busy is not a timeout, and a case that only waits
takes four times as long to stop. The runner warns when the option is combined with `canonical-2026`, and `timeouts.clock`
records which clock ran. Results of the two clocks do not stand in one table.

## What the profile does not reproduce

These differences remain, so a disagreement between the two pipelines can still come from
them. They are listed in SPEC section 11.17; this is the same list with what to do about each.

1. **Input path.** The reference pipeline passed the absolute path of a file named by the
   SHA-256 of its content, in a directory shared by all cases. Agent Software Testing's fixed track passes
   `input` (open-track bundles: the path the case chose) in a fresh case root. A program that
   derives a file name from the input path or branches on it behaves differently:
   `woff2_decompress` writes its output next to the input, `oggenc` names its output after the
   input. For LibYAML the reference pipeline gave the input as a file argument, while the fixed
   track here (and the evaluation's harness and fuzzer launcher) use standard input; whether
   that changes the covered branches has not been measured. *If the programs involved disagree,
   compare them one by one.*
2. **Working directory.** The reference pipeline ran every case in one directory per shard that
   was emptied before each case, with `HOME` in it and a `TMPDIR` that was never cleaned.
   Agent Software Testing uses a fresh case directory with private `home` and `tmp`. A program that leaves
   something in `TMPDIR` for the next case sees it in one pipeline and not in the other.
3. **Number of shards.** The reference pipeline used 20; the reference runner uses one per core
   (pass `--workers 20` for the equivalence run). The assignment of cases to shards differs in
   any case, which matters through the shared absolute paths of SPEC section 11.8.
4. **How the captures are merged.** The reference pipeline merged the lcov captures of the shards
   (`lcov -a`) and read the merged file once. The reference runner reads each shard's capture
   and unions the sets. The two give the same keys when every shard's capture has the same
   instrumented lines as the initial capture; if not, the runner fails (a key outside the
   universe) or drops a branch.
5. **Failed captures.** The reference scripts ran lcov with its output discarded and only
   checked that an output file existed; the reference runner stops on a failed, empty or
   timed-out capture.
6. **The runner's own additions.** One more variable in the environment (`AGENT_SOFTWARE_TESTING_CASE`),
   the stray-process scan, the re-check of every case before it runs, and a layout of the shard
   (a read-only copy of the build and a separate profile tree) that the reference pipeline did not
   have. None should change what a program covers. The stray-process scan reads the descendants
   of the shard's worker process and not every process of the node (`--shard-mode
   process`; SPEC section 6.3 step 7): the worker is a child subreaper, so a process that left
   its case's group is still below it, and the guarantee that no process started by a case
   survives it is the same as when the scan read all of `/proc`. The first eight digits of the
   marker are shared by the cases of one replay (the other 24 stay random per case), which
   changes the value of one environment variable and not its length.
7. **Which cases exist.** De-duplication, the origin of the times (file name or modification
   time) and the choice of directories are the concern of the adapter or script that makes the
   bundle. For AFL++ the adapter keeps identical bytes once at their earliest time, as the
   reference pipeline did, and reads the time from the file name (`time:` in milliseconds).
8. **The date and the host.** gcal prints the calendar of the current month: the same tests
   cover 3,310 to 3,332 branches depending on the day they are replayed. A test that names `..`,
   `/tmp` or another absolute path reads what the host holds there: xorriso tests that put `..` into an
   image read the parent of the working directory, which a replay on another host cannot reproduce. A
   comparison has to state the day, or replay the reference pipeline's `klee-replay` next to the runner. The profile does
   not pin the clock to a day: that would reproduce a published number, not what the tool's
   tests do on the day they are replayed, so the day is recorded instead.
9. **Standard input as a pipe, and its times.** `klee-replay` delivers a standard input whose stat object records a
   named pipe or no file type through a pipe and makes no `fd0`, and gives every other `fd0` the access and
   modification time of the stat object. A case says both with `stdin_pipe`, `stdin_atime` and
   `stdin_mtime` (SPEC.md 5.4), and the KTest adapter writes them. `klee-replay` keeps the write end of its
   pipe open until the program has exited or half of its timeout has passed; the runner closes it after the last byte.
10. **Standard output of the KLEE baseline replays.** The replays that measured the KLEE baselines (one and six
   workers) sent standard output to `/dev/null`. grep tests whether its output is `/dev/null` and prints nothing then,
   so the pipe covers its printing code, and a 34-branch libburn block of the fixed six-worker xorriso runs is a
   candidate of the same kind. The profile keeps the pipe for every case: a
   bundle cannot say how its tool's replay treated the output, and one rule for all tools is the point of the
   profile. A comparison with a published baseline set of grep has to allow for it.

## An equivalence run

1. Take a corpus that the reference pipeline replayed, with the second at which each case was
   found.
2. Write it as a bundle (`adapt afl` for AFL++ output, whose names carry the time; a script
   otherwise). Use `t_ms = 1000 * seconds` if only whole seconds are known: the profile buckets
   by whole seconds, so nothing is lost.
3. Replay it with `--profile canonical-2026 --workers 20`.
4. Compare `coverage-branches.json` with the branch set of the reference pipeline for the same
   corpus, key by key (both use `file:line:block_id:branch_id`). Compare the snapshots of
   `timeline.csv` with the reference checkpoints.
5. Replay it again with the default profile. The difference between the two Agent Software Testing
   results is what items 1 to 4 of the first table cost; the difference between the first and
   the reference pipeline is what the list above costs.
6. Look at `flake` of both replays: a difference smaller than the replay noise is not a
   difference (SPEC section 11.2).

## What equivalence has been shown

The coverage builds of all 18 programs give the published branch universe key for key. With the
profile, the three reference corpora of Confetti give the published branch set key for key, as do the
corpora of libmatheval. For the programs that take command-line arguments, the adapter follows
`klee-replay` as SPEC section 11.19 describes, and a real `klee-replay` built from the KLEE 3.1 sources
reproduces the published numbers of Krep (526, the published set key for key), xmlwf (1,187) and sed (1,202).
In samples of up to 1,000 tests the runner and `klee-replay` cover the same branches or differ by at most four,
except for xorriso, where the host decides (item 8 above).

| Program | `klee-replay` | Runner | Only in `klee-replay` | Only in the runner |
|---|---:|---:|---:|---:|
| bc | 1,371 | 1,371 | 0 | 0 |
| diff | 1,963 | 1,961 | 2 | 0 |
| du | 1,564 | 1,566 | 2 | 4 |
| gawk | 5,580 | 5,582 | 0 | 2 |
| gcal | 4,940 | 4,940 | 0 | 0 |
| grep | 2,557 | 2,557 | 0 | 0 |
| jq | 4,721 | 4,722 | 0 | 1 |
| ls | 1,233 | 1,235 | 0 | 2 |
| oggenc | 300 | 300 | 0 | 0 |
| sed | 3,397 | 3,398 | 0 | 1 |
| xmlwf | 2,281 | 2,278 | 3 | 0 |
| xorriso | 12,248 | 11,899 | 351 | 2 |

A number of the reference pipeline and a number of Agent Software Testing may stand in one table only for programs
and conditions whose corpora have been replayed through both and agree; for all others the table has to show the
difference measured next to the number.
