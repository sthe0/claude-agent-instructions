---
name: subagent-failure-rate-w29-w30
description: The policy-scorecard `subagent_failures` counter does not measure sub-agent failures — it matches `MALFORMED:|INCOMPLETE:|ESCALATE:` in ANY tool_result text (91% of W30 hits came from Read/Bash reading the marker-protocol source), while its denominator counts native Agent tool uses, so the ratio can and does exceed 1. The measured W29→W30 rise (1.83→2.65 per spawn) is entirely artefactual; the genuine spawn-return failure rate FELL 12.3%→5.4% over the same window. Do not calibrate a flag threshold against this counter until it is fixed.
type: reference
schema: leaf/v1
created: 2026-07-28
last_verified: 2026-07-28
---

# The sub-agent failure-rate rise (W29→W30) is a counting artefact, not a regression

## Difficulty

Desired: `policy-scorecard.py`'s `subagent_failures` counter measures how often a spawned specialist
fails, so a rise in failures-per-spawn is a quality signal worth flagging. Actual: the counter is
decoupled from spawns on **both** sides of the ratio, and the observed W29→W30 rise measures what the
fleet was *reading* that week, not how its specialists performed.

The investigation was commissioned to calibrate a new sub-agent failure-rate flag against a real
example. The finding is that no such calibration is currently possible: the counter must be fixed
first. That verdict is load-bearing, so the evidence is recorded in full.

### The two independent defects

**1. The numerator matches any text, not any specialist return.** `SUBAGENT_FAIL_RE` is
`\b(?:MALFORMED|INCOMPLETE|ESCALATE):` (`policy-scorecard.py:121`) and it is applied to the joined
text of *every* `tool_result` message (`:291`), with no check that the result came from a spawn. A
`Read` of `skills/specializations/_shared/marker-protocol.md`, a `Bash` grep over `agentctl`'s marker parser, or a
diff of a test named `test_parse_marker_malformed` each increments the counter. Attributing every
match to the tool that produced it:

| Week | Matches | From `Read` | From `Bash` | From a genuine spawn return |
|---|---|---|---|---|
| 2026-W27 | 91 | 43 | 38 | 10 |
| 2026-W28 | 205 | 84 | 75 | 46 |
| 2026-W29 | 157 | 55 | 60 | 42 |
| 2026-W30 | 342 | 146 | 166 | 30 |

In W30, **91% of the counter's value is incidental text** (312 of 342). The incidental component
nearly tripled W29→W30 (115 → 312) and is the entire source of the rise. The cause is visible in the
matched excerpts: W29–W30 is exactly when this fleet was working *on the marker protocol and the
`agentctl` marker parser*, so files containing the literal strings were read, grepped and diffed far
more than usual. The metric tracks the **topic of the week's work**, not its quality.

**2. The denominator counts a different population.** `spawns_total` counts uses of the native
`Agent`/`Task` tool (`:234-235`), but a specialist spawn in this system runs through
`spawn-specialist.py` under **Bash**. So genuine specialist returns are not in the denominator at
all. This is why the reported "failures per spawn" is **greater than 1** (W29 1.83, W30 2.65) — a
structurally impossible value for a rate, and the cheapest available tell that the two terms are
unrelated.

### What the genuine rate actually did

Restricting the numerator to results of Bash calls that actually launch a specialist
(`spawn-specialist` / `claude -p`) and using those invocations as the denominator:

| Week | Spawn invocations | Marker returns | Genuine rate | Of which `MALFORMED` | Real (`INCOMPLETE`/`ESCALATE`) |
|---|---|---|---|---|---|
| 2026-W27 | 169 | 10 | 5.9% | 10 | 0 |
| 2026-W28 | 559 | 45 | 8.1% | 42 | 3 |
| 2026-W29 | 316 | 39 | **12.3%** | 39 | 0 |
| 2026-W30 | 426 | 23 | **5.4%** | 21 | 6 |

The genuine failure rate **more than halved** across the window in which the reported metric rose by
45%. The reported metric moved *opposite* to the quantity it names.

Further, even the genuine failures are dominated by `MALFORMED:` — the wrapper could not find a
return marker in otherwise-successful output — rather than by a specialist reporting it could not
finish. Real non-completion (`INCOMPLETE:` / `ESCALATE:`) is 0 of 316 in W29 and 6 of 426 (1.4%) in
W30. `MALFORMED` is a **protocol-parse hygiene** signal and belongs on its own axis; folding it into
"failures" is what makes the genuine rate look volatile.

### Hypotheses tested

- **Definitional change in the W29→W30 range — RULED OUT.** Falsifier: `git log -S` pickaxe on both
  the regex and the increment. Both trace to `b74fb9f` (2026-06-17), well before the window, and
  `policy-scorecard.py`'s commits in range touch pricing and the cadence hook only. The definition
  did not move; the input did.
- **Spawn-mix composition shift — RULED OUT as a driver of the reported rise.** Falsifier: attribute
  the numerator to producing tool. Since 91% of W30's numerator is not a spawn return at all, no
  change in the mix of spawn kinds can move it. (The mix did shift — `general-purpose` 38→77,
  `code-reviewer` 0→4 — but the counter cannot see spawn kind.)
- **Instruction-surface change degrading marker discipline — NOT SUPPORTED as the cause of the
  rise,** though a related effect is real. Falsifier: the `MALFORMED` share of *genuine* returns.
  `MALFORMED` dominates every week including the baseline, and its rate fell (12.3%→4.9% of spawn
  invocations), so marker discipline improved rather than degraded over the window.
- **Real capability or infra regression — NOT SUPPORTED, and not fully settleable.** Falsifier: the
  genuine rate, which fell 12.3%→5.4%. Nothing in the data indicates specialists got worse. See the
  limits below for why this is "no evidence of regression" rather than "proven absent".

### What this data cannot settle

- **Whether real specialist capability changed at all.** True non-completions number 0 (W29) and 6
  (W30). At that sample size no rate comparison is meaningful; the honest statement is that the
  observable is too sparse to test, not that capability is unchanged. Settling it needs either a
  longer window or a counter that records every spawn's terminal marker (see recommendation 1).
- **The exact denominator.** `spawn-specialist|claude -p` in a Bash command is a heuristic: it counts
  a `--help` or a retried invocation as a spawn, and misses any spawn launched through a wrapper that
  names neither. The genuine rates above are therefore accurate to roughly ±10% relative, which is
  far inside the effect being described but not a precise figure.
- **Why the brief's numbers differ.** The task brief quoted 1.36→2.02 with spawns 81→200; this
  investigation measures 1.83→2.65 with Agent-tool spawns 86→129, from timestamp-accurate
  per-message aggregation. The discrepancy is consistent with the known lumpiness of windowing on the
  ledger row's `date` field (a row is stamped with its session's *last* timestamp). Both derivations
  agree on direction and on the fact that the ratio exceeds 1; neither conclusion depends on which
  figure is used.

> verified by: direct aggregation over `~/.claude-agent/projects/*/*.jsonl` on 2026-07-28, read-only,
> reusing `cost-report.py`'s `_iter_jsonl` / `_msg_text` / `_is_tool_result` / `parse_ts` and
> `policy-scorecard.py`'s own `SUBAGENT_FAIL_RE` / `AGENT_TOOLS` so the counter's semantics are not
> re-implemented. Note the live transcript root is `agent_home()/projects`
> (`~/.claude-agent/projects`); `~/.claude/projects` is the legacy root and has been frozen since
> 2026-07-22, so an aggregation rooted there returns zero rows for these weeks.

## Guidance

### RECOMMENDATION

1. **Fix the counter before flagging on it.** Attribute the numerator: count a match only when the
   `tool_result`'s `tool_use_id` maps to an `Agent`/`Task` call or to a Bash call that launched a
   specialist. Use the same population for the denominator. Until then the ratio is not a rate.
2. **Split `MALFORMED` onto its own axis.** It reports that the wrapper could not parse a marker from
   work that usually succeeded — protocol hygiene, not specialist failure. Keeping it inside
   `subagent_failures` is what makes the genuine series look volatile.
3. **Do not calibrate a failure-rate flag threshold to fire on the W29→W30 movement.** A threshold
   tuned to that movement would fire on *reading files that mention the marker vocabulary* — it would
   be loudest precisely when the fleet works on its own spawn protocol, which is a topic-sensitivity
   defect, not a quality signal. Ship the flag report-only, or gate it on the corrected counter.
4. **Treat "a rate above 1" as an invariant violation worth asserting in code.** Failures per spawn
   cannot exceed 1. An assertion or a rendered warning would have surfaced this defect the first week
   the counter ran, instead of it standing since 2026-06-17.

### Reproducing the figures

Self-contained; read-only; no repo file needed. Adjust the week list as required.

```python
# python3 - <<'PY'
import importlib.machinery, importlib.util, re
from collections import Counter, defaultdict
from pathlib import Path
SC = Path.home() / "claude-agent-instructions" / "scripts"
def load(n, p):
    s = importlib.util.spec_from_loader(n, importlib.machinery.SourceFileLoader(n, str(p)))
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
cr = load("cr", SC / "cost-report.py"); ps = load("ps", SC / "policy-scorecard.py")
STRICT = re.compile(r"spawn-specialist|claude\s+-p\b")
MARK = re.compile(r"\b(MALFORMED|INCOMPLETE|ESCALATE):")
spawn, genuine, incid, marks = Counter(), Counter(), defaultdict(Counter), defaultdict(Counter)
for f in sorted(cr.PROJECTS_DIR.glob("*/*.jsonl")):
    info = {}
    for d in cr._iter_jsonl(f):
        raw = d.get("timestamp") or (d.get("message") or {}).get("ts")
        if not raw: continue
        try: y, w, _ = cr.parse_ts(raw).isocalendar()
        except Exception: continue
        wk = f"{y}-W{w:02d}"; c0 = (d.get("message") or {}).get("content")
        if d.get("type") == "assistant" and isinstance(c0, list):
            for c in c0:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id"):
                    sp = bool(STRICT.search((c.get("input") or {}).get("command", "") or ""))
                    info[c["id"]] = (c.get("name"), sp)
                    if sp: spawn[wk] += 1
        elif d.get("type") == "user" and cr._is_tool_result(c0) and isinstance(c0, list):
            for c in c0:
                if not (isinstance(c, dict) and c.get("type") == "tool_result"): continue
                r = c.get("content"); t = r if isinstance(r, str) else cr._msg_text(r)
                if not t or not ps.SUBAGENT_FAIL_RE.search(t): continue
                nm, sp = info.get(c.get("tool_use_id"), ("(unknown)", False))
                if sp or nm in ps.AGENT_TOOLS:
                    genuine[wk] += 1
                    for m in set(MARK.findall(t)): marks[wk][m] += 1
                else: incid[wk][nm] += 1
for wk in ["2026-W27", "2026-W28", "2026-W29", "2026-W30"]:
    print(wk, "spawns", spawn[wk], "genuine", genuine[wk],
          "incidental", dict(incid[wk]), "markers", dict(marks[wk]))
# PY
```

## See also

- [[policy-effectiveness-tracking]] — the standing instrument this counter belongs to, and where a
  corrected failure-rate flag would be declared.
- [[quality-regression-investigation]] — the runbook shape this investigation follows (baseline →
  hypotheses with cheap falsifiers → verdict).
- [[workflow-debug-investigation]] — the ordered method: establish the baseline and rule out a
  definitional artefact before asserting a regression.
- [[doubt-own-snapshot]] — the same failure class one level up: a metric that passed every check it
  was given (it computed, it rendered, it moved) while measuring the wrong thing.
