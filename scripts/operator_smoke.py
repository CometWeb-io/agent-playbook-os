from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def run(*args: str) -> dict:
    cp = subprocess.run(
        [sys.executable, "-m", "agent_playbook_os.cli", *args],
        cwd=ROOT,
        env=ENV,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(cp.stdout)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="apbos-operator-smoke-") as td:
        root = Path(td)
        response = root / "planner-response.json"
        response.write_text(json.dumps({
            "mode": "ephemeral",
            "confidence": "high",
            "rationale": "deterministic release smoke",
            "playbook": {
                "apiVersion": "playbook.agent/v1alpha1",
                "kind": "Playbook",
                "metadata": {
                    "id": "operator-release-smoke",
                    "version": "0.5.0-ephemeral",
                    "description": "Operator release-gate smoke candidate",
                },
                "spec": {
                    "steps": [
                        {"id": "result", "type": "action", "action": "set", "with": {"value": "ok"}}
                    ],
                    "outputs": {"result": "{{ steps.result.output }}"},
                },
            },
        }, indent=2), encoding="utf-8")

        review_root = root / "review-runs"
        review = run(
            "operate", "produce release smoke result",
            "--planner-response", str(response),
            "--run-root", str(review_root), "--run-id", "review",
            "--strict-capabilities",
        )
        assert review["status"] == "PLAN_APPROVAL_REQUIRED", review
        plan_path = Path(review["compiled_plan"])
        assert plan_path.exists()
        assert not (review_root / "review" / "state.json").exists()

        executed = run(
            "run-plan", str(plan_path),
            "--run-root", str(root / "executed-runs"), "--run-id", "reviewed",
            "--strict-capabilities",
        )
        assert executed["status"] == "COMPLETED", executed
        assert executed["outputs"] == {"result": "ok"}, executed

        approved_root = root / "approved-runs"
        approved = run(
            "operate", "produce release smoke result",
            "--planner-response", str(response),
            "--run-root", str(approved_root), "--run-id", "sqlite",
            "--approve-plan", "--store", "sqlite", "--strict-capabilities",
        )
        assert approved["status"] == "COMPLETED", approved
        verified = run("verify", str(approved_root / "sqlite"))
        assert verified["ok"] is True, verified

    print("OPERATOR_SMOKE_OK")


if __name__ == "__main__":
    main()
