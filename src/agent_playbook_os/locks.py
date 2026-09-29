from __future__ import annotations

from .models import CompiledPlan, PlaybookLock


def lock_from_plan(plan: CompiledPlan) -> PlaybookLock:
    return PlaybookLock(
        playbook_id=plan.playbook_id,
        playbook_version=plan.playbook_version,
        playbook_hash=plan.playbook_hash,
        plan_integrity_hash=plan.integrity_hash,
        plan_semantic_hash=plan.semantic_hash,
        skills=plan.skill_lock,
    )
