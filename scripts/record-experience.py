#!/usr/bin/env python3
"""Generate and maintain difficulty-centric experience leaves.

The unit of experience is a recurring difficulty (see
memory-global/leaves/experience-leaf-schema.md). This tool guarantees the
`schema: difficulty/v1` structure (the generality-0 profile of the unified
difficulty-record model — it stamps `generality: 0` on newly created leaves;
the generality>=1 profile is the principle/v1 leaf), auto-dates filenames,
maintains the `experience/MEMORY.md` sub-index, and implements the
search-before-record / extend-with-a-new-context flow that lets one difficulty
accumulate several contexts.

Subcommands:
  search <keywords>   Rank existing experience leaves by description +
                      `## Difficulty` overlap. MANDATORY before recording —
                      extend an analogous leaf instead of duplicating.
  new                 Write a fresh standalone leaf (first occurrence).
  extend              Append a new `### context` to an existing leaf; once it
                      holds >=2 contexts, scaffold `## Common core & variations`.
  ticket              Write a THIN pointer leaf (record lives in the ticket)
                      and print the structured body to stdout for posting.

Scope: --scope global (default; this repo's memory-global/leaves/experience)
or --scope project --project-dir <dir> (<dir>/.claude/agent-memory/experience).

verify-experience-leaf.py enforces the shape this tool produces.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import edit_ledger  # noqa: E402

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
        return Path(project_dir) / ".claude/agent-memory/experience"
    return REPO_ROOT / "memory-global/leaves/experience"


# The ranking section per tier: experience leaves rank on their `## Difficulty`,
# principles on their `## Principle`. The term-counting ranking itself is identical.
TIER_SECTION = {"experience": "Difficulty", "principles": "Principle"}


def search_root(scope: str, project_dir: str | None, tier: str) -> Path:
    # principles are a global-only tier (ADR-0001 generality gradient); experience
    # keeps its existing global/project split.
    if tier == "principles":
        return REPO_ROOT / "memory-global/leaves/principles"
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
        # untouched (absence implies 0). An experience leaf is the generality-0
        # profile of the unified difficulty-record model whose generality>=1
        # profile is the principle/v1 leaf — see experience-leaf-schema.md.
        "generality: 0",
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
def tokenize(text: str) -> list[str]:
    """The single tokenizer behind both search-before-record and difficulty clustering."""
    return [t.lower() for t in re.findall(r"\w+", text)]


def term_score(haystack: str, terms: list[str]) -> int:
    """Count term occurrences — the identical ranking the digest reuses as its clustering join,
    so there is exactly one ranking engine (ADR-0001: 'that search IS the clustering')."""
    hay = haystack.lower()
    return sum(hay.count(t) for t in terms)


# Join ratio: shared-term overlap above which two functional grounds are the same cluster.
# Consumed by core-difficulty-digest (channel records) and promote-scan (experience leaves).
JOIN_RATIO = 0.6


def _similarity(ground_a: str, ground_b: str) -> float:
    """Symmetric term-overlap ratio using the reused ranking scorer. 1.0 == identical terms."""
    terms_b = tokenize(ground_b)
    if not terms_b:
        return 0.0
    # term_score counts occurrences; normalise by the smaller token count for a 0..1 ratio.
    matched = sum(1 for t in set(terms_b) if term_score(ground_a, [t]) > 0)
    denom = max(len(set(tokenize(ground_a))), len(set(terms_b))) or 1  # larger (union) set size
    return matched / denom


def cluster_by_ground(items, ground_fn, join_ratio=JOIN_RATIO) -> list[list]:
    """Group items by functional ground using the shared ranking engine.

    An item joins the best-matching existing group when _similarity >= join_ratio,
    else opens a new group. ground_fn(item) -> str yields the comparison ground.
    Returns list[list]; the first item in each group is the representative."""
    groups: list[list] = []
    grounds: list[str] = []
    for item in items:
        g = ground_fn(item)
        best_idx, best_sim = -1, 0.0
        for i, rep in enumerate(grounds):
            sim = _similarity(rep, g)
            if sim > best_sim:
                best_idx, best_sim = i, sim
        if best_idx >= 0 and best_sim >= join_ratio:
            groups[best_idx].append(item)
        else:
            groups.append([item])
            grounds.append(g)
    return groups


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
def cmd_search(a) -> int:
    tier = getattr(a, "tier", "experience")
    dom = getattr(a, "domain", None)
    root = search_root(a.scope, a.project_dir, tier)
    section = TIER_SECTION[tier]
    terms = tokenize(a.keywords)
    if not terms:
        sys.exit("search needs keywords")
    scored: list[tuple[int, Path, str]] = []
    for leaf in sorted(root.glob("*.md")):
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
        if dom and tier == "principles" and leaf_domain and leaf_domain != dom:
            continue
        sec_span = section_span(text, section)
        sec = text[sec_span[0]:sec_span[1]] if sec_span else ""
        score = term_score(desc + " " + sec, terms)
        if score:
            scored.append((score, leaf, desc.strip()))
    scored.sort(key=lambda x: -x[0])
    noun = "principle" if tier == "principles" else "experience leaf"
    if not scored:
        print(f"no analogous {noun} found — record a NEW leaf")
        return 0
    verb = "ground a stage in" if tier == "principles" else "extend"
    print(f"analogous {noun}s ({verb} one instead of duplicating):")
    for score, leaf, desc in scored[:8]:
        print(f"  [{score:>3}] {leaf.name}\n        {desc}")
    return 0


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

    groups = cluster_by_ground(records, lambda r: r.ground)
    clusters = []
    for group in groups:
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
        print(_json.dumps(clusters, indent=2))
        return 0

    for c in clusters:
        print(f"[{c['occurrences_total']} occurrence(s)] {', '.join(c['members'])}")
        if c["flagged"]:
            print(f"  → candidate: lift into a principle/v1 leaf "
                  f"(induced_from = {c['members']})")
        if c["fragmented"]:
            print(f"  → fragmented across {len(c['members'])} leaves "
                  f"— consider merging via extend")
    return 0


def cmd_new(a) -> int:
    exp_dir = experience_dir(a.scope, a.project_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{a.date}-{a.slug}.md"
    path = exp_dir / filename
    if path.exists():
        sys.exit(f"refusing to overwrite existing leaf: {path}")
    # Fragmentation guard: refuse silent duplication of an analogous difficulty.
    justify_new = getattr(a, "justify_new", None)
    best_sim, best_name = 0.0, None
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
        existing_ground = desc + " " + diff_body
        sim = _similarity(existing_ground, a.difficulty)
        if sim > best_sim:
            best_sim, best_name = sim, leaf.name
    if best_sim >= JOIN_RATIO and not justify_new:
        sys.exit(
            f"refusing to fragment: analogous leaf {best_name!r} already exists "
            f"(similarity {best_sim:.2f} >= {JOIN_RATIO:.2f}). "
            f"Use `extend --leaf {best_name}` to add a context, "
            f"or pass `--justify-new \"<reason>\"` for a genuinely distinct difficulty."
        )
    path.write_text(standalone_body(a), encoding="utf-8")
    edit_ledger.stamp(str(path), "record-experience:new", session=getattr(a, "session", None))
    update_subindex(exp_dir, a.date, a.title, filename, a.description,
                    session=getattr(a, "session", None))
    print(f"wrote {path}\nupdated {exp_dir / 'MEMORY.md'}")
    return 0


def cmd_extend(a) -> int:
    path = Path(a.leaf)
    if not path.exists():
        sys.exit(f"leaf not found: {path}")
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
    print(f"extended {path} (now {n_ctx} context(s))")
    if n_ctx >= 2 and (not a.common or not a.variations):
        print("→ fill `## Common core & variations` to distill the general solution")
    return 0


def cmd_set_last_verified(a) -> int:
    path = Path(a.leaf)
    if not path.exists():
        sys.exit(f"leaf not found: {path}")
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
        choices=["experience", "principles"],
        default="experience",
        help="which leaf tier to search: experience (default) or the principles generality tier",
    )
    s.add_argument(
        "--domain",
        default=None,
        help=(
            "filter principle leaves by their domain: frontmatter tag; "
            "untagged leaves always match; a differently-tagged leaf is excluded. "
            "Only applied when --tier principles is set."
        ),
    )
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
                             "principle-induction candidates")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
