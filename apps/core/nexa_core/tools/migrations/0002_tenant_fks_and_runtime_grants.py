"""Tenant keys and Runtime's write access for AI tools (Phase 10, docs/architecture/tools.md).

1. Composite foreign keys keep an invocation in its message's and conversation's organization
   and an approval in its invocation's. They cascade in the database: Runtime deletes messages
   (regenerating) and conversations with plain SQL, and their tool records go with them.
2. Runtime records invocations and approvals while it runs a turn, and organization tool
   settings when an administrator changes them, so `nexa_runtime` may write those tables. It
   also writes the tool audit events, so it may append to `audit_logs` (INSERT only: the log
   is append-only for every writer). The grants are skipped where the role does not exist
   (CI without the init script).
"""

from django.db import migrations

COMPOSITE_FKS = [
    (
        "ALTER TABLE tool_invocations ADD CONSTRAINT tool_invocations_message_same_org_fk "
        "FOREIGN KEY (organization_id, message_id) "
        "REFERENCES messages (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE tool_invocations DROP CONSTRAINT tool_invocations_message_same_org_fk",
    ),
    (
        "ALTER TABLE tool_invocations ADD CONSTRAINT tool_invocations_conversation_same_org_fk "
        "FOREIGN KEY (organization_id, conversation_id) "
        "REFERENCES conversations (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE tool_invocations DROP CONSTRAINT tool_invocations_conversation_same_org_fk",
    ),
    (
        "ALTER TABLE tool_approvals ADD CONSTRAINT tool_approvals_invocation_same_org_fk "
        "FOREIGN KEY (organization_id, invocation_id) "
        "REFERENCES tool_invocations (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE tool_approvals DROP CONSTRAINT tool_approvals_invocation_same_org_fk",
    ),
]

GRANT_RUNTIME = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        GRANT SELECT, INSERT, UPDATE ON tool_invocations, tool_approvals TO nexa_runtime;
        GRANT SELECT, INSERT, UPDATE ON tool_settings TO nexa_runtime;
        GRANT SELECT, INSERT ON audit_logs TO nexa_runtime;
    END IF;
END $$;
"""

REVOKE_RUNTIME = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        REVOKE INSERT, UPDATE ON tool_invocations, tool_approvals, tool_settings
            FROM nexa_runtime;
        REVOKE INSERT ON audit_logs FROM nexa_runtime;
    END IF;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("tools", "0001_initial"),
        ("chat", "0004_message_tool_trace"),
        ("audit", "0001_initial"),
    ]

    operations = [
        *(migrations.RunSQL(forward, reverse) for forward, reverse in COMPOSITE_FKS),
        migrations.RunSQL(GRANT_RUNTIME, REVOKE_RUNTIME),
    ]
