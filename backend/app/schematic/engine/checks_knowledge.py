"""设计资料是否互相冲突，以及产品要求的接口有没有足够的引脚约束。"""

from __future__ import annotations

from app.schematic.engine.report import NOT_APPLICABLE, CheckResult, finish, make_finding


def check_kn_001(view, rule) -> CheckResult:
    knowledge = view.knowledge
    if knowledge is None or not knowledge.active:
        return NOT_APPLICABLE
    findings = []
    for conflict in knowledge.open_conflicts():
        findings.append(
            make_finding(
                rule,
                title="设计约束互相冲突",
                status="needs_review",
                severity="high",
                description=f"{conflict.reason}{conflict.detail}",
                components=conflict.components,
                constraints=conflict.evidence,
                missing=["conflict_resolution"],
                verification_steps=["保留各方原始约束，由设计者确定接口方案后再复核。不要取平均，也不要采用后上传的那一份。"],
            )
        )
    for name in knowledge.missing_interfaces(view.circuit):
        findings.append(
            make_finding(
                rule,
                title=f"{name} 接口映射缺少已复核的引脚约束",
                status="not_assessable",
                severity="info",
                description=(
                    f"产品要求检查 {name}，但没有足够的已复核引脚角色可建立端到端映射。"
                    "缺资料不会被当成通过。"
                ),
                missing=[f"{name}.pin_role"],
            )
        )
    for note in view.constraint_conflicts:
        findings.append(
            make_finding(
                rule,
                title="同一参数的约束数值不一致",
                status="needs_review",
                severity="high",
                description=f"{note} 有多条数值不同的约束。审查没有选取其中一条，也没有取平均。",
                missing=[note],
            )
        )
    if not findings and not knowledge.bindings and not knowledge.withheld and not knowledge.product:
        return NOT_APPLICABLE
    return finish(findings)
