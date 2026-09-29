# ADR-036: The model-backed Operator is advisory

**Status:** Accepted

Planner output is untrusted structured input. It may select or draft a playbook, but cannot execute work directly. Every ephemeral candidate traverses the same validation, compilation, policy and capability boundaries as authored playbooks.
