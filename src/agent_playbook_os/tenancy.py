from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from .models import PolicySpec
from .policy import compose_policies
from .secret_resolver import SecretResolutionError
from .distributed import TenantScope


def tenant_path(root: str | Path, scope: TenantScope, *parts: str) -> Path:
    base = Path(root).resolve()
    candidate = base / scope.tenant_id / scope.namespace
    for part in parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part:
            raise ValueError(f"unsafe tenant path segment: {part!r}")
        candidate = candidate / part
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise ValueError("tenant path escapes configured root")
    return resolved


def _load_policy_file(path: Path) -> PolicySpec | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError(f"refusing symlinked policy file: {path}")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if value is None:
        return PolicySpec()
    if not isinstance(value, dict):
        raise ValueError(f"policy must be a mapping: {path}")
    return PolicySpec.model_validate(value)


class TenantPolicyResolver:
    """Resolve root -> tenant -> namespace policy as a monotonic restriction chain."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, scope: TenantScope, base_policy: PolicySpec | None = None) -> PolicySpec | None:
        effective = base_policy.model_copy(deep=True) if base_policy is not None else None
        candidates = [
            self.root / "policy.yaml",
            self.root / scope.tenant_id / "policy.yaml",
            self.root / scope.tenant_id / scope.namespace / "policy.yaml",
        ]
        for path in candidates:
            if path.is_symlink():
                raise ValueError(f"refusing symlinked policy file: {path}")
            resolved = path.resolve(strict=False)
            if not resolved.is_relative_to(self.root):
                raise ValueError("policy path escapes tenant policy root")
            layer = _load_policy_file(path)
            if layer is None:
                continue
            effective = layer if effective is None else compose_policies(effective, layer)
        return effective


class TenantFileSecretResolver:
    """Resolve tenant-secret://NAME inside one tenant/namespace secret root.

    Secret names are intentionally single path segments. The resolver refuses symlinks
    so a tenant cannot redirect a secret lookup outside its assigned directory.
    """

    scheme = "tenant-secret://"

    def __init__(self, root: str | Path, scope: TenantScope):
        self.root = Path(root).resolve()
        self.scope = scope

    def can_resolve(self, ref: str) -> bool:
        return isinstance(ref, str) and ref.startswith(self.scheme)

    def resolve(self, ref: str) -> str:
        if not self.can_resolve(ref):
            raise SecretResolutionError(f"unsupported tenant secret reference: {ref}")
        name = ref[len(self.scheme):]
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise SecretResolutionError("tenant secret name must be one safe path segment")
        raw = self.root / self.scope.tenant_id / self.scope.namespace / name
        if raw.is_symlink():
            raise SecretResolutionError("tenant secret file must not be a symlink")
        path = tenant_path(self.root, self.scope, name)
        if not path.exists() or not path.is_file():
            raise SecretResolutionError(f"tenant secret not found: {name}")
        return path.read_text(encoding="utf-8").rstrip("\r\n")
