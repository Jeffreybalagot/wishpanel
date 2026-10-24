"""Requests, discover cache, title matching cache, metadata cache, jobs.

Revision ID: 0002_requests_discover_jobs
Revises: 0001_initial
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_requests_discover_jobs"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("alt_titles", sa.Text(), nullable=True),
        sa.Column("year", sa.String(8), nullable=True),
        sa.Column("publisher", sa.String(255), nullable=True),
        sa.Column("cover_url", sa.String(1000), nullable=True),
        sa.Column("anilist_id", sa.Integer(), nullable=True),
        sa.Column("expected_count", sa.Integer(), nullable=True),
        sa.Column("downloader", sa.String(16), nullable=True),
        sa.Column("downloader_ref", sa.String(64), nullable=True),
        sa.Column("source_id", sa.String(64), nullable=True),
        sa.Column("source_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_message", sa.String(500), nullable=True),
        sa.Column("have_count", sa.Integer(), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("imported_from", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_requests_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requests")),
    )
    op.create_index("ix_requests_lookup", "requests", ["media_type", "provider", "external_id"])
    op.create_index("ix_requests_status", "requests", ["status"])
    op.create_index("ix_requests_user_created", "requests", ["user_id", "created_at"])

    op.create_table(
        "discover_rows",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("position", sa.Float(), nullable=False, server_default="0"),
        sa.Column("items", sa.Text(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discover_rows")),
    )
    op.create_index(op.f("ix_discover_rows_media_type"), "discover_rows", ["media_type"])

    op.create_table(
        "match_cache",
        sa.Column("key", sa.String(300), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("hint_year", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Float(), nullable=False, server_default="0"),
        sa.Column("comicvine_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(500), nullable=True),
        sa.Column("year", sa.String(8), nullable=True),
        sa.Column("publisher", sa.String(255), nullable=True),
        sa.Column("issues", sa.Integer(), nullable=True),
        sa.Column("cover_url", sa.String(1000), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("tries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_match_cache")),
    )
    op.create_index(op.f("ix_match_cache_status"), "match_cache", ["status"])

    op.create_table(
        "metadata_cache",
        sa.Column("key", sa.String(300), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_metadata_cache")),
    )
    op.create_index(op.f("ix_metadata_cache_expires_at"), "metadata_cache", ["expires_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("last_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(16), nullable=True),
        sa.Column("last_message", sa.String(500), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_duration_ms", sa.Integer(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_index(op.f("ix_metadata_cache_expires_at"), table_name="metadata_cache")
    op.drop_table("metadata_cache")
    op.drop_index(op.f("ix_match_cache_status"), table_name="match_cache")
    op.drop_table("match_cache")
    op.drop_index(op.f("ix_discover_rows_media_type"), table_name="discover_rows")
    op.drop_table("discover_rows")
    op.drop_index("ix_requests_user_created", table_name="requests")
    op.drop_index("ix_requests_status", table_name="requests")
    op.drop_index("ix_requests_lookup", table_name="requests")
    op.drop_table("requests")
