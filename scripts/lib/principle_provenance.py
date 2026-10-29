"""The provenance link between experience and the regularities that generalize it.

An experience leaf (difficulty/v1, either tier) carries `generalized_into: [...]`
naming the regularities that state its general aspect; `[]` is the explicit
"not generalized yet". A regularity (regularity/v1, under `regularities/`, the
knowledge layer) carries `induced_from: [...]` naming its sources; a regularity
with no experience source carries a `rule_derived:` reason instead. The two
sides mirror each other, and the regularity's `regularities/MEMORY.md` pointer
lists the same sources after its `← `.

A global principle (principle/v1, the norm layer) is not induced: it carries
`grounded_in: [regularity:<slug>, ...]` naming the regularities it rests on and
no `induced_from`; its `principles/MEMORY.md` pointer lists the same slugs, the
`regularity:` prefix dropped, after its `← `.

A project leaf never names a global regularity directly: its general aspect
reaches Core as a sanitized case (case/v1, under `principles/cases/`) that is
linked two-sided with its global regularities and cited one way by the project
leaf. Project-only generalizations live in the project's own `principles/`,
which keep `induced_from` (the project tier is grandfathered).

Every rule is a module-level function, called by its global name at call time,
so each can be replaced in-process by a test of the rule it decides. Callers
import this module as `lib.principle_provenance` and call `pp.<fn>`.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

FIELD = "generalized_into"
INDUCED = "induced_from"
GROUNDED = "grounded_in"
INTRODUCED_BY = "introduced_by"
ORDER_DERIVED = "order_derived"
ORDER_SECTION_TITLE = "Derived from order"
REGULARITY_PREFIX = "regularity:"
RULE_DERIVED = "rule_derived"
SOURCE_DIGEST = "source_digest"
ORIGIN_TIER_FIELD = "origin_tier"
DIGEST_PREFIX = "sha256:"
CASE_SCHEMA = "case/v1"
CASE_SECTIONS = ("Aspect", "Expected", "Actual", "Mismatch")
ORIGIN_TIER = "project"
LEAVES_REL = Path("memory-global/leaves")
CASES_DIR = Path("principles/cases")
INDEX = "MEMORY.md"
EXPERIENCE_SOURCE_KINDS = ("experience", "case")
STAGED_SCOPES = ("memory-global/leaves/experience/", "memory-global/leaves/regularities/",
                 "memory-global/leaves/principles/")
GENERIC_DIR_NAMES = frozenset({"/", ".claude", "agent-memory"})
MIN_IDENTIFIER_LEN = 6

MISSING_STATUS = "missing_status"
BAD_STATUS = "bad_status"
UNKNOWN_PRINCIPLE = "unknown_principle"
ASYMMETRIC = "asymmetric_link"
NO_SOURCE = "no_experience_source"
STALE_MARK = "stale_rule_mark"
DANGLING = "dangling_source"
INDEX_MISMATCH = "index_mismatch"
CASE_SECTIONS_KIND = "case_sections"
CASE_DIGEST_FORMAT = "case_digest_format"
CASE_ORIGIN_TIER = "case_origin_tier"
UNCITED_CASE = "uncited_case"
PROJECT_DIRECT_GLOBAL = "project_direct_global"
STALE_CASE = "stale_case"
ORPHAN_CASE = "orphan_case"
CASE_LEAK = "case_leak"
UNGROUNDED_PRINCIPLE = "ungrounded_principle"
UNKNOWN_REGULARITY = "unknown_regularity"
PRINCIPLE_INDUCED = "principle_induced_from"
ORDER_NO_QUOTE = "order_no_quote"
ORDER_ON_PRINCIPLE = "order_on_principle"
ORDER_NO_INTRODUCED = "order_no_introduced"

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
DIGEST_RE = re.compile(re.escape(DIGEST_PREFIX) + r"[0-9a-f]{64}")
POINTER_SLUG = re.compile(r"^- \[.*\]\(([^)/]+)\.md\)")
ARROW = "← "


@dataclass(frozen=True)
class Leaf:
    slug: str
    path: Path
    tier: str  # 'global' | 'project'
    area: str  # 'experience' | 'regularity' | 'principle' | 'case' | 'other'
    text: str


@dataclass(frozen=True)
class Change:
    path: Path
    new_text: str
    role: str  # 'leaf' | 'regularity' | 'principle' | 'index' | 'case'


@dataclass(frozen=True)
class Finding:
    kind: str
    path: Path
    detail: str


_AREA_PRIORITY = ("experience", "case", "regularity", "principle", "other")


@dataclass
class Corpus:
    """Every leaf of the global leaves dir and, optionally, of a project memory dir.

    `files` holds the text of every markdown file read (indexes included) and any
    pending in-memory edit, so a batch of changes can be computed one after another."""
    leaves_dir: Path
    project_root: Path | None = None
    files: dict[Path, str] = field(default_factory=dict)
    items: list[Leaf] = field(default_factory=list)

    def find(self, slug: str, tier: str = "global") -> Leaf | None:
        tiers = ("project", "global") if tier == "project" else ("global",)
        for t in tiers:
            hits = [l for l in self.items if l.slug == slug and l.tier == t]
            if hits:
                return min(hits, key=lambda l: _AREA_PRIORITY.index(l.area))
        return None

    def of(self, area: str, tier: str | None = None) -> list[Leaf]:
        return [l for l in self.items if l.area == area and (tier is None or l.tier == tier)]

    def read(self, path: Path) -> str | None:
        path = Path(path)
        if path in self.files:
            return self.files[path]
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def apply(self, changes) -> "Corpus":
        files = dict(self.files)
        for ch in changes:
            files[Path(ch.path)] = ch.new_text
        return _build(self.leaves_dir, self.project_root, files)


def _area(rel: Path, tier: str) -> str:
    parts = rel.parts
    if len(parts) == 2 and parts[0] == "experience":
        return "experience"
    if tier == "global" and len(parts) == 3 and parts[:2] == CASES_DIR.parts:
        return "case"
    if tier == "global" and len(parts) == 2 and parts[0] == "regularities":
        return "regularity"
    if len(parts) == 2 and parts[0] == "principles":
        return "principle"
    return "other"


def _build(leaves_dir: Path, project_root: Path | None, files: dict[Path, str]) -> Corpus:
    items = []
    for path, text in sorted(files.items()):
        if path.name == INDEX:
            continue
        for tier, base in (("project", project_root), ("global", leaves_dir)):
            if base is None:
                continue
            try:
                rel = path.relative_to(base)
            except ValueError:
                continue
            items.append(Leaf(path.stem, path, tier, _area(rel, tier), text))
            break
    return Corpus(leaves_dir, project_root, files, items)


def load_corpus(leaves_dir, project_root=None) -> Corpus:
    leaves_dir = Path(leaves_dir)
    project_root = Path(project_root) if project_root is not None else None
    files: dict[Path, str] = {}
    for base in (leaves_dir, project_root):
        if base is not None and base.is_dir():
            for path in sorted(base.rglob("*.md")):
                files[path] = path.read_bytes().decode("utf-8")
    return _build(leaves_dir, project_root, files)


def corpus_from_texts(leaves_dir, files, project_root=None) -> Corpus:
    """A corpus over the given path -> text map instead of the working tree (e.g. the git index)."""
    return _build(Path(leaves_dir), Path(project_root) if project_root is not None else None,
                  {Path(p): t for p, t in files.items()})


# --------------------------------------------------------------------------
# frontmatter fields
# --------------------------------------------------------------------------
def _line_re(key: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(key)}[ \t]*:(.*)$", re.MULTILINE)


def field_value(text: str, key: str) -> str | None:
    m = FRONTMATTER.match(text)
    if not m:
        return None
    line = _line_re(key).search(m.group(1))
    return line.group(1).strip() if line else None


def parse_list(value: str) -> list[str]:
    if not (value.startswith("[") and value.endswith("]")):
        raise ValueError(f"not an inline list: {value!r}")
    inner = value[1:-1].strip()
    return [s.strip().strip("'\"") for s in inner.split(",") if s.strip()] if inner else []


def format_list(items) -> str:
    return "[" + ", ".join(items) + "]"


def set_field(text: str, key: str, value: str) -> str:
    """Replace the field's line, or insert it before the closing `---`; no other byte moves."""
    m = FRONTMATTER.match(text)
    if not m:
        return text
    body = m.group(1)
    line = _line_re(key).search(body)
    if line:
        body = body[:line.start()] + f"{key}: {value}" + body[line.end():]
    else:
        body = body + f"\n{key}: {value}"
    return text[:m.start(1)] + body + text[m.end(1):]


def remove_field(text: str, key: str) -> str:
    m = FRONTMATTER.match(text)
    if not m:
        return text
    body = m.group(1)
    line = _line_re(key).search(body)
    if not line:
        return text
    start, end = line.start(), line.end()
    if end < len(body):
        end += 1
    elif start > 0:
        start -= 1
    return text[:m.start(1)] + body[:start] + body[end:] + text[m.end(1):]


def read_status(text: str) -> list[str] | None:
    """The leaf's `generalized_into`; None only when the field is absent."""
    value = field_value(text, FIELD)
    return None if value is None else parse_list(value)


def read_induced(text: str) -> list[str]:
    value = field_value(text, INDUCED)
    return [] if value is None else parse_list(value)


def read_grounded(text: str) -> list[str]:
    value = field_value(text, GROUNDED)
    return [] if value is None else parse_list(value)


def _grounded_or_empty(text: str) -> list[str]:
    try:
        return read_grounded(text)
    except ValueError:
        return []


def _induced_or_empty(text: str) -> list[str]:
    try:
        return read_induced(text)
    except ValueError:
        return []


def _status_or_empty(text: str) -> list[str]:
    try:
        return read_status(text) or []
    except ValueError:
        return []


def merge_status(existing, new) -> list[str]:
    out = list(existing or [])
    for slug in new or []:
        if slug not in out:
            out.append(slug)
    return out


def status_error(text: str) -> str | None:
    value = field_value(text, FIELD)
    if value is None:
        return (f"frontmatter missing required field `{FIELD}` "
                f"(`{FIELD}: []` when no principle states this difficulty yet)")
    try:
        parse_list(value)
    except ValueError:
        return f"`{FIELD}` must be an inline list, got {value!r}"
    return None


def birth_fields() -> dict[str, list[str]]:
    return {FIELD: []}


def clear_rule_mark(text: str) -> str:
    return remove_field(text, RULE_DERIVED)


# --------------------------------------------------------------------------
# relation rules
# --------------------------------------------------------------------------
def target_of(slug: str, corpus: Corpus, tier: str = "global") -> Leaf | None:
    """What a leaf of `tier` names: a global leaf a global regularity; a project leaf
    a project principle, a case, or (refused later) a global regularity or principle."""
    if tier == "project":
        for leaf in corpus.items:
            if leaf.slug == slug and leaf.tier == "project" and leaf.area == "principle":
                return leaf
        for area in ("case", "regularity", "principle"):
            for leaf in corpus.items:
                if leaf.slug == slug and leaf.tier == "global" and leaf.area == area:
                    return leaf
        return None
    for leaf in corpus.items:
        if leaf.slug == slug and leaf.tier == "global" and leaf.area == "regularity":
            return leaf
    return None


def principle_exists(slug: str, corpus: Corpus, tier: str = "global") -> bool:
    return target_of(slug, corpus, tier) is not None


def symmetric(source: Leaf, target: Leaf, corpus: Corpus) -> bool:
    try:
        status = read_status(source.text) or []
    except ValueError:
        status = []
    return target.slug in status and source.slug in _induced_or_empty(target.text)


def classify(slug: str, corpus: Corpus, tier: str = "global") -> str:
    leaf = corpus.find(slug, tier)
    if leaf is not None and leaf.area in EXPERIENCE_SOURCE_KINDS:
        return leaf.area
    return "rule"


def resolve_source(slug: str, corpus: Corpus, tier: str = "global") -> str | None:
    if corpus.find(slug, tier) is None:
        return None
    return classify(slug, corpus, tier)


def rule_mark_state(target: Leaf, corpus: Corpus) -> str:
    kinds = [resolve_source(s, corpus, target.tier) for s in _induced_or_empty(target.text)]
    has_experience = any(k in EXPERIENCE_SOURCE_KINDS for k in kinds)
    marked = bool(field_value(target.text, RULE_DERIVED))
    from_order = bool(field_value(target.text, ORDER_DERIVED))
    if has_experience and marked:
        return "stale"
    if not (has_experience or marked or from_order):
        return "missing"
    return "ok"


def promote_sources_ok(kinds, rule_derived, order_derived=None) -> bool:
    return (bool(rule_derived) or bool(order_derived)
            or any(k in EXPERIENCE_SOURCE_KINDS for k in kinds))


def order_kind_ok(kind: str) -> bool:
    """Order provenance yields knowledge, so only a regularity may carry it."""
    return kind == "regularity"


def order_introduced_by(order_derived, introduced_by):
    """The `introduced_by` value promote writes (the given one, else the order's task); the
    reader of a written leaf is `introduced_by()`."""
    return introduced_by or order_derived or None


def introduced_by(text: str) -> str | None:
    return field_value(text, INTRODUCED_BY) or None


def order_quote(text: str) -> str | None:
    """The body of the `## Derived from order` section, outer newlines trimmed; None when the
    section is absent or holds only blank lines."""
    body = section_body(text, ORDER_SECTION_TITLE)
    return body.strip("\n") if body and body.strip() else None


def render_order_section(quote: str, date: str) -> str:
    """The chunk a promoted regularity carries: a dated line, then the section whose whole
    body is the quote."""
    return f"Generalized from an order on {date}.\n\n## {ORDER_SECTION_TITLE}\n\n{quote}\n"


def order_introduced_problems(text: str) -> list[str]:
    if field_value(text, ORDER_DERIVED) and not introduced_by(text):
        return [f"an order-derived leaf carries no `{INTRODUCED_BY}`"]
    return []


def target_allowed(leaf_tier: str, target: Leaf) -> bool:
    return not (leaf_tier == "project" and target.tier == "global"
                and target.area in ("regularity", "principle"))


def induced_leaves(corpus: Corpus) -> list[Leaf]:
    """The leaves that carry `induced_from`: every regularity and each project principle."""
    return [l for l in corpus.items
            if l.area == "regularity" or (l.area == "principle" and l.tier == "project")]


def grounded_regularities(principle: Leaf) -> list[str]:
    """The regularity slugs a global principle's `grounded_in` names (prefix dropped)."""
    return [s[len(REGULARITY_PREFIX):] for s in _grounded_or_empty(principle.text)
            if s.startswith(REGULARITY_PREFIX)]


def pointer_expected(leaf: Leaf) -> list[str]:
    """What the leaf's index pointer lists after its arrow."""
    if leaf.area == "principle" and leaf.tier == "global":
        return grounded_regularities(leaf)
    return _induced_or_empty(leaf.text)


def is_cross_tier(leaf: Leaf, target: Leaf) -> bool:
    return leaf.tier == "project" and target.area == "case"


def cross_tier_enabled(project_root) -> bool:
    return project_root is not None


def staged_triggers(paths) -> bool:
    return any(str(p).startswith(STAGED_SCOPES) for p in paths)


def index_exempt(relpath) -> bool:
    return f"/{CASES_DIR.as_posix()}/" in "/" + str(relpath).replace(os.sep, "/")


# --------------------------------------------------------------------------
# principles/MEMORY.md pointers
# --------------------------------------------------------------------------
def index_path(leaf: Leaf) -> Path:
    return leaf.path.parent / INDEX


def pointer_sources(line: str) -> list[str] | None:
    if ARROW not in line:
        return None
    return re.findall(r"`([^`]+)`", line.rsplit(ARROW, 1)[1])


def _pointer_index(lines: list[str], slug: str) -> int | None:
    for i, line in enumerate(lines):
        m = POINTER_SLUG.match(line)
        if m and m.group(1) == slug and ARROW in line:
            return i
    return None


def index_sync(index_text: str, slug: str, sources) -> str:
    """The index with `slug`'s pointer listing exactly `sources` after its arrow."""
    lines = index_text.split("\n")
    i = _pointer_index(lines, slug)
    if i is None:
        return index_text
    head = lines[i].rsplit(ARROW, 1)[0]
    lines[i] = head + ARROW + ", ".join(f"`{s}`" for s in sources) + "."
    return "\n".join(lines)


def index_mismatches(corpus: Corpus) -> list[Leaf]:
    out = []
    for leaf in corpus.of("regularity") + corpus.of("principle"):
        text = corpus.read(index_path(leaf))
        if text is None:
            continue
        lines = text.split("\n")
        i = _pointer_index(lines, leaf.slug)
        if i is None:
            continue
        if set(pointer_sources(lines[i]) or []) != set(pointer_expected(leaf)):
            out.append(leaf)
    return out


def _index_change(corpus: Corpus, leaf: Leaf, sources) -> Change | None:
    path = index_path(leaf)
    text = corpus.read(path)
    if text is None:
        return None
    new = index_sync(text, leaf.slug, sources)
    return Change(path, new, "index") if new != text else None


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------
def body_digest(text: str) -> str:
    m = FRONTMATTER.match(text)
    body = text[m.end():] if m else text
    return DIGEST_PREFIX + hashlib.sha256(body.encode("utf-8")).hexdigest()


def digest_format_ok(value) -> bool:
    return bool(DIGEST_RE.fullmatch(value or ""))


def origin_tier_ok(value) -> bool:
    return value == ORIGIN_TIER


def section_body(text: str, name: str) -> str | None:
    m = re.search(rf"^##[ \t]+{re.escape(name)}[ \t]*$", text, re.MULTILINE)
    if not m:
        return None
    nxt = re.search(r"^##[ \t]", text[m.end():], re.MULTILINE)
    return text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]


def case_section_error(text: str) -> str | None:
    missing = [n for n in CASE_SECTIONS if not (section_body(text, n) or "").strip()]
    if missing:
        return "case is missing or has empty section(s): " + ", ".join(f"## {n}" for n in missing)
    return None


def case_cited(case: Leaf, corpus: Corpus) -> bool:
    return any(case.slug in _induced_or_empty(r.text) for r in corpus.of("regularity", "global"))


def _citing_project_leaves(case: Leaf, corpus: Corpus) -> list[Leaf]:
    return [l for l in corpus.of("experience", "project") if case.slug in _status_or_empty(l.text)]


def orphaned(case: Leaf, corpus: Corpus) -> bool:
    return not _citing_project_leaves(case, corpus)


def digest_matches(case: Leaf, leaf_text: str) -> bool:
    return field_value(case.text, SOURCE_DIGEST) == body_digest(leaf_text)


def project_identifiers(corpus: Corpus) -> list[str]:
    if corpus.project_root is None:
        return []
    root = corpus.project_root.resolve()
    ids = {str(root)}
    names = [p for p in root.parts if p not in GENERIC_DIR_NAMES]
    if names:
        ids.add(names[-1])
    ids |= {l.slug for l in corpus.items
            if l.tier == "project" and l.area in ("experience", "principle")}
    return sorted(i for i in ids if len(i) >= MIN_IDENTIFIER_LEN)


def leaks(text: str, identifiers) -> list[str]:
    """Identifiers that occur in `text` as a literal substring; a paraphrase is not caught."""
    return [i for i in identifiers if i in text]


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------
def experiences_of(target: Leaf, corpus: Corpus) -> list[Leaf]:
    """Every leaf a revision of `target` (a regularity, a project principle, or a
    global principle through the regularities it is grounded in) must consult: its
    experience and case sources and, through each case, the project leaves that cite it."""
    out: list[Leaf] = []
    if target.area == "principle" and target.tier == "global":
        for slug in grounded_regularities(target):
            for reg in corpus.of("regularity", "global"):
                if reg.slug == slug:
                    out += [l for l in experiences_of(reg, corpus) if l not in out]
        return out
    for slug in _induced_or_empty(target.text):
        leaf = corpus.find(slug, target.tier)
        if leaf is None or leaf.area not in EXPERIENCE_SOURCE_KINDS:
            continue
        out.append(leaf)
        if leaf.area == "case":
            out += [l for l in _citing_project_leaves(leaf, corpus) if l not in out]
    return out


def unaccounted(corpus: Corpus) -> list[Finding]:
    findings: list[Finding] = []

    def report(kind, leaf, detail):
        findings.append(Finding(kind, leaf.path, detail))

    for leaf in corpus.of("experience") + corpus.of("case"):
        try:
            status = read_status(leaf.text)
        except ValueError as exc:
            report(BAD_STATUS, leaf, str(exc))
            continue
        if status is None:
            report(MISSING_STATUS, leaf, f"no `{FIELD}` field")
            continue
        for slug in status:
            if not principle_exists(slug, corpus, leaf.tier):
                noun = "principle" if leaf.tier == "project" else "regularity"
                report(UNKNOWN_PRINCIPLE, leaf, f"names unknown {noun} {slug!r}")
                continue
            target = target_of(slug, corpus, leaf.tier)
            if target is None:
                continue
            if not target_allowed(leaf.tier, target):
                report(PROJECT_DIRECT_GLOBAL, leaf,
                       f"a project leaf names global {target.area} {slug!r}; cite a case instead")
                continue
            if is_cross_tier(leaf, target):
                continue
            if not symmetric(leaf, target, corpus):
                report(ASYMMETRIC, leaf, f"names {slug!r}, whose `{INDUCED}` omits it")

    for target in induced_leaves(corpus):
        try:
            induced = read_induced(target.text)
        except ValueError as exc:
            report(BAD_STATUS, target, str(exc))
            continue
        for slug in induced:
            kind = resolve_source(slug, corpus, target.tier)
            if kind is None:
                report(DANGLING, target, f"`{INDUCED}` names missing leaf {slug!r}")
                continue
            if kind in EXPERIENCE_SOURCE_KINDS:
                source = corpus.find(slug, target.tier)
                if source is not None and not symmetric(source, target, corpus):
                    report(ASYMMETRIC, target, f"lists {slug!r}, whose `{FIELD}` omits it")
        state = rule_mark_state(target, corpus)
        if state == "missing":
            report(NO_SOURCE, target,
                   f"no experience or case source and no `{RULE_DERIVED}` reason")
        elif state == "stale":
            report(STALE_MARK, target, f"has an experience source and a stale `{RULE_DERIVED}`")

    regularity_slugs = {r.slug for r in corpus.of("regularity", "global")}
    for principle in corpus.of("principle", "global"):
        if field_value(principle.text, INDUCED) is not None:
            report(PRINCIPLE_INDUCED, principle,
                   f"a principle names its regularities in `{GROUNDED}`, not `{INDUCED}`")
        try:
            grounded = read_grounded(principle.text)
        except ValueError as exc:
            report(BAD_STATUS, principle, str(exc))
            continue
        if not grounded:
            report(UNGROUNDED_PRINCIPLE, principle,
                   f"no `{GROUNDED}: [{REGULARITY_PREFIX}<slug>, ...]` naming a regularity")
        for item in grounded:
            if not (item.startswith(REGULARITY_PREFIX)
                    and item[len(REGULARITY_PREFIX):] in regularity_slugs):
                report(UNKNOWN_REGULARITY, principle,
                       f"`{GROUNDED}` item {item!r} is not `{REGULARITY_PREFIX}<slug>` of an "
                       "existing regularity")

    for leaf in corpus.items:
        if not field_value(leaf.text, ORDER_DERIVED):
            continue
        if not order_kind_ok(leaf.area):
            report(ORDER_ON_PRINCIPLE, leaf,
                   f"`{ORDER_DERIVED}` belongs to a regularity only (found on {leaf.area})")
        if order_quote(leaf.text) is None:
            report(ORDER_NO_QUOTE, leaf, f"no quote under `## {ORDER_SECTION_TITLE}`")
        for problem in order_introduced_problems(leaf.text):
            report(ORDER_NO_INTRODUCED, leaf, problem)

    for leaf in index_mismatches(corpus):
        field_name = GROUNDED if leaf.area == "principle" and leaf.tier == "global" else INDUCED
        report(INDEX_MISMATCH, leaf, f"its {INDEX} pointer lists other sources than `{field_name}`")

    cross = cross_tier_enabled(corpus.project_root)
    identifiers = project_identifiers(corpus) if cross else []
    for case in corpus.of("case"):
        error = case_section_error(case.text)
        if error:
            report(CASE_SECTIONS_KIND, case, error)
        if not digest_format_ok(field_value(case.text, SOURCE_DIGEST)):
            report(CASE_DIGEST_FORMAT, case, f"`{SOURCE_DIGEST}` is not {DIGEST_PREFIX}<64 hex>")
        if not origin_tier_ok(field_value(case.text, ORIGIN_TIER_FIELD)):
            report(CASE_ORIGIN_TIER, case, f"`{ORIGIN_TIER_FIELD}` is not {ORIGIN_TIER}")
        if not case_cited(case, corpus):
            report(UNCITED_CASE, case, "no regularity lists this case")
        if not cross:
            continue
        if orphaned(case, corpus):
            report(ORPHAN_CASE, case, "no project leaf cites this case")
        elif not any(digest_matches(case, l.text) for l in _citing_project_leaves(case, corpus)):
            report(STALE_CASE, case, "no citing project leaf has the body this case was written from")
        found = leaks(case.text, identifiers)
        if found:
            report(CASE_LEAK, case, "carries project identifier(s): " + ", ".join(found))
    return findings


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------
def link_changes(source: Leaf, target: Leaf, corpus: Corpus) -> list[Change]:
    """Every side of the link source -> target: the source's status, and, unless the
    link is a project leaf citing a case, the target's sources and its index pointer."""
    changes = []
    status = read_status(source.text)
    new_status = merge_status(status or [], [target.slug])
    if status != new_status:
        changes.append(Change(source.path, set_field(source.text, FIELD, format_list(new_status)),
                              "leaf"))
    if is_cross_tier(source, target):
        return changes
    induced = merge_status(read_induced(target.text), [source.slug])
    text = clear_rule_mark(set_field(target.text, INDUCED, format_list(induced)))
    if text != target.text:
        changes.append(Change(target.path, text, target.area))
    index = _index_change(corpus, target, induced)
    if index is not None:
        changes.append(index)
    return changes


def unlink_changes(source: Leaf, target: Leaf, corpus: Corpus) -> list[Change]:
    changes = []
    status = read_status(source.text) or []
    if target.slug in status:
        kept = [s for s in status if s != target.slug]
        changes.append(Change(source.path, set_field(source.text, FIELD, format_list(kept)), "leaf"))
    if is_cross_tier(source, target):
        return changes
    induced = read_induced(target.text)
    if source.slug in induced:
        kept = [s for s in induced if s != source.slug]
        changes.append(Change(target.path, set_field(target.text, INDUCED, format_list(kept)),
                              target.area))
        index = _index_change(corpus, target, kept)
        if index is not None:
            changes.append(index)
    return changes


def backfill_status(corpus: Corpus, rule_derived: dict[str, str] | None = None) -> list[Change]:
    """Stamp `[]` on every leaf and case without the field, mirror every regularity's (and
    project principle's) sources onto them, set the given rule_derived reasons on global
    regularities and sync the index pointers."""
    texts: dict[Path, tuple[str, str]] = {}

    def current(path: Path) -> str:
        return texts[path][0] if path in texts else corpus.read(path)

    mirrored: dict[Path, list[str]] = {}
    for target in induced_leaves(corpus):
        for slug in _induced_or_empty(target.text):
            leaf = corpus.find(slug, target.tier)
            if leaf is not None and leaf.area in EXPERIENCE_SOURCE_KINDS:
                mirrored.setdefault(leaf.path, []).append(target.slug)
    for leaf in corpus.of("experience") + corpus.of("case"):
        try:
            status = read_status(leaf.text)
        except ValueError:
            continue
        new = merge_status(status or [], mirrored.get(leaf.path, []))
        if status != new:
            texts[leaf.path] = (set_field(leaf.text, FIELD, format_list(new)), "leaf")
    for slug, reason in (rule_derived or {}).items():
        regularity = next((p for p in corpus.of("regularity", "global") if p.slug == slug), None)
        if regularity is None:
            raise ValueError(f"no global regularity {slug!r} to mark rule-derived")
        texts[regularity.path] = (set_field(current(regularity.path), RULE_DERIVED, reason),
                                  "regularity")
    for leaf in corpus.of("regularity") + corpus.of("principle"):
        path = index_path(leaf)
        text = current(path)
        if text is None:
            continue
        new = index_sync(text, leaf.slug, pointer_expected(leaf))
        if new != text:
            texts[path] = (new, "index")
    return [Change(path, text, role) for path, (text, role) in texts.items()
            if text != corpus.read(path)]


def write_all(changes) -> None:
    """Write every change or none: each to a temp file beside its target, then replace all."""
    changes = list(changes)
    staged: list[tuple[str, Path]] = []
    try:
        for ch in changes:
            path = Path(ch.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            staged.append((tmp, path))
            with os.fdopen(fd, "wb") as fh:
                fh.write(ch.new_text.encode("utf-8"))
            os.chmod(tmp, mode)
    except BaseException:
        for tmp, _ in staged:
            _unlink_quiet(tmp)
        raise
    originals = {path: (path.read_bytes() if path.exists() else None) for _, path in staged}
    replaced: list[Path] = []
    try:
        for tmp, path in staged:
            os.replace(tmp, path)
            replaced.append(path)
    except BaseException:
        for path in replaced:
            if originals[path] is None:
                _unlink_quiet(path)
            else:
                path.write_bytes(originals[path])
        for tmp, path in staged:
            if path not in replaced:
                _unlink_quiet(tmp)
        raise


def _unlink_quiet(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
