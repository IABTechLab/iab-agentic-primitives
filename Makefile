# Quality gates for the Web Bot Auth module.
#
# `make quality` runs every gate in order and stops at the first failure:
# the ruff format check, the ruff lint (ruff-wba.toml, mccabe 10), the seam
# boundary, the structural rules, the import contracts, mypy strict, and the
# behavior suite with its branch-coverage floor. The upstream
# commands in README.md (`uv run pytest`, `uv run ruff check .`) are
# unchanged; these gates are scoped to the web_bot_auth paths so they never
# fail on upstream modules.
#
# The interop tests drive Cloudflare's library through Node. pytest deselects
# them by default (pyproject.toml), and `make interop` runs them.

WBA_SRC := src/iab_agentic_primitives/web_bot_auth
WBA_TESTS := tests/web_bot_auth
WBA_PATHS := $(WBA_SRC) $(WBA_TESTS)
WBA_INTEROP := $(WBA_TESTS)/interop

# The branch-coverage floor for the module, measured over the WBA suite.
WBA_COV_FLOOR := 94

.PHONY: quality lint format-check lint-wba boundary guards imports typecheck
.PHONY: test test-wba interop interop-deps

# The stages run in sequence even under `make -j`.
quality:
	$(MAKE) format-check
	$(MAKE) lint-wba
	$(MAKE) boundary
	$(MAKE) guards
	$(MAKE) imports
	$(MAKE) typecheck
	$(MAKE) test-wba

lint:
	$(MAKE) format-check
	$(MAKE) lint-wba
	$(MAKE) boundary

format-check:
	uv run ruff format --check $(WBA_PATHS)

lint-wba:
	uv run ruff check --config ruff-wba.toml $(WBA_PATHS)

# Only the wire seams import the RFC 9421 and Structured Fields libraries, and
# only the fetcher, the httpx edge, and testing import httpx.
boundary:
	uv run ruff check --config ruff-wba-boundary.toml $(WBA_SRC)

# Every structural rule is `severity: error`, so any finding fails the scan.
guards:
	uv run ast-grep scan --config sgconfig.yml --report-style short

# The layer order and the I/O and seam contracts.
imports:
	uv run lint-imports --config .importlinter

# mypy strict on the whole module and the whole suite, with the settings in
# pyproject.toml. Imported modules are analyzed but not reported.
typecheck:
	uv run mypy --follow-imports=silent $(WBA_PATHS)

# The WBA behavior suite with the branch-coverage floor.
test-wba:
	uv run pytest $(WBA_TESTS) -q \
		--cov=iab_agentic_primitives.web_bot_auth --cov-branch \
		--cov-fail-under=$(WBA_COV_FLOOR)

# The whole repo, upstream tests included, as CI's test job runs it.
test:
	uv run pytest

# The harness's pinned Cloudflare libraries. npm ci reruns only when the lock
# file is newer than the installed tree.
interop-deps: $(WBA_INTEROP)/node_modules/.package-lock.json

$(WBA_INTEROP)/node_modules/.package-lock.json: $(WBA_INTEROP)/package-lock.json
	npm ci --prefix $(WBA_INTEROP)

# Interop with Cloudflare's web-bot-auth, the tests pytest deselects by default.
interop: interop-deps
	uv run pytest -m interop $(WBA_TESTS)/test_wba_interop.py -v
