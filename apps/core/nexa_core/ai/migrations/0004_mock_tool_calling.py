"""The mock model can call tools (Phase 10), so development and tests exercise tool calling
without an API key. The catalog sync never changes existing rows, so the capability is added
here to an existing `mock-1` row that lacks it; nothing else is touched. Reversing removes it."""

from typing import Any

from django.db import migrations


def add(apps: Any, schema_editor: Any) -> None:
    model = apps.get_model("ai", "AIModel")
    for row in model.objects.filter(provider="mock", provider_model="mock-1"):
        if "tool_calling" not in row.capabilities:
            row.capabilities = [*row.capabilities, "tool_calling"]
            row.save(update_fields=["capabilities"])


def remove(apps: Any, schema_editor: Any) -> None:
    model = apps.get_model("ai", "AIModel")
    for row in model.objects.filter(provider="mock", provider_model="mock-1"):
        if "tool_calling" in row.capabilities:
            row.capabilities = [c for c in row.capabilities if c != "tool_calling"]
            row.save(update_fields=["capabilities"])


class Migration(migrations.Migration):
    dependencies = [("ai", "0003_aiembeddingmodel")]

    operations = [migrations.RunPython(add, remove)]
