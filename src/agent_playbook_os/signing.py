from __future__ import annotations

import hashlib
import hmac
from typing import Callable, Protocol


class AttestationSigner(Protocol):
    signer_id: str
    def sign(self, payload: bytes) -> str: ...
    def verify(self, payload: bytes, signature: str) -> bool: ...


class HMACSigner:
    signer_id = "local-hmac"

    def __init__(self, key: bytes):
        if not key:
            raise ValueError("HMAC key must not be empty")
        self.key = key

    def sign(self, payload: bytes) -> str:
        return "hmac-sha256:" + hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)


class CallableSigner:
    """Bridge for KMS/HSM/Vault sign+verify callables owned by the embedding host."""

    def __init__(
        self,
        signer_id: str,
        sign: Callable[[bytes], str],
        verify: Callable[[bytes, str], bool],
    ):
        if not signer_id:
            raise ValueError("signer_id must not be empty")
        self.signer_id = signer_id
        self._sign = sign
        self._verify = verify

    def sign(self, payload: bytes) -> str:
        return str(self._sign(payload))

    def verify(self, payload: bytes, signature: str) -> bool:
        return bool(self._verify(payload, signature))
