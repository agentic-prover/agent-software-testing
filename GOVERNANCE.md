# Governance

Agent Software Testing has one maintainer, who owns `agent-software-testing` (the standard), `agent-software-testing-results` (the data) and
`agent-software-testing-site` (the website). This page says how changes and releases are made today.

## Changes

The maintainer reviews and merges every pull request. A change to what is measured (the specification, the formats, the
program definitions, the build recipes, the image, the signature normalisation tables and the statistics in SPEC
section 9) is proposed in an issue first, with the *Specification change (RFC)* or the *Program proposal* form. The
maintainer decides in the issue and gives the reason. An accepted change is versioned as
SPEC section 10 says. Fixes, tests and documentation need no issue.

A program's definition does not change in place; a new version of a program is a new program id. A program that can no
longer be measured as defined is retired. It takes no new results and its definition and results stay available.

## Releases

The maintainer cuts a release from a tagged state whose tests pass (`make test` and `make check-examples`). A published
results release does not change. A mistake is corrected in a later release and listed in its errata. Results measured on
different tags of a program are not compared with each other.

## Submissions

The maintainer replays each submitted bundle with the pinned image and the reference runner and publishes what the
replay produced. A submission that breaks the rules (a bundle that does not validate, for example) is declined and the
reason is given in the submission issue.
