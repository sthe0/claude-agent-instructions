"""Latency sample for judge_landing_discipline_ask (n=16), covering
PR-proposing and direct-push-proposing AskUserQuestion menus drawn from this
project's own resolution gate.

Sixteen DISTINCT menus rather than two repeated texts (unlike the earlier
approval_ask/binary_ask samples): this judge has no regex prefilter gating
whether it runs at all -- see memory-global/leaves/
regex-not-for-semantic-classification.md and the "no prefilter" note on
judge_landing_discipline_ask itself -- so its own latency sample needs real
wording diversity rather than one text run many times.

ONE process, O_CREAT|O_EXCL pid lock, arms alternating inside it so
machine-load drift hits both equally -- same discipline as approval2.py.

Run from this directory:  python3 sample_landing_discipline.py
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
from agentctl import advisor  # noqa: E402

OUT = HERE / "landing-discipline-sample.json"
LOCK = Path("/tmp/cc-scratch/premise-loop/landing-discipline.lock")

# Each entry is a question/header line followed by every option's label and
# description, joined the way lib/ask_text.question_texts flattens a real
# AskUserQuestion payload. PR_PROPOSING menus offer or wait on a pull
# request / merge review; DIRECT_PUSH menus land straight into trunk with no
# distinct human reviewer -- the two arms judge_landing_discipline_ask must
# tell apart.
PR_PROPOSING = [
    "Задача решена, ветка запушена. Как приземляем?\n"
    "Открыть PR (Рекомендую)\n"
    "Открываю pull request в общий репозиторий и жду ревью перед мержем.\n"
    "Прямой push в trunk\n"
    "Мержу сейчас без ревью, изменения тривиальны.",

    "Стадия 2 готова, тесты зелёные (84 passed). Куда дальше?\n"
    "Создать pull request\n"
    "PR с описанием диффа, ожидание аппрува ревьюера перед мержем.\n"
    "Fast-forward в main\n"
    "Приземляю веткой без ревью.",

    "Ветка готова, хочу закрыть задачу. Как поступим?\n"
    "PR + ревью (Рекомендую)\n"
    "Открываю PR, назначаю ревьюера, жду approve.\n"
    "Push напрямую\n"
    "Закрываю без review, репозиторий мой личный.",

    "Все проверки пройдены локально. Финальный шаг?\n"
    "Открыть review request\n"
    "PR остаётся draft до explicit review от тимлида.\n"
    "Squash-merge сразу\n"
    "Мержу без внешнего ревью.",

    "Рефактор завершён, диф небольшой. Что делаем с веткой?\n"
    "PR на review (Рекомендую)\n"
    "Открываю pull request, жду хотя бы один approve.\n"
    "Закоммитить в main\n"
    "Пушу прямо в основную ветку без PR.",

    "Готово к сдаче — какой путь мержа?\n"
    "Merge request с ревьюером\n"
    "Создаю MR, ожидаю ревью перед слиянием.\n"
    "Direct push\n"
    "Merge без review, доверенная ветка.",

    "Изменения в конфиге готовы. Как выкатываем?\n"
    "Открыть pull request (Рекомендую)\n"
    "PR остаётся open до explicit approve второго разработчика.\n"
    "Push и деплой\n"
    "Пушу и деплою немедленно.",

    "Патч мелкий, но затрагивает shared-код. Дальше?\n"
    "PR с ревью коллеги\n"
    "Открываю PR, жду ревью перед мержем в main.\n"
    "Мержу сам\n"
    "Закрываю веткой без внешнего ревью.",
]

DIRECT_PUSH = [
    "Задача решена в Core-репозитории, права на push есть. Как закрываем?\n"
    "Push в trunk (Рекомендую)\n"
    "Прямой push / fast-forward merge, отдельного ревьюера в этом репо нет.\n"
    "Оставить в личной ветке\n"
    "Не приземлять сейчас.",

    "Тесты зелёные (142 passed), ветка готова. Финальный шаг?\n"
    "Fast-forward в main (Рекомендую)\n"
    "land-branch.py мержит без PR — в этом репо нет распределённого ревью.\n"
    "Удалить рабочее дерево\n"
    "Просто убрать worktree без мержа.",

    "Правки в memory-leaf, изменений кода нет. Как фиксируем?\n"
    "Коммит и push в main (Рекомендую)\n"
    "Документационные правки коммитятся прямо в main без PR-гейта.\n"
    "Оставить незакоммиченным\n"
    "Подождать другой сессии.",

    "Скрипт готов, вне production-пути. Куда его класть?\n"
    "Коммитим в репозиторий (Рекомендую)\n"
    "Push в main, инструмент полезен другим разработчикам.\n"
    "Оставить в junk/\n"
    "Локальный хелпер, не для общего дерева.",

    "Разбор беклога завершён, список тикетов обновлён. Что дальше?\n"
    "Закрыть задачу (Рекомендую)\n"
    "Изменений в коде нет, просто подтверждаем результат.\n"
    "Продолжить разбор\n"
    "Ещё не все тикеты рассмотрены.",

    # Index 5's rejected option is worded "review в другом репо" -- its own
    # vocabulary matches the judge's YES criterion, so the model in fact
    # answered YES/proposes-PR here (see README.md's landing_discipline
    # section) though this arm expects NO. Left in place: it is real
    # latency data and a genuinely ambiguous fixture, not a defect to hide.
    "Хук починен, регресс покрыт тестом. Финализируем?\n"
    "Push в main напрямую (Рекомендую)\n"
    "В этом репо push и есть приземление — ревьюера нет.\n"
    "Оставить на review в другом репо\n"
    "Этот процесс не подходит для данного репозитория.",

    "Единственная правка — опечатка в комментарии. Как поступим?\n"
    "Прямой коммит (Рекомендую)\n"
    "Одна строка, тривиально, коммитим и пушим в main.\n"
    "Открыть отдельную ветку\n"
    "Не требуется для такой мелкой правки.",

    "Эксперимент завершён, результат зафиксирован в experience-leaf. Закрываем?\n"
    "Push памяти в main (Рекомендую)\n"
    "Memory-леф коммитится напрямую, без review-гейта.\n"
    "Перепроверить вручную\n"
    "Дополнительная проверка перед фиксацией.",
]

ARMS = [
    ("pr_proposing", PR_PROPOSING, True),
    ("direct_push", DIRECT_PUSH, False),
]

LOCK.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.write(fd, str(os.getpid()).encode())
os.close(fd)
print(f"lock={LOCK} pid={os.getpid()}", flush=True)
try:
    out = {name: [] for name, _, _ in ARMS}
    for i in range(len(PR_PROPOSING)):
        for name, texts, want in ARMS:
            text = texts[i]
            t0 = time.monotonic()
            verdict, reason = advisor.judge_landing_discipline_ask(
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
