# Walkthrough: an AFL++ campaign from the distribution package to a replayed bundle

This follows a tool that Agent Software Testing did not build, AFL++ 4.00c from the `afl++` package of Ubuntu 22.04, on two
programs of the **fixed track**: Krep (`krep test <file>`) and LibYAML (`run-parser-test-suite`, standard input).
Each campaign is one `afl-fuzz` process for 20 minutes with one minimal seed. It ends with the bundle replayed on the
coverage build, the sanitiser pass, and the archive and manifest a submission needs. One repeat per program is run
here; a submission needs three (`docs/SUBMITTING.md` of the results repository).

The commands ran in `linux/amd64` containers on an Apple-silicon Mac with Docker Desktop (Rosetta), so
the execution speeds (about 300 per second) are low; on native x86-64 expect more. All commands below are bash.

You need a checkout of this repository (`CR`), Docker, and the replay image (`IMAGE`) that holds the coverage builds at
`/opt/agent-software-testing/builds/<id>/gcov` ([environment/README.md](../../environment/README.md); the image of the
`builds` target is `agent-software-testing:builds`).

```
RUNNER=/path/to/agent-software-testing          # this repository
WORK=$PWD/work                    # holds everything below; it must be writable by uid 10001 (the image's user)
IMAGE=agent-software-testing:builds
mkdir -p "$WORK"
```

## 1. Build and fuzz (about 4 minutes of setup, 20 minutes of fuzzing)

Save as `fuzz.sh`. It runs in a fresh Ubuntu container, installs AFL++ from apt, downloads the two pinned sources and
checks their SHA-256 (the values are in `programs/<id>/program.json`), builds them with `afl-clang-fast`, and fuzzes
both at once (each `afl-fuzz` binds to its own core). Krep gets the two patches that `environment/build_programs.py`
applies; LibYAML is built with the configure arguments of its `program.json`. The fuzzer command lines are the fixed
invocations of the programs: `krep test @@` (`@@` stands for `{input}`) and `run-parser-test-suite` with standard input.

```
#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends afl++ curl ca-certificates

mkdir -p /fuzz/src && cd /fuzz/src
curl -fsSL -o krep.tar.gz https://github.com/davidesantangelo/krep/archive/9c4d41ce92f4e9ef6dc786eaf0e5be488153c4b4.tar.gz
curl -fsSL -o yaml.tar.gz https://github.com/yaml/libyaml/releases/download/0.2.5/yaml-0.2.5.tar.gz
echo "18298b91d2edd24f0e2e56a703c3fb372d218bfa63038b689f4b084bd8b1a87e  krep.tar.gz" | sha256sum -c -
echo "c642ae9b75fee120b2d96c712538bd2cf283228d2337df2cf2988e3c02678ef4  yaml.tar.gz" | sha256sum -c -

mkdir krep && tar -xf krep.tar.gz --strip-components=1 -C krep
mkdir krep/obj-afl && cd krep/obj-afl
patch --batch --forward -p1 --directory=.. --input=/cr/environment/third_party/concollmic/krep/Makefile.diff
patch --batch --forward -p1 --directory=.. --input=/cr/environment/third_party/concollmic/krep/krep.diff
CC=afl-clang-fast make -f ../Makefile VPATH=..
cd /fuzz/src

mkdir yaml && tar -xf yaml.tar.gz --strip-components=1 -C yaml
mkdir yaml/obj-afl && cd yaml/obj-afl
CC=afl-clang-fast ../configure --disable-shared --enable-static
CC=afl-clang-fast make
cd /fuzz

mkdir in-krep in-yaml
printf 'hello test world\ntest again\n' > in-krep/seed1
printf 'a: 1\nb: [x, y]\n' > in-yaml/seed1
export AFL_NO_UI=1
afl-fuzz -i in-krep -o out-krep -V 1200 -- src/krep/obj-afl/krep test @@ > krep.log 2>&1 &
afl-fuzz -i in-yaml -o out-yaml -V 1200 -- src/yaml/obj-afl/tests/run-parser-test-suite > yaml.log 2>&1 &
wait
grep -h -E '^(start_time|run_time|execs_done|corpus_count|saved_crashes)' out-krep/default/fuzzer_stats out-yaml/default/fuzzer_stats
```

```
docker run -d --name afl-jammy --platform linux/amd64 --cpus 3 -v "$RUNNER":/cr:ro ubuntu:jammy-20251001 sleep infinity
docker exec -i afl-jammy bash -s < fuzz.sh
```

A fresh Ubuntu container has neither `curl` nor a certificate bundle, hence the install line. After the trace line of
the final `grep` (`set -x`), the output ends with the stats of the two campaigns (the values vary from run to run):

```
start_time        : 1792222389
run_time          : 1200
execs_done        : 342787
corpus_count      : 71
saved_crashes     : 0
start_time        : 1792222389
run_time          : 1200
execs_done        : 408244
corpus_count      : 894
saved_crashes     : 0
```

## 2. Copy the AFL++ output out

Wait until both `afl-fuzz` processes have exited (`-V 1200` makes them stop; `fuzz.sh` returns when they have).
Copying a directory that is still being written fails with `unexpected EOF`. A single instance writes to
`out-*/default/`, so the queue is `out-*/default/queue`.

```
docker cp afl-jammy:/fuzz/out-krep "$WORK/out-krep"
docker cp afl-jammy:/fuzz/out-yaml "$WORK/out-yaml"
ls "$WORK/out-krep/default/queue" | head -3
```
```
id:000000,time:0,execs:0,orig:seed1
id:000001,src:000000,time:25,execs:15,op:havoc,rep:2,+cov
id:000002,src:000000,time:55,execs:24,op:havoc,rep:8,+cov
```

The `time:` field is the discovery time in milliseconds; the seed is at 0. The queue holds 71 files for Krep and 894 for
LibYAML.

## 3. Convert, validate, replay (about 80 seconds)

The replay container has no network, a read-only root file system and a scratch directory that may execute files
(`exec`: the runner runs copies of the build from there; a plain `--tmpfs /tmp` is `noexec` and every case then fails
with `Permission denied`).

```
RUN="docker run --rm --platform linux/amd64 --cpus 3 --network none --read-only --pids-limit 4096 \
  --tmpfs /tmp:exec,size=2g -e PYTHONPATH=/cr/src -e PYTHONDONTWRITEBYTECODE=1 \
  -v $RUNNER:/cr:ro -v $WORK:/work -w /work $IMAGE"
cd "$WORK"
```

Krep (`--track fixed` is needed: the default is `open`; `--wall-s` is the budget of the campaign, 20 minutes):

```
$RUN python3 -m agent_software_testing adapt afl \
    --queue out-krep/default/queue \
    --out bundle-krep --program-id krep --track fixed \
    --tool-name AFL++ --tool-version 4.00c --family fuzzer --tool-url https://aflplus.plus/ \
    --cores 1 --wall-s 1200 \
    --notes "Ubuntu 22.04 package afl++ 4.00c-1ubuntu1, afl-clang-fast, one instance, one minimal seed"
$RUN python3 -m agent_software_testing validate bundle-krep
$RUN python3 -m agent_software_testing replay bundle-krep \
    --build /opt/agent-software-testing/builds/krep/gcov --out replay-krep --workers 3
```
```
warning: tool.json: [tool.budget_nonstandard] budget.wall_s is 1200; the standard budget is 21600
bundle-krep: valid bundle with 71 case(s)
{
  "source_cases": 71,
  "written_cases": 71,
  "duplicates": 0,
  "skipped": 0,
  "skipped_examples": [],
  "notes_total": 0,
  "notes": []
}
warning: tool.json: [tool.budget_nonstandard] budget.wall_s is 1200; the standard budget is 21600
bundle-krep: valid (71 cases)
[replay] capturing the branch universe
[replay] preparing 3 shard(s)
[replay] t=0s: 1 cases replayed, 183 branches covered
[replay] t=900s: 71 cases replayed, 196 branches covered
[replay] t=1200s: 71 cases replayed, 196 branches covered
[replay] replaying the final corpus a second time (flake check)
krep: 196/1231 branches covered; 71 cases replayed, 0 beyond the budget; 0 branch(es) differ on a second replay. Results in replay-krep
```

Times: adapt 2 s, validate 1 s, replay 7 s (each includes the start of a container). The warning appears because the
budget is not the standard six hours; it is a label, not an error. What matters is the line `valid`.

LibYAML (same commands, other names; `adapt` takes 9 s, `validate` 3 s, `replay` 54 s; the output below leaves out the
lines that are the same as for Krep):

```
$RUN python3 -m agent_software_testing adapt afl \
    --queue out-yaml/default/queue \
    --out bundle-libyaml --program-id libyaml --track fixed \
    --tool-name AFL++ --tool-version 4.00c --family fuzzer --tool-url https://aflplus.plus/ \
    --cores 1 --wall-s 1200 \
    --notes "Ubuntu 22.04 package afl++ 4.00c-1ubuntu1, afl-clang-fast, one instance, one minimal seed"
$RUN python3 -m agent_software_testing validate bundle-libyaml
$RUN python3 -m agent_software_testing replay bundle-libyaml \
    --build /opt/agent-software-testing/builds/libyaml/gcov --out replay-libyaml --workers 3
```
```
bundle-libyaml: valid bundle with 894 case(s)
bundle-libyaml: valid (894 cases)
[replay] t=0s: 1 cases replayed, 528 branches covered
[replay] t=900s: 764 cases replayed, 1953 branches covered
[replay] t=1200s: 894 cases replayed, 2038 branches covered
libyaml: 2038/4275 branches covered; 894 cases replayed, 0 beyond the budget; 0 branch(es) differ on a second replay. Results in replay-libyaml
```

| Program | Cases | Seed alone (t = 0) | After 20 minutes | Branch total |
|---|---:|---:|---:|---:|
| Krep | 71 | 183 | 196 | 1,231 |
| LibYAML | 894 | 528 | 2,038 | 4,275 |

A second, independent replay of the same bundles gave the same numbers (196 and 2,038).

## 4. Sanitiser pass (about 75 seconds to build, 2 s and 16 s to run)

`docs/SUBMITTING.md` asks for this before a bundle is published. It needs the `asan-ubsan` build. If your image has
none (the `builds` target of the Dockerfile makes them), build the two programs once, with network, into `$WORK/san`:

```
docker run --rm --platform linux/amd64 --cpus 3 -e PYTHONDONTWRITEBYTECODE=1 -v "$RUNNER":/cr:ro -v "$WORK":/work $IMAGE \
    python3 /cr/environment/build_programs.py --variant sanitizer --jobs 3 --out /work/san krep libyaml
```
```
building krep (sanitizer)
  built /work/san/krep/asan-ubsan
building libyaml (sanitizer)
  built /work/san/libyaml/asan-ubsan
```

Then, offline as before (`obj-native/` replaces the `obj-gcov/` prefix of `binary` in `program.json`):

```
$RUN python3 -m agent_software_testing signatures bundle-krep --build /work/san/krep/asan-ubsan \
    --binary obj-native/krep --out sig-krep
$RUN python3 -m agent_software_testing signatures bundle-libyaml --build /work/san/libyaml/asan-ubsan \
    --binary obj-native/tests/run-parser-test-suite --out sig-libyaml
```
```
krep: 0 distinct signature(s) from 0 case(s) with reports. Results in sig-krep
libyaml: 0 distinct signature(s) from 0 case(s) with reports. Results in sig-libyaml
```

Neither bundle contains an input that makes a sanitiser report, so both can be published without an embargo
(`DISCLOSURE.md` of the results repository).

## 5. Archive, hash, manifest

The archive holds the files of the bundle at its top level. Make it in the container (a `tar` on the host may add
metadata files), and check that the unpacked bundle still validates:

```
for p in krep libyaml; do
  $RUN tar -czf $p-fixed-r1.tar.gz -C bundle-$p .
  $RUN sha256sum $p-fixed-r1.tar.gz
  mkdir unpack-$p && $RUN tar -xzf $p-fixed-r1.tar.gz -C unpack-$p
  $RUN python3 -m agent_software_testing validate unpack-$p
done
```
```
<64 hex digits>  krep-fixed-r1.tar.gz
unpack-krep: valid (71 cases)
<64 hex digits>  libyaml-fixed-r1.tar.gz
unpack-libyaml: valid (894 cases)
```

(`warning: tool.json: [tool.budget_nonstandard]` lines precede each `valid`.) The archives are 13 KB and 107 KB. After
they are uploaded, write the manifest of the example in `submissions/README.md` of the results repository (one `tool`,
`track: fixed`, `budget` of 1200 s and 1 core, `hardware`, and one `bundles` entry per program and repeat with the
address and the hash), and check its structure from the results repository with the command given there. It prints
nothing when the manifest is valid:

```
python -c "import json, jsonschema; jsonschema.validate(json.load(open('manifest.json')), json.load(open('submissions/schema.json')))"
```

## What to watch for

* `--track fixed` is not the default. On a program with a fixed track, leaving it out gives a bundle with empty
  arguments and the bytes on standard input; it validates with a `case.unlisted_surface` warning and replays the wrong
  invocation.
* The user that runs `adapt` must be able to read the queue. AFL++ writes the queue files it finds with mode 0600; a
  file that cannot be read is skipped, listed only in the JSON report (`"skipped"`), and the command still exits 0. Compare
  `source_cases` and `written_cases` with `ls out-*/default/queue | wc -l`.
* The repository has no seeds. Say in `--notes` which seeds you used; the seed alone already gives 183 of Krep's 196
  branches.
* The replay needs `exec` scratch space and, with the default `--workers` (the host's CPU count, not the container's
  `--cpus`), copies the build once per worker; give `--workers` the number of CPUs you gave the container.
* One repeat is not a result: run three independent campaigns of each program with the same budget before submitting.
