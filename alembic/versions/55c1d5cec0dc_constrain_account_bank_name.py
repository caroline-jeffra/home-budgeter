"""constrain account bank_name

Revision ID: 55c1d5cec0dc
Revises: 33d1b7828532
Create Date: 2026-09-11 23:35:48.021209

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '55c1d5cec0dc'
down_revision: str | Sequence[str] | None = '33d1b7828532'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the CHECK constraint backing the BankName enum."""
    op.execute("DELETE FROM accounts WHERE bank_name NOT IN ('abn_amro')")
    op.create_check_constraint(
        "bank_name",
        "accounts",
        sa.column("bank_name").in_(["abn_amro"]),
    )

def downgrade() -> None:
    """Drop the CHECK constraint."""
    op.drop_constraint("bank_name", "accounts", type_="check")