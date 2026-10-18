"""dedup key occurrence

Revision ID: c7d71a3e7f0f
Revises: af3933eb89ac
Create Date: 2026-10-17 22:13:49.234970

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d71a3e7f0f"
down_revision: str | Sequence[str] | None = "af3933eb89ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Key dedup on date and same-key occurrence, and freeze occurrence."""
    op.add_column(
        "transactions",
        sa.Column("occurrence", sa.Integer(), server_default="0", nullable=False),
    )
    op.drop_constraint(op.f("uq_transactions_account_id"), "transactions", type_="unique")
    op.create_unique_constraint(
        op.f("uq_transactions_account_id"),
        "transactions",
        ["account_id", "booked_on", "balance_after_cents", "amount_cents", "occurrence"],
    )
    op.execute("DROP TRIGGER transactions_money_immutable ON transactions")
    op.execute(
        """
        CREATE TRIGGER transactions_money_immutable
        BEFORE UPDATE OF
            account_id, booked_on, amount_cents, balance_before_cents,
            balance_after_cents, raw_description, import_batch_id, line_no, occurrence
        ON transactions
        FOR EACH ROW
        WHEN (
            (OLD.account_id, OLD.booked_on, OLD.amount_cents, OLD.balance_before_cents,
            OLD.balance_after_cents, OLD.raw_description, OLD.import_batch_id, OLD.line_no,
            OLD.occurrence)
            IS DISTINCT FROM
            (NEW.account_id, NEW.booked_on, NEW.amount_cents, NEW.balance_before_cents,
            NEW.balance_after_cents, NEW.raw_description, NEW.import_batch_id, NEW.line_no,
            NEW.occurrence)
        )
        EXECUTE FUNCTION transactions_append_only()
        """
    )


def downgrade() -> None:
    """Restore the balance-only key and the trigger without occurrence."""
    op.execute("DROP TRIGGER transactions_money_immutable ON transactions")
    op.execute(
        """
        CREATE TRIGGER transactions_money_immutable
        BEFORE UPDATE OF
            account_id, booked_on, amount_cents, balance_before_cents,
            balance_after_cents, raw_description, import_batch_id, line_no
        ON transactions
        FOR EACH ROW
        WHEN (
            (OLD.account_id, OLD.booked_on, OLD.amount_cents, OLD.balance_before_cents,
            OLD.balance_after_cents, OLD.raw_description, OLD.import_batch_id, OLD.line_no)
            IS DISTINCT FROM
            (NEW.account_id, NEW.booked_on, NEW.amount_cents, NEW.balance_before_cents,
            NEW.balance_after_cents, NEW.raw_description, NEW.import_batch_id, NEW.line_no)
        )
        EXECUTE FUNCTION transactions_append_only()
        """
    )
    op.drop_constraint(op.f("uq_transactions_account_id"), "transactions", type_="unique")
    op.create_unique_constraint(
        op.f("uq_transactions_account_id"),
        "transactions",
        ["account_id", "balance_after_cents", "amount_cents"],
    )
    op.drop_column("transactions", "occurrence")
