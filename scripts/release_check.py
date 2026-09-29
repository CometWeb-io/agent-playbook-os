from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str):
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    run(sys.executable, "-m", "pytest", "-q")
    run(sys.executable, "scripts/export_schemas.py")
    run(sys.executable, "scripts/validate_repo.py")
    run(sys.executable, "scripts/smoke_demo.py")
    run(sys.executable, "scripts/operator_smoke.py")
    run(sys.executable, "scripts/distributed_smoke.py")
    run(sys.executable, "-m", "agent_playbook_os.cli", "doctor")
    run(sys.executable, "-m", "agent_playbook_os.cli", "preflight", "playbooks/examples/kernel-demo.yaml", "--input", "name=Ada", "--dry-run")
    run(sys.executable, "-m", "agent_playbook_os.cli", "plugins", "list")
    run(sys.executable, "-m", "agent_playbook_os.cli", "eval", "evals/cases/kernel.jsonl")
    run(sys.executable, "-m", "agent_playbook_os.cli", "eval", "evals/cases/adversarial.jsonl")
    run(sys.executable, "-m", "agent_playbook_os.cli", "conformance", "--dry-run")
    run(sys.executable, "-m", "agent_playbook_os.cli", "queue", "conformance")
    run(sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests")
    run(sys.executable, "scripts/check_reproducible_release.py")
    run(sys.executable, "scripts/check_reproducible_wheel.py")
    print("RELEASE_CHECK_OK")


if __name__ == "__main__":
    main()
