"""Background worker: polls Google Drive and runs queued jobs (e-mails),
retrying failures with back-off. Run `rikz worker` as a long-lived process
(or `rikz worker --once` from cron)."""

from __future__ import annotations

import logging
import os
import time
import traceback
from datetime import timedelta
from typing import Callable

from sqlalchemy import select

from ..store.db import Job, now, set_meta

log = logging.getLogger("rikz.worker")
MAX_ATTEMPTS = 8
HANDLERS: dict[str, Callable] = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(Session, kind: str, payload: dict) -> int:
    with Session() as s:
        job = Job(kind=kind, payload=payload)
        s.add(job)
        s.commit()
        return job.id


def run_jobs(store, context: dict) -> tuple[int, int]:
    """Run due jobs once. Returns (done, failed_attempts)."""
    done = failed = 0
    with store.Session() as s:
        due = [j.id for j in s.scalars(select(Job).where(Job.status == "pending").order_by(Job.id))
               if (j.run_after if j.run_after.tzinfo else j.run_after.replace(tzinfo=now().tzinfo)) <= now()]
    for job_id in due:
        with store.Session() as s:
            job = s.get(Job, job_id)
            fn = HANDLERS.get(job.kind)
            try:
                if fn is None:
                    raise RuntimeError(f"no handler for job kind {job.kind!r}")
                fn(store, job.payload, context)
                job.status = "done"
                job.last_error = None
                done += 1
            except Exception as exc:
                job.attempts += 1
                job.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                if job.attempts >= MAX_ATTEMPTS:
                    job.status = "failed"
                else:
                    job.run_after = now() + timedelta(minutes=min(2 ** job.attempts, 360))
                failed += 1
                log.warning("job %s (%s) failed: %s", job.id, job.kind, job.last_error)
            s.commit()
    return done, failed


def drive_folders() -> list[str]:
    return [x.strip() for x in os.environ.get("DRIVE_FOLDER_IDS", "").split(",") if x.strip()]


def run_once(store, *, drive=None, folders: list[str] | None = None, on_result=None, context: dict | None = None) -> dict:
    from .drive import poll

    summary: dict = {}
    folders = drive_folders() if folders is None else folders
    if drive is not None and folders:
        try:
            wait = int(os.environ.get("RIKZ_DRIVE_WAIT_HOURS", "48"))
            r = poll(store, drive, folders, wait_hours=wait, on_result=on_result)
            summary["drive"] = {"found": r.found, "new": r.new, "batches": len(r.batches), "waiting": r.waiting,
                                "errors": r.errors}
        except Exception as exc:  # Drive down or credentials wrong: record and try again next round
            set_meta(store.Session, "drive_last_error", f"{now().isoformat()} {type(exc).__name__}: {exc}"[:1000])
            summary["drive"] = {"error": f"{type(exc).__name__}: {exc}"}
            log.warning("drive poll failed: %s", traceback.format_exc(limit=2))
    summary["jobs"] = run_jobs(store, context or {})
    return summary


def run_forever(store, interval: int, **kw) -> None:
    while True:
        started = time.monotonic()
        summary = run_once(store, **kw)
        log.info("worker round: %s", summary)
        time.sleep(max(5, interval - (time.monotonic() - started)))
