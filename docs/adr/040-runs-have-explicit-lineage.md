# ADR-040: Replays, forks and subruns have explicit lineage

**Status:** Accepted

RunState stores parent/root identities, lineage depth and fork reason. Experimental and replayed runs are new immutable provenance branches rather than mutations of their source run.
