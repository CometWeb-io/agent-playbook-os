# ADR-039: Playbook promotion is evidence-gated

**Status:** Accepted

The runtime may collect observations for ephemeral candidates but may not silently self-modify reusable playbooks. Promotion requires a verified append-only observation chain, configured success/diversity thresholds and an explicit maintainer action.
