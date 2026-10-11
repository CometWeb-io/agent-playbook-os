"""Secret-aware cassette signatures bind sensitive *values* without storing them."""
import json

import pytest

from agent_playbook_os.errors import IntegrityError, SecretResolutionError
from agent_playbook_os.integrity import canonical_hash
from agent_playbook_os.models import StepSpec
from agent_playbook_os.recording import RecordingRuntime, ReplayRuntime
from agent_playbook_os.runtime import CallableRuntime
from agent_playbook_os.secret_resolver import SecretValue
from agent_playbook_os.telemetry import redact


KEY = b"S" * 32
STEP = StepSpec(id="send", type="action", action="send")


def secret(value, ref="secret://API"):
    return {"key": SecretValue(value, ref)}


@pytest.mark.asyncio
async def test_unkeyed_secret_recording_is_rejected_before_dispatch(tmp_path):
    calls = []

    async def send(step, inputs, context):
        calls.append(inputs)
        return {"ok": True}

    cassette = tmp_path / "unsafe.jsonl"
    recorder = RecordingRuntime(CallableRuntime(actions={"send": send}), cassette)
    with pytest.raises(SecretResolutionError, match="requires integrity_key"):
        await recorder.execute_action(STEP, secret("alpha"), {})
    assert calls == []
    assert not cassette.exists()


@pytest.mark.asyncio
async def test_hmac_cassette_binds_secret_value_without_persisting_secret(tmp_path):
    calls = []

    async def send(step, inputs, context):
        calls.append(str(inputs["key"]))
        return {"ok": True}

    cassette = tmp_path / "secure.jsonl"
    recorder = RecordingRuntime(
        CallableRuntime(actions={"send": send}), cassette, integrity_key=KEY,
    )
    result = await recorder.execute_action(STEP, secret("alpha-secret-123"), {})
    assert result == {"ok": True}
    assert calls == ["alpha-secret-123"]
    raw = cassette.read_text()
    assert "alpha-secret-123" not in raw
    assert "secret://API" not in raw
    parsed = json.loads(raw)
    assert parsed["signature_scheme"] == "hmac-sha256:inputs-v1"
    assert parsed["signature"].startswith("hmac-sha256:")
    assert parsed["record_mac"].startswith("hmac-sha256:")

    replay = ReplayRuntime(cassette, integrity_key=KEY)
    with pytest.raises(RuntimeError, match="replay divergence"):
        await replay.execute_action(STEP, secret("different-secret-123"), {})
    assert replay.index == 0
    assert await replay.execute_action(STEP, secret("alpha-secret-123"), {}) == {"ok": True}
    assert replay.index == 1
    assert calls == ["alpha-secret-123"], "replay must not call the provider"


@pytest.mark.asyncio
async def test_same_secret_value_with_different_source_ref_is_not_equivalent(tmp_path):
    cassette = tmp_path / "secure.jsonl"
    recorder = RecordingRuntime(
        CallableRuntime(actions={"send": lambda s, i, c: {"ok": True}}),
        cassette, integrity_key=KEY,
    )
    await recorder.execute_action(STEP, secret("same-bytes", "secret://PRIMARY"), {})
    replay = ReplayRuntime(cassette, integrity_key=KEY)
    with pytest.raises(RuntimeError, match="replay divergence"):
        await replay.execute_action(STEP, secret("same-bytes", "secret://BACKUP"), {})
    assert replay.index == 0


@pytest.mark.asyncio
async def test_replay_legacy_redacted_signature_does_not_match_secret_input(tmp_path):
    cassette = tmp_path / "old.jsonl"
    inputs = secret("old-value")
    old_signature = canonical_hash({
        "kind": "action", "step_id": STEP.id, "inputs": redact(inputs),
    })
    writer = RecordingRuntime(
        CallableRuntime(actions={"send": lambda s, i, c: 1}),
        cassette, integrity_key=KEY,
    )
    writer._write({
        "kind": "action", "step_id": STEP.id,
        "signature": old_signature, "ok": True, "output": 1,
        "inputs": redact(inputs),
    })
    reader = ReplayRuntime(cassette, integrity_key=KEY)
    with pytest.raises(SecretResolutionError, match="requires integrity_key"):
        # Old signature scheme is ambiguous even if the enclosing record
        # has an authenticated HMAC: it never included the original secret.
        await reader.execute_action(STEP, inputs, {})
    assert reader.index == 0


@pytest.mark.asyncio
async def test_legacy_keyed_nonsecret_signature_remains_replayable(tmp_path):
    cassette = tmp_path / "old.jsonl"
    inputs = {"value": "ordinary"}
    signature = canonical_hash({
        "kind": "action", "step_id": STEP.id, "inputs": redact(inputs),
    })
    RecordingRuntime(
        CallableRuntime(actions={"send": lambda s, i, c: 1}), cassette, integrity_key=KEY,
    )._write({
        "kind": "action", "step_id": STEP.id, "signature": signature,
        "ok": True, "output": 7, "inputs": inputs,
    })
    replay = ReplayRuntime(cassette, integrity_key=KEY)
    assert await replay.execute_action(STEP, inputs, {}) == 7


@pytest.mark.asyncio
async def test_keyed_mode_rejects_unsupported_nondeterministic_input_before_dispatch(tmp_path):
    calls = []

    async def handler(step, inputs, context):
        calls.append(True)
        return "executed"

    recorder = RecordingRuntime(
        CallableRuntime(actions={"send": handler}),
        tmp_path / "unsafe.jsonl", integrity_key=KEY,
    )
    with pytest.raises(SecretResolutionError, match="unsupported input"):
        await recorder.execute_action(STEP, {"obj": object()}, {})
    assert calls == []


def test_hmac_input_scheme_is_independent_of_cassette_record_mac(tmp_path):
    from agent_playbook_os.recording import _input_signature
    payload = secret("canary")
    signature = _input_signature(
        "action", "send", payload, KEY, scheme="hmac-sha256:inputs-v1"
    )
    assert signature.startswith("hmac-sha256:")
    assert signature != _input_signature(
        "action", "send", secret("canary-2"), KEY, scheme="hmac-sha256:inputs-v1"
    )
