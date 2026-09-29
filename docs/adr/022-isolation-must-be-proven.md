# ADR-022: Isolation must be proven by the runtime

## Decision

Treat `sandbox` and `subagent` as runtime capabilities, not descriptive labels.

## Rationale

A playbook cannot create a security boundary by naming one.

## Consequence

Requested/required isolation fails closed unless the active runtime adapter declares support.
