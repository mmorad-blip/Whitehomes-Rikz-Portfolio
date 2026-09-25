"""The board website: server-rendered pages read from stored snapshots."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..store.db import Snapshot, ViewLog
from . import views
from .auth import AccessConfig, check_key

HERE = Path(__file__).parent
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


def create_app(store, access: AccessConfig) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
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
        if not check_key(key.strip().upper(), access.key_hashes[role]):
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

    return app
