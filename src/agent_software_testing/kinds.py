# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0
"""The kinds a signature may have (SPEC.md section 8).

A sanitiser report is text that the program under test can print itself, so a
public count must never contain a kind that the report chose. A kind is one of
the names below; every other text is counted as ``other``.
"""

from __future__ import annotations

#: Kind used for every report whose text matches no known category.
OTHER_KIND = "other"

#: Longest kind accepted in a public document (all names below are shorter).
MAX_KIND_CHARS = 64

ASAN_KINDS = frozenset(
    {
        "abrt",
        "alloc-dealloc-mismatch",
        "allocation-size-too-big",
        "bad-free",
        "bad-malloc-usable-size",
        "bus",
        "calloc-overflow",
        "container-overflow",
        "deadlysignal",
        "double-free",
        "dynamic-stack-buffer-overflow",
        "failed-to-allocate",
        "fpe",
        "global-buffer-overflow",
        "heap-buffer-overflow",
        "heap-use-after-free",
        "ill",
        "invalid-aligned-alloc-alignment",
        "invalid-allocation-alignment",
        "invalid-pointer-pair",
        "invalid-posix-memalign-alignment",
        "negative-size-param",
        "new-delete-type-mismatch",
        "odr-violation",
        "out-of-memory",
        "param-overlap",
        "pvalloc-overflow",
        "reallocarray-overflow",
        "segv",
        "stack-buffer-overflow",
        "stack-buffer-underflow",
        "stack-overflow",
        "stack-use-after-return",
        "stack-use-after-scope",
        "trap",
        "unknown-crash",
        "use-after-poison",
        OTHER_KIND,
    }
)

UBSAN_KINDS = frozenset(
    {
        "bad-downcast",
        "bad-vptr",
        "division-by-zero",
        "float-cast-overflow",
        "function-type-mismatch",
        "implicit-conversion",
        "index-out-of-bounds",
        "insufficient-object-space",
        "invalid-builtin",
        "invalid-value-load",
        "left-shift-negative",
        "left-shift-overflow",
        "misaligned-access",
        "missing-return",
        "negation-overflow",
        "nonnull-argument",
        "nonnull-return",
        "null-pointer-use",
        "pointer-overflow",
        "shift-exponent",
        "signed-integer-overflow",
        "unreachable",
        "unsigned-integer-overflow",
        "vla-bound",
        OTHER_KIND,
    }
)

KINDS_BY_SANITIZER = {"asan": ASAN_KINDS, "ubsan": UBSAN_KINDS}
