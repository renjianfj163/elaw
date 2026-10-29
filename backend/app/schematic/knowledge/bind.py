"""把资料库里的约束绑到网表上的器件、引脚和接口。

型号对上多个订货号时不自动挑一个。推荐值留在原处，不会升成硬性限值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.hashing import stable_hash
from app.schematic.engine.calculators import compare
from app.schematic.engine.model import Circuit, Constraint, Endpoint, SignalGroup
from app.schematic.knowledge.model import ConstraintConflict, DesignConstraint, DesignDocument, DeviceMatch
from app.schematic.knowledge.store import KnowledgeStore, _describe, _primary

_ROLES = {"mcu", "som", "module", "regulator", "level_shifter", "usb_phy", "phy", "opamp", "crystal", "io"}


@dataclass
class Withheld:
    constraint: DesignConstraint
    refdes: str
    reason: str


@dataclass
class Bound:
    constraint: DesignConstraint
    document: DesignDocument | None
    refdes: str
    engine: Constraint


@dataclass
class OpenConflict:
    conflict_id: str
    reason: str
    detail: str
    evidence: list[Constraint] = field(default_factory=list)
    components: list[str] = field(default_factory=list)


@dataclass
class KnowledgeIndex:
    matches: list[DeviceMatch]
    bindings: list[Bound]
    withheld: list[Withheld]
    product: list[DesignConstraint]
    conflicts: list[OpenConflict]
    active: bool

    def fingerprint(self) -> str:
        rows = []
        for item in self.bindings:
            constraint = item.constraint
            rows.append(
                {
                    "id": constraint.constraint_id,
                    "refdes": item.refdes,
                    "revision": constraint.source_revision,
                    "verification": constraint.verification_status,
                    "effective": constraint.effective_status,
                    "type": constraint.constraint_type,
                    "quote": constraint.source_quote,
                }
            )
        withheld = [
            {"id": item.constraint.constraint_id, "refdes": item.refdes, "reason": item.reason} for item in self.withheld
        ]
        return stable_hash({"bindings": rows, "withheld": withheld, "conflicts": [item.conflict_id for item in self.conflicts]})

    def engine_constraints(self, comp, parameter: str, pin: str | None = None) -> list[Constraint]:
        found = [
            item.engine
            for item in self.bindings
            if item.refdes == comp.refdes
            and item.engine.parameter == parameter
            and item.constraint.constraint_type == "hard_limit"
            and _pin_applies(item.constraint, comp, pin)
        ]
        if len({(item.value, item.min_value, item.max_value, item.unit.lower(), item.value_text) for item in found}) > 1:
            return []
        return found

    def cite(self, refdes: str, parameter: str, pin: str | None = None) -> list[Constraint]:
        return [
            item.engine
            for item in self.bindings
            if item.refdes == refdes and item.engine.parameter == parameter and _cite_pin(item.constraint, pin)
        ]

    def primary(self, comp, pin: str, names: tuple[str, ...]):
        for name in names:
            if self.block_reason(comp, pin, name):
                continue
            found = self.engine_constraints(comp, name, pin)
            if found:
                return found[0]
        return None

    def block_reason(self, comp, pin: str | None, parameter: str) -> str:
        if self.engine_constraints(comp, parameter, pin):
            return ""
        reasons = [
            item.reason
            for item in self.withheld
            if item.refdes == comp.refdes and _name(item.constraint) == parameter and _withheld_pin(item.constraint, comp, pin)
        ]
        if any(reason == "conflict" for reason in reasons):
            return "约束冲突未消解，不能选取其中一个数值。"
        if any(reason == "ambiguous" for reason in reasons):
            return "型号不完整，存在多个可能匹配，未自动选择。"
        if any(reason == "unconfirmed" for reason in reasons):
            return "文档修订后约束适用性待确认。"
        if any(reason == "unverified" for reason in reasons):
            return "约束尚未复核，不能作出确定结论。"
        if any(reason == "conditional" for reason in reasons):
            return "约束适用条件尚未确认。"
        if any(reason == "recommended" for reason in reasons):
            return "约束类型是推荐，不能当作强制限值。"
        if any(reason == "suffix" for reason in reasons):
            return "订货后缀和资料适用范围不一致。"
        kinds = [
            item.constraint.constraint_type
            for item in self.bindings
            if item.refdes == comp.refdes
            and item.engine.parameter == parameter
            and _pin_applies(item.constraint, comp, pin)
        ]
        if "conditional" in kinds:
            return "约束适用条件尚未确认。"
        if "recommended" in kinds:
            return "约束类型是推荐，不能当作强制限值。"
        return ""

    def open_conflicts(self) -> list[OpenConflict]:
        return list(self.conflicts)

    def missing_interfaces(self, circuit: Circuit) -> list[str]:
        missing: list[str] = []
        present = {group.kind for group in circuit.signal_groups}
        for item in self.product:
            if not _usable_hard(item) or _name(item) != "require_interface":
                continue
            parameter = _primary(item)
            kind = (parameter.value_text if parameter is not None else "").strip().lower()
            if kind and kind not in present and kind not in missing:
                missing.append(kind)
        return missing


def bind_circuit(circuit: Circuit, store: KnowledgeStore) -> tuple[Circuit, KnowledgeIndex]:
    copied = circuit.model_copy(deep=True)
    documents = store.documents
    matches: list[DeviceMatch] = []
    bindings: list[Bound] = []
    withheld: list[Withheld] = []
    product = [
        item
        for item in store.constraints.values()
        if not item.applicability.mpn and not item.applicability.refdes
    ]
    for comp in copied.components.values():
        if not _interesting(comp):
            refdes_only = _refdes_constraints(store, comp.refdes)
            if not refdes_only and not comp.mpn:
                continue
        status, chosen, note, candidates = _select(comp, list(store.constraints.values()))
        document_ids = sorted({item.source_document_id for item in chosen if item.source_document_id})
        matches.append(
            DeviceMatch(
                refdes=comp.refdes,
                schematic_mpn=comp.mpn or "",
                manufacturer=comp.manufacturer or "",
                package=comp.package or "",
                status=status,  # type: ignore[arg-type]
                matched_mpns=sorted({item.applicability.mpn for item in chosen if item.applicability.mpn}),
                document_ids=document_ids,
                candidates=candidates,
                note=note,
            )
        )
        if status == "ambiguous":
            for item in chosen:
                withheld.append(Withheld(item, comp.refdes, "ambiguous"))
            continue
        if status == "missing":
            continue
        for item in chosen:
            reason = _hold_reason(item, comp)
            if reason:
                withheld.append(Withheld(item, comp.refdes, reason))
                continue
            bindings.append(
                Bound(
                    constraint=item,
                    document=documents.get(item.source_document_id),
                    refdes=comp.refdes,
                    engine=_engine(item, comp.refdes, documents.get(item.source_document_id)),
                )
            )
    conflict_ids, conflicts = _local_conflicts(bindings)
    if conflict_ids:
        kept: list[Bound] = []
        for item in bindings:
            if item.constraint.constraint_id in conflict_ids and item.constraint.constraint_type == "hard_limit":
                withheld.append(Withheld(item.constraint, item.refdes, "conflict"))
                continue
            kept.append(item)
        bindings = kept
    _add_signal_groups(copied, bindings, withheld)
    active = bool(store.documents or store.constraints)
    return copied, KnowledgeIndex(matches, bindings, withheld, product, conflicts, active)


def _select(comp, constraints: list[DesignConstraint]):
    exact: list[DesignConstraint] = []
    family: list[DesignConstraint] = []
    narrower: list[DesignConstraint] = []
    suffix_bad: list[DesignConstraint] = []
    refdes_hits: list[DesignConstraint] = []
    for item in constraints:
        if item.effective_status == "superseded" or item.verification_status == "obsolete":
            continue
        app = item.applicability
        if app.refdes and app.refdes != comp.refdes:
            continue
        if app.refdes == comp.refdes and not app.mpn:
            refdes_hits.append(item)
            continue
        if not app.mpn:
            continue
        relation = _relation(comp.mpn or "", app.mpn, app.order_suffix)
        if relation == "exact":
            exact.append(item)
        elif relation == "family":
            family.append(item)
        elif relation == "narrower":
            narrower.append(item)
        elif relation == "suffix":
            suffix_bad.append(item)
    component_key = _key(comp.mpn or "")
    narrower_keys = sorted({_key(item.applicability.mpn) for item in narrower})
    if not exact and len(narrower_keys) > 1 and component_key and all(item.startswith(component_key) for item in narrower_keys):
        return "ambiguous", narrower, "型号不完整，存在多个可能匹配，未自动选择。", narrower_keys
    chosen = exact or family
    chosen = chosen + [item for item in suffix_bad if item not in chosen]
    chosen = chosen + [item for item in refdes_hits if item not in chosen]
    if not chosen:
        if not component_key and not refdes_hits:
            return "missing", [], "原理图没有可核对的型号。", []
        return "missing", [], "", []
    return "unique", chosen, "", []


def _relation(component_mpn: str, constraint_mpn: str, suffix: str) -> str:
    left = _key(component_mpn)
    right = _key(constraint_mpn)
    if not left or not right:
        return "none"
    if left == right:
        base = "exact"
    elif left.startswith(right):
        base = "family"
    elif right.startswith(left):
        base = "narrower"
    else:
        return "none"
    if suffix and _key(suffix) not in left:
        return "suffix"
    return base


def _hold_reason(item: DesignConstraint, comp) -> str:
    app = item.applicability
    if app.order_suffix and _key(app.order_suffix) not in _key(comp.mpn or ""):
        return "suffix"
    if item.effective_status == "applicability_unconfirmed":
        return "unconfirmed"
    if item.verification_status == "unverified":
        return "unverified"
    if item.verification_status == "ambiguous":
        return "unverified"
    if item.verification_status != "verified" or item.effective_status != "current":
        return "unverified"
    return ""


def _local_conflicts(bindings: list[Bound]) -> tuple[set[str], list[OpenConflict]]:
    groups: dict[tuple[str, str, str], list[Bound]] = {}
    for item in bindings:
        if item.constraint.constraint_type != "hard_limit":
            continue
        pin = item.constraint.applicability.pin or item.constraint.applicability.pin_name or ""
        groups.setdefault((item.refdes, pin, item.engine.parameter), []).append(item)
    blocked: set[str] = set()
    conflicts: list[OpenConflict] = []
    for (refdes, pin, parameter), items in groups.items():
        signatures = {(item.engine.value, item.engine.min_value, item.engine.max_value, item.engine.unit.lower(), item.engine.value_text) for item in items}
        if len(signatures) < 2:
            continue
        ids = {item.constraint.constraint_id for item in items}
        blocked.update(ids)
        conflicts.append(_open(refdes, pin, parameter, items, "同一参数有多条已复核硬性限值，数值不一致。"))
    by_pin: dict[tuple[str, str], list[Bound]] = {}
    for item in bindings:
        if item.constraint.constraint_type != "hard_limit":
            continue
        pin = item.constraint.applicability.pin or item.constraint.applicability.pin_name or ""
        by_pin.setdefault((item.refdes, pin), []).append(item)
    for (refdes, pin), items in by_pin.items():
        requirements = [item for item in items if item.engine.parameter in {"required_io_v", "required_vh"}]
        limits = [item for item in items if item.engine.parameter in {"abs_max_v", "tolerant_v"}]
        for requirement in requirements:
            for limit in limits:
                low = requirement.engine.min_value if requirement.engine.min_value is not None else requirement.engine.value
                high = limit.engine.max_value if limit.engine.max_value is not None else limit.engine.value
                if low is None or high is None or compare(">", low, high)["result"] != "true":
                    continue
                ids = {requirement.constraint.constraint_id, limit.constraint.constraint_id}
                blocked.update(ids)
                conflicts.append(
                    _open(
                        refdes,
                        pin,
                        f"{requirement.engine.parameter}<>{limit.engine.parameter}",
                        [requirement, limit],
                        "产品或接口要求的电压高于器件允许的上限。",
                    )
                )
    return blocked, conflicts


def _open(refdes: str, pin: str, parameter: str, items: list[Bound], reason: str) -> OpenConflict:
    ids = sorted(item.constraint.constraint_id for item in items)
    return OpenConflict(
        conflict_id=stable_hash({"refdes": refdes, "pin": pin, "parameter": parameter, "ids": ids})[:16],
        reason=reason,
        detail="；".join(_describe(item.constraint) for item in items),
        evidence=[item.engine for item in items],
        components=[refdes],
    )


def _add_signal_groups(circuit: Circuit, bindings: list[Bound], withheld: list[Withheld]) -> None:
    grouped: dict[str, list[Endpoint]] = {}
    for item in bindings:
        if item.engine.parameter != "pin_role" or item.constraint.constraint_type != "hard_limit":
            continue
        app = item.constraint.applicability
        role = item.engine.value_text or app.role
        kind = (app.interface or item.engine.interface or "").strip().lower()
        mode = (item.engine.operating_mode or app.operating_mode or "").strip()
        selected = (circuit.components[item.refdes].mode or "").strip() if item.refdes in circuit.components else ""
        if mode and mode != selected:
            continue
        if not kind or not app.side or not role or not app.pin:
            withheld.append(Withheld(item.constraint, item.refdes, "incomplete"))
            continue
        grouped.setdefault(kind, []).append(
            Endpoint(component=item.refdes, pin=app.pin, role=role.strip().lower(), side=app.side)
        )
    existing = {(group.kind, tuple(sorted((item.component, item.pin, item.role, item.side) for item in group.endpoints))) for group in circuit.signal_groups}
    for kind, endpoints in grouped.items():
        if not any(item.side == "host" for item in endpoints) or not any(item.side == "device" for item in endpoints):
            continue
        key = (kind, tuple(sorted((item.component, item.pin, item.role, item.side) for item in endpoints)))
        if key in existing:
            continue
        circuit.signal_groups.append(SignalGroup(name=kind.upper(), kind=kind, endpoints=endpoints))


def _engine(item: DesignConstraint, refdes: str, document: DesignDocument | None) -> Constraint:
    parameter = _primary(item)
    title = document.title if document is not None else ""
    revision = item.source_revision or (document.revision if document is not None else "")
    return Constraint(
        parameter=parameter.name if parameter is not None else "",
        mpn=item.applicability.mpn,
        refdes=refdes,
        pin=item.applicability.pin,
        value=None if parameter is None else parameter.value,
        min_value=None if parameter is None else parameter.min_value,
        max_value=None if parameter is None else parameter.max_value,
        tolerance_pct=None if parameter is None else parameter.tolerance_pct,
        unit="" if parameter is None else parameter.unit,
        value_text="" if parameter is None else parameter.value_text,
        condition=item.applicability.condition,
        source=title,
        revision=revision,
        verified=item.verification_status == "verified" and item.effective_status == "current",
        constraint_id=item.constraint_id,
        constraint_type=item.constraint_type,
        origin=item.origin,
        document_title=title,
        source_page=item.source_page,
        source_section=item.source_section,
        source_quote=item.source_quote,
        verification_status=item.verification_status,
        interface=item.applicability.interface,
        side=item.applicability.side,
        operating_mode=item.applicability.operating_mode,
    )


def _pin_applies(constraint: DesignConstraint, comp, pin: str | None) -> bool:
    app = constraint.applicability
    if pin is None:
        return not app.pin and not app.pin_name
    if not app.pin and not app.pin_name:
        return True
    return _pin_hit(comp, app, pin)


def _cite_pin(constraint: DesignConstraint, pin: str | None) -> bool:
    app = constraint.applicability
    if pin is None:
        return not app.pin and not app.pin_name
    if not app.pin and not app.pin_name:
        return True
    return pin in {app.pin, app.pin_name}


def _withheld_pin(constraint: DesignConstraint, comp, pin: str | None) -> bool:
    return _pin_applies(constraint, comp, pin)


def _pin_hit(comp, app, pin: str) -> bool:
    names = {pin}
    for item in comp.pins:
        if item.number == pin or item.name == pin:
            names.add(item.number)
            if item.name:
                names.add(item.name)
    return app.pin in names or app.pin_name in names


def _interesting(comp) -> bool:
    return comp.kind == "ic" or comp.role in _ROLES or bool(comp.mpn)


def _refdes_constraints(store: KnowledgeStore, refdes: str) -> list[DesignConstraint]:
    return [item for item in store.constraints.values() if item.applicability.refdes == refdes]


def _usable_hard(item: DesignConstraint) -> bool:
    return item.constraint_type == "hard_limit" and item.verification_status == "verified" and item.effective_status == "current"


def _name(item: DesignConstraint) -> str:
    parameter = _primary(item)
    return parameter.name if parameter is not None else ""


def _key(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())
