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
  - spawn-cost ledger  ~/.local/log/claude-spawn-costs.jsonl (the process-failure
    axis; read through agentctl.cost.read_rows, never re-parsed here)

Fired flags are routed into the self-diagnose findings store, so a flag outlives
the run that printed it and re-surfaces at the turn boundary until it is acked or
stops firing. Rendering is unchanged by that routing, and no model is consulted:
this script stays a pure reader.
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
  policy-scorecard.py [...] --ledger PATH        override the ledger path
                                                 (tests / the cadence hook use
                                                 this so a run never touches
                                                 the real ledger)
  policy-scorecard.py --calibrate-spend-rate [--days N]
                                                 re-derive SPEND_RATE_FACTOR
                                                 from the stored ledger
                                                 (read-only: no scan, no upsert)
  policy-scorecard.py --calibrate-spend-rate --calibrate-until YYYY-MM-DD
                                                 same, over the ledger as it
                                                 stood on that date, so the
                                                 figures a shipped comment
                                                 quotes stay reproducible as
                                                 the ledger grows
  policy-scorecard.py --calibrate-failure-rate [--calibrate-until YYYY-MM-DD]
                                                 same for FAILURE_RATE_FACTOR,
                                                 over the SPAWN ledger
  policy-scorecard.py [...] --spawn-ledger PATH  override the spawn-cost ledger
  policy-scorecard.py [...] --findings-store PATH
                                                 override the store fired flags
                                                 are routed into
  policy-scorecard.py rate <session_id> <1-5> [--note "..."]
                                                 attach a manual quality rating
  policy-scorecard.py reprice [--dry-run]        re-price stored rows in place
                                                 at the current rate table
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

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
PRICING_SHA = cost_report.PRICING_SHA

# --- reuse hook-si-freetext-answer.py's option-label comparison (no copy-paste) ---
_FTA_PATH = Path(__file__).resolve().parent / "hook-si-freetext-answer.py"
_fta_spec = importlib.util.spec_from_file_location("hook_si_freetext_answer", _FTA_PATH)
hook_si_freetext_answer = importlib.util.module_from_spec(_fta_spec)
_fta_spec.loader.exec_module(hook_si_freetext_answer)
free_text_questions = hook_si_freetext_answer.free_text_questions

# System root (resolved via config_root) for transcripts
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config_root import agent_home, agentctl_gate_log, legacy_home
# The store that gives a fired flag closure state, and the spawn ledger's reader.
# Both are reused as-is: a second store schema or a second ledger parser would be
# two places to keep in step, and the ledger's attribution is already solved.
import self_diagnose_store as findings_store
from agentctl.cost import COST_LOG as SPAWN_LEDGER, read_rows as read_spawn_rows
PROJECTS_DIR = agent_home() / "projects"
LEDGER = Path.home() / ".local" / "log" / "claude-policy-ledger.jsonl"
# Per-task quality ledger written by `agentctl resolve --quality` (agentctl/cli.py
# TASK_QUALITY_LOG) -- same path, independent constant so this reader has no
# import dependency on the agentctl package.
TASK_QUALITY_LEDGER = Path.home() / ".local" / "log" / "claude-task-quality.jsonl"
# This script's own repo root, for instructions_head stamping / commit-range
# lookups -- never hard-code a machine-specific path (worktrees move it).
REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT_TIMEOUT_S = 5
# Written by agentctl cli._log_gate: one {ts, session, node, gate, blockers,
# passed} row per gate evaluation. Read-only here. Both the resolved root and
# the legacy pre-isolation root are read so history spanning the migration
# (old rows under ~/.claude, new rows under the isolated root) stays complete.
GATE_LOGS = tuple(dict.fromkeys(
    (agentctl_gate_log(), legacy_home() / "agentctl" / "gate-log.jsonl")))

# Derived from the price table, never written in parallel with it: every model-keyed
# structure below (token buckets, spawn counts, cache-read pricing) follows this, so
# registering a model is one row in cost-report.py's PRICING_USD_PER_MTOK. A
# hand-written tuple that merely happens to match would re-create the two-lists-
# disagree defect — a key here with no price row raises on every scan.
MODEL_KEYS = tuple(PRICING)
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
# A real user prompt that asks something (crude but cheap: any "?").
QUESTION_RE = re.compile(r"\?")

# --- spend rate (burn rate) ------------------------------------------------
# Every other metric here is normalised per session or per prompt. In the
# 2026-W29→W30 event all of them improved while total consumption roughly
# doubled, because the denominator grew with the numerator — so the
# un-normalised per-time rate is the axis that makes such an event visible.
#
# The FIGURE is always rendered; only the FLAG is conditional, and the flag is a
# CONJUNCTION — a rate rise alone is budget consumption, not degradation. That
# is the user's disposition of `q-rate-flag-semantics` at the plan-approval
# gate; "rate_only" is the alternative the question named, and flipping this
# constant is the whole switch.
SPEND_RATE_FLAG_MODE = "conjunction"  # "conjunction" | "rate_only"
SPEND_RATE_BASELINE_WINDOWS = 4       # trailing windows whose median is the baseline
SPEND_RATE_MIN_BASELINE_WINDOWS = 3   # fewer non-empty than this -> no flag, not a guess
# Calibrated against a NAMED ledger snapshot, because a number derived from a
# growing file is not reproducible unless it says what it was derived from. The
# whole block below is re-derived by
#     policy-scorecard.py --calibrate-spend-rate --calibrate-until 2026-07-28
# which is the pin: it drops the still-live current day, so the rows it keeps
# are finished sessions that no later re-scan moves. At that snapshot — 1680
# rows, 2026-06-11→2026-07-27, a 7d window rolled one day at a time over the
# median of its trailing 4 windows, 19 samples:
#     q50 1.24  q75 1.90  q90 2.08  q95 2.20  max 2.44
# THE RULE IS A FIRING BUDGET, NOT A GAP. There is no gap here to place a
# threshold in: the sorted samples run continuously from 0.65 to 2.44 with no
# step wider than 0.27, so the "two regimes" the previous calibration described
# were an artefact of the numerator defect fixed above (it inflated the rate by
# a drifting 1.0-1.7×, which manufactured the separation). What survives is a
# tolerated firing frequency: take the first 0.25 step at or above q90 → 2.25,
# which fires on 1/19 samples in 1 episode.
#   Why not the largest gap. Re-applying each rule to the ledger as it stood on
#   each of the 8 preceding days (the stability block the reproducer prints) the
#   gap rule's pick drops 2.22→1.67 on one day's data, holds, then returns to
#   2.30 once the last day is in — 0.54 in a single step, non-monotone, as lone
#   samples land either side of its widest gap. The quantile rule never
#   moves more than one grid step (0.25) per day. Its FULL range over that sweep
#   is wider, 0.75 against 0.54, and that is not a point against it: the move is
#   monotone 1.50→2.25 as the late-July elevated episode enters the sample,
#   which is a rule tracking new data rather than reacting to noise.
#   Honest limits. (1) 19 rolling samples over 46 days with 7d windows overlap
#   by 6/7, so this is ~6 independent points, and q90 of 19 samples sits 2
#   samples from the top — re-derive as the ledger grows, and re-quote the pin
#   when you do. (2) The sample includes the very event this axis was built for,
#   so the calibration set is not event-free. (3) It is inseparable from the
#   baseline depth above, and derived at the DEFAULT 7d window only — 4 trailing
#   14d windows need 56 days, more than this ledger spans, so
#   `--calibrate-spend-rate --days 14` correctly refuses rather than
#   extrapolating.
SPEND_RATE_FACTOR = 2.25
SPEND_RATE_TARGET_QUANTILE = 0.90     # the selection rule's tolerated firing frequency
SPEND_RATE_FACTOR_GRID = 0.25         # the rule rounds up to this, so it cannot report spurious precision
SPEND_RATE_STABILITY_DAYS = 8         # end dates the calibrator re-applies each rule to
# "$/prompt is not improving": current >= previous * (1 - this). A window whose
# per-unit cost fell by more than 5% is getting cheaper per unit of work, which
# is the benign-volume-growth shape the conjunction exists to stay silent on.
SPEND_RATE_EFFICIENCY_TOLERANCE = 0.05

# --- sub-agent process-failure rate ----------------------------------------
# WHICH AXIS, and why only one. A spawn can go wrong on two independent axes and
# they must not be averaged into a single "failure" number:
#   PROCESS   exit_code != 0 — the spawn did not complete. This is what the flag
#             governs. It is the axis whose remedy is a policy change (budget,
#             tier, task size), which is what a scorecard flag can actually ask
#             for.
#   PROTOCOL  malformed / a non-COMPLETED return_marker — the specialist finished
#             but its reply did not parse, or it reported a blocker. That is
#             parse hygiene and legitimate escalation respectively, not a failed
#             spawn: at the pinned snapshot below 41% of spawns are `malformed`
#             while 14% exit non-zero, and folding them together would make a
#             marker-format change look like a reliability collapse.
# Only the process axis ships. A protocol flag would need its own key and its own
# threshold, not a shared one — see subagent-failure-rate-w29-w30.md.
#
# The rate JOINS the spawn ledger, ~/.local/log/claude-spawn-costs.jsonl, and is
# read through agentctl.cost.read_rows. It does NOT reuse `subagent_failures`
# below, which is a regex over transcript text: that counter's numerator and
# denominator count different populations, so its "rate" reached 2.65 — see the
# render in `scorecard()`.
FAILURE_RATE_BASELINE_WINDOWS = 4     # trailing windows POOLED into the baseline
# Pooled, not a median of per-window rates: the quantity is a proportion, so the
# right aggregate over several windows is total failures / total spawns, which
# weights each window by the evidence it carries instead of by existing.
FAILURE_RATE_MIN_SPAWNS = 30          # below this the current window is not evidence
FAILURE_RATE_MIN_BASELINE_SPAWNS = 60
# Why 30: at n < 30 one spawn moves the rate by more than 3.3 points, which is
# larger than the whole 2026-W30 rate (3.1%) — a single event could carry the
# flag by itself. 60 for the baseline keeps the denominator of the ratio at least
# twice the numerator's evidence.
#
# Calibrated against a NAMED ledger snapshot, re-derived by
#     policy-scorecard.py --calibrate-failure-rate --calibrate-until 2026-07-28
# (the cutoff drops the still-live current day, so the snapshot is settled and the
# figures below reproduce against a file that keeps growing). At that snapshot —
# 1182 spawn rows, 2026-05-25→2026-07-27, a 7d window rolled one day at a time
# against the pooled preceding 28 days, 36 samples:
#     q50 0.64  q75 0.85  q90 0.93  q95 0.96  max 1.04
# THE HISTORY CONTAINS NO FIRING EPISODE. This axis has only ever improved:
# weekly 50.0 / 60.0 / 26.3 / 33.3 / 11.8 / 15.1 / 15.5 / 16.5 / 3.1%. So the
# quantile rule SPEND_RATE_FACTOR uses is NOT transferable here: calibrating to a
# tolerated firing frequency needs firings to budget against, and at q90 it would
# pick 1.00 and fire on half of ordinary weeks.
# What ships instead is a NOVELTY threshold with two terms, both computed from
# the ledger (`_failure_rate_factor`), taking the first grid step at or above
# both:
#   (a) the empirical envelope, max observed ratio 1.041 — fire when the metric
#       leaves the band its entire history has stayed inside; and
#   (b) the sampling-noise floor, 1 + 3 × the relative binomial sd at median
#       n=191 and median baseline p=19.44% (rel sd 0.147) = 1.442 — so a
#       threshold can never sit inside the range two identical regimes differ by
#       from counting noise alone. Term (b) binds here; (a) is what stops the rule
#       collapsing when a future quiet period makes the noise term small.
# -> first 0.25 step at or above max(1.041, 1.442) = 1.50, and re-applying the
# rule to the ledger as it stood on each of the 8 preceding days gives 1.50 every
# time — largest 1-day move 0.00 (the reproducer prints that sweep).
#   Honest limits. (1) A threshold no observation has ever crossed is calibrated
#   for SILENCE, not for detection: it can say what is clearly normal, and cannot
#   say from this data what a real degradation looks like — the opposed test in
#   test_scorecard_flag_routing.py is a constructed elevation, not a measured one.
#   (2) 36 rolling 7d samples overlap by 6/7, so this is ~9 independent points.
#   (3) The W30 regime shift (16.5%->3.1%) is inside the baseline window, which
#   drags the baseline down and makes the flag MORE sensitive over the next month,
#   not less — re-derive then and re-quote the pin.
FAILURE_RATE_FACTOR = 1.5
FAILURE_RATE_FACTOR_GRID = 0.25       # the rule rounds up to this
FAILURE_RATE_NOISE_SIGMAS = 3.0
FAILURE_RATE_STABILITY_DAYS = 8


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


def _instructions_head_at(ts: dt.datetime) -> str | None:
    """Best-effort instructions-repo HEAD as of `ts` (`git rev-list -1 --before`).
    None on any failure -- git absent, REPO_ROOT not a repo, no commit before ts --
    never blocks a session scan on the git call."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-list", "-1",
             f"--before={ts.isoformat()}", "HEAD"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _commit_range_lines(good: str | None, bad: str | None) -> list[str]:
    """`git log --oneline good..bad` restricted to instruction-affecting paths,
    capped at 20 lines. [] when either ref is missing/equal, or on any git
    failure (never blocks the scorecard render)."""
    if not good or not bad or good == bad:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "--oneline", f"{good}..{bad}",
             "--", "CLAUDE.md", "config.md", "skills", "agents", "scripts"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [f"  {line}" for line in proc.stdout.splitlines()[:20]]


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
    user_questions = freetext_askuser_answers = 0
    replans = overcome_difficulty = subagent_failures = 0
    edits_per_path: Counter = Counter()
    resolution_confirmed = 0
    timestamps: list[dt.datetime] = []
    pending_askq: dict[str, dict] = {}  # tool_use id -> AskUserQuestion input, awaiting its answer

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
                    tuid = c.get("id")
                    if tuid:
                        pending_askq[tuid] = c.get("input") or {}
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
                    item_texts = []
                    for c in content:
                        if not (isinstance(c, dict) and c.get("type") == "tool_result"):
                            continue
                        item_text = (c.get("content") if isinstance(c.get("content"), str)
                                     else _msg_text(c.get("content")))
                        item_texts.append(item_text)
                        tuid = c.get("tool_use_id")
                        pending_input = pending_askq.pop(tuid, None) if tuid else None
                        if pending_input is not None and item_text:
                            freetext_askuser_answers += len(
                                free_text_questions(pending_input, item_text))
                    text = " ".join(item_texts)
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
                if QUESTION_RE.search(text):
                    user_questions += 1
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
    first_ts = min(timestamps)
    last_ts = max(timestamps)
    project = main_file.parent.name
    return {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session_id": session_id,
        "project": project,
        "date": last_ts.date().isoformat(),
        "first_ts": first_ts.isoformat(),
        "last_ts": last_ts.isoformat(),
        "instructions_head": _instructions_head_at(first_ts),
        "mtime": main_file.stat().st_mtime,
        "model_tokens": model_tokens,
        "cost_usd": round(cost, 6),
        "cache_read_usd": round(_cache_read_cost(model_tokens), 6),
        "priced_by": PRICING_SHA,
        "main_read_bash": main_read_bash,
        "agent_spawns": {
            "total": spawns_total,
            **{k: spawns[k] for k in MODEL_KEYS},
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
        "user_signals": {
            "n_user_corrections": corrections,
            "n_user_questions": user_questions,
            "n_freetext_askuser_answers": freetext_askuser_answers,
            "n_interrupts": interrupts,
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


# ------------------------------------------------------------------ repricing

def _stored_model_tokens(row: dict) -> dict:
    """A stored row's token buckets in today's shape. A row written before a
    model joined the price table has no bucket for it, so every level defaults."""
    stored = row.get("model_tokens", {})
    out = _empty_model_tokens()
    for k in MODEL_KEYS:
        bucket = stored.get(k, {})
        for short, _ in USAGE_FIELDS:
            out[k][short] = int(bucket.get(short, 0) or 0)
    return out


def _row_costs(model_tokens: dict) -> tuple[float, float]:
    """(cost_usd, cache_read_usd) for one row's buckets at today's rates. The
    bucket key IS the model, so token_cost runs once per bucket instead of once
    per API call — the same linear sum over the same tokens at the same rates."""
    total = 0.0
    for k in MODEL_KEYS:
        b = model_tokens[k]
        total += token_cost({raw: b[short] for short, raw in USAGE_FIELDS}, k)
    return total, _cache_read_cost(model_tokens)


def _stale_priced_rows(rows: dict[str, dict]) -> int:
    return sum(1 for r in rows.values() if r.get("priced_by") != PRICING_SHA)


def reprice(dry_run: bool = False) -> str:
    """Re-price the stored ledger in place at the current table.

    In place, not rebuilt: `upsert` re-scans a session only while its transcript
    keeps growing, and a manual `quality_rating` exists nowhere but this file —
    so deleting the ledger to regenerate it would buy identical dollars at the
    cost of every rating ever attached. Only the two dollar fields and the rate
    stamp are written; every other field is carried through untouched."""
    rows = load_ledger()
    if not rows:
        return f"ledger has no rows ({LEDGER}) — nothing to reprice."
    before = sum(r.get("cost_usd", 0.0) or 0.0 for r in rows.values())
    changed = 0
    for r in rows.values():
        if "model_tokens" not in r:
            # Left exactly as found, stamp included. `_scan_session` always writes
            # model_tokens, so a row without it is corrupt or foreign — treating its
            # absent buckets as zero would rewrite real dollars to $0.00. It keeps
            # counting as stale below, which is the honest report: no known table
            # priced it.
            continue
        cost, cache_read = _row_costs(_stored_model_tokens(r))
        cost, cache_read = round(cost, 6), round(cache_read, 6)
        if (r.get("cost_usd") != cost or r.get("cache_read_usd") != cache_read
                or r.get("priced_by") != PRICING_SHA):
            changed += 1
        r["cost_usd"] = cost
        r["cache_read_usd"] = cache_read
        r["priced_by"] = PRICING_SHA
    after = sum(r.get("cost_usd", 0.0) or 0.0 for r in rows.values())
    out = [f"rows {len(rows)}  ·  repriced {changed}  ·  table {PRICING_SHA}",
           f"total cost_usd ${before:.2f} → ${after:.2f}"]
    if dry_run:
        out.append("--dry-run: ledger untouched, no backup written.")
        return "\n".join(out)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = LEDGER.with_name(LEDGER.name + f".bak-{stamp}")
    shutil.copy2(LEDGER, backup)
    write_ledger(rows)
    out.append(f"backup: {backup}")
    return "\n".join(out)


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


def _row_day_span(r: dict,
                  lo: dt.datetime | None = None,
                  hi: dt.datetime | None = None) -> tuple[list[dt.date], int]:
    """(the row's calendar days that fall inside [lo, hi), its own total days).

    Built from `first_ts`/`last_ts` — the min/max of the session's own message
    timestamps — and never from the row's `date` field, which is only
    `last_ts.date()`: a session running 23:00→01:00 is one `date` but two days
    of activity, and one that ran all day is still one `date`. Daily sums built
    off `date` are lumpy for exactly that reason.

    Both numbers come from here so the rate's denominator (`_active_days`, the
    union of the first element) and its numerator (`_in_window_share`, the
    ratio of the two) cannot describe different time extents — which is the
    defect they did have.
    """
    try:
        last = parse_ts(r.get("last_ts", ""))
    except ValueError:
        return [], 0
    try:
        first = parse_ts(r.get("first_ts", ""))
    except ValueError:
        first = last
    if first > last:
        first = last
    total = (last.date() - first.date()).days + 1
    if lo is not None and first < lo:
        first = lo
    if hi is not None and last >= hi:
        last = hi - dt.timedelta(microseconds=1)
    days = []
    d = first.date()
    while d <= last.date():
        days.append(d)
        d += dt.timedelta(days=1)
    return days, total


def _row_tokens(r: dict) -> int:
    """Every token a session used, over all models and usage fields."""
    stored = r.get("model_tokens", {})
    return sum(stored.get(k, {}).get(short, 0)
               for k in MODEL_KEYS for short, _ in USAGE_FIELDS)


def _in_window_share(r: dict,
                     lo: dt.datetime | None = None,
                     hi: dt.datetime | None = None) -> float:
    """How much of a row's cost and tokens belongs to [lo, hi), as a fraction.

    APPROXIMATE, and this is the only approximation in the rate: a session's
    spend is assumed uniform across its own calendar days, because the ledger
    stores first_ts/last_ts and a single total — no per-day breakdown. It is
    EXACT for any session lying wholly inside the window (share 1.0), which is
    the overwhelming majority, and the error is bounded by the spend of the one
    session straddling each window edge.
    """
    in_window, total = _row_day_span(r, lo, hi)
    return len(in_window) / total if total else 0.0


def _active_days(window: list[dict],
                 lo: dt.datetime | None = None,
                 hi: dt.datetime | None = None) -> int:
    """Distinct calendar days on which at least one session was live.

    Days are clamped to [lo, hi) when the window is known, so a session that
    began before the window contributes only its in-window days. That clamp is
    what makes `active_days <= _window_span_days(lo, hi)` an invariant rather
    than a hope.
    """
    days: set[dt.date] = set()
    for r in window:
        days.update(_row_day_span(r, lo, hi)[0])
    return len(days)


def _window_span_days(lo: dt.datetime, hi: dt.datetime) -> int:
    """Calendar dates the window touches — the ceiling `active_days` cannot pass.

    Not the same as `--days`: a `--days 14` window is 14×24h ending now, so
    unless `now` is exactly midnight it straddles 15 dates. Reporting "15 active
    days of 14" would read as an impossible ratio; this is the denominator that
    makes the printed fraction mean what it says.
    """
    return ((hi - dt.timedelta(microseconds=1)).date() - lo.date()).days + 1


def _aggregate(window: list[dict],
               lo: dt.datetime | None = None,
               hi: dt.datetime | None = None) -> dict:
    a = {
        "sessions": len(window),
        "sessions_with_agent": sum(1 for r in window if r["agent_spawns"]["total"] > 0),
        "spawns_total": sum(r["agent_spawns"]["total"] for r in window),
        # Rows written before a model joined the price table have no bucket for it.
        **{
            f"spawn_{k}": sum(r["agent_spawns"].get(k, 0) for r in window)
            for k in MODEL_KEYS
        },
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
        stored = r.get("model_tokens", {})
        for k in MODEL_KEYS:
            bucket = stored.get(k, {})
            for short, _ in USAGE_FIELDS:
                tok[k][short] += bucket.get(short, 0)
    a["model_tokens"] = tok
    a["active_days"] = _active_days(window, lo, hi)
    a["window_days"] = (_window_span_days(lo, hi)
                        if lo is not None and hi is not None else 0)
    a["tokens_total"] = sum(tok[k][short] for k in MODEL_KEYS for short, _ in USAGE_FIELDS)
    # Spend rate. Its numerator covers exactly the time extent its denominator
    # does — the days of [lo, hi) — so spend a session earned BEFORE the window
    # opened is left out of a window it did not happen in. `cost_usd` above is
    # the whole-session sum every other metric here is built on and keeps that
    # meaning; only the rate uses the apportioned one. Without this the two
    # sides measured different extents and the 7d rate on this ledger read 1.3×
    # high on average and 1.7× at worst — a drifting bias, so it did not cancel
    # in the ratio against the baseline. (The neighbouring `subagent_failures`
    # counter had the same shape of defect and went unnoticed for six weeks —
    # see system-knowledge/subagent-failure-rate-w29-w30.)
    shares = [(r, _in_window_share(r, lo, hi)) for r in window]
    a["cost_usd_in_window"] = sum(r["cost_usd"] * s for r, s in shares)
    a["tokens_in_window"] = sum(_row_tokens(r) * s for r, s in shares)
    a["cost_per_active_day"] = (a["cost_usd_in_window"] / a["active_days"]
                                if a["active_days"] else 0.0)
    a["tokens_per_active_day"] = (a["tokens_in_window"] / a["active_days"]
                                  if a["active_days"] else 0.0)
    a["cost_per_prompt"] = a["cost_usd"] / a["prompts"] if a["prompts"] else 0.0
    assert not a["window_days"] or a["active_days"] <= a["window_days"], (
        f"active_days {a['active_days']} > window span {a['window_days']}")
    return a


def load_quality_ledger() -> list[dict]:
    """Rows from TASK_QUALITY_LEDGER (agentctl `resolve --quality`), tolerant of
    a missing file (no task has resolved with --quality yet on this machine)."""
    if not TASK_QUALITY_LEDGER.exists():
        return []
    rows: list[dict] = []
    for line in TASK_QUALITY_LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _quality_window_rows(qrows: list[dict], lo: dt.datetime, hi: dt.datetime) -> list[dict]:
    out = []
    for r in qrows:
        try:
            t = parse_ts(r.get("ts", ""))
        except ValueError:
            continue
        if lo <= t < hi:
            out.append(r)
    return out


def _aggregate_quality(window: list[dict], session_rows: dict[str, dict]) -> dict:
    """Task-quality window aggregate, joined to the session ledger (by the task
    row's `session` field) for the per-task user-signal averages."""
    ratings = [r.get("quality") for r in window if isinstance(r.get("quality"), (int, float))]
    joined = [session_rows[r["session"]]["user_signals"]
              for r in window
              if r.get("session") in session_rows
              and "user_signals" in session_rows[r["session"]]]

    def _avg(key: str) -> float | None:
        vals = [j.get(key, 0) for j in joined]
        return round(sum(vals) / len(vals), 2) if vals else None

    avg_corrections = _avg("n_user_corrections")
    avg_freetext = _avg("n_freetext_askuser_answers")
    correction_rate = (round(avg_corrections + avg_freetext, 2)
                       if avg_corrections is not None and avg_freetext is not None else None)
    last_head = max(window, key=lambda r: r.get("ts", "")).get("instructions_head") if window else None
    return {
        "n_tasks": len(window),
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "n_rated": len(ratings),
        "n_joined": len(joined),
        "avg_corrections": avg_corrections,
        "avg_questions": _avg("n_user_questions"),
        "avg_freetext": avg_freetext,
        "avg_interrupts": _avg("n_interrupts"),
        "correction_rate": correction_rate,
        "last_instructions_head": last_head,
    }


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


def _spend_rate_baseline(rows: dict[str, dict], now: dt.datetime, days: int) -> float | None:
    """Median $/active-day over the windows trailing the current one.

    Relative, not a fixed dollar ceiling: the workload itself is what moves, so
    a static threshold calibrated on any one week flags all of the next one and
    then normalises into noise. None when fewer than
    SPEND_RATE_MIN_BASELINE_WINDOWS trailing windows carry any activity — too
    little history to claim a baseline is worse than claiming none."""
    rates = []
    for i in range(1, SPEND_RATE_BASELINE_WINDOWS + 1):
        hi = now - dt.timedelta(days=days * i)
        lo = now - dt.timedelta(days=days * (i + 1))
        agg = _aggregate(_window_rows(rows, lo, hi), lo, hi)
        # The apportioned numerator, not the whole-session sum: a trailing
        # window whose only rows are straddlers carrying no in-window days has
        # a rate of 0, and a 0 must not enter a median of rates.
        if agg["active_days"] and agg["cost_usd_in_window"] > 0:
            rates.append(agg["cost_per_active_day"])
    if len(rates) < SPEND_RATE_MIN_BASELINE_WINDOWS:
        return None
    rates.sort()
    mid = len(rates) // 2
    return rates[mid] if len(rates) % 2 else (rates[mid - 1] + rates[mid]) / 2


def _spend_rate_flag(cur: dict, prev: dict, baseline: float | None) -> str | None:
    """The burn-rate degradation flag, or None.

    A rate rise alone is budget consumption, not degradation — SRE burn-rate
    practice always pairs a consumption rate with an error/quality term, and the
    W29→W30 event this axis was built for had the rate roughly double while
    $/prompt FELL. So `conjunction` additionally requires that per-unit cost is
    not improving; `rate_only` is the alternative disposition, kept as a switch
    rather than a fork."""
    if not baseline or not cur.get("active_days"):
        return None
    rate = cur["cost_per_active_day"]
    if rate <= baseline * SPEND_RATE_FACTOR:
        return None
    head = (f"spend rate ${rate:,.2f} per active day is "
            f"{rate / baseline:.2f}× the trailing {SPEND_RATE_BASELINE_WINDOWS}-window "
            f"baseline ${baseline:,.2f}")
    if SPEND_RATE_FLAG_MODE == "rate_only":
        return head + " — budget consumption, not necessarily degradation."
    cur_pp, prev_pp = cur.get("cost_per_prompt", 0.0), prev.get("cost_per_prompt", 0.0)
    if not prev_pp:
        return None  # no prior per-unit cost: the second conjunct is unevaluable
    if cur_pp < prev_pp * (1 - SPEND_RATE_EFFICIENCY_TOLERANCE):
        return None  # cheaper per unit of work: volume growth, not degradation
    return (head + f", and $/prompt is not improving "
            f"(${prev_pp:.3f}→${cur_pp:.3f}) — the rate rise is not paying for "
            "more work per dollar.")


class Flag(NamedTuple):
    """A fired flag and its STABLE identity.

    `key` is flag kind + window granularity and nothing else — never the
    formatted message. A key derived from the message would change with every
    number in it, so the same standing condition would arrive as a new finding on
    every run and could never be acked, deduped or resolved. `__str__` is the
    message, so existing render and test call sites that interpolate a flag are
    unaffected."""
    key: str
    message: str

    def __str__(self) -> str:
        return self.message


# Keys whose firing means "look at the instruction-commit range" — a membership
# test on stable keys, replacing a `message.startswith("task quality")` prefix
# match that any rewording of the message silently broke.
QUALITY_FLAG_KEYS = ("task-quality", "correction-rate")


def _placeable_ts(raw: str) -> "dt.datetime | None":
    """A timestamp this module can place in a window, or None.

    A row's ts is unusable in two ways and only one of them raises at the parse:
    a malformed string raises ValueError here, but a tz-NAIVE one parses fine and
    raises TypeError later — at the first comparison against an aware window edge,
    a line no `except` around the parse can reach, which is why the naive case is
    handled by the return below rather than by the clause above. Naive rows do
    occur (tests/test_usage_digest_emit.py writes one into a spawn-ledger
    fixture). To a caller both are the same event: this row cannot be located in
    time, so it cannot be counted in a window."""
    try:
        ts = parse_ts(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def _spawn_failure_stats(spawn_rows: list[dict], lo: dt.datetime,
                         hi: dt.datetime) -> tuple[int, int]:
    """(spawns, process failures) over the spawn ledger rows in [lo, hi).

    ONE population, filtered twice. The denominator is every `event == "spawn"`
    row whose ts is in the window; the numerator is the subset of THOSE rows with
    exit_code != 0. Numerator ⊆ denominator by construction, over identical rows
    and an identical time extent — so the ratio is a proportion per spawn, an
    INTENSIVE quantity that carries no time dimension for a window edge to
    stretch. Both neighbours in this file lack one half of that: `subagent_failures`
    counts transcript text against native Agent uses (two populations), and
    `cost_per_active_day` divides a whole-session sum by in-window days (two time
    extents)."""
    n = bad = 0
    for row in spawn_rows:
        if row.get("event") != "spawn":
            continue
        ts = _placeable_ts(row.get("ts", ""))
        if ts is None or not lo <= ts < hi:
            continue
        n += 1
        if row.get("exit_code") != 0:
            bad += 1
    return n, bad


def _failure_rate(spawns: int, failures: int) -> float:
    """The proportion, with the invariant a rate over one population must obey.

    A rate whose numerator is a subset of its denominator CANNOT exceed 1. The
    old counter's reached 2.65 and stood for six weeks because nothing checked.
    Asserting it costs nothing and converts that class of defect from a silent
    wrong number into a crash at the first bad run.

    `assert` is stripped under `python -O`. Nothing in this repo runs it that way
    — no hook, script, or test passes -O or sets PYTHONOPTIMIZE — and the check
    is a self-consistency guard on this function's own arithmetic, not input
    validation, so a stripped build loses a tripwire rather than a protection."""
    if not spawns:
        return 0.0
    assert 0 <= failures <= spawns, (
        f"process-failure rate over one population: {failures} failures cannot "
        f"exceed {spawns} spawns — numerator and denominator have diverged")
    return failures / spawns


def _failure_rate_baseline(spawn_rows: list[dict], now: dt.datetime,
                           days: int) -> tuple[float, int] | None:
    """(pooled rate, spawns) over the FAILURE_RATE_BASELINE_WINDOWS windows
    trailing the current one, or None when that stretch is too thin to be a
    baseline.

    Pooled over one contiguous stretch rather than averaged across per-window
    rates: proportions from unequal denominators do not average, and a quiet
    window of 3 spawns would otherwise weigh as much as a busy one of 300."""
    hi = now - dt.timedelta(days=days)
    lo = now - dt.timedelta(days=days * (FAILURE_RATE_BASELINE_WINDOWS + 1))
    n, bad = _spawn_failure_stats(spawn_rows, lo, hi)
    if n < FAILURE_RATE_MIN_BASELINE_SPAWNS:
        return None
    return _failure_rate(n, bad), n


def _failure_rate_flag(spawn_rows: list[dict], now: dt.datetime,
                       days: int) -> str | None:
    """The sub-agent process-failure-rate flag, or None.

    Relative to the ledger's own recent history, not to a fixed percentage: the
    absolute rate has ranged from 60% to 3% in nine weeks, so any fixed ceiling
    is either always firing or never. See the FAILURE_RATE_* block for the axis
    choice and for why this threshold is calibrated for silence."""
    n, bad = _spawn_failure_stats(spawn_rows, now - dt.timedelta(days=days), now)
    if n < FAILURE_RATE_MIN_SPAWNS:
        return None
    base = _failure_rate_baseline(spawn_rows, now, days)
    if base is None:
        return None
    baseline, base_n = base
    rate = _failure_rate(n, bad)
    # A failure-free baseline has no ratio. The rule of three gives the 95% upper
    # bound on a rate that produced 0 failures in base_n trials, which is the
    # smallest claim the evidence supports — without it a 0%→1 -failure move
    # would divide by zero and fire on a single event.
    floor = 3.0 / base_n
    comparator = max(baseline, floor)
    if rate <= comparator * FAILURE_RATE_FACTOR:
        return None
    # Name the comparator the multiple was actually divided by. Reporting the
    # multiple against `comparator` while naming `baseline` produces a sentence
    # whose own arithmetic does not close whenever the floor binds — "6.2% is
    # 8.33× the baseline 0.0%" — and the act this flag requests begins with a
    # reader reproducing the number.
    against = (f"the pooled trailing {FAILURE_RATE_BASELINE_WINDOWS}-window "
               f"baseline {baseline:.1%} ({base_n} spawns)")
    if floor > baseline:
        against = (f"the rule-of-three floor {floor:.1%} — the 95% ceiling "
                   f"{base_n} baseline spawns support, which stands above the "
                   f"observed pooled baseline {baseline:.1%}")
    return (f"sub-agent process-failure rate {rate:.1%} ({bad}/{n} spawns exited "
            f"non-zero) is {rate / comparator:.2f}× {against} — spawns are "
            "failing to complete, which no per-session metric here can see.")


def _flags(cur: dict, prev: dict, cur_q: dict | None = None, prev_q: dict | None = None,
           spend_baseline: float | None = None, days: int = 7,
           spawn_rows: list[dict] | None = None,
           now: dt.datetime | None = None) -> list[Flag]:
    """Every fired flag, each carrying a stable key.

    The key's window granularity (`/7d`) is part of the identity because the same
    condition at two window sizes is two findings with two thresholds; sharing a
    key would make the last run to finish silently overwrite the other's."""
    flags = []
    w = f"/{days}d"
    spend_flag = _spend_rate_flag(cur, prev, spend_baseline)
    if spend_flag:
        flags.append(Flag("spend-rate" + w, spend_flag))
    if spawn_rows is not None:
        # Deliberately NOT `cur["subagent_failures"]`, which is in scope and looks
        # like the right input: that counter is a marker-word regex over transcript
        # text, not a spawn outcome, and it is divided by a different population.
        # This flag joins to the spawn ledger so numerator and denominator are the
        # same rows.
        fail_flag = _failure_rate_flag(spawn_rows, now or dt.datetime.now(dt.timezone.utc),
                                       days)
        if fail_flag:
            flags.append(Flag("subagent-failure-rate" + w, fail_flag))
    if cur["spawns_total"] and cur["inherit_opus_rate"] > 0.5:
        flags.append(Flag("inherit-opus" + w,
            f"inherit→opus rate {cur['inherit_opus_rate']:.0%} "
            f"({cur['inherit_opus']}/{cur['spawns_total']} spawns ran opus with no explicit cheap model:) "
            "— policy says name the tier (delegatable-work-patterns)."))
    if cur["clusters_per_session"] > 0.5:
        flags.append(Flag("missed-delegation" + w,
            f"missed-delegation clusters {cur['clusters']} over {cur['sessions']} sessions "
            f"({cur['clusters_per_session']:.2f}/session) — ≥{CLUSTER_MIN} consecutive "
            "mechanical main-thread calls that belonged on a cheap sub-agent."))
    if prev["cost_per_session"] and cur["cost_per_session"] > prev["cost_per_session"] * 1.25:
        flags.append(Flag("cost-per-session" + w,
            f"$/session up {(cur['cost_per_session']/prev['cost_per_session']-1)*100:.0f}% "
            f"(${prev['cost_per_session']:.2f}→${cur['cost_per_session']:.2f})."))
    if prev["sessions"] and cur["resolution_rate"] < prev["resolution_rate"] - 0.1:
        flags.append(Flag("resolution-rate" + w,
            f"resolution-confirmed rate down {prev['resolution_rate']:.0%}→{cur['resolution_rate']:.0%} "
            "(proxy: user-side confirmation phrase present)."))
    if cur["avg_quality"] is not None and cur["avg_quality"] < 3:
        flags.append(Flag("manual-quality-low" + w,
            f"avg manual quality {cur['avg_quality']} (<3) over {cur['n_rated']} rated session(s)."))
    if (cur["avg_quality"] is not None and prev.get("avg_quality") is not None
            and cur["avg_quality"] < prev["avg_quality"] - 0.5):
        flags.append(Flag("manual-quality-down" + w,
            f"avg manual quality down {prev['avg_quality']}→{cur['avg_quality']}."))
    if cur_q and cur_q.get("avg_rating") is not None:
        down = (prev_q is not None and prev_q.get("avg_rating") is not None
                and cur_q["avg_rating"] < prev_q["avg_rating"] - 0.5)
        if cur_q["avg_rating"] < 3.5 or down:
            reason = (f"down {prev_q['avg_rating']}→{cur_q['avg_rating']}" if down
                     else f"{cur_q['avg_rating']} < 3.5")
            flags.append(Flag("task-quality" + w,
                f"task quality avg over {cur_q['n_rated']} rated task(s): {reason} "
                "— see Task quality section."))
    if (cur_q and prev_q and cur_q.get("correction_rate") is not None
            and prev_q.get("correction_rate")
            and cur_q["correction_rate"] > prev_q["correction_rate"] * 1.5):
        flags.append(Flag("correction-rate" + w,
            "user-correction/free-text-answer rate per task up "
            f"{(cur_q['correction_rate'] / prev_q['correction_rate'] - 1) * 100:.0f}% "
            f"({prev_q['correction_rate']}→{cur_q['correction_rate']})."))
    return flags


def route_flags(flags: list[Flag], *, store_path=None,
                now: dt.datetime | None = None) -> list[dict]:
    """Upsert the fired flags into the findings store and return the store rows.

    ADDITIVE: nothing here changes what the scorecard prints, and no model is
    consulted — the scorecard stays a pure reader. What it adds is CLOSURE STATE.
    A flag printed to stdout dies with the run; a store row carries first_seen /
    times_surfaced / status, so `hook-turn-end-gate.py` re-surfaces it at the turn
    boundary once per session until it is acked, snoozed, or stops firing.

    Passing the WHOLE fired set (not just the new ones) is what lets the store
    resolve out a flag that has stopped firing — `upsert_findings` treats absence
    from a completed scan as the condition being gone, and `source` scopes that
    to this producer's own rows so a self-diagnose scan is untouched.

    ACTIONABLE_MIN_AGE_DAYS applies: a newly fired flag is silent at the turn
    boundary for two days before it may block. That delay is deliberate and
    inherited unmodified — the scorecard's cadence is weekly, so a flag worth
    blocking on is standing, not momentary."""
    findings = [{"kind": findings_store.KIND_POLICY_FLAG, "path": f.key, "detail": f.message}
                for f in flags]
    return findings_store.upsert_findings(
        findings, path=store_path, now=now,
        source=findings_store.SOURCE_POLICY_SCORECARD)


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
    """In-window gate evaluations from GATE_LOGS; [] when absent/unreadable."""
    cutoff = now - dt.timedelta(days=days)
    events: list[dict] = []
    for log in GATE_LOGS:
        if not log.exists():
            continue
        try:
            lines = log.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
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
        return [f"- no gate events in the last {days}d ({GATE_LOGS[0]})."]
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


def scorecard(rows: dict[str, dict], days: int, project: str | None,
              spawn_rows: list[dict] | None = None,
              route: bool = False, store_path=None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    if spawn_rows is None:
        spawn_rows = read_spawn_rows(SPAWN_LEDGER)
    cur_lo = now - dt.timedelta(days=days)
    prev_lo = now - dt.timedelta(days=2 * days)
    cur = _aggregate(_window_rows(rows, cur_lo, now), cur_lo, now)
    prev = _aggregate(_window_rows(rows, prev_lo, cur_lo), prev_lo, cur_lo)
    spend_baseline = _spend_rate_baseline(rows, now, days)
    qrows = load_quality_ledger()
    cur_q = _aggregate_quality(_quality_window_rows(qrows, cur_lo, now), rows)
    prev_q = _aggregate_quality(_quality_window_rows(qrows, prev_lo, cur_lo), rows)

    L = [f"# Policy scorecard — last {days}d"
         + (f" · project={project}" if project else "")
         + f"  ({cur_lo.date()} → {now.date()})", ""]
    L.append(f"Sessions: **{cur['sessions']}**  ·  with Agent: "
             f"**{cur['sessions_with_agent']}/{cur['sessions']}**  "
             f"{_arrow(cur['sessions_with_agent'], prev['sessions_with_agent'], higher_is_worse=False)}")
    L.append("")
    L.append("## Policy compliance (headline)")
    per_model = " / ".join(f"{k} {cur[f'spawn_{k}']}" for k in MODEL_KEYS)
    L.append(f"- Agent spawns: **{cur['spawns_total']}**  "
             f"({per_model})  "
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
    # Rendered unconditionally: quota-burn awareness is informational and every
    # other figure here is normalised, so this is the only one that shows total
    # consumption moving. Only the flag below is conditional.
    L.append(f"- Spend rate: **${cur['cost_per_active_day']:,.2f} per active day**  "
             f"·  **{cur['tokens_per_active_day']/1e6:,.1f}M tokens per active day**  "
             f"(over **{cur['active_days']}** active day(s) of {cur['window_days']})  "
             f"{_arrow(cur['cost_per_active_day'], prev['cost_per_active_day'])}")
    # Otherwise the rate and the Cost line above cannot be reconciled by eye:
    # sessions that started before the window keep their whole cost there.
    if abs(cur["cost_usd_in_window"] - cur["cost_usd"]) > 0.01 * max(cur["cost_usd"], 1e-9):
        L.append(f"  · rate numerator **${cur['cost_usd_in_window']:,.2f}** — the rest of the "
                 f"**${cur['cost_usd']:,.2f}** was earned before this window opened")
    if spend_baseline:
        L.append(f"  · trailing {SPEND_RATE_BASELINE_WINDOWS}-window baseline "
                 f"**${spend_baseline:,.2f}/active day**  ·  "
                 f"$/prompt **${cur['cost_per_prompt']:.3f}**  "
                 f"{_arrow(cur['cost_per_prompt'], prev['cost_per_prompt'])}")
    # Whole-ledger, unlike every figure above it: `reprice` is whole-ledger too, so
    # a stale row outside this window still needs the same single command.
    stale = _stale_priced_rows(rows)
    if stale:
        L.append(f"- ⚠ **{stale}** ledger row(s) priced by an older rate table — "
                 f"dollar comparisons mix two tables until `policy-scorecard.py reprice`.")
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
             f"·  rework edits: **{cur['rework_edits']}**")
    # The old `subagent_failures` used to render here as "sub-agent failures",
    # which it is not: it is a marker regex over the joined text of EVERY tool
    # result, so a Read of a file containing "ESCALATE:" counts — 93% of its
    # matches were incidental, and it was divided by native Agent uses, a
    # different population, giving a "rate" of 2.65. It is RELABELLED rather than
    # deleted (the series is six weeks long and still tracks something real about
    # how often those words appear), and the ledger-derived figure it was mistaken
    # for is rendered beside it so no reader takes one for the other.
    fail_n, fail_bad = _spawn_failure_stats(spawn_rows, cur_lo, now)
    L.append(f"- Sub-agent spawns: **{fail_n}**  ·  process failures (exit≠0): "
             f"**{fail_bad}** ({_failure_rate(fail_n, fail_bad):.1%})  ·  "
             f"marker words in tool-result text: **{cur['subagent_failures']}** "
             "(transcript scan, mostly incidental — not a spawn outcome; see "
             "system-knowledge/subagent-failure-rate-w29-w30.md)")
    aq = cur["avg_quality"]
    L.append(f"- Manual quality (1–5): **{aq if aq is not None else '—'}**  "
             f"(rated {cur['n_rated']}/{cur['sessions']}; attach via `rate <session_id> <1-5>`)")
    L.append("")
    L.append("## Task quality")
    if not qrows:
        L.append(f"- no task-quality rows found ({TASK_QUALITY_LEDGER}) — "
                 "resolve a task with `agentctl resolve --quality` to start the series.")
    else:
        L.append(f"- Tasks: **{cur_q['n_tasks']}**  "
                 f"{_arrow(cur_q['n_tasks'], prev_q['n_tasks'], higher_is_worse=False)}")
        aqr = cur_q["avg_rating"]
        arrow = (f"  {_arrow(aqr, prev_q['avg_rating'], higher_is_worse=False)}"
                 if aqr is not None and prev_q["avg_rating"] is not None else "")
        L.append(f"- Avg user rating (1–5): **{aqr if aqr is not None else '—'}**  "
                 f"(rated {cur_q['n_rated']}/{cur_q['n_tasks']}){arrow}")
        if cur_q["n_joined"]:
            L.append(f"- Avg per-task user signals (joined {cur_q['n_joined']}/{cur_q['n_tasks']} "
                     "to a session ledger row): "
                     f"corrections **{cur_q['avg_corrections']}**  ·  "
                     f"questions **{cur_q['avg_questions']}**  ·  "
                     f"free-text answers **{cur_q['avg_freetext']}**  ·  "
                     f"interrupts **{cur_q['avg_interrupts']}**")
        else:
            L.append("- no task row joined to a session ledger row this window.")
    L.append("")
    L.append("## Gates (agentctl)")
    L.extend(_gates_lines(days, now))
    L.append("")
    L.append("## Flags")
    fl = _flags(cur, prev, cur_q, prev_q, spend_baseline=spend_baseline,
                days=days, spawn_rows=spawn_rows, now=now)
    if route:
        route_flags(fl, store_path=store_path, now=now)
    if fl:
        L.extend(f"- ⚠ {f.message}" for f in fl)
        quality_flags = [f for f in fl if f.key.split("/")[0] in QUALITY_FLAG_KEYS]
        if quality_flags:
            good = prev_q.get("last_instructions_head")
            bad = cur_q.get("last_instructions_head")
            L.append("")
            range_lines = _commit_range_lines(good, bad)
            if good and bad and good != bad:
                L.append(f"Instruction-commit range `{good[:12]}..{bad[:12]}` "
                         "(CLAUDE.md/config.md/skills/agents/scripts):")
                L.extend(range_lines or ["  (no matching commits in range)"])
            else:
                L.append("Instruction-commit range unavailable "
                         "(missing or equal instructions_head across windows).")
            L.append("Run `scripts/quality-regression-investigate.py` to investigate further.")
        L.append("")
        L.append("When a flag fires: invoke `self-improvement` to adjust the policy, then record "
                 "the adjustment + observed metric movement in policy-effectiveness-tracking.md.")
    else:
        L.append("- none past threshold this window.")
    return "\n".join(L)


def _rolling_ratio_samples(rows: dict[str, dict],
                           days: int) -> tuple[list[tuple[dt.date, float, float, float]],
                                               dt.datetime | None, dt.datetime | None]:
    """(window end, $/active-day, baseline, ratio) for every rolling window."""
    stamps = []
    for r in rows.values():
        try:
            stamps.append(parse_ts(r.get("last_ts", "")))
        except ValueError:
            continue
    if not stamps:
        return [], None, None
    lo_all, hi_all = min(stamps), max(stamps)
    samples: list[tuple[dt.date, float, float, float]] = []
    now = lo_all + dt.timedelta(days=days * SPEND_RATE_BASELINE_WINDOWS)
    # Windows are half-open, so a loop stopping at hi_all never samples the
    # newest row's own day — which would systematically under-weight the most
    # recent episode, the one a threshold most needs to be placed against.
    while now <= hi_all + dt.timedelta(days=1):
        cur = _aggregate(_window_rows(rows, now - dt.timedelta(days=days), now),
                         now - dt.timedelta(days=days), now)
        base = _spend_rate_baseline(rows, now, days)
        if base and cur["active_days"]:
            rate = cur["cost_per_active_day"]
            samples.append((now.date(), rate, base, rate / base))
        now += dt.timedelta(days=1)
    return samples, lo_all, hi_all


class FailureSample(NamedTuple):
    """One rolling window of the process-failure axis."""
    end: dt.date
    n: int
    bad: int
    rate: float
    base_n: int
    base: float
    ratio: float


def _failure_rate_samples(spawn_rows: list[dict],
                          days: int) -> tuple[list[FailureSample],
                                              dt.datetime | None, dt.datetime | None]:
    """Every rolling window of the failure-rate axis, one day apart."""
    stamps = []
    for r in spawn_rows:
        if r.get("event") != "spawn":
            continue
        ts = _placeable_ts(r.get("ts", ""))
        if ts is not None:
            stamps.append(ts)
    if not stamps:
        return [], None, None
    lo_all, hi_all = min(stamps), max(stamps)
    samples: list[FailureSample] = []
    now = lo_all + dt.timedelta(days=days * FAILURE_RATE_BASELINE_WINDOWS)
    # +1 day for the same reason as the spend-rate sweep: half-open windows mean
    # a loop stopping at hi_all never samples the newest row's own day.
    while now <= hi_all + dt.timedelta(days=1):
        n, bad = _spawn_failure_stats(spawn_rows, now - dt.timedelta(days=days), now)
        base = _failure_rate_baseline(spawn_rows, now, days)
        if n >= FAILURE_RATE_MIN_SPAWNS and base is not None:
            baseline, base_n = base
            rate = _failure_rate(n, bad)
            samples.append(FailureSample(now.date(), n, bad, rate, base_n, baseline,
                                         rate / max(baseline, 3.0 / base_n)))
        now += dt.timedelta(days=1)
    return samples, lo_all, hi_all


def _failure_noise_floor(samples: list[FailureSample]) -> float:
    """1 + N sigma of pure binomial sampling noise, relative, at a typical window.

    Two windows drawn from an IDENTICAL regime still differ, by an amount set by
    how many spawns each counted. This is that amount: a threshold below it would
    fire on a difference that carries no information about the regime at all.

    "Typical" is two MARGINAL medians — median n over the windows, median baseline
    over the windows — not the pair belonging to any one window. That is
    deliberate: a single window can carry p = 0, at which the relative sigma is
    undefined, so summarising each term separately keeps the floor defined
    wherever the history has any failures at all. It is a summary of the regime,
    not a description of one sample, and the two need not co-occur."""
    if not samples:
        return 1.0
    n = sorted(s.n for s in samples)[len(samples) // 2]
    p = sorted(s.base for s in samples)[len(samples) // 2]
    if not n or not p:
        return 1.0
    return 1.0 + FAILURE_RATE_NOISE_SIGMAS * math.sqrt(p * (1 - p) / n) / p


def _failure_rate_factor(samples: list[FailureSample]) -> float:
    """The SHIPPED selection rule: the first grid step at or above BOTH the
    empirical envelope and the sampling-noise floor.

    A NOVELTY rule, not a firing budget. `_factor_by_quantile` calibrates to a
    tolerated firing frequency, which needs firings to budget against; this axis
    has none — see the FAILURE_RATE_FACTOR comment. So the threshold is placed
    outside everything the history has done (the envelope) and outside what two
    identical regimes would differ by from counting noise (the floor), and the
    binding term is whichever is larger."""
    if not samples:
        return FAILURE_RATE_FACTOR
    envelope = max(s.ratio for s in samples)
    target = max(envelope, _failure_noise_floor(samples))
    return math.ceil(target / FAILURE_RATE_FACTOR_GRID) * FAILURE_RATE_FACTOR_GRID


def _truncated_spawn_rows(spawn_rows: list[dict], before: dt.date) -> list[dict]:
    """The spawn ledger as it stood before `before` — the input to
    `--calibrate-until`, so a figure quoted in a comment stays reproducible
    against an append-only file that has grown since."""
    out = []
    for r in spawn_rows:
        try:
            if parse_ts(r.get("ts", "")).date() < before:
                out.append(r)
        except ValueError:
            continue
    return out


def calibrate_failure_rate(spawn_rows: list[dict], days: int) -> str:
    """Re-derive FAILURE_RATE_FACTOR from the spawn ledger's own history."""
    samples, lo_all, hi_all = _failure_rate_samples(spawn_rows, days)
    if lo_all is None:
        return "calibrate: spawn ledger has no dated spawn rows."
    if not samples:
        return (f"calibrate: spawn ledger spans {(hi_all - lo_all).days}d — too short "
                f"for {FAILURE_RATE_BASELINE_WINDOWS} baseline windows of {days}d, or "
                f"too few spawns per window (min {FAILURE_RATE_MIN_SPAWNS}).")
    ratios = sorted(s.ratio for s in samples)
    n = len(ratios)
    spawn_total = sum(1 for r in spawn_rows if r.get("event") == "spawn")
    out = [f"failure-rate calibration — axis exit_code != 0, window {days}d, baseline "
           f"POOLED over the trailing {FAILURE_RATE_BASELINE_WINDOWS} windows "
           f"(min {FAILURE_RATE_MIN_SPAWNS} spawns current / "
           f"{FAILURE_RATE_MIN_BASELINE_SPAWNS} baseline)",
           f"ledger {spawn_total} spawn rows, {lo_all.date()} → {hi_all.date()} "
           f"({(hi_all - lo_all).days}d); {n} rolling samples",
           "  " + "  ".join(f"q{int(p*100)} {_quantile(ratios, p):.2f}"
                            for p in (0.5, 0.75, 0.9, 0.95))
           + f"  max {ratios[-1]:.2f}",
           "",
           f"{'window end':>12}{'n':>6}{'bad':>5}{'rate':>9}{'base n':>8}"
           f"{'baseline':>10}{'ratio':>8}"]
    for s in samples:
        out.append(f"{str(s.end):>12}{s.n:>6}{s.bad:>5}{s.rate:>9.2%}{s.base_n:>8}"
                   f"{s.base:>10.2%}{s.ratio:>8.2f}")
    out.append("")
    for factor in sorted({1.25, 1.5, 2.0, 2.5, 3.0, FAILURE_RATE_FACTOR}):
        firing = [s.end for s in samples if s.ratio > factor]
        episodes = sum(1 for i, d in enumerate(firing)
                       if i == 0 or (d - firing[i - 1]).days > 1)
        mark = "  <- shipped" if factor == FAILURE_RATE_FACTOR else ""
        out.append(f"  factor {factor}: {len(firing):>2}/{n} samples in "
                   f"{episodes} distinct episode(s){mark}")
    out.append("Rolling samples overlap, so n over-states the independent evidence "
               f"(~{max(1, (hi_all - lo_all).days // days)} independent points here).")
    out.append("")
    envelope = max(s.ratio for s in samples)
    floor = _failure_noise_floor(samples)
    med_n = sorted(s.n for s in samples)[len(samples) // 2]
    med_p = sorted(s.base for s in samples)[len(samples) // 2]
    out.append(f"selection rule: first {FAILURE_RATE_FACTOR_GRID} step at or above BOTH")
    out.append(f"  empirical envelope (max observed ratio) {envelope:.3f}")
    out.append(f"  noise floor 1 + {FAILURE_RATE_NOISE_SIGMAS:g}sd at median n={med_n}, "
               f"median baseline p={med_p:.2%} (marginal medians, not one window) "
               f"{floor:.3f}")
    out.append(f"  -> {_failure_rate_factor(samples):.2f}   [shipped {FAILURE_RATE_FACTOR}]")
    if not any(s.ratio > FAILURE_RATE_FACTOR for s in samples):
        # Said out loud, because a silent 0/N is indistinguishable from a
        # well-behaved threshold and this one is calibrated for silence: the axis
        # has only ever improved, so nothing here demonstrates the flag can
        # detect a real degradation. Only the constructed opposed test does.
        out.append("NOTE: no sample in this history fires. The ratio never exceeds "
                   f"{envelope:.2f}, so this threshold is calibrated for SILENCE — "
                   "it bounds what is clearly normal and cannot, from this data, "
                   "say what a real degradation looks like.")
    out.append("stability — the rule re-applied to the ledger as it stood BEFORE that date:")
    out.append(f"{'ledger before':>14}{'n':>5}{'envelope':>10}{'noise':>8}{'pick':>7}")
    picks = []
    for back in range(FAILURE_RATE_STABILITY_DAYS, 0, -1):
        cutoff = hi_all.date() - dt.timedelta(days=back - 1)
        older, _, _ = _failure_rate_samples(_truncated_spawn_rows(spawn_rows, cutoff), days)
        if len(older) < 2:
            continue
        pick = _failure_rate_factor(older)
        picks.append(pick)
        out.append(f"{str(cutoff):>14}{len(older):>5}{max(s.ratio for s in older):>10.2f}"
                   f"{_failure_noise_floor(older):>8.2f}{pick:>7.2f}")
    if picks:
        out.append(f"{'largest 1-day move':>19}"
                   f"{max((abs(b - a) for a, b in zip(picks, picks[1:])), default=0.0):>10.2f}")
    return "\n".join(out)


def _quantile(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    k = (n - 1) * p
    f, c = int(k), min(int(k) + 1, n - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _factor_by_quantile(ratios: list[float]) -> float:
    """The SHIPPED selection rule: the first grid step at or above the target
    quantile of the ratio distribution. Calibrating to a tolerated firing
    frequency rather than to a gap in the histogram, because this distribution
    has no gap to find — see the SPEND_RATE_FACTOR comment."""
    return math.ceil(_quantile(sorted(ratios), SPEND_RATE_TARGET_QUANTILE)
                     / SPEND_RATE_FACTOR_GRID) * SPEND_RATE_FACTOR_GRID


def _widest_step(sorted_vals: list[float]) -> tuple[float, float]:
    """The adjacent pair furthest apart; a lone sample is its own pair."""
    return max(zip(sorted_vals, sorted_vals[1:]),
               key=lambda p: p[1] - p[0],
               default=(sorted_vals[0], sorted_vals[0]))


def _factor_by_largest_gap(ratios: list[float]) -> float:
    """The REJECTED rule, kept because rejecting it is a claim the reproducer
    has to be able to substantiate: it picks the midpoint of the widest gap
    between consecutive sorted samples, and the stability block below shows how
    far that midpoint travels on one day of extra data."""
    s = sorted(ratios)
    lo, hi = _widest_step(s)
    return (lo + hi) / 2


def _truncated(rows: dict[str, dict], before: dt.date) -> dict[str, dict]:
    """The ledger as it stood before `before` — the input to a stability sweep,
    and to `--calibrate-until`, which is how a figure quoted in a comment stays
    reproducible against a ledger that has grown since."""
    out = {}
    for sid, r in rows.items():
        try:
            if parse_ts(r.get("last_ts", "")).date() < before:
                out[sid] = r
        except ValueError:
            continue
    return out


def calibrate_spend_rate(rows: dict[str, dict], days: int) -> str:
    """Re-derive SPEND_RATE_FACTOR from the ledger's own history (read-only).

    Rolls the window one day at a time across the whole ledger and reports the
    distribution of rate / trailing-baseline, so the constant's comment can be
    checked rather than believed."""
    samples, lo_all, hi_all = _rolling_ratio_samples(rows, days)
    if lo_all is None:
        return "calibrate: ledger has no dated rows."
    if not samples:
        return (f"calibrate: ledger spans {(hi_all - lo_all).days}d — too short for "
                f"{SPEND_RATE_BASELINE_WINDOWS} baseline windows of {days}d.")
    ratios = sorted(s[3] for s in samples)
    n = len(ratios)

    def q(p: float) -> float:
        return _quantile(ratios, p)

    out = [f"spend-rate calibration — window {days}d, baseline median of trailing "
           f"{SPEND_RATE_BASELINE_WINDOWS} windows (min {SPEND_RATE_MIN_BASELINE_WINDOWS} non-empty)",
           f"ledger {len(rows)} rows, {lo_all.date()} → {hi_all.date()} "
           f"({(hi_all - lo_all).days}d); {n} rolling samples",
           "  " + "  ".join(f"q{int(p*100)} {q(p):.2f}" for p in (0.5, 0.75, 0.9, 0.95))
           + f"  max {ratios[-1]:.2f}",
           "",
           f"{'window end':>12}{'$/act.day':>11}{'baseline':>10}{'ratio':>8}"]
    for d, rate, base, r in samples:
        out.append(f"{str(d):>12}{rate:>11.2f}{base:>10.2f}{r:>8.2f}")
    out.append("")
    # Consecutive rolling windows overlap by days-1, so one real episode shows up
    # as a run of adjacent firing dates. Counting runs, not samples, is the only
    # honest answer to "how often would this have fired".
    for factor in sorted({1.5, 2.0, 2.5, 3.0, 3.5, 4.0, SPEND_RATE_FACTOR}):
        firing = [s[0] for s in samples if s[3] > factor]
        episodes = sum(1 for i, d in enumerate(firing)
                       if i == 0 or (d - firing[i - 1]).days > 1)
        mark = "  <- shipped" if factor == SPEND_RATE_FACTOR else ""
        out.append(f"  factor {factor}: {len(firing):>2}/{n} samples in "
                   f"{episodes} distinct episode(s){mark}")
    out.append("Rolling samples overlap, so n over-states the independent evidence "
               f"(~{max(1, (hi_all - lo_all).days // days)} independent points here).")
    out.append("")
    out.append(f"selection rule: first {SPEND_RATE_FACTOR_GRID} step at or above "
               f"q{int(SPEND_RATE_TARGET_QUANTILE * 100)} "
               f"({q(SPEND_RATE_TARGET_QUANTILE):.2f}) -> "
               f"{_factor_by_quantile(ratios):.2f}   [shipped {SPEND_RATE_FACTOR}]")
    gap_lo, gap_hi = _widest_step(ratios)
    out.append(f"  range {ratios[0]:.2f}-{ratios[-1]:.2f}; widest step between adjacent "
               f"samples {gap_hi - gap_lo:.2f} ({gap_lo:.2f}->{gap_hi:.2f}), which the "
               f"largest-gap rule would read as a regime boundary at "
               f"{_factor_by_largest_gap(ratios):.2f}")
    out.append("stability — each rule re-applied to the ledger as it stood BEFORE that date:")
    out.append(f"{'ledger before':>14}{'n':>5}{'quantile rule':>15}{'largest-gap rule':>18}")
    picks_q, picks_g = [], []
    for back in range(SPEND_RATE_STABILITY_DAYS, 0, -1):
        cutoff = hi_all.date() - dt.timedelta(days=back - 1)
        older, _, _ = _rolling_ratio_samples(_truncated(rows, cutoff), days)
        if len(older) < 2:
            continue
        r_older = [s[3] for s in older]
        pq, pg = _factor_by_quantile(r_older), _factor_by_largest_gap(r_older)
        picks_q.append(pq)
        picks_g.append(pg)
        out.append(f"{str(cutoff):>14}{len(older):>5}{pq:>15.2f}{pg:>18.2f}")

    def _biggest_step(picks: list[float]) -> float:
        return max((abs(b - a) for a, b in zip(picks, picks[1:])), default=0.0)

    if picks_q:
        # The statistic is the biggest ONE-DAY move, not the range. A rule that
        # tracks genuinely new data will drift across a sweep and should; the
        # failure mode is a rule that lurches — the gap rule drops 2.22->1.67 and
        # climbs back to 2.30 as single samples land either side of its widest
        # gap, while the quantile rule never moves more than one grid step.
        out.append(f"{'largest 1-day move':>19}{_biggest_step(picks_q):>10.2f}"
                   f"{_biggest_step(picks_g):>18.2f}")
        out.append(f"{'full range':>19}{max(picks_q) - min(picks_q):>10.2f}"
                   f"{max(picks_g) - min(picks_g):>18.2f}")
    return "\n".join(out)


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
    if argv and argv[0] == "reprice":
        p = argparse.ArgumentParser(prog="policy-scorecard.py reprice")
        p.add_argument("--dry-run", action="store_true",
                       help="report the delta without writing the ledger")
        a = p.parse_args(argv[1:])
        print(reprice(dry_run=a.dry_run))
        return 0

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=7, help="window size in days (default 7)")
    p.add_argument("--project", help="restrict to one project dir under ~/.claude/projects")
    p.add_argument("--ledger-only", action="store_true", help="upsert without printing (for the hook)")
    p.add_argument("--ledger", type=Path,
                   help="override the ledger path (default: the real ~/.local/log ledger) — "
                        "tests and the cadence hook use this so a run never touches live state")
    p.add_argument("--calibrate-spend-rate", action="store_true",
                   help="re-derive SPEND_RATE_FACTOR from the stored ledger and exit "
                        "(read-only: no scan, no upsert)")
    p.add_argument("--calibrate-failure-rate", action="store_true",
                   help="re-derive FAILURE_RATE_FACTOR from the spawn ledger and exit "
                        "(read-only)")
    p.add_argument("--calibrate-until", metavar="YYYY-MM-DD",
                   help="calibrate against the ledger as it stood before this date, so the "
                        "snapshot a shipped comment names stays reproducible as the ledger grows")
    p.add_argument("--spawn-ledger", type=Path,
                   help="override the spawn-cost ledger path (default: the real "
                        "~/.local/log one) — the failure-rate axis reads it")
    p.add_argument("--findings-store", type=Path,
                   help="override the self-diagnose findings store that fired flags are "
                        "routed into, so a run never touches live state")
    a = p.parse_args(argv)

    if a.ledger:
        # Every reader/writer below (load_ledger, write_ledger, the trailing
        # `_ledger:` line) refers to this module-global by bare name, so
        # rebinding it here is enough to redirect the whole run.
        global LEDGER
        LEDGER = a.ledger

    spawn_ledger = a.spawn_ledger or SPAWN_LEDGER

    cutoff = None
    if a.calibrate_until:
        try:
            cutoff = dt.date.fromisoformat(a.calibrate_until)
        except ValueError:
            print(f"calibrate: --calibrate-until wants YYYY-MM-DD, got "
                  f"{a.calibrate_until!r}", file=sys.stderr)
            return 1

    if a.calibrate_spend_rate:
        ledger_rows = load_ledger()
        if cutoff:
            ledger_rows = _truncated(ledger_rows, cutoff)
        print(calibrate_spend_rate(ledger_rows, a.days))
        return 0

    if a.calibrate_failure_rate:
        spawn_rows = read_spawn_rows(spawn_ledger)
        if cutoff:
            spawn_rows = _truncated_spawn_rows(spawn_rows, cutoff)
        print(calibrate_failure_rate(spawn_rows, a.days))
        return 0

    rows, scanned, skipped = upsert(a.days, a.project)
    if a.ledger_only:
        print(f"policy-scorecard: ledger upsert — scanned {scanned}, "
              f"unchanged {skipped}, total rows {len(rows)}", file=sys.stderr)
        return 0
    print(scorecard(rows, a.days, a.project,
                    spawn_rows=read_spawn_rows(spawn_ledger),
                    route=True, store_path=a.findings_store))
    print(f"\n_ledger: {LEDGER} ({len(rows)} rows; this run scanned {scanned}, "
          f"reused {skipped} unchanged)_")
    return 0


if __name__ == "__main__":
    sys.exit(main())
