"""Second latency sample for judge_approval_ask (n=16 per arm), taken because
the first row's max no longer bounds observed latency.

`approval-sample.json` put the approval_ask row's max at 11.42s, and
`ceil(max) + 1` made hook-plan-delivery-gate.py's whole-invocation budget 13s.
On a live session the same judge, on the real ask text of that session, returned
in 12.71 / 12.51 / 11.96s -- past the ceiling the first sample computed, and the
judge ledger recorded two consecutive `timed_out: true` rows for it minutes
apart. A ceiling derived from a max the population exceeds is not a ceiling, so
the row is re-derived from BOTH samples' four series rather than replaced: the
first sample was not wrong, it was too small a draw of the same population.

Identical arms and identical discipline to approval.py -- ONE process under an
O_CREAT|O_EXCL pid lock, arms alternating inside it so machine-load drift hits
both equally -- so the two files' series pool as one sample. Only the lock path
and the output file differ, which is what lets this run be told apart from the
first while nothing else about the measurement changes.

Run from this directory:  python3 approval2.py
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
from agentctl import advisor  # noqa: E402

OUT = HERE / "approval2-sample.json"
LOCK = Path("/tmp/cc-scratch/premise-loop/approval2.lock")
N = 16

APPROVAL = (
    "Утверждаем план?\n"
    "Утверждение\n"
    "Утверждаю (Рекомендую)\n"
    "Десять этапов, механизм первым; исполняю по этапам с проверкой на каждом.\n"
    "Показать полный план [SHOW_FULL_PLAN]\n"
    "Показать разбор всех десяти этапов целиком, прежде чем решать.\n"
    "Доработать\n"
    "Скажите, что поменять в объёме или порядке."
)

NOT_APPROVAL = (
    "Как закрывать код-ревью этапа 7?\n"
    "Ревью\n"
    "Ещё один раунд, ~$1 (Рекомендую)\n"
    "Четвёртый раунд вернул fail; пятый может найти ещё мелочь.\n"
    "Закрыть сейчас\n"
    "Зафиксировать вердикт и идти к этапу 8."
)

ARMS = [
    ("approval", APPROVAL, True),
    ("not_approval", NOT_APPROVAL, False),
]

LOCK.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.write(fd, str(os.getpid()).encode())
os.close(fd)
print(f"lock={LOCK} pid={os.getpid()}", flush=True)
try:
    out = {name: [] for name, _, _ in ARMS}
    for i in range(N):
        for name, text, want in ARMS:
            t0 = time.monotonic()
            verdict, reason = advisor.judge_approval_ask(
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
