"""Statement files in a private Supabase Storage bucket.

Same contract as the local FileStore: content-addressed paths
(<sha[:2]>/<sha[2:4]>/<sha>), written once, checked against the hash on every
read. Uses the Storage REST API with the service-role key, which must only
ever live in the server's environment."""

from __future__ import annotations

import hashlib
import json

import httpx

from .files import IntegrityError

API = "/storage/v1"


class StorageError(RuntimeError):
    pass


def _missing(r: httpx.Response) -> bool:
    """Storage answers a missing object/bucket with 404, or 400 carrying a 404 code."""
    if r.status_code == 404:
        return True
    if r.status_code == 400:
        try:
            body = r.json()
        except ValueError:
            return False
        return str(body.get("statusCode")) == "404" or "not found" in str(body.get("error", "")).lower() \
            or "not_found" in str(body.get("error", "")).lower()
    return False


def _duplicate(r: httpx.Response) -> bool:
    if r.status_code == 409:
        return True
    if r.status_code == 400:
        try:
            body = r.json()
        except ValueError:
            return False
        return str(body.get("statusCode")) == "409" or "duplicate" in str(body.get("error", "")).lower() \
            or "already exists" in str(body.get("message", "")).lower()
    return False


class SupabaseFileStore:
    def __init__(self, url: str, service_key: str, bucket: str = "statements", *, client: httpx.Client | None = None,
                 create_bucket: bool = True):
        if not url or not service_key:
            raise StorageError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for Supabase storage")
        self.base = url.rstrip("/") + API
        self.bucket = bucket
        self.client = client or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        if create_bucket:
            self.ensure_bucket()

    # -- bucket ----------------------------------------------------------------
    def bucket_info(self) -> dict | None:
        r = self.client.get(f"{self.base}/bucket/{self.bucket}", headers=self.headers)
        if _missing(r):
            return None
        self._check(r, "read the bucket")
        return r.json()

    def ensure_bucket(self) -> None:
        info = self.bucket_info()
        if info is None:
            r = self.client.post(f"{self.base}/bucket", headers=self.headers,
                                 json={"id": self.bucket, "name": self.bucket, "public": False})
            if not _duplicate(r):
                self._check(r, "create the bucket")
            info = self.bucket_info() or {"public": False}
        if info.get("public"):
            raise StorageError(f"Supabase bucket {self.bucket!r} is public; statements must be in a private bucket")

    # -- files -----------------------------------------------------------------
    @staticmethod
    def _path(sha: str) -> str:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("not a sha256 hex digest")
        return f"{sha[:2]}/{sha[2:4]}/{sha}"

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        r = self.client.post(f"{self.base}/object/{self.bucket}/{self._path(sha)}", content=data,
                             headers={**self.headers, "Content-Type": "application/octet-stream", "x-upsert": "false"})
        if not _duplicate(r):  # already there: same name means same content, checked on read
            self._check(r, "store a file")
        return sha

    def get(self, sha: str) -> bytes:
        r = self.client.get(f"{self.base}/object/{self.bucket}/{self._path(sha)}", headers=self.headers)
        if _missing(r):
            raise FileNotFoundError(f"stored file {sha[:12]} is missing from Supabase storage")
        self._check(r, "read a file")
        if hashlib.sha256(r.content).hexdigest() != sha:
            raise IntegrityError(f"stored file {sha[:12]} does not match its hash")
        return r.content

    def exists(self, sha: str) -> bool:
        r = self.client.head(f"{self.base}/object/{self.bucket}/{self._path(sha)}", headers=self.headers)
        if r.status_code == 200:
            return True
        if _missing(r) or r.status_code in (400, 404):
            return False
        self._check(r, "check a file")
        return False

    def _list(self, prefix: str) -> list[dict]:
        out, offset = [], 0
        while True:
            r = self.client.post(f"{self.base}/object/list/{self.bucket}", headers=self.headers,
                                 json={"prefix": prefix, "limit": 1000, "offset": offset,
                                       "sortBy": {"column": "name", "order": "asc"}})
            self._check(r, "list files")
            page = r.json()
            out.extend(page)
            if len(page) < 1000:
                return out
            offset += 1000

    def all_hashes(self) -> list[str]:
        """Every stored file, walking the two folder levels."""
        found = []
        for a in self._list(""):
            if a.get("id") is not None:
                continue
            for b in self._list(a["name"]):
                if b.get("id") is not None:
                    continue
                for f in self._list(f"{a['name']}/{b['name']}"):
                    if f.get("id") is not None:
                        found.append(f["name"])
        return found

    def verify_all(self) -> list[str]:
        """Hashes of stored files whose content no longer matches."""
        bad = []
        for sha in self.all_hashes():
            try:
                self.get(sha)
            except (IntegrityError, FileNotFoundError, ValueError):
                bad.append(sha)
        return bad

    @staticmethod
    def _check(r: httpx.Response, what: str) -> None:
        if r.status_code >= 400:
            try:
                detail = json.dumps(r.json())[:300]
            except ValueError:
                detail = r.text[:300]
            raise StorageError(f"Supabase storage could not {what} (HTTP {r.status_code}): {detail}")
