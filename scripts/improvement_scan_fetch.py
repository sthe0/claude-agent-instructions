"""The network reach of `improvement-scan.py sources` — and nothing else.

Difficulty removed: `improvement-scan.py` is held to the transport-purity predicate in
`scripts/tests/ast_purity.py` (no subprocess, socket, urllib, requests, http): a report-only
producer must not be able to file, dispatch or phone home. The `sources` producer has to
read feeds and ask three public APIs, so that reach lives here, in the one module allowed
to name `urllib`, and `improvement_scan_sources.py` (rules) and `improvement-scan.py`
(wiring) reach it only through the functions below. Every call takes a size cap and a
timeout, and every failure is a value `(None, error)`, never an exception: one dead feed
must not stop the others.

Only page STRUCTURE is parsed here (RSS/Atom elements, the t.me preview's message
markup); nothing in this module decides what a text means.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

FETCH_TIMEOUT_S = 20.0
FETCH_MAX_BYTES = 2_000_000
SUMMARY_CHARS = 2000
TITLE_CHARS = 120
FEED_WINDOW_DAYS = 183
OPENALEX_CALLS_PER_RUN = 20
HN_HITS_PER_PAGE = 20
USER_AGENT = "claude-agent-improvement-scan/1 (report-only reader)"

TELEGRAM_PREFIX = "https://t.me/s/"
TELEGRAM_BASIS = "telegram preview, the last ~20 posts only"

HN_ENDPOINT = "https://hn.algolia.com/api/v1/search"
GITHUB_ENDPOINT = "https://api.github.com/users/"
OPENALEX_ENDPOINT = "https://api.openalex.org/authors"

# Hosts that carry many unrelated authors: a search by this domain says nothing about the
# candidate, so the domain-keyed signal is not applicable.
SHARED_HOSTS = (
    "arxiv.org", "t.me", "github.com", "medium.com", "youtube.com", "x.com",
    "twitter.com", "reddit.com", "linkedin.com",
)


def is_http_url(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def domain_of(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def is_shared_host(domain: str) -> bool:
    return any(domain == h or domain.endswith("." + h) for h in SHARED_HOSTS)


def fetch(
    url: str, timeout_s: float = FETCH_TIMEOUT_S, max_bytes: int = FETCH_MAX_BYTES, opener=None,
) -> "tuple[bytes | None, str | None]":
    """GET `url`: `(body, None)` on success, `(None, reason)` on any failure."""
    if not is_http_url(url):
        return None, "not an http(s) url"
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout_s) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except TimeoutError:
        return None, "timeout"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    if len(body) > max_bytes:
        return None, f"oversize: more than {max_bytes} bytes"
    return body, None


def fetch_json(url: str, fetch_fn=None) -> "tuple[object | None, str | None]":
    body, error = (fetch_fn or fetch)(url)
    if body is None:
        return None, error
    try:
        return json.loads(body.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "not json"


# --- page structure -> entries ------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _TextCollector(HTMLParser):
    """Visible text of an HTML fragment, with <br> and block ends as line breaks."""

    _BLOCKS = {"p", "div", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: "list[str]" = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_text(fragment: str) -> str:
    collector = _TextCollector()
    collector.feed(fragment)
    collector.close()
    lines = (" ".join(line.split()) for line in "".join(collector.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _parse_date(raw: str) -> "datetime | None":
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(moment: "datetime | None") -> str:
    return moment.astimezone(timezone.utc).isoformat() if moment else ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, *names: str) -> str:
    for child in node:
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def _atom_link(node: ET.Element) -> str:
    chosen = ""
    for child in node:
        if _local(child.tag) == "link" and child.get("href"):
            if child.get("rel", "alternate") == "alternate":
                return child.get("href")
            chosen = chosen or child.get("href")
    return chosen


def parse_feed(data: bytes, source_id: str) -> "list[dict]":
    """RSS 2.0 `item` and Atom `entry` elements -> entries. Raises ValueError when the
    bytes are not a feed. A document type declaration is refused outright: a feed needs
    none, and entity definitions are how an untrusted XML bomb is built."""
    if re.search(rb"<!ENTITY", data, re.IGNORECASE):
        raise ValueError("feed declares entities")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"not a feed: {exc}") from exc
    entries: "list[dict]" = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        link = _atom_link(node) or _child_text(node, "link")
        identity = _child_text(node, "guid", "id") or link
        if not identity:
            continue
        summary = _child_text(node, "encoded", "content", "summary", "description")
        entries.append({
            "entry_id": _sha(identity),
            "source_id": source_id,
            "title": html_text(_child_text(node, "title"))[:TITLE_CHARS],
            "url": link,
            "published": _iso(_parse_date(_child_text(node, "pubDate", "published", "updated", "date"))),
            "summary": html_text(summary)[:SUMMARY_CHARS],
        })
    if not entries and _local(root.tag) not in ("rss", "feed", "RDF"):
        raise ValueError(f"not a feed: root element {_local(root.tag)!r}")
    return entries


class _TelegramPreview(HTMLParser):
    """Collects each `div.tgme_widget_message`: its date link, its <time> and its text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: "list[dict]" = []
        self._msg_depth = 0
        self._text_depth = 0
        self._in_date_link = False
        self._current: "dict | None" = None

    @staticmethod
    def _classes(attrs) -> "set[str]":
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag, attrs):
        classes = self._classes(attrs)
        if tag == "div":
            if self._msg_depth:
                self._msg_depth += 1
            elif "tgme_widget_message" in classes:
                self._msg_depth = 1
                self._current = {"url": "", "time": "", "text": []}
                self.messages.append(self._current)
            if self._text_depth:
                self._text_depth += 1
            elif self._msg_depth and "tgme_widget_message_text" in classes:
                self._text_depth = 1
        elif not self._msg_depth or self._current is None:
            return
        elif tag == "a" and "tgme_widget_message_date" in classes:
            self._current["url"] = dict(attrs).get("href") or ""
            self._in_date_link = True
        elif tag == "time" and self._in_date_link:
            self._current["time"] = dict(attrs).get("datetime") or ""
        elif tag == "br" and self._text_depth:
            self._current["text"].append("\n")

    def handle_endtag(self, tag):
        if tag == "div":
            if self._text_depth:
                self._text_depth -= 1
            if self._msg_depth:
                self._msg_depth -= 1
        elif tag == "a":
            self._in_date_link = False

    def handle_data(self, data):
        if self._text_depth and self._current is not None:
            self._current["text"].append(data)


def parse_telegram(data: bytes, source_id: str) -> "list[dict]":
    parser = _TelegramPreview()
    parser.feed(data.decode("utf-8", errors="replace"))
    parser.close()
    entries: "list[dict]" = []
    for message in parser.messages:
        text = html_text("".join(message["text"]))
        if not message["url"] or not text:
            continue
        entries.append({
            "entry_id": _sha(message["url"]),
            "source_id": source_id,
            "title": text.splitlines()[0][:TITLE_CHARS],
            "url": message["url"],
            "published": _iso(_parse_date(message["time"])),
            "summary": text[:SUMMARY_CHARS],
        })
    return entries


def parse_entries(data: bytes, kind: str, source_id: str) -> "list[dict]":
    if kind == "telegram":
        entries = parse_telegram(data, source_id)
        if not entries:
            raise ValueError("no preview")
        return entries
    return parse_feed(data, source_id)


def read_source(source: dict, fetch_fn=None) -> "tuple[list[dict], str | None]":
    """Fetch and parse one configured source: `(entries, None)` or `([], reason)`."""
    body, error = (fetch_fn or fetch)(source["url"])
    if body is None:
        return [], error
    try:
        return parse_entries(body, source["kind"], source["id"]), None
    except ValueError as exc:
        return [], str(exc)


# --- dossier signals ----------------------------------------------------------

def _ok(**fields) -> dict:
    return {"status": "ok", **fields}


def _error(note: str) -> dict:
    return {"status": "error", "note": note}


def _na(note: str) -> dict:
    return {"status": "n/a", "note": note}


class ExternalBudget:
    """The per-run bound on external API calls, and its OpenAlex sub-bound."""

    def __init__(self, max_calls: int, max_openalex: int = OPENALEX_CALLS_PER_RUN) -> None:
        self.max_calls = max_calls
        self.max_openalex = max_openalex
        self.calls = 0
        self.openalex_calls = 0

    def take(self, api: str) -> bool:
        if self.calls >= self.max_calls:
            return False
        if api == "openalex":
            if self.openalex_calls >= self.max_openalex:
                return False
            self.openalex_calls += 1
        self.calls += 1
        return True


def feed_signal(url: str, kind: str, now: datetime, fetch_fn=None) -> dict:
    if kind == "html":
        return _na("html page, no feed")
    source_kind = "telegram" if url.startswith(TELEGRAM_PREFIX) else kind
    body, error = (fetch_fn or fetch)(url)
    if body is None:
        return _error(error or "fetch failed")
    try:
        entries = parse_entries(body, source_kind, "dossier")
    except ValueError:
        return _ok(alive=False, entries_6mo=0)
    cutoff = now - timedelta(days=FEED_WINDOW_DAYS)
    recent = sum(1 for e in entries if (_parse_date(e["published"]) or cutoff) > cutoff)
    extra = {"basis": TELEGRAM_BASIS} if source_kind == "telegram" else {}
    return _ok(alive=True, entries_6mo=recent, **extra)


def hn_signal(domain: str, budget: ExternalBudget, fetch_fn=None) -> dict:
    if not domain:
        return _na("no domain")
    if is_shared_host(domain):
        return _na("shared host")
    if not budget.take("hn"):
        return _error("call bound")
    query = urllib.parse.urlencode({
        "query": domain, "restrictSearchableAttributes": "url", "hitsPerPage": HN_HITS_PER_PAGE,
    })
    payload, error = fetch_json(f"{HN_ENDPOINT}?{query}", fetch_fn)
    if payload is None:
        return _error(error or "fetch failed")
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        return _error("unexpected response")
    points = sum(h.get("points") or 0 for h in hits if isinstance(h, dict))
    return _ok(story_count=payload.get("nbHits", len(hits)), points_top_n=points, n=len(hits))


def github_signal(login: "str | None", budget: ExternalBudget, fetch_fn=None) -> dict:
    if not login:
        return _na("no login")
    if not budget.take("github"):
        return _error("call bound")
    payload, error = fetch_json(GITHUB_ENDPOINT + urllib.parse.quote(login, safe=""), fetch_fn)
    if payload is None:
        return _error(error or "fetch failed")
    if not isinstance(payload, dict) or "followers" not in payload:
        return _error("unexpected response")
    return _ok(login=payload.get("login", login), followers=payload["followers"])


def openalex_signal(author: "str | None", budget: ExternalBudget, fetch_fn=None) -> dict:
    if not author:
        return _na("no author")
    if not budget.take("openalex"):
        return _error("call bound")
    payload, error = fetch_json(
        f"{OPENALEX_ENDPOINT}?{urllib.parse.urlencode({'search': author})}", fetch_fn
    )
    if payload is None:
        return _error(error or "fetch failed")
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return _error("unexpected response")
    if not results:
        return _na("no academic record")
    top = results[0] if isinstance(results[0], dict) else {}
    stats = top.get("summary_stats") or {}
    return _ok(
        matched=top.get("display_name", ""), h_index=stats.get("h_index"),
        cited_by_count=top.get("cited_by_count"),
    )
