.PHONY: test schemas validate smoke eval conformance preflight reproducible benchmark-smoke release-check check

test:
	python -m pytest -q

schemas:
	python scripts/export_schemas.py

validate:
	python scripts/validate_repo.py

smoke:
	python scripts/smoke_demo.py

eval:
	python -m agent_playbook_os.cli eval evals/cases/kernel.jsonl
	python -m agent_playbook_os.cli eval evals/cases/adversarial.jsonl

conformance:
	python -m agent_playbook_os.cli conformance --dry-run

preflight:
	python -m agent_playbook_os.cli preflight playbooks/examples/kernel-demo.yaml --input name=Ada --dry-run

reproducible:
	python scripts/check_reproducible_release.py

benchmark-smoke:
	python -m agent_playbook_os.cli benchmark playbooks/examples/kernel-demo.yaml --input name=Ada --dry-run --approve approval --approval-actor benchmark --repeat 3 --run-root .playbook-runs/benchmark-smoke

release-check:
	python scripts/release_check.py

check: test schemas validate smoke eval conformance preflight
