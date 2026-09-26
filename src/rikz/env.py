"""Runtime settings from environment variables. Secrets are only ever read
from the environment, never from files in the repository."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_DIR


@dataclass(frozen=True)
class Env:
    database_url: str
    file_store_dir: Path
    config_dir: Path
    database_schema: str | None = None
    file_store: str = "local"  # local | supabase
    supabase_url: str | None = None
    supabase_key: str | None = None
    supabase_bucket: str = "statements"

    @classmethod
    def load(cls) -> "Env":
        from .store.db import is_supabase

        data = Path(os.environ.get("RIKZ_DATA_DIR", "data"))
        url = database_url() or f"sqlite:///{data / 'rikz.db'}"
        # On Supabase the tables go in their own schema by default, away from
        # the "public" schema that Supabase's automatic web API exposes.
        schema = os.environ.get("DATABASE_SCHEMA") or ("rikz" if is_supabase(url) else None)
        store = os.environ.get("FILE_STORE", "local").lower()
        if store not in ("local", "supabase", "database"):
            raise RuntimeError("FILE_STORE must be local, supabase or database")
        return cls(
            database_url=url,
            file_store_dir=Path(os.environ.get("FILE_STORE_DIR", str(data / "files"))),
            config_dir=Path(os.environ.get("RIKZ_CONFIG_DIR", str(CONFIG_DIR))),
            database_schema=schema,
            file_store=store,
            supabase_url=os.environ.get("SUPABASE_URL"),
            supabase_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
            supabase_bucket=os.environ.get("SUPABASE_STORAGE_BUCKET", "statements"),
        )


def database_url() -> str | None:
    """The database connection string.

    DATABASE_URL wins. Otherwise POSTGRES_URL, which Vercel sets on its own
    when the Supabase database is connected to the project in Vercel's
    Storage tab, so no one has to copy the database password around.
    """
    for var in ("DATABASE_URL", "POSTGRES_URL"):
        url = os.environ.get(var, "").strip().strip("'\"")
        if not url:
            continue
        if url.startswith(("http://", "https://")):
            # The project's web address, a common paste mistake.
            if var == "DATABASE_URL" and os.environ.get("POSTGRES_URL"):
                continue
            raise RuntimeError(
                f"{var} holds a web address; it needs the postgresql:// connection string"
            )
        return url
    return None


def open_store(env: Env | None = None):
    from .store.db import init_db, make_engine
    from .store.files import FileStore
    from .store.service import Store

    env = env or Env.load()
    if env.database_url.startswith("sqlite:///"):
        Path(env.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    Session = init_db(make_engine(env.database_url, env.database_schema), env.database_schema)
    if env.file_store == "database":
        from .store.db_files import DbFileStore

        return Store(Session, DbFileStore(Session), env.config_dir)
    if env.file_store == "supabase":
        from .store.supabase_files import SupabaseFileStore

        files = SupabaseFileStore(env.supabase_url, env.supabase_key, env.supabase_bucket)
    else:
        files = FileStore(env.file_store_dir)
    return Store(Session, files, env.config_dir)
