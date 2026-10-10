"""Authorization for NEXA Core: the permission catalog, principal loading, DRF integration
and the rules that prevent privilege escalation. The policy itself (who may do what to which
resource) lives in `nexa_common.access`, so Core and Runtime share one implementation.
"""
