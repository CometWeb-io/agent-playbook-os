"""Cassette safety: bounded memory, record-chain verification and strict replay."""
import json
import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import IntegrityError, SideEffectOutcomeUnknown
from agent_playbook_os.integrity import canonical_hash
from agent_playbook_os.models import Playbook, RunStatus, RuntimeStreamEvent, InvocationStatus
from agent_playbook_os.recording import RecordingRuntime, ReplayRuntime
from agent_playbook_os.runtime import CallableRuntime, ReferenceRuntime


def plan(action="set", effect="none"):
    return Compiler().compile(Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "cassette-safety", "version": "0.1.0", "description": "record/replay"},
        "spec": {"policy": {"require_approval_for": []}, "steps": [{
            "id": "step", "type": "action", "action": action,
            "side_effects": effect, "with": {"value": 7}
        }]},
    }))


@pytest.mark.asyncio
async def test_recording_chain_roundtrip_and_tamper_detection(tmp_path):
    cassette = tmp_path / "cassette.jsonl"
    compiled = plan()
    recorded = await Runner(RecordingRuntime(ReferenceRuntime(), cassette)).run(
        compiled, tmp_path / "original"
    )
    assert recorded.status == RunStatus.COMPLETED
    rows = [json.loads(line) for line in cassette.read_text().splitlines()]
    assert rows[0]["prev_hash"] is None
    assert rows[0]["record_hash"].startswith("sha256:")
    assert ReplayRuntime(cassette).records[0]["record_hash"] == rows[0]["record_hash"]
    replay = await Runner(ReplayRuntime(cassette)).run(compiled, tmp_path / "replay")
    assert replay.status == RunStatus.COMPLETED

    rows[0]["output"] = "changed"
    cassette.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    with pytest.raises(IntegrityError, match="record hash mismatch"):
        ReplayRuntime(cassette)


def test_chain_detects_deleted_middle_record(tmp_path):
    cassette = tmp_path / "chain.jsonl"
    recorder = RecordingRuntime(ReferenceRuntime(), cassette)
    recorder._write({"kind": "action", "step_id": "a", "signature": "one", "ok": True, "output": 1})
    recorder._write({"kind": "action", "step_id": "b", "signature": "two", "ok": True, "output": 2})
    recorder._write({"kind": "action", "step_id": "c", "signature": "three", "ok": True, "output": 3})
    original = [json.loads(line) for line in cassette.read_text().splitlines()]
    assert original[1]["prev_hash"] == original[0]["record_hash"]
    assert original[2]["prev_hash"] == original[1]["record_hash"]
    cassette.write_text(json.dumps(original[0]) + "\n" + json.dumps(original[2]) + "\n")
    with pytest.raises(IntegrityError, match="predecessor mismatch"):
        ReplayRuntime(cassette)


def test_legacy_records_require_explicit_opt_in(tmp_path):
    cassette = tmp_path / "legacy.jsonl"
    cassette.write_text(json.dumps({"kind": "action", "step_id": "a",
                                    "signature": "one", "ok": True, "output": 1}) + "\n")
    with pytest.raises(IntegrityError, match="allow_legacy"):
        ReplayRuntime(cassette)
    assert len(ReplayRuntime(cassette, allow_legacy=True).records) == 1
    recorder = RecordingRuntime(ReferenceRuntime(), cassette)
    with pytest.raises(SideEffectOutcomeUnknown, match="could not be committed"):
        recorder._write({"kind": "action", "step_id": "b", "signature": "two",
                         "ok": True, "output": 2})


def test_mixed_hashed_and_legacy_entries_are_rejected(tmp_path):
    cassette = tmp_path / "mixed.jsonl"
    recorder = RecordingRuntime(ReferenceRuntime(), cassette)
    recorder._write({"kind": "action", "step_id": "a", "signature": "one",
                     "ok": True, "output": 1})
    with cassette.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "action", "step_id": "b", "signature": "two",
                            "ok": True, "output": 2}) + "\n")
    with pytest.raises(IntegrityError, match="mix hashed and legacy"):
        ReplayRuntime(cassette, allow_legacy=True)


def test_cassette_replay_limits_are_enforced(tmp_path):
    cassette = tmp_path / "large.jsonl"
    recorder = RecordingRuntime(ReferenceRuntime(), cassette)
    for i in range(3):
        recorder._write({"kind": "action", "step_id": f"x{i}",
                         "signature": f"s{i}", "ok": True, "output": i})
    with pytest.raises(IntegrityError, match="record count"):
        ReplayRuntime(cassette, max_records=2)
    with pytest.raises(IntegrityError, match="maximum size"):
        ReplayRuntime(cassette, max_cassette_bytes=100, max_record_bytes=100)
    with pytest.raises(IntegrityError, match="record exceeds"):
        ReplayRuntime(cassette, max_record_bytes=70)


@pytest.mark.asyncio
async def test_cassette_write_exhaustion_preserves_material_uncertainty(tmp_path):
    cassette = tmp_path / "small.jsonl"
    calls = []

    async def action(step, inputs, context):
        calls.append("effect")
        return {"payload": "x" * 1000}

    runtime = RecordingRuntime(
        CallableRuntime(actions={"write": action}),
        cassette, max_record_bytes=256, max_cassette_bytes=500,
    )
    state = await Runner(runtime).run(plan("write", effect="external"), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert calls == ["effect"]


@pytest.mark.asyncio
async def test_stream_recording_limit_preserves_material_uncertainty(tmp_path):
    cassette = tmp_path / "stream.jsonl"

    async def action(step, inputs, context):
        async def generator():
            for i in range(5):
                yield RuntimeStreamEvent(type="progress", value=i)
            yield RuntimeStreamEvent(type="result", value=7)
        return generator()

    runtime = RecordingRuntime(
        CallableRuntime(actions={"stream": action}),
        cassette, max_stream_events=2,
    )
    state = await Runner(runtime).run(plan("stream", effect="external"), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert "cassette" in state.steps["step"].error.lower()


@pytest.mark.asyncio
async def test_replay_divergence_does_not_advance_cursor(tmp_path):
    cassette = tmp_path / "cassette.jsonl"
    await Runner(RecordingRuntime(ReferenceRuntime(), cassette)).run(plan(), tmp_path / "run")
    replay = ReplayRuntime(cassette)
    from agent_playbook_os.models import StepSpec
    bad = StepSpec(id="wrong", type="action", action="set")
    good = StepSpec(id="step", type="action", action="set")
    with pytest.raises(RuntimeError, match="replay divergence"):
        await replay.execute_action(bad, {"value": 7}, {})
    assert replay.index == 0
    result = await replay.execute_action(good, {"value": 7}, {})
    assert result == 7


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_invalid_recording_limits(invalid, tmp_path):
    with pytest.raises(ValueError):
        RecordingRuntime(ReferenceRuntime(), tmp_path / "a", max_record_bytes=invalid)
    with pytest.raises(ValueError):
        ReplayRuntime(tmp_path / "a", max_records=invalid)


def test_authenticated_cassette_requires_matching_hmac_key(tmp_path):
    key = b"secret-signing-key-" + b"K" * 32
    cassette = tmp_path / "authenticated.jsonl"
    recorder = RecordingRuntime(ReferenceRuntime(), cassette, integrity_key=key)
    recorder._write({"kind": "action", "step_id": "a", "signature": "sig", "ok": True, "output": "yes"})
    record = json.loads(cassette.read_text())
    assert record["record_mac"].startswith("hmac-sha256:")
    assert len(ReplayRuntime(cassette, integrity_key=key).records) == 1
    with pytest.raises(IntegrityError, match="requires an integrity_key"):
        ReplayRuntime(cassette)
    with pytest.raises(IntegrityError, match="authentication mismatch"):
        ReplayRuntime(cassette, integrity_key=b"W" * 32)
    with pytest.raises(SideEffectOutcomeUnknown):
        RecordingRuntime(ReferenceRuntime(), cassette, integrity_key=b"W" * 32)._write(
            {"kind": "action", "step_id": "b", "signature": "sig2", "ok": True, "output": 1}
        )


def test_hmac_detects_recomputed_plaintext_hash(tmp_path):
    key = b"S" * 32
    cassette = tmp_path / "authenticated.jsonl"
    writer = RecordingRuntime(ReferenceRuntime(), cassette, integrity_key=key)
    writer._write({"kind": "action", "step_id": "a", "signature": "sig", "ok": True, "output": 1})
    row = json.loads(cassette.read_text())
    row["output"] = 99
    row["record_hash"] = canonical_hash({
        k: v for k, v in row.items() if k not in {"record_hash", "record_mac"}
    })
    cassette.write_text(json.dumps(row) + "\n")
    with pytest.raises(IntegrityError, match="authentication mismatch"):
        ReplayRuntime(cassette, integrity_key=key)


def test_authenticated_reader_never_downgrades_to_legacy(tmp_path):
    cassette = tmp_path / "legacy.jsonl"
    cassette.write_text(json.dumps({"kind": "action", "step_id": "x", "ok": True}) + "\n")
    with pytest.raises(IntegrityError, match="authenticated replay"):
        ReplayRuntime(cassette, allow_legacy=True, integrity_key=b"Z" * 32)


@pytest.mark.parametrize("bad", [b"short", b"", "not bytes", True])
def test_invalid_authentication_keys_are_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="integrity_key"):
        RecordingRuntime(ReferenceRuntime(), tmp_path / "new.jsonl", integrity_key=bad)
    with pytest.raises(ValueError, match="integrity_key"):
        ReplayRuntime(tmp_path / "missing.jsonl", integrity_key=bad)
