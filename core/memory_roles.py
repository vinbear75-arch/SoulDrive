"""Deterministic memory-family projection for the local learning store.

The store keeps semantic memories and reusable Skills in separate durable
tables. This module makes that boundary explicit at read time without
changing legacy rows or attempting automatic long-term-memory extraction.
"""

from __future__ import annotations

from typing import Any


MEMORY_FAMILY_POLICY = "policy"
MEMORY_FAMILY_SEMANTIC = "semantic"
MEMORY_FAMILY_PROCEDURAL = "procedural"
MEMORY_FAMILY_UNKNOWN = "unknown"

_MEMORY_SUBTYPE_FAMILIES = {
    "user_preference": MEMORY_FAMILY_POLICY,
    # A confirmed project constraint is a policy for one project, not a
    # factual claim extracted from the knowledge base.
    "project_constraint": MEMORY_FAMILY_POLICY,
    "project_fact": MEMORY_FAMILY_SEMANTIC,
    # Workflow facts remain semantic claims; their project/workflow scope
    # determines whether they may be visible in a particular context.
    "workflow_fact": MEMORY_FAMILY_SEMANTIC,
}


def classify_memory_family(asset_type: Any, subtype: Any) -> str:
    """Return the stable family used by retrieval and read-time governance."""
    normalized_asset_type = str(asset_type or "").strip().casefold()
    normalized_subtype = str(subtype or "").strip().casefold()
    if normalized_asset_type == "skill":
        return MEMORY_FAMILY_PROCEDURAL
    if normalized_asset_type == "memory":
        return _MEMORY_SUBTYPE_FAMILIES.get(normalized_subtype, MEMORY_FAMILY_UNKNOWN)
    return MEMORY_FAMILY_UNKNOWN


def is_context_admissible_memory_family(family: Any) -> bool:
    """Only classified long-term memory families may enter a prompt context."""
    return str(family or "").strip().casefold() in {
        MEMORY_FAMILY_POLICY,
        MEMORY_FAMILY_SEMANTIC,
    }
