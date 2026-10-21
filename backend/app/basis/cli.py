"""人工增删依据。示例：python -m app.basis.cli add --help"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from app.basis.search import search_basis
from app.basis.store import BasisError, add_document, disable_document, list_documents, open_session, verify_clause


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="维护依据库")
    parser.add_argument("--database-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="从 data/basis/ 入库，条款默认未核验")
    add.add_argument("--doc-id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--kind", required=True, choices=["法律", "国家标准", "其他"])
    add.add_argument("--effective", required=True, help="施行日 YYYY-MM-DD")
    add.add_argument("--publish", default=None, help="通过日或发布日 YYYY-MM-DD")
    add.add_argument("--source", required=True, help="来源说明，例如人工上传")
    add.add_argument("--file", required=True, type=Path)
    add.add_argument("--by", required=True)
    add.add_argument("--expect-count", type=int, default=None, help="期望条数，不一致则拒绝入库")

    verify = sub.add_parser("verify", help="把一条条款标为已核验")
    verify.add_argument("doc_id")
    verify.add_argument("path")
    verify.add_argument("--by", required=True)
    verify.add_argument("--note", default="")

    disable = sub.add_parser("disable", help="停用一份依据，不删除原文")
    disable.add_argument("doc_id")
    disable.add_argument("--by", required=True)
    disable.add_argument("--reason", required=True)

    found = sub.add_parser("search", help="只检索已核验且已施行的条款")
    found.add_argument("query")
    found.add_argument("--as-of", default="2026-10-21")
    found.add_argument("--limit", type=int, default=8)

    sub.add_parser("list", help="列出已入库的依据")

    args = parser.parse_args(argv)
    db = open_session(args.database_url)
    try:
        if args.command == "add":
            document = add_document(
                db,
                doc_id=args.doc_id,
                title=args.title,
                kind=args.kind,
                effective_date=_date(args.effective),
                publish_date=_date(args.publish) if args.publish else None,
                source_note=args.source,
                file_path=args.file,
                by=args.by,
                expect_count=args.expect_count,
            )
            db.commit()
            print(f"已入库 {document.doc_id}，条款 {document.clause_count}，均未核验")
        elif args.command == "verify":
            clause = verify_clause(db, doc_id=args.doc_id, path=args.path, by=args.by, note=args.note)
            db.commit()
            print(f"已核验 {args.doc_id} {clause.path}")
        elif args.command == "disable":
            document = disable_document(db, doc_id=args.doc_id, by=args.by, reason=args.reason)
            db.commit()
            print(f"已停用 {document.doc_id}，原文仍在 data/basis/{document.relative_path}")
        elif args.command == "search":
            hits = search_basis(db, args.query, _date(args.as_of), args.limit)
            if not hits:
                print("没有已核验且已施行的匹配条款")
            for hit in hits:
                print(f"{hit['doc_id']} {hit['path']} {hit['text_sha256'][:12]}")
        elif args.command == "list":
            rows = list_documents(db)
            if not rows:
                print("依据库是空的")
            for row in rows:
                print(f"{row.doc_id}\t{row.status}\t{row.kind}\t{row.effective_date}\t{row.clause_count}")
    except BasisError as exc:
        db.rollback()
        raise SystemExit(str(exc)) from exc
    finally:
        db.close()


def _date(value: str) -> date:
    year, month, day = (int(part) for part in value.split("-"))
    return date(year, month, day)


if __name__ == "__main__":
    main()
