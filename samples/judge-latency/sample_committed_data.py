"""Latency sample for judge_committed_data (n=16), the judge behind
scripts/hook-guard-committed-data.py.

Why this file exists as a script rather than as a row somebody types: the hook
is registered at a harness timeout, and lib/judge_latency.required_budget_s
REFUSES to size that timeout from a row with no p90 — `committed_data` currently
carries UNMEASURED_HOOK_CALLED_NOTE and the size test raises on it. Running this
once produces the observations that close it.

Sixteen DISTINCT samples, eight per arm, for the same reason as
sample_landing_discipline.py: what varies the judge's work here is the shape and
length of the payload it reads, so repeating two texts would measure one prompt
rather than the population the hook meets.

Every RAW_DATA fixture is SYNTHESIZED and obviously so — invented sentences,
`chat-fake-*` / `user-fake-*` identifiers. A sampler for this particular judge is
the last place that may carry a real record: committing one would reproduce, in
the repository, the exact defect the judge exists to catch.

ONE process, O_CREAT|O_EXCL pid lock, arms alternating inside it so machine-load
drift hits both equally — same discipline as sample_landing_discipline.py.

Run from this directory:  python3 sample_committed_data.py

Then feed the two latency lists to stats.py and record the resulting row in
scripts/lib/judge_latency.py (n / min / median / p90 / max + this file's output
as provenance), replacing the unmeasured placeholder.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
from agentctl import advisor  # noqa: E402

OUT = HERE / "committed-data-sample.json"
LOCK = Path("/tmp/cc-scratch/premise-loop/committed-data.lock")


def _chat_rows(pairs):
    return "\n".join(
        json.dumps(
            {"chat_id": f"chat-fake-{i:04d}", "user_id": f"user-fake-{i:04d}",
             "first_message": first, "response": reply},
            ensure_ascii=False,
        )
        for i, (first, reply) in enumerate(pairs, start=1)
    ) + "\n"


# Arm A: what a committed measurement artifact actually looked like — records
# captured from usage, in the formats such a dump arrives in.
RAW_DATA = [
    ("chats.jsonl", _chat_rows([
        ("Can you help me plan a birthday party for my daughter next weekend? "
         "She is turning six and likes dinosaurs.",
         "Of course. Let us start with the guest list and a dinosaur theme."),
        ("My laptop will not boot after last night's update and I have a "
         "deadline tomorrow morning.",
         "Let us try recovery mode and see whether the previous kernel starts."),
    ])),
    ("dialogs.jsonl", _chat_rows([
        ("I need to write a resignation letter but I do not want to burn any "
         "bridges with my current manager.",
         "A short, warm letter works best. Here is a structure to start from."),
        ("What is a good way to explain compound interest to a teenager who "
         "just got a first summer job?",
         "Start with a concrete number they earned themselves, then double it."),
    ])),
    ("export.csv",
     "chat_id,user_id,ts,first_message,response\n"
     "chat-fake-0101,user-fake-0101,2026-04-02T10:11:00Z,"
     '"Is it normal for my cat to sleep twenty hours a day after the move?",'
     '"Yes, a relocation usually raises a cat\'s sleep for a week or two."\n'
     "chat-fake-0102,user-fake-0102,2026-04-02T10:14:00Z,"
     '"Help me draft a message asking my landlord to fix the boiler again.",'
     '"Here is a firm but polite third request that references the earlier two."\n'),
    ("sessions.json", json.dumps({
        "sessions": [
            {"session_id": "sess-fake-0001", "user_id": "user-fake-0201",
             "messages": [
                 {"role": "user", "content": "My flight was cancelled and the "
                                             "airline is not answering. What "
                                             "compensation can I claim?"},
                 {"role": "assistant", "content": "That depends on the route and "
                                                  "the delay. Start with the "
                                                  "carrier's own claim form."},
             ]},
        ]
    }, ensure_ascii=False, indent=2)),
    ("prompts.txt",
     "chat-fake-0301\tPlease review this paragraph of my thesis introduction, "
     "I think the second sentence is doing too much work.\n"
     "chat-fake-0302\tHow do I tell a friend that I cannot lend them money "
     "again without ending the friendship?\n"
     "chat-fake-0303\tWhat should I pack for two weeks in a place where it "
     "rains every afternoon?\n"),
    ("first_messages.jsonl", "\n".join(
        json.dumps({"chat_id": f"chat-fake-04{i:02d}", "first_message": text},
                   ensure_ascii=False)
        for i, text in enumerate([
            "I keep getting a 500 from your API when the payload has a nested "
            "array — here is the exact body I am sending.",
            "Can you look over my cover letter? I am applying for a junior data "
            "role and I have no commercial experience yet.",
            "My doctor mentioned a test result I did not understand and I was "
            "too embarrassed to ask what it meant.",
        ], start=1)
    ) + "\n"),
    ("transcript.md",
     "## chat-fake-0501 (user-fake-0501)\n\n"
     "**user:** We are moving house in three weeks and I have not started "
     "packing. Where do I even begin with a two-bedroom flat?\n\n"
     "**assistant:** Start with the room you use least, and pack by category "
     "rather than by shelf.\n\n"
     "## chat-fake-0502 (user-fake-0502)\n\n"
     "**user:** Explain why my sourdough starter smells like acetone after I "
     "moved it to a colder kitchen.\n\n"
     "**assistant:** That smell is hooch — the starter is hungry and cold.\n"),
    ("queries.tsv",
     "user_id\tquery\n"
     "user-fake-0601\thow to apologise to a colleague for missing their demo\n"
     "user-fake-0602\tis it safe to take ibuprofen with my blood pressure "
     "medication\n"
     "user-fake-0603\tbest way to explain a two year employment gap in an "
     "interview\n"),
]

# Arm B: everything the prefilter also fires on and the judge must clear — the
# false positives that made the original remediation a hand audit. Same field
# names, same formats, no payload.
NOT_DATA = [
    ("aggregate.py",
     '"""Aggregate a chat export into per-day counts."""\n'
     "import collections\n"
     "import json\n\n\n"
     "def counts_by_day(path):\n"
     "    seen = collections.Counter()\n"
     "    with open(path) as handle:\n"
     "        for line in handle:\n"
     "            row = json.loads(line)\n"
     "            seen[(row['chat_id'][:8], len(row['first_message']))] += 1\n"
     "    return seen\n"),
    ("schema.json", json.dumps({
        "type": "object",
        "required": ["chat_id", "user_id", "first_message", "response"],
        "properties": {
            "chat_id": {"type": "string", "description": "opaque chat key"},
            "user_id": {"type": "string", "description": "opaque user key"},
            "first_message": {"type": "string", "description": "user's opening turn"},
            "response": {"type": "string", "description": "model reply"},
        },
    }, indent=2)),
    ("test_fixtures.jsonl", _chat_rows([
        ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do",
         "eiusmod tempor incididunt ut labore et dolore magna aliqua"),
        ("the quick brown fox jumps over the lazy dog and then again",
         "pack my box with five dozen liquor jugs, twice, for the test"),
    ])),
    ("metrics.csv",
     "day,chats,median_first_message_chars,p90_response_chars,resolved_rate\n"
     "2026-04-01,10412,187,940,0.71\n"
     "2026-04-02,10998,191,955,0.73\n"
     "2026-04-03,9877,183,921,0.70\n"),
    ("loader.sql",
     "-- Materialize the daily rollup the report reads.\n"
     "SELECT chat_id, user_id, length(first_message) AS first_message_chars,\n"
     "       length(response) AS response_chars\n"
     "FROM raw.chats\n"
     "WHERE ts >= CURRENT_DATE - INTERVAL '7' DAY;\n"),
    ("pipeline.yaml",
     "source:\n"
     "  table: raw.chats\n"
     "  columns: [chat_id, user_id, first_message, response, ts]\n"
     "sink:\n"
     "  table: marts.chat_daily\n"
     "  aggregate: [count, median_length, p90_length]\n"),
    ("README.md",
     "# Chat export\n\n"
     "Each line of `chats.jsonl` is one record with `chat_id`, `user_id`, "
     "`first_message` and `response`. The loader never writes these columns "
     "anywhere outside the warehouse; only the aggregate in `metrics.csv` is "
     "published, and the query that regenerates the raw rows is in "
     "`loader.sql`.\n"),
    ("conftest.py",
     "import pytest\n\n\n"
     "@pytest.fixture\n"
     "def chat_row():\n"
     '    """One synthetic record, shaped like the export the loader reads."""\n'
     "    return {\n"
     '        "chat_id": "chat-0000",\n'
     '        "user_id": "user-0000",\n'
     '        "first_message": "example question one two three",\n'
     '        "response": "example answer one two three",\n'
     "    }\n"),
]

ARMS = [
    ("raw_data", RAW_DATA, True),
    ("not_data", NOT_DATA, False),
]

LOCK.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.write(fd, str(os.getpid()).encode())
os.close(fd)
print(f"lock={LOCK} pid={os.getpid()}", flush=True)
try:
    out = {name: [] for name, _, _ in ARMS}
    for i in range(len(RAW_DATA)):
        for name, cases, want in ARMS:
            filename, text = cases[i]
            # The hook never shows the judge more than its own sample cap, so
            # neither does the sample: a latency measured over a whole file
            # would describe a call the hook cannot make.
            sample = text.encode("utf-8")[:4096].decode("utf-8", errors="ignore")
            t0 = time.monotonic()
            verdict, reason = advisor.judge_committed_data(
                sample, advisor.subprocess_runner,
                filename=filename, enabled=True, timeout=120,
            )
            row = {"i": i, "filename": filename, "verdict": bool(verdict),
                   "reason": reason, "ok": bool(verdict) == want,
                   "latency_s": round(time.monotonic() - t0, 2)}
            out[name].append(row)
            print(f"{name} {i} ({filename}): {verdict} {row['latency_s']}s", flush=True)
            OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("DONE")
finally:
    os.unlink(str(LOCK))
