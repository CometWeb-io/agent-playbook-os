# ADR-015: Adapters may return a typed RuntimeResult envelope

## Decision

Plain step outputs remain valid. Production adapters may instead return `RuntimeResult`, containing the user-visible value plus usage metrics, artifact references, and evidence references.

## Why

Execution metadata must not be mixed into domain payloads with magic keys. A typed envelope gives the kernel a provider-neutral way to aggregate cost/tokens/provenance while preserving arbitrary specialist outputs.
