"""Content-addressed, write-once file store. A file lives at
<root>/<sha[:2]>/<sha[2:4]>/<sha> and is checked against its hash on every read."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class IntegrityError(Exception):
    pass


class FileStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha: str) -> Path:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("not a sha256 hex digest")
        return self.root / sha[:2] / sha[2:4] / sha

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        if path.exists():
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o440)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha

    def get(self, sha: str) -> bytes:
        data = self._path(sha).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise IntegrityError(f"stored file {sha[:12]} does not match its hash")
        return data

    def exists(self, sha: str) -> bool:
        return self._path(sha).exists()

    def verify_all(self) -> list[str]:
        """Hashes of stored files whose content no longer matches."""
        bad = []
        for p in self.root.glob("*/*/*"):
            if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() != p.name:
                bad.append(p.name)
        return bad
