"""Add TOTP MFA factors, challenges, and recovery codes.

Revision ID: 136_totp_mfa
Revises: 135_native_sessions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "136_totp_mfa"
down_revision = "135_native_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if all(
        inspector.has_table(table_name)
        for table_name in (
            "user_totp_factors",
            "mfa_recovery_codes",
            "mfa_challenges",
            "mfa_attempt_budgets",
        )
    ) and all(
        "auth_version" in {str(column["name"]) for column in inspector.get_columns(table_name)}
        for table_name in ("users", "browser_sessions", "native_sessions")
    ):
        return
    op.add_column(
        "users",
        sa.Column("auth_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "browser_sessions",
        sa.Column("auth_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "native_sessions",
        sa.Column("auth_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "user_totp_factors",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_accepted_counter", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_email"),
    )
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("code_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("code_id"),
        sa.UniqueConstraint("code_hash", name="uq_mfa_recovery_codes_code_hash"),
    )
    op.create_index("ix_mfa_recovery_codes_user_email", "mfa_recovery_codes", ["user_email"])
    op.create_table(
        "mfa_challenges",
        sa.Column("challenge_id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("login_mode", sa.String(), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("pending_secret", sa.LargeBinary(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("challenge_id"),
        sa.UniqueConstraint("token_hash", name="uq_mfa_challenges_token_hash"),
    )
    op.create_index("ix_mfa_challenges_user_email", "mfa_challenges", ["user_email"])
    op.create_index("ix_mfa_challenges_expires_at", "mfa_challenges", ["expires_at"])
    op.create_table(
        "mfa_attempt_budgets",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_email"),
    )


def downgrade() -> None:
    op.drop_table("mfa_attempt_budgets")
    op.drop_index("ix_mfa_challenges_expires_at", table_name="mfa_challenges")
    op.drop_index("ix_mfa_challenges_user_email", table_name="mfa_challenges")
    op.drop_table("mfa_challenges")
    op.drop_index("ix_mfa_recovery_codes_user_email", table_name="mfa_recovery_codes")
    op.drop_table("mfa_recovery_codes")
    op.drop_table("user_totp_factors")
    op.drop_column("native_sessions", "auth_version")
    op.drop_column("browser_sessions", "auth_version")
    op.drop_column("users", "auth_version")
