# ADR-041: Adaptive loops are bounded and durable

**Status:** Accepted

`foreach` and `while` control flow must have hard execution ceilings. `while` requires `max_iterations`; nested steps have stable durable identities so approval/recovery does not replay completed work. Hitting a still-true loop condition at the ceiling fails closed.
