from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from .distributed import TenantScope
from .models import ArtifactRef
from .tenancy import tenant_path


@runtime_checkable
class ArtifactStoreProtocol(Protocol):
    scheme: str
    def put(
        self,
        scope: TenantScope,
        artifact_id: str,
        data: bytes | str | dict[str, Any] | list[Any],
        *,
        produced_by: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactRef: ...
    def get(self, scope: TenantScope, ref: ArtifactRef) -> bytes: ...
    def verify(self, scope: TenantScope, ref: ArtifactRef) -> tuple[bool, str | None]: ...


class FilesystemArtifactStore:
    scheme = "artifact+file"

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_artifact_id(artifact_id: str) -> str:
        if not artifact_id or artifact_id in {".", ".."} or "/" in artifact_id or "\\" in artifact_id:
            raise ValueError("artifact_id must be a single safe path segment")
        return artifact_id

    @staticmethod
    def _encode(data, media_type):
        if isinstance(data, bytes):
            return data, ".bin", media_type or "application/octet-stream"
        if isinstance(data, str):
            return data.encode("utf-8"), ".txt", media_type or "text/plain"
        return (
            json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8"),
            ".json",
            media_type or "application/json",
        )

    def put(self, scope: TenantScope, artifact_id: str, data, *, produced_by=None, media_type=None) -> ArtifactRef:
        artifact_id = self._safe_artifact_id(artifact_id)
        payload, suffix, media_type = self._encode(data, media_type)
        directory = tenant_path(self.root, scope, "artifacts")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{artifact_id}{suffix}"
        if path.is_symlink():
            raise ValueError("refusing to overwrite symlinked artifact")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        uri = f"{self.scheme}://{scope.tenant_id}/{scope.namespace}/{path.name}"
        return ArtifactRef(
            id=artifact_id,
            uri=uri,
            media_type=media_type,
            content_hash=digest,
            produced_by=produced_by,
        )

    def _path_from_ref(self, scope: TenantScope, ref: ArtifactRef) -> Path:
        parsed = urlparse(ref.uri)
        if parsed.scheme != self.scheme:
            raise ValueError(f"artifact scheme mismatch: {parsed.scheme}")
        if parsed.netloc != scope.tenant_id:
            raise ValueError("artifact tenant mismatch")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 2 or parts[0] != scope.namespace:
            raise ValueError("artifact namespace/path mismatch")
        name = parts[1]
        path = tenant_path(self.root, scope, "artifacts", name)
        if path.is_symlink():
            raise ValueError("artifact path is a symlink")
        return path

    def get(self, scope: TenantScope, ref: ArtifactRef) -> bytes:
        return self._path_from_ref(scope, ref).read_bytes()

    def verify(self, scope: TenantScope, ref: ArtifactRef) -> tuple[bool, str | None]:
        if not ref.content_hash:
            return False, f"artifact {ref.id} missing content_hash"
        try:
            path = self._path_from_ref(scope, ref)
        except Exception as exc:
            return False, f"artifact {ref.id}: {exc}"
        if not path.exists():
            return False, f"artifact {ref.id} missing"
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != ref.content_hash:
            return False, f"artifact {ref.id} hash mismatch"
        return True, None


class ArtifactVerifierRegistry:
    def __init__(self):
        self._stores: dict[str, ArtifactStoreProtocol] = {}

    def register(self, store: ArtifactStoreProtocol):
        if store.scheme in self._stores:
            raise ValueError(f"duplicate artifact scheme: {store.scheme}")
        self._stores[store.scheme] = store

    def verify_refs(
        self,
        refs: list[ArtifactRef],
        *,
        local_store=None,
        tenant_scope: TenantScope | None = None,
        require_all_external: bool = True,
    ) -> tuple[bool, list[str]]:
        errors: list[str] = []
        local_refs = [ref for ref in refs if ref.uri.startswith("file:")]
        if local_refs:
            if local_store is None:
                errors.append("local artifact refs present but no local run store was supplied")
            else:
                ok, local_errors = local_store.verify_artifacts(local_refs)
                if not ok:
                    errors.extend(local_errors)
        for ref in refs:
            if ref.uri.startswith("file:"):
                continue
            scheme = urlparse(ref.uri).scheme
            store = self._stores.get(scheme)
            if store is None:
                if require_all_external:
                    errors.append(f"artifact {ref.id} external scheme is not verified: {scheme or '<none>'}")
                continue
            if tenant_scope is None:
                errors.append(f"artifact {ref.id} requires tenant scope for verification")
                continue
            ok, error = store.verify(tenant_scope, ref)
            if not ok:
                errors.append(error or f"artifact {ref.id} verification failed")
        return not errors, errors
