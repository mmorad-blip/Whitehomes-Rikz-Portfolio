"""Database schema. PostgreSQL in production (DATABASE_URL), SQLite for tests
and local runs. Stored files and snapshots are never updated once written."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
                        create_engine, event)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

SCHEMA_VERSION = 1


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Batch(Base):
    """One upload (or one Drive pick-up). Accepted or rejected as a whole."""

    __tablename__ = "batch"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    source: Mapped[str] = mapped_column(String(16))  # upload | drive | cli
    status: Mapped[str] = mapped_column(String(16))  # accepted | rejected | unchanged
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[list] = mapped_column(JSON, default=list)
    file_names: Mapped[list] = mapped_column(JSON, default=list)
    file_hashes: Mapped[list] = mapped_column(JSON, default=list)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshot.id"), nullable=True)


class StoredFile(Base):
    __tablename__ = "stored_file"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(Integer)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batch.id"))
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # Period the file covers: statement period, or the Awaed order date.
    covers_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    covers_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    # A Manafa portfolio export is only meaningful with the statement it came with.
    paired_with: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)  # parsed rows, for audit


class Snapshot(Base):
    """A versioned, immutable report. Pages and PDFs read this, never a live
    calculation."""

    __tablename__ = "snapshot"
    __table_args__ = (UniqueConstraint("version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    inputs: Mapped[dict] = mapped_column(JSON)  # file hashes + config contents
    content_hash: Mapped[str] = mapped_column(String(64))
    coverage: Mapped[dict] = mapped_column(JSON)
    headline: Mapped[dict] = mapped_column(JSON)
    changes: Mapped[list] = mapped_column(JSON)
    report: Mapped[str] = mapped_column(Text)  # the full report as JSON
    batch: Mapped[list[Batch]] = relationship(foreign_keys=[Batch.snapshot_id], viewonly=True)


class ViewLog(Base):
    __tablename__ = "view_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    role: Mapped[str] = mapped_column(String(16))  # shareholder | admin
    path: Mapped[str] = mapped_column(String(255))
    snapshot_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client: Mapped[str] = mapped_column(String(64))  # keyed hash of the IP address
    user_agent: Mapped[str] = mapped_column(String(255))


class LoginAttempt(Base):
    __tablename__ = "login_attempt"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    role: Mapped[str] = mapped_column(String(16))
    client: Mapped[str] = mapped_column(String(64))
    ok: Mapped[bool] = mapped_column(Boolean)


class Job(Base):
    """Background work (Drive pick-ups, e-mails), retried with back-off."""

    __tablename__ = "job"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Notification(Base):
    """One e-mail to one recipient. Shareholder notices are sent once per
    report version unless the admin sends again."""

    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    audience: Mapped[str] = mapped_column(String(16))  # shareholder | admin
    kind: Mapped[str] = mapped_column(String(32))  # report | rejected | drive_error | job_failed
    snapshot_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recipient: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | sent | failed
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DriveFile(Base):
    """Drive files already picked up, so each is fetched once."""

    __tablename__ = "drive_file"
    drive_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    md5: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(16), default="waiting")  # waiting | ingested | rejected
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


def get_meta(Session, key: str, default: str | None = None) -> str | None:
    with Session() as s:
        row = s.get(Meta, key)
        return row.value if row else default


def set_meta(Session, key: str, value: str) -> None:
    with Session() as s:
        row = s.get(Meta, key)
        if row is None:
            s.add(Meta(key=key, value=value))
        else:
            row.value = value
        s.commit()


SUPABASE_HOSTS = (".supabase.co", ".supabase.com")


def normalize_url(url: str) -> tuple[str, dict]:
    """Accept connection strings as providers print them.

    * postgres:// and postgresql:// get the psycopg driver name.
    * Supabase hosts require SSL.
    * Supabase's transaction pooler (port 6543) cannot keep prepared
      statements, so psycopg is told not to use them there.
    """
    from sqlalchemy.engine import make_url

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    u = make_url(url)
    connect_args: dict = {}
    if u.drivername.startswith("postgresql") and u.host and u.host.endswith(SUPABASE_HOSTS):
        if "sslmode" not in u.query:
            u = u.update_query_dict({"sslmode": "require"})
        if u.port == 6543:
            connect_args["prepare_threshold"] = None
    return u.render_as_string(hide_password=False), connect_args


def is_supabase(url: str) -> bool:
    from sqlalchemy.engine import make_url

    host = make_url(normalize_url(url)[0]).host or ""
    return host.endswith(SUPABASE_HOSTS)


def make_engine(url: str, schema: str | None = None) -> Engine:
    """schema: keep every table in this PostgreSQL schema (e.g. "rikz").

    On Supabase this keeps the tables out of the "public" schema that its
    automatic web API exposes."""
    url, connect_args = normalize_url(url)
    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    elif schema:
        if not schema.isidentifier():
            raise ValueError(f"invalid schema name {schema!r}")
        engine = engine.execution_options(schema_translate_map={None: schema})
    return engine


def init_db(engine: Engine, schema: str | None = None) -> sessionmaker:
    """Create the tables (in `schema` when given, which must match the one
    passed to make_engine) and return a session factory."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            if schema:
                conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        # Row-level security with no policies: roles other than the owner (the
        # app) see nothing, e.g. Supabase's anon/authenticated API roles.
        # Only the table owner may switch it on; if the app's user is not the
        # owner, say so and carry on (`rikz check-config` reports the state).
        from sqlalchemy.exc import DBAPIError

        try:
            with engine.begin() as conn:
                for table in Base.metadata.sorted_tables:
                    name = f'"{schema}"."{table.name}"' if schema else f'"{table.name}"'
                    conn.exec_driver_sql(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        except DBAPIError as exc:
            import logging

            logging.getLogger("rikz.store").warning("could not enable row-level security: %s", exc.orig)
    Session = sessionmaker(engine, expire_on_commit=False, future=True)
    with Session() as s:
        row = s.get(Meta, "schema_version")
        if row is None:
            s.add(Meta(key="schema_version", value=str(SCHEMA_VERSION)))
            s.commit()
        elif int(row.value) != SCHEMA_VERSION:
            raise RuntimeError(f"database schema version {row.value}, code expects {SCHEMA_VERSION}")
    return Session
