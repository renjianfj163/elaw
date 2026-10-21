"""引用校验失败就丢弃，不降级放行。"""

from __future__ import annotations

from datetime import date

from app.hashing import stable_hash
from app.verify.citation import evidence_matches, quote_matches

from app.citation.numeric import RULE_VERSION


def accept_quote(
    *,
    quote: str,
    source_text: str,
    report_text: str,
    evidence_span: str,
    char_start: int,
    char_end: int,
    in_force: bool,
) -> str | None:
    if not in_force:
        return "依据未核验，或施行日晚于基准日"
    if not quote_matches(quote, source_text):
        return "quote 不是依据原文的子串"
    if not evidence_matches(report_text, evidence_span, char_start, char_end):
        return "evidence_span 与报告原文对不上"
    return None


def citation_result_hash(
    *,
    report_sha: str,
    basis_sha: str,
    as_of: date,
    findings: list[dict],
    llm_output: dict | None = None,
) -> str:
    return stable_hash(
        {
            "report_sha256": report_sha,
            "basis_sha256": basis_sha,
            "rule_version": RULE_VERSION,
            "as_of": as_of.isoformat(),
            "llm_output": llm_output or {},
            "findings": findings,
        }
    )
