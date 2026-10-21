import time

from sqlalchemy.orm import Session

from app.laws.collectors.flk import collect_flk
from app.laws.collectors.mee import collect_mee
from app.laws.collectors.std import collect_std
from app.laws.seeds import load_seeds
from app.laws.store import add_report, add_version


def run_collection(db: Session, http, names: list[str] | None = None, pause_seconds: float = 0.3) -> list[dict]:
    wanted = set(names or [])
    seeds = [item for item in load_seeds() if not wanted or item["name"] in wanted]
    saved = []
    for index, seed in enumerate(seeds):
        outcome = _dispatch(seed, http)
        version_id = None
        text = (outcome.get("raw_text") or "").strip()
        if text:
            version = add_version(
                db,
                name=seed["name"],
                level=seed["level"],
                source=outcome["source"],
                text=text,
                status="待确认",
                publish_date=outcome.get("publish_date"),
                effective_date=outcome.get("effective_date"),
                raw_status_code=outcome.get("raw_status"),
                issuer=outcome.get("issuer"),
            )
            version_id = version.id
        report = add_report(db, outcome, level=seed["level"], version_id=version_id)
        db.commit()
        saved.append(
            {
                "id": report.id,
                "seed_name": report.seed_name,
                "source": report.source,
                "outcome": report.outcome,
                "fulltext": report.fulltext,
                "version_id": version_id,
                "publish_date": _iso(report.publish_date),
                "effective_date": _iso(report.effective_date),
                "raw_status": report.raw_status,
                "superseded_note": report.superseded_note,
                "detail": report.detail,
            }
        )
        if pause_seconds and index < len(seeds) - 1:
            time.sleep(pause_seconds)
    return saved


def _dispatch(seed: dict, http) -> dict:
    source = seed.get("source")
    try:
        if source == "flk":
            return collect_flk(seed["name"], http)
        if source == "mee":
            return collect_mee(seed["name"], http)
        if source == "std":
            return collect_std(seed["name"], seed.get("search_query") or seed["name"], http)
    except Exception as exc:
        return {
            "seed_name": seed["name"],
            "source": source or "unknown",
            "outcome": "未找到",
            "fulltext": "不适用",
            "raw_text": "",
            "detail": {"error": str(exc)},
        }
    return {
        "seed_name": seed["name"],
        "source": source or "unknown",
        "outcome": "未找到",
        "fulltext": "不适用",
        "raw_text": "",
        "detail": {"error": "未知来源"},
    }


def _iso(value):
    return value.isoformat() if value else None
