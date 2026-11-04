# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Replay every test generator's test cases on one pinned gcov build per program.

The package has no runtime dependencies beyond the Python standard library. The
command line entry point is ``python -m agent_software_testing``.
"""

__version__ = "0.1.0"

#: Version of the bundle, program, branch-set and replay formats.
FORMAT_VERSION = "0.1"
