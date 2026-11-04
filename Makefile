# Copyright The Agent Software Testing authors
# SPDX-License-Identifier: Apache-2.0

PYTHON ?= python3
export PYTHONPATH := $(CURDIR)/src

.PHONY: help test test-runner test-environment test-quick test-linux test-e2e break-it compile check-examples clean

help:
	@echo "make test              run both test suites: tests/ (runner) and environment/ (build scripts)"
	@echo "make test-runner       run tests/ (the gcc/lcov end-to-end tests skip when the tools are missing)"
	@echo "make test-environment  run environment/ (the build and image scripts)"
	@echo "make test-quick        run the runner tests (they are quick; see tests/README.md)"
	@echo "make test-linux        run the runner tests on Linux in a container (AGENT_SOFTWARE_TESTING_IMAGE, see tests/in_container.py)"
	@echo "make test-e2e          replay one program with the real gcc, gcov and lcov in a container (AGENT_SOFTWARE_TESTING_BUILDS_IMAGE)"
	@echo "make break-it          break the code on purpose and see whether the tests notice (tests/break_it.py)"
	@echo "make compile        byte-compile the package to catch syntax errors"
	@echo "make check-examples validate the program definitions and the example bundles with the validator"
	@echo "make clean          remove caches"

test: test-runner test-environment

test-runner:
	$(PYTHON) -m pytest tests

test-environment:
	$(PYTHON) -m pytest environment

test-quick:
	$(PYTHON) -m pytest -q tests

test-linux:
	$(PYTHON) tests/in_container.py

test-e2e:
	$(PYTHON) tests/e2e_container.py

break-it:
	$(PYTHON) tests/break_it.py

compile:
	$(PYTHON) -m compileall -q src

check-examples:
	@if [ -d programs ]; then \
	  for f in programs/*/program.json; do $(PYTHON) -m agent_software_testing validate "$$f" || exit 1; done; \
	else echo "no programs/ directory here"; fi
	@if [ -d docs/examples ]; then \
	  for d in docs/examples/*/; do $(PYTHON) -m agent_software_testing validate "$$d" --programs programs || exit 1; done; \
	fi

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + ; rm -rf .pytest_cache build *.egg-info src/*.egg-info
