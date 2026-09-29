# ADR-034: Attestation signing is a host-owned interface

## Decision

Keep the attestation payload/verification contract generic and inject a signer; local HMAC is only the reference implementation.

## Rationale

Production key custody may live in KMS, HSM or Vault and should not require changing run formats or importing cloud SDKs into core.

## Consequence

`CallableSigner` can bridge external sign/verify services. Keys remain outside run state and artifacts.
