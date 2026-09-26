"""Write the Supabase migration from the app's table definitions.

    python scripts/make_supabase_migration.py > supabase/migrations/20260926000000_rikz_schema.sql

The baseline file holds the full current schema (every statement is
"if not exists", so it is safe to re-run); later files in supabase/migrations
are the steps applied to the live project since. tests/test_supabase.py fails
if the baseline and the models drift apart."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from rikz.store.db import SCHEMA_VERSION, Base


def migration_sql() -> str:
    d = postgresql.dialect()
    out = [
        "-- Whitehomes/Rikz portfolio reporting: database objects on Supabase.",
        f"-- Generated from src/rikz/store/db.py (schema version {SCHEMA_VERSION}); the app creates the",
        "-- same objects itself on start-up, so this file and the app never disagree.",
        "",
        "create schema if not exists rikz;",
        "-- Not exposed through Supabase's Data API (only 'public' is by default).",
        "revoke all on schema rikz from anon, authenticated;",
        "",
    ]
    for t in Base.metadata.sorted_tables:
        ddl = str(CreateTable(t, if_not_exists=True).compile(dialect=d)).strip()
        ddl = ddl.replace(f"CREATE TABLE IF NOT EXISTS {t.name} ", f"CREATE TABLE IF NOT EXISTS rikz.{t.name} ")
        ddl = ddl.replace("REFERENCES ", "REFERENCES rikz.")
        out += [ddl + ";", f"alter table rikz.{t.name} enable row level security;", ""]
    out += [
        f"insert into rikz.meta (key, value) values ('schema_version', '{SCHEMA_VERSION}') on conflict (key) do nothing;",
        "",
        "-- Private bucket for the statement files (content-addressed, written once).",
        "insert into storage.buckets (id, name, public) values ('statements', 'statements', false)",
        "on conflict (id) do update set public = false;",
        "",
    ]
    return "\n".join(out)


if __name__ == "__main__":
    print(migration_sql(), end="")
