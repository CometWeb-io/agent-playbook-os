# ADR-017: Human approvals are durable records

## Decision

Approvals are persisted as typed records with step, actor, and timestamp and emit a `gate.approved` event. Prompt text cannot create an approval record.

## Why

A production gate needs an audit trail. A boolean in transient process memory is insufficient for release, destructive, financial, or customer-facing workflows.
