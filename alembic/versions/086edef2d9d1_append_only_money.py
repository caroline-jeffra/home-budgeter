"""append only money

Revision ID: 086edef2d9d1
Revises: dffa2008caed
Create Date: 2026-10-14 20:05:24.274694

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '086edef2d9d1'
down_revision: str | Sequence[str] | None = 'dffa2008caed'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Refuse changes to a transaction's money facts and provenance."""
    op.execute(
        """
        CREATE FUNCTION transactions_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'transactions are append-only: % on row % ', TG_OP, OLD.id 
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER transactions_money_immutable
        BEFORE UPDATE OF
            account_id, booked_on, amount_cents, balance_before_cents,
            balance_after_cents, raw_description, import_batch_id, line_no
        ON transactions
        FOR EACH ROW
        EXECUTE FUNCTION transactions_append_only()
        """
    )


def downgrade() -> None:
    """Drop the append-only trigger and its function."""
    op.execute("DROP TRIGGER transactions_money_immutable ON transactions")
    op.execute("DROP FUNCTION transactions_append_only()")
