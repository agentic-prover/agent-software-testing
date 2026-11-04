# Build inputs from the ConcoLLMic artifact

This directory holds three small files that the build script needs to build two of the 18 programs,
Krep and libmatheval. They were written by the authors of ConcoLLMic and published in the artifact repository of
that project. They are **build inputs only**: they are not part of Agent Software Testing's own code, they are not covered by
the Apache License 2.0 of this repository, and they are kept apart from it in this directory, together with the
licence texts that apply to them.

## Contents

The layout below this directory is `<program>/<file>`, which is the layout that `--assets` of
`environment/build_programs.py` expects.

| File | Program | What it does |
|---|---|---|
| `krep/Makefile.diff` | Krep | make-only build: no host-specific vector flags, `-O1` without vectorisation |
| `krep/krep.diff` | Krep | source patch: sequential partitions instead of a thread pool |
| `libmatheval/test.c` | libmatheval | standard-input driver; it is the binary that is measured |
| `LICENSE-CONCOLLMIC.txt` | | licence of the ConcoLLMic repository, a copy |
| `COPYING` | | GNU General Public License, version 3, the text referred to by that licence |
| `LICENSE-KREP.txt` | Krep | licence of Krep (BSD 2-Clause), a copy |

The branch totals of the two programs (Krep 1,231, libmatheval 591) are the size of the branch universe of the build
made with exactly these files. A different patch or driver gives a different universe, so the builder refuses to
build the two programs unless each file has the SHA-256 given below.

## Provenance

The files come from the ConcoLLMic artifact repository,
<https://github.com/ConcoLLMic/ConcoLLMic-artifact>, at commit `797301735238cf47a062f13b9b882fa508faa1ed`, below
`c_c++_programs/<program>/` (`c_c++_programs/krep/` and `c_c++_programs/libmatheval/`). That repository is the
`experiments/benchmarks` submodule of the ConcoLLMic repository, <https://github.com/ConcoLLMic/ConcoLLMic>.

| File here | SHA-256 (as used here and pinned in the builder) | Upstream path at that commit | SHA-256 upstream |
|---|---|---|---|
| `krep/Makefile.diff` | `381cd3f517a7a802cbb0c1433b763a7369a833cb36212d26d79eaa60dfa2dea4` | `c_c++_programs/krep/Makefile.diff` | `a5b8932d5c8d07b3189e0381478e4dd1e24f8db94224566a0b40e66bb10b9631` |
| `krep/krep.diff` | `e9d7296e6d4618eb95dd1c51e89d9ec07b085bd989e0a48c70b8d0852eb07b59` | `c_c++_programs/krep/krep.diff` | `cfbf41cac6f5e7fd9587b202fde1f672f290cc4d401f71501e248515ab73baf4` |
| `libmatheval/test.c` | `3b1236126e160287cbafb30a7598bac8e8462d3d3a442549c80cf74c32443e55` | `c_c++_programs/libmatheval/test.c` | `39b6b082d8cd0098464a3c93b9a3dcd45866fe91329ced4f14a1879ffae570eb` |

The files here are the bytes that the reference measurement used. They are not byte for byte the upstream files; they
differ in white space at the end of lines and nothing else:

* `krep/Makefile.diff`: 4 empty lines carry a single space upstream (the empty context lines of the patch).
* `krep/krep.diff`: 21 empty lines carry a single space upstream, likewise.
* `libmatheval/test.c`: 2 empty lines carry two spaces upstream, and upstream ends with one more empty line.

Removing trailing spaces from the upstream lines (and the extra last line of `test.c`) gives the files here. Whether `patch` and the compiler give the same result
for the upstream bytes (they should: an empty line inside a hunk is read as an empty context line, and white space does
not change the line numbers of `test.c`) was not tested in a build. If you want the upstream bytes, fetch them from the
address above; the builder will then reject them, because their digest differs from the pinned one.

## Licence

These three files are **not** under the Apache License 2.0. They are distributed under the terms of the licence of the
ConcoLLMic repository, a copy of which is in `LICENSE-CONCOLLMIC.txt`. In short, as that text states:

* for non-commercial uses, which include academic purposes, the code is available under the GNU General Public
  License, version 3 (the full text is in `COPYING`);
* for commercial use, a separate commercial licence has to be obtained from the ConcoLLMic authors, whose contacts
  are given in `LICENSE-CONCOLLMIC.txt`.

The artifact repository itself has no licence file at the commit above. The ConcoLLMic repository states the licence
for its source code, and the artifact repository is a part of it as a submodule; this directory applies that licence
to the three files.

The two Krep files are patches against Krep's source and contain lines of it. Krep is under the BSD 2-Clause licence,
`LICENSE-KREP.txt`, which is kept with them. `libmatheval/test.c` is compiled and linked against libmatheval, which is
fetched from its upstream source (GPL-3.0-or-later; see `NOTICE`) and is not part of this repository.

What this means in practice:

* The rest of this repository is under the Apache License 2.0. These files are not. The Apache-licensed code does not
  contain or modify them; it only reads them, and checks their digest, when it builds Krep and libmatheval.
* If your use is commercial, you need a licence for these three files from the ConcoLLMic authors, or you replace them
  with files of your own. Replacing them changes the branch universe of Krep and libmatheval, so the results are no
  longer comparable with Agent Software Testing's, and the program definitions (`branch_total`) and the pinned digests in
  `environment/build_programs.py` have to be changed, which is a change of the measurement (SPEC section 10).
* Do not edit the files. The builder checks every digest above and stops on a difference.

## Use

The builder looks here when `--assets` is not given:

```
python3 environment/build_programs.py --all
python3 environment/build_programs.py krep libmatheval --assets environment/third_party/concollmic
```

`--assets DIR` replaces the directory; `DIR` must have the same layout.
