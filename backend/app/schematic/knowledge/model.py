"""设计资料和可执行约束。

未知数值保持为空。提取结果在人工复核前不能单独支撑确定结论。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ConstraintType = Literal["hard_limit", "recommended", "conditional", "reference", "intent"]
VerificationStatus = Literal["unverified", "verified", "ambiguous", "obsolete"]
EffectiveStatus = Literal["current", "superseded", "applicability_unconfirmed"]
Origin = Literal["document_fact", "extracted_candidate", "calculated", "inferred"]
ExtractionMethod = Literal["text", "table", "ocr", "manual", "calculated", "inferred"]
ParseStatus = Literal["parsed", "partial", "not_parsed", "needs_review"]
DocumentKind = Literal[
    "product_spec",
    "architecture",
    "block_diagram",
    "power_tree",
    "interface_spec",
    "bom",
    "pcb_constraint",
    "som_guide",
    "datasheet",
    "reference_design",
    "application_note",
    "errata",
    "layout_guide",
    "reference_schematic",
    "pin_table",
    "other",
]


class KnowledgeError(ValueError):
    pass


class DesignParameter(BaseModel):
    name: str
    value: float | None = None
    value_text: str = ""
    min_value: float | None = None
    max_value: float | None = None
    unit: str = ""
    tolerance_pct: float | None = None
    condition: str = ""


class Applicability(BaseModel):
    manufacturer: str = ""
    mpn: str = ""
    order_suffix: str = ""
    package: str = ""
    revision: str = ""
    symbol: str = ""
    refdes: str = ""
    pin: str = ""
    pin_name: str = ""
    net: str = ""
    voltage_domain: str = ""
    interface: str = ""
    side: str = ""
    role: str = ""
    condition: str = ""
    operating_mode: str = ""


class DesignDocument(BaseModel):
    document_id: str
    project_id: str = ""
    product_id: str = ""
    title: str
    kind: DocumentKind = "other"
    revision: str = ""
    filename: str = ""
    sha256: str = ""
    parse_status: ParseStatus = "needs_review"
    parse_note: str = ""
    page_count: int | None = None
    unparsed_pages: list[int] = Field(default_factory=list)
    ocr_pages: list[int] = Field(default_factory=list)
    ocr_note: str = ""
    ocr_text: str = ""
    created_at: str = ""
    updated_at: str = ""


class DesignConstraint(BaseModel):
    constraint_id: str
    project_id: str = ""
    product_id: str = ""
    category: str
    title: str
    description: str = ""
    constraint_type: ConstraintType
    applicability: Applicability = Field(default_factory=Applicability)
    parameters: list[DesignParameter] = Field(default_factory=list)
    validation_method: str = ""
    source_document_id: str = ""
    source_revision: str = ""
    source_page: int | None = None
    source_section: str = ""
    source_quote: str = ""
    extraction_method: ExtractionMethod = "text"
    origin: Origin = "extracted_candidate"
    verification_status: VerificationStatus = "unverified"
    effective_status: EffectiveStatus = "current"
    superseded_by: str = ""
    created_at: str = ""
    updated_at: str = ""


class ConstraintConflict(BaseModel):
    conflict_id: str
    parameter: str
    subject: str
    constraint_ids: list[str] = Field(default_factory=list)
    reason: str
    detail: str = ""
    fingerprint: str = ""
    status: Literal["open", "resolved"] = "open"


class DeviceMatch(BaseModel):
    refdes: str
    schematic_mpn: str = ""
    manufacturer: str = ""
    package: str = ""
    order_suffix: str = ""
    revision: str = ""
    symbol: str = ""
    status: Literal["unique", "ambiguous", "missing"] = "missing"
    matched_mpns: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    note: str = ""
