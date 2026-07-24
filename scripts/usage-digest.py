#!/usr/bin/env python3
"""Cross-installation usage telemetry — the OPT-IN emitter (`emit` subcommand).

Mirrors the channel model of core-difficulty-digest.py: each opted-in
installation computes a COMPACT anonymized aggregate over its own local ledgers
and posts it as one comment on a single per-channel tracking sink (a private
GitHub issue, or whatever ticket the channel's adapter names). A separate
aggregator (`pull`, stage 7)
reads those comments from every channel and sums them into one rollup.

Design invariants (load-bearing, tested):

  - Opt-in DEFAULT OFF. With `usage_telemetry` unset/≠"on" in
    agent-identity.local, `emit` posts nothing — zero bytes leave the machine.
  - The payload is COUNTS-ONLY plus an anonymized installation id: never a task
    id, tracker key, path, prompt, or ticket body. A whitelist guard refuses to
    post a payload carrying any other field.
  - `period` is a DISJOINT calendar bucket (ISO week `YYYY-Www`), not a rolling
    window, so the aggregator can sum distinct periods without double-counting.
  - `installation_id` is a salted sha256 of a machine-stable id — the raw
    hostname/login never leaves the machine — stable across emits so the
    aggregator's (installation, period) dedup key holds.
  - Emit is FAIL-OPEN and off the resolve hot path: any error (no token, no
    sink, HTTP failure) prints a skip line and exits 0.

Local aggregation is REUSED from agent-stats.py (stage 3); the sink append verb
is REUSED from the difficulty_channel adapters (`add_comment`). Only the opt-in
gate, the ISO-week window, the anonymization, and the privacy envelope are new.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import secrets
import socket
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Reuse stage-3's local aggregation (hyphenated filename -> load by path). agent-stats.py
# transitively loads cost-report.py + lib.config_root at import, so those come along.
_AS_PATH = SCRIPTS_DIR / "agent-stats.py"
_as_spec = importlib.util.spec_from_file_location("agent_stats", _AS_PATH)
agent_stats = importlib.util.module_from_spec(_as_spec)
_as_spec.loader.exec_module(agent_stats)

parse_ts = agent_stats.cost_report.parse_ts

from difficulty_channel import detect  # noqa: E402
from difficulty_channel.adapters import BUILTIN_NAMES, github, load_adapter  # noqa: E402
from lib.config_root import identity_file  # noqa: E402

# Per-channel tracking sinks. Every channel resolves its sink from the agent-identity.local
# key `usage_sink_<channel>`, so an org channel wires its own sink on the machine that uses it
# and Core carries no sink identifier of anyone's. The built-in channels all speak the same
# public GitHub surface, so they share this one default. That sink MUST be a PRIVATE repo issue
# to honor the "closed" requirement; the built-in default below is empty because the account's
# fine-grained PAT cannot create a repo — the private repo is provisioned manually when an
# installation opts into telemetry (default OFF), then wired via `usage_sink_github`. An
# unconfigured sink fail-open-skips: nothing emits there until then.
USAGE_SINK_GITHUB = ""
SINK_IDENTITY_PREFIX = "usage_sink_"

# The ONLY fields allowed to leave the machine — counts + an anonymized id. The guard below
# refuses to post a payload carrying anything else (a task id / key / path would be a leak).
WHITELIST = {
    "schema",
    "period",
    "installation_id",
    "channel",
    "n_invocations",
    "n_resolved",
    "n_quality_rated",
    "n_marked_precedents",
    "mean_quality",
    "total_cost_usd",
    "n_spawns",
}


# --- identity / anonymization -------------------------------------------------

def read_identity(path: Path) -> dict:
    """Parse agent-identity.local's `key=value` lines (comments/blank lines ignored)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def installation_id(machine_id: str, salt: str) -> str:
    """Anonymized, stable installation id: a salted sha256 digest. The raw machine id
    (hostname/login) is never emitted, only this hash."""
    return hashlib.sha256(f"{machine_id}:{salt}".encode("utf-8")).hexdigest()[:16]


def _ensure_salt(identity: dict, path: Path) -> str:
    """Return the persisted anonymization salt, generating + persisting one on first use.
    Fail-soft: an unwritable identity file degrades to an ephemeral salt (still anonymized,
    only not stable across emits)."""
    salt = identity.get("usage_salt")
    if salt:
        return salt
    salt = secrets.token_hex(16)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"usage_salt={salt}\n")
    except OSError:
        pass
    return salt


# --- ISO-week disjoint buckets ------------------------------------------------

def current_iso_week(now: dt.datetime) -> str:
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def just_closed_week(now: dt.datetime) -> str:
    """The last COMPLETE ISO week (the week containing `now - 7 days`)."""
    return current_iso_week(now - dt.timedelta(days=7))


def week_bounds(period: str) -> tuple[dt.datetime, dt.datetime]:
    """[Monday 00:00, next Monday 00:00) for an ISO-week `YYYY-Www` string (naive UTC)."""
    year_s, week_s = period.split("-W")
    start = dt.datetime.fromisocalendar(int(year_s), int(week_s), 1)
    return start, start + dt.timedelta(days=7)


def _naive_utc(d: dt.datetime) -> dt.datetime:
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def window_rows(rows: list[dict], start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Rows whose `ts` falls in the half-open [start, end) week. A missing/unparseable ts
    is skipped (fail-soft), never fatal."""
    out: list[dict] = []
    for r in rows:
        ts = r.get("ts")
        if not ts:
            continue
        try:
            t = _naive_utc(parse_ts(ts))
        except (ValueError, TypeError):
            continue
        if start <= t < end:
            out.append(r)
    return out


# --- payload ------------------------------------------------------------------

def build_payload(
    task_rows: list[dict],
    policy_rows: list[dict],
    spawn_rows: list[dict],
    *,
    period: str,
    channel: str,
    installation_id: str,
) -> dict:
    """Compact counts-only aggregate for one (installation, period). Reuses agent-stats'
    aggregate; adds n_quality_rated so the aggregator can weight the mean correctly."""
    base = agent_stats.aggregate(task_rows, policy_rows, spawn_rows)
    n_quality_rated = sum(1 for r in task_rows if isinstance(r.get("quality"), (int, float)))
    return {
        "schema": "usage/v1",
        "period": period,
        "installation_id": installation_id,
        "channel": channel,
        "n_invocations": base["invocations"],
        "n_resolved": base["resolved"],
        "n_quality_rated": n_quality_rated,
        "n_marked_precedents": base["marked_precedents"],
        "mean_quality": base["mean_quality"],
        "total_cost_usd": base["cost"],
        "n_spawns": base["spawns"],
    }


def assert_counts_only(payload: dict) -> None:
    """Refuse a payload carrying any non-whitelisted field (a task id / key / path is a leak)."""
    extra = set(payload) - WHITELIST
    if extra:
        raise ValueError(f"payload carries non-whitelisted fields: {sorted(extra)}")


AGGREGATE_MARKER = "<!-- agent-usage-aggregate -->"


def format_comment(payload: dict) -> str:
    """One fenced-JSON comment body the aggregator (stage 7) can extract robustly."""
    return (
        f"{AGGREGATE_MARKER}\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n```"
    )


# --- emit ---------------------------------------------------------------------

def resolve_sink(channel: str, identity: dict, override: str | None = None) -> str:
    """This machine's tracking sink for `channel`: explicit override, else the identity key
    `usage_sink_<channel>`, else the built-in default (built-ins only; empty = unconfigured)."""
    default = USAGE_SINK_GITHUB if channel in BUILTIN_NAMES else ""
    return override or identity.get(f"{SINK_IDENTITY_PREFIX}{channel}") or default


def emit(
    *,
    identity: dict,
    channel: str,
    period: str,
    installation_id: str,
    task_rows: list[dict],
    policy_rows: list[dict],
    spawn_rows: list[dict],
    sink: str | None = None,
    github_add_comment=None,
    plugin_add_comment=None,
    http=None,
    log=None,
) -> dict:
    """Post one anonymized counts-only aggregate to this machine's channel sink — but ONLY
    when opt-in is ON. Returns a status dict and NEVER raises (fail-open)."""
    log = log or print
    if identity.get("usage_telemetry") != "on":
        log("usage-digest: opt-in OFF (usage_telemetry != 'on'); nothing emitted")
        return {"emitted": False, "reason": "opt-in-off"}
    try:
        payload = build_payload(
            task_rows, policy_rows, spawn_rows,
            period=period, channel=channel, installation_id=installation_id,
        )
        assert_counts_only(payload)
        body = format_comment(payload)
        sink = resolve_sink(channel, identity, sink)
        if not sink:
            log(f"usage-digest: no {channel} sink configured; skipped")
            return {"emitted": False, "reason": "no-sink"}
        if channel in BUILTIN_NAMES:
            (github_add_comment or github.add_comment)(sink, body, http=http)
        else:
            # Any non-built-in channel is a machine-local plugin adapter (ADR-0001 B1); an
            # unresolvable name raises and is caught below as a fail-open skip.
            (plugin_add_comment or load_adapter(channel).add_comment)(sink, body, http=http)
    except Exception as exc:  # noqa: BLE001 - fail-open by design; telemetry never blocks
        log(f"usage-digest: emit failed ({exc}); skipped")
        return {"emitted": False, "reason": f"error:{exc}"}
    log(f"usage-digest: emitted {period} aggregate to {channel} sink {sink}")
    return {"emitted": True, "channel": channel, "sink": sink, "period": period, "payload": payload}


def _detect_channel() -> str:
    return detect.detect_this_machine().channel


def cmd_emit(args) -> int:
    """Wire the real IO around emit(); always returns 0 (fail-open)."""
    try:
        identity_path = args.identity or identity_file()
        identity = read_identity(identity_path)
        now = dt.datetime.now(dt.timezone.utc)
        period = args.period or just_closed_week(now)
        channel = args.channel or identity.get("difficulty_channel") or _detect_channel()
        start, end = week_bounds(period)
        task_rows = window_rows(agent_stats.read_rows(args.task_log), start, end)
        policy_rows = window_rows(agent_stats.read_rows(args.policy_log), start, end)
        spawn_rows = window_rows(agent_stats.read_rows(args.spawn_log), start, end)
        salt = _ensure_salt(identity, identity_path)
        iid = installation_id(socket.getfqdn(), salt)
        emit(
            identity=identity, channel=channel, period=period, installation_id=iid,
            task_rows=task_rows, policy_rows=policy_rows, spawn_rows=spawn_rows,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open at the CLI boundary too
        print(f"usage-digest: emit skipped ({exc})")
    return 0


# --- pull (cross-installation aggregator) -------------------------------------
#
# Read-only. Mirrors core-difficulty-digest.py's channel-iteration shape: for each
# configured sink, list its comments, extract the well-formed usage aggregates
# (ignoring human chatter), dedup re-emitted periods, and sum the DISJOINT
# (installation, period) rows into one per-channel rollup. Writes nothing.

# Channel -> fleet segment. The built-ins all fold into one public segment; every org channel
# segments under its own name, so a rollup separates public from org installations without Core
# knowing any org's name.
PUBLIC_SEGMENT = "public"


def channel_segment(channel: str | None) -> str:
    return PUBLIC_SEGMENT if channel in BUILTIN_NAMES else (channel or "unknown")


def extract_aggregate(comment_text: str) -> dict | None:
    """Pull the fenced-JSON usage aggregate out of one comment body, or None if the
    comment is human chatter / malformed. Robust-by-construction: a shared tracking
    ticket is human-writable, so a non-aggregate comment must be skipped, never fatal."""
    if not comment_text or AGGREGATE_MARKER not in comment_text:
        return None
    _, _, after = comment_text.partition(AGGREGATE_MARKER)
    fence = after.find("```json")
    if fence == -1:
        return None
    blob = after[fence + len("```json"):]
    close = blob.find("```")
    if close == -1:
        return None
    try:
        data = json.loads(blob[:close].strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != "usage/v1":
        return None
    if not data.get("installation_id") or not data.get("period"):
        return None
    return data


def _empty_segment() -> dict:
    return {
        "installations": set(),
        "n_invocations": 0,
        "n_resolved": 0,
        "n_marked_precedents": 0,
        "n_spawns": 0,
        "total_cost_usd": 0.0,
        "n_quality_rated": 0,
        "_q_weighted_sum": 0.0,
    }


def _accumulate(seg: dict, agg: dict) -> None:
    seg["installations"].add(agg.get("installation_id"))
    seg["n_invocations"] += int(agg.get("n_invocations") or 0)
    seg["n_resolved"] += int(agg.get("n_resolved") or 0)
    seg["n_marked_precedents"] += int(agg.get("n_marked_precedents") or 0)
    seg["n_spawns"] += int(agg.get("n_spawns") or 0)
    seg["total_cost_usd"] += float(agg.get("total_cost_usd") or 0.0)
    n_rated = int(agg.get("n_quality_rated") or 0)
    mean_q = agg.get("mean_quality")
    # Weighted mean of quality is weighted by RATED-ROW count, never by invocations —
    # a high-traffic low-quality installation must not dominate the fleet mean.
    if n_rated and isinstance(mean_q, (int, float)):
        seg["n_quality_rated"] += n_rated
        seg["_q_weighted_sum"] += float(mean_q) * n_rated


def _finalize_segment(seg: dict) -> dict:
    n_rated = seg["n_quality_rated"]
    seg["n_installations"] = len(seg.pop("installations"))
    seg["mean_quality"] = round(seg["_q_weighted_sum"] / n_rated, 3) if n_rated else None
    seg["total_cost_usd"] = round(seg["total_cost_usd"], 4)
    del seg["_q_weighted_sum"]
    return seg


def rollup(aggregates: list[dict]) -> dict:
    """Dedup by (installation_id, period) keeping the LATEST comment per pair, then sum the
    disjoint rows per channel-segment and across the whole fleet, with a rated-row-weighted
    mean quality. Disjoint ISO-week periods make the cross-period sum double-count-free."""
    dedup: dict[tuple, dict] = {}
    for agg in aggregates:
        # list order is chronological, so a later occurrence is a re-emit that supersedes.
        dedup[(agg.get("installation_id"), agg.get("period"))] = agg
    rows = list(dedup.values())

    by_segment: dict[str, dict] = {}
    total = _empty_segment()
    for agg in rows:
        segment = channel_segment(agg.get("channel"))
        _accumulate(by_segment.setdefault(segment, _empty_segment()), agg)
        _accumulate(total, agg)

    return {
        "n_aggregates": len(rows),
        "by_segment": {name: _finalize_segment(seg) for name, seg in sorted(by_segment.items())},
        "total": _finalize_segment(total),
    }


def _sink_comment_texts(channel, sink, *, github_list_comments, plugin_list_comments, http, log):
    """List one sink's comments and return their text bodies; fail-soft to [] on an
    unreachable sink (a down channel degrades to the other channels' rollup, never a crash).

    `body` is the adapter contract's comment-text key (see `load_adapter`'s contract block);
    `text` is accepted as a tolerance because that is the native spelling on some trackers and
    an adapter that passes its rows through untranslated would otherwise read as all-chatter."""
    try:
        if channel in BUILTIN_NAMES:
            comments = (github_list_comments or github.list_comments)(sink, http=http)
        else:
            comments = (plugin_list_comments or load_adapter(channel).list_comments)(sink, http=http)
        return [c.get("body") or c.get("text") or "" for c in comments]
    except Exception as exc:  # noqa: BLE001 - read-only, fail-soft on an unreachable sink
        log(f"usage-digest: pull from {channel} sink failed ({exc}); skipped")
    return []


def pull(
    *,
    sinks: dict,
    github_list_comments=None,
    plugin_list_comments=None,
    http=None,
    log=None,
) -> dict:
    """Read every configured sink's comments, extract the well-formed aggregates, and roll
    them up. READ-ONLY: never posts or edits a sink. `sinks` maps channel -> sink id."""
    log = log or print
    aggregates: list[dict] = []
    for channel, sink in sinks.items():
        if not sink:
            continue
        for text in _sink_comment_texts(
            channel, sink,
            github_list_comments=github_list_comments,
            plugin_list_comments=plugin_list_comments,
            http=http, log=log,
        ):
            agg = extract_aggregate(text)
            if agg is not None:
                # Trust the payload's own channel field; fall back to the sink's channel.
                agg.setdefault("channel", channel)
                aggregates.append(agg)
    return rollup(aggregates)


def _resolve_sinks(args, identity: dict) -> dict:
    """channel -> sink id for every channel this machine knows a sink for: the github
    built-in, every `usage_sink_<channel>` identity key, then `--sink <channel>=<ref>`."""
    sinks = {"github": USAGE_SINK_GITHUB}
    for key, value in identity.items():
        if key.startswith(SINK_IDENTITY_PREFIX) and value:
            sinks[key[len(SINK_IDENTITY_PREFIX):]] = value
    for spec in getattr(args, "sink", None) or []:
        channel, _, ref = spec.partition("=")
        if channel.strip():
            sinks[channel.strip()] = ref.strip()
    return sinks


def format_rollup_markdown(result: dict) -> str:
    lines = [f"# Cross-installation usage rollup ({result['n_aggregates']} aggregate(s))", ""]

    def _seg_block(title: str, seg: dict) -> None:
        lines.append(f"## {title}")
        lines.append(f"- installations: **{seg['n_installations']}**")
        lines.append(f"- invocations: **{seg['n_invocations']}**")
        lines.append(f"- resolved: **{seg['n_resolved']}**")
        lines.append(f"- marked precedents (`solved_by_007`): **{seg['n_marked_precedents']}**")
        mq = seg["mean_quality"]
        lines.append(f"- mean quality (rated-row-weighted): **{mq if mq is not None else 'n/a'}**")
        lines.append(f"- spawns: **{seg['n_spawns']}**")
        lines.append(f"- total cost: **${seg['total_cost_usd']}**")
        lines.append("")

    _seg_block("Total (all installations)", result["total"])
    for name, seg in result["by_segment"].items():
        _seg_block(f"Segment: {name}", seg)
    return "\n".join(lines).rstrip()


def cmd_pull(args) -> int:
    """Read-only cross-installation rollup. Always returns 0 (fail-soft)."""
    identity_path = args.identity or identity_file()
    identity = read_identity(identity_path)
    result = pull(sinks=_resolve_sinks(args, identity))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_rollup_markdown(result))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit", help="post this machine's anonymized usage aggregate (opt-in)")
    e.add_argument("--period", default=None, help="ISO week YYYY-Www (default: last complete week)")
    e.add_argument("--channel", default=None, help="override channel (default: identity/detect)")
    e.add_argument("--identity", type=Path, default=None, help="agent-identity.local path override")
    e.add_argument("--task-log", type=Path, default=agent_stats.TASK_QUALITY_LOG)
    e.add_argument("--policy-log", type=Path, default=agent_stats.POLICY_LEDGER)
    e.add_argument("--spawn-log", type=Path, default=agent_stats.SPAWN_COST_LOG)
    pl = sub.add_parser("pull", help="read every channel sink and print the summed cross-installation rollup")
    pl.add_argument("--identity", type=Path, default=None, help="agent-identity.local path override")
    pl.add_argument("--sink", action="append", default=None, metavar="CHANNEL=REF",
                    help="override one channel's tracking sink (repeatable)")
    pl.add_argument("--json", action="store_true", help="emit a JSON dict instead of markdown")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.cmd == "emit":
        return cmd_emit(args)
    if args.cmd == "pull":
        return cmd_pull(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
