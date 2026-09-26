import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import AWAED, MANAFA
from rikz.store.db import ViewLog, init_db, make_engine
from rikz.store.files import FileStore
from rikz.store.service import Store
from rikz.web.auth import AccessConfig, check_key, hash_key, new_key
from rikz.web.charts import bars, share_bar

VIEW, ADMIN = "v" * 24, "a" * 24


def read(paths):
    return [(p.name, p.read_bytes()) for p in paths]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    t = tmp_path_factory.mktemp("web")
    st = Store(init_db(make_engine(f"sqlite:///{t / 'db.sqlite'}")), FileStore(t / "files"))
    aw = sorted(AWAED.glob("*.pdf"))
    st.ingest(read(aw[:19] + [MANAFA / "2026-09-06_portfolio.xlsx", MANAFA / "2026-09-06_account_statement.xlsx"]), "cli")
    st.ingest(read(aw[19:] + [MANAFA / "2026-09-24_portfolio.xlsx", MANAFA / "2026-09-24_account_statement.xlsx"]), "cli")
    return st


@pytest.fixture(scope="module")
def access():
    return AccessConfig(secret=b"s" * 40, tokens={"shareholder": VIEW, "admin": ADMIN},
                        key_hashes={"shareholder": hash_key("SHARE-1234"), "admin": hash_key("ADMIN-5678")},
                        public_url="https://report.example", secure_cookies=False)


@pytest.fixture
def client(store, access):
    from rikz.web.app import create_app
    return TestClient(create_app(store, access))


def login(client, area, token, key):
    return client.post(f"/{area}/{token}/login", data={"key": key}, follow_redirects=False)


def test_keys_are_hashed_and_checked():
    k = new_key()
    assert re.fullmatch(r"([A-Z2-9]{4}-){4}[A-Z2-9]{4}", k)
    h = hash_key(k)
    assert h.startswith("scrypt$") and k not in h
    assert check_key(k, h) and not check_key(k + "X", h) and not check_key(k, "garbage")


def test_links(access):
    assert access.link("shareholder") == f"https://report.example/v/{VIEW}/"
    assert access.link("admin") == f"https://report.example/a/{ADMIN}/"


def test_unknown_link_is_404(client):
    assert client.get("/v/not-the-token/").status_code == 404
    assert client.get(f"/a/{VIEW}/").status_code == 404  # shareholder token on the admin path


def test_home_is_an_access_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "private site" in r.text and 'name="key"' in r.text
    assert VIEW not in r.text and ADMIN not in r.text and "/v/" not in r.text and "/a/" not in r.text


def test_home_access_key_opens_the_right_area(client):
    bad = client.post("/", data={"key": "wrong"}, follow_redirects=False)
    assert bad.status_code == 401 and VIEW not in bad.text and ADMIN not in bad.text
    r = client.post("/", data={"key": "share-1234"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/v/{VIEW}/"
    assert f"Path=/v/{VIEW}/" in r.headers["set-cookie"]
    assert client.get(f"/v/{VIEW}/", follow_redirects=False).status_code == 200
    r = client.post("/", data={"key": "ADMIN-5678"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/a/{ADMIN}/"
    assert f"Path=/a/{ADMIN}/" in r.headers["set-cookie"]


def test_login_required(client):
    r = client.get(f"/v/{VIEW}/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/login")
    assert login(client, "v", VIEW, "wrong").status_code == 401
    assert login(client, "v", VIEW, "ADMIN-5678").status_code == 401  # admin key does not open the shareholder link


def test_dashboard(client):
    r = login(client, "v", VIEW, "share-1234")  # case-insensitive
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower() and f"Path=/v/{VIEW}/" in cookie
    page = client.get(f"/v/{VIEW}/").text
    assert "244,711.25" in page and "Report v2" in page
    assert "Manafa OID-5259-791266: active → closed, paid 2026-09-22, net profit 477.40" in page
    assert "Excel V2.2: 244,849.99" in page
    assert "BREACH" in page
    assert "Admin" not in re.sub(r"<[^>]+>", " ", page.split("<main")[0])  # no admin link for shareholders
    older = client.get(f"/v/{VIEW}/?v=1").text
    assert "as of 06 Sep 2026" in older and "244,424.04" in older
    assert client.get(f"/v/{VIEW}/?v=9").status_code == 404


def test_positions_and_pdf(client):
    login(client, "v", VIEW, "SHARE-1234")
    pos = client.get(f"/v/{VIEW}/positions").text
    assert "OID-5259-791266" in pos and "Manafa statement" in pos
    r = client.get(f"/v/{VIEW}/report.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:4] == b"%PDF"
    assert "rikz-portfolio-report-v2-2026-09-24.pdf" in r.headers["content-disposition"]


def test_sessions_do_not_cross_roles(client):
    login(client, "v", VIEW, "SHARE-1234")
    assert client.get(f"/a/{ADMIN}/", follow_redirects=False).status_code == 303  # still asks for the admin key


def test_tampered_or_expired_session(client, access):
    bad = access.make_session("shareholder")[:-2] + "00"
    assert access.read_session(bad, "shareholder") is None
    assert access.read_session(access.make_session("admin"), "shareholder") is None


def test_views_are_logged_without_ip(client, store):
    login(client, "v", VIEW, "SHARE-1234")
    client.get(f"/v/{VIEW}/?v=1")
    with store.Session() as s:
        rows = s.scalars(select(ViewLog)).all()
    assert rows and rows[-1].role == "shareholder" and rows[-1].snapshot_version == 1
    assert "testclient" not in rows[-1].client and len(rows[-1].client) == 16


def test_charts_render_literal_colours_for_pdf():
    svg = bars(["Jan"], [("Awaed", [1]), ("Manafa", [2])], stacked=True, literal=True)
    assert "var(" not in svg and "#1f5f8b" in svg
    assert "var(--c2" in share_bar([("Wallet cash", 5)])


def admin_login(client):
    assert login(client, "a", ADMIN, "ADMIN-5678").status_code == 303


def csrf_of(page: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_admin_page_and_upload(client, store):
    admin_login(client)
    page = client.get(f"/a/{ADMIN}/admin").text
    assert "Upload statements" in page and f"/v/{VIEW}/" in page and "Report versions" in page
    token = csrf_of(page)
    pdf = AWAED / "01-murabaha_confirmation.pdf"
    r = client.post(f"/a/{ADMIN}/upload", data={"csrf": token},
                    files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))])
    assert r.status_code == 200 and "the report did not change" in r.text  # already stored
    lone = MANAFA / "2026-09-24_portfolio.xlsx"
    r = client.post(f"/a/{ADMIN}/upload", data={"csrf": token},
                    files=[("files", (lone.name, lone.read_bytes(), "application/octet-stream"))])
    assert "Rejected – nothing from this upload was imported" in r.text and "no date of its own" in r.text


def test_admin_forms_need_csrf(client):
    admin_login(client)
    pdf = AWAED / "01-murabaha_confirmation.pdf"
    r = client.post(f"/a/{ADMIN}/upload", data={"csrf": "forged"},
                    files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))])
    assert r.status_code == 400
    assert client.post(f"/a/{ADMIN}/recalc", data={}).status_code == 400


def test_shareholders_cannot_reach_admin(client):
    login(client, "v", VIEW, "SHARE-1234")
    assert client.get(f"/v/{VIEW}/admin").status_code == 404
    pdf = AWAED / "01-murabaha_confirmation.pdf"
    r = client.post(f"/v/{VIEW}/upload", data={"csrf": "x"}, files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))])
    assert r.status_code == 404


def test_recalc_from_admin(client):
    admin_login(client)
    token = csrf_of(client.get(f"/a/{ADMIN}/admin").text)
    r = client.post(f"/a/{ADMIN}/recalc", data={"csrf": token})
    assert r.status_code == 200 and "did not change" in r.text


def test_send_to_shareholders_button(store, access):
    from rikz.notify.notices import NoticeConfig
    from rikz.web.app import create_app

    notices = NoticeConfig(("a@example.com",), ("admin@example.com",), "review",
                           access.link("shareholder"), access.link("admin"))
    c = TestClient(create_app(store, access, notices))
    admin_login(c)
    page = c.get(f"/a/{ADMIN}/admin").text
    assert "review first" in page and "Send to shareholders" in page
    r = c.post(f"/a/{ADMIN}/notify", data={"csrf": csrf_of(page), "version": "2"})
    assert "notice queued for 1 shareholder" in r.text
    r = c.post(f"/a/{ADMIN}/notify", data={"csrf": csrf_of(page), "version": "2"})
    assert "already sent or queued for every shareholder" in r.text
    assert c.post(f"/a/{ADMIN}/notify", data={"csrf": "bad", "version": "2"}).status_code == 400
