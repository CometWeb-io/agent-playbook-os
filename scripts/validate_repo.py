from __future__ import annotations

from pathlib import Path
import re
import tomllib

from agent_playbook_os import __version__
from agent_playbook_os.catalog import PlaybookCatalog
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.loader import load_playbook

ROOT = Path(__file__).resolve().parents[1]
errors: list[str] = []

# Version consistency.
version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
project_version = pyproject["project"]["version"]
if not (version == project_version == __version__):
    errors.append(f"version mismatch VERSION={version} pyproject={project_version} package={__version__}")

# Compile every bundled example with deterministic sample inputs.
examples = ROOT / "playbooks" / "examples"
for path in sorted(examples.glob("*.yaml")):
    pb = load_playbook(path)
    samples = {"string": "x", "object": {}, "array": [], "integer": 1, "number": 1.0, "boolean": True}
    inputs = {k: samples[v.type] for k, v in pb.spec.inputs.items() if v.required}
    try:
        Compiler().compile(pb, inputs, source_path=str(path))
        print("OK", path.relative_to(ROOT))
    except Exception as exc:
        errors.append(f"{path.relative_to(ROOT)}: {exc}")
        print("FAIL", path.relative_to(ROOT), exc)

# Same id+version must not mean different content in the bundled catalog.
cat = PlaybookCatalog.from_roots([examples])
for key, items in cat.collisions().items():
    errors.append(f"catalog collision {key}: {[x.path for x in items]}")

# No obvious unfinished markers in shipped kernel/specification files.
marker = re.compile(r"\b(?:TODO|FIXME|XXX)\b")
for base in [ROOT / "src", ROOT / "docs" / "specification"]:
    for path in base.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md"}:
            text = path.read_text(encoding="utf-8")
            if marker.search(text):
                errors.append(f"unfinished marker in {path.relative_to(ROOT)}")

if errors:
    print("\nVALIDATION_ERRORS")
    for error in errors:
        print("-", error)
    raise SystemExit(1)
print("REPO_VALIDATION_OK")
