# Proposing a program

The suite has 18 programs today (`programs/README.md`). New programs make the replay more representative, but each
one is a permanent commitment: it has to build the same way for years, replay without noise and be safe to run on other
people's test cases. This page says what we ask for, what the maintainers check and what happens next.

Open a **Program proposal** issue (the form asks for everything in the first section). You do not need to write the
build recipe yourself, but a proposal that comes with one and with the numbers of the checklist is decided faster.

## What fits

The suite measures command-line programs and parser front ends whose behaviour is determined by arguments, files in the
working directory, standard input and environment variables (SPEC section 1). The reference builds are C and C++ built
with `gcc --coverage` and, for the sanitiser pass, with `clang`. A library that can be tested only through an
in-process harness does not fit. A program that needs the network, a display, a daemon, a database or a particular
device does not fit either.

A good candidate is widely used, actively maintained, has a stable release (or a pinned revision) that can be fetched
over HTTPS, builds with a short and plain recipe and has behaviour that depends enough on its input surfaces to tell
test generators apart.

## What the proposal contains

1. **Program and version**: name, upstream version string as upstream writes it (SPEC section 4.1) and the project's
   home page.
2. **Source**: the URL of the release archive (HTTPS), its **SHA-256** and the single top-level directory the archive
   unpacks to. For a program pinned to a revision rather than a release, the archive of that revision.
3. **Licence**: an OSI-approved open-source licence, as an SPDX identifier. The program source is not copied into
   Agent Software Testing; it is downloaded from upstream and checked against the digest.
4. **Input surfaces**: which of arguments, files, standard input and environment variables the program reads.
5. **Build recipe**: the configure arguments, the build steps, the libraries it needs from the Ubuntu 22.04 image and
   anything unusual (generated sources, a driver, a patch). Say whether any patch or driver is needed, who wrote it and
   under which licence. Third-party build inputs are kept apart under `environment/third_party/` with their own licence.
6. **Fixed-track invocation, if one is established**: an argument list in which `{input}` stands for the input file or
   a note that the input arrives on standard input. "Established" means that the program's own project or a published
   evaluation runs it that way. Without one, the program is open-track only (SPEC section 4.2).
7. **Seeds, if any**: test cases the upstream project ships (a test suite, a corpus) that tools may start from, with
   their licence. Seeds are information for tool authors; the runner does not use them.
8. **The checklist below**, with the evidence for each item.

Do not put known unfixed defects of the program or inputs that trigger them, in a public proposal. See `DISCLOSURE.md` in
`agent-software-testing-results`.

## Acceptance checklist

A maintainer reproduces each item in the pinned image. The proposer can do the same with `environment/build_programs.py`
inside the image; `environment/README.md` describes the builds and their layout.

- [ ] **Builds reproducibly in the image.** `python3 /opt/agent-software-testing/environment/build_programs.py <id>` produces the
      coverage build (`-O0 --coverage`, gcc 11.4) and the ASan+UBSan build (clang 15) from the pinned archive alone, with
      no network access other than the digest-checked downloads of the builder and a second build from clean gives the
      same set of `.gcno` files and the same binary names. The recipe is an entry in the `RECIPES` table of the builder,
      with a test in `environment/test_build_programs.py`.
- [ ] **The universe is identical across two builds.** The branch universe (SPEC section 3.3, the initial `lcov`
      capture) of two independent coverage builds is the same set of keys, not just the same count. Its size is the
      program's `branch_total`. A runner whose universe differs from `branch_total` rejects the replay, so a number that
      moves between builds disqualifies the program. Keys that name files outside the source tree (system headers) are
      stated in the proposal.
- [ ] **It replays deterministically on a sample corpus.** On a corpus of at least a few hundred varied cases (including
      empty input, large input, invalid input and every input surface the program has), `python -m agent_software_testing replay`
      with the flake check on (the default) reports `flake.branches_differing` of zero. If a few branches differ, the
      proposal says which and why (time, randomness, process identifiers, locale); the maintainers decide whether that
      noise is small and stable enough to document in the program definition. Noise that cannot be explained disqualifies
      the program.
- [ ] **No network, no daemons, no writes outside the case directory.** Run the sample corpus in the container with
      networking off and a read-only root file system (SPEC section 6.7): every case ends with the program stopped
      (`stray_killed` absent or zero) and nothing is written except below the case directory, which is also `HOME` and
      `TMPDIR`. A program that starts helpers or writes elsewhere by design (as some programs of the suite run commands)
      is described in the proposal, together with what the sandbox must confine.
- [ ] **It is within the size limits of the format.** Everything a useful test case needs fits the bundle limits of SPEC
      section 5.6: at most 64 files and 64 path components, 32 MiB per blob, 64 MiB per case, 256 arguments, 64 environment
      variables and a time limit of at most 30 s per case. A program whose typical case takes longer than that or needs
      larger inputs cannot be replayed.
- [ ] **Sanitiser build works.** The ASan+UBSan build runs the sample corpus and `python -m agent_software_testing signatures`
      finishes. Whether the program has findings is not a criterion for acceptance; counts are published as counts
      only (SPEC section 8).
- [ ] **The fixed-track invocation, if given, validates.** A bundle for the program in the fixed track passes
      `python -m agent_software_testing validate`.
- [ ] **Licences are clear.** The program licence is OSI-approved; any patch or driver has a licence that allows
      redistribution with the repository or is fetched from its owner and pinned by digest.
- [ ] **Build cost is stated**: how long the two builds take and how large the build trees are.

## What gets added when a program is accepted

A pull request (by the proposer or a maintainer) adds:

* `programs/<id>/program.json` (schema `agent-software-testing/program@0.1`: `id`, `name`, `version`, `source`, `build`, `binary`,
  `branch_total`, `fixed_track`, `inputs`, `status`), checked with
  `python -m agent_software_testing validate programs/<id>/program.json`;
* the recipe in `environment/build_programs.py` and its tests and any patch or driver (in `programs/<id>/` if it is
  ours, in `environment/third_party/` with its licence if it is somebody else's);
* the row in `programs/README.md`, and the licence line in `NOTICE`.

Adding a program does not change the existing ones (SPEC section 10). The `agent-software-testing-results` repository gets the new
`programs.csv` row and the universe file when the program enters a results release.

## What happens to a proposal

1. A maintainer checks the scope and the licence and labels the issue. Proposals that do not fit are closed with a reason.
2. The proposer or the maintainer prepares the definition and the recipe and runs the checklist. The maintainer
   reproduces the build in the pinned image.
3. An accepted program enters the next results release.
4. A program's definition is never changed in place: a new version of the program is a new program id. A program that can
   no longer be measured as defined is retired (GOVERNANCE.md); its definition and results stay available.

We cannot promise a date for a decision, but we will say in the issue what we are waiting for.
