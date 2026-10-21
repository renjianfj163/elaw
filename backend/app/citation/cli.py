"""在样例或文本上跑引用核验。不调用模型。"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from app.basis.store import evaluable_clauses, open_session
from app.citation.check import BasisClauseView, check_report
from app.parse.cli import parse_report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="引用核验与引用时效")
    parser.add_argument("source", type=Path)
    parser.add_argument("--as-of", default="2026-10-21")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)
    year, month, day = (int(part) for part in args.as_of.split("-"))
    as_of = date(year, month, day)
    parsed = parse_report(args.source)
    clauses = []
    db = open_session(args.database_url)
    try:
        for document, clause in evaluable_clauses(db, as_of):
            clauses.append(
                BasisClauseView(
                    doc_id=document.doc_id,
                    title=document.title,
                    path=clause.path,
                    text=clause.text,
                    text_sha256=clause.text_sha256,
                    effective_date=document.effective_date,
                    verified=clause.verified,
                    status=document.status,
                )
            )
    finally:
        db.close()
    result = check_report(parsed, as_of=as_of, clauses=clauses)
    counts: dict[str, int] = {}
    for finding in result.findings:
        counts[f"{finding.kind}:{finding.verdict}"] = counts.get(f"{finding.kind}:{finding.verdict}", 0) + 1
    for key in sorted(counts):
        print(f"{key} {counts[key]}")
    print(f"rejected {len(result.rejected)}")
    print(f"result_hash {result.result_hash}")


if __name__ == "__main__":
    main()
