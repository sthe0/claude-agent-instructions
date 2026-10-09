"""Standing-mandate rules: the pure, decidable half of the background debt cycle.

Difficulty removed: a standing authority the user grants once is only as safe as the
bounds on it, and a bound enforced by a model's judgement is a suggestion. Everything
here is decidable from observable inputs -- the mandate record, spend rows, the label
log, issue and pull-request JSON, a diff's name-status lines -- so it lives in one
module that does no I/O and reads no clock (`now` is always a parameter). The I/O half
(files, the cycle lock, events) is `mandate_store`; the model supplies only perception
(triage verdicts, the fix, the review) and never decides a bound.

The purity contract is tested: no filesystem, process, network or clock reach here.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

DEFAULT_MANDATE_ID = "core-debt"
DEFAULT_TERM_DAYS = 14

DEFAULT_LABELS = {"ok": "auto-ok", "no": "auto-no", "veto": "no-auto"}
DOMAIN_LABELS = ("backlog", "difficulty")
UMBRELLA_LABEL = "process-umbrella"
SEVERITY_HIGH_LABEL = "severity:high"

DAY_HOURS = 24
WEEK_HOURS = 7 * 24

DEFAULT_CONSTITUTION = (
    "scripts/agentctl/mandate.py",
    "scripts/agentctl/mandate_store.py",
    "scripts/mandate-cycle.py",
    "scripts/mandate_cycle/",
    "scripts/install-mandate-timer.sh",
    "scripts/tests/test_mandate*.py",
    "scripts/tests/mandate_mutation_control.py",
    "scripts/lib/widening_targets.py",
    "scripts/agentctl/grants.py",
    "scripts/lib/kind_baselines.py",
    "scripts/spawn-specialist.py",
    "scripts/cap-run.sh",
    "scripts/kill-tree.py",
    "scripts/session-isolate.sh",
    "scripts/verify-all.py",
    "scripts/lint-prose-length.py",
    "scripts/check-org-neutral.py",
    "githooks/",
    ".github/",
    "hooks/",
    "scripts/hook-*.py",
)
SETTINGS_GLOB = "settings*.json"

OUTCOMES = ("pr-opened", "failed", "overrun", "declined", "limit")
FAILURE_KINDS = (
    "tests", "review-reject", "constitution", "permission", "org-neutral", "state-tampered",
)


class MandateError(ValueError):
    """A mandate record or a request against it is malformed."""


@dataclass(frozen=True)
class MandateLimits:
    daily_usd: float = 30.0
    weekly_usd: float = 120.0
    max_items: int = 3
    item_usd_cap: float = 15.0
    item_minutes_cap: float = 125.0
    cycle_minutes_cap: float = 375.0
    veto_window_hours: float = 24.0

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise MandateError(f"{f.name} must be a positive number, got {value!r}")
        if int(self.max_items) != self.max_items:
            raise MandateError(f"max_items must be a whole number, got {self.max_items!r}")


LIMIT_FIELDS = tuple(f.name for f in fields(MandateLimits))


@dataclass(frozen=True)
class Mandate:
    id: str
    granted_by: str
    granted_at: str
    expires_at: str
    daily_usd: float
    weekly_usd: float
    max_items: int
    item_usd_cap: float
    item_minutes_cap: float
    cycle_minutes_cap: float
    veto_window_hours: float
    labels: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_LABELS))
    constitution: Sequence[str] = DEFAULT_CONSTITUTION
    paused: bool = False
    breaker_open: bool = False
    breaker_reason: str = ""

    @property
    def limits(self) -> MandateLimits:
        return MandateLimits(**{f.name: getattr(self, f.name) for f in fields(MandateLimits)})

    def to_dict(self) -> dict:
        data = asdict(self)
        data["labels"] = dict(self.labels)
        data["constitution"] = list(self.constitution)
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "Mandate":
        names = {f.name for f in fields(cls)}
        required = names - {"labels", "constitution", "paused", "breaker_open", "breaker_reason"}
        missing = sorted(required - set(data))
        if missing:
            raise MandateError(f"mandate record lacks {', '.join(missing)}")
        picked = {k: data[k] for k in names if k in data}
        picked["labels"] = {**DEFAULT_LABELS, **dict(picked.get("labels") or {})}
        picked["constitution"] = tuple(picked.get("constitution") or DEFAULT_CONSTITUTION)
        mandate = cls(**picked)
        mandate.validate()
        return mandate

    def validate(self) -> None:
        """Raise MandateError unless every limit is a positive number and both timestamps
        parse; a record that fails is never loaded, so a damaged file cannot grant defaults."""
        MandateLimits(**{f.name: getattr(self, f.name) for f in fields(MandateLimits)})
        parse_ts(self.granted_at, strict=True)
        parse_ts(self.expires_at, strict=True)


def parse_ts(value, *, strict: bool = False) -> "datetime | None":
    """An ISO-8601 timestamp as an aware UTC datetime; naive input is read as UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            if strict:
                raise MandateError(f"unparsable timestamp {value!r}") from None
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def require_slug(value, what: str) -> str:
    """A value that becomes a path component must be a plain slug: no separator, no `..`."""
    if not isinstance(value, str) or not _SLUG.fullmatch(value) or ".." in value:
        raise MandateError(f"{what} must be a plain slug, got {value!r}")
    return value


# --- state transitions (each is the whole effect of one user-facing verb) -------------

def new_mandate(
    mandate_id: str,
    granted_by: str,
    now: datetime,
    limits: MandateLimits = MandateLimits(),
    *,
    term_days: float = DEFAULT_TERM_DAYS,
) -> Mandate:
    require_slug(mandate_id, "mandate id")
    if term_days <= 0:
        raise MandateError(f"term must be positive, got {term_days!r}")
    return Mandate(
        id=mandate_id,
        granted_by=granted_by,
        granted_at=format_ts(now),
        expires_at=format_ts(now + timedelta(days=term_days)),
        **{f.name: getattr(limits, f.name) for f in fields(MandateLimits)},
    )


def extended(mandate: Mandate, now: datetime, days: float) -> Mandate:
    """Push expiry `days` past the later of now and the current expiry."""
    if days <= 0:
        raise MandateError(f"extension must be positive, got {days!r}")
    base = max(now, parse_ts(mandate.expires_at, strict=True))
    return replace(mandate, expires_at=format_ts(base + timedelta(days=days)))


def stopped(mandate: Mandate) -> Mandate:
    return replace(mandate, paused=True)


def resumed(mandate: Mandate) -> Mandate:
    return replace(mandate, paused=False, breaker_open=False, breaker_reason="")


def breaker_opened(mandate: Mandate, reason: str) -> Mandate:
    return replace(mandate, breaker_open=True, breaker_reason=reason)


def is_expired(mandate: Mandate, now: datetime) -> bool:
    return now >= parse_ts(mandate.expires_at, strict=True)


# --- spend and the gate -------------------------------------------------------------

@dataclass(frozen=True)
class SpendWindows:
    day_usd: float = 0.0
    week_usd: float = 0.0


EVENT_COST_KINDS = ("judge-cost", "overrun-charge")


def _under(path: str, directory: str) -> bool:
    prefix = posixpath.normpath(directory).rstrip("/") + "/"
    return posixpath.normpath(path).startswith(prefix)


def _spend_amounts(
    cost_rows: Iterable[Mapping],
    events: Iterable[Mapping],
    mandate_dir: str,
) -> "list[tuple[object, float]]":
    amounts: "list[tuple[object, float]]" = []
    for row in cost_rows:
        if row.get("event") == "refused":
            continue
        plan_path = row.get("plan_path")
        cost = row.get("cost_usd")
        if not isinstance(plan_path, str) or isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        if _under(plan_path, mandate_dir):
            amounts.append((row.get("ts"), float(cost)))
    for event in events:
        if event.get("event") not in EVENT_COST_KINDS:
            continue
        cost = (event.get("detail") or {}).get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        amounts.append((event.get("ts"), float(cost)))
    return amounts


def item_cost_rows(cost_rows: Iterable[Mapping], item_dir: str) -> "tuple[float, int]":
    """Total cost and row count of the cost rows whose plan path lies under one item directory."""
    total, count = 0.0, 0
    for row in cost_rows:
        if row.get("event") == "refused":
            continue
        plan_path, cost = row.get("plan_path"), row.get("cost_usd")
        if not isinstance(plan_path, str) or isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        if _under(plan_path, item_dir):
            total, count = total + float(cost), count + 1
    return total, count


def compute_spend(
    cost_rows: Iterable[Mapping],
    events: Iterable[Mapping],
    mandate_dir: str,
    now: datetime,
) -> SpendWindows:
    """Spend over the rolling 24 h and 7 x 24 h windows ending at `now`.

    Cost rows count only when their plan_path lies under `mandate_dir`; judge-cost and
    overrun-charge events count wherever they are. A row whose timestamp cannot be read
    counts in both windows -- an unreadable record must not shrink a spend cap.
    """
    day_start = now - timedelta(hours=DAY_HOURS)
    week_start = now - timedelta(hours=WEEK_HOURS)
    day = week = 0.0
    for ts, amount in _spend_amounts(cost_rows, events, mandate_dir):
        moment = parse_ts(ts)
        if moment is None or moment > day_start:
            day += amount
        if moment is None or moment > week_start:
            week += amount
    return SpendWindows(day_usd=day, week_usd=week)


@dataclass(frozen=True)
class GateResult:
    may_start: bool
    reasons: "tuple[str, ...]" = ()


def gate(
    mandate: "Mandate | None",
    now: datetime,
    spend: SpendWindows,
    cycle_elapsed_minutes: float = 0.0,
) -> GateResult:
    """May a cycle (or the next step of one) start? Every refusal reason is reported."""
    if mandate is None:
        return GateResult(False, ("no-mandate",))
    reasons = []
    if is_expired(mandate, now):
        reasons.append("expired")
    if mandate.paused:
        reasons.append("paused")
    if mandate.breaker_open:
        reasons.append("breaker-open")
    if spend.day_usd >= mandate.daily_usd:
        reasons.append("daily-budget")
    if spend.week_usd >= mandate.weekly_usd:
        reasons.append("weekly-budget")
    if cycle_elapsed_minutes >= mandate.cycle_minutes_cap:
        reasons.append("cycle-minutes-cap")
    return GateResult(not reasons, tuple(reasons))


# --- issues: normalization, open-PR references, eligibility, triage, selection ---------

def normalize_issue(raw: Mapping) -> dict:
    """`gh issue list --json number,labels,author,createdAt` row -> {number, labels, author, created_at}."""
    labels = [(item.get("name") if isinstance(item, Mapping) else item) for item in raw.get("labels") or []]
    author = raw.get("author")
    login = author.get("login") if isinstance(author, Mapping) else author
    return {
        "number": int(raw["number"]),
        "labels": [str(name) for name in labels if name],
        "author": str(login or ""),
        "created_at": raw.get("createdAt") or raw.get("created_at") or "",
    }


_ISSUE_TOKEN = re.compile(r"(?<!\w)#(\d+)(?!\w)")
_MANDATE_BRANCH = re.compile(r"^mandate/(\d+)-")


def pr_referenced_issues(prs: Iterable[Mapping]) -> "set[int]":
    """Issue numbers some open PR references: `#n` as a token in title or body, or a
    head branch `mandate/<n>-*`."""
    referenced: "set[int]" = set()
    for pr in prs:
        for text in (pr.get("title"), pr.get("body")):
            referenced.update(int(m) for m in _ISSUE_TOKEN.findall(text or ""))
        branch = _MANDATE_BRANCH.match(pr.get("headRefName") or "")
        if branch:
            referenced.add(int(branch.group(1)))
    return referenced


@dataclass(frozen=True)
class Candidate:
    issue: int
    labels: "tuple[str, ...]"
    eligible: bool
    reason: str
    auto_ok_source: "str | None" = None
    created_at: str = ""


def _in_domain(labels: Sequence[str]) -> bool:
    return any(name in DOMAIN_LABELS for name in labels)


def _latest_cycle_label_row(label_rows: Iterable[Mapping], issue: int, label: str) -> "Mapping | None":
    latest = None
    latest_ts = None
    for row in label_rows:
        if row.get("issue") != issue or row.get("label") != label or row.get("by") != "cycle":
            continue
        moment = parse_ts(row.get("ts"))
        if latest is None or (moment is not None and (latest_ts is None or moment >= latest_ts)):
            latest, latest_ts = row, moment
    return latest


def evaluate_candidate(
    issue: Mapping,
    *,
    owner: str,
    labels_cfg: Mapping[str, str],
    label_rows: Sequence[Mapping],
    digests: Mapping[str, Mapping],
    pr_refs: "set[int]",
    veto_window_hours: float,
    now: datetime,
) -> Candidate:
    """May this issue be taken in this cycle, and if not, why not.

    `issue` is the normalized shape of `normalize_issue`. `label_rows` is the cycle's own
    label log: the repo owner's account applies every label, so only that log can tell a
    label the user set from one the cycle set. `digests` maps cycle_id to the delivery
    record of that cycle's digest.
    """
    number = issue["number"]
    names = tuple(issue.get("labels") or ())
    created_at = issue.get("created_at") or ""

    def result(eligible: bool, reason: str, source: "str | None" = None) -> Candidate:
        return Candidate(number, names, eligible, reason, source, created_at)

    ok, no, veto = labels_cfg["ok"], labels_cfg["no"], labels_cfg["veto"]
    if issue.get("author") != owner:
        return result(False, "not-owner-authored")
    if not _in_domain(names):
        return result(False, "out-of-domain")
    if veto in names:
        return result(False, "vetoed")
    if UMBRELLA_LABEL in names:
        return result(False, "process-umbrella")
    if number in pr_refs:
        return result(False, "open-pr")
    if no in names:
        return result(False, "auto-no")
    if ok not in names:
        return result(False, "not-auto-ok")
    applied = _latest_cycle_label_row(label_rows, number, ok)
    if applied is None:
        return result(True, "user-set", "user")
    if SEVERITY_HIGH_LABEL in names:
        return result(False, "severity-high-cycle-set", "cycle")
    delivery = digests.get(str(applied.get("cycle_id")))
    delivered_at = parse_ts((delivery or {}).get("delivered_at"))
    if (
        delivery is None
        or delivery.get("ok") is not True
        or delivery.get("notifier") in (None, "", "file")
        or delivered_at is None
    ):
        return result(False, "digest-not-delivered", "cycle")
    if now < delivered_at + timedelta(hours=veto_window_hours):
        return result(False, "veto-window", "cycle")
    return result(True, "cycle-set-veto-window-passed", "cycle")


def _age_key(item) -> "tuple[str, int]":
    created, number = (
        (item.created_at, item.issue) if isinstance(item, Candidate)
        else (item.get("created_at") or "", item["number"])
    )
    moment = parse_ts(created)
    return (format_ts(moment) if moment else "", number)


def triage_candidates(
    issues: Iterable[Mapping], *, owner: str, labels_cfg: Mapping[str, str],
) -> "list[dict]":
    """Issues the triage judge may see, oldest first: owner-authored, in the domain, and
    carrying none of auto-ok, auto-no, no-auto, severity:high, process-umbrella."""
    barred = {labels_cfg["ok"], labels_cfg["no"], labels_cfg["veto"], SEVERITY_HIGH_LABEL, UMBRELLA_LABEL}
    seen = [
        issue for issue in issues
        if issue.get("author") == owner
        and _in_domain(issue.get("labels") or ())
        and not barred.intersection(issue.get("labels") or ())
    ]
    return sorted(seen, key=_age_key)


def select_take(
    candidates: Iterable[Candidate], *, max_items: int, rank: "Sequence[int] | None" = None,
) -> "list[int]":
    """Eligible candidates in taking order, cut to `max_items`: the board rank when one is
    given (unranked ones after, oldest first), else oldest first."""
    eligible = sorted((c for c in candidates if c.eligible), key=_age_key)
    if rank:
        position = {number: index for index, number in enumerate(rank)}
        eligible.sort(key=lambda c: position.get(c.issue, len(position)))
    return [c.issue for c in eligible[: max(0, int(max_items))]]


# --- the test gate, overrun, outcome -------------------------------------------------

def failing_ids(baseline_passed: Iterable[str], item_passed: Iterable[str]) -> "list[str]":
    """Test ids that passed on the baseline and did not pass on the item (a vanished
    test or a collection error is a failure of its ids)."""
    return sorted(set(baseline_passed) - set(item_passed))


def passes(baseline_passed: Iterable[str], item_passed: Iterable[str]) -> bool:
    return not failing_ids(baseline_passed, item_passed)


def passes_after_rerun(
    baseline_passed: Iterable[str], item_passed: Iterable[str], rerun_passed: Iterable[str],
) -> bool:
    """The item verdict once the ids that failed the first run have been rerun once."""
    return passes(baseline_passed, set(item_passed) | set(rerun_passed))


@dataclass(frozen=True)
class OverrunVerdict:
    overrun: bool
    reasons: "tuple[str, ...]" = ()


def check_overrun(
    item_usd: float, elapsed_minutes: float, *, usd_cap: float, minutes_cap: float,
) -> OverrunVerdict:
    reasons = []
    if item_usd > usd_cap:
        reasons.append("item-usd-cap")
    if elapsed_minutes > minutes_cap:
        reasons.append("item-minutes-cap")
    return OverrunVerdict(bool(reasons), tuple(reasons))


def killed_spawn_charge(has_cost_row: bool, usd_cap: float) -> float:
    """What a killed spawn costs the spend windows: its cost row if it left one (counted
    already), else the whole per-item cap."""
    return 0.0 if has_cost_row else float(usd_cap)


def opens_breaker(outcome: str) -> bool:
    """Only a failed item opens the breaker; an unknown outcome is treated as one."""
    return outcome not in ("pr-opened", "overrun", "declined", "limit")


# --- the constitution classifier ------------------------------------------------------

@dataclass(frozen=True)
class ConstitutionVerdict:
    reject: bool
    offending: "tuple[tuple[str, str], ...]" = ()


_OCTAL = re.compile(r"\\([0-7]{3})")
_SIMPLE_ESCAPES = {"\\\\": "\\", '\\"': '"', "\\t": "\t", "\\n": "\n"}


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of a path that has unusual characters."""
    if not (len(path) >= 2 and path.startswith('"') and path.endswith('"')):
        return path
    body = path[1:-1]
    raw = bytearray()
    index = 0
    while index < len(body):
        chunk = body[index:index + 4]
        if _OCTAL.match(chunk):
            raw.append(int(chunk[1:4], 8))
            index += 4
        elif body[index:index + 2] in _SIMPLE_ESCAPES:
            raw.extend(_SIMPLE_ESCAPES[body[index:index + 2]].encode("utf-8"))
            index += 2
        else:
            raw.extend(body[index].encode("utf-8"))
            index += 1
    return raw.decode("utf-8", errors="replace")


def _diff_paths(line: str) -> "list[str]":
    """Every path a `git diff --name-status` line names: both sides of a rename or copy.

    git separates the status from the paths with tabs. A line without one is not in that
    shape, so rather than guess which field is the path, the whole line and each
    whitespace-separated token are checked -- over-matching only rejects more.
    """
    fields_ = line.rstrip("\r\n").split("\t")
    if len(fields_) >= 2:
        return [_unquote(p) for p in fields_[1:] if p]
    text = line.strip()
    if not text:
        return []
    return [_unquote(text)] + [_unquote(token) for token in text.split() if token != text]


def _normal(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/")).lstrip("/").casefold()


def _entry_matches(path: str, entry: str) -> bool:
    pattern = entry.casefold()
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return path == pattern or path.startswith(pattern + "/")


def classify_diff(name_status_lines: Iterable[str], constitution: Iterable[str]) -> ConstitutionVerdict:
    """Reject a diff that touches a constitution path on either side of a rename or copy,
    or any file named settings*.json. The settings rule is not part of the removable list."""
    entries = list(constitution)
    offending: "list[tuple[str, str]]" = []
    for line in name_status_lines:
        for raw in _diff_paths(line):
            path = _normal(raw)
            if not path or path == ".":
                continue
            hit = next((e for e in entries if _entry_matches(path, e)), None)
            if hit is None and fnmatch.fnmatchcase(posixpath.basename(path), SETTINGS_GLOB):
                hit = SETTINGS_GLOB
            if hit is not None:
                offending.append((raw, hit))
    return ConstitutionVerdict(bool(offending), tuple(offending))
