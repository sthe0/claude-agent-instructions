#!/usr/bin/env python3
"""Generate and maintain difficulty-centric experience leaves.

The unit of experience is a recurring difficulty (see
memory-global/leaves/experience-leaf-schema.md). This tool guarantees the
`schema: difficulty/v1` structure (experience, generality 0 — it stamps
`generality: 0` on newly created leaves; induction lifts it into a
regularity/v1 leaf, and a principle/v1 norm is grounded in regularities via
`grounded_in`), auto-dates filenames,
maintains the `experience/MEMORY.md` sub-index, and implements the
search-before-record / extend-with-a-new-context flow that lets one difficulty
accumulate several contexts.

Subcommands:
  search <keywords>   Rank existing leaves by description + `## Difficulty`
                      overlap. Default --tier experience: MANDATORY before
                      recording — extend an analogous leaf instead of
                      duplicating. --tier system-knowledge: retrieve a
                      durable system fact before authoring a plan that
                      touches that system (same difficulty-led ranking,
                      read-before-designing rather than extend-before-
                      recording). --tier regularities: the knowledge layer a
                      recurring cluster is lifted into; --tier principles: the
                      norm layer that names regularities.
  new                 Write a fresh standalone leaf (first occurrence).
  extend              Append a new `### context` to an existing leaf; once it
                      holds >=2 contexts, scaffold `## Common core & variations`.
  ticket              Write a THIN pointer leaf (record lives in the ticket)
                      and print the structured body to stdout for posting.
  split-contexts      Read-only: list `<id> <heading>` per `### ` context of a
                      leaf — the ids a split manifest names.
  split-apply         Execute a split manifest the memory curator wrote: move
                      contexts byte-exactly into per-cluster leaves, index
                      them, shrink the source to its kept contexts plus a
                      pointer list. Exit 2, nothing written, on an
                      inconsistent manifest.
  split-verify        Read-only: every context of the manifest's base appears
                      exactly once across source and cluster leaves, and the
                      memory verifiers pass. Prints `missing:` / `duplicate:` /
                      `unexpected:` / `misplaced:` / `verifier:` lines.
  promote             Write a regularity/v1 skeleton (the knowledge layer), its
                      regularities/MEMORY.md pointer, and record it in a
                      --manifest; stamps the new regularity into each source
                      leaf's `generalized_into`. --rule-derived REASON for a
                      regularity with no experience source. --principle
                      --grounded-in REG[,REG] writes a principle skeleton
                      (the norm layer) naming existing regularities instead.
                      --project-dir D writes into the project's own
                      regularities/ (or principles/ with --principle), as the
                      root does (D is the repo dir, as for every verb; a
                      memory dir holding experience/, regularities/ or
                      principles/ is also accepted).
  link / unlink       Write / remove every side of a leaf -> regularity (or
                      project leaf -> case) link: the leaf's
                      `generalized_into`, the regularity's `induced_from` and
                      its index pointer, atomically (lib/principle_provenance.py).
  case-add            Write sanitized case/v1 leaves of project leaves under
                      principles/cases/ and link them to global regularities;
                      refuses a case carrying a project identifier.
  backfill            Stamp `generalized_into` on every leaf from the
                      regularities' `induced_from`; idempotent.
  provenance          Read-only: --check lists unaccounted links (exit 1 on
                      any); --of SLUG lists every leaf a revision must consult.

Scope: --scope global (default; this repo's memory-global/leaves/experience)
or --scope project --project-dir <dir> (<dir>/.claude/agent-memory/experience).

Routing: the write subcommands (new, extend, ticket, set-last-verified) on the
global scope never edit the canonical checkout in place. Run from the main
checkout they write through a fresh worktree cut from origin/main, commit,
land on origin/main, fast-forward the canonical checkout and remove the
worktree (lib/worktree_route.py); run from a linked worktree they write there.
search and promote-scan, and --scope project, are not routed.

The split flow (split-apply, promote) is deliberately NOT routed: it produces a
proposal the user accepts, so both commands refuse (exit 2) to write under a
root that lies in the canonical checkout and, in a linked worktree, write in
place and commit nothing. The link verbs (link, unlink, case-add, backfill)
follow the same rule.

verify-experience-leaf.py enforces the shape this tool produces.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import edit_ledger  # noqa: E402
from lib import leaf_forms  # noqa: E402
from lib import memory_retrieval_ledger  # noqa: E402
from lib import principle_provenance as pp  # noqa: E402
from lib import semantic_join  # noqa: E402
from lib import worktree_route  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.md"
H2 = re.compile(r"^##\s", re.MULTILINE)
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def today() -> str:
    return _dt.date.today().isoformat()


def experience_dir(scope: str, project_dir: str | None) -> Path:
    if scope == "project":
        if not project_dir:
            sys.exit("--scope project requires --project-dir")
        return Path(project_dir) / pp.PROJECT_MEMORY_REL / "experience"
    return REPO_ROOT / "memory-global/leaves/experience"


# The ranking section per tier: experience leaves rank on their `## Difficulty`,
# principles on their `## Principle`, system-knowledge leaves on their `## Difficulty`
# too — leaf-schema.md requires every system-knowledge leaf to be difficulty-led (a
# full `## Difficulty` section under schema:leaf/v1, or the grandfathered
# description/lead-blockquote baseline; a leaf with neither has no matching section
# and degrades to description-only scoring, same as an experience leaf would). The
# term-counting ranking itself is identical across all three tiers.
TIER_SECTION = {
    "experience": "Difficulty",
    "regularities": "Regularity",
    "principles": "Principle",
    "system-knowledge": "Difficulty",
}


def search_root(scope: str, project_dir: str | None, tier: str) -> Path:
    # regularities and principles are global-only tiers (ADR-0001 generality
    # gradient); experience and system-knowledge keep the same global/project split.
    if tier == "regularities":
        return REPO_ROOT / "memory-global/leaves/regularities"
    if tier == "principles":
        return REPO_ROOT / "memory-global/leaves/principles"
    if tier == "system-knowledge":
        if scope == "project":
            if not project_dir:
                sys.exit("--scope project requires --project-dir")
            return Path(project_dir) / pp.PROJECT_MEMORY_REL / "system-knowledge"
        return REPO_ROOT / "memory-global/leaves/system-knowledge"
    return experience_dir(scope, project_dir)


# --------------------------------------------------------------------------
# section helpers
# --------------------------------------------------------------------------
def section_span(text: str, heading: str) -> tuple[int, int] | None:
    """Return (start, end) byte span of a `## <heading>` section, end being the
    start of the next `## ` heading or EOF. None if the heading is absent."""
    m = re.search(rf"^##\s+{re.escape(heading)}\b.*$", text, re.MULTILINE)
    if not m:
        return None
    nxt = H2.search(text, m.end())
    return m.start(), (nxt.start() if nxt else len(text))


def set_fm_field(text: str, key: str, value: str) -> str:
    """Insert or replace a top-level `key: value` line in the YAML frontmatter.

    Replaces the first existing top-level occurrence; otherwise appends the line
    just before the closing `---`. Returns text unchanged if there is no
    frontmatter block (callers handle that case explicitly)."""
    m = FRONTMATTER.match(text)
    if not m:
        return text
    fm_body = m.group(1)
    line_re = re.compile(rf"^{re.escape(key)}\s*:.*$", re.MULTILINE)
    if line_re.search(fm_body):
        new_fm = line_re.sub(f"{key}: {value}", fm_body, count=1)
    else:
        new_fm = fm_body.rstrip("\n") + f"\n{key}: {value}"
    return text[: m.start(1)] + new_fm + text[m.end(1):]


def context_block(date: str, label: str, where: str, plan: str) -> str:
    return (
        f"\n### {date} — {label}\n"
        f"- Where it arose: {where}\n"
        f"- Working plan: {plan}\n"
    )


# --------------------------------------------------------------------------
# leaf assembly
# --------------------------------------------------------------------------
def birth_lines() -> list[str]:
    return [f"{key}: {pp.format_list(value)}" for key, value in pp.birth_fields().items()]


def frontmatter(name: str, description: str, confirmed_by: str,
                refs: list[str] | None, plan_file: str | None,
                ticket: str | None, date: str, tier: int | None = None) -> str:
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        "type: reference",
        "schema: difficulty/v1",
        # Emitted on newly created leaves only; existing leaves are left
        # untouched (absence implies 0). Chain: experience (generality 0) ->
        # regularity/v1 (induced from experience) -> principle/v1 (norm grounded
        # in regularities) — see experience-leaf-schema.md.
        "generality: 0",
        *birth_lines(),
        f'resolution_confirmed_by_user: "{confirmed_by}"',
    ]
    # Difficulty tier (ADR-0002): emitted only when explicitly tier-1. Absence
    # implies tier 0 (state-level), so a clean run needs no tag — the σ-sentinel
    # (sigma-sentinel.py) treats untagged leaves as tier 0.
    if tier == 1:
        lines.append(f"tier: {tier}")
    if refs:
        lines.append("refs: [" + ", ".join(refs) + "]")
    if plan_file:
        lines.append(f"plan_file: {plan_file}")
    if ticket:
        lines.append(f"ticket: {ticket}")
    # Temporal frontmatter (memory-temporal-frontmatter.md): created is set once
    # at birth; last_verified equals created at birth and is bumped on revision.
    lines.append(f"created: {date}")
    lines.append(f"last_verified: {date}")
    lines.append("---\n")
    return "\n".join(lines)


def standalone_body(a) -> str:
    name = f"{a.date}-{a.slug}"
    parts = [
        frontmatter(name, a.description, a.confirmed_by, a.refs, a.plan_file,
                    None, a.date, getattr(a, "tier", None)),
        f"\n# {a.title}\n",
        "\n## Difficulty\n", f"{a.difficulty}\n",
        "\n## Order & criterion\n", f"{a.order}\n",
        f"\n**Acceptance check:** {a.criterion}\n",
        "\n## Contexts\n",
        context_block(a.date, a.context_label, a.context_where, a.plan),
        "\n## Cost\n", f"{a.cost or 'TODO — fill from the figure surfaced by `agentctl resolve` (see also scripts/cost-report.py)'}\n",
    ]
    if a.self_critique:
        parts += ["\n## Self-critique of the agent system\n", f"{a.self_critique}\n"]
    return "".join(parts)


def ticket_leaf_body(a) -> str:
    name = f"{a.date}-{a.slug}"
    url = a.ticket_url or a.ticket
    body = [
        frontmatter(name, a.description, a.confirmed_by, a.refs, None, a.ticket,
                    a.date, getattr(a, "tier", None)),
        f"\n# {a.title}\n",
        "\nFull structured record (Difficulty / Order & criterion / Context / "
        f"Working plan) — in the ticket: {url}.\n",
    ]
    if a.distill:
        body += [f"\n{a.distill}\n"]
    return "".join(body)


def ticket_comment(a) -> str:
    return (
        f"## Difficulty\n{a.difficulty}\n\n"
        f"## Order & criterion\n{a.order}\n\n"
        f"**Acceptance check:** {a.criterion}\n\n"
        f"## Context\n{a.context_where}\n\n"
        f"## Working plan\n{a.plan}\n"
    )


# --------------------------------------------------------------------------
# sub-index maintenance
# --------------------------------------------------------------------------
def update_subindex(exp_dir: Path, date: str, title: str, filename: str,
                    description: str, session: str | None = None) -> None:
    idx = exp_dir / "MEMORY.md"
    pointer = f"- [{date} — {title}]({filename}) — {description}\n"
    month = date[:7]
    if not idx.exists():
        text = (
            "# Experience\n\nDifficulty-centric experience leaves "
            "(see ../experience-leaf-schema.md). Most recent first.\n\n"
            f"## {month}\n\n{pointer}"
        )
    else:
        text = idx.read_text(encoding="utf-8")
        mhdr = re.search(rf"^##\s+{re.escape(month)}\s*$", text, re.MULTILINE)
        if mhdr:
            nl = text.find("\n", mhdr.end())
            insert_at = nl + 1
            # skip a single blank line after the header for neatness
            if text[insert_at:insert_at + 1] == "\n":
                insert_at += 1
            text = text[:insert_at] + pointer + text[insert_at:]
        else:
            first_month = re.search(r"^##\s+\d{4}-\d{2}\s*$", text, re.MULTILINE)
            block = f"## {month}\n\n{pointer}\n"
            if first_month:
                text = text[:first_month.start()] + block + text[first_month.start():]
            else:
                text = text.rstrip() + "\n\n" + block
    idx.write_text(text, encoding="utf-8")
    edit_ledger.stamp(str(idx), "record-experience:subindex", session=session)


# --------------------------------------------------------------------------
# ranking primitive (the search-before-record scorer)
# --------------------------------------------------------------------------
tokenize = semantic_join.tokenize
term_score = semantic_join.term_score

def cluster_by_ground_result(items, ground_fn):
    """Group items by functional ground: lexical overlap only nominates, a judge decides.

    Returns semantic_join's ClusterResult (groups, stats, per-item unjudged/undecided flags)."""
    return semantic_join.judged_clusters(items, ground_fn, k=semantic_join.K_CLUSTER)


def cluster_by_ground(items, ground_fn) -> list[list]:
    """Group items by functional ground; the first item in each group is the representative.
    ground_fn(item) -> str yields the comparison ground."""
    return cluster_by_ground_result(items, ground_fn).groups


DEFAULT_PRINCIPLE_PROMOTION_THRESHOLD = 3


def read_threshold(key: str, default: int, config_path: Path = CONFIG_PATH) -> int:
    """Read a numeric threshold from the config.md constants table by key name.

    Non-integer values (e.g. placeholders) and missing keys fall back to default.
    """
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if "`" + key + "`" in line and line.lstrip().startswith("|"):
                cells = [c.strip().strip("`") for c in line.split("|")]
                for cell in cells:
                    if cell.isdigit():
                        return int(cell)
    except FileNotFoundError:
        pass
    return default


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------
def ledger_record(event: str, a, query: str, targets) -> None:
    """Log what the session used against what the hook surfaced; never affects the run.

    A routed child (marked by the routing env) logs nothing: its parent logs once after
    the child exits 0."""
    if os.environ.get(worktree_route.GUARD_ENV):
        return
    try:
        session = getattr(a, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID")
        memory_retrieval_ledger.record_use(
            event, session=session, query=query, targets=[str(t) for t in targets])
    except Exception:
        pass


def written_leaf(a) -> str | None:
    """Path of the leaf a `new` run writes, or None for commands that name no single leaf."""
    if a.cmd == "extend":
        return a.leaf
    if a.cmd == "new":
        return str(experience_dir(a.scope, a.project_dir) / f"{a.date}-{a.slug}.md")
    return None


def cmd_search(a) -> int:
    tier = getattr(a, "tier", "experience")
    dom = getattr(a, "domain", None)
    root = search_root(a.scope, a.project_dir, tier)
    section = TIER_SECTION[tier]
    terms = tokenize(a.keywords)
    if not terms:
        sys.exit("search needs keywords")
    scored: list[tuple[int, Path, str]] = []
    if tier in leaf_forms.TIER_AREAS:
        candidates = leaf_forms.tier_leaves(REPO_ROOT / "memory-global/leaves", tier)
    else:
        candidates = sorted(root.glob("*.md"))
    for leaf in candidates:
        if leaf.name == "MEMORY.md":
            continue
        text = leaf.read_text(encoding="utf-8")
        fm = FRONTMATTER.match(text)
        desc = ""
        leaf_domain = None
        if fm:
            dm = re.search(r"^description:\s*(.*)$", fm.group(1), re.MULTILINE)
            desc = dm.group(1) if dm else ""
            ddm = re.search(r"^domain:\s*(\S+)", fm.group(1), re.MULTILINE)
            leaf_domain = ddm.group(1) if ddm else None
        if dom and tier in ("regularities", "principles") and leaf_domain and leaf_domain != dom:
            continue
        sec_span = section_span(text, section)
        sec = text[sec_span[0]:sec_span[1]] if sec_span else ""
        score = term_score(desc + " " + sec, terms)
        if score:
            scored.append((score, leaf, desc.strip()))
    scored.sort(key=lambda x: -x[0])
    noun = {"regularities": "regularity", "principles": "principle",
            "system-knowledge": "system-knowledge leaf"}.get(tier, "experience leaf")
    ledger_record("search", a, a.keywords, [leaf.resolve() for _, leaf, _ in scored[:3]])
    if not scored:
        print(f"no analogous {noun} found — record a NEW leaf")
        return 0
    verb = {"regularities": "cite", "principles": "ground a stage in",
            "system-knowledge": "read before designing"}.get(tier, "extend")
    plural = noun[:-1] + "ies" if noun.endswith("y") else noun + "s"
    print(f"analogous {plural} ({verb} one instead of duplicating):")
    for score, leaf, desc in scored[:8]:
        print(f"  [{score:>3}] {leaf.name}\n        {desc}")
        # The runnable form is ADDED, never substituted for the readable label —
        # and only in the tier where `extend` is the verb the listing offers: a
        # principle leaf has no `## Contexts`, so cmd_extend rejects it outright.
        if tier == "experience":
            print(f"        extend --leaf {leaf.resolve()}")
    return 0


def cluster_judge_line(stats: dict) -> str:
    return (
        f"cluster judge: judged_calls={stats['judged_calls']} cached_hits={stats['cached_hits']} "
        f"identity_joins={stats['identity_joins']} unjudged_items={stats['unjudged_items']}"
    )


def cmd_promote_scan(a) -> int:
    """Cluster the experience corpus by functional ground; flag clusters >= threshold."""
    import json as _json

    exp_dir = experience_dir(a.scope, a.project_dir)
    threshold = read_threshold(
        "principle-promotion-threshold", DEFAULT_PRINCIPLE_PROMOTION_THRESHOLD
    )
    if getattr(a, "threshold", None) is not None:
        threshold = a.threshold

    if not exp_dir.is_dir():
        print("no experience leaves found")
        return 0

    class _Rec:
        __slots__ = ("name", "ground", "occurrences")

        def __init__(self, name: str, ground: str, occurrences: int) -> None:
            self.name = name
            self.ground = ground
            self.occurrences = occurrences

    records = []
    for leaf in sorted(exp_dir.glob("*.md")):
        if leaf.name == "MEMORY.md":
            continue
        text = leaf.read_text(encoding="utf-8")
        fm = FRONTMATTER.match(text)
        desc = ""
        if fm:
            dm = re.search(r"^description:\s*(.*)$", fm.group(1), re.MULTILINE)
            desc = dm.group(1).strip() if dm else ""
        diff_span = section_span(text, "Difficulty")
        diff_body = text[diff_span[0]:diff_span[1]] if diff_span else ""
        ground = desc + " " + diff_body
        ctx_span = section_span(text, "Contexts")
        occurrences = 0
        if ctx_span:
            ctx_body = text[ctx_span[0]:ctx_span[1]]
            occurrences = len(re.findall(r"^###\s", ctx_body, re.MULTILINE))
        occurrences = max(1, occurrences)
        records.append(_Rec(leaf.name, ground, occurrences))

    if not records:
        print("no experience leaves found")
        return 0

    result = cluster_by_ground_result(records, lambda r: r.ground)
    stats = result.stats.as_dict()
    clusters = []
    for group in result.groups:
        total = sum(r.occurrences for r in group)
        members = [r.name for r in group]
        clusters.append({
            "occurrences_total": total,
            "members": members,
            "flagged": total >= threshold,
            "fragmented": len(members) >= 2,
        })
    clusters.sort(key=lambda c: -c["occurrences_total"])

    if getattr(a, "json_out", False):
        print(_json.dumps({"clusters": clusters, "stats": stats}, indent=2))
        return 0

    for c in clusters:
        print(f"[{c['occurrences_total']} occurrence(s)] {', '.join(c['members'])}")
        if c["flagged"]:
            print(f"  → candidate: lift into a regularity/v1 leaf "
                  f"(induced_from = {c['members']})")
        if c["fragmented"]:
            print(f"  → fragmented across {len(c['members'])} leaves "
                  f"— consider merging via extend")
    print(cluster_judge_line(stats))
    return 0


def cmd_new(a) -> int:
    exp_dir = experience_dir(a.scope, a.project_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{a.date}-{a.slug}.md"
    path = exp_dir / filename
    if path.exists():
        sys.exit(f"refusing to overwrite existing leaf: {path}")
    # Fragmentation guard: a judge decides whether an existing leaf is the same
    # difficulty; word overlap only nominates which leaves it is asked about.
    justify_new = getattr(a, "justify_new", None)
    leaves = []
    for leaf in sorted(exp_dir.glob("*.md")):
        if leaf.name == "MEMORY.md" or leaf.name == filename:
            continue
        text = leaf.read_text(encoding="utf-8")
        fm = FRONTMATTER.match(text)
        desc = ""
        if fm:
            dm = re.search(r"^description:\s*(.*)$", fm.group(1), re.MULTILINE)
            desc = dm.group(1).strip() if dm else ""
        diff_span = section_span(text, "Difficulty")
        diff_body = text[diff_span[0]:diff_span[1]] if diff_span else ""
        leaves.append((leaf, desc + " " + diff_body))
    result = semantic_join.judged_match(
        a.difficulty, leaves, lambda item: item[1], k=semantic_join.K_NEW_LEAF)
    if result.outcome == "match" and not justify_new:
        best_leaf = result.candidate[0]
        # The name stays where it is READ, the path goes where it is EXECUTED.
        # Resolved here rather than trusting the root: on --scope project the
        # root is caller-supplied via --project-dir and may be relative.
        sys.exit(
            f"refusing to fragment: a judge found analogous leaf {best_leaf.name!r} "
            f"to be the same difficulty. "
            f"Use `extend --leaf {best_leaf.resolve()}` to add a context, "
            f"or pass `--justify-new \"<reason>\"` for a genuinely distinct difficulty."
        )
    if result.outcome == "unjudged":
        cache = semantic_join.same_difficulty_cache()
        names = ", ".join(
            item[0].name for item in result.nominated
            if cache.get(a.difficulty, item[1]) is not False)
        print(f"note: no judged verdict against nominated leaf(s) {names}; "
              f"creating the leaf unchecked", file=sys.stderr)
    path.write_text(standalone_body(a), encoding="utf-8")
    edit_ledger.stamp(str(path), "record-experience:new", session=getattr(a, "session", None))
    update_subindex(exp_dir, a.date, a.title, filename, a.description,
                    session=getattr(a, "session", None))
    ledger_record("new", a, a.difficulty, [path])
    print(f"wrote {path}\nupdated {exp_dir / 'MEMORY.md'}")
    return 0


def cmd_extend(a) -> int:
    path = Path(a.leaf)
    if not path.exists():
        sys.exit(
            f"leaf not found: {path} — --leaf takes a path to the leaf file, "
            f"not a leaf name. Run `record-experience.py search <keywords>` to "
            f"print a runnable one.")
    text = path.read_text(encoding="utf-8")
    span = section_span(text, "Contexts")
    if not span:
        sys.exit("leaf has no `## Contexts` section — not a difficulty/v1 leaf")
    block = context_block(a.date, a.context_label, a.context_where, a.plan)
    text = text[:span[1]] + block + text[span[1]:]
    # recount contexts; scaffold synthesis once there are >=2
    new_span = section_span(text, "Contexts")
    n_ctx = len(re.findall(r"^###\s", text[new_span[0]:new_span[1]], re.MULTILINE))
    if n_ctx >= 2 and not section_span(text, "Common core & variations"):
        common = a.common or "TODO — shared solution across contexts"
        variations = a.variations or "TODO — what differs per context"
        synth = (
            "\n## Common core & variations\n"
            f"**Common:** {common}\n\n**Variations:** {variations}\n\n"
        )
        text = text[:new_span[1]] + synth + text[new_span[1]:]
    # Extending a leaf re-confirms it: bump last_verified to the extend date, and
    # backfill created if a legacy leaf predates the temporal-frontmatter contract.
    if FRONTMATTER.match(text):
        if not re.search(r"^created\s*:", FRONTMATTER.match(text).group(1), re.MULTILINE):
            text = set_fm_field(text, "created", a.date)
        text = set_fm_field(text, "last_verified", a.date)
    path.write_text(text, encoding="utf-8")
    edit_ledger.stamp(str(path), "record-experience:extend", session=getattr(a, "session", None))
    ledger_record("extend", a, f"{a.context_label} {a.plan}", [path])
    print(f"extended {path} (now {n_ctx} context(s))")
    if n_ctx >= 2 and (not a.common or not a.variations):
        print("→ fill `## Common core & variations` to distill the general solution")
    return 0


def cmd_set_last_verified(a) -> int:
    path = Path(a.leaf)
    if not path.exists():
        sys.exit(
            f"leaf not found: {path} — --leaf takes a path to the leaf file, "
            f"not a leaf name. Run `record-experience.py search <keywords>` to "
            f"print a runnable one.")
    text = path.read_text(encoding="utf-8")
    if not FRONTMATTER.match(text):
        sys.exit(f"leaf has no YAML frontmatter block: {path}")
    new_text = set_fm_field(text, "last_verified", a.date)
    path.write_text(new_text, encoding="utf-8")
    edit_ledger.stamp(str(path), "record-experience:set-last-verified",
                      session=getattr(a, "session", None))
    print(f"set last_verified: {a.date} on {path}")
    return 0


def cmd_ticket(a) -> int:
    exp_dir = experience_dir(a.scope, a.project_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{a.date}-{a.slug}.md"
    path = exp_dir / filename
    if path.exists():
        sys.exit(f"refusing to overwrite existing leaf: {path}")
    path.write_text(ticket_leaf_body(a), encoding="utf-8")
    edit_ledger.stamp(str(path), "record-experience:ticket", session=getattr(a, "session", None))
    update_subindex(exp_dir, a.date, a.title, filename, a.description,
                    session=getattr(a, "session", None))
    print(f"wrote thin leaf {path}\nupdated {exp_dir / 'MEMORY.md'}", file=sys.stderr)
    print("\n===== post this on the ticket =====", file=sys.stderr)
    print("NOTE: headers below are English (the LEAF stays English per repo policy).",
          file=sys.stderr)
    print("  Before posting, translate headers + prose to the TICKET's language,",
          file=sys.stderr)
    print("  and use BARE URLs — markdown [text](url) does NOT render in Tracker.\n",
          file=sys.stderr)
    print(ticket_comment(a))
    return 0


# --------------------------------------------------------------------------
# split flow: the curator writes a manifest (judgement), code applies and
# checks it (rule) — see skills/specializations/curator/SKILL.md
# --------------------------------------------------------------------------
CONTEXT_ID = re.compile(r"[0-9]+:[0-9a-f]{12}")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
REVISION = re.compile(r"[0-9A-Za-z._/~^@{}-]+")
CONTEXTS_HEADING = re.compile(r"## Contexts(\s.*)?")
SPLIT_MARKER = "Moved by memory-curator split on"
LEGACY_SPLIT_NOTE = "split by memory-curator"  # legacy name, as in "Moved by memory-curator split on"
SPLIT_MARKER_LINE = re.compile(rf"{SPLIT_MARKER} \d{{4}}-\d{{2}}-\d{{2}}:")
CLUSTER_TEXT_KEYS = ("slug", "name", "description", "functional_ground", "difficulty")
CLUSTER_OPTIONAL_KEYS = ("existing_regularity", "existing_principle", "no_principle_reason")
PROMOTED_KEYS = (("regularities", "regularity"), ("principles", "principle"))
CARRIED_KEYS = ("type", "schema", "generality", "tier")
LEVEL_LABELS = {1: "sibling-context rules", 2: "task-class rules", 3: "cross-domain invariants"}
SUBPROCESS_TIMEOUT_S = 120


def refuse(*problems: str):
    """Exit 2 with one line per problem; nothing has been written."""
    for problem in problems:
        print(f"record-experience: {problem}", file=sys.stderr)
    sys.exit(2)


class SourceContexts:
    """A leaf's `## Contexts` section cut into its `### ` blocks.

    Delimited exactly as self-diagnose's measure_leaf counts them: fence-aware, a
    `### ` line under `## Contexts` opens a block, the next `### ` or `## ` closes it."""

    __slots__ = ("head", "preamble", "blocks", "tail")

    def __init__(self, head: str, preamble: str, blocks: list[str], tail: str) -> None:
        self.head, self.preamble, self.blocks, self.tail = head, preamble, blocks, tail


def parse_contexts(text: str) -> SourceContexts:
    lines = text.splitlines(keepends=True)
    start = end = None
    block_starts: list[int] = []
    in_fence = in_contexts = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## "):
            if in_contexts:
                end, in_contexts = i, False
            if CONTEXTS_HEADING.fullmatch(line.strip()):
                if start is not None:
                    raise ValueError("more than one `## Contexts` section")
                start, in_contexts = i, True
        elif in_contexts and line.startswith("### "):
            block_starts.append(i)
    if start is None:
        raise ValueError("no `## Contexts` section")
    if end is None:
        end = len(lines)
    bounds = block_starts + [end]
    blocks = []
    for first, last in zip(bounds, bounds[1:]):
        chunk = lines[first:last]
        while chunk and not chunk[-1].strip():
            chunk.pop()
        block = "".join(chunk)
        blocks.append(block if block.endswith("\n") else block + "\n")
    head = "".join(lines[:start + 1])
    return SourceContexts(
        head if head.endswith("\n") else head + "\n",
        "".join(lines[start + 1:bounds[0]]), blocks, "".join(lines[end:]))


def context_ids(blocks: list[str]) -> list[str]:
    """`<1-based ordinal>:<first 12 hex of sha256 of the block's bytes>` per block."""
    return [f"{n}:{hashlib.sha256(b.encode('utf-8')).hexdigest()[:12]}"
            for n, b in enumerate(blocks, 1)]


def block_sha(block: str) -> str:
    return hashlib.sha256(block.encode("utf-8")).hexdigest()[:12]


def read_text_exact(path: Path) -> str:
    """Decode without newline translation, so block bytes match `git show` bytes."""
    return path.read_bytes().decode("utf-8")


def write_text_exact(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def one_line(text: str) -> str:
    return " ".join(text.split())


class Snapshot:
    """Remembers files before a multi-file write so a failure leaves none of it behind."""

    def __init__(self, paths) -> None:
        self.saved = {p: (p.read_bytes() if p.exists() else None) for p in paths}

    def restore(self) -> None:
        for path, data in self.saved.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)


def refuse_canon(root: Path, command: str) -> None:
    """Exit 2 when `root` lies in a checkout worktree_route would auto-land from.

    The write subcommands of the split flow produce a PROPOSAL the user accepts; a
    write in the canonical checkout would be the accepted state with no acceptance."""
    probe = root
    while not probe.is_dir() and probe != probe.parent:
        probe = probe.parent
    top = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S)
    if top.returncode != 0:
        return
    repo = Path(top.stdout.strip())
    if worktree_route.should_route(repo):
        refuse(f"{command} refuses to write under {root}: it lies in the canonical "
               f"checkout {repo}; run it from a linked worktree, where the uncommitted "
               f"diff is the proposal")


def read_manifest(path: str) -> tuple[dict, Path]:
    manifest_path = Path(path).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        refuse(f"cannot read manifest {path}: {exc}")
    if not isinstance(manifest, dict):
        refuse(f"manifest {path} must be a JSON object")
    return manifest, manifest_path


def manifest_root(manifest: dict, manifest_path: Path) -> Path:
    """The memory root; a relative one resolves against the manifest's own directory."""
    declared = manifest.get("root")
    if not isinstance(declared, str) or not declared:
        refuse("manifest 'root' must be a non-empty string")
    return (manifest_path.parent / declared).resolve()


def _id_list(value) -> bool:
    return isinstance(value, list) and all(
        isinstance(v, str) and CONTEXT_ID.fullmatch(v) for v in value)


def manifest_problems(manifest: dict) -> list[str]:
    out: list[str] = []
    if not isinstance(manifest.get("source"), str) or not manifest["source"]:
        out.append("manifest 'source' must be a non-empty string")
    base_rev, base_path = manifest.get("base_rev"), manifest.get("base_path")
    if not (isinstance(base_rev, str) and base_rev) and not (isinstance(base_path, str) and base_path):
        out.append("manifest needs 'base_rev' (a commit) or 'base_path' (a file of base bytes)")
    if isinstance(base_rev, str) and base_rev and (
            base_rev.startswith("-") or not REVISION.fullmatch(base_rev)):
        out.append(f"manifest 'base_rev' {base_rev!r} is not a plain revision")
    if not _id_list(manifest.get("keep")):
        out.append("manifest 'keep' must be a list of context ids (<ordinal>:<12 hex>)")
    clusters = manifest.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        out.append("manifest 'clusters' must be a non-empty list")
        clusters = []
    seen: set[str] = set()
    for i, cluster in enumerate(clusters):
        where = f"clusters[{i}]"
        if not isinstance(cluster, dict):
            out.append(f"{where} must be an object")
            continue
        for key in CLUSTER_TEXT_KEYS:
            if not isinstance(cluster.get(key), str) or not cluster[key].strip():
                out.append(f"{where} '{key}' must be a non-empty string")
        slug = cluster.get("slug")
        if isinstance(slug, str) and slug:
            if not SLUG.fullmatch(slug):
                out.append(f"{where} slug {slug!r} must match [a-z0-9][a-z0-9-]*")
            if slug in seen:
                out.append(f"{where} slug {slug!r} is used twice")
            seen.add(slug)
        difficulty = cluster.get("difficulty")
        if isinstance(difficulty, str) and re.search(r"^#{1,6} ", difficulty, re.MULTILINE):
            out.append(f"{where} 'difficulty' must not contain markdown headings")
        contexts = cluster.get("contexts")
        if not _id_list(contexts) or not contexts:
            out.append(f"{where} 'contexts' must be a non-empty list of context ids")
        for key in CLUSTER_OPTIONAL_KEYS:
            if key in cluster and not isinstance(cluster[key], str):
                out.append(f"{where} '{key}' must be a string")
        if cluster.get("existing_regularity") and cluster.get("existing_principle") not in (
                None, cluster["existing_regularity"]):
            out.append(f"{where} 'existing_regularity' and 'existing_principle' disagree")
    for key, noun in PROMOTED_KEYS:
        listed = manifest.get(key, [])
        if not isinstance(listed, list) or not all(
                isinstance(p, str) and SLUG.fullmatch(p) for p in listed):
            out.append(f"manifest '{key}' must be a list of {noun} slugs")
    out += rewrite_shape_problems(manifest.get("rewrites", []))
    return out


def rewrite_shape_problems(rewrites) -> list[str]:
    if not isinstance(rewrites, list):
        return ["manifest 'rewrites' must be a list of {contexts, replace} objects"]
    out: list[str] = []
    for i, entry in enumerate(rewrites):
        where = f"rewrites[{i}]"
        if not isinstance(entry, dict):
            out.append(f"{where} must be an object")
            continue
        if not _id_list(entry.get("contexts")) or not entry["contexts"]:
            out.append(f"{where} 'contexts' must be a non-empty list of context ids")
        replace = entry.get("replace")
        if not isinstance(replace, list) or not replace:
            out.append(f"{where} 'replace' must be a non-empty list of {{from, to}} objects")
            continue
        for j, pair in enumerate(replace):
            if not (isinstance(pair, dict) and isinstance(pair.get("from"), str)
                    and isinstance(pair.get("to"), str)):
                out.append(f"{where} replace[{j}] must be an object with string 'from' and 'to'")
    return out


def rewritten_blocks(manifest: dict, blocks: list[str], ids: list[str]) -> tuple[list[str], list[str]]:
    """Base blocks with the manifest's declared replacements applied, plus problems.

    Ids always name the ORIGINAL blocks, so the manifest keeps one id space."""
    by_id = dict(zip(ids, blocks))
    problems: list[str] = []
    for entry in manifest.get("rewrites", []):
        for context_id in entry["contexts"]:
            if context_id not in by_id:
                problems.append(f"rewrite: {context_id} is not in the source")
                continue
            for pair in entry["replace"]:
                old, new = pair["from"], pair["to"]
                if not old or old == new:
                    problems.append(f"rewrite: {context_id} replacement {old!r} -> {new!r} "
                                    f"must have a non-empty 'from' that differs from 'to'")
                elif old not in by_id[context_id]:
                    problems.append(f"rewrite: {context_id} text {old!r} does not occur "
                                    f"in the context")
                else:
                    by_id[context_id] = by_id[context_id].replace(old, new)
    return [by_id[i] for i in ids], problems


def assignment_problems(manifest: dict, ids: list[str]) -> list[str]:
    """Every id kept or clustered exactly once, none unknown."""
    known, owner, out = set(ids), {}, []

    def claim(context_id: str, where: str) -> None:
        if context_id not in known:
            out.append(f"{where}: context {context_id} is not in the source")
        elif context_id in owner:
            out.append(f"{where}: context {context_id} is already assigned to {owner[context_id]}")
        else:
            owner[context_id] = where

    for context_id in manifest["keep"]:
        claim(context_id, "keep")
    for cluster in manifest["clusters"]:
        for context_id in cluster["contexts"]:
            claim(context_id, f"cluster {cluster['slug']}")
    out += [f"context {i} is neither kept nor clustered" for i in ids if i not in owner]
    return out


def frontmatter_values(text: str) -> dict[str, str]:
    """Top-level single-line `key: value` pairs of the frontmatter, values verbatim."""
    m = FRONTMATTER.match(text)
    values: dict[str, str] = {}
    for line in (m.group(1).splitlines() if m else []):
        key, sep, value = line.partition(":")
        if sep and key and not key[0].isspace() and key.strip() not in values:
            values[key.strip()] = value.strip()
    return values


def note_split_in_description(head: str, date: str) -> str:
    """Append a short note to a single-line `description:`; anything else is left alone."""
    m = FRONTMATTER.match(head)
    if not m:
        return head
    note = f" (contexts split by curator on {date})"
    fm_lines = m.group(1).split("\n")
    for i, line in enumerate(fm_lines):
        if not line.startswith("description:"):
            continue
        value = line[len("description:"):].strip()
        continuation = i + 1 < len(fm_lines) and fm_lines[i + 1][:1] in (" ", "\t")
        if not value or value[0] in ">|" or continuation or LEGACY_SPLIT_NOTE in value or "split by curator" in value:
            return head
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[:-1] + note + value[-1]
        else:
            value += note
        fm_lines[i] = f"description: {value}"
        return head[:m.start(1)] + "\n".join(fm_lines) + head[m.end(1):]
    return head


def pointer_bullet(target: Path, source: Path, label: str, suffix: str = "") -> str:
    return f"- {label}({os.path.relpath(target, source.parent)}){suffix}"


def promoted_leaves(manifest: dict, root: Path) -> list[tuple[str, str, Path]]:
    """(kind, slug, path) of every regularity and principle the manifest lists."""
    return [(noun, slug, root / f"leaves/{key}/{slug}.md")
            for key, noun in PROMOTED_KEYS for slug in manifest.get(key, [])]


def split_pointer_list(source: Path, root: Path, clusters: list[dict],
                       promoted: list[tuple[str, str, Path]], date: str) -> str:
    experience = root / "leaves/experience"
    lines = [f"{SPLIT_MARKER} {date}:", ""]
    for cluster in clusters:
        n = len(cluster["contexts"])
        lines.append(pointer_bullet(
            experience / f"{cluster['slug']}.md", source, f"[{one_line(cluster['name'])}]",
            f" — {n} context{'s' if n != 1 else ''}"))
    for noun, slug, path in promoted:
        lines.append(pointer_bullet(path, source, f"{noun}: [{slug}]"))
    return "\n".join(lines) + "\n"


def rewritten_source(parsed: SourceContexts, kept: list[str], pointers: str, date: str) -> str:
    out = note_split_in_description(parsed.head, date) + "\n" + pointers
    if parsed.preamble.strip():
        out += "\n" + parsed.preamble.strip("\n") + "\n"
    out += "".join("\n" + block for block in kept)
    if parsed.tail:
        out += "\n" + parsed.tail
    return out


def add_pointer_to_split_list(text: str, bullet: str) -> str | None:
    """Add `bullet` to the source's 'Moved by ...' list; None when the source is unsplit."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not SPLIT_MARKER_LINE.fullmatch(line.rstrip("\r\n")):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        last = i
        while j < len(lines) and lines[j].startswith("- "):
            last, j = j, j + 1
        if any(l.rstrip("\r\n") == bullet for l in lines):
            return text
        lines.insert(last + 1, bullet + "\n")
        return "".join(lines)
    return None


def cluster_leaf_text(cluster: dict, blocks: list[str], carried: dict[str, str],
                      source: Path, leaf: Path, date: str) -> str:
    fm = ["---", f"name: {cluster['slug']}", f"description: {one_line(cluster['description'])}"]
    fm += [f"{key}: {carried[key]}" for key in CARRIED_KEYS if key in carried]
    fm += birth_lines()
    fm += [f"resolution_confirmed_by_user: {carried['resolution_confirmed_by_user']}",
           f"created: {date}", f"last_verified: {date}", "---", ""]
    origin = os.path.relpath(source, leaf.parent)
    return (
        "\n".join(fm)
        + f"\n# {one_line(cluster['name'])}\n"
        + f"\n## Difficulty\n{cluster['difficulty'].strip()}\n"
        + f"\n**Functional ground:** {one_line(cluster['functional_ground'])}\n"
        + "\n## Order & criterion\n"
        + f"Split out of [{source.stem}]({origin}) by curator split on {date}: "
        + "the contexts below arose under that leaf's difficulty and cluster here by the "
        + "functional ground named above. The order and acceptance check of the original "
        + "stay in the source leaf.\n"
        + "\n## Contexts\n\n" + "\n".join(blocks)
        + "\n## Cost\n"
        + f"Not costed separately: the contexts keep the cost recorded in [{source.stem}]({origin}).\n"
    )


def cmd_split_contexts(a) -> int:
    source = Path(a.source)
    if not source.is_file():
        refuse(f"source not found: {source}")
    try:
        parsed = parse_contexts(read_text_exact(source))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        refuse(f"{source}: {exc}")
    for context_id, block in zip(context_ids(parsed.blocks), parsed.blocks):
        print(f"{context_id} {block.split(chr(10), 1)[0][4:].strip()}")
    return 0


def existing_regularity_of(cluster: dict) -> str:
    """The regularity a cluster names as already stating its ground; `existing_principle`
    is the key's deprecated spelling, from when the induction target was a principle."""
    return cluster.get("existing_regularity") or cluster.get("existing_principle") or ""


def cluster_regularity_links(leaves_dir: Path, clusters: list[dict], leaves: dict[str, Path],
                             writes: dict[Path, str]) -> dict[Path, str]:
    """Both sides of the link from each cluster leaf to the regularity it names."""
    corpus = pp.load_corpus(leaves_dir).apply(
        pp.Change(path, text, "leaf") for path, text in writes.items())
    out: dict[Path, str] = {}
    for cluster in clusters:
        regularity = pp.target_of(existing_regularity_of(cluster), corpus)
        if regularity is None:
            continue
        changes = pp.link_changes(corpus.find(cluster["slug"]), regularity, corpus)
        corpus = corpus.apply(changes)
        out.update({ch.path: ch.new_text for ch in changes})
    return out


def cmd_split_apply(a) -> int:
    manifest, manifest_path = read_manifest(a.manifest)
    root = manifest_root(manifest, manifest_path)
    refuse_canon(root, "split-apply")
    problems = manifest_problems(manifest)
    if problems:
        refuse(*problems)
    source = (root / manifest["source"]).resolve()
    try:
        source.relative_to(root)
        text = read_text_exact(source)
        parsed = parse_contexts(text)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        refuse(f"source {manifest['source']}: {exc}")
    ids = context_ids(parsed.blocks)
    problems = assignment_problems(manifest, ids)
    moved_blocks, rewrite_problems = rewritten_blocks(manifest, parsed.blocks, ids)
    problems += rewrite_problems
    experience = root / "leaves/experience"
    leaves = {c["slug"]: experience / f"{c['slug']}.md" for c in manifest["clusters"]}
    problems += [f"cluster leaf already exists: {p}" for p in leaves.values()
                 if p.exists() or p.resolve() == source]
    promoted = promoted_leaves(manifest, root)
    problems += [f"{noun} {slug} is listed but {path} does not exist"
                 for noun, slug, path in promoted if not path.is_file()]
    problems += [f"cluster {c['slug']}: existing_regularity {existing_regularity_of(c)} is not "
                 f"a regularity leaf" for c in manifest["clusters"] if existing_regularity_of(c)
                 and not (root / f"leaves/regularities/{existing_regularity_of(c)}.md").is_file()]
    if any("existing_principle" in c for c in manifest["clusters"]):
        print("record-experience: manifest key existing_principle is deprecated; "
              "use existing_regularity", file=sys.stderr)
    carried = frontmatter_values(text)
    if not carried.get("resolution_confirmed_by_user"):
        problems.append("source has no resolution_confirmed_by_user to carry over")
    carried.setdefault("type", "reference")
    carried.setdefault("schema", "difficulty/v1")
    if problems:
        refuse(*problems)

    pointers = split_pointer_list(source, root, manifest["clusters"], promoted, a.date)
    # blocks keep the source's own order, not the order the manifest lists them in
    kept = set(manifest["keep"])
    writes = {source: rewritten_source(
        parsed, [b for i, b in zip(ids, moved_blocks) if i in kept], pointers, a.date)}
    for cluster in manifest["clusters"]:
        wanted = set(cluster["contexts"])
        blocks = [b for i, b in zip(ids, moved_blocks) if i in wanted]
        leaf = leaves[cluster["slug"]]
        writes[leaf] = cluster_leaf_text(cluster, blocks, carried, source, leaf, a.date)
    writes.update(cluster_regularity_links(root / "leaves", manifest["clusters"], leaves, writes))

    index = experience / "MEMORY.md"
    snapshot = Snapshot([index, *writes])
    try:
        for path, content in writes.items():
            write_text_exact(path, content)
        for cluster in manifest["clusters"]:
            update_subindex(experience, a.date, one_line(cluster["name"]),
                            f"{cluster['slug']}.md", one_line(cluster["description"]),
                            session=getattr(a, "session", None))
    except Exception as exc:
        snapshot.restore()
        sys.exit(f"split-apply failed, nothing written: {exc}")
    for path in writes:
        edit_ledger.stamp(str(path), "record-experience:split-apply",
                          session=getattr(a, "session", None))
    print(f"split {source.name}: {len(manifest['clusters'])} cluster leaf/leaves written, "
          f"{len(kept)} context(s) kept, indexed in {index}")
    return 0


def promote_skeleton(a, area: str, induced: list[str], grounded: list[str]) -> tuple[str, str]:
    """The provenance frontmatter line and the section skeleton of a promoted leaf:
    a grounded principle or an induced regularity (either tier)."""
    statement = a.statement.strip()
    refutation = ("\n## Refutation\n\nTODO (curator): the observation that would refute this "
                  f"{area} or force a broader form.\n")
    if a.principle:
        link_line = f"grounded_in: [{', '.join(pp.REGULARITY_PREFIX + s for s in grounded)}]\n"
        body = (
            f"## Principle\n\n{statement}\n\n"
            "## Goal class\n\nTODO (curator): name the goal this norm serves.\n\n"
            "## Generality\n\n"
            f"Level {a.generality} — TODO (curator): name the class of tasks this norm ranges "
            "over and what it is deliberately not claimed for.\n\n"
            "## Grounded in\n\n"
            + "".join(f"- [[{s}]] — TODO (curator): what this regularity contributes.\n"
                      for s in grounded)
            + refutation
        )
        return link_line, body
    heading = "Regularity" if area == "regularity" else "Principle"
    order_derived = getattr(a, "order_derived", None)
    induced_part = ("## Induced from\n\n"
                    + "".join(f"- [[{s}]] — TODO (curator): what this source contributes.\n"
                             for s in induced)) if induced or not order_derived else ""
    order_part = (("\n" if induced else "")
                  + pp.render_order_section(a.order_quote, a.date)) if order_derived else ""
    body = (
        f"## {heading}\n\n{statement}\n\n"
        "## Generality\n\n"
        f"Level {a.generality} — TODO (curator): name the class of tasks this statement "
        "ranges over and what it is deliberately not claimed for.\n\n"
        + induced_part
        + order_part
        + refutation
        + ("" if area == "regularity" else
           "\n## See also\n\n"
           "- TODO (curator): sibling principles and the leaves that apply this one.\n")
    )
    return f"induced_from: [{', '.join(induced)}]\n", body


def promote_problems(a, corpus, leaves: Path, project: Path | None,
                     induced: list[str], grounded: list[str]) -> list[str]:
    """Why the requested leaf cannot be written: a principle is grounded in existing
    regularities, a regularity is induced from existing leaves."""
    problems = []
    for flag, value in (("--order-derived", a.order_derived), ("--introduced-by", a.introduced_by)):
        if value is not None and not TASK_ID.fullmatch(value):
            problems.append(f"{flag} must be a task id: one token of letters, digits, '.', '_', '-'")
    if a.order_derived:
        area = "principle" if a.principle else "regularity"
        if not pp.order_kind_ok(area):
            problems.append("--order-derived belongs to a regularity: order provenance yields "
                            f"knowledge, not a {area}")
        quote = a.order_quote or ""
        if pp.order_quote(pp.render_order_section(quote, a.date)) is None:
            problems.append("--order-derived needs a non-empty --order-quote: the order's "
                            "factual statement, verbatim")
        if any(line.startswith("##") for line in quote.splitlines()):
            problems.append("--order-quote must not contain a heading line (## ...)")
    elif a.order_quote is not None:
        problems.append("--order-quote goes with --order-derived")
    if a.principle:
        if induced or a.rule_derived:
            problems.append("a principle names the regularities it rests on with "
                            "--grounded-in; --induced-from and --rule-derived belong to a "
                            "regularity")
        if not grounded:
            problems.append("--principle needs --grounded-in REG[,REG]: the existing "
                            "regularities the principle rests on")
        known = {r.slug for t in pp.fallback_tiers("project" if project else "global")
                 for r in corpus.of("regularity", t)}
        global_dir = pp.promote_target_dir(leaves)
        searched = f"{pp.promote_target_dir(leaves, project)} or {global_dir}" if project else str(global_dir)
        problems += [f"grounded-in regularity {slug!r} not found under {searched}"
                     for slug in grounded if slug not in known]
        return problems
    tier = "project" if project else "global"
    kinds = []
    if grounded:
        problems.append("--grounded-in belongs to --principle")
    if not induced and not a.order_derived:
        problems.append("promote needs --induced-from SLUG (repeatable), --order-derived TASK "
                        "--order-quote TEXT, or --principle --grounded-in REG[,REG] for a "
                        "principle")
    for slug in induced:
        found = corpus.find(slug, tier) if SLUG.fullmatch(slug) else None
        if found is None or (project and found.tier != "project"):
            problems.append(f"induced-from leaf {slug!r} not found under {project or leaves}")
        else:
            kinds.append(pp.classify(slug, corpus, tier))
    if ((induced or a.order_derived) and not problems
            and not pp.promote_sources_ok(kinds, a.rule_derived, a.order_derived)):
        problems.append("no induced-from leaf is an experience leaf or a case; name one, "
                        "or give --rule-derived REASON for a regularity written from rules")
    if a.rule_derived and any(k in pp.EXPERIENCE_SOURCE_KINDS for k in kinds):
        problems.append("--rule-derived is for a regularity with no experience source, "
                        "but an induced-from leaf is an experience leaf or a case")
    return problems


def cmd_promote(a) -> int:
    manifest = manifest_path = None
    if a.manifest:
        manifest, manifest_path = read_manifest(a.manifest)
    if a.root:
        root = Path(a.root).resolve()
    elif manifest is not None:
        root = manifest_root(manifest, manifest_path)
    else:
        root = (REPO_ROOT / "memory-global").resolve()
    refuse_canon(root, "promote")
    problems = []
    if not SLUG.fullmatch(a.slug):
        problems.append(f"slug {a.slug!r} must match [a-z0-9][a-z0-9-]*")
    if manifest is not None:
        problems += manifest_problems(manifest)
    leaves = root / "leaves"
    project = pp.project_memory(a.project_dir)
    tier = "project" if project else "global"
    corpus = pp.load_corpus(leaves, project)
    induced = a.induced_from or []
    grounded = [s for item in (a.grounded_in or []) for s in item.split(",") if s]
    problems += promote_problems(a, corpus, leaves, project, induced, grounded)
    area = "principle" if a.principle else "regularity"
    dest_dir = pp.promote_target_dir(leaves, project, area)
    path = dest_dir / f"{a.slug}.md"
    if path.exists():
        problems.append(f"{area} already exists: {path}")
    if problems:
        refuse(*problems)

    source_links = grounded if a.principle else induced
    schema = "principle/v1" if area == "principle" else "regularity/v1"
    link_line, body = promote_skeleton(a, area, induced, grounded)
    stamp = pp.order_introduced_by(a.order_derived, a.introduced_by)
    text = (
        "---\n"
        f"name: {a.slug}\n"
        f"description: {one_line(a.description)}\n"
        "type: reference\n"
        f"schema: {schema}\n"
        f"generality: {a.generality}\n"
        + link_line
        + (f"{pp.RULE_DERIVED}: {json.dumps(one_line(a.rule_derived))}\n"
           if a.rule_derived else "")
        + (f"{pp.ORDER_DERIVED}: {one_line(a.order_derived)}\n" if a.order_derived else "")
        + (f"{pp.INTRODUCED_BY}: {one_line(stamp)}\n" if stamp else "")
        + (f"domain: {a.domain}\n" if a.domain else "")
        + f"created: {a.date}\n"
        f"last_verified: {a.date}\n"
        "---\n\n"
        f"# {one_line(a.name)}\n\n"
        + body
    )
    index = dest_dir / "MEMORY.md"
    pointer = (f"- [{one_line(a.name)}]({a.slug}.md) — {one_line(a.description)} "
               f"← {', '.join(f'`{s}`' for s in source_links)}.\n")
    title, blurb = (("Regularities", "Refutable claims about how things are, induced from "
                     "experience and grouped by generality level (schema: regularity/v1).")
                    if area == "regularity" else
                    ("Principles", "Generality-graded, provenance-rooted, refutable principles "
                     "(schema: ../principle-leaf-schema.md)."))
    index_text = read_text_exact(index) if index.exists() else f"# {title}\n\n{blurb}\n"
    heading = re.search(rf"^## Level {a.generality}\b.*$", index_text, re.MULTILINE)
    if heading:
        nxt = H2.search(index_text, heading.end())
        end = nxt.start() if nxt else len(index_text)
        listed_pointers = index_text[heading.end():end].rstrip("\n")
        index_text = (index_text[:heading.end()] + (listed_pointers or "\n") + "\n" + pointer
                      + ("\n" if nxt else "") + index_text[end:])
    else:
        index_text = (index_text.rstrip("\n") + f"\n\n## Level {a.generality} — "
                      f"{LEVEL_LABELS[a.generality]}\n\n{pointer}")

    writes = {path: text, index: index_text}
    promoted = pp.Leaf(a.slug, path, tier, area, text)
    for slug in induced:
        source = corpus.find(slug, tier)
        if source.area in pp.EXPERIENCE_SOURCE_KINDS:
            writes.update({ch.path: ch.new_text for ch in pp.link_changes(source, promoted, corpus)
                           if ch.role == "leaf"})
    if manifest is not None:
        manifest_key = "regularities" if area == "regularity" else "principles"
        listed = list(manifest.get(manifest_key, []))
        if a.slug not in listed:
            listed.append(a.slug)
        manifest[manifest_key] = listed
        writes[manifest_path] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        source = (root / manifest["source"]).resolve()
        if source.is_file():
            bullet = pointer_bullet(path, source, f"{area}: [{a.slug}]")
            updated = add_pointer_to_split_list(read_text_exact(source), bullet)
            if updated is not None:
                writes[source] = updated
    snapshot = Snapshot(writes)
    try:
        for target, content in writes.items():
            write_text_exact(target, content)
    except Exception as exc:
        snapshot.restore()
        sys.exit(f"promote failed, nothing written: {exc}")
    for target in writes:
        edit_ledger.stamp(str(target), "record-experience:promote",
                          session=getattr(a, "session", None))
    print(f"wrote {path}\nupdated {index}")
    return 0


# --------------------------------------------------------------------------
# provenance links (lib/principle_provenance.py)
# --------------------------------------------------------------------------
def memory_root(a) -> Path:
    return Path(a.root).resolve() if a.root else (REPO_ROOT / "memory-global").resolve()


def provenance_tier(a) -> str:
    return "project" if a.project_dir else "global"


def provenance_corpus(a) -> pp.Corpus:
    return pp.load_corpus(memory_root(a) / "leaves", pp.project_memory(a.project_dir))


def commit_changes(a, changes, command: str) -> None:
    """Write every change or none; refuses the canonical checkout like the split flow."""
    by_path = {Path(ch.path): ch for ch in changes}
    root = memory_root(a)
    if any(path.is_relative_to(root) for path in by_path):
        refuse_canon(root, command)
    try:
        pp.write_all(by_path.values())
    except OSError as exc:
        sys.exit(f"{command} failed, nothing written: {exc}")
    for path in by_path:
        edit_ledger.stamp(str(path), f"record-experience:{command}",
                          session=getattr(a, "session", None))
        print(f"updated {path}")


def link_pairs(a) -> list[tuple[str, str]]:
    if a.pairs:
        lines = Path(a.pairs).read_text(encoding="utf-8").splitlines()
        pairs = [tuple(line.split("\t")) for line in lines if line.strip()]
        bad = [p for p in pairs if len(p) != 2]
        if bad:
            refuse(*(f"--pairs line is not 'source<TAB>target': {chr(9).join(p)!r}" for p in bad))
        return pairs
    if not (a.source and a.target):
        refuse("give SOURCE TARGET or --pairs FILE")
    return [(a.source, a.target)]


def resolve_pair(corpus: pp.Corpus, tier: str, source_slug: str, target_slug: str):
    source = corpus.find(source_slug, tier)
    if source is None or source.area not in pp.EXPERIENCE_SOURCE_KINDS:
        refuse(f"{source_slug!r} is not an experience leaf or a case")
    target = pp.target_of(target_slug, corpus, source.tier)
    if target is None:
        refuse(f"{target_slug!r} is not a regularity (a project regularity or case for a "
               "project leaf) this leaf may name")
    return source, target


def cmd_link(a) -> int:
    corpus = provenance_corpus(a)
    tier = provenance_tier(a)
    changes = []
    for source_slug, target_slug in link_pairs(a):
        source, target = resolve_pair(corpus, tier, source_slug, target_slug)
        if not pp.target_allowed(source.tier, target):
            refuse(f"project leaf {source_slug} may not name global {target.area} {target_slug}; "
                   "write a case with case-add and link the leaf to the case")
        step = pp.link_changes(source, target, corpus)
        corpus = corpus.apply(step)
        changes += step
    commit_changes(a, changes, "link")
    return 0


def cmd_unlink(a) -> int:
    corpus = provenance_corpus(a)
    tier = provenance_tier(a)
    changes = []
    for source_slug, target_slug in link_pairs(a):
        source, target = resolve_pair(corpus, tier, source_slug, target_slug)
        step = pp.unlink_changes(source, target, corpus)
        corpus = corpus.apply(step)
        changes += step
    commit_changes(a, changes, "unlink")
    return 0


CASE_SPEC_KEYS = ("slug", "description", "source", "into", "aspect", "expected", "actual",
                  "mismatch")


def case_text(spec: dict, digest: str, date: str) -> str:
    sections = "".join(f"\n## {name}\n\n{spec[name.lower()].strip()}\n"
                       for name in pp.CASE_SECTIONS)
    return (
        "---\n"
        f"name: {spec['slug']}\n"
        f"description: {one_line(spec['description'])}\n"
        "type: reference\n"
        f"schema: {pp.CASE_SCHEMA}\n"
        f"{pp.ORIGIN_TIER_FIELD}: {pp.ORIGIN_TIER}\n"
        f"{pp.SOURCE_DIGEST}: {digest}\n"
        f"{pp.FIELD}: []\n"
        f"created: {date}\n"
        f"last_verified: {date}\n"
        "---\n\n"
        f"# Case: {spec['slug']}\n"
        + sections
    )


def cmd_case_add(a) -> int:
    try:
        specs = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        refuse(f"cannot read --spec {a.spec}: {exc}")
    if not isinstance(specs, list):
        refuse("--spec must hold a JSON list of case objects")
    leaves = memory_root(a) / "leaves"
    cases_dir = leaves / pp.CASES_DIR
    problems, changes = [], []
    for spec in specs:
        missing = [k for k in CASE_SPEC_KEYS if not isinstance(spec, dict) or k not in spec]
        if missing:
            problems.append(f"case spec lacks {', '.join(missing)}")
    if problems:
        refuse(*problems)
    explicit = pp.project_memory(a.project_dir)
    projects = {explicit or Path(s["source"]).resolve().parent.parent for s in specs}
    if len(projects) > 1:
        refuse("case specs name leaves of more than one project: "
               + ", ".join(sorted(str(p) for p in projects)) + "; run case-add once per project")
    project = projects.pop() if projects else None
    if project is not None and (project == leaves.resolve() or leaves.resolve() in project.parents):
        project = None  # a source under the global leaves is not a project leaf
    corpus = pp.load_corpus(leaves, project)
    project_leaves = {l.path: l for l in corpus.of("experience", "project")}
    for spec in specs:
        slug, source = spec["slug"], Path(spec["source"]).resolve()
        if not SLUG.fullmatch(slug):
            problems.append(f"case slug {slug!r} must match [a-z0-9][a-z0-9-]*")
            continue
        if source not in project_leaves:
            problems.append(f"case {slug}: source {source} is not an experience leaf of the project")
            continue
        if not isinstance(spec["into"], list) or not spec["into"]:
            problems.append(f"case {slug}: `into` names no regularity")
            continue
        source_text = project_leaves[source].text
        path = cases_dir / f"{slug}.md"
        text = case_text(spec, pp.body_digest(source_text), a.date)
        identifiers = pp.project_identifiers(corpus) + [
            i for i in (str(source), source.stem) if len(i) >= pp.MIN_IDENTIFIER_LEN]
        if path.exists():
            problems.append(f"case already exists: {path}")
        error = pp.case_section_error(text)
        if error:
            problems.append(f"case {slug}: {error}")
        leaked = pp.leaks(text, identifiers)
        if leaked:
            problems.append(f"case {slug} carries project identifier(s): {', '.join(leaked)}")
        targets = [pp.target_of(p, corpus, "global") for p in spec["into"]]
        problems += [f"case {slug}: unknown regularity {p!r}"
                     for p, t in zip(spec["into"], targets) if t is None]
        if problems:
            continue
        step = [pp.Change(path, text, "case")]
        corpus = corpus.apply(step)
        for target in targets:
            link = pp.link_changes(corpus.find(slug), target, corpus)
            corpus = corpus.apply(link)
            step += link
        changes += step
    if problems:
        refuse(*problems)
    commit_changes(a, changes, "case-add")
    return 0


def cmd_backfill(a) -> int:
    marks = {}
    for pair in a.rule_derived or []:
        slug, sep, reason = pair.partition("=")
        if not sep or not reason.strip():
            refuse(f"--rule-derived wants SLUG=REASON, got {pair!r}")
        marks[slug] = json.dumps(one_line(reason))
    try:
        changes = pp.backfill_status(provenance_corpus(a), marks)
    except ValueError as exc:
        refuse(str(exc))
    commit_changes(a, changes, "backfill")
    print(f"backfill: {len(changes)} file(s) changed")
    return 0


def cmd_provenance(a) -> int:
    corpus = provenance_corpus(a)
    if a.of:
        subject = corpus.find(a.of, provenance_tier(a))
        if subject is None or subject.area not in ("regularity", "principle"):
            refuse(f"{a.of!r} is not a regularity or a principle")
        for leaf in pp.experiences_of(subject, corpus):
            print(f"{leaf.tier}\t{leaf.slug}\t{leaf.path}")
    if a.of and not a.check:
        return 0
    findings = pp.unaccounted(corpus)
    if a.check:
        for f in findings:
            print(f"{f.kind}\t{f.path}\t{f.detail}")
        return 1 if findings else 0
    print(f"provenance: {len(findings)} unaccounted item(s); --check lists them")
    return 0


def read_base(manifest: dict, root: Path, source: Path) -> str:
    base_path = manifest.get("base_path")
    if isinstance(base_path, str) and base_path:
        try:
            return read_text_exact(root / base_path)
        except (OSError, UnicodeDecodeError) as exc:
            refuse(f"cannot read base_path {base_path}: {exc}")
    shown = subprocess.run(
        ["git", "-C", str(source.parent), "show", f"{manifest['base_rev']}:./{source.name}"],
        capture_output=True, timeout=SUBPROCESS_TIMEOUT_S)
    if shown.returncode != 0:
        refuse(f"cannot read {source.name} at {manifest['base_rev']}: "
               f"{shown.stderr.decode('utf-8', 'replace').strip()}")
    return shown.stdout.decode("utf-8")


def run_verifier(script: str, *args: str, env: dict | None = None) -> str | None:
    """None when the verifier passes, else a one-line account of what it said."""
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / script), *args],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S, env=env)
    if done.returncode == 0:
        return None
    said = [l.strip() for l in (done.stdout + done.stderr).splitlines() if l.strip()]
    return " | ".join(said[:5]) or f"exit {done.returncode}"


def memory_index_verdict(root: Path) -> str | None:
    """verify-memory-index.py wants <repo>/memory-global/leaves; present the root as that."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "memory-global").symlink_to(root, target_is_directory=True)
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(tmp).parent))
        return run_verifier("verify-memory-index.py", "--root", tmp, env=env)


def cmd_split_verify(a) -> int:
    manifest, manifest_path = read_manifest(a.manifest)
    root = manifest_root(manifest, manifest_path)
    problems = manifest_problems(manifest)
    if problems:
        refuse(*problems)
    source = (root / manifest["source"]).resolve()
    try:
        base = parse_contexts(read_base(manifest, root, source))
    except ValueError as exc:
        refuse(f"base of {manifest['source']}: {exc}")
    base_ids = context_ids(base.blocks)
    failures = [f"manifest: {p}" for p in assignment_problems(manifest, base_ids)]
    moved_blocks, rewrite_problems = rewritten_blocks(manifest, base.blocks, base_ids)
    failures += rewrite_problems
    expected_sha = {i: block_sha(b) for i, b in zip(base_ids, moved_blocks)}

    experience = root / "leaves/experience"
    expected = {source: list(manifest["keep"])}
    for cluster in manifest["clusters"]:
        expected[experience / f"{cluster['slug']}.md"] = list(cluster["contexts"])
    present: dict[Path, list[str]] = {}
    for leaf in expected:
        present[leaf] = []
        if not leaf.is_file():
            continue
        try:
            present[leaf] = [block_sha(b) for b in parse_contexts(read_text_exact(leaf)).blocks]
        except (ValueError, UnicodeDecodeError) as exc:
            failures.append(f"verifier: {leaf}: {exc}")

    base_by_sha: dict[str, list[str]] = {}
    for context_id in base_ids:
        base_by_sha.setdefault(expected_sha[context_id], []).append(context_id)
    found: dict[str, int] = {}
    for shas in present.values():
        for sha in shas:
            found[sha] = found.get(sha, 0) + 1
    for sha, ids in base_by_sha.items():
        failures += [f"missing: {i}" for i in ids[found.get(sha, 0):]]
        if found.get(sha, 0) > len(ids):
            failures.append(f"duplicate: {ids[0]}")
    for leaf, shas in present.items():
        failures += [f"unexpected: {n}:{sha} in {leaf}"
                     for n, sha in enumerate(shas, 1) if sha not in base_by_sha]

    if not failures:
        for leaf, ids in expected.items():
            allowed = [expected_sha.get(i, i.split(":")[1]) for i in ids]
            for n, sha in enumerate(present[leaf], 1):
                if sha in allowed:
                    allowed.remove(sha)
                else:
                    failures.append(f"misplaced: {n}:{sha} in {leaf}")

    checked = list(expected) + [path for _noun, _slug, path in promoted_leaves(manifest, root)]
    for leaf in checked:
        if not leaf.is_file():
            failures.append(f"verifier: {leaf}: file not found")
            continue
        for script in ("verify-experience-leaf.py", "verify-leaf-structure.py"):
            said = run_verifier(script, str(leaf))
            if said:
                failures.append(f"verifier: {leaf}: {said}")
    said = memory_index_verdict(root)
    if said:
        failures.append(f"verifier: {root}: {said}")

    for line in failures:
        print(line)
    if failures:
        print(f"split-verify: FAIL — {len(failures)} problem(s)")
        return 1
    print(f"split-verify: OK — {len(base_ids)} context(s) of {manifest['source']} conserved "
          f"across {len(expected)} leaf/leaves, verifiers green")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_scope(sp):
        sp.add_argument("--scope", choices=["global", "project"], default="global")
        sp.add_argument("--project-dir")
        sp.add_argument("--date", default=today())

    s = sub.add_parser("search")
    add_scope(s)
    s.add_argument(
        "--tier",
        choices=["experience", "regularities", "principles", "system-knowledge"],
        default="experience",
        help=(
            "which leaf tier to search: experience (default), the regularities "
            "(knowledge layer), the principles (norm layer), or system-knowledge "
            "(durable non-obvious facts, ranked on the same difficulty-led basis "
            "as experience)"
        ),
    )
    s.add_argument(
        "--domain",
        default=None,
        help=(
            "filter regularity or principle leaves by their domain: frontmatter tag; "
            "untagged leaves always match; a differently-tagged leaf is excluded. "
            "Only applied when --tier regularities or principles is set."
        ),
    )
    s.add_argument("--session", default=None,
                   help="session id the retrieval ledger joins this search to "
                        "(default: $CLAUDE_CODE_SESSION_ID)")
    s.add_argument("keywords")
    s.set_defaults(func=cmd_search)

    def add_content(sp, *, ticket=False):
        sp.add_argument("--slug", required=True)
        sp.add_argument("--title", required=True)
        sp.add_argument("--description", required=True)
        sp.add_argument("--confirmed-by", required=True, dest="confirmed_by")
        sp.add_argument("--difficulty", required=True)
        sp.add_argument("--order", required=True)
        sp.add_argument("--criterion", required=True)
        sp.add_argument("--context-where", required=True, dest="context_where")
        sp.add_argument("--plan", required=True)
        sp.add_argument("--refs", nargs="*", default=[])
        # Difficulty tier (ADR-0002): 0 = state-level (the default; omitted from
        # frontmatter, absence implies 0), 1 = principle-level — a tier-1
        # difficulty whose P0/P1 are principles, the fuel the sigma operator
        # would consume. The σ-sentinel (sigma-sentinel.py) reads this tag.
        sp.add_argument(
            "--tier", type=int, choices=[0, 1], default=None,
            help="difficulty tier: 0 state-level (default/omitted), 1 principle-level (sigma fuel)",
        )
        sp.add_argument("--session", default=None,
                        help="ledger session_id for the edit-ledger stamp of this write")

    n = sub.add_parser("new")
    add_scope(n)
    add_content(n)
    n.add_argument("--context-label", default="initial", dest="context_label")
    n.add_argument("--plan-file", dest="plan_file")
    n.add_argument("--cost")
    n.add_argument("--self-critique", dest="self_critique")
    n.add_argument(
        "--justify-new", dest="justify_new", metavar="REASON",
        help="override the fragmentation guard when this difficulty is genuinely distinct",
    )
    n.set_defaults(func=cmd_new)

    e = sub.add_parser("extend")
    add_scope(e)
    e.add_argument("--leaf", required=True)
    e.add_argument("--context-label", required=True, dest="context_label")
    e.add_argument("--context-where", required=True, dest="context_where")
    e.add_argument("--plan", required=True)
    e.add_argument("--common")
    e.add_argument("--variations")
    e.add_argument("--session", default=None,
                   help="ledger session_id for the edit-ledger stamp of this write")
    e.set_defaults(func=cmd_extend)

    slv = sub.add_parser("set-last-verified",
                         help="bump last_verified on an existing leaf (re-confirmation)")
    slv.add_argument("--leaf", required=True)
    slv.add_argument("--date", default=today())
    slv.add_argument("--session", default=None,
                     help="ledger session_id for the edit-ledger stamp of this write")
    slv.set_defaults(func=cmd_set_last_verified)

    t = sub.add_parser("ticket")
    add_scope(t)
    add_content(t, ticket=True)
    t.add_argument("--ticket", required=True)
    t.add_argument("--ticket-url", dest="ticket_url")
    t.add_argument("--context-label", default="initial", dest="context_label")
    t.add_argument("--distill")
    t.set_defaults(func=cmd_ticket)

    ps = sub.add_parser("promote-scan",
                        help="cluster experience leaves by functional ground and flag "
                             "regularity-induction candidates")
    add_scope(ps)
    ps.add_argument(
        "--threshold", type=int, default=None,
        help="override principle-promotion-threshold from config.md",
    )
    ps.add_argument(
        "--json", action="store_true", dest="json_out", default=False,
        help="emit clusters as JSON instead of human-readable text",
    )
    ps.set_defaults(func=cmd_promote_scan)

    def add_manifest(sp, *, required: bool = True):
        sp.add_argument("--manifest", required=required,
                        help="split manifest JSON written by the memory curator")

    sc = sub.add_parser("split-contexts",
                        help="list '<id> <heading>' per context block of a leaf (read-only)")
    sc.add_argument("--source", required=True)
    sc.set_defaults(func=cmd_split_contexts)

    sa = sub.add_parser("split-apply",
                        help="execute a split manifest: move contexts byte-exactly into "
                             "cluster leaves (refuses the canonical checkout)")
    add_manifest(sa)
    sa.add_argument("--date", default=today())
    sa.add_argument("--session", default=None,
                    help="ledger session_id for the edit-ledger stamp of this write")
    sa.set_defaults(func=cmd_split_apply)

    sv = sub.add_parser("split-verify",
                        help="check a split manifest: every base context present exactly "
                             "once, verifiers green (read-only)")
    add_manifest(sv)
    sv.set_defaults(func=cmd_split_verify)

    pr = sub.add_parser("promote",
                        help="write a regularity/v1 skeleton and its regularities/MEMORY.md "
                             "pointer, or with --principle --grounded-in a principle/v1 "
                             "skeleton (refuses the canonical checkout)")
    pr.add_argument("--root", help="memory root (default: <repo>/memory-global, or the "
                                   "manifest's root when --manifest is given)")
    add_manifest(pr, required=False)
    pr.add_argument("--slug", required=True)
    pr.add_argument("--name", required=True)
    pr.add_argument("--description", required=True)
    pr.add_argument("--generality", type=int, choices=[1, 2, 3], required=True)
    pr.add_argument("--induced-from", action="append", dest="induced_from", metavar="SLUG",
                    help="a leaf slug this regularity generalizes; repeatable")
    pr.add_argument("--principle", action="store_true",
                    help="write a principle skeleton (the norm layer) instead of a "
                         "regularity; needs --grounded-in")
    pr.add_argument("--grounded-in", action="append", dest="grounded_in", metavar="REG",
                    help="with --principle: an existing regularity slug the principle rests "
                         "on; repeatable or comma-separated")
    pr.add_argument("--statement", required=True)
    pr.add_argument("--domain")
    pr.add_argument("--date", default=today())
    pr.add_argument("--session", default=None,
                    help="ledger session_id for the edit-ledger stamp of this write")
    pr.add_argument("--rule-derived", metavar="REASON",
                    help="why a regularity with no experience or case source exists")
    pr.add_argument("--order-derived", metavar="TASK",
                    help="a regularity generalized from the factual statements of this task's "
                         "order; needs --order-quote, --induced-from becomes optional")
    pr.add_argument("--order-quote", metavar="TEXT",
                    help="with --order-derived: the order's factual statement, stored verbatim")
    pr.add_argument("--introduced-by", metavar="TASK",
                    help="the task that introduces this leaf (default with --order-derived: "
                         "that task)")
    pr.add_argument("--project-dir", metavar="D",
                    help="project repo dir (a memory dir holding experience/ or principles/ is "
                         "also accepted): write the project's principles/<slug>.md from its leaves")
    pr.set_defaults(func=cmd_promote)

    def provenance_parser(name: str, help_text: str, func) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--root", help="memory root (default: <repo>/memory-global)")
        sp.add_argument("--project-dir", metavar="D",
                        help="project repo dir, as for every verb; these verbs also "
                             "accept a memory dir")
        sp.add_argument("--session", default=None,
                        help="ledger session_id for the edit-ledger stamp of this write")
        sp.set_defaults(func=func)
        return sp

    for name, func, verb in (("link", cmd_link, "write"), ("unlink", cmd_unlink, "remove")):
        lk = provenance_parser(name, f"{verb} every side of a leaf -> regularity/case link",
                               func)
        lk.add_argument("source", nargs="?", metavar="SOURCE")
        lk.add_argument("target", nargs="?", metavar="TARGET")
        lk.add_argument("--pairs", metavar="FILE", help="one 'source<TAB>target' line each")

    ca = provenance_parser("case-add", "write sanitized cases of project leaves and link "
                                       "them to global regularities", cmd_case_add)
    ca.add_argument("--spec", required=True, metavar="FILE",
                    help="JSON list of {" + ", ".join(CASE_SPEC_KEYS) + "} of one project; the "
                         "identifier check is a literal substring match, a paraphrase of a "
                         "project name is not caught")
    ca.add_argument("--date", default=today())

    bf = provenance_parser("backfill", "stamp generalized_into on every leaf from the "
                                       "regularities' induced_from", cmd_backfill)
    bf.add_argument("--rule-derived", action="append", metavar="SLUG=REASON")

    pv = provenance_parser("provenance", "report provenance links (read-only)",
                           cmd_provenance)
    pv.add_argument("--check", action="store_true", help="list unaccounted items; exit 1 on any")
    pv.add_argument("--of", metavar="SLUG",
                    help="every leaf a revision of the regularity or principle SLUG must consult")
    return p


ROUTED_ENV = worktree_route.GUARD_ENV
WRITE_COMMANDS = ("new", "extend", "ticket", "set-last-verified")


def _under_repo(path: str) -> bool:
    try:
        Path(path).resolve().relative_to(REPO_ROOT)
    except ValueError:
        return False
    return True


def needs_worktree(args) -> bool:
    if args.cmd not in WRITE_COMMANDS or os.environ.get(ROUTED_ENV):
        return False
    if args.cmd in ("extend", "set-last-verified"):
        return _under_repo(args.leaf)
    return args.scope == "global"


def write_via_worktree(args, argv: list[str]) -> int:
    label = Path(args.leaf).name if hasattr(args, "leaf") else args.slug
    return worktree_route.route_script(
        REPO_ROOT, "scripts/record-experience.py", argv,
        f"experience: {args.cmd} {label}", path_options=("--leaf",),
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if needs_worktree(args) and worktree_route.should_route(REPO_ROOT):
        code = write_via_worktree(args, argv)
        if code == 0:
            leaf = written_leaf(args)
            if leaf:
                query = (args.difficulty if args.cmd == "new"
                         else f"{args.context_label} {args.plan}")
                ledger_record(args.cmd, args, query, [leaf])
        return code
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
