"""ORM 模型。law_version 的正文与哈希不可改；确认核验只能填写状态、日期和核对人。"""

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

CONFIRM_FIELDS = {
    "status",
    "publish_date",
    "effective_date",
    "repeal_date",
    "verified_by",
    "verified_at",
}


class LawInstrument(Base):
    __tablename__ = "law_instrument"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    level: Mapped[str] = mapped_column(String(32))
    issuer: Mapped[str | None] = mapped_column(String(300))
    source_url: Mapped[str | None] = mapped_column(Text)
    doc_no: Mapped[str | None] = mapped_column(String(200))


class LawVersion(Base):
    __tablename__ = "law_version"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("law_instrument.id"))
    publish_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)
    repeal_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    raw_text: Mapped[str] = mapped_column(Text)
    raw_text_sha256: Mapped[str] = mapped_column(String(64))
    raw_file_path: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))
    raw_status_code: Mapped[str | None] = mapped_column(String(32))
    verified_by: Mapped[str | None] = mapped_column(String(100))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class LawArticle(Base):
    __tablename__ = "law_article"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("law_version.id"))
    path: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[str | None] = mapped_column(Text)


class RulePack(Base):
    __tablename__ = "rule_pack"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class CheckItem(Base):
    __tablename__ = "check_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    article_id: Mapped[str] = mapped_column(ForeignKey("law_article.id"))
    item_code: Mapped[str] = mapped_column(String(80))
    applies_when: Mapped[dict] = mapped_column(JSON)
    requirement: Mapped[dict] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(20))
    required_params: Mapped[list] = mapped_column(JSON)
    quote: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    pack_id: Mapped[str | None] = mapped_column(ForeignKey("rule_pack.id"))


class ProjectCase(Base):
    __tablename__ = "project_case"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    reference_date: Mapped[date] = mapped_column(Date)
    params_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class Document(Base):
    __tablename__ = "document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("project_case.id"))
    filename: Mapped[str] = mapped_column(String(500))
    file_sha256: Mapped[str] = mapped_column(String(64))
    parsed_text: Mapped[str] = mapped_column(Text)
    page_map: Mapped[dict] = mapped_column(JSON)


class Fact(Base):
    __tablename__ = "fact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("project_case.id"))
    param_key: Mapped[str] = mapped_column(String(120))
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
    unit: Mapped[str | None] = mapped_column(String(40))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("document.id"))
    page: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    evidence_span: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    confirmed_by: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Finding(Base):
    __tablename__ = "finding"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("project_case.id"))
    item_id: Mapped[str] = mapped_column(ForeignKey("check_item.id"))
    verdict: Mapped[str] = mapped_column(String(20))
    reasons: Mapped[dict] = mapped_column(JSON)
    quote: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list] = mapped_column(JSON)
    method: Mapped[str] = mapped_column(String(20))
    result_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class RejectedFinding(Base):
    __tablename__ = "rejected_finding"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(String(36))
    item_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class BasisDocument(Base):
    __tablename__ = "basis_document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(16))
    publish_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date] = mapped_column(Date)
    source_note: Mapped[str] = mapped_column(Text)
    relative_path: Mapped[str] = mapped_column(Text)
    file_sha256: Mapped[str] = mapped_column(String(64))
    text_sha256: Mapped[str] = mapped_column(String(64))
    clause_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class BasisClause(Base):
    __tablename__ = "basis_clause"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("basis_document.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(300))
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str | None] = mapped_column(String(100))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)


class BasisCheckItem(Base):
    """依据条款生成的检查项。draft 不参与评估，人工审核后才是 active。"""

    __tablename__ = "basis_check_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(80))
    clause_path: Mapped[str] = mapped_column(String(300))
    clause_sha256: Mapped[str] = mapped_column(String(64))
    item_code: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    report_topics: Mapped[list] = mapped_column(JSON)
    required_params: Mapped[list] = mapped_column(JSON)
    requirement: Mapped[dict] = mapped_column(JSON)
    applies_when: Mapped[dict] = mapped_column(JSON)
    quote: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class EiaReport(Base):
    """已核查的环评报告。复用看 input_key，不按文件和基准日单独复用。"""

    __tablename__ = "eia_report"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(500))
    file_sha256: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[date] = mapped_column(Date)
    input_key: Mapped[str | None] = mapped_column(String(64))
    result_hash: Mapped[str] = mapped_column(String(64))
    basis_in_force: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    shown: Mapped[int] = mapped_column(Integer)
    stats: Mapped[dict] = mapped_column(JSON)
    counts: Mapped[dict] = mapped_column(JSON)
    findings: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ReviewLog(Base):
    __tablename__ = "review_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    finding_id: Mapped[str | None] = mapped_column(String(36))
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(40))
    by: Mapped[str] = mapped_column(String(100))
    at: Mapped[datetime] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)


class LlmCache(Base):
    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    response_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class VerificationReport(Base):
    __tablename__ = "verification_report"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    seed_name: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(16))
    version_label: Mapped[str | None] = mapped_column(String(200))
    publish_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)
    raw_status: Mapped[str | None] = mapped_column(String(32))
    superseded_note: Mapped[str | None] = mapped_column(Text)
    fulltext: Mapped[str] = mapped_column(String(20))
    version_id: Mapped[str | None] = mapped_column(String(36))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


@event.listens_for(LawVersion, "before_update")
def _law_version_append_only(mapper, connection, target) -> None:
    state = inspect(target)
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    illegal = changed - CONFIRM_FIELDS
    if illegal:
        raise PermissionError("law_version 的正文与哈希不可修改，请追加新版本：" + ",".join(sorted(illegal)))


@event.listens_for(LawVersion, "before_delete")
def _law_version_no_delete(mapper, connection, target) -> None:
    raise PermissionError("law_version 不可删除")
