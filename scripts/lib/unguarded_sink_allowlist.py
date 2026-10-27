"""The frozen allowlist of hard-outcome sinks with no same-scope judge_* call.

Difficulty removed: scripts/verify-semantic-gates.py (the standing per-run
verifier) and scripts/tests/test_no_semantic_unguarded.py (the allowlist-growth
pin) answer the same question about the same enumerator output; one copy of the
allowlist keeps them from disagreeing about which sites are known.

A site is listed here when crutch-inventory.py's enumerate_code_sites reports a
real deny/block/exit(2) sink in its scope and no judge_* call in that scope.
Keyed by the site's stable id (file + scope name, never the regex's content);
value is (file, scope, why-safe ground). Add an entry only for a genuine false
pairing (the regex reads unrelated structural syntax); otherwise guard the site
with a fail-open judge_* call (agentctl/advisor.py::judge_binary_ask).
"""
from __future__ import annotations

HARD_SINK_CLASSES = frozenset({"pretooluse_deny", "stop_block", "exit_code_2"})

KNOWN_UNGUARDED_HARD_SINKS = {
    "470d72f243768e36": (
        "scripts/record-experience.py", "refuse",
        "sink-only scope with no regex: refuse() prints caller-built problems "
        "and exits 2; every caller derives them from structure "
        "(canonical-checkout routing, manifest JSON shape, path existence, "
        "SLUG/CONTEXT_ID/REVISION identifier fullmatch); the judged "
        "same-difficulty refusal in cmd_new exits via sys.exit(message), not "
        "via refuse().",
    ),
    "156a5c93ad796c48": (
        "scripts/hook-escalation-diagnosis-gate.py", "deny_with",
        "the judge_outage_escalation call that guards this deny happens one "
        "scope up, in decide() — the scope-local severing crutch-inventory.py's "
        "own docstring names; confirmed by reading decide() -> deny_with() at "
        "source.",
    ),
    "a0ce40334c52b3b1": (
        "scripts/hook-published-text-writer-gate.py", "deny_with",
        "decide() dispatches to _decide_text (structural transcript-witness "
        "check, then judge_published_text_rules behind a candidate-only "
        "prefilter) or _decide_attachment (judge_published_attachment behind a "
        "structural prefilter) one scope up — the same scope-local severing as "
        "the hook-escalation-diagnosis-gate.py row above; confirmed by reading "
        "decide() -> deny_with() at source.",
    ),
    "c66f1c838758119e": (
        "scripts/hook-guard-canon-readonly.py", "main",
        "structural: this file has no local regex; its one-hop transitive "
        "import lib.shell_tokens carries only shell-token-syntax regexes "
        "(assignment-prefix / delimiter-word / definition patterns), never "
        "natural-language meaning.",
    ),
    "6fd054ca18985f63": (
        "scripts/hook-guard-bash-source-edit.py", "main",
        "structural: the verdict is a function of shell syntax (write-verb "
        "shape, redirect target) and filesystem facts (git work tree, `.arc` "
        "marker, allowlist roots) only; the one regex is the `NAME=value` "
        "assignment-prefix shape, and one-hop import lib.shell_tokens carries "
        "only shell-token-syntax regexes, never natural-language meaning.",
    ),
    "943098c19a6861d7": (
        "scripts/hook-guard-destructive-rm.py", "main",
        "structural: only regex is shell-variable syntax "
        "(`\\$\\{?\\w+\\}?`), never natural-language meaning.",
    ),
    "4c318993ddc97c55": (
        "scripts/hook-multi-mount-search-guard.py", "_run",
        "structural: the verdict from decide() is a function of shlex-token "
        "command shape, path containment and the /proc mounts fstype; the only "
        "regex is the mounts octal decode in fuse_mounts_from_text, never "
        "natural-language meaning.",
    ),
    "6b802bd6c3ae8553": (
        "scripts/hook-plan-delivery-gate.py", "deny_with",
        "structural: this file contains no regex, at scope or anywhere in its "
        "full transitive import closure (agentctl.delivery, agentctl.gates, "
        "agentctl.state, agentctl.text_shape, lib.config_root, "
        "lib.transcript_turns — all verified regex-free); the deny is driven "
        "by engine-state / path predicates.",
    ),
    "756f3a83003805ac": (
        "scripts/hook-scope-conflict.py", "deny",
        "structural: this file has no local regex; its two-hop transitive "
        "import (session_scope.detector -> agentctl.exempt_paths) carries only "
        "a file-extension path-syntax regex (_PRODUCTION_FILE_RE), never "
        "natural-language meaning.",
    ),
    "c487fa3edf74bd84": (
        "scripts/hook-state-gate.py", "deny_with",
        "structural: this file has no local regex; its one-hop transitive "
        "import agentctl.exempt_paths carries only a file-extension "
        "path-syntax regex (_PRODUCTION_FILE_RE), never natural-language "
        "meaning.",
    ),
    "23f1443ae65073d6": (
        "scripts/hook-turn-end-gate.py", "decide",
        "decide() calls build_context() (which runs every prefilter + judge_* "
        "call and freezes the results) then collect_blockers() — the "
        "propagation point where the judge-guarded booleans reach this "
        "stop_block sink.",
    ),
    "43ad016e7533db1e": (
        "scripts/hook-guard-agent-specialist-stand-in.py", "main",
        "structural: main() calls decide(), which matches a literal "
        "`skills/.../SKILL.md` path token in the Agent/Task prompt and "
        "description fields — path syntax, no natural-language classification; "
        "the deny verdict rests on the token's presence, not on what the prompt "
        "means — confirmed by reading decide() at source.",
    ),
    "69a68ca9bdda4152": (
        "scripts/hook-guard-subagent-destructive-action.py", "main",
        "structural: main() calls decide(), which gates on two structural "
        "facts only — payload shape (agent_id/agent_type presence, via "
        "_is_subagent_call) and hook-guard-destructive-rm.py's "
        "_rm_recursive_targets, a shlex-token flag parser recognizing -r/-R/"
        "--recursive; no free-text or natural-language classification "
        "anywhere in the call chain — confirmed by reading decide() and "
        "_rm_recursive_targets at source.",
    ),
}
