# Changelog

All notable changes to this project are documented in this file. The
project follows semantic versioning (semver): a breaking schema change is a
major version bump.

## [0.1.0] - 2026-07-13

### Added

- Initial package scaffold: `iab_agentic_primitives` with empty
  `primitives`, `protocol`, `state`, `events`, and `conformance`
  subpackages (module docstrings only; domain models land in later work
  items).
- `spec/` directory structure (`openapi/`, `jsonschema/`, `fixtures/`) for
  the normative language-neutral contract artifacts, with README
  establishing the spec-is-normative / package-is-reference-implementation
  relationship.
- CI (continuous integration) workflow running pytest and ruff on push and
  pull request.
- Packaging: hatchling build backend, Python >= 3.11, `pydantic>=2` as the
  sole runtime dependency.
