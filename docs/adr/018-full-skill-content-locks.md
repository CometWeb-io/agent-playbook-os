# ADR-018: Skill locks cover the entire package

## Decision

Resolved skill locks hash the complete skill directory, including instructions, scripts, references, and assets. Symlinks are rejected and duplicate skill IDs across configured roots are ambiguous errors.

## Why

Hashing only `SKILL.md` misses executable or knowledge drift in supporting files. Root-order resolution is also not a safe supply-chain policy.
