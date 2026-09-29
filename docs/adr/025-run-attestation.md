# ADR-025: Optional HMAC run attestation

## Decision

Support an optional HMAC attestation over plan identity, state integrity, event-chain head and artifacts.

## Rationale

A hash chain detects drift but does not authenticate an attacker who can rewrite and recompute the entire run directory.

## Consequence

Authenticity can be anchored to a secret kept outside the run. The secret is never persisted by Agent Playbook OS.
