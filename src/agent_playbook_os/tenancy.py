from __future__ import annotations

from pathlib import Path
from typing import Iterable
import os
import stat

import yaml

from .models import PolicySpec
from .policy import compose_policies
from .secret_resolver import SecretResolutionError, SecretValue
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

    def __init__(self, root: str | Path, scope: TenantScope, *, max_bytes: int = 65536):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.root = Path(root).resolve()
        self.scope = scope
        self.max_bytes = max_bytes

    def supports(self, ref: str) -> bool:
        return isinstance(ref, str) and ref.startswith(self.scheme)

    def can_resolve(self, ref: str) -> bool:
        # Compatibility with hosts using the older tenant-only API.
        return self.supports(ref)

    def resolve(self, ref: str) -> SecretValue:
        if not self.supports(ref):
            raise SecretResolutionError(f"unsupported tenant secret reference: {ref}")
        name = ref[len(self.scheme):]
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise SecretResolutionError("tenant secret name must be one safe path segment")
        # Resolve path components using openat-style dir_fd operations. Checking
        # Path.is_symlink() and later calling read_text() leaves a TOCTOU gap:
        # a tenant could replace a file or ancestor directory after validation.
        if not all(hasattr(os, x) for x in ("O_NOFOLLOW", "O_DIRECTORY")) or os.name != "posix":
            raise SecretResolutionError("secure tenant secrets require POSIX no-follow directory handles")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        fd = None
        try:
            fd = os.open(self.root, directory_flags)
            for component in (self.scope.tenant_id, self.scope.namespace):
                next_fd = os.open(component, directory_flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            secret_fd = os.open(name, file_flags, dir_fd=fd)
            try:
                info = os.fstat(secret_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise SecretResolutionError("tenant secret must be a regular file")
                if info.st_size > self.max_bytes:
                    raise SecretResolutionError("tenant secret exceeds configured maximum size")
                data = os.read(secret_fd, self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise SecretResolutionError("tenant secret exceeds configured maximum size")
                try:
                    return SecretValue(data.decode("utf-8").rstrip("\r\n"), ref)
                except UnicodeDecodeError as exc:
                    raise SecretResolutionError("tenant secret must be UTF-8") from exc
            finally:
                os.close(secret_fd)
        except OSError as exc:
            raise SecretResolutionError(
                f"secure tenant secret lookup failed (symlink or inaccessible path) for {name!r}"
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)
