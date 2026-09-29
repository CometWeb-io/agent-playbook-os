from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .integrity import canonical_hash
from .loader import load_playbook

_TOKEN = re.compile(r"[a-zA-Z0-9_-]+")


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: str
    description: str
    tags: list[str] = Field(default_factory=list)
    path: str
    playbook_hash: str


class PlaybookCatalog:
    def __init__(self, entries: Iterable[CatalogEntry] = ()):
        self.entries = list(entries)

    @classmethod
    def from_roots(cls, roots: Iterable[str | Path]):
        entries = []
        seen = set()
        for root in roots:
            root = Path(root).expanduser().resolve()
            for ext in ("*.yaml", "*.yml", "*.json"):
                for path in root.rglob(ext):
                    try:
                        pb = load_playbook(path)
                    except Exception:
                        continue
                    key = (pb.metadata.id, pb.metadata.version, str(path))
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append(CatalogEntry(
                        id=pb.metadata.id,
                        version=pb.metadata.version,
                        description=pb.metadata.description,
                        tags=pb.metadata.tags,
                        path=str(path),
                        playbook_hash=canonical_hash(pb.model_dump(mode="json", by_alias=True)),
                    ))
        return cls(sorted(entries, key=lambda x: (x.id, x.version, x.path)))

    def search(self, query: str, limit: int = 10):
        q = {x.lower() for x in _TOKEN.findall(query)}
        scored = []
        for e in self.entries:
            hay = " ".join([e.id, e.description, *e.tags]).lower()
            tokens = set(_TOKEN.findall(hay))
            overlap = len(q & tokens)
            phrase_bonus = 3 if query.lower() in hay else 0
            tag_bonus = sum(2 for t in e.tags if t.lower() in q)
            id_bonus = 5 if query.lower() == e.id.lower() else 0
            score = overlap + phrase_bonus + tag_bonus + id_bonus
            if score:
                scored.append((score, e))
        return [e for _, e in sorted(scored, key=lambda x: (-x[0], x[1].id, x[1].version))[:limit]]

    def collisions(self) -> dict[str, list[CatalogEntry]]:
        grouped: dict[tuple[str, str], list[CatalogEntry]] = {}
        for entry in self.entries:
            grouped.setdefault((entry.id, entry.version), []).append(entry)
        result = {}
        for (pid, version), items in grouped.items():
            hashes = {x.playbook_hash for x in items}
            if len(items) > 1 and len(hashes) > 1:
                result[f"{pid}@{version}"] = items
        return result
