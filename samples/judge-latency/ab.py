"""Paired A/B: standard `claude -p` invocation vs a lean one (no CLAUDE.md /
memory / skills), alternating so machine load drift hits both arms equally.
Single process on purpose — a second concurrent copy invalidates the numbers."""
import sys, time, json, subprocess, os
sys.path.insert(0, "/home/the0/cai-wt-judge-budget/scripts")
from agentctl import advisor
from lib import ask_text

OUT = "/tmp/cc-scratch/live-run/ab-sample.json"
LOCK = "/tmp/cc-scratch/live-run/ab.lock"

LEAN = [
    "--system-prompt",
    "You are a strict binary classifier. Follow the user's instructions exactly.",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--no-session-persistence",
]


def make_runner(lean):
    def run(argv, *, timeout=advisor._ADVISOR_TIMEOUT_S):
        if lean:
            argv = argv[:-1] + LEAN + [argv[-1]]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return advisor.RunResult(p.returncode, p.stdout, p.stderr)
        except subprocess.TimeoutExpired:
            return advisor.RunResult(1, "", "advisor timed out")
    return run


def_ti = {"questions": [{
    "question": ("Нашёл дефект: хук зарегистрирован с таймаутом меньше собственного бюджета "
                 "судьи, поэтому харнесс убивает его на каждом вызове. Что делаем?"),
    "header": "Дефект", "multiSelect": False,
    "options": [
        {"label": "Завести отдельной задачей (Рекомендую)", "description": "Оформить тикет в бэклоге и вернуться к нему позже"},
        {"label": "Не трогать", "description": "Оставить как есть — прямо сейчас не мешает"}]}]}
esc_ti = {"questions": [{
    "question": ("Не могу продолжить: внутренний трекер не отвечает, пробник возвращает "
                 "504 no upstreams, повторные запросы дают то же самое. К кому обратиться "
                 "за доступом и что делать дальше?"),
    "header": "Трекер лежит", "multiSelect": False,
    "options": [
        {"label": "Написать дежурному", "description": "Попросить дежурного восстановить доступ к трекеру"},
        {"label": "Подождать", "description": "Подождать, пока сервис поднимется сам"}]}]}

CASES = [
    ("defer", ask_text.question_texts(def_ti)[0], advisor.judge_deferring_disposition, True, 8),
    ("outage", ask_text.flat_text(esc_ti), advisor.judge_outage_escalation, True, 6),
]

fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.write(fd, str(os.getpid()).encode())
os.close(fd)
try:
    out = {}
    for name, text, fn, want, n in CASES:
        for arm in ("std", "lean"):
            out.setdefault(f"{name}_{arm}", [])
        for i in range(n):
            for arm, lean in (("std", False), ("lean", True)):
                t0 = time.monotonic()
                v = fn(text, make_runner(lean), enabled=True, timeout=120)
                row = {"i": i, "verdict": bool(v), "ok": bool(v) == want,
                       "latency_s": round(time.monotonic() - t0, 2)}
                out[f"{name}_{arm}"].append(row)
                print(f"{name}/{arm} {i}: {v} {row['latency_s']}s", flush=True)
                json.dump(out, open(OUT, "w"), indent=2)
    print("DONE")
finally:
    os.unlink(LOCK)
