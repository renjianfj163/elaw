"""初始表。PostgreSQL 上 embedding 使用 vector(1024)，允许为空。"""

from alembic import op
import sqlalchemy as sa

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "law_instrument",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("level", sa.String(32), nullable=False),
        sa.Column("issuer", sa.String(300)),
        sa.Column("source_url", sa.Text()),
        sa.Column("doc_no", sa.String(200)),
    )
    op.create_table(
        "law_version",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instrument_id", sa.String(36), sa.ForeignKey("law_instrument.id"), nullable=False),
        sa.Column("publish_date", sa.Date()),
        sa.Column("effective_date", sa.Date()),
        sa.Column("repeal_date", sa.Date()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("raw_text_sha256", sa.String(64), nullable=False),
        sa.Column("raw_file_path", sa.Text()),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("raw_status_code", sa.String(32)),
        sa.Column("verified_by", sa.String(100)),
        sa.Column("verified_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "law_article",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("law_version.id"), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
    )
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE law_article ADD COLUMN embedding vector(1024)")
    else:
        op.add_column("law_article", sa.Column("embedding", sa.Text()))

    op.create_table(
        "rule_pack",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "check_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("article_id", sa.String(36), sa.ForeignKey("law_article.id"), nullable=False),
        sa.Column("item_code", sa.String(80), nullable=False),
        sa.Column("applies_when", sa.JSON(), nullable=False),
        sa.Column("requirement", sa.JSON(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("required_params", sa.JSON(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reviewed_by", sa.String(100)),
        sa.Column("reviewed_at", sa.DateTime()),
        sa.Column("pack_id", sa.String(36), sa.ForeignKey("rule_pack.id")),
    )
    op.create_table(
        "project_case",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("reference_date", sa.Date(), nullable=False),
        sa.Column("params_confirmed", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("project_case.id"), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("parsed_text", sa.Text(), nullable=False),
        sa.Column("page_map", sa.JSON(), nullable=False),
    )
    op.create_table(
        "fact",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("project_case.id"), nullable=False),
        sa.Column("param_key", sa.String(120), nullable=False),
        sa.Column("value", sa.JSON()),
        sa.Column("unit", sa.String(40)),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("document.id")),
        sa.Column("page", sa.Integer()),
        sa.Column("char_start", sa.Integer()),
        sa.Column("char_end", sa.Integer()),
        sa.Column("evidence_span", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("confirmed_by", sa.String(100)),
        sa.Column("confirmed_at", sa.DateTime()),
    )
    op.create_table(
        "finding",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("project_case.id"), nullable=False),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("check_item.id"), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "rejected_finding",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36)),
        sa.Column("item_id", sa.String(36)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "review_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("finding_id", sa.String(36)),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("by", sa.String(100), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.Text()),
    )
    op.create_table(
        "llm_cache",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "verification_report",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("seed_name", sa.String(500), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("version_label", sa.String(200)),
        sa.Column("publish_date", sa.Date()),
        sa.Column("effective_date", sa.Date()),
        sa.Column("raw_status", sa.String(32)),
        sa.Column("superseded_note", sa.Text()),
        sa.Column("fulltext", sa.String(20), nullable=False),
        sa.Column("version_id", sa.String(36)),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    for name in (
        "verification_report",
        "llm_cache",
        "review_log",
        "rejected_finding",
        "finding",
        "fact",
        "document",
        "project_case",
        "check_item",
        "rule_pack",
        "law_article",
        "law_version",
        "law_instrument",
    ):
        op.drop_table(name)
