# Plugins and capability negotiation

## Why plugins are separate from playbooks

A playbook describes required work. A plugin supplies host/runtime infrastructure. Mixing the two would let workflow content silently install or authorize execution code.

## Plugin groups

The discovery registry recognizes:

```text
agent_playbook_os.runtimes
agent_playbook_os.stores
agent_playbook_os.telemetry
agent_playbook_os.secrets
agent_playbook_os.schema_resolvers
agent_playbook_os.planners
```

Entry points are discovered as descriptors first. Provider code is loaded only when the host explicitly selects/creates the plugin.

```bash
playbook plugins list
playbook plugins list --kind runtime
playbook plugins list --kind planner
```

Duplicate plugin names in the same group are ambiguity errors.

## Capability registry

A runtime advertises typed capability descriptors such as:

```text
action:<name>
skill-runtime:<id>
agent-runtime:<id>
eval-runtime:<id>
isolation:<mode>
provider:<name>
receipt:<name>
streaming:<name>
```

IDs are unique within a registry.

## Negotiation

`playbook preflight` compares the compiled plan against runtime capabilities without invoking steps:

```bash
playbook preflight playbooks/examples/kernel-demo.yaml --dry-run
```

Missing required capabilities fail the report. Receipt support for material side effects may be reported as a warning unless policy/adapter contract requires it.

Strict run mode executes the same admission check before durable execution side effects:

```bash
playbook run ... --strict-capabilities
```

Capability negotiation prevents accidental mismatch; it does not prove implementation truthfulness or provider authorization.
