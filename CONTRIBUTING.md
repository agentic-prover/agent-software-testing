# Contributing to Agent Software Testing

Thank you for helping. This repository holds the standard: the specification (`SPEC.md`), the JSON Schemas, the
program definitions, the reference runner, the validator, the adapters and the build environment. Results live in
`agent-software-testing-results` and the website in `agent-software-testing-site`; each has its own `CONTRIBUTING.md`.

Everyone who takes part follows the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems are reported privately,
see [SECURITY.md](SECURITY.md). How decisions are made is in [GOVERNANCE.md](GOVERNANCE.md).

## Where to start

| You want to | Do this |
|---|---|
| Report a problem with the runner, validator, adapters, program definitions, build scripts or documentation | Open an issue with the **Bug report** form. |
| Propose a change to the specification or to a data format | Open an issue with the **Specification change (RFC)** form, before writing code. See below. |
| Add a program to the suite | Open an issue with the **Program proposal** form. See [docs/PROPOSING-A-PROGRAM.md](docs/PROPOSING-A-PROGRAM.md). |
| Submit your tool's results | That is done in `agent-software-testing-results`, see its `docs/SUBMITTING.md`. |
| Report a wrong number in a results release | `agent-software-testing-results`, **Data issue** form. |
| Report a bug you found in one of the 18 programs | Not in public. See `DISCLOSURE.md` in `agent-software-testing-results`. |
| Fix a typo, clarify a sentence, improve an example | A pull request is welcome without an issue. |

Please search the existing issues first. For anything larger than a small fix, talk to us in an issue before you spend
time on a pull request; a change to what is measured needs a decision, not only a review (see GOVERNANCE.md).

## Development setup

You need Python 3.10 or later (the code is kept compatible with 3.10; the tests have been run on 3.10 and 3.12). The runner uses only the standard library;
`pytest` is the one test dependency. The end-to-end tests also use `gcc`, `gcov` and `lcov`. The reference versions are
those of Ubuntu 22.04 (gcc 11.4, lcov 1.14) and the full test suite is meant for Linux.

```
git clone <your fork of this repository>
cd agent-software-testing
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
```

Tests that need the toolchain are marked `toolchain` and are skipped when `gcc`, `gcov` or `lcov` is missing, so a
plain checkout still runs the rest. To get the toolchain on Ubuntu: `sudo apt-get install gcc lcov`.

Replaying test cases runs untrusted programs. Do it only in the container of `environment/Dockerfile`, started without
network access (SPEC section 6.7). The unit tests do not need the container; they use small synthetic programs and a
fake toolchain.

## Running the tests

```
make test              # both suites: tests/ (the runner) and environment/ (the build scripts)
make test-runner       # tests/ only
make test-environment  # environment/ only
make test-quick        # the runner tests (they are quick; tests/README.md says what each protects)
make compile           # byte-compile the package to catch syntax errors
make check-examples    # validate the program definitions and the example bundles with the validator
```

One test file or one test: `python -m pytest tests/test_bundle_validation.py -k link`. If you do not use `make`, set
`PYTHONPATH=src` or install the package with `pip install -e .`; `make help` lists the targets.

Before you open a pull request, `make test` (or at least `make test-quick` and `make test-environment`) and
`make check-examples` must pass; the latter validates every `programs/*/program.json` and every example bundle. The
repository has no continuous-integration service: the tests run on your machine, so say in the pull request on which
platform and Python version you ran them. Ideally run them on 3.10 and on a newer version.

## Code style

There is no formatter or linter configuration; match the code around you. What the code base does consistently:

* Every Python file starts with `# Copyright The Agent Software Testing authors` and `# SPDX-License-Identifier: Apache-2.0`.
* The runner, validator and adapters use **only the standard library** (`dependencies = []`). A new runtime dependency
  needs an RFC; test-only dependencies are fine in the `test` extra.
* Code stays compatible with Python 3.10. Use `from __future__ import annotations` and type hints on public functions.
* Errors that a user can cause (a bad bundle, a missing build) are reported with the project's error types and a message
  that says what is wrong and where, not with a traceback.
* Comments and docstrings explain why, in plain sentences. Rules that belong to the specification are cited by section
  number (for example "SPEC section 5.6") rather than copied.
* Documentation is Markdown, wrapped at about 90 columns, in plain and precise prose. Say what is measured and what is
  not; do not claim more than was checked.
* No personal data, user names, host names or absolute local paths in code, tests, fixtures or documentation. Runner
  output must not contain host-specific data either (SPEC section 7.1).

## Tests

* Every behaviour change comes with a test that fails without it. Rules in `SPEC.md` that the validator or runner
  enforce have a test for the accepting and the rejecting case.
* Prefer small synthetic fixtures (`tests/fixtures/`, `tests/bundles.py`, `tests/fake_toolchain.py`) to real programs.
  A test must not need network access.
* A test that depends on the toolchain carries the `toolchain` marker, so it is skipped, not failed, where the tools are
  missing.
* Do not add bundles or inputs that trigger known unfixed defects of one of the programs.

## Proposing a change to the specification (RFC)

`SPEC.md` is normative; the schemas in `schemas/` describe structure only and where they differ from the text the text
governs. Because a replay is a measurement, even a small change can change published numbers. Changes are therefore
proposed in an issue first, using the **Specification change (RFC)** form. An RFC says:

1. **The problem**: what cannot be done or is measured wrongly today, with an example.
2. **The proposal**: the exact change, by section of `SPEC.md` and the schema, validator, runner and documentation
   changes it needs.
3. **The effect on the measurement**, in the terms of SPEC section 10: editorial; a clarification that changes no
   behaviour; a compatible addition; a change that makes a valid bundle invalid; a change of a branch key; a change of a
   measured number. During `0.x` the last three increase the `MINOR` version of the specification.
4. **Compatibility**: what happens to bundles and results releases that exist, whether anything has to be re-measured
   and whether results measured before and after the change may be compared (SPEC section 10 says that results measured
   on different tags of a program may not be mixed).
5. **Alternatives** you considered and why not.

The maintainer labels the issue and keeps it open for comment before accepting it, asking for changes or closing it with
a reason (GOVERNANCE.md). Editorial fixes do not need an RFC. Once an RFC is accepted, the pull request that implements
it changes the specification, the schemas, the validator, the runner, the tests and the examples together;
a change that touches only some of them is not merged.

## Pull requests

1. Fork the repository and make a branch from `main`.
2. Keep the change focused: one fix or one feature. Do not mix a formatting sweep with a behaviour change.
3. Add or update tests and documentation.
4. A change to a program definition, a build recipe, a patch or driver, the image or the signature normalisation tables
   changes the measurement (SPEC section 10): say so in the description and expect it to need an accepted RFC or
   program proposal and a new tag.
5. Fill in the pull request template. Describe what you tested and on which platform.
6. A maintainer reviews. We may ask for changes; we may also decline a change that does not fit the scope in SPEC
   section 1. We try to say why.

By submitting a contribution you license it under the Apache License 2.0 (section 5 of the licence), the licence of this
repository. There is no separate contributor agreement. Do not submit code or data you do not have the right to
license that way; third-party build inputs live in `environment/third_party/` under their own licences and change only
by a maintainer.

## Adding an adapter

An adapter converts a tool's native output into a bundle (`docs/adapters.md`). A new one comes with: the mapping rules in
`docs/adapters.md`, including what is lost and what the `t_ms` of a case is; checks of the format's limits before a case
is written; a report of skipped cases; a validation of the written bundle; and tests with a very small native output.
Adapters use no third-party packages.

## Proposing a program

See [docs/PROPOSING-A-PROGRAM.md](docs/PROPOSING-A-PROGRAM.md) for the checklist a program has to meet.
