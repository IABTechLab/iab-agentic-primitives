"""Conformance kit: schema validators, golden vectors, standards checklist.

Makes standards and interop executable rather than aspirational. Golden
test vectors (mirrored from ``spec/fixtures/``) map each IAB standard --
OpenDirect 2.1, Deals API v1.0, AAMP, supply-chain transparency, privacy
signals -- to concrete assertions. Both agent repos import these vectors in
continuous integration and assert that their (de)serialization round-trips:
a gap is a failing test, and a schema change that breaks a vector fails
both repos on the next dependency bump -- the drift detector the pair of
repos never had.
"""
