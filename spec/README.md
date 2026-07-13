# spec/ — the normative contract artifacts

**This directory is the standard. The Python package in
`src/iab_agentic_primitives/` is a reference implementation of it, not the
other way around.**

The IAB Tech Lab agentic-advertising contract is published as
language-neutral schema artifacts so that third parties can implement the
standard in any language. An implementation conforms to the *spec* — the
Python package in this repo is simply the first implementation, and it is
CI (continuous integration)-verified against these artifacts like any
other.

## Layout

| Directory | Contents |
|---|---|
| `openapi/` | OpenAPI documents for the protocol surfaces (Deals API v1.0, catalog, negotiation, Agent Card) |
| `jsonschema/` | JSON-Schema definitions for every exchanged primitive, state enum, and event type |
| `fixtures/` | Golden fixtures (conformance vectors): canonical serialized examples every implementation must round-trip |

## Rules

1. **Spec first.** A contract change lands here before (or together with)
   its Python implementation; the fixtures are the acceptance test.
2. **Fixtures are load-bearing.** Both agent repos import the fixtures and
   assert (de)serialization round-trips in CI. A schema change that breaks
   a fixture fails both repos on their next dependency bump — that is the
   drift detector, by design.
3. **Breaking = major.** Any change that breaks a fixture or alters a wire
   shape is a breaking change and requires a major version bump of the
   package (see the semantic versioning policy in the root README).

The directories are scaffolded but empty: normative content lands with the
primitive- and protocol-definition work items (EP-1.2 onward in the
remediation plan, IAB_Agents_Remediation_Plan_20260713).
