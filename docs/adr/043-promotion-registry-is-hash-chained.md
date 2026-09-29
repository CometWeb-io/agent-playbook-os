# ADR-043: Promotion observations are hash-chained provenance

**Status:** Accepted

Promotion observations carry `prev_hash` and `record_hash`. The registry must verify before append, readiness evaluation or promotion. Promotion receipts bind to the current registry head so maintainers can detect post-hoc observation tampering.
