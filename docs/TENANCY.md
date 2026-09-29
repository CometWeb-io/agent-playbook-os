# Tenant and namespace isolation

`TenantScope` contains two validated single path segments:

- `tenant_id`
- `namespace`

The reference filesystem helpers never concatenate unchecked arbitrary paths.

## Policy inheritance

`TenantPolicyResolver` loads, when present:

1. `<root>/policy.yaml`
2. `<root>/<tenant>/policy.yaml`
3. `<root>/<tenant>/<namespace>/policy.yaml`

Every layer is composed monotonically: allowlists intersect, denylists and mandatory approvals union, and numeric ceilings become the stricter minimum. A tenant or namespace cannot loosen a root policy.

Policy files must not be symlinks and their resolved path must remain under the configured root.

## Tenant secrets

`tenant-secret://NAME` is resolved by a `TenantFileSecretResolver` instantiated for one scope. `NAME` must be one path segment; `/`, `\\`, `.` and `..` are rejected. Symlinked secret files are rejected.

The resolver should be created by the worker/host after tenant authentication. Tenant identity must never be accepted solely from untrusted playbook content.

## Run paths

Queue-created run directories use:

```text
<run-root>/<tenant>/<namespace>/<run-id>
```

Workers may enforce `allowed_run_root` and `allowed_plan_root` before reading a queued plan or mutating a run.

## Non-goal

This layer defines kernel scoping and filesystem safety. It is not an identity provider, RBAC system or billing tenant model. Production hosts must authenticate the principal that is allowed to submit/claim work for a given scope.
