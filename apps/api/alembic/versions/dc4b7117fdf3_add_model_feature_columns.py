"""add model feature columns

Adds the three inputs the recovery model needs that the original schema did not
carry: the payment method and subscription age on a case, and the customer's
prior failure count. Stored rather than derived so training and serving read
identical values.

Revision ID: dc4b7117fdf3
Revises: 2f367f4b81a9
Create Date: 2026-09-02 22:25:23.620999
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dc4b7117fdf3"
down_revision: str | None = "2f367f4b81a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A NOT NULL column cannot be added to a populated table without a default
    # for the existing rows. server_default backfills them; it is then dropped
    # so the application layer stays the single source of the default and the
    # column behaves identically on a fresh database.
    op.add_column(
        "customers",
        sa.Column(
            "prior_failed_payments",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column("customers", "prior_failed_payments", server_default=None)

    # Nullable: not every case has a known payment method, and only
    # subscription-sourced cases have an age.
    op.add_column(
        "recovery_cases", sa.Column("payment_method", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "recovery_cases", sa.Column("subscription_age_days", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("recovery_cases", "subscription_age_days")
    op.drop_column("recovery_cases", "payment_method")
    op.drop_column("customers", "prior_failed_payments")
