import threading
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schematic.datasheet_fetch import (
    DatasheetCredentials,
    DatasheetMatch,
    DatasheetStore,
    DatasheetVerdict,
    HttpFetched,
    parse_verdict,
    reset_token_for_tests,
    resolve_datasheets,
    store_manual,
    verdict_problem,
)
from app.schematic.mpn import normalize_mpn

CREDS = DatasheetCredentials(nexar_client_id="id", nexar_client_secret="secret")
client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_nexar_token():
    reset_token_for_tests()
    yield
    reset_token_for_tests()


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 36 100 Td ({text}) Tj ET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


class FakeHttp:
    def __init__(self):
        self.nexar: dict[str, list[str]] = {}
        self.jina: dict[str, str] = {}
        self.pdfs: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def post_form(self, url, data):
        self._mark("form", url)
        return {"access_token": "token-test", "expires_in": 3600}

    def post_json(self, url, payload, headers=None):
        query = payload["variables"]["q"]
        self._mark("gql", query)
        sheets = self.nexar.get(query, [])
        if not sheets:
            return {"data": {"supSearch": {"results": []}}}
        return {
            "data": {
                "supSearch": {
                    "results": [{"item": {"mpn": query, "datasheets": [{"url": item} for item in sheets]}}]
                }
            }
        }

    def get_json(self, url, headers=None):
        self._mark("jina", url)
        text = unquote(url).upper()
        for part, link in self.jina.items():
            if part.upper() in text:
                return {"data": [{"url": link, "content": link}]}
        return {"data": []}

    def get_bytes(self, url):
        self._mark("get", url)
        content = self.pdfs.get(url, b"")
        return HttpFetched(status=200 if content else 404, content=content, url=url)

    def _mark(self, kind, url):
        with self._lock:
            self.calls.append((kind, url))


def _accept(text, mpn):
    parsed = normalize_mpn(mpn)
    return DatasheetVerdict(
        is_valid_datasheet=True,
        matched_mpn=parsed.base or parsed.cleaned or mpn,
        confidence=0.95,
        document_type="Datasheet",
    )


def _run(mpns, http, store, **kwargs):
    return resolve_datasheets(
        mpns,
        http=http,
        verifier=kwargs.get("verifier", _accept),
        store=store,
        allow_network=kwargs.get("allow_network", True),
        credentials=kwargs.get("credentials", CREDS),
        refresh=kwargs.get("refresh", False),
    )


def test_normalize_strips_reel_and_keeps_a_searchable_base():
    st = normalize_mpn("STM32F405RGT6TR")
    assert st.cleaned == "STM32F405RGT6"
    assert st.base == "STM32F405RG"
    assert st.vendor == "st"
    assert "STM32F405" in st.needles
    ti = normalize_mpn("U2(TPS54331DDAR)")
    assert ti.cleaned == "TPS54331DDAR"
    assert ti.base == "TPS54331"
    assert ti.vendor == "ti"
    assert normalize_mpn("U12").cleaned == ""
    assert normalize_mpn("10K").cleaned == ""


def test_nexar_hit_is_verified_and_then_reused(tmp_path):
    url = "https://www.ti.com/lit/gpn/tps54331"
    http = FakeHttp()
    http.nexar["TPS54331"] = [url]
    http.pdfs[url] = _pdf("TPS54331 datasheet")
    store = DatasheetStore(tmp_path)
    first = _run(["TPS54331"], http, store)
    assert first[0].status == "official_api"
    assert first[0].level == "ok"
    assert first[0].sha256
    assert (tmp_path / "files" / f"{first[0].sha256}.pdf").is_file()
    calls = len(http.calls)
    second = _run(["TPS54331"], http, store)
    assert second[0].cached is True
    assert "本地缓存" in second[0].label
    assert len(http.calls) == calls


def test_search_is_used_when_the_api_misses(tmp_path):
    url = "https://www.st.com/resource/en/datasheet/stm32f405rg.pdf"
    http = FakeHttp()
    http.jina["STM32F405RG"] = url
    http.pdfs[url] = _pdf("STM32F405RG datasheet")
    found = _run(["STM32F405RGT6TR"], http, DatasheetStore(tmp_path))
    assert found[0].status == "fetched"
    assert found[0].tier == "jina"
    assert found[0].level == "warn"
    assert "92" not in found[0].label
    assert "95" in found[0].label


def test_vendor_template_is_the_last_step(tmp_path):
    url = "https://www.ti.com/lit/gpn/tps54331"
    http = FakeHttp()
    http.pdfs[url] = _pdf("TPS54331 datasheet")
    found = _run(["TPS54331DDAR"], http, DatasheetStore(tmp_path), credentials=DatasheetCredentials())
    assert found[0].tier == "ti"
    assert any(kind == "get" and url in target for kind, target in http.calls)
    assert not any(kind == "gql" for kind, _ in http.calls)


def test_errata_and_wrong_part_are_not_stored(tmp_path):
    url = "https://www.ti.com/lit/gpn/tps54331"
    http = FakeHttp()
    http.nexar["TPS54331"] = [url]
    http.pdfs[url] = _pdf("TPS54331 errata")

    def errata(text, mpn):
        return DatasheetVerdict(is_valid_datasheet=False, matched_mpn="TPS54331", confidence=0.99, document_type="Errata")

    store = DatasheetStore(tmp_path)
    found = _run(["TPS54331"], http, store, verifier=errata)
    assert found[0].status == "rejected"
    assert "勘误表" in found[0].note
    assert list((tmp_path / "files").glob("*.pdf")) == []


def test_pdf_that_does_not_mention_the_part_skips_verification(tmp_path):
    url = "https://www.ti.com/lit/gpn/tps54331"
    http = FakeHttp()
    http.nexar["TPS54331"] = [url]
    http.jina["TPS54331"] = url
    http.pdfs[url] = _pdf("USER GUIDE ONLY")
    seen = []

    def verifier(text, mpn):
        seen.append(mpn)
        return _accept(text, mpn)

    found = _run(["TPS54331"], http, DatasheetStore(tmp_path), verifier=verifier)
    assert seen == []
    assert found[0].status == "rejected"
    assert "没有这个料号" in found[0].note


def test_private_and_non_pdf_links_are_dropped(tmp_path):
    html_url = "https://www.ti.com/lit/gpn/gd32f103c8"
    http = FakeHttp()
    http.jina["GD32F103C8"] = f"http://127.0.0.1/secret.pdf {html_url}"
    http.pdfs[html_url] = b"<html>GD32F103C8</html>"
    found = _run(["GD32F103C8T6"], http, DatasheetStore(tmp_path))
    assert found[0].status == "missing"
    assert not any(kind == "get" and "127.0.0.1" in target for kind, target in http.calls)
    assert any(kind == "get" and target == html_url for kind, target in http.calls)
    assert list(tmp_path.glob("files/*.pdf")) == []


def test_network_stays_off_until_outbound_is_allowed(tmp_path):
    http = FakeHttp()
    found = _run(["TPS54331"], http, DatasheetStore(tmp_path), allow_network=False)
    assert found[0].status == "blocked"
    assert http.calls == []


def test_failed_refresh_keeps_the_saved_file(tmp_path):
    url = "https://www.ti.com/lit/gpn/tps54331"
    http = FakeHttp()
    http.nexar["TPS54331"] = [url]
    http.pdfs[url] = _pdf("TPS54331 datasheet")
    store = DatasheetStore(tmp_path)
    first = _run(["TPS54331"], http, store)
    http.nexar.clear()
    http.pdfs.clear()
    again = _run(["TPS54331"], http, store, refresh=True)
    assert again[0].sha256 == first[0].sha256
    assert "仍使用" in again[0].note


def test_manual_upload_rejects_a_different_part(tmp_path):
    store = DatasheetStore(tmp_path)
    with pytest.raises(Exception, match="没有这个料号"):
        store_manual("TPS54331", _pdf("STM32F405RG"), store=store)
    saved = store_manual("TPS54331", _pdf("TPS54331 datasheet"), store=store)
    assert saved.status == "manual"
    assert saved.level == "warn"


def test_verdict_parser_accepts_fenced_json_and_percent_confidence():
    verdict = parse_verdict(
        '```json\n{"is_valid_datasheet": true, "matched_mpn": "TPS54331", "confidence": 95, "document_type": "Datasheet"}\n```'
    )
    assert verdict.confidence == 0.95
    parsed = normalize_mpn("TPS54331")
    assert verdict_problem(verdict, parsed) == ""
    weak = verdict.model_copy(update={"confidence": 0.7})
    assert "匹配度不够" in verdict_problem(weak, parsed)
    other = verdict.model_copy(update={"matched_mpn": "TPS54332"})
    assert "对不上" in verdict_problem(other, parsed)


def test_resolve_route_and_cached_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def fake(mpns, refresh=False):
        return [
            DatasheetMatch(
                mpn=mpns[0],
                base_mpn="TPS54331",
                status="missing",
                level="bad",
                label="抓取失败，请手动补充 PDF",
                note="没有找到可下载的 Datasheet",
            )
        ]

    monkeypatch.setattr("app.schematic.api.resolve_datasheets", fake)
    empty = client.post("/api/schematic/datasheets/resolve", json={"mpns": []})
    assert empty.status_code == 422
    missing = client.post("/api/schematic/datasheets/resolve", json={"mpns": ["TPS54331"]})
    assert missing.status_code == 200
    assert missing.json()["items"][0]["status"] == "missing"
    assert client.get("/api/schematic/datasheets/file/not-a-hash").status_code == 404

    saved = store_manual("TPS54331", _pdf("TPS54331 datasheet"), store=DatasheetStore(Path(tmp_path) / "datasheets"))
    fetched = client.get(f"/api/schematic/datasheets/file/{saved.sha256}")
    assert fetched.status_code == 200
    assert fetched.content.startswith(b"%PDF")
    text = client.get(f"/api/schematic/datasheets/text/{saved.sha256}")
    assert text.status_code == 200
    assert "TPS54331" in text.json()["text"]

    uploaded = client.post(
        "/api/schematic/datasheets/upload",
        data={"mpn": "TPS54331"},
        files={"file": ("tps.pdf", _pdf("TPS54331 datasheet"), "application/pdf")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["status"] == "manual"
    rejected = client.post(
        "/api/schematic/datasheets/upload",
        data={"mpn": "TPS54331"},
        files={"file": ("other.pdf", _pdf("UNRELATED"), "application/pdf")},
    )
    assert rejected.status_code == 400
