from __future__ import annotations

from typing import Protocol
from pydantic import BaseModel, ConfigDict, Field

from .catalog import CatalogEntry, PlaybookCatalog


class OperatorSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    selected: CatalogEntry | None = None
    alternatives: list[CatalogEntry] = Field(default_factory=list)
    confidence: str = "low"
    rationale: str


class Operator(Protocol):
    async def select(self, goal: str, catalog: PlaybookCatalog) -> OperatorSelection: ...


class DeterministicOperator:
    """Reference router for tests and offline usage.

    Production hosts are expected to replace this with a model-backed operator while
    keeping selection output typed and auditable.
    """

    async def select(self, goal: str, catalog: PlaybookCatalog) -> OperatorSelection:
        matches = catalog.search(goal, limit=5)
        selected = matches[0] if matches else None
        confidence = "medium" if selected and len(matches) == 1 else ("low" if selected else "none")
        rationale = (
            f"Selected by deterministic metadata overlap: {selected.id}" if selected
            else "No catalog entry matched the goal; an adaptive operator may draft an ephemeral playbook."
        )
        return OperatorSelection(
            goal=goal,
            selected=selected,
            alternatives=matches[1:],
            confidence=confidence,
            rationale=rationale,
        )
