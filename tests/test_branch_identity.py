# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Promise B1 (tests/README.md): a branch is one key, and the keys come from the lcov tracefile by the rules of
SPEC section 3.1. Two tools can only be compared if both are counted by exactly these rules.

The fake toolchain writes tidy tracefiles; these are hand-written to hit the rules it never needs.
"""

from __future__ import annotations

import pytest

from agent_software_testing.common import AgentSoftwareTestingError
from agent_software_testing.gcov import branch_counts
from agent_software_testing.validate import validate_branch_set_document

ROOT = "/build/proj"


def tracefile(tmp_path, text: str):
    path = tmp_path / "t.info"
    path.write_text(text)
    return path


def test_branches_are_counted_by_the_rules_of_the_specification(tmp_path):
    info = tracefile(
        tmp_path,
        "\n".join(
            [
                "TN:",
                f"SF:{ROOT}/src/a.c",
                "DA:10,4",
                "BRDA:10,0,0,3",  # taken
                "BRDA:10,0,1,-",  # block never executed: counts as 0
                "BRDA:10,1,0,0",
                "BRDA:10,2,0,1",  # two entries for one branch are one branch with the sum
                "BRDA:10,2,0,2",
                "BRDA:11,0,0,5",  # no DA line for line 11: not counted at all
                "end_of_record",
                f"SF:{ROOT}/src/common.h",  # one header included by two objects: two records, one file
                "DA:3,1",
                "BRDA:3,0,0,1",
                "BRDA:3,0,1,0",
                "end_of_record",
                f"SF:{ROOT}/src/common.h",
                "DA:3,0",
                "BRDA:3,0,0,0",
                "BRDA:3,0,1,7",
                "end_of_record",
                "SF:./rel.c",  # relative paths lose "." and doubled separators and are not resolved further
                "DA:1,1",
                "BRDA:1,0,0,1",
                "end_of_record",
                "SF:lib//x.c",
                "DA:1,1",
                "BRDA:1,0,0,0",
                "end_of_record",
                "SF:/elsewhere/proj/lib/moved.c",  # measured under another mount point: the part after the last "proj"
                "DA:2,1",
                "BRDA:2,0,0,1",
                "end_of_record",
                "SF:/usr/include/ctype.h",  # a system header keeps its absolute path
                "DA:5,1",
                "BRDA:5,0,0,1",
                "end_of_record",
                "SF:" + ROOT + "/odd:name.c",  # a colon in the file name
                "DA:9,1",
                "BRDA:9,0,0,1",
                "end_of_record",
            ]
        )
        + "\n",
    )
    counts = branch_counts(info, ROOT)
    assert counts == {
        "src/a.c:10:0:0": 3,
        "src/a.c:10:0:1": 0,
        "src/a.c:10:1:0": 0,
        "src/a.c:10:2:0": 3,
        "src/common.h:3:0:0": 1,
        "src/common.h:3:0:1": 7,
        "rel.c:1:0:0": 1,
        "lib/x.c:1:0:0": 0,
        "lib/moved.c:2:0:0": 1,
        "/usr/include/ctype.h:5:0:0": 1,
        "odd:name.c:9:0:0": 1,
    }
    covered = sorted(key for key, count in counts.items() if count > 0)
    assert "src/a.c:10:0:1" not in covered and "lib/x.c:1:0:0" not in covered and len(covered) == 8


def test_two_file_names_that_become_one_key_prefix_are_an_error_not_a_merge(tmp_path):
    """Silently merging them would let one file's branches stand for another's."""

    info = tracefile(tmp_path, f"SF:{ROOT}/a.c\nDA:1,1\nBRDA:1,0,0,1\nend_of_record\nSF:./a.c\nDA:1,1\nBRDA:1,0,0,0\nend_of_record\n")
    # "/build/proj/a.c" and "./a.c" are the same file under the specification's normalisation of "SF" paths
    with pytest.raises(AgentSoftwareTestingError):
        branch_counts(info, ROOT)


def test_a_key_is_file_line_block_branch_with_plain_decimal_numbers():
    """A key is parsed from the right: a file name may hold colons, the three numbers have no sign or leading zero."""

    def problems(*keys):
        doc = {
            "schema": "agent-software-testing/branch-set@0.1", "program": "demo", "branch_key_format": "file:line:block_id:branch_id",
            "branch_total": 10, "branch_covered": len(keys), "covered_branch_keys": sorted(keys),
        }
        return validate_branch_set_document(doc)

    assert problems("odd:name.c:9:0:0", "src/a.c:10:0:1", "/usr/include/ctype.h:5:0:0") == []
    for bad in ("a.c:01:0:0", "a.c:1:00:0", "a.c:1:0:-1", "a.c:1:0", "a.c:x:0:0", "a.c:1:0:0:"):
        assert problems(bad), bad
    assert problems("a.c:1:0:0", "a.c:1:0:0")  # unique
    assert validate_branch_set_document(
        {"schema": "agent-software-testing/branch-set@0.1", "program": "demo", "branch_key_format": "file:line:block_id:branch_id",
         "branch_total": 1, "branch_covered": 2, "covered_branch_keys": ["a.c:1:0:0", "a.c:1:0:1"]}
    )  # more covered than the universe holds
