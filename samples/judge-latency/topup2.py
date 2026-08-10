"""Precondition 1 of stage 3: take a judge_binary_ask sample (n>=16) and top the
judge_feedback_signal sample up from n=10 to n>=16.

ONE process on purpose, under an O_CREAT|O_EXCL lock that carries the pid: two
concurrent copies of the runner already inflated latency twofold once, and a
sample taken while the machine is contended is invalid. The two arms alternate
inside this single process so machine-load drift hits both equally.
"""
import sys, time, json, os
sys.path.insert(0, "/home/the0/cai-wt-judge-budget/scripts")
from agentctl import advisor

OUT = "/tmp/cc-scratch/live-run/topup2-sample.json"
LOCK = "/tmp/cc-scratch/live-run/topup2.lock"
N = 16

# A real end-of-turn message that ends in a one-of-N confirm question: the exact
# shape the Stop-gate blocker exists to catch. Passes the punctuation prefilter.
BINARY_ASK = (
    "Стадии 1-2 приземлены в ветке, тесты зелёные (27 + 166 passed). "
    "Дальше по плану — калибровка потолков по выборке. "
    "Начинаем стадию 3 сейчас или сперва приземлить ветку в trunk?"
)

# Same text the n=10 sample used, so the top-up extends ONE population rather
# than mixing two.
FEEDBACK = ("Зачем ты завёл отдельную задачу? Не надо было — у тебя есть все права "
            "и инструменты, чтобы починить сразу.")

ARMS = [
    ("binary_ask", BINARY_ASK, advisor.judge_binary_ask, True),
    ("feedback", FEEDBACK, advisor.judge_feedback_signal, True),
]

fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.write(fd, str(os.getpid()).encode())
os.close(fd)
print(f"lock={LOCK} pid={os.getpid()}", flush=True)
try:
    out = {name: [] for name, _, _, _ in ARMS}
    for i in range(N):
        for name, text, fn, want in ARMS:
            t0 = time.monotonic()
            v = fn(text, advisor.subprocess_runner, enabled=True, timeout=120)
            row = {"i": i, "verdict": bool(v), "ok": bool(v) == want,
                   "latency_s": round(time.monotonic() - t0, 2)}
            out[name].append(row)
            print(f"{name} {i}: {v} {row['latency_s']}s", flush=True)
            json.dump(out, open(OUT, "w"), indent=2)
    print("DONE")
finally:
    os.unlink(LOCK)
