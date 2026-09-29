# ADR-016: Separate semantic fingerprints from integrity hashes

## Decision

A compiled plan has two hashes:

- `semantic_hash` — stable across recompilation on another machine when executable semantics and locked content are unchanged;
- `integrity_hash` — binds the exact serialized compiled plan, including compilation metadata and local resolution details.

## Why

One hash cannot simultaneously be portable/reproducible and bind every byte of a local compiled artifact. Replay comparisons use semantic identity; tamper detection uses integrity identity.
