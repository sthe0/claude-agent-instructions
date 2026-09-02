"""Latency sample for judge_silent_closure (n=16), covering genuine silent
closures (Cluster A: a fork-point decision taken without asking; Cluster C: a
completion declared without asking) against near-miss texts that still trip the
silent_closure_detect prefilter but must judge NO (a real question posed despite
decision/completion wording, a decision framed as the only option, a routine
sub-step, or a status update that says more work remains).

Sixteen DISTINCT texts, like sample_landing_discipline.py and for the same
reason: this judge has no self-contained punctuation prefilter (unlike
judge_binary_ask) -- see memory-global/leaves/
regex-not-for-semantic-classification.md -- so its latency sample needs real
wording diversity rather than one text run many times.

ONE process, O_CREAT|O_EXCL pid lock, arms alternating inside it so
machine-load drift hits both equally -- same discipline as
sample_landing_discipline.py.

Run via the approved `python3 -m pytest` entry point (no bare script execution
in this environment):
    python3 -m pytest samples/judge-latency/sample_silent_closure.py -q -s
pytest imports the module, which is enough to run every statement below; there
are no test_ functions to collect, so pytest reports "no tests ran" and that is
expected -- the sampling happens as an import side effect.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
from agentctl import advisor  # noqa: E402

OUT = HERE / "silent-closure-sample.json"
LOCK = Path("/tmp/cc-scratch/premise-loop/silent-closure.lock")

# Genuine silent closures: a fork-point decision or a completion declaration,
# with no question posed anywhere in the text. Expect YES.
SIGNAL = [
    "The two options are equally valid on paper, but I'll go with the "
    "JWT-based approach since it needs no new infra. Implementing now.",

    "Готово — задача решена, тесты зелёные, ветку запушил в main.",

    "I've decided to keep the retry count at 3 rather than bumping it to 5; "
    "that matches the rest of the codebase's convention. Moving on to the "
    "next file.",

    "Все правки внесены, документация обновлена, всё сделано.",

    "Between rewriting the parser and patching the regex, I'm going with the "
    "regex patch — it's smaller and touches less code. Committing now.",

    "This completes the migration: every table has been backfilled and the "
    "old column dropped. The task is complete.",

    "Учитывая, что оба варианта рабочие, остановлюсь на варианте с "
    "кэшированием на уровне сервиса — он проще для отладки. Начинаю "
    "реализацию.",

    "Wrapped up the refactor — all call sites updated, tests passing, "
    "nothing left to do here.",
]

# Near-misses that still trip the prefilter's decision/completion cues but must
# judge NO: a real question posed despite the wording, a decision framed as the
# only option, a routine sub-step, or a status update naming remaining work.
# Expect NO.
NOT_SIGNAL = [
    "There are two ways to do this — should I go with the JWT approach or "
    "the session-cookie one?",

    "Готово — я закончил читать конфигурационный файл, дальше перейду к "
    "тестам.",

    "The only reasonable option here is to use the existing retry helper — "
    "there's no real alternative worth considering, so I used it. What "
    "would you like me to work on next?",

    "I finished running the test suite locally; still need to check CI "
    "before calling this done.",

    "Часть работы завершена — миграция схемы прошла успешно, но данные ещё "
    "не перенесены полностью.",

    "I'll go with whichever approach you prefer — let me know which one to "
    "implement.",

    "Готово. Хотите, чтобы я запушил изменения, или оставить в рабочей "
    "ветке?",

    "Finished writing the draft outline for the README section — happy to "
    "expand any part further if useful?",
]

ARMS = [
    ("signal", SIGNAL, True),
    ("not_signal", NOT_SIGNAL, False),
]

LOCK.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.write(fd, str(os.getpid()).encode())
os.close(fd)
print(f"lock={LOCK} pid={os.getpid()}", flush=True)
try:
    out = {name: [] for name, _, _ in ARMS}
    for i in range(len(SIGNAL)):
        for name, texts, want in ARMS:
            text = texts[i]
            t0 = time.monotonic()
            verdict, reason = advisor.judge_silent_closure(
                text, advisor.subprocess_runner, enabled=True, timeout=120,
            )
            row = {"i": i, "verdict": bool(verdict), "reason": reason,
                   "ok": bool(verdict) == want,
                   "latency_s": round(time.monotonic() - t0, 2)}
            out[name].append(row)
            print(f"{name} {i}: {verdict} {row['latency_s']}s", flush=True)
            OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("DONE")
finally:
    os.unlink(str(LOCK))
