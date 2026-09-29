import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.loader import MAX_PLAYBOOK_BYTES, load_playbook
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime


def test_loader_rejects_oversized_playbook(tmp_path):
    path = tmp_path / "big.yaml"
    path.write_bytes(b"x" * (MAX_PLAYBOOK_BYTES + 1))
    with pytest.raises(ValueError, match="too large"):
        load_playbook(path)


@pytest.mark.asyncio
async def test_remote_output_schema_is_not_silently_skipped(tmp_path):
    from agent_playbook_os.errors import CompileError
    pb = Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1","kind":"Playbook",
        "metadata":{"id":"remote-schema","version":"0.2.0","description":"remote schema"},
        "spec":{"steps":[{
            "id":"x","type":"action","action":"set","with":{"value":{"ok":True}},
            "output_schema":"https://example.test/schema.json"
        }]}
    })
    with pytest.raises(CompileError, match="requires output_schema_hash pin"):
        Compiler().compile(pb)
