# Signed human-approval grants for distributed workers

The reference queue already separates `WAITING_APPROVAL` from `COMPLETED`.
Queue fields `approvals` and `approval_actor` are **untrusted input**. The
worker rejects them unless an independent host authority provides grants.

This module adds a concrete, offline-verifiable implementation:
`agent_playbook_os.approval_grants.HMACApprovalAuthorizer`. It uses standard
HMAC-SHA256. It does **not** authenticate a person by itself: the issuer must
authenticate and authorize the reviewer *before* signing the grant.

## Binding and validation

A signed grant binds all of the following:

| Bound field | Why |
| --- | --- |
| `audience` | Separate production, staging and other deployment trust domains |
| `key_id` and `issuer` | Explicit signing identity and key rotation |
| `actor` | Reviewer identity asserted by the authenticated issuer |
| `tenant_id`, `namespace`, `queue_name` | Prevent cross-tenant/queue reuse |
| `work_id`, `run_id` | Prevent moving authority to a different job |
| `plan_integrity_hash` | Bind the **exact** compiled plan, not its display name |
| `plan_path`, `run_dir` | Prevent redirection to a different plan file or durable run directory |
| `store_backend`, `max_attempts` | Prevent changing the approved storage/retry envelope |
| `approved_step_ids` | Approve only named, existing review gates |
| `not_before`, `expires_at`, `nonce` | Time-bound grant with an explicit issue ID |
| `signature` | HMAC-SHA256 over the canonical JSON payload |

HMAC keys must be at least **32 random bytes**. Use an independent key per
trust domain and manage it outside GitHub, plan input, logs and the work queue.

The v1 verifier enforces a maximum 24-hour issuance window (900 seconds by
default), a maximum 16 KiB grant size and clock skew of at most five minutes.
It compares signatures using constant-time equality. An incomplete, modified,
expired or incorrectly scoped grant is rejected before creating the run state.

Grant-bearing CLI output is redacted, and arbitrary queue metadata is **not**
copied into durable run state. Enqueue validates every signed path/backend field
before accepting a grant; worker verifies the HMAC and the complete scope.
Unsigned or overlarge grant files are not interpreted as authority. Instead, a SHA-256 digest of the submitted
metadata is retained as a provenance pointer.

## Recommended issuance flow

A trusted approval service verifies the real user session and permissions,
reads the frozen reviewed compiled plan, then issues a signed grant.

```python
import json
import os
from pathlib import Path

from agent_playbook_os.approval_grants import HMACApprovalAuthorizer
from agent_playbook_os.distributed import WorkSubmission
from agent_playbook_os.models import CompiledPlan

plan = CompiledPlan.model_validate_json(Path("compiled-plan.json").read_text())
request = WorkSubmission(
    work_id="review-job-42",
    run_id="review-run-42",
    queue_name="priority",
    tenant_id="acme",
    namespace="production",
    plan_path="/srv/plans/compiled-plan.json",
    run_dir="/srv/runs/acme/production/review-run-42",
    approvals=["review"],
)
authority = HMACApprovalAuthorizer(
    {"primary": os.environ["APBOS_APPROVAL_KEY"].encode("utf-8")},
    audience="agent-playbook-prod",
)
# IMPORTANT: reviewer identity and permission must already be verified by host.
grant = authority.issue(
    request, plan,
    approved_step_ids={"review"},
    actor="user-id-from-authenticated-session",
    issuer="your-approval-service",
    key_id="primary",
    ttl_seconds=900,
)
Path("grant.json").write_text(json.dumps(grant, indent=2))
```

A submission may then carry this signed grant. IDs and scope must match
exactly. The queue does **not** need the secret key; the worker validates the
signature immediately before dispatch.

```bash
playbook queue enqueue /srv/plans/compiled-plan.json \
  --queue-db /srv/work.sqlite3 \
  --queue-name priority --tenant acme --namespace production \
  --run-root /srv/runs \
  --work-id review-job-42 --run-id review-run-42 \
  --approval-grant-file grant.json

playbook worker once \
  --queue-db /srv/work.sqlite3 --queue-name priority \
  --allowed-plan-root /srv/plans --allowed-run-root /srv/runs \
  --approval-key-env APBOS_APPROVAL_KEY \
  --approval-key-id primary \
  --approval-audience agent-playbook-prod
```

The existing bare `queue enqueue --approve review` remains prohibited. A
worker without a trusted approval key or an injected host authorization
callback rejects queued approval requests.

## SDK embedding and key rotation

```python
authority = HMACApprovalAuthorizer(
    {
        "primary": current_random_secret_bytes,
        "retiring": prior_random_secret_bytes,
    },
    audience="agent-playbook-prod",
)
worker = DistributedWorker(
    queue=queue, runtime=runtime, runner_factory=runner_factory,
    approval_authorizer=authority,
)
```

The signing service chooses `key_id`; workers may temporarily accept both
current and retiring keys. Remove retired keys when outstanding grants expire.
The reference CLI supports one signing key from an environment variable;
multiple-key rotation is available through the SDK.

## Security limits and remaining release gates

- **HMAC verifies the issuer, not the human:** signing an arbitrary
  `actor` argument without first authenticating that actor is unsafe.
  Connect a real identity provider and approval policy before production use.
- **Cross-job replay is blocked** by work/run identity, scope, plan hash and
  expiration. A recovery retry of the *same* work ID may reuse the grant
  until it expires; this implementation does not implement one-use nonce
  consumption. Anti-rollback, revocation and one-time authorization require a
  transactional host-owned ledger.
- Grant issuance is a privileged operation: never expose the signing key in
  an unauthenticated endpoint. Treat `grant.json` as a scoped bearer
  capability until expiration, even though its HMAC key is not embedded.
- The worker's direct Python callback remains a host-owned trust boundary.
  An incorrectly written callback can issue broader grants; do not attach
  unauthenticated caller-controlled callbacks.
- This is **local functional integration**, not live production IAM,
  multi-host Postgres/fencing certification or a security penetration test.
