# Security policy

Agent Software Testing replays test cases from other people's tools through programs that can write files and start processes.
Isolation is therefore a central property of the project (SPEC section 6.7) and we want to hear about any way in which
it can be broken.

## Reporting a vulnerability

Please report a vulnerability **privately**, with GitHub's private vulnerability reporting: open the *Security* tab of
this repository and choose *Report a vulnerability*. Do not open a public issue, pull request or discussion for it
and do not post details elsewhere before we have agreed on a date.

A useful report says:

* what you found and what an attacker gains (for example, a case writes a file outside its case directory, reaches the
  network or leaves a process running after the replay);
* the version of the runner (`python -m agent_software_testing --version`), the commit and how you ran it (the container image
  and the options with which the container was started);
* the smallest bundle or command that shows it. A bundle is data (the runner never executes its files) and we replay
  it only inside the sandbox described in SPEC section 6.7;
* what you expect to be the fix, if you have an idea.

If you are unsure whether something is a vulnerability, report it privately anyway. We would rather read a report that
turns out to be a bug than miss one.

## What is in scope

* **Sandbox escapes.** A test case, a bundle or a tool's output that makes the reference runner, run as described in
  SPEC section 6.7, do something outside what a replay may do: write outside the case directory and the shard areas,
  follow a symbolic link out of the case root, read files of the host, use the network, change the limits, survive its
  case or the replay or affect another case or another shard.
* **Vulnerabilities in the runner.** Memory-safe code can still be tricked: path traversal when a bundle is
  materialised, a symbolic-link or race problem in the creation of files, a limit that can be exceeded (file count,
  sizes, path length, number of cases, timeout), unbounded memory or disk use from a bundle that passes the limits,
  injection through a field of `tool.json` or `cases.jsonl` into a command line, a log or an output file and a
  validator that accepts a bundle which the runner then handles unsafely.
* **Adapters.** Parsing a hostile test-case directory (KTest files, AFL++ queues) in `python -m agent_software_testing adapt`: crashes,
  unsafe paths, code execution.
* **Environment and supply chain.** A way around the checks of `environment/build_programs.py` (source archives are
  verified against pinned SHA-256 values and so are the third-party build inputs), insecure downloads or a script,
  Makefile target or test in this repository that could be abused with a malicious pull request.
* **Confidentiality of findings.** Output that would publish what the counts-only rule keeps private (SPEC section 8):
  signature frames, function or file names, stack traces or the inputs behind a sanitiser finding.

## What is out of scope

* **What the programs under test do within the sandbox.** The programs are replayed because they can misbehave: gawk
  runs commands, sed writes files, xorriso starts helpers. That is the reason for the sandbox, not a vulnerability in
  Agent Software Testing. Running the runner **outside** the container of `environment/Dockerfile` or without disabled
  networking, a read-only root file system and resource limits is not a supported configuration.
* **Bugs in the programs.** A crash or a sanitiser finding in a program such as gzip or jq is a bug in
  that program. Please do not file it as an Agent Software Testing vulnerability and do not publish it. Follow the disclosure policy
  of the `agent-software-testing-results` repository (`DISCLOSURE.md`); you can send such a report privately in the same way as
  above and we will pass it on to the program's maintainers.
* **Slow or large but valid input.** A bundle that is within the limits of the specification and merely takes long to
  replay is not an attack. Tell us if the limits themselves are too generous.
* **Vulnerabilities in tools we use but do not distribute**: Docker, the Linux kernel, gcc, clang, lcov, Python. Report
  them to the projects concerned. We want to know if our use of them is unsafe.
* **The website and the data.** Problems with the generated site go to the `agent-software-testing-site` repository and a wrong
  or mislabelled number to the `agent-software-testing-results` repository. If you think a site page could harm its visitors (for
  example a script injection through a tool name), that is a security problem: report it privately here.
* Reports from automated scanners without a demonstration of impact and missing hardening headers on a static page.

## What to expect

Agent Software Testing is maintained by one person who has other work. The maintainer will:

* acknowledge a private report and tell you whether we consider it in scope, as soon as we can;
* keep you informed while we reproduce it, decide on a fix and prepare a release;
* agree a disclosure date with you and ask you not to publish before it;
* publish a GitHub security advisory with the fix and credit you in it if you wish.

We cannot promise a response within a fixed number of days and there is no bug bounty. If you have heard nothing after
a reasonable time, add a comment to your report. We prefer to fix a problem well over fixing it fast, but a problem that
lets a test case escape the sandbox is the most urgent kind of problem we have.

## Supported versions

Agent Software Testing is at version 0.x. Security fixes are made on the main branch and released as a new patch version of the
runner; we do not maintain older versions. A fix that changes what a replay measures is a change of the specification
and is versioned as such (SPEC section 10).
