"""Authentication and authorization for Runtime.

Runtime never calls Core at request time and never trusts identity claims from the client.
It verifies Core-issued access tokens with Core's published public keys, then loads the
session, membership and permissions from the shared database on every request, so revoked
sessions and changed roles take effect immediately. The policy that decides who may do what
is `nexa_common.access`, the same code Core uses.
"""
