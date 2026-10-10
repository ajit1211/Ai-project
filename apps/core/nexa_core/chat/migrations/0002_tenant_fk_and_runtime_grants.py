"""Tenant consistency and write ownership for the chat tables.

1. A message's organization must be its conversation's organization: a composite foreign
   key makes PostgreSQL refuse anything else, as Phase 03 did for memberships and teams.
2. Runtime is the writer of conversations and messages (the token stream must not pass
   through Django), so `nexa_runtime` gets INSERT, UPDATE and DELETE on both tables. The
   grant is skipped where the role does not exist (CI without the init script).
"""

from django.db import migrations

COMPOSITE_FK = (
    "ALTER TABLE messages ADD CONSTRAINT messages_conversation_same_org_fk "
    "FOREIGN KEY (organization_id, conversation_id) "
    "REFERENCES conversations (organization_id, id) ON DELETE CASCADE"
)
DROP_COMPOSITE_FK = "ALTER TABLE messages DROP CONSTRAINT messages_conversation_same_org_fk"

GRANT_RUNTIME = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON conversations, messages TO nexa_runtime;
    END IF;
END $$;
"""

REVOKE_RUNTIME = """
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexa_runtime') THEN
        REVOKE INSERT, UPDATE, DELETE ON conversations, messages FROM nexa_runtime;
    END IF;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [("chat", "0001_initial")]

    operations = [
        migrations.RunSQL(COMPOSITE_FK, DROP_COMPOSITE_FK),
        migrations.RunSQL(GRANT_RUNTIME, REVOKE_RUNTIME),
    ]
