"""WAITING_APPROVAL CLI exit stays 0; stderr must name the resume command."""

from types import SimpleNamespace

from agent_playbook_os.cli import _hint_waiting_approval
from agent_playbook_os.models import RunStatus


def test_waiting_approval_hint_names_resume_and_approve(capsys):
    state = SimpleNamespace(
        status=RunStatus.WAITING_APPROVAL,
        pending_approvals=["approval"],
    )
    _hint_waiting_approval(".playbook-runs/demo-run", state)
    err = capsys.readouterr().err
    assert "playbook resume .playbook-runs/demo-run" in err
    assert "--approve approval" in err
    assert "--approval-actor" in err


def test_completed_status_prints_no_waiting_hint(capsys):
    state = SimpleNamespace(status=RunStatus.COMPLETED, pending_approvals=[])
    _hint_waiting_approval(".playbook-runs/done", state)
    assert capsys.readouterr().err == ""
