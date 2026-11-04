## What this changes

<!-- One or two sentences. Link the issue it closes or belongs to, for example "Closes #123". -->

## Why

<!-- The problem this solves. For a change to the specification, a format, a program or the environment, link the accepted RFC or program proposal. -->

## Kind of change

- [ ] Fix or improvement that changes no behaviour of the measurement (code, tests, documentation)
- [ ] Specification or format change (accepted RFC: #)
- [ ] New program or change to a program definition (accepted proposal: #)
- [ ] Adapter
- [ ] Environment or build scripts
- [ ] Other:

## Does this change what is measured?

<!-- SPEC section 10: a change that makes a valid bundle invalid, changes a branch key or the universe of a program, or changes a measured number increases the MINOR version of the specification, needs a new tag, and results measured before and after it must not be mixed. -->

- [ ] No
- [ ] Yes. What changes, and what has to be re-measured:

## Checklist

- [ ] `make test` passes (it runs the runner tests and the build-script tests; if I ran less, I say below why).
- [ ] `make check-examples` passes.
- [ ] I added or updated tests that fail without this change.
- [ ] I updated the documentation, the schemas, the validator and the examples that this change touches, together.
- [ ] Python files I added start with the copyright and SPDX lines; I added no runtime dependency.
- [ ] I did not add personal data, user or host names, absolute local paths, or inputs that trigger unfixed defects of one
      of the programs.
- [ ] I license this contribution under the Apache License 2.0.

## How it was tested

<!-- Platform, Python version, whether the toolchain tests ran, and anything you could not test. -->
