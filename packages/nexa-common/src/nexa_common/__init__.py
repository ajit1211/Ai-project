"""Contracts shared by NEXA services.

Kept deliberately small: anything here becomes a coupling point between Core and Runtime
(architecture doc 26.1). It must never import from `apps/`.
"""
