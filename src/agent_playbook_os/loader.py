from pathlib import Path
from typing import Any
import json
import yaml

from .models import Playbook

MAX_PLAYBOOK_BYTES = 2_000_000


def load_data(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_PLAYBOOK_BYTES:
        raise ValueError(f"playbook file too large: {size} > {MAX_PLAYBOOK_BYTES} bytes")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        value = json.loads(text)
    elif path.suffix.lower() in {".yaml", ".yml"}:
        value = yaml.safe_load(text)
    else:
        raise ValueError(f"unsupported playbook format: {path.suffix}")
    if not isinstance(value, dict):
        raise ValueError("playbook root must be a mapping")
    return value


def load_playbook(path: str | Path) -> Playbook:
    return Playbook.model_validate(load_data(path))
