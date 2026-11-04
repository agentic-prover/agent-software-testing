# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""Adapters turn a tool's native output into a bundle (contract section 2).

``ktest``
    KLEE test cases (``.ktest`` files, with the optional ``.ktest.env`` sidecar).
``afl``
    AFL++ ``queue``, ``crashes`` and ``hangs`` directories.
``concollmic``  ConcoLLMic queue directories: YAML files whose code runs the program, recorded
    without running it (record mode).
"""
