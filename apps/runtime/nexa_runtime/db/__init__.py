"""Runtime's view of the shared database.

Django owns the schema (architecture doc 11.3). The tables here are read-only mirrors of
the columns Runtime needs; a contract test in Core checks they match the migrated schema.
"""
