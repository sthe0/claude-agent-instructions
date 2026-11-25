NEGATIVE_ANCHOR = "A1"
SUBJECT_FILES = [
    "integrations/opencode/plugin/agent-gates.ts",
    "integrations/opencode/plugin/package.json",
    "integrations/opencode/parity.toml",
    "scripts/opencode-gate-bridge.py",
    "scripts/lib/opencode_events.py",
    "scripts/lib/engine_session_binding.py",
    "scripts/agentctl/runtime_host.py",
    "scripts/verify-layout-contract.sh",
    "scripts/canon_writers.toml",
    "scripts/lib/unguarded_sink_allowlist.py",
    "docs/operations/lexical-meaning-inventory.toml",
    "docs/operations/hook-unparsable-input-inventory.toml",
    "scripts/hook-ask-answer-record.py",
    "scripts/hook-branch-backup.py",
    "scripts/hook-burn-rate-guard.py",
    "scripts/hook-canon-guard-wired-check.py",
    "scripts/hook-deferring-disposition-gate.py",
    "scripts/hook-engine-start.py",
    "scripts/hook-escalation-diagnosis-gate.py",
    "scripts/hook-guard-agent-specialist-stand-in.py",
    "scripts/hook-guard-bash-source-edit.py",
    "scripts/hook-guard-canon-readonly.py",
    "scripts/hook-guard-destructive-rm.py",
    "scripts/hook-guard-permission-surface.py",
    "scripts/hook-guard-subagent-destructive-action.py",
    "scripts/hook-long-job-arm.py",
    "scripts/hook-memory-consistency.py",
    "scripts/hook-memory-retrieval.py",
    "scripts/hook-multi-mount-search-guard.py",
    "scripts/hook-plan-delivery-gate.py",
    "scripts/hook-prompt-reminders.py",
    "scripts/hook-published-text-writer-gate.py",
    "scripts/hook-reaper.py",
    "scripts/hook-resolution-reminder.py",
    "scripts/hook-retry-detector.py",
    "scripts/hook-review-mergeable-guardian.py",
    "scripts/hook-review-monitor-arm.py",
    "scripts/hook-run-url-surfaced-reminder.py",
    "scripts/hook-scope-conflict.py",
    "scripts/hook-scope-track.py",
    "scripts/hook-self-critique-reminder.py",
    "scripts/hook-session-due.py",
    "scripts/hook-si-freetext-answer.py",
    "scripts/hook-skill-first.py",
    "scripts/hook-state-gate.py",
    "scripts/hook-telemetry-session-end.py",
    "scripts/hook-term-neutrality.py",
    "scripts/hook-turn-end-gate.py",
    "scripts/verify-experience-leaf.py",
    "scripts/verify-leaf-structure.py",
    "scripts/verify-no-conflict-markers.py",
]


def install_inert_shim(config_dir):
    """Write the harness's inert copy of the gate shim into ``config_dir`` and nowhere else.

    The copy is the shipped shim with its refusal made a no-op and its turn-end hold made a no-op,
    so a deny-resting case run with it must see its marked call execute. Returns the directory
    that holds the plugin (``<config_dir>/agent-gates``)."""
    from pathlib import Path

    from opencode_probe.adapter import shared_a

    target = Path(config_dir) / "agent-gates"
    target.mkdir(parents=True, exist_ok=True)
    for name, text in shared_a.shim_files(inert=True).items():
        (target / name).write_text(text, encoding="utf-8")
    return target
