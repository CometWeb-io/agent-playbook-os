# ADR-037: Ephemeral plans require explicit plan approval

**Status:** Accepted

A newly generated playbook is persisted for review and must receive a distinct plan-level approval before the Runner creates executable state. Re-running a planner is not a substitute for reviewing the exact compiled artifact; `run-plan` exists for that boundary.
