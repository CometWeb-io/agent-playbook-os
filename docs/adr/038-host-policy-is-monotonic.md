# ADR-038: Host policy composition is monotonic

**Status:** Accepted

Host/org policy cannot be weakened by model-generated or repository-authored playbook policy. Allowlists intersect, denylists and mandatory controls union, ceilings take the stricter value, and host isolation requirements win conflicts.
