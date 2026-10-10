"""Composite foreign keys that keep every row inside one tenant.

Django models only declare single-column foreign keys. These constraints additionally make
PostgreSQL reject, for example, a membership whose role belongs to a different organization,
or a team membership whose team and member come from two organizations. They are deliberately
NOT DEFERRABLE so violations fail immediately, including inside test transactions.
"""

from django.db import migrations

CONSTRAINTS = [
    (
        "organization_memberships",
        "organization_memberships_role_same_org_fk",
        "(organization_id, role_id) REFERENCES roles (organization_id, id)",
    ),
    (
        "invitations",
        "invitations_role_same_org_fk",
        "(organization_id, role_id) REFERENCES roles (organization_id, id)",
    ),
    (
        "team_memberships",
        "team_memberships_team_same_org_fk",
        "(organization_id, team_id) REFERENCES teams (organization_id, id) ON DELETE CASCADE",
    ),
    (
        "team_memberships",
        "team_memberships_member_same_org_fk",
        "(organization_id, membership_id) REFERENCES organization_memberships (organization_id, id)"
        " ON DELETE CASCADE",
    ),
]


class Migration(migrations.Migration):
    dependencies = [("organizations", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql=f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY {definition}",
            reverse_sql=f"ALTER TABLE {table} DROP CONSTRAINT {name}",
        )
        for table, name, definition in CONSTRAINTS
    ]
