import asyncio

from agent_playbook_os.attestation import create_attestation, verify_attestation
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore


def make_pb():
    return Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1","kind":"Playbook",
        "metadata":{"id":"attest","version":"0.3.0","description":"attestation"},
        "spec":{"steps":[{"id":"x","type":"action","action":"set","with":{"value":1}}]},
    })


def test_hmac_attestation_detects_wrong_key_and_drift(tmp_path):
    run_dir = tmp_path / "run"
    asyncio.run(Runner(ReferenceRuntime()).run(Compiler().compile(make_pb()), run_dir))
    att = create_attestation(run_dir, b"key-one", signer="test")
    assert verify_attestation(run_dir, att, b"key-one") == (True, None)
    ok, error = verify_attestation(run_dir, att, b"wrong-key")
    assert ok is False
    assert "signature mismatch" in error

    # A new valid event changes the event head and invalidates the old attestation.
    store = RunStore(run_dir)
    state = store.load_state()
    store.append_system_event(state, "audit.note", note="after-attestation")
    ok, error = verify_attestation(run_dir, att, b"key-one")
    assert ok is False
    assert "event_head_hash" in error
