#!/usr/bin/env python3
"""Shared "did I open a code review this session, and is it being driven?" scan.

Difficulty removed (recorded 2026-07-29): the author opens a review request and
moves on — a red CI check or an unanswered reviewer comment then sits until the
user says *"the tests failed in your review"*. The norm (author drives an open
review to MERGEABLE unprompted, monitored by a zero-token detached poller) lives
in `memory-global/leaves/review-accompanies-code.md` and
`memory-global/leaves/long-job-monitoring.md` § Generalization, but prose alone
kept being missed. This module is the deterministic half of the structural fix:
whether a review the agent AUTHORED this session has a monitor armed for it is
decidable from the transcript plus the monitored-reviews registry — no judgment
needed. `hook-review-mergeable-guardian.py` is its consumer; the logic lives
here because a hyphenated hook filename cannot be imported (the same reason
`long_job_detect.py` exists).

Vendor-neutral by design, in two places:

- A URL is a review URL when its PATH carries a review/pull/merge-request
  segment followed by a numeric id — the generic surface shared by every
  review platform. No host is hard-coded.
- The "I authored it" evidence is a *create verb* seen in a Bash command, and
  the verb list is operator-configurable via `review_open_verbs=` in the config
  root's `agent-identity.local`. Core ships a generic default set; an org whose
  review CLI uses different wording configures it there rather than patching
  this file.

Detection is deliberately STRICTER than the sibling run-URL scan, which nudges
on any run URL merely seen. Nagging about every review the agent happens to
*read* is the failure mode to avoid, so authorship is correlated PER REVIEW,
not per session: a review counts only when its identity is tied to the create
command itself — the URL printed by that command's own output (paired by
`tool_use_id`, or by the author→tool turn when the transcript carries no ids),
named in the command line, or referenced there by its bare numeric id. A
review whose URL only ever arrived through an unrelated status call is never
returned, even in a session that did open some other review.

The verbs are matched as whole tokens, never as substrings, and Core's default
set carries no bare `publish`: `npm publish` must not make the agent the author
of every review it looked at that day.

Every function is fail-open: bad input yields an empty result, never an
exception.
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transcript_read  # noqa: E402

# Any http(s) URL, minus trailing markdown / quote delimiters.
_URL_RE = re.compile(r"https?://[^\s)\]}>\"'`]+", re.IGNORECASE)

# Sentence punctuation the URL regex cannot exclude (a URL may legally contain
# these mid-path), trimmed from the END of a match. Tool output routinely reads
# "review created: <url>." or "status of <url>: green", and without this trim
# the trailing character defeats the numeric-id match and the review goes
# undetected — silently, which is the expensive direction for this guard.
_TRAILING_PUNCT = ".,;:!?"

# A URL is a "review URL" when its path carries one of these segments followed
# by a numeric id. Vendor-neutral: the segment vocabulary is what review
# platforms share; the host is never inspected.
_REVIEW_SEGMENT_RE = re.compile(
    r"/(review|review-requests|pull|pulls|pull-requests|merge_requests"
    r"|merge-requests|prs?)/(\d+)(?=/|$)",
    re.IGNORECASE,
)

# Core ships a generic verb vocabulary rather than none (unlike
# long_job_detect's orchestrator names, which have no vendor-neutral form):
# these phrases are the shape review CLIs share, so an unconfigured machine
# still detects the common cases.
#
# Every entry is review-QUALIFIED. A bare verb like `publish` is not: it is the
# ordinary word for shipping a package (`npm publish`, `cargo publish`), and as
# a default it makes any session that released anything claim authorship of the
# reviews it merely read. A platform whose CLI really does publish a review
# with one word configures `review_open_verbs=` rather than getting it here.
DEFAULT_REVIEW_OPEN_VERBS: tuple[str, ...] = (
    "pr create",
    "pull-request create",
    "pull request create",
    "review create",
    "merge-request create",
    "merge request create",
    "pr publish",
    "review publish",
    "pull-request publish",
    "pull request publish",
    "merge-request publish",
    "merge request publish",
)

# Word characters for verb-boundary purposes. `-` counts as one so a verb never
# matches inside a longer hyphenated command name (`pr create` must not fire on
# `my-pr-create-helper`).
_VERB_BOUNDARY = r"[0-9A-Za-z_-]"

# A standalone numeric argument of a create command — how a "publish the draft
# review I already have" invocation names its review (`... review publish 42`).
_NUMERIC_TOKEN_RE = re.compile(r"^\d+$")


def review_identity(url: str) -> str | None:
    """Stable identity for a review URL, or None when it is not a review URL.

    Collapses the segment spelling so the same review referred to as
    ``/review/42``, ``/pull/42`` or ``/merge_requests/42`` on one host is one
    identity, while keeping the path prefix (a repo/project path on a
    multi-repo host) so two different repos' #42 stay distinct. Any trailing
    view segment (``/files``, ``/checks``), query and fragment are dropped.
    """
    try:
        m = re.match(r"(https?://[^?#]*)", url or "", re.IGNORECASE)
        core = (m.group(1) if m else (url or "")).rstrip("/")
        parts = re.match(r"https?://([^/]+)(/.*)?$", core, re.IGNORECASE)
        if not parts:
            return None
        host = parts.group(1)
        path = parts.group(2) or ""
        seg = _REVIEW_SEGMENT_RE.search(path)
        if not seg:
            return None
        prefix = path[: seg.start()].rstrip("/")
        return f"{host}{prefix}/{seg.group(2)}".lower()
    except Exception:
        return None


def review_urls(text: str) -> dict:
    """Map {review_identity -> a representative URL} for review URLs in text.

    Trailing sentence punctuation is trimmed before matching, so a review URL
    that ends a sentence or precedes a colon is still recognized.
    """
    out: dict = {}
    if not text:
        return out
    try:
        for m in _URL_RE.finditer(text):
            url = m.group(0).rstrip(_TRAILING_PUNCT)
            ident = review_identity(url)
            if ident and ident not in out:
                out[ident] = url
    except Exception:
        return {}
    return out


def review_open_verbs(identity_path=None) -> tuple[str, ...]:
    """Resolve the create-verb list.

    Reads `review_open_verbs=` from the resolved `agent-identity.local`,
    falling back to DEFAULT_REVIEW_OPEN_VERBS. Entries are comma-separated so a
    multi-word phrase survives; a value carrying no comma at all is split on
    whitespace instead, so a single-word list reads naturally too. Fail-open:
    any error yields the default.
    """
    try:
        from difficulty_channel.authority import LOCAL_IDENTITY_PATH, read_local_identity

        raw = read_local_identity(identity_path or LOCAL_IDENTITY_PATH).get(
            "review_open_verbs", ""
        ).strip()
        if not raw:
            return DEFAULT_REVIEW_OPEN_VERBS
        pieces = raw.split(",") if "," in raw else raw.split()
        verbs = tuple(
            v for v in (" ".join(p.split()).lower() for p in pieces) if v
        )
        return verbs or DEFAULT_REVIEW_OPEN_VERBS
    except Exception:
        return DEFAULT_REVIEW_OPEN_VERBS


@functools.lru_cache(maxsize=64)
def _verb_pattern(verbs: tuple):
    """Compile a verb list into one whole-token alternation, or None if empty.

    Substring matching is what made `publish` fire on `npm publish`; a verb
    phrase must match as a sequence of whole tokens, so it can never be found
    inside a longer word.
    """
    parts = []
    for verb in verbs:
        tokens = [re.escape(t) for t in str(verb).split() if t]
        if tokens:
            parts.append(r"\s+".join(tokens))
    if not parts:
        return None
    return re.compile(
        f"(?<!{_VERB_BOUNDARY})(?:" + "|".join(parts) + f")(?!{_VERB_BOUNDARY})"
    )


def opens_review(cmd: str, verbs=None) -> bool:
    """True when a Bash command carries a review-create verb as whole tokens."""
    try:
        normalized = " ".join((cmd or "").split()).lower()
        if not normalized:
            return False
        resolved = review_open_verbs() if verbs is None else verbs
        pattern = _verb_pattern(tuple(resolved))
        return bool(pattern and pattern.search(normalized))
    except Exception:
        return False


def referenced_ids(cmd: str, verbs=None) -> set:
    """Numeric review ids a create command names, e.g. `... review publish 42`.

    Only the argument standing immediately after a create verb counts. Every
    other number in the line — a flag's value (`--retries 9008`), a digit
    inside a quoted title — is not the review being acted on, and treating it
    as one would re-open the same false-authorship hole this module closed.
    """
    try:
        normalized = " ".join((cmd or "").split()).lower()
        resolved = review_open_verbs() if verbs is None else verbs
        pattern = _verb_pattern(tuple(resolved))
        if not normalized or pattern is None:
            return set()
        out: set = set()
        for match in pattern.finditer(normalized):
            tail = normalized[match.end():].split()
            if not tail:
                continue
            bare = tail[0].strip("'\"`,;:()[]{}")
            if _NUMERIC_TOKEN_RE.match(bare):
                out.add(bare)
        return out
    except Exception:
        return set()


def authored_reviews(entries, verbs=None) -> dict:
    """Map {identity -> sample URL} for reviews this session AUTHORED.

    Authorship is correlated PER REVIEW. A review qualifies when its identity
    is tied to a create-verb command in one of three ways:

    - its URL was printed by that command — paired through `tool_use_id` when
      the transcript carries ids, else by the author→tool turn (the create
      command's own output is where a newly opened review's URL normally
      appears, so this covers the common case without pairing anything else);
    - its URL appears in the create command line itself;
    - its bare numeric id stands directly after the create verb there
      (`review publish 42`), matched against the reviews seen anywhere this
      session.

    A review whose URL only ever arrived via an unrelated read/status call is
    NOT returned, even when the session opened a different review — nagging
    about every review merely read is the failure this filter exists to
    prevent.
    """
    try:
        resolved = review_open_verbs() if verbs is None else verbs
        authored: dict = {}
        seen: dict = {}
        create_call_ids: set = set()
        create_numeric_ids: set = set()
        turn_created = False
        for entry in entries or []:
            msg = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "assistant":
                turn_created = False
                for use in transcript_read.bash_tool_uses(msg):
                    cmd = use.get("command") or ""
                    if not opens_review(cmd, resolved):
                        continue
                    turn_created = True
                    if use.get("id"):
                        create_call_ids.add(use["id"])
                    for ident, url in review_urls(cmd).items():
                        authored.setdefault(ident, url)
                    create_numeric_ids |= referenced_ids(cmd)
            elif role == "user":
                for block in transcript_read.tool_results(msg):
                    call_id = block.get("tool_use_id")
                    from_create = (
                        call_id in create_call_ids if call_id else turn_created
                    )
                    for ident, url in review_urls(block.get("text") or "").items():
                        seen.setdefault(ident, url)
                        if from_create:
                            authored.setdefault(ident, url)
        for ident, url in seen.items():
            if ident.rsplit("/", 1)[-1] in create_numeric_ids:
                authored.setdefault(ident, url)
        return authored
    except Exception:
        return {}


def registry_path() -> Path:
    """The monitored-reviews registry `<agent-home>/monitored-reviews.json`,
    written by `review-monitor.sh` when a poller is armed. Honors a
    `$CLAUDE_MONITORED_REVIEWS` override, mirroring how `config_root` exposes
    an override for the other runtime state files."""
    override = os.environ.get("CLAUDE_MONITORED_REVIEWS")
    if override:
        return Path(override).expanduser()
    from lib.config_root import agent_home

    return agent_home() / "monitored-reviews.json"


def armed_identities(path=None) -> set:
    """Review identities that have a monitor armed. Missing or malformed
    registry → empty set (fail-open: nudge rather than stay silent)."""
    try:
        resolved = Path(path) if path is not None else registry_path()
        data = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return set()
        return {str(k).lower() for k in data}
    except Exception:
        return set()


@contextlib.contextmanager
def _registry_lock(resolved: Path):
    """Serialize the registry's read-modify-write across pollers.

    Two monitors armed seconds apart otherwise interleave read → mutate →
    write-whole-file and one silently drops the other's entry, which the
    guardian then reads as "not armed" and nudges about a review that IS being
    driven. Fail-open: if the lock cannot be taken (no fcntl, unwritable dir)
    the write still proceeds unserialized — a lost update is better than a
    poller that refuses to register.
    """
    handle = None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        handle = open(resolved.with_suffix(resolved.suffix + ".lock"), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except Exception:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def registry_upsert(identity, out, pid, status, path=None) -> bool:
    """Record/refresh one review's monitor entry. Returns False on any failure —
    the poller must keep polling even if the registry is unwritable (the
    registry drives a nudge, not the monitoring itself).

    The key is `review_identity(identity)` whenever the argument is a review
    URL, which is exactly what `authored_reviews` derives from that same URL —
    the two key spaces MUST agree, or arming a monitor never silences the
    guardian. A bare platform id has no derivable identity and is stored as
    itself: usable by the probe, but unmatchable by the guardian (see
    `review-monitor.sh --help`).
    """
    try:
        resolved = Path(path) if path is not None else registry_path()
        key = (review_identity(identity) or str(identity)).lower()
        with _registry_lock(resolved):
            try:
                data = json.loads(resolved.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data[key] = {"out": str(out), "status": str(status), "pid": int(pid)}
            resolved.parent.mkdir(parents=True, exist_ok=True)
            tmp = resolved.with_suffix(resolved.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(resolved)
        return True
    except Exception:
        return False


def _cli(argv) -> int:
    """Tiny CLI so `review-monitor.sh` reuses this module's identity
    normalization and registry schema instead of re-deriving either in shell."""
    args = list(argv)
    if len(args) >= 2 and args[0] == "identity":
        ident = review_identity(args[1])
        if not ident:
            # Exit non-zero with no stdout so a shell caller can tell "this is
            # a review URL the guardian will match" from "this is a bare id it
            # never can" — review-monitor.sh warns on the latter.
            return 1
        print(ident)
        return 0
    if len(args) >= 2 and args[0] == "registry-upsert":
        opts = dict(zip(args[2::2], args[3::2]))
        ok = registry_upsert(
            args[1],
            opts.get("--out", ""),
            opts.get("--pid", 0),
            opts.get("--status", "running"),
            path=opts.get("--registry"),
        )
        return 0 if ok else 1
    print(
        "usage: review_open_detect.py identity <url-or-id>\n"
        "       review_open_detect.py registry-upsert <url-or-id> "
        "--out <file> --pid <n> --status running|done [--registry <file>]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
