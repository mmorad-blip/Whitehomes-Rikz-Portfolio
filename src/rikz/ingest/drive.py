"""Google Drive watcher: picks up new statements from shared folders.

Each Drive file is fetched once (tracked by its Drive ID and checksum).
Files found in one poll are handled together:

* Awaed confirmations are ingested as one batch; if that batch is rejected,
  each confirmation is tried on its own so one bad file does not hold back
  the good ones (each is then its own all-or-nothing upload).
* A Manafa portfolio export has no date, so it is paired with the account
  statement uploaded closest in time to it. The full cross-check (every
  position must have its investment row, repayments must match) rejects a
  wrong pair; the next-closest statement is then tried.
* An export whose statement has not arrived waits; after `wait_hours` it is
  rejected with a reason. A statement without its export also waits.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import select

from ..store.db import DriveFile, now, set_meta

FOLDER = "application/vnd.google-apps.folder"
SHEET = "application/vnd.google-apps.spreadsheet"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
WANTED = {"application/pdf", XLSX, SHEET}


@dataclass(frozen=True)
class RemoteFile:
    id: str
    name: str
    mime: str
    md5: str | None
    modified: datetime


class DriveClient(Protocol):
    def list_folder(self, folder_id: str) -> list[RemoteFile]: ...

    def download(self, f: RemoteFile) -> bytes: ...


class GoogleDrive:
    """Drive v3 REST with a service account (read-only scope). Share the
    statement folders with the service account's e-mail address."""

    API = "https://www.googleapis.com/drive/v3"

    def __init__(self, info: dict):
        from google.oauth2 import service_account

        self.creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive.readonly"])

    @classmethod
    def from_env(cls) -> "GoogleDrive":
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
        return cls(json.loads(raw))

    def _headers(self) -> dict:
        import google.auth.transport.requests

        if not self.creds.valid:
            self.creds.refresh(google.auth.transport.requests.Request())
        return {"Authorization": f"Bearer {self.creds.token}"}

    def list_folder(self, folder_id: str) -> list[RemoteFile]:
        import httpx

        out, token = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed = false", "pageSize": 1000,
                      "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime)",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            r = httpx.get(f"{self.API}/files", params=params, headers=self._headers(), timeout=60)
            r.raise_for_status()
            data = r.json()
            for f in data.get("files", []):
                out.append(RemoteFile(f["id"], f["name"], f["mimeType"], f.get("md5Checksum"),
                                      datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))))
            token = data.get("nextPageToken")
            if not token:
                return out

    def download(self, f: RemoteFile) -> bytes:
        import httpx

        if f.mime == SHEET:
            url, params = f"{self.API}/files/{f.id}/export", {"mimeType": XLSX}
        else:
            url, params = f"{self.API}/files/{f.id}", {"alt": "media", "supportsAllDrives": "true"}
        r = httpx.get(url, params=params, headers=self._headers(), timeout=120, follow_redirects=True)
        r.raise_for_status()
        return r.content


def walk(client: DriveClient, folder_ids: list[str], depth: int = 3) -> list[RemoteFile]:
    """Statement files in the folders and their sub-folders."""
    out, seen = [], set()
    frontier = [(fid, 0) for fid in folder_ids]
    while frontier:
        fid, d = frontier.pop()
        if fid in seen:
            continue
        seen.add(fid)
        for f in client.list_folder(fid):
            if f.mime == FOLDER and d < depth:
                frontier.append((f.id, d + 1))
            elif f.mime in WANTED:
                out.append(f)
    return out


def _kind(name: str, data: bytes) -> str:
    """Cheap classification to decide grouping; the parsers decide for real."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                wb = z.read("xl/workbook.xml").decode("utf8", "ignore")
        except (zipfile.BadZipFile, KeyError):
            return "other"
        if "الاستثمارات" in wb:
            return "export"
        return "statement"
    return "other"


def _age(f: RemoteFile) -> timedelta:
    modified = f.modified if f.modified.tzinfo else f.modified.replace(tzinfo=timezone.utc)
    return now() - modified


@dataclass
class PollResult:
    found: int = 0
    new: int = 0
    batches: list[tuple[list[str], str, list[str]]] = field(default_factory=list)  # names, status, reasons
    waiting: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def poll(store, client: DriveClient, folder_ids: list[str], *, wait_hours: int = 48, on_result=None) -> PollResult:
    res = PollResult()
    files = walk(client, folder_ids)
    res.found = len(files)
    with store.Session() as s:
        known = {d.drive_id: d for d in s.scalars(select(DriveFile))}
    fresh: list[tuple[RemoteFile, bytes]] = []
    for f in files:
        k = known.get(f.id)
        if k is not None and (k.md5 == f.md5 or f.md5 is None) and k.status != "waiting":
            continue
        try:
            data = client.download(f)
        except Exception as exc:  # network / permission: retried next poll
            res.errors.append(f"{f.name}: download failed ({type(exc).__name__})")
            continue
        fresh.append((f, data))
    res.new = sum(1 for f, _ in fresh if f.id not in known)

    def mark(f: RemoteFile, data: bytes | None, status: str, note: str | None = None):
        import hashlib

        with store.Session() as s:
            row = s.get(DriveFile, f.id) or DriveFile(drive_id=f.id, name=f.name)
            row.name, row.md5, row.status, row.note = f.name, f.md5, status, note
            row.sha256 = hashlib.sha256(data).hexdigest() if data else None
            if row.seen_at is None:
                row.seen_at = now()
            s.merge(row)
            s.commit()

    def ingest(group: list[tuple[RemoteFile, bytes]]):
        r = store.ingest([(f.name, d) for f, d in group], source="drive")
        res.batches.append(([f.name for f, _ in group], r.status, r.reasons))
        if on_result:
            on_result(r)
        return r

    by_kind: dict[str, list[tuple[RemoteFile, bytes]]] = {"pdf": [], "export": [], "statement": [], "other": []}
    for f, d in fresh:
        by_kind[_kind(f.name, d)].append((f, d))

    # Awaed confirmations: together when they pass the checks as a group,
    # otherwise one by one so a bad file does not hold back the good ones.
    pdfs = by_kind["pdf"]
    if pdfs:
        groups = [pdfs] if not store.check([(f.name, d) for f, d in pdfs]) else [[x] for x in pdfs]
        for group in groups:
            r = ingest(group)
            for f, d in group:
                mark(f, d, "rejected" if r.status == "rejected" else "ingested", "; ".join(r.reasons) or None)

    # Manafa: pair each export with the statement closest in time that passes
    # the cross-checks (tried without recording anything). Statements picked
    # up in the last week are candidates too, in case the export came later.
    statements = list(by_kind["statement"])
    earlier = []
    if by_kind["export"]:
        cutoff = now() - timedelta(days=7)
        with store.Session() as s:
            rows = [r for r in s.scalars(select(DriveFile).where(DriveFile.status == "ingested"))
                    if r.sha256 and store.files.exists(r.sha256)]
        for r in rows:
            seen = r.seen_at if r.seen_at.tzinfo else r.seen_at.replace(tzinfo=timezone.utc)
            if seen < cutoff:
                continue
            data = store.files.get(r.sha256)
            if _kind(r.name, data) == "statement":
                earlier.append((RemoteFile(r.drive_id, r.name, XLSX, r.md5, seen), data))
    for f, d in sorted(by_kind["export"], key=lambda x: x[0].modified):
        match = None
        for sf, sd in sorted(statements + earlier, key=lambda x: abs((x[0].modified - f.modified).total_seconds())):
            if not store.check([(f.name, d), (sf.name, sd)]):
                match = (sf, sd)
                break
        if match:
            sf, sd = match
            if match in statements:
                statements.remove(match)
            r = ingest([(f, d), (sf, sd)])
            status = "rejected" if r.status == "rejected" else "ingested"
            mark(f, d, status, "; ".join(r.reasons) or None)
            mark(sf, sd, status, "; ".join(r.reasons) or None)
            continue
        if _age(f) > timedelta(hours=wait_hours):
            r = ingest([(f, d)])  # records the rejection with the parser's own reason
            mark(f, d, "rejected", f"no matching account statement arrived within {wait_hours} hours")
        else:
            res.waiting.append(f.name)
            mark(f, d, "waiting", "waiting for the account statement of the same day")
    for sf, sd in statements:
        if _age(sf) > timedelta(hours=wait_hours):
            r = ingest([(sf, sd)])  # stored on its own; the report waits for a statement with its export
            mark(sf, sd, "rejected" if r.status == "rejected" else "ingested", "; ".join(r.reasons) or None)
        else:
            res.waiting.append(sf.name)
            mark(sf, sd, "waiting", "waiting for the portfolio export of the same day")

    for f, d in by_kind["other"]:
        ingest([(f, d)])
        mark(f, d, "rejected", "not a recognised statement")

    set_meta(store.Session, "drive_last_poll", now().isoformat())
    set_meta(store.Session, "drive_last_result", json.dumps(
        {"found": res.found, "new": res.new, "waiting": res.waiting, "errors": res.errors,
         "batches": [{"files": n, "status": st, "reasons": rs} for n, st, rs in res.batches]}, ensure_ascii=False))
    return res
