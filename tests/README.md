# Tests of the runner

A published number is wrong when a branch is counted twice, a case lands in the wrong snapshot or a failed capture is read
as zero. A bundle is hostile when it tries to leave its directory, change the environment of the program or hang the
validator. The tests here are the few that stop those things, each tied to a promise in the list below. A test that
cannot be tied to a promise does not belong here.

The tests go through the command line and the files it reads and writes. The few that call a function call one that the
specification defines (the key rules, the kind tables). A fake toolchain (`fake_toolchain.py`) stands in for gcc, gcov and
lcov, so every test runs on any machine; it lists what the fake program covers for each input.

## Running them

| Command | What it does |
|---|---|
| `python3 -m pytest tests` | The everyday run. Needs only pytest. A few tests need `/proc` and are skipped where there is none. |
| `AGENT_SOFTWARE_TESTING_IMAGE=<image with Python 3.11 or later> python3 tests/in_container.py` | The same suite on Linux in a container, so that nothing is skipped. |
| `AGENT_SOFTWARE_TESTING_BUILDS_IMAGE=<image with the coverage builds> python3 tests/e2e_container.py` | A real replay with real gcc, gcov and lcov on the pinned build of one program. Slow. |
| `python3 tests/break_it.py` | Breaks the code on purpose, one bug at a time in a throwaway copy. It says for each whether a test noticed. `--list` shows the bugs. |

`break_it.py` leaves its copies in the folder it names at the start and deletes nothing. Its bugs are in
`break_it_bugs.py`, each with the promise it breaks. Rows that start with `OK-` are harmless changes (a reworded message,
a tuned constant) that the tests have to survive.

## What must never break

### The numbers

| | Promise | Tests |
|---|---|---|
| B1 | A branch is one key `file:line:block:branch`, counted by the rules of SPEC section 3.1 (covered means above zero, a line needs its `DA` record, duplicates add up, records of one header merge, paths are normalised, two names for one file are an error). | `test_branch_identity.py` |
| M1 | A case is in the first snapshot at or after its time. The budget itself is replayed and a millisecond later is not. The curve, the snapshot files and the final set tell one story. | `test_snapshots_hold_the_cases_due_by_then...`, `test_a_snapshot_without_new_cases...` |
| M2 | A case that fails, crashes or times out is counted as such and keeps the coverage it wrote. | `test_cases_that_crash_time_out_or_fail...` |
| M3 | Shard count and shard mode change speed and nothing else. | `test_the_result_does_not_depend_on_how_many_shards...` |
| M4 | The flake check reports what differs in both directions. Only the first replay enters the result. The check can be off and says so. | `test_the_flake_check_reports_what_differs...` |
| M5 | An empty corpus is zero coverage and a valid result. | `test_an_empty_corpus...` |
| M6 | A replay that cannot be trusted stops as early as it can and publishes nothing. No output folder and no leftover remains. A failure is never reported as zero coverage. Causes tested are lcov warnings, a capture that fails after writing a file, stale profile data, a build that writes no profiles, a stray file in the profile tree, a branch outside the universe, a universe of the wrong size and one bad case in the bundle. | `test_a_replay_that_cannot_be_trusted...`, `test_one_invalid_case_stops...`, `test_the_universe_must_be...` |
| M7 | Existing results are never written over. The refusal comes at the start. | `test_results_are_never_written_over...` |
| M8 | `replay.json` names what was replayed (digests of the bundle files, the build and the definition) and carries no path or user name of the machine. | `test_the_record_names_exactly...` |
| M9 | Tool name, family, model and the origin of a case are labels. The same cases give the same numbers. | `test_who_made_the_cases...` |
| M10 | A profile changes what is replayed, so numbers of different profiles are never mixed up. | `test_the_earlier_pipeline_profile...` |
| S1 | One bug is one signature (sanitiser, kind, first frame in the program's own source). Public output holds kinds and counts and nothing a program printed. | `test_one_bug_is_one_signature...` |
| S2 | Text a program prints cannot make a new or long kind. | `test_text_a_program_prints...` |
| S3 | The kind tables are the published vocabulary. | `test_addresssanitizer_reports...`, `test_undefinedbehaviorsanitizer_messages...` |
| A1 | Converting KLEE or AFL++ output keeps what the tool made and nothing else (the byte klee_init_env overwrites, times, duplicates, files that are no cases). | `test_adapters.py` |
| A2 | An adapter writes the budget it is told and cores only when told. | `test_an_adapter_states_only...` |

### Hostile bundles

| | Promise | Tests |
|---|---|---|
| H1 | The validator refuses a bundle in which anything reaches outside the case directory, sets what the runner owns, breaks a limit or disagrees with its blobs, program or track. It accepts the largest valid values. | `test_bundle_validation.py` |
| H2 | Cases after the budget are skipped and counted. `--strict-budget` refuses them. | `test_cases_after_the_budget...` |
| H3 | The validator cannot be hung or exhausted by a bundle (a named pipe for a blob, a line of any length, links in place of files). | `test_a_line_longer...`, `test_a_named_pipe...`, `test_a_bundle_folder_or_file...` |
| C1 | A program gets the environment of the specification and nothing of the environment the replay was started in. | `test_the_program_gets_exactly...` |
| C2 | Every case starts in a fresh directory, so order does not change coverage. | `test_a_case_starts_in_a_fresh...` |
| C3 | Files, links and the working directory are laid out as listed. | `test_the_files_of_a_case...` |
| C4 | The runner checks a case again before it creates anything. | `test_a_case_that_would_leave...` |
| C5 | A program cannot change the build the next case runs on. Profile data never lands in the original build. | `test_a_program_cannot_change...` |
| C6 | The time limit stops the whole process group. A process that left the group is found and killed (Linux). | `test_a_case_that_runs_past...`, `test_a_process_that_left_its_group...` |
| C7 | Standard input arrives byte for byte, as a file or a pipe as asked. | `test_standard_input_arrives...` |
| A3 | The ConcoLLMic adapter runs code a model wrote. It never starts the program under test, writes nowhere outside its sandbox, gives up on code that does not end and keeps the recorded times. | `test_adapter_concollmic.py` |

### The documents others write against

| | Promise | Tests |
|---|---|---|
| D1 | Every program definition and example bundle is valid. | `test_published_documents.py` |
| D2 | What the runner writes satisfies the JSON Schemas. The schemas refuse what the validator refuses. | same |
| D3 | The published kinds are the same list in the code and in the schema. | same |

## Left out on purpose

These are in the code and are not tested here, because a failure shows in no published number and gives a hostile bundle
nothing. They are the pseudo-terminal and named-pipe file kinds, the CPU clock option, the zone probe, the
worker-process protocol, the progress messages and the text of error messages.
