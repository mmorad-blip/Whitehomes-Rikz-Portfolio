"""Statement files kept in the database itself (FILE_STORE=database).

Used where the app has no disk of its own, such as Vercel with Supabase.
Same contract as the other stores: content-addressed, written once, checked
against the hash on every read."""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from .db import FileBlob
from .files import IntegrityError


class DbFileStore:
    def __init__(self, Session):
        self.Session = Session

    @staticmethod
    def _check(sha: str) -> None:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("not a sha256 hex digest")

    def put(self, data: bytes) -> str:
        with self.Session() as s:
            sha = self.put_in(s, data)
            s.commit()
        return sha

    def put_in(self, s, data: bytes) -> str:
        """Add within the caller's transaction (committed or rolled back with it)."""
        sha = hashlib.sha256(data).hexdigest()
        if s.get(FileBlob, sha) is None:
            s.add(FileBlob(sha256=sha, data=data))
            s.flush()
        return sha

    def get(self, sha: str) -> bytes:
        with self.Session() as s:
            return self.get_in(s, sha)

    def get_in(self, s, sha: str) -> bytes:
        self._check(sha)
        row = s.get(FileBlob, sha)
        if row is None:
            raise FileNotFoundError(f"stored file {sha[:12]} is missing")
        data = bytes(row.data)
        if hashlib.sha256(data).hexdigest() != sha:
            raise IntegrityError(f"stored file {sha[:12]} does not match its hash")
        return data

    def exists(self, sha: str) -> bool:
        self._check(sha)
        with self.Session() as s:
            return s.get(FileBlob, sha) is not None

    def verify_all(self) -> list[str]:
        bad = []
        with self.Session() as s:
            for sha, data in s.execute(select(FileBlob.sha256, FileBlob.data)):
                if hashlib.sha256(bytes(data)).hexdigest() != sha:
                    bad.append(sha)
        return bad
