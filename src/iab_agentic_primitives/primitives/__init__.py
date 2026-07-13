"""Shared wire primitives exchanged between the buyer and seller agents.

Every primitive the two agents exchange is defined exactly once here --
Product, MediaKit, Package, RateCard, Quote, Proposal, Negotiation, Deal,
Order, Line, ChangeRequest, Session, Creative, Assignment, and the identity
objects (Agent, Organization, Account). Agent-local primitives (a buyer's
planning internals, a seller's pricing configuration) stay in their own
repos; only the "both" wire primitives live in this package, which kills
the copy-fork divergence and incompatible-schema problems by making sure
there is exactly one schema for anything that crosses the wire.
"""
