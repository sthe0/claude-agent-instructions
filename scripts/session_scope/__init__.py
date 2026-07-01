"""session_scope — reusable session -> filesystem-scope registry.

Difficulty removed: parallel Claude Code sessions share one working tree / arc
mount with no way to see what another live session is touching, so collisions
are caught only after the fact (git status forensics, stash surgery). This
package gives any session a place to record its active scope (cwd, repo root,
VCS kind, touched paths) so a later online conflict detector (a separate
primitive) can compare LIVE sessions' scopes without re-deriving them.

Deliberately independent of agentctl/state.py: agentctl owns coordination
state (workflow node, stages, gates); this module owns filesystem scope only,
so it stays reusable outside the coordination spine.

registry.py is the sole module: ScopeRecord (the record) + record_touch /
set_context / heartbeat / load_all / live_sessions / prune_stale (the API).
All clock values are caller-injected (now_ts) — the module never reads the
wall clock itself, so tests are fully deterministic.
"""
