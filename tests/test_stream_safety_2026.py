"""Adversarial streaming and real provider configuration drift (no network needed)."""
from types import SimpleNamespace

import pytest

from agent_playbook_os.adapters.anthropic import AnthropicMessagesRuntime
from agent_playbook_os.adapters.openai import OpenAIResponsesRuntime
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import (
    InvocationStatus, Playbook, RunStatus, RuntimeStreamEvent, UsageMetrics,
)
from agent_playbook_os.runtime import CallableRuntime
from agent_playbook_os.store import RunStore
from agent_playbook_os.errors import StepExecutionError


def plan(*, side_effects="none", retry=1):
    return Compiler().compile(Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "stream-limits", "version": "0.1.0", "description": "stream limits"},
        "spec": {
            "policy": {"require_approval_for": []},
            "steps": [{"id": "stream", "type": "action", "action": "stream",
                       "side_effects": side_effects,
                       "on_fail": "skip",
                       "retry": {"max_attempts": retry}}],
        },
    }))


def streaming_runtime(events, calls):
    async def handler(step, inputs, context):
        calls.append("dispatched")

        async def stream():
            for event in events:
                calls.append(event.type)
                yield event
        return stream()

    return CallableRuntime(actions={"stream": handler})


@pytest.mark.asyncio
async def test_one_terminal_result_is_successful(tmp_path):
    events = [
        RuntimeStreamEvent(type="progress", value="running"),
        RuntimeStreamEvent(type="usage", usage=UsageMetrics(input_tokens=3)),
        RuntimeStreamEvent(type="result", value={"ok": True}),
    ]
    calls = []
    state = await Runner(
        streaming_runtime(events, calls),
        max_stream_events=3, max_stream_event_bytes=4096, max_stream_total_bytes=12288,
    ).run(plan(), tmp_path / "valid")
    assert state.status == RunStatus.COMPLETED
    assert state.usage.input_tokens == 3
    assert state.steps["stream"].output == {"ok": True}


@pytest.mark.asyncio
async def test_event_count_overflow_is_unknown_and_not_skippable(tmp_path):
    events = [RuntimeStreamEvent(type="progress", value="chunk") for _ in range(3)] + [
        RuntimeStreamEvent(type="result", value="not reached")
    ]
    calls = []
    state = await Runner(
        streaming_runtime(events, calls),
        max_stream_events=2, max_stream_event_bytes=4096, max_stream_total_bytes=12288,
    ).run(plan(side_effects="external"), tmp_path / "overflow")
    assert state.status == RunStatus.FAILED
    assert state.invocations[-1].status == InvocationStatus.UNKNOWN
    assert state.steps["stream"].attempts == 1
    assert "max_stream_events" in state.steps["stream"].error
    assert calls.count("result") == 0


@pytest.mark.asyncio
async def test_large_stream_event_refused_before_persistence(tmp_path):
    calls = []
    result = await Runner(
        streaming_runtime([RuntimeStreamEvent(type="result", value="SENSITIVE_" * 500)], calls),
        max_stream_events=3, max_stream_event_bytes=256, max_stream_total_bytes=1024,
    ).run(plan(side_effects="external"), tmp_path / "large")
    assert result.status == RunStatus.FAILED
    assert result.invocations[0].status == InvocationStatus.UNKNOWN
    assert "max_stream_event_bytes" in result.steps["stream"].error
    assert "SENSITIVE_" not in (tmp_path / "large" / "state.json").read_text()


@pytest.mark.asyncio
async def test_total_stream_limit_stops_repeated_progress(tmp_path):
    calls = []
    events = [RuntimeStreamEvent(type="progress", value="a" * 130) for _ in range(8)]
    events.append(RuntimeStreamEvent(type="result", value="done"))
    state = await Runner(
        streaming_runtime(events, calls),
        max_stream_events=20, max_stream_event_bytes=512, max_stream_total_bytes=600,
    ).run(plan(side_effects="local"), tmp_path / "total")
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert "max_stream_total_bytes" in state.steps["stream"].error
    assert len(calls) < len(events) + 1


@pytest.mark.asyncio
async def test_second_result_is_protocol_error(tmp_path):
    calls = []
    events = [
        RuntimeStreamEvent(type="result", value="first"),
        RuntimeStreamEvent(type="result", value="second"),
    ]
    state = await Runner(streaming_runtime(events, calls)).run(plan(), tmp_path / "second")
    assert state.status == RunStatus.FAILED
    assert "after terminal result" in state.steps["stream"].error


@pytest.mark.asyncio
async def test_missing_terminal_result_blocks_success(tmp_path):
    calls = []
    events = [RuntimeStreamEvent(type="usage", usage=UsageMetrics(model_calls=1))]
    state = await Runner(streaming_runtime(events, calls)).run(
        plan(side_effects="external"), tmp_path / "unfinished",
    )
    assert state.status == RunStatus.FAILED
    assert state.usage.model_calls == 1
    assert state.invocations[0].status == InvocationStatus.UNKNOWN


@pytest.mark.parametrize("kwargs", [
    {"max_stream_events": 0},
    {"max_stream_events": True},
    {"max_stream_event_bytes": 0},
    {"max_stream_total_bytes": 5, "max_stream_event_bytes": 6},
    {"max_stream_total_bytes": -1},
])
def test_invalid_stream_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Runner(CallableRuntime(), **kwargs)


def fake_openai(model_response="ok"):
    return SimpleNamespace(responses=SimpleNamespace(
        create=lambda **kwargs: {
            "status": "completed", "id": "response-1", "output_text": model_response, "output": []
        }
    ))


@pytest.mark.asyncio
async def test_openai_model_change_during_resume_requires_explicit_review(tmp_path):
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "resume-drift", "version": "0.1.0", "description": "model drift"},
        "spec": {"steps": [
            {"id": "gate", "type": "gate", "gate": "human"},
            {"id": "agent", "type": "agent", "needs": ["gate"]},
        ]},
    })
    compiled = Compiler().compile(pb)
    root = tmp_path / "run"
    original = await Runner(OpenAIResponsesRuntime(fake_openai(), model="model-a")).run(compiled, root)
    assert original.status == RunStatus.WAITING_APPROVAL
    with pytest.raises(StepExecutionError, match="runtime capabilities changed"):
        await Runner(OpenAIResponsesRuntime(fake_openai(), model="model-b")).run(
            compiled, root, resume=True, approvals={"gate"},
        )
    assert RunStore(root).load_state().status == RunStatus.WAITING_APPROVAL
    done = await Runner(OpenAIResponsesRuntime(fake_openai(), model="model-b")).run(
        compiled, root, resume=True, approvals={"gate"}, allow_runtime_change=True,
    )
    assert done.status == RunStatus.COMPLETED


def test_anthropic_max_tokens_and_model_are_fingerprinted():
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: {}))
    a = AnthropicMessagesRuntime(client, model="a", max_tokens=128)
    b = AnthropicMessagesRuntime(client, model="a", max_tokens=256)
    c = AnthropicMessagesRuntime(client, model="b", max_tokens=128)
    from agent_playbook_os.integrity import canonical_hash
    def fp(runtime):
        return canonical_hash([x.model_dump(mode="json") for x in runtime.capabilities().list()])
    assert len({fp(a), fp(b), fp(c)}) == 3


@pytest.mark.parametrize("field", ["cost_usd", "wall_time_seconds"])
@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")])
def test_usage_rejects_nonfinite_numeric_values(field, nonfinite):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        UsageMetrics(**{field: nonfinite})


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")])
def test_integrity_hash_rejects_noncanonical_floats(nonfinite):
    from agent_playbook_os.integrity import canonical_hash
    with pytest.raises(ValueError):
        canonical_hash({"metric": nonfinite})


@pytest.mark.asyncio
async def test_stream_resource_is_closed_after_limit_abort(tmp_path):
    closed = []
    async def handler(step, inputs, context):
        async def stream():
            try:
                for _ in range(100):
                    yield RuntimeStreamEvent(type="progress", value="tick")
            finally:
                closed.append("yes")
        return stream()
    runtime = CallableRuntime(actions={"stream": handler})
    state = await Runner(
        runtime, max_stream_events=1, max_stream_event_bytes=4096,
        max_stream_total_bytes=8192
    ).run(plan(side_effects="external"), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert closed == ["yes"]
