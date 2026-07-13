"""Canonical lifecycle state machines for exchanged primitives.

Home of the single canonical order lifecycle machine. Each exchanged
primitive with lifecycle state -- Deal, Order, ChangeRequest -- has exactly
one state enum and one lifecycle machine, defined here and imported by both
agents. There is no buyer-vocabulary/seller-vocabulary split, so there is
nothing to map and nothing to disagree about. State transitions are pure,
deterministic functions: booking and state changes never involve a large
language model, per the deterministic-core / LLM-at-the-edges principle.
"""
