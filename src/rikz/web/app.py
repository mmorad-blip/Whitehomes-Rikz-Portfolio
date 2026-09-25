"""The board website: server-rendered pages read from stored snapshots."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import json

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..store.db import Batch, DriveFile, Job, Notification, Snapshot, ViewLog, get_meta
from . import views
from .auth import AccessConfig, check_key
from .security import SecurityMiddleware, locked_out, record_attempt

HERE = Path(__file__).parent
MAX_FILES = 60
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_REQUEST_BYTES = 100 * 1024 * 1024
COOKIE = {"shareholder": "rikz_v", "admin": "rikz_a"}


def fmt_sar(v, signed: bool = False) -> str:
    if v is None:
        return "–"
    v = Decimal(v)
    return f"{v:+,.2f}" if signed else f"{v:,.2f}"


def fmt_pct(v, dp: int = 2) -> str:
    return "–" if v is None else f"{Decimal(v) * 100:.{dp}f}%"


def fmt_value(v, unit: str) -> str:
    if v is None:
        return "–"
    if unit == "SAR":
        return fmt_sar(v)
    if unit == "ratio":
        return fmt_pct(v)
    if unit == "x":
        return f"{Decimal(v):.2f}×"
    if unit == "days":
        return f"{Decimal(v):.1f}"
    return f"{Decimal(v):,.0f}"


def create_app(store, access: AccessConfig, notices=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityMiddleware, max_body=MAX_REQUEST_BYTES, hsts=access.secure_cookies)

    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        messages = {404: "Not found.", 400: "Bad request.", 413: "Upload too large.", 422: "Bad request."}
        detail = exc.detail if exc.status_code in (400, 413) and isinstance(exc.detail, str) else None
        return templates.TemplateResponse(request, "error.html", {"status": exc.status_code,
                                          "message": detail or messages.get(exc.status_code, "Something went wrong.")},
                                          status_code=exc.status_code)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def bad_form(request: Request, exc: RequestValidationError):
        return templates.TemplateResponse(request, "error.html", {"status": 400, "message": "Bad request."},
                                          status_code=400)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(sar=fmt_sar, pct=fmt_pct, value=fmt_value)
    templates.env.globals["access"] = access
    app.state.store, app.state.access, app.state.templates = store, access, templates

    def base_url(role: str) -> str:
        return f"/{'v' if role == 'shareholder' else 'a'}/{access.tokens[role]}/"

    def gate(request: Request, area: str, token: str, need: str | None = None) -> tuple[str, dict | None]:
        role = access.role_for(area, token)
        if role is None or (need and role != need):
            raise HTTPException(404)
        return role, access.read_session(request.cookies.get(COOKIE[role]), role)

    def log_view(request: Request, role: str, version: int | None) -> None:
        with store.Session() as s:
            s.add(ViewLog(role=role, path=request.url.path.split("/", 3)[-1][:255] or "/", snapshot_version=version,
                          client=access.client_id(request.client.host if request.client else "?"),
                          user_agent=request.headers.get("user-agent", "")[:255]))
            s.commit()

    def load_snapshot(version: int | None):
        with store.Session() as s:
            q = select(Snapshot).order_by(Snapshot.version.desc())
            all_v = [(x.version, x.as_of, x.coverage) for x in s.scalars(q)]
            if not all_v:
                return None, []
            snap = s.scalars(select(Snapshot).where(Snapshot.version == (version or all_v[0][0]))).first()
            if snap is None:
                raise HTTPException(404)
            return snap, all_v

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/{area}/{token}/login", response_class=HTMLResponse)
    def login_form(request: Request, area: str, token: str):
        role, _ = gate(request, area, token)
        return templates.TemplateResponse(request, "login.html", {"role": role, "error": None})

    @app.post("/{area}/{token}/login")
    def login(request: Request, area: str, token: str, key: str = Form(...)):
        role, _ = gate(request, area, token)
        client = access.client_id(request.client.host if request.client else "?")
        wait = locked_out(store.Session, role, client)
        if wait:
            return templates.TemplateResponse(request, "login.html", {"role": role, "error": wait}, status_code=429)
        ok = check_key(key.strip().upper()[:64], access.key_hashes[role])
        record_attempt(store.Session, role, client, ok)
        if not ok:
            return templates.TemplateResponse(request, "login.html", {"role": role, "error": "That access key is not right."},
                                              status_code=401)
        resp = RedirectResponse(base_url(role), status_code=303)
        resp.set_cookie(COOKIE[role], access.make_session(role), httponly=True, secure=access.secure_cookies,
                        samesite="strict", path=base_url(role))
        return resp

    @app.get("/{area}/{token}/logout")
    def logout(request: Request, area: str, token: str):
        role, _ = gate(request, area, token)
        resp = RedirectResponse(base_url(role) + "login", status_code=303)
        resp.delete_cookie(COOKIE[role], path=base_url(role))
        return resp

    def need_session(request, area, token):
        role, session = gate(request, area, token)
        if session is None:
            return role, None, RedirectResponse(base_url(role) + "login", status_code=303)
        return role, session, None

    @app.get("/{area}/{token}/", response_class=HTMLResponse)
    def dashboard(request: Request, area: str, token: str, v: int | None = None):
        role, session, redirect = need_session(request, area, token)
        if redirect:
            return redirect
        snap, versions = load_snapshot(v)
        log_view(request, role, snap.version if snap else None)
        if snap is None:
            return templates.TemplateResponse(request, "empty.html", {"role": role, "base": base_url(role)})
        ctx = views.build(snap, versions)
        return templates.TemplateResponse(request, "dashboard.html", {**ctx, "role": role, "base": base_url(role),
                                                                      "pdf": False})

    @app.get("/{area}/{token}/positions", response_class=HTMLResponse)
    def positions(request: Request, area: str, token: str, v: int | None = None):
        role, session, redirect = need_session(request, area, token)
        if redirect:
            return redirect
        snap, versions = load_snapshot(v)
        if snap is None:
            raise HTTPException(404)
        log_view(request, role, snap.version)
        ctx = views.build(snap, versions)
        return templates.TemplateResponse(request, "positions.html", {**ctx, "role": role, "base": base_url(role)})

    @app.get("/{area}/{token}/report.pdf")
    def pdf(request: Request, area: str, token: str, v: int | None = None):
        role, session, redirect = need_session(request, area, token)
        if redirect:
            return redirect
        snap, versions = load_snapshot(v)
        if snap is None:
            raise HTTPException(404)
        log_view(request, role, snap.version)
        ctx = views.build(snap, versions, pdf=True)
        html = templates.get_template("dashboard.html").render({**ctx, "role": role, "base": base_url(role),
                                                                 "pdf": True, "request": request})
        from weasyprint import HTML

        data = HTML(string=html).write_pdf()
        name = f"rikz-portfolio-report-v{snap.version}-{snap.as_of}.pdf"
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # -- admin -------------------------------------------------------------
    def need_admin(request, area, token):
        role, session, redirect = need_session(request, area, token)
        if role != "admin":
            raise HTTPException(404)
        return session, redirect

    def check_csrf(session, value: str) -> None:
        import hmac

        if not value or not hmac.compare_digest(value, access.csrf_token(session)):
            raise HTTPException(400, "The form expired; reload the page and try again.")

    def admin_context(request, session, result=None) -> dict:
        from sqlalchemy import select as sel

        with store.Session() as s:
            batches = s.scalars(sel(Batch).order_by(Batch.id.desc()).limit(30)).all()
            snaps = s.scalars(sel(Snapshot).order_by(Snapshot.version.desc()).limit(30)).all()
            snap_rows = [(x.version, x.as_of, x.created_at, x.coverage, x.headline, len(x.changes)) for x in snaps]
            version_of = dict(s.execute(sel(Snapshot.id, Snapshot.version)).all())
            views_ = s.scalars(sel(ViewLog).order_by(ViewLog.id.desc()).limit(50)).all()
            drive = s.scalars(sel(DriveFile).order_by(DriveFile.seen_at.desc()).limit(30)).all()
            jobs = s.scalars(sel(Job).where(Job.status != "done").order_by(Job.id.desc()).limit(30)).all()
            sent: dict[int, dict[str, int]] = {}
            for n in s.scalars(sel(Notification).where(Notification.audience == "shareholder")):
                d = sent.setdefault(n.snapshot_version, {})
                d[n.status] = d.get(n.status, 0) + 1
        last = get_meta(store.Session, "drive_last_result")
        return {
            "role": "admin", "base": base_url("admin"), "csrf": access.csrf_token(session), "result": result,
            "batches": batches, "snapshots": snap_rows, "views": views_, "drive_files": drive, "jobs": jobs,
            "drive_last_poll": get_meta(store.Session, "drive_last_poll"),
            "drive_last_error": get_meta(store.Session, "drive_last_error"),
            "drive_last": json.loads(last) if last else None,
            "links": {"shareholder": access.link("shareholder"), "admin": access.link("admin")},
            "notices": notices, "sent": sent, "version_of": version_of,
        }

    @app.get("/{area}/{token}/admin", response_class=HTMLResponse)
    def admin(request: Request, area: str, token: str):
        session, redirect = need_admin(request, area, token)
        if redirect:
            return redirect
        log_view(request, "admin", None)
        return templates.TemplateResponse(request, "admin.html", admin_context(request, session))

    @app.post("/{area}/{token}/upload", response_class=HTMLResponse)
    async def upload(request: Request, area: str, token: str, files: list[UploadFile] = File(...),
                     csrf: str = Form("")):
        session, redirect = need_admin(request, area, token)
        if redirect:
            return redirect
        check_csrf(session, csrf)
        if len(files) > MAX_FILES:
            raise HTTPException(413, f"Upload at most {MAX_FILES} files at a time.")
        payload = []
        for f in files:
            data = await f.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(413, f"{f.filename} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            if data:
                payload.append((Path(f.filename or "upload").name, data))
        if not payload:
            raise HTTPException(400, "No files were attached.")
        result = store.ingest(payload, source="upload")
        return templates.TemplateResponse(request, "admin.html", admin_context(request, session, result))

    @app.post("/{area}/{token}/recalc", response_class=HTMLResponse)
    def recalc(request: Request, area: str, token: str, csrf: str = Form("")):
        session, redirect = need_admin(request, area, token)
        if redirect:
            return redirect
        check_csrf(session, csrf)
        result = store.recalculate(source="upload")
        return templates.TemplateResponse(request, "admin.html", admin_context(request, session, result))

    @app.post("/{area}/{token}/notify", response_class=HTMLResponse)
    def notify(request: Request, area: str, token: str, version: int = Form(...), csrf: str = Form(""),
               resend: str = Form("")):
        session, redirect = need_admin(request, area, token)
        if redirect:
            return redirect
        check_csrf(session, csrf)
        from ..notify.notices import queue_shareholder_report

        if notices is None or not notices.shareholders:
            message = "Shareholder e-mail addresses are not configured (RIKZ_SHAREHOLDER_EMAILS)."
        else:
            try:
                n = queue_shareholder_report(store, notices, version, resend=bool(resend))
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
            message = (f"Report v{version}: notice queued for {n} shareholder(s); the worker sends it within minutes."
                       if n else f"Report v{version} was already sent or queued for every shareholder.")
        ctx = admin_context(request, session)
        ctx["flash"] = message
        return templates.TemplateResponse(request, "admin.html", ctx)

    return app
