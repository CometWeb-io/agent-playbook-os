"""Operator-side reconciliation is fenced and SQLite checkpoints are atomic."""
import asyncio
import json
import sqlite3

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import LeaseConflict, ReconciliationRequired
from agent_playbook_os.models import (
    InvocationStatus, Playbook, ProviderReceipt, RunStatus,
)
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.secret_resolver import SecretValue
from agent_playbook_os.storage import SQLiteRunStore
from agent_playbook_os.store import RunStore


def make_plan():
    return Compiler().compile(Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "manual-mutation", "version": "0.1.0",
                     "description": "operator concurrency and recovery"},
        "spec": {"steps": [{"id": "step", "type": "action", "action": "set",
                            "with": {"value": 123}}]},
    }))


async def completed(tmp_path, backend):
    root = tmp_path / backend.backend_id
    state = await Runner(ReferenceRuntime(), store_factory=backend).run(make_plan(), root)
    assert state.status == RunStatus.COMPLETED
    store = backend(root)
    return store, state.invocations[0].invocation_id


def simulate_unknown(store, invocation_id):
    state = store.load_state()
    for inv in state.invocations:
        if inv.invocation_id == invocation_id:
            inv.status = InvocationStatus.UNKNOWN
            inv.finished_at = None
            inv.result_hash = None
    store.save_state(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_operator_mutations_respect_active_runner_lease(tmp_path, backend):
    store, inv = await completed(tmp_path, backend)
    token = store.acquire_lease("active-runner", ttl_seconds=30)
    before = store.load_state().model_dump(mode="json")
    events_before = store.read_events()
    try:
        with pytest.raises(LeaseConflict):
            backend(store.run_dir).attach_provider_receipt(
                inv, ProviderReceipt(provider="test", call_id="native-call")
            )
        with pytest.raises(LeaseConflict):
            backend(store.run_dir).reconcile_invocation(
                inv, outcome="not-executed"
            )
    finally:
        store.release_lease(token)
    assert backend(store.run_dir).load_state().model_dump(mode="json") == before
    assert backend(store.run_dir).read_events() == events_before


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_operator_rejects_tampered_event_chain_before_mutating_state(tmp_path, backend):
    store, inv = await completed(tmp_path, backend)
    original_state = store.load_state().model_dump(mode="json")
    if backend is RunStore:
        events = store.read_events()
        events[0]["type"] = "altered-invocation"
        store.events_path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
    else:
        with sqlite3.connect(store.db_path) as db:
            row = json.loads(db.execute("SELECT payload FROM events ORDER BY seq LIMIT 1").fetchone()[0])
            row["type"] = "altered-invocation"
            db.execute("UPDATE events SET payload=? WHERE seq=1", (json.dumps(row),))
    with pytest.raises(ReconciliationRequired, match="invalid events evidence"):
        backend(store.run_dir).attach_provider_receipt(
            inv, ProviderReceipt(provider="test", call_id="native-call")
        )
    assert backend(store.run_dir).load_state().model_dump(mode="json") == original_state


@pytest.mark.asyncio
async def test_sqlite_receipt_attached_atomically_with_event(tmp_path):
    store, inv = await completed(tmp_path, SQLiteRunStore)
    before_state = store.load_state().model_dump(mode="json")
    before_events = store.read_events()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "CREATE TRIGGER reject_operator BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'forced receipt audit failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="forced receipt audit failure"):
        SQLiteRunStore(store.run_dir).attach_provider_receipt(
            inv, ProviderReceipt(provider="test", call_id="call-001")
        )
    assert store.load_state().model_dump(mode="json") == before_state
    assert store.read_events() == before_events
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER reject_operator")
    updated = SQLiteRunStore(store.run_dir).attach_provider_receipt(
        inv, ProviderReceipt(provider="test", call_id="call-001")
    )
    assert updated.invocations[0].provider_call_id == "call-001"
    assert store.read_events()[-1]["type"] == "provider.receipt_attached"
    assert store.verify_event_chain() == (True, None)
    assert store.verify_state_integrity() == (True, None)


@pytest.mark.asyncio
async def test_sqlite_reconciliation_is_atomic_on_audit_insert_failure(tmp_path):
    store, inv = await completed(tmp_path, SQLiteRunStore)
    simulate_unknown(store, inv)
    before_state = store.load_state().model_dump(mode="json")
    before_events = store.read_events()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "CREATE TRIGGER reject_operator BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'forced reconcile audit failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="forced reconcile audit failure"):
        SQLiteRunStore(store.run_dir).reconcile_invocation(
            inv, outcome="succeeded", output={"ok": True},
        )
    assert store.load_state().model_dump(mode="json") == before_state
    assert store.read_events() == before_events
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER reject_operator")
    state = SQLiteRunStore(store.run_dir).reconcile_invocation(
        inv, outcome="succeeded",
        output={"ok": True, "api_key": SecretValue("canary", "secret://API")},
    )
    assert state.invocations[0].status == InvocationStatus.RECONCILED
    assert "canary" not in json.dumps(state.model_dump(mode="json"))
    assert state.steps["step"].output["api_key"] == "<redacted>"
    assert store.verify_state_integrity() == (True, None)
    assert store.verify_event_chain() == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_operator_manual_update_keeps_run_journal_sequence(tmp_path, backend):
    store, inv = await completed(tmp_path, backend)
    start_events = len(store.read_events())
    store.attach_provider_receipt(inv, ProviderReceipt(provider="host", call_id="receipt-1"))
    assert len(store.read_events()) == start_events + 1
    assert [event["seq"] for event in store.read_events()] == list(
        range(1, len(store.read_events()) + 1)
    )
    assert store.verify_event_chain() == (True, None)
    assert store.verify_state_integrity() == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_receipt_id_collision_is_denied_without_state_or_event_changes(tmp_path, backend):
    store, invocation = await completed(tmp_path, backend)
    from uuid import UUID
    first = ProviderReceipt(
        receipt_id="fixed-receipt",
        provider="provider-a", call_id="native-1", status="completed",
    )
    store.attach_provider_receipt(invocation, first)
    before_state = store.load_state().model_dump(mode="json")
    before_events = store.read_events()
    with pytest.raises(ReconciliationRequired, match="receipt ID collision"):
        backend(store.run_dir).attach_provider_receipt(
            invocation, first.model_copy(update={"call_id": "forged-call"}),
        )
    assert backend(store.run_dir).load_state().model_dump(mode="json") == before_state
    assert backend(store.run_dir).read_events() == before_events


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_reconciliation_note_is_redacted_before_persistence(tmp_path, backend):
    store, inv = await completed(tmp_path, backend)
    simulate_unknown(store, inv)
    new_state = backend(store.run_dir).reconcile_invocation(
        inv, outcome="not-executed", note="Bearer sensitive-example-token1234",
    )
    assert "sensitive-example-token1234" not in new_state.invocations[0].reconciliation_note
    assert "sensitive-example-token1234" not in new_state.model_dump_json()
    assert backend(store.run_dir).verify_state_integrity() == (True, None)


@pytest.mark.asyncio
async def test_cli_receipt_attach_redacts_returned_metadata(tmp_path, capsys):
    from types import SimpleNamespace
    from agent_playbook_os.cli import cmd_receipt_attach
    store, invocation = await completed(tmp_path, RunStore)
    args = SimpleNamespace(
        run_dir=str(store.run_dir), invocation_id=invocation,
        provider="manual", call_id="native-verified", operation="test",
        status="completed", idempotency_key=None,
        metadata=json.dumps({"api_key": "private-api-key-123", "result": "ok"}),
        actor="ops",
    )
    cmd_receipt_attach(args)
    printed = capsys.readouterr().out
    assert "private-api-key-123" not in printed
    assert '"api_key": "<redacted>"' in printed
    assert store.verify_state_integrity() == (True, None)
