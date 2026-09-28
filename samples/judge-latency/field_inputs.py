"""Fourth latency sample: REAL field latency for feedback_signal, binary_ask and
deferring_disposition, measured on genuine prefilter-passing inputs pulled from
this machine's own session transcripts -- as opposed to every prior sample in
this directory (approval2.py, topup2.py, drift.py), which measures synthetic
hand-written texts chosen to exercise a specific YES/NO branch.

Difficulty removed: a hand-written sample proves the judge CAN answer a clean
example quickly; it says nothing about the latency of the messy, long,
code-heavy, multi-language text a real turn actually produces. The three input
BUILDERS below (prefilter + text-extraction) are imported from the exact
production sites -- si_feedback_detect for feedback_signal,
hook-turn-end-gate.py's own `_assistant_text_of` + advisor.binary_ask_prefilter
for binary_ask, lib/ask_text.py + hook-deferring-disposition-gate.py's own
`_prefilter` for deferring_disposition -- never re-implemented here, so a
future change to any one of those predicates is picked up by this sampler for
free rather than silently drifting out of sync with what the hooks actually run.

Each call runs ALONE (no interleaving) under an UNCENSORED 300s per-call
ceiling -- far above anything the live ledger or any prior sample has shown --
so a slow call is a real observation, not the sampler's own bound (see
drift.py's docstring on right-censoring for why this matters: a population
where most calls hit the ceiling cannot say by how much the ceiling should
move). `AGENTCTL_JUDGE_LEDGER` is redirected before `agentctl.advisor` is
imported, exactly as drift.py and approval2.py do, so these calls never land
in the production ledger that judge-usage-report.py counts real hook
executions from.

The committed output (field-inputs-sample.json) carries ONLY
{judge, prompt_chars, duration, timed_out, verdict} per row, plus
target/available/n per judge -- never the prompt text and never a hash or
digest of it, because this repository is public and a real user turn or
AskUserQuestion menu can carry anything a session contained. `prompt_chars` is
read back from the judge execution ledger's own `call` row rather than
recomputed here, because that is the one place (subprocess_runner) that
already knows `len(stdin)` of the exact templated prompt actually sent --
recomputing it here would mean re-deriving each judge's private prompt
template, which is exactly the re-implementation this sampler exists to avoid.

Run from this directory:  python3 field_inputs.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parents[1] / "scripts"

# A direct-run caller (`python3 field_inputs.py <dir>`) may pass the scratch
# dir as its first positional argument instead of the env var -- convenient
# when the invoking shell's permission grant covers only the literal
# `python3 samples/judge-latency/field_inputs.py <args>` command string, not
# an env-var-prefixed variant of it. Gated on `__name__ == "__main__"` so an
# import under pytest (whose own sys.argv is the test runner's, not this
# script's) never mistakes a test path for a scratch dir.
if __name__ == "__main__" and len(sys.argv) > 1:
    os.environ["FIELD_INPUTS_SCRATCH_DIR"] = sys.argv[1]

# Scratch home for the redirected ledger and incremental partial writes. A
# caller doing a real run overrides this to point at a durable evidence dir
# (this stage's own run does, via the env var or the CLI arg above) rather
# than /tmp, which can be cleared between a crash and its resume.
SCRATCH = Path(os.environ.get("FIELD_INPUTS_SCRATCH_DIR", "/tmp/cc-scratch/field-inputs"))
SCRATCH.mkdir(parents=True, exist_ok=True)

# Set BEFORE importing advisor, which resolves the ledger path through
# lib.judge_ledger.ledger_path() -- see that function's docstring: it reads
# the env var fresh on every call, so this only has to run once, here.
os.environ["AGENTCTL_JUDGE_LEDGER"] = str(SCRATCH / "field-inputs-judge-ledger.jsonl")

sys.path.insert(0, str(SCRIPTS_DIR))
from agentctl import advisor  # noqa: E402
from lib import ask_text  # noqa: E402
from lib import config_root  # noqa: E402
from lib import judge_ledger  # noqa: E402
import si_feedback_detect  # noqa: E402
import transcript_read  # noqa: E402

# Bound by NAME to the production symbols, never copied -- so
# `field_inputs.find_signals is si_feedback_detect.find_signals` holds, and a
# test can assert that identity instead of trusting a behavioral echo.
strip_injected_context = si_feedback_detect.strip_injected_context
find_signals = si_feedback_detect.find_signals
binary_ask_prefilter = advisor.binary_ask_prefilter
question_texts = ask_text.question_texts
option_texts = ask_text.option_texts
question_stems = ask_text.question_stems


def _load_hook(filename: str):
    """Load a dash-named hook module, reusing an already-loaded instance from
    sys.modules -- so a caller (this module, or a test) that loads the same
    hook twice gets back the SAME module object and SAME function objects,
    rather than two independently-exec'd copies that merely behave alike."""
    name = filename.replace("-", "_").removesuffix(".py")
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_TURN_END = _load_hook("hook-turn-end-gate.py")
_DEFERRING = _load_hook("hook-deferring-disposition-gate.py")

assistant_text_of = _TURN_END._assistant_text_of
deferring_prefilter = _DEFERRING._prefilter

OUT = HERE / "field-inputs-sample.json"
PARTIAL = SCRATCH / "field-inputs-sample.partial.json"

TARGET_N = 32
TIMEOUT_S = 300
DAYS = 30
# Fixed seed for the sample draw: today's date at authoring time, so a re-run
# against an unchanged transcript pool reproduces the same selection.
SEED = 20260928


def _entry_ts(entry: dict):
    ts = entry.get("timestamp") if isinstance(entry, dict) else None
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_recent_entries(days: int = DAYS):
    """Every transcript entry across the full agent-home project tree
    (lib.config_root.iter_transcripts -- both `agent_home()` and
    `harness_config_root()`, not a hardcoded ~/.claude/projects) whose own
    timestamp falls inside the last `days`. An entry with no parseable
    timestamp is included rather than dropped: excluding it would bias the
    pool toward whichever transcript shape happens to carry timestamps."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for path in config_root.iter_transcripts():
        try:
            for entry in transcript_read.iter_transcript(path):
                ts = _entry_ts(entry)
                if ts is not None and ts < cutoff:
                    continue
                yield entry
        except OSError:
            continue


def collect_feedback_signal_inputs() -> list[str]:
    """Real user-turn texts that pass si_feedback_detect.find_signals once
    injected context is stripped -- exactly hook-turn-end-gate.py's own
    feedback_signal call site prefilter (strip_injected_context then
    find_signals on the stripped text)."""
    seen: list[str] = []
    for entry in _iter_recent_entries():
        msg = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = transcript_read.message_text(msg)
        if not text.strip():
            continue
        stripped = strip_injected_context(text)
        if stripped and find_signals(stripped):
            seen.append(stripped)
    return seen


def collect_binary_ask_inputs() -> list[str]:
    """Real assistant-turn texts that pass advisor.binary_ask_prefilter --
    exactly hook-turn-end-gate.py's own binary_ask call site (the turn's
    assistant prose via `_assistant_text_of`, then the punctuation prefilter)."""
    seen: list[str] = []
    for entry in _iter_recent_entries():
        msg = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        text = assistant_text_of([entry])
        if text and binary_ask_prefilter(text):
            seen.append(text)
    return seen


def collect_deferring_disposition_inputs() -> list[str]:
    """Real AskUserQuestion menu texts (one entry per question) that pass
    hook-deferring-disposition-gate.py's own `_prefilter` applied to the
    question's OPTION text -- exactly the hook's decide() loop, which never
    runs the prefilter over the question stem, only its options."""
    seen: list[str] = []
    for entry in _iter_recent_entries():
        msg = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for block in transcript_read.tool_use_blocks(msg):
            if block.get("name") != "AskUserQuestion":
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            full_texts = question_texts(tool_input)
            opt_texts = option_texts(tool_input)
            for full_text, opt_text in zip(full_texts, opt_texts):
                if deferring_prefilter(opt_text):
                    seen.append(full_text)
    return seen


# (judge, collector, judge_fn) -- the judge_fn signature is shared across all
# three: (text, runner, *, enabled=True, timeout, remaining=None, ceiling=None).
JUDGES = [
    ("feedback_signal", collect_feedback_signal_inputs, advisor.judge_feedback_signal),
    ("binary_ask", collect_binary_ask_inputs, advisor.judge_binary_ask),
    ("deferring_disposition", collect_deferring_disposition_inputs, advisor.judge_deferring_disposition),
]


def draw_sample(pool: list[str], *, target: int = TARGET_N, seed: int = SEED) -> list[str]:
    """min(target, len(pool)) items, deterministically -- ALL of the pool when
    it has fewer than target, matching this stage's "use all available if
    fewer" instruction rather than sampling with replacement to pad it out."""
    if len(pool) <= target:
        return list(pool)
    return random.Random(seed).sample(pool, target)


def build_row(judge: str, prompt: str, *, prompt_chars: "int | None", duration, timed_out: bool, verdict) -> dict:
    """The committed row shape: metadata only. `prompt` is accepted so a
    caller cannot forget it exists, but it never appears in the return value --
    that is the property test_field_sample_carries_no_input_text pins."""
    return {
        "judge": judge,
        "prompt_chars": prompt_chars if prompt_chars is not None else len(prompt),
        "duration": duration,
        "timed_out": bool(timed_out),
        "verdict": bool(verdict),
    }


def run_judge_once(judge: str, judge_fn, prompt: str) -> dict:
    """One real, uncensored call. `prompt_chars`/`duration`/`timed_out` are read
    back from the judge execution ledger's own `call` row -- the authoritative
    record subprocess_runner just wrote for this exact invocation -- rather
    than recomputed, so this sampler never has to know a judge's private
    prompt-template format."""
    verdict, _reason = judge_fn(prompt, advisor.subprocess_runner, enabled=True, timeout=TIMEOUT_S)
    records = judge_ledger.read_records()
    call_rows = [r for r in records if r.get("kind") == "call"]
    last = call_rows[-1] if call_rows else {}
    return build_row(
        judge, prompt,
        prompt_chars=last.get("prompt_chars"),
        duration=last.get("duration"),
        timed_out=bool(last.get("timed_out")),
        verdict=verdict,
    )


def sample_judge(judge: str, collector, judge_fn) -> dict:
    pool = collector()
    drawn = draw_sample(pool)
    rows: list[dict] = []
    for i, prompt in enumerate(drawn):
        t0 = time.monotonic()
        row = run_judge_once(judge, judge_fn, prompt)
        elapsed = round(time.monotonic() - t0, 1)
        rows.append(row)
        print(
            f"{judge} {i + 1}/{len(drawn)}: duration={row['duration']} "
            f"timed_out={row['timed_out']} verdict={row['verdict']} "
            f"(wall {elapsed}s)",
            flush=True,
        )
        _write_partial(judge, rows)
    return {"target": TARGET_N, "available": len(pool), "n": len(rows), "rows": rows}


_partial_state: dict = {}


def _write_partial(judge: str, rows: list[dict]) -> None:
    _partial_state[judge] = rows
    PARTIAL.write_text(json.dumps(_partial_state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    print(f"scratch={SCRATCH} ledger={os.environ['AGENTCTL_JUDGE_LEDGER']} out={OUT}", flush=True)
    started = time.monotonic()
    out: dict = {}
    for judge, collector, judge_fn in JUDGES:
        out[judge] = sample_judge(judge, collector, judge_fn)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DONE in {round(time.monotonic() - started)}s, wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
