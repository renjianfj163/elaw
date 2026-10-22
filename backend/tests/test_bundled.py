from datetime import datetime

from sqlalchemy import select

from app.db import make_engine, make_session_factory
from app.hashing import sha256_text
from app.laws.bundled import confirmed_dir, install_confirmed, load_confirmed
from app.models import Base, LawVersion, VerificationReport


def test_shipped_confirmations_match_their_files():
    items = load_confirmed()
    names = {item["name"] for item in items}
    assert names == {
        "中华人民共和国生态环境法典",
        "GB 18871-2002 电离辐射防护与辐射源安全基本标准",
        "环保部十八号令",
    }
    root = confirmed_dir()
    for item in items:
        text = (root / item["file"]).read_text(encoding="utf-8")
        assert sha256_text(text) == item["sha256"]
        assert item["status"] == "有效"
        assert item["verified_by"] == "rj"


def test_install_confirmed_is_idempotent(tmp_path):
    folder = tmp_path / "confirmed"
    folder.mkdir()
    text = "第一条 为了测试，写下一句完整的话。\n"
    (folder / "sample.txt").write_text(text, encoding="utf-8")
    digest = sha256_text(text)
    (folder / "manifest.yaml").write_text(
        "\n".join(
            [
                "items:",
                "  - name: 测试依据",
                "    level: 法律",
                "    status: 有效",
                "    source: manual",
                "    publish_date: 2020-01-02",
                "    effective_date: 2020-03-04",
                "    verified_by: rj",
                "    verified_at: 2026-10-21 15:13:44.721008",
                "    file: sample.txt",
                f"    sha256: {digest}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = make_session_factory(engine)()
    try:
        assert install_confirmed(db, folder) == 1
        assert install_confirmed(db, folder) == 0
        version = db.scalar(select(LawVersion))
        assert version.status == "有效"
        assert version.verified_by == "rj"
        assert version.verified_at == datetime(2026, 10, 21, 15, 13, 44, 721008)
        assert version.raw_text_sha256 == digest
        report = db.scalar(select(VerificationReport))
        assert report.seed_name == "测试依据"
        assert report.version_id == version.id
        assert report.outcome == "找到"
    finally:
        db.close()
