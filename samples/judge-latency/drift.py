"""Third latency sample: judge_feedback_signal, judge_binary_ask and
judge_outage_escalation -- the three judges hook-turn-end-gate.py runs on one
budget -- taken because their rows no longer bound what production observes.

Two DIFFERENT reasons bring the three judges into one run, and the result has to
keep them apart:

  * `binary_ask` and `feedback_signal` are here because their live populations
    are RIGHT-CENSORED and cannot be re-derived from. Since 2026-08-19 17:00 the
    ledger shows binary_ask n=76 (min 11.3 / p50 13.0 / max 13.2, 69 killed) at
    ceiling 13, and feedback_signal n=250 (p50 16.0 / max 16.2, 244 killed) at
    ceiling 16. In both the observed max IS the ceiling and the median sits at
    it: when a call killed at ceiling C is recorded as C, a sample where 91-98%
    of calls are killed has a max equal to C by construction, so `ceil(max) + 1`
    computed from it returns C + 1 whether the true latency is 14s or 40s. Such
    a population can prove a ceiling is too low; it cannot say by how much. Only
    a sample taken under a bound the population does not reach can.
  * `outage_escalation` is here because stage 3's budget inequality rests on its
    p90 (`call_floor_s` = ceil(19.16) = 20, the trailing term of
    `required_budget_s("hook-turn-end-gate.py")`) and the live data has already
    contradicted its max: 2 of its 7 calls at ceiling 27 were killed, at 27.04s
    and 27.03s, against a declared max of 25.96s. Seven calls cannot re-derive a
    row. This arm SETTLES an anchor rather than repairing a rate, and it is
    decisive either way -- if it too runs above 25.96s the kills are drift, and
    if it does not, stage 3 proceeds on a re-observed rather than an inherited
    number.

Method, copied from approval2.py rather than reinvented, because this is the
same situation that file was written for (a row whose max the population had
outgrown, re-derived from both regimes instead of replaced):

  * ONE process under an O_CREAT|O_EXCL pid lock, all six arms alternating
    inside it so machine-load drift hits every arm equally. The lock guards
    against a second copy of the sampler; it cannot guard against other work on
    this machine, which is what invalidated topup-sample.json -- so the run also
    needs the machine otherwise idle (samples/judge-latency/README.md).
  * A 60s per-call timeout, far above anything either the old rows or the live
    ledger have shown. That makes these latencies UNCENSORED: every recorded max
    is an observation rather than the harness's own bound. 60s is a sampling
    instrument and is never committed as a ceiling.
  * Two arms per judge, one that should classify YES and one NO, as
    approval-sample.json does with approval/not_approval: latency can differ
    with the answer, and a one-sided sample measures half the population.
    `not_binary_ask` still ends in a question mark on purpose -- judge_binary_ask
    applies its own punctuation prefilter and returns without calling the model
    on anything that does not, which would record a latency of ~0 for a call that
    never happened.
  * N=16 per arm (32 per judge), the size of every existing per-regime sample,
    and enough for a nearest-rank p90 to rest on more than one observation.
  * A call that returned NO ANSWER is not an observation and is never recorded.
    The first run of this file recorded 66 such calls -- all six arms went from
    a real verdict to `judge exited non-zero (fail-open)` at i=5 and stayed
    there -- as `latency_s` values of ~10s, the cost of a `claude -p` that
    starts and fails rather than of a judge that answers. Nothing downstream
    would have caught it: test_every_row_re_derives_from_the_samples_it_cites
    reads EVERY entry of a cited series and filters on nothing, so those 10s
    non-calls would have re-derived a row that looks perfectly consistent and
    describes a latency no judge has. So each arm retries instead of recording,
    and a run that cannot get answers stops rather than filling up with them.

The YES arms reuse the EXACT prompt texts of the series already in each row's
provenance (ab.py's escalation ask, topup2.py's binary-ask turn and feedback
message), so the merged population is one population and not two prompts pooled.

Run from this directory:  python3 drift.py
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRATCH = Path("/tmp/cc-scratch/judge-ceiling-drift")
SCRATCH.mkdir(parents=True, exist_ok=True)

# Send the judges' execution-ledger rows to a scratch file instead of the live
# one. These 96 calls run at a 60s ceiling that no hook has ever passed, so in
# the production ledger they would read as a judge regime that never existed --
# and that ledger is exactly what --latency reports on and what stage 4's drift
# check reads. Set before importing advisor, which resolves the path through
# lib.judge_ledger.ledger_path().
os.environ["AGENTCTL_JUDGE_LEDGER"] = str(SCRATCH / "drift-judge-ledger.jsonl")

sys.path.insert(0, str(HERE.parents[1] / "scripts"))
from agentctl import advisor  # noqa: E402
from lib import ask_text  # noqa: E402

OUT = HERE / "drift-sample.json"
PARTIAL = SCRATCH / "drift-sample.partial.json"
LOCK = SCRATCH / "drift.lock"
N = 16
TIMEOUT_S = 60

# Retry budget for calls that came back without an answer. RETRY_SLEEP_S is a
# pause, not a measurement: the failing regime the first run hit was flat and
# machine-wide, so a retry that follows instantly only converts one wasted call
# into two. GIVE_UP_AFTER consecutive no-answers ends the run, because that is
# no longer noise -- it is a machine that cannot produce a sample right now, and
# continuing would spend an hour proving it.
RETRY_SLEEP_S = 10
GIVE_UP_AFTER = 5

# Reasons that mean the call produced NO ANSWER, so there is no latency to
# record (advisor's three-valued contract: reason is "" or None for a genuine
# verdict, and a non-empty "...(fail-open)" string otherwise). "unparseable" is
# deliberately NOT here: the model answered and the wall-clock is a real
# observation of this judge: only the parse of its first line failed.
NO_ANSWER_MARKERS = (
    "exited non-zero", "returned no output", "timed out", "raised",
    "disabled", "no runner", "no text",
)


def no_answer(reason: "str | None") -> bool:
    return bool(reason) and any(m in reason for m in NO_ANSWER_MARKERS)

# --- binary_ask ---------------------------------------------------------------

# topup2.py's exact text: the turn-end message that ends in a one-of-N confirm
# question, i.e. the shape the Stop-gate blocker exists to catch.
BINARY_ASK = (
    "Стадии 1-2 приземлены в ветке, тесты зелёные (27 + 166 passed). "
    "Дальше по плану — калибровка потолков по выборке. "
    "Начинаем стадию 3 сейчас или сперва приземлить ветку в trunk?"
)

# Ends in a question mark so the punctuation prefilter passes and the model
# actually runs, but asks for a free-text answer rather than a choice between
# named branches -- nothing an AskUserQuestion click-gate could carry.
NOT_BINARY_ASK = (
    "Потолок судьи считается из выборки, а выборка лежит в samples/judge-latency/. "
    "Под каким именем сохранить новый файл выборки?"
)

# --- feedback_signal ----------------------------------------------------------

# ab.py's and topup2.py's exact text.
FEEDBACK = (
    "Зачем ты завёл отдельную задачу? Не надо было — у тебя есть все права "
    "и инструменты, чтобы починить сразу."
)

# A neutral task instruction that describes corrective machinery without
# evaluating the assistant's own conduct -- the meta/analytical false positive
# this judge was introduced to remove.
NOT_FEEDBACK = (
    "Опиши, как устроен hook-turn-end-gate.py: каких судей он вызывает, в каком "
    "порядке и как делит между ними общий бюджет вызова."
)

# --- outage_escalation --------------------------------------------------------

# ab.py's exact escalation ask, rendered through the same ask_text.flat_text.
ESCALATION_TI = {"questions": [{
    "question": ("Не могу продолжить: внутренний трекер не отвечает, пробник возвращает "
                 "504 no upstreams, повторные запросы дают то же самое. К кому обратиться "
                 "за доступом и что делать дальше?"),
    "header": "Трекер лежит", "multiSelect": False,
    "options": [
        {"label": "Написать дежурному", "description": "Попросить дежурного восстановить доступ к трекеру"},
        {"label": "Подождать", "description": "Подождать, пока сервис поднимется сам"}]}]}

# A failure that is already diagnosed and has a proposed fix, so there is
# nothing live being escalated -- the judge's own NO case.
NOT_OUTAGE = (
    "Вчерашние 504 от трекера объяснились: истёк OAuth-токен в ~/.tracker-token, "
    "и клиент отдавал ошибку шлюза вместо 401. Токен перевыпущен, запросы проходят, "
    "в ретро добавлю проверку срока жизни токена перед запуском."
)

# (series, prompt, judge, expected verdict). One entry per arm; the loop walks
# this list once per iteration so all six arms interleave.
ARMS = [
    ("feedback", FEEDBACK, advisor.judge_feedback_signal, True),
    ("not_feedback", NOT_FEEDBACK, advisor.judge_feedback_signal, False),
    ("binary_ask", BINARY_ASK, advisor.judge_binary_ask, True),
    ("not_binary_ask", NOT_BINARY_ASK, advisor.judge_binary_ask, False),
    ("outage", ask_text.flat_text(ESCALATION_TI), advisor.judge_outage_escalation, True),
    ("not_outage", NOT_OUTAGE, advisor.judge_outage_escalation, False),
]

fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.write(fd, str(os.getpid()).encode())
os.close(fd)
print(f"lock={LOCK} pid={os.getpid()} ledger={os.environ['AGENTCTL_JUDGE_LEDGER']}", flush=True)
started = time.monotonic()
try:
    out = {name: [] for name, _, _, _ in ARMS}
    discarded = 0
    consecutive = 0
    for i in range(N):
        for name, text, fn, want in ARMS:
            while True:
                t0 = time.monotonic()
                verdict, reason = fn(
                    text, advisor.subprocess_runner, enabled=True, timeout=TIMEOUT_S,
                )
                elapsed = round(time.monotonic() - t0, 2)
                if not no_answer(reason):
                    consecutive = 0
                    break
                discarded += 1
                consecutive += 1
                print(f"{name} {i}: NO ANSWER after {elapsed}s ({reason}) — "
                      f"discarded, {consecutive} in a row", flush=True)
                if consecutive >= GIVE_UP_AFTER:
                    raise SystemExit(
                        f"{consecutive} calls in a row came back without an answer; "
                        f"stopping with {sum(len(v) for v in out.values())} of "
                        f"{N * len(ARMS)} observations taken. The partial run is at "
                        f"{PARTIAL} and {OUT} was NOT written — a sample missing its "
                        f"slow tail is not a smaller sample, it is a different one."
                    )
                time.sleep(RETRY_SLEEP_S)
            row = {"i": i, "verdict": bool(verdict), "reason": reason,
                   "ok": bool(verdict) == want, "latency_s": elapsed}
            out[name].append(row)
            print(f"{name} {i}: {verdict} {elapsed}s", flush=True)
            # Progress goes to scratch, not to OUT: a run that stops early must
            # not leave behind a file with a sample's name and a sample's shape.
            PARTIAL.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DONE in {round(time.monotonic() - started)}s, "
          f"{discarded} no-answer call(s) discarded and retried")
finally:
    os.unlink(str(LOCK))
