"""Wire-protocol message definitions for the buyer <-> seller surfaces.

Defines the request/response messages and envelopes for the protocol
surfaces the two agents speak to each other: the IAB Tech Lab Deals API
v1.0 (quote and deal wire format plus error envelope), A2A JSON-RPC,
OpenDirect 2.1, and the Agent Card used for discovery. Request/response
query messages that are capabilities rather than persisted objects (for
example an avails query) are classified here as protocol messages, not
primitives. Buyer and seller interoperate only through these shared
definitions, so the contract can no longer drift silently between repos.
"""
