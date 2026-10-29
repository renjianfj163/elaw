"""设计资料库。

新文档不会覆盖已有约束。数值冲突会留下来，不会取平均，也不会改成最新上传的那一条。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from pypdf import PdfReader

from app.db import data_dir
from app.hashing import new_id, sha256_bytes, sha256_text, stable_hash, utc_now
from app.schematic.engine.calculators import compare
from app.schematic.knowledge.extract import extract_text, quote_supported
from app.schematic.knowledge.model import (
    ConstraintConflict,
    DesignConstraint,
    DesignDocument,
    DocumentKind,
    KnowledgeError,
)

_VOLTAGE_REQUIREMENTS = {"required_io_v", "required_vh"}
_VOLTAGE_LIMITS = {"abs_max_v", "tolerant_v"}


class KnowledgeStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.documents: dict[str, DesignDocument] = {}
        self.constraints: dict[str, DesignConstraint] = {}
        self.conflicts: dict[str, ConstraintConflict] = {}
        self._texts: dict[str, str] = {}
        self._load()

    def import_text(
        self,
        *,
        title: str,
        text: str,
        kind: DocumentKind = "other",
        revision: str = "",
        project_id: str = "",
        product_id: str = "",
        filename: str = "",
    ) -> DesignDocument:
        digest = sha256_text(text)
        existing = self._by_hash(digest)
        if existing is not None:
            return existing
        document = DesignDocument(
            document_id=new_id(),
            project_id=project_id,
            product_id=product_id,
            title=title,
            kind=kind,
            revision=revision,
            filename=filename,
            sha256=digest,
            created_at=_now(),
            updated_at=_now(),
        )
        extracted = extract_text(text)
        self._adopt(document, text, extracted)
        return document

    def import_pdf(
        self,
        *,
        title: str,
        payload: bytes,
        kind: DocumentKind = "datasheet",
        revision: str = "",
        project_id: str = "",
        product_id: str = "",
        filename: str = "",
        ocr_text: str = "",
        ocr_note: str = "",
        ocr_pages: list[int] | None = None,
    ) -> DesignDocument:
        digest = sha256_bytes(payload)
        existing = self._by_hash(digest)
        if existing is not None:
            return existing
        reader = PdfReader(io.BytesIO(payload))
        pages: dict[int, str] = {}
        chunks: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            body = page.extract_text() or ""
            pages[index] = body
            chunks.append(f"\n--- page {index} ---\n{body}")
        text = "\n".join(chunks)
        if ocr_text.strip():
            text = f"{text.rstrip()}\n\n{ocr_text.strip()}".strip()
        document = DesignDocument(
            document_id=new_id(),
            project_id=project_id,
            product_id=product_id,
            title=title,
            kind=kind,
            revision=revision,
            filename=filename,
            sha256=digest,
            ocr_text=ocr_text.strip(),
            ocr_note=ocr_note.strip(),
            ocr_pages=list(ocr_pages or []),
            created_at=_now(),
            updated_at=_now(),
        )
        extracted = extract_text(text, pages=pages)
        if ocr_pages:
            covered = set(ocr_pages)
            extracted.unparsed_pages = [number for number in extracted.unparsed_pages if number not in covered]
        self._adopt(document, text, extracted)
        return document

    def update_revision(self, document_id: str, *, revision: str, text: str) -> list[str]:
        document = self._document(document_id)
        affected: list[str] = []
        for item in self.constraints.values():
            if item.source_document_id != document_id or item.effective_status != "current":
                continue
            item.effective_status = "applicability_unconfirmed"
            item.updated_at = _now()
            affected.append(item.constraint_id)
        document.revision = revision
        document.sha256 = sha256_text(text)
        document.updated_at = _now()
        extracted = extract_text(text)
        document.parse_status = extracted.parse_status
        document.parse_note = extracted.parse_note
        document.page_count = extracted.page_count
        document.unparsed_pages = list(extracted.unparsed_pages)
        self._texts[document_id] = text
        self._write_text(document_id, text)
        for item in extracted.constraints:
            self._insert(self._stamp(item, document))
        self._rebuild_conflicts()
        self.save()
        return affected

    def verify(self, constraint_id: str) -> DesignConstraint:
        item = self._constraint(constraint_id)
        if item.effective_status != "current":
            raise KnowledgeError("这份约束不是当前有效状态，不能复核通过。")
        if item.verification_status == "obsolete":
            raise KnowledgeError("已废弃的约束不能复核通过。")
        text = self._texts.get(item.source_document_id, "")
        document = self._document(item.source_document_id)
        if item.source_page in document.unparsed_pages:
            item.verification_status = "ambiguous"
            item.updated_at = _now()
            self.save()
            raise KnowledgeError("来源页没有抽出文字，不能复核通过。")
        if not quote_supported(text, item.source_quote):
            item.verification_status = "ambiguous"
            item.updated_at = _now()
            self.save()
            raise KnowledgeError("摘录不在来源文本中，不能复核通过。")
        item.verification_status = "verified"
        item.updated_at = _now()
        self._rebuild_conflicts()
        self.save()
        return item

    def withdraw(self, constraint_id: str) -> DesignConstraint:
        item = self._constraint(constraint_id)
        item.effective_status = "superseded"
        item.updated_at = _now()
        self._rebuild_conflicts()
        self.save()
        return item

    def restore(self, constraint_id: str) -> DesignConstraint:
        item = self._constraint(constraint_id)
        if item.verification_status != "verified":
            raise KnowledgeError("只有已复核的约束可以恢复。")
        if item.effective_status == "applicability_unconfirmed":
            raise KnowledgeError("文档已更新，旧约束不能直接恢复。")
        item.effective_status = "current"
        item.updated_at = _now()
        self._rebuild_conflicts()
        self.save()
        return item

    def affected_by(self, document_id: str) -> list[str]:
        return [
            item.constraint_id
            for item in self.constraints.values()
            if item.source_document_id == document_id and item.effective_status == "applicability_unconfirmed"
        ]

    def query(
        self,
        *,
        mpn: str = "",
        pin: str = "",
        net: str = "",
        voltage_domain: str = "",
        interface: str = "",
        category: str = "",
        document_id: str = "",
        project_id: str = "",
        product_id: str = "",
        constraint_type: str = "",
        verified_only: bool = False,
        current_only: bool = False,
    ) -> list[DesignConstraint]:
        found: list[DesignConstraint] = []
        for item in self.constraints.values():
            app = item.applicability
            if mpn and _key(app.mpn) != _key(mpn):
                continue
            if pin and pin not in {app.pin, app.pin_name}:
                continue
            if net and app.net != net:
                continue
            if voltage_domain and app.voltage_domain != voltage_domain:
                continue
            if interface and app.interface != interface:
                continue
            if category and item.category != category:
                continue
            if document_id and item.source_document_id != document_id:
                continue
            if project_id and item.project_id != project_id:
                continue
            if product_id and item.product_id != product_id:
                continue
            if constraint_type and item.constraint_type != constraint_type:
                continue
            if verified_only and item.verification_status != "verified":
                continue
            if current_only and item.effective_status != "current":
                continue
            found.append(item)
        return found

    def open_conflicts(self) -> list[ConstraintConflict]:
        return [item for item in self.conflicts.values() if item.status == "open"]

    def save(self) -> None:
        payload = {
            "documents": [item.model_dump(mode="json") for item in self.documents.values()],
            "constraints": [item.model_dump(mode="json") for item in self.constraints.values()],
            "conflicts": [item.model_dump(mode="json") for item in self.conflicts.values()],
        }
        target = self.root / "knowledge.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def _adopt(self, document: DesignDocument, text: str, extracted) -> None:
        document.parse_status = extracted.parse_status
        document.parse_note = extracted.parse_note
        document.page_count = extracted.page_count
        document.unparsed_pages = list(extracted.unparsed_pages)
        self.documents[document.document_id] = document
        self._texts[document.document_id] = text
        self._write_text(document.document_id, text)
        for item in extracted.constraints:
            self._insert(self._stamp(item, document))
        self._rebuild_conflicts()
        self.save()

    def _stamp(self, item: DesignConstraint, document: DesignDocument) -> DesignConstraint:
        item.project_id = document.project_id
        item.product_id = document.product_id
        item.source_document_id = document.document_id
        item.source_revision = document.revision
        item.created_at = _now()
        item.updated_at = item.created_at
        if not item.constraint_id:
            item.constraint_id = new_id()
        return item

    def _insert(self, item: DesignConstraint) -> None:
        if item.constraint_id in self.constraints:
            item.constraint_id = new_id()
            note = "未覆盖已有同号约束。"
            item.description = f"{item.description} {note}".strip()
        self.constraints[item.constraint_id] = item

    def _rebuild_conflicts(self) -> None:
        fresh = _detect_conflicts(list(self.constraints.values()))
        self.conflicts = {item.conflict_id: item for item in fresh}

    def _by_hash(self, digest: str) -> DesignDocument | None:
        for item in self.documents.values():
            if item.sha256 == digest:
                return item
        return None

    def _document(self, document_id: str) -> DesignDocument:
        try:
            return self.documents[document_id]
        except KeyError as exc:
            raise KnowledgeError("找不到这份资料。") from exc

    def _constraint(self, constraint_id: str) -> DesignConstraint:
        try:
            return self.constraints[constraint_id]
        except KeyError as exc:
            raise KnowledgeError("找不到这条约束。") from exc

    def _load(self) -> None:
        target = self.root / "knowledge.json"
        if not target.exists():
            return
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.documents = {item["document_id"]: DesignDocument.model_validate(item) for item in payload.get("documents") or []}
        self.constraints = {
            item["constraint_id"]: DesignConstraint.model_validate(item) for item in payload.get("constraints") or []
        }
        self.conflicts = {item["conflict_id"]: ConstraintConflict.model_validate(item) for item in payload.get("conflicts") or []}
        for document_id in self.documents:
            path = self.root / "sources" / f"{document_id}.txt"
            if path.exists():
                self._texts[document_id] = path.read_text(encoding="utf-8")

    def _write_text(self, document_id: str, text: str) -> None:
        folder = self.root / "sources"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{document_id}.txt").write_text(text, encoding="utf-8")


def open_default_store() -> KnowledgeStore:
    return KnowledgeStore(data_dir() / "design_knowledge")


def _detect_conflicts(constraints: list[DesignConstraint]) -> list[ConstraintConflict]:
    usable = [item for item in constraints if _hard_current(item)]
    grouped: dict[tuple[str, str], list[DesignConstraint]] = {}
    for item in usable:
        parameter = _primary(item)
        if parameter is None:
            continue
        grouped.setdefault((_subject(item), parameter.name), []).append(item)
    found: list[ConstraintConflict] = []
    for (subject, name), items in grouped.items():
        signatures = {_signature(item) for item in items}
        if len(signatures) > 1:
            found.append(
                _conflict(
                    subject,
                    name,
                    items,
                    "同一参数有多条已复核硬性限值，数值不一致。",
                )
            )
    by_subject: dict[str, list[DesignConstraint]] = {}
    for item in usable:
        by_subject.setdefault(_subject(item), []).append(item)
    for subject, items in by_subject.items():
        requirements = [item for item in items if _name(item) in _VOLTAGE_REQUIREMENTS]
        limits = [item for item in items if _name(item) in _VOLTAGE_LIMITS]
        for requirement in requirements:
            for limit in limits:
                low = _bound(requirement, "min")
                high = _bound(limit, "max")
                if low is None or high is None:
                    continue
                if compare(">", low, high)["result"] != "true":
                    continue
                found.append(
                    _conflict(
                        subject,
                        f"{_name(requirement)}<>{_name(limit)}",
                        [requirement, limit],
                        "产品或接口要求的电压高于器件允许的上限。",
                    )
                )
    return found


def _conflict(subject: str, parameter: str, items: list[DesignConstraint], reason: str) -> ConstraintConflict:
    ids = sorted(item.constraint_id for item in items)
    detail = "；".join(_describe(item) for item in items)
    fingerprint = stable_hash({"ids": ids, "values": [_signature(item) for item in items]})
    return ConstraintConflict(
        conflict_id=stable_hash({"subject": subject, "parameter": parameter, "ids": ids})[:16],
        parameter=parameter,
        subject=subject,
        constraint_ids=ids,
        reason=reason,
        detail=detail,
        fingerprint=fingerprint,
        status="open",
    )


def _describe(item: DesignConstraint) -> str:
    parameter = _primary(item)
    if parameter is None:
        return item.constraint_id
    numbers = []
    if parameter.min_value is not None:
        numbers.append(f"最小 {parameter.min_value:g}")
    if parameter.max_value is not None:
        numbers.append(f"最大 {parameter.max_value:g}")
    if parameter.value is not None:
        numbers.append(f"值 {parameter.value:g}")
    if parameter.value_text:
        numbers.append(parameter.value_text)
    unit = parameter.unit
    text = " ".join(numbers)
    if unit:
        text = f"{text} {unit}"
    return f"{item.title}（{item.constraint_id}）：{text}。摘录：{item.source_quote}"


def _hard_current(item: DesignConstraint) -> bool:
    return (
        item.constraint_type == "hard_limit"
        and item.verification_status == "verified"
        and item.effective_status == "current"
    )


def _primary(item: DesignConstraint):
    return item.parameters[0] if item.parameters else None


def _name(item: DesignConstraint) -> str:
    parameter = _primary(item)
    return parameter.name if parameter is not None else ""


def _subject(item: DesignConstraint) -> str:
    app = item.applicability
    who = app.refdes or app.mpn or app.net or app.voltage_domain or "*"
    pin = app.pin or app.pin_name or ""
    return f"{who}|{pin}"


def _signature(item: DesignConstraint) -> tuple:
    parameter = _primary(item)
    if parameter is None:
        return ()
    return (
        parameter.value,
        parameter.min_value,
        parameter.max_value,
        parameter.unit.lower(),
        parameter.value_text,
    )


def _bound(item: DesignConstraint, side: str) -> float | None:
    parameter = _primary(item)
    if parameter is None:
        return None
    if side == "min":
        return parameter.min_value if parameter.min_value is not None else parameter.value
    return parameter.max_value if parameter.max_value is not None else parameter.value


def _key(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def _now() -> str:
    return utc_now().isoformat(timespec="seconds")
