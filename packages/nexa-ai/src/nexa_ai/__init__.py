"""NEXA AI Gateway (docs/architecture/ai-gateway.md).

Framework-free: no web framework, no database, no Redis. Runtime and the AI workers wire it
to their infrastructure. Nothing outside `nexa_ai.providers` imports a provider SDK.
"""
