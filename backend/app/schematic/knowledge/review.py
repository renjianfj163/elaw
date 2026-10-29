"""用设计资料审查一张网表，并比较前后两次结论。"""

from __future__ import annotations

from app.schematic.engine.model import Circuit
from app.schematic.engine.report import Report
from app.schematic.engine.runner import review_circuit
from app.schematic.knowledge.bind import bind_circuit
from app.schematic.knowledge.store import KnowledgeStore


def review_with_knowledge(
    circuit: Circuit,
    store: KnowledgeStore,
    *,
    only: list[str] | None = None,
    catalog=None,
) -> Report:
    bound, index = bind_circuit(circuit, store)
    return review_circuit(bound, only=only, catalog=catalog, knowledge=index)


def review_changes(previous: Report, current: Report) -> list[dict[str, str]]:
    """先前确定的问题若不再出现，记为 cleared。不把缺资料写成通过。"""

    def key(item):
        return (item.rule_id, tuple(sorted(item.affected_components)), tuple(sorted(item.affected_nets)))

    before = {key(item): item for item in previous.findings if item.status == "confirmed"}
    after = {key(item): item for item in current.findings}
    changes: list[dict[str, str]] = []
    for identity, item in before.items():
        now = after.get(identity)
        if now is None:
            changes.append({"rule_id": item.rule_id, "from": "confirmed", "to": "cleared", "finding_id": item.finding_id})
        elif now.status != "confirmed":
            changes.append({"rule_id": item.rule_id, "from": "confirmed", "to": now.status, "finding_id": item.finding_id})
    return changes
