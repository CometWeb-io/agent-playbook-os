"""Do not permit output JSON Schema to read remote files, URLs or other resources."""
import hashlib
import json
import pytest
from jsonschema import ValidationError

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import IntegrityError
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.schema_resolver import (
    MappingSchemaResolver, MAX_SCHEMA_BYTES, validate_output_offline, verify_schema_bytes,
)


def contract(schema, *, output=None):
    """Return a remote-pinned playbook and resolver without making network calls."""
    payload = json.dumps(schema, separators=(",", ":"), sort_keys=True).encode()
    url = "https://schemas.invalid/output.json"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "schema-boundary", "version": "0.1.0", "description": "schema boundary"},
        "spec": {"steps": [{
            "id": "step", "type": "action", "action": "set",
            "with": {"value": output if output is not None else {"ok": True}},
            "output_schema": url, "output_schema_hash": digest,
        }]},
    })
    return Compiler().compile(pb), MappingSchemaResolver({url: payload})


@pytest.mark.parametrize("reference", [
    "http://127.0.0.1:8000/secret",
    "https://internal.invalid/config.json",
    "file:///etc/passwd",
    "../secret.json",
    "/tmp/private.json",
    "urn:internal:untrusted",
])
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_external_refs_rejected_without_network_or_filesystem(keyword, reference):
    schema = {"allOf": [{"type": "object"}, {keyword: reference}]}
    with pytest.raises(ValueError, match="external JSON Schema reference denied"):
        validate_output_offline({"ok": True}, schema)


@pytest.mark.asyncio
async def test_pinned_remote_schema_cannot_use_secondary_refs(tmp_path):
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"okay": {"type": "object"}},
        "$ref": "http://127.0.0.1:19999/private.json",
    }
    plan, resolver = contract(schema)
    state = await Runner(ReferenceRuntime(), schema_resolver=resolver).run(plan, tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert "external JSON Schema reference denied" in state.steps["step"].error


def test_internal_json_pointer_works_and_validates_output():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"complete": {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"const": True}},
        }},
        "$ref": "#/$defs/complete",
    }
    validate_output_offline({"ok": True}, schema)
    with pytest.raises(ValidationError):
        validate_output_offline({"ok": False}, schema)


def test_local_anchor_is_supported():
    schema = {
        "$defs": {"value": {"$anchor": "result", "type": "integer"}},
        "$ref": "#result",
    }
    validate_output_offline(3, schema)
    with pytest.raises(ValidationError):
        validate_output_offline("no", schema)


def test_remote_bytes_rejected_before_json_parse_or_hash_check():
    payload = b" " * (MAX_SCHEMA_BYTES + 1)
    with pytest.raises(ValueError, match="2 MB"):
        verify_schema_bytes(payload, "sha256:" + "0" * 64)


def test_direct_schema_is_bounded_and_fails_closed():
    huge = {"description": "x" * (MAX_SCHEMA_BYTES + 1)}
    with pytest.raises(ValueError, match="too large"):
        validate_output_offline({}, huge)


def test_structural_depth_limit_rejects_deep_nested_schemas():
    node = {"type": "object"}
    for _ in range(90):
        node = {"allOf": [node]}
    with pytest.raises(ValueError, match="structural limits"):
        validate_output_offline({}, node)


@pytest.mark.asyncio
async def test_local_relative_schema_cannot_indirectly_fetch_file(tmp_path):
    source = tmp_path / "parent.yaml"
    source.write_text("# source anchor", encoding="utf-8")
    target = tmp_path / "schema.json"
    target.write_text(json.dumps({"$ref": "file:///etc/passwd"}), encoding="utf-8")
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "local-guard", "version": "0.1.0", "description": "schema guard"},
        "spec": {"steps": [{
            "id": "x", "type": "action", "action": "set",
            "with": {"value": "safe"}, "output_schema": "schema.json",
        }]},
    })
    plan = Compiler().compile(pb, source_path=str(source))
    state = await Runner(ReferenceRuntime()).run(plan, tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert "external JSON Schema reference denied" in state.steps["x"].error
