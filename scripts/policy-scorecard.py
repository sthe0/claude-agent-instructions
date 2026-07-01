#!/usr/bin/env python3
"""Policy effectiveness & efficiency scorecard.

Standing instrument that tracks the model / sub-agent invocation policy over
time along two axes — efficiency (token cost, $, the user's attention) and
effectiveness (proxies for task-resolution quality) — so that
"policy -> measured outcome -> policy adjustment" becomes a closed loop instead
of a hand-computed one-off `jq` audit. See
memory-global/leaves/policy-effectiveness-tracking.md.

Data sources, per session:
  - main transcript  ~/.claude/projects/<project>/<session>.jsonl
  - sub-agent transcripts  <project>/<session>/subagents/*.jsonl
The pricing / usage / attention helpers are imported from cost-report.py (no
copy-paste): the per-model price table, token_cost(), parse_ts(), the JSONL
iterator, the interrupt sentinel and the correction regex.

A per-session ledger (~/.local/log/claude-policy-ledger.jsonl, one JSON row per
session, upsert keyed by session_id) accumulates the measurements cheaply: a
session is re-scanned only when its transcript mtime grew, and a manual
quality_rating attached via `rate` survives re-scans. Trend is then a diff of
two equal windows over the ledger.

Modes:
  policy-scorecard.py [--days N] [--project P]   upsert in-window rows, print
                                                 the markdown scorecard
  policy-scorecard.py --ledger-only [--days N]   upsert only (for the hook)
  policy-scorecard.py rate <session_id> <1-5> [--note "..."]
                                                 attach a manual quality rating
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# --- reuse cost-report.py (hyphenated filename -> load by path, no copy-paste) ---
_CR_PATH = Path(__file__).resolve().parent / "cost-report.py"
_spec = importlib.util.spec_from_file_location("cost_report", _CR_PATH)
cost_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cost_report)

token_cost = cost_report.token_cost
parse_ts = cost_report.parse_ts
_iter_jsonl = cost_report._iter_jsonl
_msg_text = cost_report._msg_text
_is_tool_result = cost_report._is_tool_result
INTERRUPT_SENTINEL = cost_report.INTERRUPT_SENTINEL
CORRECTION_RE = cost_report.CORRECTION_RE
PRICING = cost_report.PRICING_USD_PER_MTOK

# System root (resolved via config_root) for transcripts
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config_root import agent_home
PROJECTS_DIR = agent_home() / "projects"
LEDGER = Path.home() / ".local" / "log" / "claude-policy-ledger.jsonl"
# Written by agentctl cli._log_gate: one {ts, session, node, gate, blockers,
# passed} row per gate evaluation. Read-only here.
GATE_LOG = Path.home() / ".claude" / "agentctl" / "gate-log.jsonl"

MODEL_KEYS = ("opus", "sonnet", "haiku")
USAGE_FIELDS = (
    ("in", "input_tokens"),
    ("out", "output_tokens"),
    ("cache_read", "cache_read_input_tokens"),
    ("cache_create", "cache_creation_input_tokens"),
)

# Mechanical Bash commands (first token) that count toward a missed-delegation
# cluster — pure retrieval / polling that belongs on a cheap sub-agent.
_MECH_BASH_FIRST = re.compile(
    r"^\s*(?:cat|grep|rg|tail|head|sed|awk|less|more|wc|jq|yt)\b")
_CURL_POLL = re.compile(r"\bcurl\b.*\b(?:poll|--retry|sleep|while)\b")
MECH_TOOLS = {"Read", "Grep", "Glob"}
AGENT_TOOLS = {"Agent", "Task"}
CLUSTER_MIN = 8  # >= this many consecutive mechanical main-thread calls

# A user prompt that confirms the task is resolved (effectiveness proxy).
RESOLUTION_RE = re.compile(
    r"реш(?:ен|ён|и)|так и оставим|подтвержда|готово|all good|"
    r"\bresolved\b|looks good|считаем",
    re.IGNORECASE)
# Non-clean sub-agent return markers seen in a tool_result.
SUBAGENT_FAIL_RE = re.compile(r"\b(?:MALFORMED|INCOMPLETE|ESCALATE):")


def _model_key(model: str | None) -> str:
    m = (model or "").lower()
    for k in MODEL_KEYS:
        if k in m:
            return k
    return "opus"  # main thread / unknown defaults to opus


def _resolved_spawn_model(tool_use: dict) -> str:
    """The model a spawn actually ran on: explicit model: > Explore->haiku > opus."""
    inp = tool_use.get("input") or {}
    if inp.get("model"):
        return _model_key(inp.get("model"))
    if (inp.get("subagent_type") or "") == "Explore":
        return "haiku"
    return "opus"  # inherits the opus parent


def _empty_model_tokens() -> dict:
    return {k: {f: 0 for f, _ in USAGE_FIELDS} for k in MODEL_KEYS}


def _add_usage(model_tokens: dict, usage: dict, model: str | None) -> None:
    bucket = model_tokens[_model_key(model)]
    for short, raw in USAGE_FIELDS:
        bucket[short] += int(usage.get(raw, 0) or 0)


def _cache_read_cost(model_tokens: dict) -> float:
    total = 0.0
    for k in MODEL_KEYS:
        total += model_tokens[k]["cache_read"] * PRICING[k]["cache_read"]
    return total / 1_000_000


def _scan_session(main_file: Path) -> dict | None:
    """Scan one session (main transcript + its sub-agent transcripts) -> ledger row."""
    session_id = main_file.stem
    model_tokens = _empty_model_tokens()
    cost = 0.0
    spawns = Counter()       # resolved model -> count
    spawns_total = inherit_opus = no_explicit_model = 0
    main_read_bash = 0
    clusters = 0
    run = 0                  # current consecutive-mechanical run length
    askq = prompts = interrupts = corrections = 0
    replans = overcome_difficulty = subagent_failures = 0
    edits_per_path: Counter = Counter()
    resolution_confirmed = 0
    timestamps: list[dt.datetime] = []

    for d in _iter_jsonl(main_file):
        ts = d.get("timestamp") or (d.get("message") or {}).get("ts")
        if isinstance(ts, str):
            try:
                timestamps.append(parse_ts(ts))
            except ValueError:
                pass
        typ = d.get("type")
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        if typ == "assistant":
            usage = msg.get("usage")
            if usage:
                _add_usage(model_tokens, usage, msg.get("model"))
                cost += token_cost(usage, msg.get("model"))
            for c in (msg.get("content") or []):
                if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                    continue
                name = c.get("name")
                is_mech = False
                if name in AGENT_TOOLS:
                    spawns_total += 1
                    rm = _resolved_spawn_model(c)
                    spawns[rm] += 1
                    if not (c.get("input") or {}).get("model"):
                        no_explicit_model += 1
                        if rm == "opus":
                            inherit_opus += 1
                    run = 0  # delegation breaks any cluster
                elif name == "AskUserQuestion":
                    askq += 1
                elif name == "Skill":
                    if "overcome-difficulty" in json.dumps(c.get("input") or {}):
                        overcome_difficulty += 1
                elif name in ("Edit", "Write", "NotebookEdit"):
                    fp = (c.get("input") or {}).get("file_path")
                    if fp:
                        edits_per_path[fp] += 1
                elif name in ("Read", "Bash"):
                    main_read_bash += 1
                    if name == "Read":
                        is_mech = True
                    else:
                        cmd = (c.get("input") or {}).get("command", "") or ""
                        is_mech = bool(_MECH_BASH_FIRST.search(cmd)
                                       or _CURL_POLL.search(cmd))
                elif name in MECH_TOOLS:
                    is_mech = True
                # cluster accounting
                if is_mech:
                    run += 1
                elif name not in AGENT_TOOLS:
                    if run >= CLUSTER_MIN:
                        clusters += 1
                    run = 0
        elif typ == "user":
            content = msg.get("content")
            if _is_tool_result(content):
                text = _msg_text(content) if isinstance(content, str) else ""
                # tool_result text lives inside the list items
                if isinstance(content, list):
                    text = " ".join(
                        (c.get("content") if isinstance(c.get("content"), str)
                         else _msg_text(c.get("content")))
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "tool_result")
                if SUBAGENT_FAIL_RE.search(text or ""):
                    subagent_failures += 1
                continue
            text = _msg_text(content)
            if not text.strip():
                continue
            if INTERRUPT_SENTINEL in text:
                interrupts += 1
            else:
                prompts += 1
                if CORRECTION_RE.search(text):
                    corrections += 1
                if RESOLUTION_RE.search(text):
                    resolution_confirmed = 1
        # REPLAN can appear in assistant text or tool_result text
        if typ in ("assistant", "user"):
            if "REPLAN:" in _msg_text(msg.get("content")):
                replans += 1
    if run >= CLUSTER_MIN:
        clusters += 1

    # sub-agent transcripts: tokens + cost by their own model
    subdir = main_file.parent / session_id / "subagents"
    if subdir.is_dir():
        for sf in subdir.glob("*.jsonl"):
            for d in _iter_jsonl(sf):
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                usage = msg.get("usage")
                if usage:
                    _add_usage(model_tokens, usage, msg.get("model"))
                    cost += token_cost(usage, msg.get("model"))

    rework_edits = sum(v - 1 for v in edits_per_path.values() if v > 1)
    if not timestamps:
        return None
    last_ts = max(timestamps)
    project = main_file.parent.name
    return {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session_id": session_id,
        "project": project,
        "date": last_ts.date().isoformat(),
        "first_ts": min(timestamps).isoformat(),
        "last_ts": last_ts.isoformat(),
        "mtime": main_file.stat().st_mtime,
        "model_tokens": model_tokens,
        "cost_usd": round(cost, 6),
        "cache_read_usd": round(_cache_read_cost(model_tokens), 6),
        "main_read_bash": main_read_bash,
        "agent_spawns": {
            "total": spawns_total,
            "opus": spawns["opus"],
            "sonnet": spawns["sonnet"],
            "haiku": spawns["haiku"],
            "no_explicit_model": no_explicit_model,
            "inherit_opus": inherit_opus,
        },
        "missed_delegation_clusters": clusters,
        "attention": {
            "askq": askq,
            "prompts": prompts,
            "interrupts": interrupts,
            "corrections": corrections,
        },
        "effectiveness": {
            "resolution_confirmed": resolution_confirmed,
            "replans": replans,
            "overcome_difficulty": overcome_difficulty,
            "subagent_failures": subagent_failures,
            "rework_edits": rework_edits,
        },
        "quality_rating": None,
        "quality_note": None,
    }


# ---------------------------------------------------------------- ledger I/O

def load_ledger() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not LEDGER.exists():
        return rows
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = row.get("session_id")
        if sid:
            rows[sid] = row  # later wins (idempotent rewrite dedups)
    return rows


def write_ledger(rows: dict[str, dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: r.get("last_ts", ""))
    with LEDGER.open("w", encoding="utf-8") as fh:
        for row in ordered:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def in_window_files(days: int, project: str | None) -> list[Path]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    proj_dirs = ([PROJECTS_DIR / project] if project
                 else [p for p in PROJECTS_DIR.iterdir() if p.is_dir()]
                 if PROJECTS_DIR.is_dir() else [])
    files: list[Path] = []
    for pd in proj_dirs:
        if not pd.is_dir():
            continue
        for f in pd.glob("*.jsonl"):
            try:
                hit = False
                for d in _iter_jsonl(f):
                    ts = d.get("timestamp") or (d.get("message") or {}).get("ts")
                    if isinstance(ts, str):
                        try:
                            if parse_ts(ts) >= cutoff:
                                hit = True
                                break
                        except ValueError:
                            continue
                if hit:
                    files.append(f)
            except OSError:
                continue
    return files


def upsert(days: int, project: str | None) -> tuple[dict[str, dict], int, int]:
    """Scan in-window files; (re)scan only when mtime grew. Returns (ledger, scanned, skipped)."""
    rows = load_ledger()
    scanned = skipped = 0
    for f in in_window_files(days, project):
        sid = f.stem
        existing = rows.get(sid)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if existing and existing.get("mtime") == mtime:
            skipped += 1
            continue
        row = _scan_session(f)
        if row is None:
            continue
        if existing:  # preserve manual rating across re-scans
            row["quality_rating"] = existing.get("quality_rating")
            row["quality_note"] = existing.get("quality_note")
        rows[sid] = row
        scanned += 1
    write_ledger(rows)
    return rows, scanned, skipped


# ---------------------------------------------------------------- reporting

def _window_rows(rows: dict[str, dict], lo: dt.datetime, hi: dt.datetime) -> list[dict]:
    out = []
    for r in rows.values():
        try:
            t = parse_ts(r.get("last_ts", ""))
        except ValueError:
            continue
        if lo <= t < hi:
            out.append(r)
    return out


def _aggregate(window: list[dict]) -> dict:
    a = {
        "sessions": len(window),
        "sessions_with_agent": sum(1 for r in window if r["agent_spawns"]["total"] > 0),
        "spawns_total": sum(r["agent_spawns"]["total"] for r in window),
        "spawn_opus": sum(r["agent_spawns"]["opus"] for r in window),
        "spawn_sonnet": sum(r["agent_spawns"]["sonnet"] for r in window),
        "spawn_haiku": sum(r["agent_spawns"]["haiku"] for r in window),
        "no_explicit_model": sum(r["agent_spawns"]["no_explicit_model"] for r in window),
        "inherit_opus": sum(r["agent_spawns"]["inherit_opus"] for r in window),
        "main_read_bash": sum(r.get("main_read_bash", 0) for r in window),
        "clusters": sum(r["missed_delegation_clusters"] for r in window),
        "cost_usd": sum(r["cost_usd"] for r in window),
        "cache_read_usd": sum(r.get("cache_read_usd", 0.0) for r in window),
        "askq": sum(r["attention"]["askq"] for r in window),
        "prompts": sum(r["attention"]["prompts"] for r in window),
        "interrupts": sum(r["attention"]["interrupts"] for r in window),
        "corrections": sum(r["attention"]["corrections"] for r in window),
        "resolution_confirmed": sum(r["effectiveness"]["resolution_confirmed"] for r in window),
        "replans": sum(r["effectiveness"]["replans"] for r in window),
        "overcome_difficulty": sum(r["effectiveness"]["overcome_difficulty"] for r in window),
        "subagent_failures": sum(r["effectiveness"]["subagent_failures"] for r in window),
        "rework_edits": sum(r["effectiveness"]["rework_edits"] for r in window),
    }
    ratings = [r["quality_rating"] for r in window if r.get("quality_rating")]
    a["avg_quality"] = round(sum(ratings) / len(ratings), 2) if ratings else None
    a["n_rated"] = len(ratings)
    a["cost_per_session"] = a["cost_usd"] / a["sessions"] if a["sessions"] else 0.0
    a["inherit_opus_rate"] = a["inherit_opus"] / a["spawns_total"] if a["spawns_total"] else 0.0
    a["clusters_per_session"] = a["clusters"] / a["sessions"] if a["sessions"] else 0.0
    a["resolution_rate"] = a["resolution_confirmed"] / a["sessions"] if a["sessions"] else 0.0
    a["cache_read_share"] = a["cache_read_usd"] / a["cost_usd"] if a["cost_usd"] else 0.0
    tok = _empty_model_tokens()
    for r in window:
        for k in MODEL_KEYS:
            for short, _ in USAGE_FIELDS:
                tok[k][short] += r["model_tokens"][k][short]
    a["model_tokens"] = tok
    return a


def _arrow(cur: float, prev: float, higher_is_worse: bool = True) -> str:
    if prev == 0 and cur == 0:
        return "→ (0)"
    if prev == 0:
        return f"↑ new ({cur:.3g})"
    delta = (cur - prev) / prev * 100
    if abs(delta) < 1:
        return f"→ ({cur:.3g})"
    up = delta > 0
    bad = up if higher_is_worse else not up
    mark = ("↑" if up else "↓") + (" ⚠" if bad else " ✓")
    return f"{mark} {delta:+.0f}% ({prev:.3g}→{cur:.3g})"


def _flags(cur: dict, prev: dict) -> list[str]:
    flags = []
    if cur["spawns_total"] and cur["inherit_opus_rate"] > 0.5:
        flags.append(
            f"inherit→opus rate {cur['inherit_opus_rate']:.0%} "
            f"({cur['inherit_opus']}/{cur['spawns_total']} spawns ran opus with no explicit cheap model:) "
            "— policy says name the tier (delegatable-work-patterns).")
    if cur["clusters_per_session"] > 0.5:
        flags.append(
            f"missed-delegation clusters {cur['clusters']} over {cur['sessions']} sessions "
            f"({cur['clusters_per_session']:.2f}/session) — ≥{CLUSTER_MIN} consecutive "
            "mechanical main-thread calls that belonged on a cheap sub-agent.")
    if prev["cost_per_session"] and cur["cost_per_session"] > prev["cost_per_session"] * 1.25:
        flags.append(
            f"$/session up {(cur['cost_per_session']/prev['cost_per_session']-1)*100:.0f}% "
            f"(${prev['cost_per_session']:.2f}→${cur['cost_per_session']:.2f}).")
    if prev["sessions"] and cur["resolution_rate"] < prev["resolution_rate"] - 0.1:
        flags.append(
            f"resolution-confirmed rate down {prev['resolution_rate']:.0%}→{cur['resolution_rate']:.0%} "
            "(proxy: user-side confirmation phrase present).")
    if cur["avg_quality"] is not None and cur["avg_quality"] < 3:
        flags.append(f"avg manual quality {cur['avg_quality']} (<3) over {cur['n_rated']} rated session(s).")
    if (cur["avg_quality"] is not None and prev.get("avg_quality") is not None
            and cur["avg_quality"] < prev["avg_quality"] - 0.5):
        flags.append(f"avg manual quality down {prev['avg_quality']}→{cur['avg_quality']}.")
    return flags


def _fmt_tokens(tok: dict) -> list[str]:
    lines = []
    for k in MODEL_KEYS:
        t = tok[k]
        if any(t.values()):
            lines.append(
                f"  {k:<7} in={t['in']:>10}  out={t['out']:>9}  "
                f"cache_r={t['cache_read']:>12}  cache_c={t['cache_create']:>11}")
    return lines


def _gate_events(days: int, now: dt.datetime) -> list[dict]:
    """In-window gate evaluations from GATE_LOG; [] when absent/unreadable."""
    if not GATE_LOG.exists():
        return []
    cutoff = now - dt.timedelta(days=days)
    events: list[dict] = []
    try:
        lines = GATE_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if parse_ts(row.get("ts", "")) >= cutoff:
                events.append(row)
        except (json.JSONDecodeError, ValueError):
            continue
    return events


def _gates_lines(days: int, now: dt.datetime) -> list[str]:
    """Markdown lines for the Gates section: firing counts and block-vs-pass
    rates per gate, so mechanical-gate calibration disputes become data
    (policy-effectiveness-tracking loop applied to the engine's gates)."""
    events = _gate_events(days, now)
    if not events:
        return [f"- no gate events in the last {days}d ({GATE_LOG})."]
    per_gate: dict[str, Counter] = defaultdict(Counter)
    for e in events:
        per_gate[e.get("gate", "?")]["fired"] += 1
        if not e.get("passed", False):
            per_gate[e.get("gate", "?")]["blocked"] += 1
    lines = []
    for gate in sorted(per_gate):
        c = per_gate[gate]
        rate = c["blocked"] / c["fired"] if c["fired"] else 0.0
        lines.append(f"- `{gate}`: fired **{c['fired']}**  ·  blocked **{c['blocked']}**  "
                     f"·  block rate **{rate:.0%}**")
    return lines


def scorecard(rows: dict[str, dict], days: int, project: str | None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    cur_lo = now - dt.timedelta(days=days)
    prev_lo = now - dt.timedelta(days=2 * days)
    cur = _aggregate(_window_rows(rows, cur_lo, now))
    prev = _aggregate(_window_rows(rows, prev_lo, cur_lo))

    L = [f"# Policy scorecard — last {days}d"
         + (f" · project={project}" if project else "")
         + f"  ({cur_lo.date()} → {now.date()})", ""]
    L.append(f"Sessions: **{cur['sessions']}**  ·  with Agent: "
             f"**{cur['sessions_with_agent']}/{cur['sessions']}**  "
             f"{_arrow(cur['sessions_with_agent'], prev['sessions_with_agent'], higher_is_worse=False)}")
    L.append("")
    L.append("## Policy compliance (headline)")
    L.append(f"- Agent spawns: **{cur['spawns_total']}**  "
             f"(opus {cur['spawn_opus']} / haiku {cur['spawn_haiku']} / sonnet {cur['spawn_sonnet']})  "
             f"{_arrow(cur['spawns_total'], prev['spawns_total'], higher_is_worse=False)}")
    L.append(f"- No explicit `model:` (\"inherit\"): **{cur['no_explicit_model']}/{cur['spawns_total']}**  "
             f"· of which ran opus (inherit→opus): **{cur['inherit_opus']}**  "
             f"· rate **{cur['inherit_opus_rate']:.0%}**  {_arrow(cur['inherit_opus_rate'], prev['inherit_opus_rate'])}")
    L.append(f"- Main-thread Read+Bash: **{cur['main_read_bash']}**  "
             f"{_arrow(cur['main_read_bash'], prev['main_read_bash'])}")
    L.append(f"- Missed-delegation clusters (≥{CLUSTER_MIN} consecutive mechanical): "
             f"**{cur['clusters']}**  ({cur['clusters_per_session']:.2f}/session)  "
             f"{_arrow(cur['clusters'], prev['clusters'])}")
    L.append("")
    L.append("## Efficiency")
    L.append(f"- Cost: **${cur['cost_usd']:.2f}**  ·  $/session **${cur['cost_per_session']:.3f}**  "
             f"{_arrow(cur['cost_per_session'], prev['cost_per_session'])}")
    L.append(f"- cache_read share of cost: **{cur['cache_read_share']:.0%}**  "
             f"{_arrow(cur['cache_read_share'], prev['cache_read_share'])}")
    L.append("- Tokens by model (main thread = opus; sub-agents = their own model):")
    L.extend(_fmt_tokens(cur["model_tokens"]) or ["  (none)"])
    L.append("")
    L.append("## Attention (agent ↔ user)")
    L.append(f"- AskUserQuestion: **{cur['askq']}**  ·  your prompts: **{cur['prompts']}**  "
             f"·  interrupts: **{cur['interrupts']}**  ·  likely corrections: **{cur['corrections']}**")
    L.append("")
    L.append("## Effectiveness (proxies)")
    L.append(f"- Resolution-confirmed sessions: **{cur['resolution_confirmed']}/{cur['sessions']}**  "
             f"({cur['resolution_rate']:.0%})  {_arrow(cur['resolution_rate'], prev['resolution_rate'], higher_is_worse=False)}")
    L.append(f"- REPLAN: **{cur['replans']}**  ·  overcome-difficulty: **{cur['overcome_difficulty']}**  "
             f"·  sub-agent failures: **{cur['subagent_failures']}**  ·  rework edits: **{cur['rework_edits']}**")
    aq = cur["avg_quality"]
    L.append(f"- Manual quality (1–5): **{aq if aq is not None else '—'}**  "
             f"(rated {cur['n_rated']}/{cur['sessions']}; attach via `rate <session_id> <1-5>`)")
    L.append("")
    L.append("## Gates (agentctl)")
    L.extend(_gates_lines(days, now))
    L.append("")
    L.append("## Flags")
    fl = _flags(cur, prev)
    if fl:
        L.extend(f"- ⚠ {f}" for f in fl)
        L.append("")
        L.append("When a flag fires: invoke `self-improvement` to adjust the policy, then record "
                 "the adjustment + observed metric movement in policy-effectiveness-tracking.md.")
    else:
        L.append("- none past threshold this window.")
    return "\n".join(L)


# ---------------------------------------------------------------- rate mode

def cmd_rate(session_id: str, rating: int, note: str | None) -> int:
    if not 1 <= rating <= 5:
        print("rate: rating must be 1–5", file=sys.stderr)
        return 1
    rows = load_ledger()
    row = rows.get(session_id)
    if row is None:  # allow unambiguous prefix
        matches = [s for s in rows if s.startswith(session_id)]
        if len(matches) == 1:
            row = rows[matches[0]]
            session_id = matches[0]
        elif len(matches) > 1:
            print(f"rate: ambiguous session prefix '{session_id}' ({len(matches)} matches)", file=sys.stderr)
            return 1
    if row is None:
        print(f"rate: session '{session_id}' not in ledger — run a scan first", file=sys.stderr)
        return 1
    row["quality_rating"] = rating
    if note is not None:
        row["quality_note"] = note
    write_ledger(rows)
    print(f"rate: {session_id} → {rating}/5"
          + (f"  note={note!r}" if note else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "rate":
        p = argparse.ArgumentParser(prog="policy-scorecard.py rate")
        p.add_argument("session_id")
        p.add_argument("rating", type=int)
        p.add_argument("--note")
        a = p.parse_args(argv[1:])
        return cmd_rate(a.session_id, a.rating, a.note)

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=7, help="window size in days (default 7)")
    p.add_argument("--project", help="restrict to one project dir under ~/.claude/projects")
    p.add_argument("--ledger-only", action="store_true", help="upsert without printing (for the hook)")
    a = p.parse_args(argv)

    rows, scanned, skipped = upsert(a.days, a.project)
    if a.ledger_only:
        print(f"policy-scorecard: ledger upsert — scanned {scanned}, "
              f"unchanged {skipped}, total rows {len(rows)}", file=sys.stderr)
        return 0
    print(scorecard(rows, a.days, a.project))
    print(f"\n_ledger: {LEDGER} ({len(rows)} rows; this run scanned {scanned}, "
          f"reused {skipped} unchanged)_")
    return 0


if __name__ == "__main__":
    sys.exit(main())
