from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
from typing import Iterable

from .errors import ResolutionError
from .models import SkillDescriptor, SkillDependency, SkillLockEntry

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def _package_files(root: Path) -> list[Path]:
    return sorted(file for file in root.rglob("*") if file.is_file()
                  and not any(part in _EXCLUDED_PARTS for part in file.relative_to(root).parts)
                  and file.suffix != ".pyc")


def _verify_local_ref(package: Path, ref: str) -> None:
    """Check local committed bytes only; never fetch, run hooks or apply filters."""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", ref):
        raise ResolutionError("skill ref must be a full lowercase Git commit ID")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")

    def git(*args: str, missing_ok: bool = False) -> bytes:
        try:
            result = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
                 "-c", "core.untrackedCache=false", "-C", str(package), *args],
                env=env, check=False, capture_output=True, timeout=10,
            )
            if result.returncode != 0 and not (missing_ok and result.returncode == 1):
                raise ResolutionError("cannot verify skill ref against local Git source")
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ResolutionError("cannot verify skill ref against local Git source") from exc

    if git("rev-parse", "--verify", "HEAD").decode().strip() != ref:
        raise ResolutionError("skill ref does not match local Git HEAD")
    # Even status may execute clean/process filters. Ref verification must not
    # activate repository-controlled programs, including apparently clean ones.
    if git("config", "--get-regexp", r"^filter\..*\.(clean|process)$", missing_ok=True):
        raise ResolutionError("skill ref verification does not permit configured Git clean/process filters")
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ResolutionError("skill ref requires a clean local Git checkout")
    root = Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip()).resolve()
    relative = package.resolve().relative_to(root).as_posix()
    # Do not rely on the index: assume-unchanged/skip-worktree flags can hide
    # modified bytes from status. Compare every packaged file with the HEAD tree.
    records = git("ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", ":(top,literal)" + relative)
    tracked = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, kind, oid = header.split()
        tracked[os.fsdecode(path)] = (mode, kind, oid)
    files = _package_files(package)
    actual_paths = {file.relative_to(root).as_posix() for file in files}
    expected_paths = {
        path for path in tracked
        if not any(part in _EXCLUDED_PARTS for part in Path(path).relative_to(relative).parts)
        and Path(path).suffix != ".pyc"
    }
    if actual_paths != expected_paths:
        raise ResolutionError("skill ref requires all and only the packaged files in HEAD")
    for file in files:
        identity = tracked.get(file.resolve().relative_to(root).as_posix())
        if identity is None or identity[1] != b"blob" or identity[0] not in {b"100644", b"100755"}:
            raise ResolutionError("skill ref requires every packaged file to be tracked in HEAD")
        if git("hash-object", "--no-filters", "--", str(file)).strip() != identity[2]:
            raise ResolutionError("skill ref package bytes differ from HEAD")
    # Detect a concurrent commit/status change during the comparison as well.
    if git("rev-parse", "--verify", "HEAD").decode().strip() != ref or git(
        "status", "--porcelain=v1", "--untracked-files=all"
    ):
        raise ResolutionError("skill ref source changed during verification")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def sha256_tree(path: Path) -> str:
    """Hash the complete skill package, including references/scripts/assets.

    Relative paths and bytes are both committed into the digest so rename-only drift
    is detected. Transient caches and VCS metadata are intentionally excluded.
    """
    root = path.resolve()
    h = hashlib.sha256()
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise ResolutionError(f"skill packages may not contain symlinks: {candidate}")
    for file in _package_files(root):
        rel = file.relative_to(root)
        rel_bytes = rel.as_posix().encode("utf-8")
        h.update(len(rel_bytes).to_bytes(8, "big"))
        h.update(rel_bytes)
        size = file.stat().st_size
        h.update(size.to_bytes(8, "big"))
        with file.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    return "sha256:" + h.hexdigest()


class SkillResolver:
    """Resolve Agent Skills from local roots and CometWeb registry-compatible repos.

    Duplicate skill IDs across roots are treated as ambiguous instead of silently
    letting root order choose a package.
    """

    def __init__(self, roots: Iterable[str | Path] = ()):
        self.roots = [Path(x).expanduser().resolve() for x in roots]
        self._registry: dict[str, list[SkillDescriptor]] = {}
        self._seen_paths: set[tuple[str, str]] = set()
        for root in self.roots:
            self._index_root(root)

    def _add(self, desc: SkillDescriptor):
        key = (desc.id, desc.path or f"source:{desc.source}")
        if key in self._seen_paths:
            return
        self._seen_paths.add(key)
        self._registry.setdefault(desc.id, []).append(desc)

    def _index_root(self, root: Path):
        registry = root / "registry" / "skills.json"
        if registry.exists():
            data = json.loads(registry.read_text(encoding="utf-8"))
            for item in data.get("skills", []):
                sid = item.get("id")
                if not sid:
                    continue
                skill_dir = root / "skills" / sid
                self._add(SkillDescriptor(
                    id=sid,
                    version=item.get("version"),
                    description=item.get("description"),
                    path=str(skill_dir) if skill_dir.exists() else None,
                    source=str(root),
                    content_hash=sha256_tree(skill_dir) if skill_dir.exists() else None,
                    inputs=item.get("inputs", []),
                    outputs=item.get("outputs", []),
                    compatible_hosts=item.get("compatible_hosts", []),
                ))
        candidates = [root / "skills", root]
        for base in candidates:
            if not base.exists() or not base.is_dir():
                continue
            for child in base.iterdir():
                skill_md = child / "SKILL.md"
                if child.is_dir() and skill_md.exists():
                    self._add(self._descriptor_from_skill_md(child.name, skill_md, root))

    def _descriptor_from_skill_md(self, sid: str, path: Path, root: Path) -> SkillDescriptor:
        text = path.read_text(encoding="utf-8")
        name = sid
        description = None
        m = _FRONTMATTER.match(text)
        if m:
            for line in m.group(1).splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"\'')
                if line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"\'')
        version_path = path.parent / "VERSION"
        version = version_path.read_text().strip() if version_path.exists() else None
        return SkillDescriptor(
            id=name,
            version=version,
            description=description,
            path=str(path.parent),
            source=str(root),
            content_hash=sha256_tree(path.parent),
        )

    def list(self) -> list[SkillDescriptor]:
        values = [item for items in self._registry.values() for item in items]
        return sorted(values, key=lambda x: (x.id, x.source or "", x.path or ""))

    def resolve(self, skill_id: str, dependency: SkillDependency | None = None) -> SkillDescriptor:
        candidates = list(self._registry.get(skill_id, []))
        if dependency and dependency.path:
            dep_path = Path(dependency.path).expanduser()
            candidates = [x for x in candidates if x.path and Path(x.path).name == dep_path.name]
        if len(candidates) == 1:
            if dependency:
                self.verify_dependency(candidates[0], dependency)
            return candidates[0]
        if len(candidates) > 1:
            sources = [x.path or x.source or "unknown" for x in candidates]
            raise ResolutionError(f"ambiguous skill id '{skill_id}' across configured roots: {sources}")
        if dependency and dependency.optional:
            return SkillDescriptor(id=skill_id, source=dependency.source)
        raise ResolutionError(f"skill not found in configured roots: {skill_id}")

    def verify_dependency(self, descriptor: SkillDescriptor, dependency: SkillDependency) -> None:
        if dependency.version is not None:
            if not _SEMVER.fullmatch(dependency.version):
                raise ResolutionError("skill version must be an exact SemVer, not a range")
            version_file = Path(descriptor.path) / "VERSION" if descriptor.path else None
            if version_file is None or not version_file.is_file():
                raise ResolutionError("skill version requires a package VERSION file")
            actual = version_file.read_text(encoding="utf-8").strip()
            if actual != dependency.version or (descriptor.version is not None and descriptor.version != actual):
                raise ResolutionError(f"skill version mismatch: requested={dependency.version} package={actual}")
        if dependency.ref is not None:
            if descriptor.path is None:
                raise ResolutionError("skill ref requires a local package")
            _verify_local_ref(Path(descriptor.path), dependency.ref)

    def lock(self, skill_id: str, dependency: SkillDependency) -> SkillLockEntry:
        # Optional absence is handled by resolve; invalid constraints and
        # ambiguity must not be converted into an apparently usable dependency.
        d = self.resolve(skill_id, dependency)
        return SkillLockEntry(
            id=skill_id,
            source=dependency.source,
            version=d.version or dependency.version,
            ref=dependency.ref,
            path=d.path or dependency.path,
            content_hash=d.content_hash,
            resolved=d.path is not None,
        )

    def verify_lock(self, entry: SkillLockEntry) -> tuple[bool, str | None]:
        if not entry.resolved:
            return False, f"skill lock is unresolved: {entry.id}"
        try:
            current = self.resolve(entry.id)
        except ResolutionError as exc:
            return False, str(exc)
        if not current.path:
            return False, f"skill path unavailable: {entry.id}"
        if not entry.path or Path(entry.path).resolve() != Path(current.path).resolve():
            return False, f"skill lock path differs from verified package path: {entry.id}"
        try:
            actual = sha256_tree(Path(current.path))
            self.verify_dependency(current, SkillDependency(
                source=entry.source, version=entry.version, ref=entry.ref,
            ))
        except (OSError, ResolutionError) as exc:
            return False, str(exc)
        if entry.content_hash != actual:
            return False, f"skill content hash drift: {entry.id}"
        if entry.version and current.version and entry.version != current.version:
            return False, f"skill version drift: {entry.id} locked={entry.version} current={current.version}"
        return True, None
