"""Set the agent_readonly role's password from .env.

The migration creates the role without a password (migrations are committed
to git). Run this once after `supabase db push`, and again if you rotate it:

    .venv/bin/python scripts/set_readonly_password.py
"""
from urllib.parse import urlparse

import psycopg
from psycopg import sql
from dotenv import dotenv_values

env = dotenv_values(".env")
readonly = urlparse(env["SUPABASE_READONLY_DB_URL"])

with psycopg.connect(env["SUPABASE_DB_URL"]) as conn:
    conn.execute(
        sql.SQL("alter role agent_readonly with password {}").format(
            sql.Literal(readonly.password)
        )
    )
print("agent_readonly password set.")
