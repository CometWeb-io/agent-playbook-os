import pytest
from agent_playbook_os.catalog import PlaybookCatalog
from agent_playbook_os.operator import DeterministicOperator


def test_catalog_indexes_examples():
    cat = PlaybookCatalog.from_roots(["playbooks/examples"])
    ids = {x.id for x in cat.entries}
    assert "evidence-to-decision" in ids
    assert "audit-to-release" in ids


@pytest.mark.asyncio
async def test_reference_operator_routes_by_metadata():
    cat = PlaybookCatalog.from_roots(["playbooks/examples"])
    result = await DeterministicOperator().select("release qa audit", cat)
    assert result.selected is not None
    assert result.selected.id == "audit-to-release"


def test_catalog_detects_same_version_content_collision(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    base = '''apiVersion: playbook.agent/v1alpha1\nkind: Playbook\nmetadata:\n  id: same\n  version: 1.0.0\n  description: {desc}\nspec:\n  steps:\n    - id: x\n      type: action\n      action: set\n      with:\n        value: {value}\n'''
    (a / "x.yaml").write_text(base.format(desc="one", value=1))
    (b / "x.yaml").write_text(base.format(desc="two", value=2))
    cat = PlaybookCatalog.from_roots([a, b])
    assert "same@1.0.0" in cat.collisions()
