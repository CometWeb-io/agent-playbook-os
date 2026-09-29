from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .integrity import canonical_hash
from .loader import load_playbook
from .models import Playbook, RunStatus
from .storage import open_run_store


class PromotionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_hash: str
    playbook_id: str
    playbook_version: str
    run_id: str
    status: str
    goal: str | None = None
    plan_semantic_hash: str
    observed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    prev_hash: str | None = None
    record_hash: str = "pending"


class PromotionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_hash: str
    observations: int
    successes: int
    failures: int
    run_ids: list[str]
    ready: bool
    min_successes: int
    distinct_goals: int = 0
    min_distinct_goals: int = 1
    registry_head_hash: str | None = None


class PromotionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_hash: str
    destination: str
    promoted_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    actor: str
    observations: int
    successes: int
    source_playbook_id: str
    source_playbook_version: str
    promoted_playbook_id: str
    promoted_playbook_version: str
    registry_head_hash: str | None = None


class PromotionRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[PromotionObservation]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(PromotionObservation.model_validate(json.loads(line)))
        return rows

    def verify(self) -> tuple[bool, str | None]:
        previous = None
        for index, row in enumerate(self._read()):
            if row.prev_hash != previous:
                return False, f"promotion registry prev_hash mismatch at index={index}"
            data = row.model_dump(mode="json")
            expected = data.pop("record_hash", None)
            actual = canonical_hash(data)
            if expected != actual:
                return False, f"promotion registry record_hash mismatch at index={index}"
            previous = row.record_hash
        return True, None

    def record(self, playbook: Playbook, run_dir: str | Path, *, goal: str | None = None) -> PromotionObservation:
        store = open_run_store(run_dir)
        state = store.load_state()
        candidate_hash = canonical_hash(playbook.model_dump(mode="json", by_alias=True))
        existing_rows = self._read()
        ok, error = self.verify()
        if not ok:
            raise ValueError(f"refusing to append to invalid promotion registry: {error}")
        obs = PromotionObservation(
            candidate_hash=candidate_hash,
            playbook_id=playbook.metadata.id,
            playbook_version=playbook.metadata.version,
            run_id=state.run_id,
            status=state.status.value,
            goal=goal,
            plan_semantic_hash=state.plan_semantic_hash,
            prev_hash=existing_rows[-1].record_hash if existing_rows else None,
        )
        payload = obs.model_dump(mode="json")
        payload.pop("record_hash", None)
        obs.record_hash = canonical_hash(payload)
        existing = {(x.candidate_hash, x.run_id) for x in existing_rows}
        if (obs.candidate_hash, obs.run_id) not in existing:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(obs.model_dump_json() + "\n")
        return obs

    def summary(self, playbook: Playbook, *, min_successes: int = 3, min_distinct_goals: int = 1) -> PromotionSummary:
        if min_successes < 1:
            raise ValueError("min_successes must be positive")
        if min_distinct_goals < 1:
            raise ValueError("min_distinct_goals must be positive")
        ok, error = self.verify()
        if not ok:
            raise ValueError(f"invalid promotion registry: {error}")
        candidate_hash = canonical_hash(playbook.model_dump(mode="json", by_alias=True))
        rows = [x for x in self._read() if x.candidate_hash == candidate_hash]
        successes = sum(x.status == RunStatus.COMPLETED.value for x in rows)
        failures = sum(x.status in {RunStatus.FAILED.value, RunStatus.CANCELLED.value} for x in rows)
        distinct_goals = len({x.goal for x in rows if x.goal})
        return PromotionSummary(
            candidate_hash=candidate_hash,
            observations=len(rows),
            successes=successes,
            failures=failures,
            run_ids=sorted({x.run_id for x in rows}),
            ready=successes >= min_successes and failures == 0 and distinct_goals >= min_distinct_goals,
            min_successes=min_successes,
            distinct_goals=distinct_goals,
            min_distinct_goals=min_distinct_goals,
            registry_head_hash=self._read()[-1].record_hash if self._read() else None,
        )

    def promote(
        self,
        playbook: Playbook,
        destination: str | Path,
        *,
        actor: str,
        min_successes: int = 3,
        min_distinct_goals: int = 1,
        new_id: str | None = None,
        new_version: str | None = None,
        force: bool = False,
    ) -> PromotionReceipt:
        summary = self.summary(playbook, min_successes=min_successes, min_distinct_goals=min_distinct_goals)
        if not summary.ready and not force:
            raise ValueError(
                f"candidate is not promotion-ready: successes={summary.successes} failures={summary.failures} "
                f"required_successes={min_successes} distinct_goals={summary.distinct_goals} "
                f"required_distinct_goals={min_distinct_goals}"
            )
        promoted = playbook.model_copy(deep=True)
        if new_id:
            promoted.metadata.id = new_id
        if new_version:
            promoted.metadata.version = new_version
        if "ephemeral" in promoted.metadata.version.lower() and new_version is None and not force:
            raise ValueError("promotion of an ephemeral version requires new_version unless force=True")
        promoted.metadata.tags = [x for x in promoted.metadata.tags if x not in {"ephemeral", "operator-generated"}]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            yaml.safe_dump(promoted.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        receipt = PromotionReceipt(
            candidate_hash=summary.candidate_hash,
            destination=str(destination),
            actor=actor,
            observations=summary.observations,
            successes=summary.successes,
            source_playbook_id=playbook.metadata.id,
            source_playbook_version=playbook.metadata.version,
            promoted_playbook_id=promoted.metadata.id,
            promoted_playbook_version=promoted.metadata.version,
            registry_head_hash=summary.registry_head_hash,
        )
        destination.with_suffix(destination.suffix + ".promotion.json").write_text(
            receipt.model_dump_json(indent=2), encoding="utf-8"
        )
        return receipt


def load_candidate(path: str | Path) -> Playbook:
    return load_playbook(path)
