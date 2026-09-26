"""Vercel entry point: the report website as one Python function.

Settings come from the Vercel project's environment variables (see
docs/OPERATIONS.md, "Hosting on Vercel"). Until they are complete the site
answers with a page naming what is missing, never the values."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("RIKZ_CONFIG_DIR", str(ROOT / "config"))


def _build():
    from rikz.cli import _attach_notifier
    from rikz.env import open_store
    from rikz.web.app import create_app
    from rikz.web.auth import AccessConfig

    access = AccessConfig.from_env()
    store = open_store()
    return create_app(store, access, _attach_notifier(store))


def _not_ready(problem: str):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from html import escape

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    page = ("<!doctype html><meta name=robots content=noindex><title>Setup needed</title>"
            "<body style='font-family:sans-serif;max-width:560px;margin:15vh auto;padding:0 16px'>"
            "<h1>Almost there</h1><p>The report site is deployed but not configured yet:</p>"
            f"<p><code>{escape(problem)}</code></p>"
            "<p>Add the missing settings in Vercel → Project → Settings → Environment Variables, then redeploy.</p>")

    @app.get("/healthz")
    def healthz():
        return {"ok": False, "setup": problem}

    @app.api_route("/{path:path}", methods=["GET", "POST"], response_class=HTMLResponse)
    def everything(path: str):
        return HTMLResponse(page, status_code=503)

    return app


try:
    app = _build()
except Exception as exc:  # noqa: BLE001 - show what is missing, never secret values
    msg = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__} while starting (check DATABASE_URL)"
    app = _not_ready(msg)
