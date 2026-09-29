# Secret resolution

Playbooks carry secret **references**, not credentials.

Supported reference forms in the reference implementation:

```text
env://NAME
secret://logical-key
file-secret://relative/path
```

## Resolution lifecycle

```text
validated reference
  -> compile/persist reference only
  -> step becomes runnable
  -> resolver resolves immediately before runtime call
  -> runtime sees SecretValue marker
  -> durable output/events/receipts are redacted
```

The raw value must not be written into `state.json`, SQLite state, event log, runtime cassette or telemetry by the kernel.

## Resolver chain

- `EnvSecretResolver` reads process environment;
- `MappingSecretResolver` is a host injection point for `secret://`;
- `FileSecretResolver` confines reads to explicitly configured roots;
- `SecretResolverChain` tries resolvers in order.

External secret managers should implement the same resolver protocol and remain provider packages/plugins.

## File secrets

File paths are resolved against configured roots and may not escape them. The resolver also applies a maximum byte size.

CLI example:

```bash
playbook run playbooks/examples/example.yaml \
  --file-secret-root /run/secrets
```

## Residual boundary

The kernel can redact values it resolves/marks. It cannot guarantee that a malicious runtime adapter or provider SDK will not log a secret after receiving it. Adapter logging and credential scopes are separate security boundaries.
