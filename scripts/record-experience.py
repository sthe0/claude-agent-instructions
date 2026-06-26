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

REPO_ROOT = Path(__file__).resolve().parents[1]
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
                ticket: str | None) -> str:
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
    if refs:
        lines.append("refs: [" + ", ".join(refs) + "]")
    if plan_file:
        lines.append(f"plan_file: {plan_file}")
    if ticket:
        lines.append(f"ticket: {ticket}")
    lines.append("---\n")
    return "\n".join(lines)


def standalone_body(a) -> str:
    name = f"{a.date}-{a.slug}"
    parts = [
        frontmatter(name, a.description, a.confirmed_by, a.refs, a.plan_file, None),
        f"\n# {a.title}\n",
        "\n## Difficulty\n", f"{a.difficulty}\n",
        "\n## Order & criterion\n", f"{a.order}\n",
        f"\n**Acceptance check:** {a.criterion}\n",
        "\n## Contexts\n",
        context_block(a.date, a.context_label, a.context_where, a.plan),
        "\n## Cost\n", f"{a.cost or 'TODO — fill via cost-report.py / tool-usage-report.py'}\n",
    ]
    if a.self_critique:
        parts += ["\n## Self-critique of the agent system\n", f"{a.self_critique}\n"]
    return "".join(parts)


def ticket_leaf_body(a) -> str:
    name = f"{a.date}-{a.slug}"
    url = a.ticket_url or a.ticket
    body = [
        frontmatter(name, a.description, a.confirmed_by, a.refs, None, a.ticket),
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
                    description: str) -> None:
    idx = exp_dir / "MEMORY.md"
    pointer = f"- [{date} — {title}]({filename}) — {description}\n"
    month = date[:7]
    if not idx.exists():
        idx.write_text(
            "# Experience\n\nDifficulty-centric experience leaves "
            "(see ../experience-leaf-schema.md). Most recent first.\n\n"
            f"## {month}\n\n{pointer}",
            encoding="utf-8",
        )
        return
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


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------
def cmd_search(a) -> int:
    tier = getattr(a, "tier", "experience")
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
        if fm:
            dm = re.search(r"^description:\s*(.*)$", fm.group(1), re.MULTILINE)
            desc = dm.group(1) if dm else ""
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


def cmd_new(a) -> int:
    exp_dir = experience_dir(a.scope, a.project_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{a.date}-{a.slug}.md"
    path = exp_dir / filename
    if path.exists():
        sys.exit(f"refusing to overwrite existing leaf: {path}")
    path.write_text(standalone_body(a), encoding="utf-8")
    update_subindex(exp_dir, a.date, a.title, filename, a.description)
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
    path.write_text(text, encoding="utf-8")
    print(f"extended {path} (now {n_ctx} context(s))")
    if n_ctx >= 2 and (not a.common or not a.variations):
        print("→ fill `## Common core & variations` to distill the general solution")
    return 0


def cmd_ticket(a) -> int:
    exp_dir = experience_dir(a.scope, a.project_dir)
    exp_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{a.date}-{a.slug}.md"
    path = exp_dir / filename
    if path.exists():
        sys.exit(f"refusing to overwrite existing leaf: {path}")
    path.write_text(ticket_leaf_body(a), encoding="utf-8")
    update_subindex(exp_dir, a.date, a.title, filename, a.description)
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

    n = sub.add_parser("new")
    add_scope(n)
    add_content(n)
    n.add_argument("--context-label", default="initial", dest="context_label")
    n.add_argument("--plan-file", dest="plan_file")
    n.add_argument("--cost")
    n.add_argument("--self-critique", dest="self_critique")
    n.set_defaults(func=cmd_new)

    e = sub.add_parser("extend")
    add_scope(e)
    e.add_argument("--leaf", required=True)
    e.add_argument("--context-label", required=True, dest="context_label")
    e.add_argument("--context-where", required=True, dest="context_where")
    e.add_argument("--plan", required=True)
    e.add_argument("--common")
    e.add_argument("--variations")
    e.set_defaults(func=cmd_extend)

    t = sub.add_parser("ticket")
    add_scope(t)
    add_content(t, ticket=True)
    t.add_argument("--ticket", required=True)
    t.add_argument("--ticket-url", dest="ticket_url")
    t.add_argument("--context-label", default="initial", dest="context_label")
    t.add_argument("--distill")
    t.set_defaults(func=cmd_ticket)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
