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

    @classmethod
    def load(cls) -> "Env":
        data = Path(os.environ.get("RIKZ_DATA_DIR", "data"))
        return cls(
            database_url=os.environ.get("DATABASE_URL", f"sqlite:///{data / 'rikz.db'}"),
            file_store_dir=Path(os.environ.get("FILE_STORE_DIR", str(data / "files"))),
            config_dir=Path(os.environ.get("RIKZ_CONFIG_DIR", str(CONFIG_DIR))),
        )


def open_store(env: Env | None = None):
    from .store.db import init_db, make_engine
    from .store.files import FileStore
    from .store.service import Store

    env = env or Env.load()
    if env.database_url.startswith("sqlite:///"):
        Path(env.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    Session = init_db(make_engine(env.database_url))
    return Store(Session, FileStore(env.file_store_dir), env.config_dir)
