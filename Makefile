# Developer shortcuts. End users only need ./install.sh (or the curl one-liner in the README).
.PHONY: help install dev test lint check run uninstall clean

help:       ## list targets
	@grep -E "^[a-z]+:.*##" $(MAKEFILE_LIST) | sed -E "s/:.*## /\t/"

install:    ## install the bugmind command from this checkout (editable, user-level)
	uv tool install --force --editable .

dev:        ## create .venv with BugMind + test/lint tools
	uv venv --allow-existing .venv
	uv pip install --python .venv/bin/python -e ".[dev]"

test:       ## run the test suite (no AI credentials needed)
	.venv/bin/python -m pytest

lint:       ## ruff + shellcheck
	.venv/bin/ruff check src tests
	uvx --from shellcheck-py shellcheck -s sh install.sh

check: lint test ## everything CI runs

run:        ## open the TUI from the checkout
	.venv/bin/bugmind

uninstall:  ## remove the bugmind command (data is kept)
	uv tool uninstall bugmind

clean:      ## remove build and cache artifacts
	rm -rf build dist .pytest_cache .ruff_cache src/*.egg-info
	find . -name __pycache__ -type d -prune -not -path "./.venv/*" -exec rm -rf {} +
