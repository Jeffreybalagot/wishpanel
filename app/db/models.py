"""ORM models.

0001: settings, users, sessions, audit_log
0002: requests, discover_rows, match_cache, metadata_cache, jobs
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class UTCDateTime(TypeDecorator):
    """SQLite drops tzinfo; store naive UTC and hand back aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Setting(Base):
    """Key/value settings. Secrets are Fernet-encrypted in ``value``."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class User(Base):
    """A person who has logged in. Cached from the auth provider (Kavita)."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_users_provider_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Per-user controls surfaced on the Users settings page (Milestone 2).
    daily_limit_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_approve: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    allowed_media_types: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Session(Base):
    """Server-side login session. Only a SHA-256 of the cookie token is stored."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(token)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditLog(Base):
    """Admin actions and (later) request status changes."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON, never secrets
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Request(Base):
    """One request for one series, any media type (SPEC §5)."""

    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_lookup", "media_type", "provider", "external_id"),
        Index("ix_requests_status", "status"),
        Index("ix_requests_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Metadata identity: provider "comicvine" | "anilist" | "mangabaka", id within it.
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    alt_titles: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    year: Mapped[str | None] = mapped_column(String(8), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    anilist_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Where it was sent.
    downloader: Mapped[str | None] = mapped_column(String(16), nullable=True)
    downloader_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    have_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    imported_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    user: Mapped[User | None] = relationship(lazy="joined")


class DiscoverRow(Base):
    """A cached Discover shelf. Comic rows hold titles resolved via match_cache."""

    __tablename__ = "discover_rows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    position: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    # JSON: manga rows -> list of item dicts; comic rows -> list of {"title","hint"}.
    items: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class MatchCache(Base):
    """A list title (e.g. from Comic Book Herald) matched to a ComicVine volume."""

    __tablename__ = "match_cache"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    hint_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    priority: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    comicvine_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    year: Mapped[str | None] = mapped_column(String(8), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issues: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class MetadataCache(Base):
    """Short-lived cache of metadata lookups (searches, details) to spare rate limits."""

    __tablename__ = "metadata_cache"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


class Job(Base):
    """Background job bookkeeping (schedule lives in settings)."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
