import sys, time, json
sys.path.insert(0, "/home/the0/cai-wt-judge-budget/scripts")
from agentctl import advisor
from lib import ask_text

esc_ti = {"questions": [{
    "question": ("Не могу продолжить: внутренний трекер не отвечает, пробник возвращает "
                 "504 no upstreams, повторные запросы дают то же самое. К кому обратиться "
                 "за доступом и что делать дальше?"),
    "header": "Трекер лежит", "multiSelect": False,
    "options": [
        {"label": "Написать дежурному", "description": "Попросить дежурного восстановить доступ к трекеру"},
        {"label": "Подождать", "description": "Подождать, пока сервис поднимется сам"}]}]}
def_ti = {"questions": [{
    "question": ("Нашёл дефект: хук зарегистрирован с таймаутом меньше собственного бюджета "
                 "судьи, поэтому харнесс убивает его на каждом вызове. Что делаем?"),
    "header": "Дефект", "multiSelect": False,
    "options": [
        {"label": "Завести отдельной задачей (Рекомендую)", "description": "Оформить тикет в бэклоге и вернуться к нему позже"},
        {"label": "Не трогать", "description": "Оставить как есть — прямо сейчас не мешает"}]}]}
FEEDBACK = ("Зачем ты завёл отдельную задачу? Не надо было — у тебя есть все права "
            "и инструменты, чтобы починить сразу.")

cases = [
    ("outage", ask_text.flat_text(esc_ti), advisor.judge_outage_escalation),
    ("defer", ask_text.question_texts(def_ti)[0], advisor.judge_deferring_disposition),
    ("feedback", FEEDBACK, advisor.judge_feedback_signal),
]
out = {}
for name, text, fn in cases:
    rows = []
    for i in range(10):
        t0 = time.monotonic()
        v = fn(text, advisor.subprocess_runner, enabled=True, timeout=60)
        rows.append({"i": i, "verdict": bool(v), "latency_s": round(time.monotonic()-t0, 2)})
        print(f"{name} {i}: {v} {rows[-1]['latency_s']}s", flush=True)
    out[name] = rows
    json.dump(out, open("/tmp/cc-scratch/live-run/latency-sample.json", "w"), indent=2)
print("DONE")
