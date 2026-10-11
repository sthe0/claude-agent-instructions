"""The background debt cycle: the driver that spends a standing mandate on backlog issues.

`driver` runs a cycle; `notifiers` is the digest-delivery seam. The bounds the driver obeys are
the pure rules in `agentctl.mandate`, and its persistence is `agentctl.mandate_store` -- this
package adds only orchestration and never restates a rule.
"""
