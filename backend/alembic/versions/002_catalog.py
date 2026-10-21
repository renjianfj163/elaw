"""名录校对、审批试算和类型差异。"""

from alembic import op
import sqlalchemy as sa

revision = "002_catalog"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_release",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("confirmed_by", sa.String(100)),
        sa.Column("confirmed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "catalog_row",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("release_id", sa.String(36), sa.ForeignKey("catalog_release.id"), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(300), nullable=False),
        sa.Column("book_text", sa.Text(), nullable=False),
        sa.Column("form_text", sa.Text(), nullable=False),
        sa.Column("register_text", sa.Text(), nullable=False),
        sa.Column("book_rule", sa.JSON()),
        sa.Column("form_rule", sa.JSON()),
        sa.Column("register_rule", sa.JSON()),
    )
    op.create_table(
        "approval_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("catalog_name", sa.String(500), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("category", sa.String(300), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "catalog_diff",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("catalog_name", sa.String(500), nullable=False),
        sa.Column("from_release_id", sa.String(36)),
        sa.Column("to_release_id", sa.String(36), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("catalog_diff")
    op.drop_table("approval_snapshot")
    op.drop_table("catalog_row")
    op.drop_table("catalog_release")
