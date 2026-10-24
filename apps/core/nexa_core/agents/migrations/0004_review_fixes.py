"""Fixes from the Phase 11 review.

- Idempotency keys are unique per (organization, user, key): a key reused in another
  organization starts a task there instead of returning the other organization's task.
- A run's conversation must be in the run's organization, and deleting the conversation only
  forgets the link (`ON DELETE SET NULL (conversation_id)`): Runtime deletes conversations with
  plain SQL, which Django's ORM-level SET_NULL never sees. The same pattern as documents.0002.
"""

from django.conf import settings
from django.db import migrations, models

CONVERSATION_FK = (
    "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_conversation_same_org_fk "
    "FOREIGN KEY (organization_id, conversation_id) "
    "REFERENCES conversations (organization_id, id) ON DELETE SET NULL (conversation_id)",
    "ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_conversation_same_org_fk",
)


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0003_run_time_left"),
        ("chat", "0004_message_tool_trace"),
        ("organizations", "0002_tenant_consistency_fks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="agentrun",
            name="agent_runs_idempotency_uniq",
        ),
        migrations.AddConstraint(
            model_name="agentrun",
            constraint=models.UniqueConstraint(
                condition=~models.Q(idempotency_key=""),
                fields=("organization", "user", "idempotency_key"),
                name="agent_runs_idempotency_uniq",
            ),
        ),
        migrations.RunSQL(*CONVERSATION_FK),
    ]
