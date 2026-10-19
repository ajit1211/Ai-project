"""Tenant consistency for the document tables, as Phases 03, 05 and 06 did for theirs.

A document's team and conversation must belong to the document's own organization, and an
attempt must belong to its document's organization: composite foreign keys make PostgreSQL
refuse anything else. Documents shared with a team go with the team, as memories do;
deleting a conversation only forgets the association (`ON DELETE SET NULL (conversation_id)`,
PostgreSQL 15 or later). No role grant is needed: Django, in the API and in the worker, is
the only writer.
"""

from django.db import migrations

COMPOSITE_FKS = [
    (
        "ALTER TABLE documents ADD CONSTRAINT documents_team_same_org_fk "
        "FOREIGN KEY (organization_id, team_id) "
        "REFERENCES teams (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE documents DROP CONSTRAINT documents_team_same_org_fk",
    ),
    (
        "ALTER TABLE documents ADD CONSTRAINT documents_conversation_same_org_fk "
        "FOREIGN KEY (organization_id, conversation_id) "
        "REFERENCES conversations (organization_id, id) ON DELETE SET NULL (conversation_id)",
        "ALTER TABLE documents DROP CONSTRAINT documents_conversation_same_org_fk",
    ),
    (
        "ALTER TABLE document_ingestions ADD CONSTRAINT document_ingestions_same_org_fk "
        "FOREIGN KEY (organization_id, document_id) "
        "REFERENCES documents (organization_id, id) ON DELETE CASCADE",
        "ALTER TABLE document_ingestions DROP CONSTRAINT document_ingestions_same_org_fk",
    ),
]


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0001_initial"),
    ]

    operations = [migrations.RunSQL(forward, backward) for forward, backward in COMPOSITE_FKS]
