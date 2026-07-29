#!/usr/bin/env python3
"""Generate scripts/crutch_registry.toml from crutch-inventory.py's own enumeration.

Difficulty removed: stage 1 (crutch-inventory.py) mechanically enumerates ~2022
candidate sites across two domains; hand-typing a disposition for each one is
both infeasible and exactly the kind of undisciplined, unauditable classification
this plan exists to replace with structure. This generator makes the PERCEPTION
pass (stage 2) reproducible: it reads the enumerator's own live output, assigns
each site a disposition via a small set of NAMED, auditable PARTITIONS (this
file's own header table, mirrored into the registry file's header comment) plus
a short list of individually-grounded per-id overrides for the handful of sites
that actually matter (the true regex+hard-sink pairings; the CLAUDE.md rules with
a distinct fate). Re-running this script over an unchanged tree reproduces a
byte-identical registry — the classification is data, not a one-off hand edit
(CLAUDE.md preamble, "Separate rule from perception... record the judgment's
output as data").

This script is itself a stage-2 output: a PERCEPTION pass encoded as a lookup
table. It contains no regex that reads free-text MEANING to drive a hard block —
the string-matching helper below (`_ALREADY_MECHANIZED_RE`) is used only to
assist THIS classification pass (a one-time authoring aid a human/model reviewed
before landing the registry), never at runtime to gate anything; using it here
is the same "prefilter feeding a judgment pass, not a hard block" shape the
enumerator's own module docstring names as legitimate.

Honest limits: the partition table is a coarse, file-path-keyed classification.
For the two large bulk prose partitions (ordinary leaves, SKILL.md/policy.md)
it assigns one verdict per ~40-400 statements, refined only by whether the
statement's own text names a concrete enforcing artifact. It does not read
every one of the ~1113 prose statements individually — CLAUDE.md (37
statements) is the one exception, read and dispositioned by hand because it is
the top-priority governance file and small enough to review in full. This
coarse-grained approach is licensed by the stage-2 method's own "size
discipline" clause: partition explicitly, name the partition, never sample
silently. The deferral/scope report (stage 5) states this limit again next to
the claim it qualifies.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
REGISTRY_PATH = SCRIPTS_DIR / "crutch_registry.toml"


def _load_inventory_module():
    path = SCRIPTS_DIR / "crutch-inventory.py"
    spec = importlib.util.spec_from_file_location("crutch_inventory", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


inv = _load_inventory_module()


# --- Partition table (CODE domain) -------------------------------------------
# Evaluated top to bottom; first matching predicate wins. Each row is
# (partition_name, predicate(file: str) -> bool, class, disposition, ground).
# Applies identically to `code` sites and `code_file_rollup` entries (the
# rollup is the same file, folded to file granularity — same partition, same
# verdict, a rollup-specific ground suffix is appended by the caller).

CODE_PARTITIONS = [
    (
        "scripts/tests/**",
        lambda f: f.startswith("scripts/tests/"),
        "not-a-gate",
        "keep",
        "Synthetic gate-input fixtures: these exercise the real detectors/gates under "
        "controlled inputs (including deliberately adversarial ones) but are not "
        "themselves gates guarding production behaviour.",
    ),
    (
        "scripts/agentctl/**",
        lambda f: f.startswith("scripts/agentctl/"),
        "structural",
        "keep",
        "The deterministic engine. Its regexes parse the engine's own structured "
        "syntax (`[stage N]` markers, return-marker prefixes, path/venue tokens, "
        "landed-check identifiers); its hard-behaviour sinks are the engine's own "
        "command dispatch — the mechanism CLAUDE.md instructs everything else to "
        "route through, not a semantic classifier of free-text meaning.",
    ),
    (
        "scripts/hook-escalation-diagnosis-gate.py (judge-guarded)",
        lambda f: f == "scripts/hook-escalation-diagnosis-gate.py",
        "semantic-guarded",
        "keep",
        "Already fixed by the prior audit (regex-not-for-semantic-classification.md): "
        "`decide()` runs the `outage_escalation_detect` prefilter AND "
        "`agentctl.advisor.judge_outage_escalation` before returning non-None; both "
        "enumerated scopes (`main`, `deny_with`) only reach the deny sink downstream "
        "of that guarded `decide()` call — confirmed by reading the source. "
        "File-rollup judge_guarded=True corroborates.",
    ),
    (
        "scripts/hook-*.py (other guardian hooks)",
        lambda f: Path(f).name.startswith("hook-"),
        "structural",
        "keep",
        "PreToolUse/Stop guardians whose regex (where present) reads shell-command "
        "shape, path/variable syntax, or `/proc/self/mounts` table syntax — never "
        "natural-language meaning; the deny/block decision is driven by path "
        "containment, token flags, or engine state, not by the regex's semantic "
        "content.",
    ),
    (
        "scripts/lib/** (incl. term_ruleset.py)",
        lambda f: f.startswith("scripts/lib/"),
        "structural",
        "keep",
        "Shared primitives that read shell, path or VCS-command GRAMMAR — "
        "tokenization, separator and redirect shape, path containment, "
        "fixed-vocabulary lookups — and carry no allow/deny policy of their own: "
        "each consumer resolves the result in its own direction, so no regex here "
        "reaches a hard-outcome sink. Matching a known token's occurrence is not "
        "classifying what a sentence MEANS. Example: term_ruleset.py's "
        "`deny`/`exempt` patterns are a literal org-identifier denylist, the same "
        "shape as a secret scanner.",
    ),
    (
        "scripts/crutch-inventory.py (self)",
        lambda f: f == "scripts/crutch-inventory.py",
        "not-a-gate",
        "keep",
        "The enumerator's own regex construction is its analysis subject matter "
        "(building the AST/prose scan this whole plan runs on); none of its scopes "
        "reach a hard-outcome sink — it prints JSONL and exits 0/1 on --check.",
    ),
    (
        "scripts/gen_crutch_registry.py (self)",
        lambda f: f == "scripts/gen_crutch_registry.py",
        "not-a-gate",
        "keep",
        "This generator's own `_ALREADY_MECHANIZED_RE` is a one-time authoring aid "
        "for the stage-2 perception pass (see module docstring), not a runtime gate; "
        "it reaches no hard-outcome sink.",
    ),
    (
        "scripts/project_entry/**",
        lambda f: f.startswith("scripts/project_entry/"),
        "structural",
        "keep",
        "Project/registry entry-point routing keyed on config values and path "
        "shape, not free-text meaning.",
    ),
    (
        "scripts/difficulty_channel/**",
        lambda f: f.startswith("scripts/difficulty_channel/"),
        "structural",
        "keep",
        "Channel adapter selection keyed on machine-local config identity, not "
        "free-text meaning.",
    ),
    (
        "scripts/*.py (other top-level scripts)",
        lambda f: True,  # catch-all, must stay last
        "structural",
        "keep",
        "Regexes found are markdown/frontmatter/command-syntax parsing (headings, "
        "`---` frontmatter delimiters, shell command-prefix shape); hard-behaviour "
        "sinks reached in the same file are the engine-adjacent verbs "
        "(record_result/dispatch/route/select) used for legitimate structural "
        "purposes (tokenizing, scoring, spawn-tag extraction), not meaning "
        "classification.",
    ),
]

# Per-id overrides: the true regex+hard-sink SAME-SCOPE pairings the enumerator's
# own docstring flags as rare (4 of 745) — individually inspected (read at
# source) and confirmed to be FALSE pairings: the regex found in that scope
# parses structural syntax unrelated to the semantic content of the
# hard-behaviour call that happens to share the scope. Recorded individually
# per the stage-2 method's step 3 ("every semantic-unguarded code site... must
# be individually named, not hidden in a partition").
CODE_ID_OVERRIDES = {
    # hook-turn-end-gate.py: per-scope split against the prior audit's row
    # granularity (regex-not-for-semantic-classification.md lists 5 of this
    # file's TURN_GUARDIANS individually). The 4 scopes below actually consume
    # a judge-computed ctx.* boolean (build_context() runs the real prefilter +
    # judge_* call and freezes the result before any guardian runs) — confirmed
    # by reading build_context() and each guardian body. The other 4 enumerated
    # scopes in this file (collect_blockers, self_diagnose_findings_blockers,
    # resolution_turn_blockers, long_job_autowake_blockers) read no semantic
    # signal at all (store state / engine state / command shape) and correctly
    # fall through to the hooks-default STRUCTURAL partition below — no
    # override needed for them.
    ("scripts/hook-turn-end-gate.py", "self_improvement_blockers"): (
        "semantic-guarded", "keep",
        "Reads only ctx.self_improvement_feedback, which build_context() sets "
        "from si_feedback_detect.find_signals (prefilter) gated by a semantic "
        "judge — this guardian itself is pure boolean logic over that frozen "
        "result, per its own docstring (\"Pure: reads only the frozen ctx "
        "booleans\").",
    ),
    ("scripts/hook-turn-end-gate.py", "escalation_without_diagnosis_blockers"): (
        "semantic-guarded", "keep",
        "Reads only ctx.outage_escalation_sought, which build_context() sets via "
        "advisor.judge_outage_escalation (fail-open semantic judge over the "
        "outage_escalation_detect prefilter) — the Stop-hook backstop for the "
        "same judge-guarded PreToolUse gate in hook-escalation-diagnosis-gate.py.",
    ),
    ("scripts/hook-turn-end-gate.py", "prose_binary_ask_blockers"): (
        "semantic-guarded", "keep",
        "Reads only ctx.prose_binary_ask, which build_context() sets via "
        "judge_binary_ask — the pre-existing exemplar the other two judge-backed "
        "guardians in this file mirror (per the leaf's own description).",
    ),
    ("scripts/hook-turn-end-gate.py", "decide"): (
        "semantic-guarded", "keep",
        "The dispatcher scope: calls build_context() (which runs every prefilter "
        "+ judge_* call and freezes the results) then collect_blockers(); this is "
        "the propagation point where the judge-guarded booleans reach the actual "
        "Stop-block sink, so it inherits semantic-guarded rather than the file's "
        "structural default.",
    ),
    ("scripts/agentctl/cli.py", "cmd_present_plan"): (
        "structural", "keep",
        "False pairing: the scope's regex (`^\\[stage (\\d+)\\]`) extracts a stage "
        "number from markdown headers — unrelated to the `plan_review_blockers()` "
        "call in the same scope, which reads engine review-gate state, not the "
        "regex's match.",
    ),
    ("scripts/policy-scorecard.py", "_scan_session"): (
        "structural", "keep",
        "The scope's regexes classify BASH COMMAND SHAPE (a fixed prefix set: "
        "cat/grep/rg/tail/head/sed/awk/less/more/wc/jq/yt, plus a curl-usage "
        "pattern) to route a spawn-cost estimate — command-syntax classification, "
        "the CLAUDE.md-preamble-sanctioned STRUCTURAL case (\"tool-invocation "
        "shape, command syntax\"), not natural-language meaning.",
    ),
    ("scripts/record-experience.py", "cmd_extend"): (
        "structural", "keep",
        "False pairing: the scope's regexes match markdown/YAML syntax (`^###\\s` "
        "heading markers, `---` frontmatter delimiters) to locate a section span; "
        "`context_block()` (flagged hard-behaviour only because \"context_block\" "
        "contains the token \"block\") builds a text string, it does not gate "
        "anything.",
    ),
    ("scripts/spawn-specialist.py", "_spawn_tags"): (
        "structural", "keep",
        "False pairing: the regex `[A-Z][A-Z0-9]+-\\d+` matches ticket-key "
        "IDENTIFIER SYNTAX in the cwd path (structural, not meaning); "
        "`_spawn_tags` is flagged hard-behaviour only because its name contains "
        "the token \"spawn\" — it builds a label list, it does not itself spawn "
        "or dispatch anything.",
    ),
}


# --- Partition table (PROSE domain) ------------------------------------------

def _prose_partition(file: str) -> str:
    if file == "CLAUDE.md":
        return "CLAUDE.md"
    parts = Path(file).parts
    if parts[0] == "skills" and parts[-1] == "SKILL.md":
        return "skills/**/SKILL.md"
    if parts[0] == "skills" and parts[-1] == "policy.md":
        return "skills/**/policy.md"
    if parts[0] == "memory-global" and "experience" in parts:
        return "memory-global/leaves/experience/**"
    if parts[0] == "memory-global" and "system-knowledge" in parts:
        return "memory-global/leaves/system-knowledge/**"
    if parts[0] == "memory-global" and "principles" in parts:
        return "memory-global/leaves/principles/**"
    if parts[0] == "memory-global":
        return "memory-global/leaves/*.md (ordinary leaves)"
    raise ValueError(f"prose file outside the discover_prose_paths() domain: {file}")


PROSE_PARTITION_DEFAULT = {
    "CLAUDE.md": (
        "perception", "keep",
        "CLAUDE.md's own § Coordination heading names this class explicitly: "
        "\"Cognition the engine does NOT replace (always yours)\" — escalation "
        "timing, task-weight judgment, verification-axis discipline, and outcome "
        "framing are judgment calls over conversational/task content, not "
        "predicates decidable from observable repo/engine state.",
    ),
    "skills/**/SKILL.md": (
        "perception", "keep",
        "A specialization's TRIGGER/SKIP criteria are judgment calls over the "
        "shape of a request (does this task fit this specialization's scope) — "
        "the shared marker-protocol design deliberately keeps this as a "
        "model-read contract, not a set of machine-checkable predicates.",
    ),
    "skills/**/policy.md": (
        "perception", "keep",
        "Policy files elaborate the judgment procedure a specialization follows "
        "at each step; distinguishing, statement-by-statement, which already "
        "point at an existing enforced gate is out of this partition-level "
        "pass's budget (stated as a scope limit, not silently sampled) — see "
        "the already-mechanized override below for the subset whose own text "
        "names a concrete enforcing artifact.",
    ),
    "memory-global/leaves/experience/**": (
        "not-normative", "keep",
        "Experience leaves (difficulty/v1) are historical narrative records of a "
        "RESOLVED past difficulty; a modal keyword inside one narrates what was "
        "true or required at that past moment, or quotes/cites a rule whose "
        "structural home (if any) lives elsewhere — the leaf itself is not a "
        "standing command to mechanize.",
    ),
    "memory-global/leaves/system-knowledge/**": (
        "not-normative", "keep",
        "System-knowledge leaves document durable FACTS about how an external "
        "system behaves (leaf-schema.md's non-self-evident-fact criterion); a "
        "modal keyword here typically reports the external system's own "
        "constraint, not a live rule this agent must decide.",
    ),
    "memory-global/leaves/principles/**": (
        "perception", "keep",
        "Principle leaves (principle/v1) are deliberately generality-graded "
        "prose retrieved by the planner as judgment input at a plan's "
        "refutable-principle element (retrieval-augmented planning) — the "
        "schema's own design keeps these as prose, not compiled rules.",
    ),
    "memory-global/leaves/*.md (ordinary leaves)": (
        "perception", "keep",
        "leaf/v1 reference/feedback leaves record guidance for a future agent's "
        "judgment (leaf-schema.md's `## Guidance` section); many describe a "
        "mechanism that already exists elsewhere, but distinguishing precisely "
        "which — statement by statement, across 377 sites — is out of this "
        "partition-level pass's budget (see the already-mechanized override "
        "below and stage 5's published scope limit).",
    ),
}

# A prose statement whose own text names a concrete enforcing artifact (a
# script filename, a hook name, or the `agentctl` engine) is a self-describing
# POINTER to a mechanism, not an unmechanized rule — override the partition
# default to already-mechanized for these. This is a one-time authoring aid
# for THIS classification pass (see module docstring); it is not installed
# anywhere as a runtime check.
_ALREADY_MECHANIZED_RE = re.compile(
    r"\.py\b|hook-[\w-]+|enforced|`agentctl|verify-[\w-]+\.py|gates\.py|state\.py",
    re.IGNORECASE,
)
_ALREADY_MECHANIZED_APPLIES_TO = {
    "skills/**/SKILL.md",
    "skills/**/policy.md",
    "memory-global/leaves/experience/**",
    "memory-global/leaves/system-knowledge/**",
    "memory-global/leaves/principles/**",
    "memory-global/leaves/*.md (ordinary leaves)",
}
_ALREADY_MECHANIZED_GROUND = (
    "The statement's own text names a concrete enforcing artifact (a script "
    "filename, a `hook-*` name, or the `agentctl` engine) — a self-describing "
    "pointer to an existing mechanism. Verified only that the pattern matched "
    "the text; NOT verified that the named artifact fully implements the exact "
    "stated rule (an honest limit of this one-time authoring heuristic, stated "
    "in the module docstring and in stage 5's published scope)."
)

# CLAUDE.md is small enough (37 statements) to have been read individually in
# full; these are the sites whose fate differs from the partition default.
#
# A "defer" entry carries two extra tuple elements beyond GROUND: REASON (4th)
# and DEFERRED_SINCE (5th). ground answers "why is this CLASS correct"
# (decidable / already-mechanized / ...); reason answers "why is remediation
# DEFERRED right now" (an ordering or cost constraint); deferred_since is an
# AUTHORED-LITERAL YYYY-MM-DD date (never computed at generation time — a
# clock call here would break byte-identical regeneration), the date this
# defer was first authored, which self-diagnose.py's scan_crutch_regressions
# compares against scan-time wall-clock to decide when a deferral is overdue
# for review. A "keep" entry has nothing to defer, so it stays a 3-tuple;
# render_registry() never reads a reason or deferred_since for those.
CLAUDE_MD_OVERRIDES = {
    # The preamble statement this whole plan exists to enforce structurally.
    # Once stage 4 lands verify-semantic-gates.py, this IS that rule's
    # structural home; stage 5 reduces the prose to a pointer at it.
    "A regex that classifies free-text **meaning** to drive a hard block is this same split done wrong — the perception half never left the rule half: demote it to a high-recall prefilter and let a fail-open model judge decide, mirroring `judge_binary_ask` ([regex-not-for-semantic-classification](memory-global/leaves/regex-not-for-semantic-classification.md)).": (
        "decidable", "defer",
        "This is precisely the rule scripts/verify-semantic-gates.py mechanizes: "
        "AST-detects a regex feeding a hard sink with no `judge_*` call on the "
        "same path.",
        "Deferred rather than remediated AT THIS STAGE (stage 3) because that "
        "structural home is a STAGE-4 output — a rule cannot point at a "
        "mechanism that does not exist yet. Once stage 4 lands "
        "verify-semantic-gates.py, this prose becomes a pointer to that check; "
        "the deferral is published in stage 5's deferral list, not silently "
        "dropped.",
        "2026-07-29",
    ),
    "Hand-walking a deterministic chain, or recording a deterministically-decidable policy as prose, is the signal that mechanism is missing — **propose the structural form yourself**, don't wait to be asked; a local hook or script is a stopgap until a structural home exists.": (
        "decidable", "defer",
        "The general \"decidable policy left as prose\" detection stays a "
        "judgment call — that IS what crutch-inventory.py's Domain B exists "
        "to surface — so only the regex-specific clause this statement "
        "shares a sentence with is decidable and in scope for stage 4's "
        "mechanism; the rest of this statement's generality stays "
        "perception, reflected by keeping this override narrowly scoped.",
        "Deferred for the same ordering reason as the adjacent sentence (id "
        "5355bf72d79adb38): its structural home is also "
        "scripts/verify-semantic-gates.py, not yet landed at this point in "
        "the plan (stage 3, ahead of stage 4). Mechanized at stage 4, "
        "published in stage 5's deferral list.",
        "2026-07-29",
    ),
    "The plan-approval and resolution **gates are non-skippable** — production Edit/Write is denied until an execution node, and **production includes the agent's own config and instructions** (settings, skills, agents, `CLAUDE.md`, the `claude-agent-instructions/` repo); the only gate-exempt state-changing writes are **memory** and `/tmp/` scratch (the session scratchpad is **not** exempt).": (
        "already-mechanized", "keep",
        "Enforced by the agentctl engine's own gate state machine "
        "(scripts/agentctl/gates.py, hook-state-gate.py) — Edit/Write on "
        "production files is denied outside an execution node; this prose is "
        "already a pointer to that mechanism, not the enforcement itself.",
    ),
    "The plan must be **TOML** for the engine to track stages.": (
        "already-mechanized", "keep",
        "Self-enforcing: the engine's own plan parser (scripts/agentctl/plan.py) "
        "only accepts TOML — a non-TOML plan simply fails to parse/load, so "
        "there is no separate rule to mechanize.",
    ),
    "If still unclear → `PERMISSION-REQUEST:`; do not burn additional lookups.": (
        "decidable", "defer",
        "Decidable in principle: a per-turn lookup-attempt counter keyed by "
        "tool name would mechanically decide when the 1-lookup budget is "
        "exceeded.",
        "Not built: no such counter exists today, and the event (an unknown "
        "tool needing more than one lookup) is rare enough that the cost of "
        "building a counter was judged, this pass, not worth it relative to "
        "the size of the win — deferred rather than remediated.",
        "2026-07-29",
    ),
}


def _to_toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _code_disposition(file: str, scope_or_none: str | None) -> tuple[str, str, str]:
    if scope_or_none is not None:
        override = CODE_ID_OVERRIDES.get((file, scope_or_none))
        if override is not None:
            cls, disp, ground = override
            return cls, disp, ground
    for _name, predicate, cls, disp, ground in CODE_PARTITIONS:
        if predicate(file):
            return cls, disp, ground
    raise AssertionError(f"no partition matched file {file!r} (catch-all should have)")


def _prose_disposition(file: str, sentence: str) -> tuple[str, str, str, str, "str | None"]:
    """Returns (class, disposition, ground, reason, deferred_since). `reason`
    is only ever rendered for a "defer" disposition (render_registry); for
    "keep" it defaults to `ground` and is simply unused. `deferred_since` is
    likewise only ever populated for "defer" (an authored-literal date — see
    CLAUDE_MD_OVERRIDES) and stays None otherwise. CLAUDE_MD_OVERRIDES
    supplies genuinely distinct reason/deferred_since values for its 3
    "defer" entries (5-tuples); its 2 "keep" entries and every other
    partition stay 3-tuples, so `ground` is reused as `reason` there — a
    deliberate no-op, not a fallback for a missing distinct reason."""
    if file == "CLAUDE.md":
        override = CLAUDE_MD_OVERRIDES.get(sentence)
        if override is not None:
            if len(override) == 5:
                return override
            if len(override) == 4:
                cls, disp, ground, reason = override
                return cls, disp, ground, reason, None
            cls, disp, ground = override
            return cls, disp, ground, ground, None
        cls, disp, ground = PROSE_PARTITION_DEFAULT["CLAUDE.md"]
        return cls, disp, ground, ground, None
    partition = _prose_partition(file)
    if partition in _ALREADY_MECHANIZED_APPLIES_TO and _ALREADY_MECHANIZED_RE.search(sentence):
        return "already-mechanized", "keep", _ALREADY_MECHANIZED_GROUND, _ALREADY_MECHANIZED_GROUND, None
    cls, disp, ground = PROSE_PARTITION_DEFAULT[partition]
    return cls, disp, ground, ground, None


def build_entries() -> list[dict]:
    code_sites = inv.enumerate_code_sites(inv.SCRIPTS_DIR)
    rollups = inv.enumerate_code_file_rollups(inv.SCRIPTS_DIR)
    prose_sites = inv.enumerate_prose_sites(inv.discover_prose_paths(inv.REPO_ROOT))

    entries: list[dict] = []
    for site in code_sites:
        cls, disp, ground = _code_disposition(site.file, site.scope)
        entries.append({
            "id": site.id, "domain": "code", "file": site.file,
            "class": cls, "disposition": disp, "ground": ground,
        })
    for rollup in rollups:
        cls, disp, ground = _code_disposition(rollup.file, None)
        entries.append({
            "id": rollup.id, "domain": "code_file_rollup", "file": rollup.file,
            "class": cls, "disposition": disp,
            "ground": ground + " (file-level rollup of the file's scopes.)",
        })
    for site in prose_sites:
        cls, disp, ground, reason, deferred_since = _prose_disposition(site.file, site.sentence)
        entries.append({
            "id": site.id, "domain": "prose", "file": site.file,
            "class": cls, "disposition": disp, "ground": ground, "reason": reason,
            "deferred_since": deferred_since,
        })
    return entries


_HEADER_PARTITIONS = (
    [(name, "code") for name, *_ in CODE_PARTITIONS]
    + [(name, "prose") for name in PROSE_PARTITION_DEFAULT]
)


def render_registry(entries: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Generated by scripts/gen_crutch_registry.py — DO NOT HAND-EDIT.")
    lines.append("# Re-run: python3 scripts/gen_crutch_registry.py")
    lines.append("#")
    lines.append("# Named partitions (stage-2 method step 1/4 \"size discipline\"): every entry's")
    lines.append("# class/disposition/ground is assigned by exactly one of these partitions,")
    lines.append("# unless a per-id or per-file override in gen_crutch_registry.py applies.")
    lines.append("#")
    lines.append("# partition                                                  | domain | default class")
    lines.append("# -----------------------------------------------------------|--------|----------------")
    for name, domain, cls, _disp, _ground in [
        (n, "code", c, d, g) for n, _p, c, d, g in CODE_PARTITIONS
    ]:
        lines.append(f"# {name:58s} | {domain:6s} | {cls}")
    for name, (cls, _disp, _ground) in PROSE_PARTITION_DEFAULT.items():
        lines.append(f"# {name:58s} | prose  | {cls}")
    lines.append("#")
    lines.append("# Overrides on top of the partition table:")
    lines.append("#  - CODE_ID_OVERRIDES (8 entries): 4 true regex+hard-sink same-scope pairings,")
    lines.append("#    individually inspected and confirmed to be FALSE pairings (the regex reads")
    lines.append("#    structural syntax), plus 4 hook-turn-end-gate propagation overrides")
    lines.append("#    (semantic-guarded — the judge_* calls run one scope up in build_context()).")
    lines.append("#  - CLAUDE_MD_OVERRIDES: CLAUDE.md was read in full (37 statements); 5 of them")
    lines.append("#    diverge from the file's own perception default (3 decidable/defer — 2")
    lines.append("#    ordering-blocked on stage 4's verify-semantic-gates.py, 1 cost-judged —")
    lines.append("#    plus 2 already-mechanized).")
    lines.append("#  - already-mechanized heuristic: a prose statement outside CLAUDE.md whose own")
    lines.append("#    text names a concrete enforcing artifact (script/hook/agentctl) is reclassified")
    lines.append("#    from its partition default to already-mechanized (see _ALREADY_MECHANIZED_RE).")
    lines.append("")
    for entry in entries:
        lines.append("[[entry]]")
        lines.append(f"id = {_to_toml_string(entry['id'])}")
        lines.append(f"domain = {_to_toml_string(entry['domain'])}")
        lines.append(f"file = {_to_toml_string(entry['file'])}")
        lines.append(f"class = {_to_toml_string(entry['class'])}")
        lines.append(f"disposition = {_to_toml_string(entry['disposition'])}")
        lines.append(f"ground = {_to_toml_string(entry['ground'])}")
        if entry["disposition"] == "defer":
            # code entries never defer (see _code_disposition), so the
            # "reason"/"deferred_since" keys are only ever populated by
            # _prose_disposition; the fallbacks below are unreachable in
            # practice.
            lines.append(f"reason = {_to_toml_string(entry.get('reason', entry['ground']))}")
            lines.append(f"deferred_since = {_to_toml_string(entry.get('deferred_since') or '')}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    entries = build_entries()
    text = render_registry(entries)
    REGISTRY_PATH.write_text(text, encoding="utf-8")
    by_class: dict[str, int] = {}
    by_disp: dict[str, int] = {}
    for e in entries:
        by_class[e["class"]] = by_class.get(e["class"], 0) + 1
        by_disp[e["disposition"]] = by_disp.get(e["disposition"], 0) + 1
    print(f"wrote {len(entries)} entries to {REGISTRY_PATH}")
    print(f"class histogram: {by_class}")
    print(f"disposition histogram: {by_disp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
